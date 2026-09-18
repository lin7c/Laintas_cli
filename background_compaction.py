"""Agent-owned, cancellable compaction workers. Only the caller commits results."""
from __future__ import annotations

import contextvars
import functools
import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field


class Cancellation(threading.Event):
    """Explicit cancel, plus a STALL deadline that each fold pushes forward.

    The deadline used to be a total budget for the whole job. Summarizing a
    head takes one generate + one review call per slice, so a thread big enough
    to need four slices needed more wall clock than the budget allowed and was
    killed every single time — the compaction that was supposed to shrink it
    never landed, and the thread only grew, which made the next attempt need
    even more slices. Timing each fold instead bounds a hung backend (the thing
    the deadline is actually for) without punishing a job for being long.
    """

    def __init__(self, parent, timeout):
        super().__init__()
        self.parent = parent
        self.timeout = max(1, int(timeout))
        self.deadline = time.monotonic() + self.timeout

    def progress(self):
        """A fold completed: the worker is alive, so restart its stall clock."""
        self.deadline = time.monotonic() + self.timeout

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

# Coordinators handed over by a finished run, by owner. A speculative summary
# outlives the turn that started it: the work is owned by the agent, not by one
# run of its loop, and killing it at every turn boundary is what made short
# turns burn a slice and leave nothing behind.
_MAX_PARKED = 32
_parked: "OrderedDict[tuple, Coordinator]" = OrderedDict()

# Completed folds, keyed by the rolling fingerprint of everything folded so far
# (see `fold_fingerprint`). A cancelled or stalled attempt keeps whatever it
# finished, so the next one resumes at the first unfolded slice instead of
# paying for slice 1 again. Chaining the fingerprint means a resumed attempt is
# byte-identical to the one it continues, and a changed slice invalidates every
# fold after it without any extra bookkeeping.
_MAX_FOLDS = 64
_folds: "OrderedDict[str, str]" = OrderedDict()


def fold_fingerprint(previous: str, *parts) -> str:
    """Chain `parts` onto the fingerprint of the folds already applied."""
    digest = hashlib.sha256(previous.encode("utf-8", "replace"))
    for part in parts:
        digest.update(b"\x00")
        digest.update(str(part).encode("utf-8", "replace"))
    return digest.hexdigest()


def remember_fold(fingerprint: str, summary: str) -> None:
    if not fingerprint or not summary:
        return
    with _lock:
        _folds[fingerprint] = summary
        _folds.move_to_end(fingerprint)
        while len(_folds) > _MAX_FOLDS:
            _folds.popitem(last=False)


def recall_fold(fingerprint: str):
    with _lock:
        summary = _folds.get(fingerprint)
        if summary is not None:
            _folds.move_to_end(fingerprint)
    return summary


def forget_folds() -> None:
    with _lock:
        _folds.clear()


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
                # A summary that landed as the stall deadline expired is a
                # finished summary. Reading the deadline here threw such a
                # result away and made the main loop redo the whole head.
                job.cancelled = job.cancel.requested() or job.summary is None
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
        self.unpark()

    def unpark(self):
        with _lock:
            for owner, parked in list(_parked.items()):
                if parked is self:
                    _parked.pop(owner, None)

    def park(self):
        """Leave unfinished work for the next run instead of killing it."""
        job = self.job
        if job is None:
            return
        if job.cancel.requested():
            self.close()
            return
        with _lock:
            previous = _parked.pop(job.owner, None)
            _parked[job.owner] = self
            stale = [_parked.popitem(last=False)[1]
                     for _ in range(max(0, len(_parked) - _MAX_PARKED))]
        if previous is not None and previous is not self:
            previous.close()
        for coordinator in stale:
            coordinator.close()

    def adopt(self, owner):
        """Take over the summary a previous run of this agent left behind."""
        if self.job is not None:
            return False
        with _lock:
            parked = _parked.pop(owner, None)
        if parked is None or parked is self or parked.job is None:
            return False
        job = parked.job
        parked.job = None
        if job.cancel.requested() or (job.done.is_set() and job.cancelled):
            _release(job)
            return False
        self.job = job
        self.retry_at = max(self.retry_at, parked.retry_at)
        self.last_signature = parked.last_signature
        return True


def scoped(function):
    """Hand speculative work to the next run; cancel only what is unusable."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        coordinator = Coordinator()
        token = current.set(coordinator)
        try:
            return function(*args, **kwargs)
        finally:
            coordinator.park()
            current.reset(token)
    return wrapped
