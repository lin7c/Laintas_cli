"""Run-owned, cancellable compaction workers. Only the caller commits results."""
from __future__ import annotations

import contextvars
import functools
import threading
import time
from dataclasses import dataclass, field


class Cancellation(threading.Event):
    def __init__(self, parent, timeout):
        super().__init__()
        self.parent = parent
        self.deadline = time.monotonic() + timeout

    def is_set(self):
        return self.requested() or time.monotonic() >= self.deadline

    def requested(self):
        return super().is_set() or (self.parent is not None and self.parent.is_set())

    def wait(self, timeout=None):
        until = min(self.deadline, time.monotonic() + timeout) if timeout is not None else self.deadline
        while not self.is_set():
            remaining = until - time.monotonic()
            if remaining <= 0:
                return False
            super().wait(min(0.05, remaining))
        return True


@dataclass
class Job:
    owner: tuple
    key: tuple
    prefix: list
    previous: str | None
    cancel: Cancellation
    done: threading.Event = field(default_factory=threading.Event)
    summary: str | None = None
    cancelled: bool = False
    thread: threading.Thread | None = None


_lock = threading.Lock()
_owners: dict[tuple, Job] = {}
# Bound speculative requests across concurrently running agents as well.
_MAX_WORKERS = 2
current = contextvars.ContextVar("background_compaction", default=None)


def status(owner):
    with _lock:
        job = _owners.get(owner)
    if job is None:
        return "idle"
    if job.cancel.requested() or job.cancelled:
        return "cancelling"
    return "ready" if job.done.is_set() else "running"


def _release(job):
    with _lock:
        if _owners.get(job.owner) is job:
            _owners.pop(job.owner, None)


def cancel_owner(owner, timeout=1.0):
    """Invalidate before manual compaction or replacing a session's history."""
    with _lock:
        job = _owners.get(owner)
    if job is None:
        return True
    job.cancel.set()
    if job.thread is not None and job.thread.ident is not None:
        job.thread.join(timeout)
    else:
        job.done.wait(timeout)
    if job.done.is_set():
        _release(job)
    return job.done.is_set()


class Coordinator:
    def __init__(self):
        self.job = None
        self.retry_at = 0.0
        self.last_signature = None

    def start(self, *, owner, key, prefix, previous, worker, parent,
              timeout, cooldown, signature):
        if (self.job is not None or time.monotonic() < self.retry_at
                or signature == self.last_signature):
            return False
        job = Job(owner, key, prefix, previous, Cancellation(parent, timeout))
        with _lock:
            if owner in _owners or sum(not j.done.is_set() for j in _owners.values()) >= _MAX_WORKERS:
                return False
            _owners[owner] = job
        self.job = job
        self.last_signature = signature
        self.retry_at = time.monotonic() + cooldown
        context = contextvars.copy_context()

        def run():
            try:
                if not job.cancel.is_set():
                    job.summary = worker(job)
            except Exception:
                job.summary = None
            finally:
                job.cancelled = job.cancel.is_set()
                job.done.set()
                if job.cancelled:
                    _release(job)

        job.thread = threading.Thread(target=lambda: context.run(run), daemon=True,
                                      name="context-compaction")
        try:
            job.thread.start()
        except Exception:
            self.job = None
            _release(job)
            return False
        return True

    def take(self, cooldown):
        job = self.job
        if job is None or not job.done.is_set():
            return None
        job.thread.join()
        self.job = None
        self.retry_at = time.monotonic() + cooldown
        _release(job)
        return job

    def wait(self, interrupt):
        job = self.job
        if job is None:
            return True
        # A cooperative backend normally stops in 100ms. Never let a broken
        # backend block the main loop indefinitely after its deadline.
        cancelled_at = None
        while not job.done.wait(0.05):
            if interrupt is not None and interrupt.is_set():
                job.cancel.set()
                return False
            if job.cancel.is_set():
                cancelled_at = cancelled_at or time.monotonic()
                if time.monotonic() - cancelled_at >= 2:
                    return False
        return True

    def close(self):
        if self.job is not None:
            cancel_owner(self.job.owner)


def scoped(function):
    """Cancel speculative work on every run exit, including exceptions."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        coordinator = Coordinator()
        token = current.set(coordinator)
        try:
            return function(*args, **kwargs)
        finally:
            coordinator.close()
            current.reset(token)
    return wrapped
