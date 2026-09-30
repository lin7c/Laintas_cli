"""Per-file reentrant thread/process locks with an enforced lock order.

Acquire STORE (handoff/profile/adoption) -> SESSION (lifecycle) -> CLAIM
(lease/GC). Different locks of the same rank follow canonical path order.
Reentering an already held file is allowed. Never unlink a lock sidecar while
it may be held: a replacement inode would be a different process lock.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import weakref

STORE = 10
SESSION = 20
CLAIM = 30
_registry_mutex = threading.Lock()
_locks = weakref.WeakValueDictionary()
_local = threading.local()


class LockOrderError(RuntimeError):
    pass


def _after_fork():
    global _registry_mutex, _locks, _local
    _registry_mutex = threading.Lock()
    _locks = weakref.WeakValueDictionary()
    _local = threading.local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


@contextmanager
def guard(path, *, rank=STORE):
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    if key in held:
        if held[key][0] != rank:
            raise LockOrderError("The same lock was requested with a different rank")
        yield
        return
    order = (rank, key)
    if held and order <= max(held.values()):
        raise LockOrderError("Lock order must be STORE -> SESSION -> CLAIM, then canonical path order")
    # This mutex only protects the lock table. Never wait on a file or run a
    # caller's critical section while holding it.
    with _registry_mutex:
        mutex = _locks.get(key)
        if mutex is None:
            mutex = threading.RLock()
            _locks[key] = mutex
    with mutex:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b" ")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[key] = order
            try:
                yield
            finally:
                held.pop(key)
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
