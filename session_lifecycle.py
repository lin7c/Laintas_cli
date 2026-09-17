"""Serialize session selection/deletion/writes and remember deleted identities."""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import threading
import uuid

import paths

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_mutex = threading.RLock()
_local = threading.local()


def _key(cwd):
    return hashlib.sha256(str(cwd).encode()).hexdigest()[:16]


def identity(blob):
    state = blob.get("state") or blob.get("agent_state") or {}
    sid = str(blob.get("session_id") or state.get("_session_id") or "")
    parent = str(blob.get("parent_session_id") or blob.get("fork_parent_session_id") or "")
    if blob.get("kind") == "fork" and sid and sid == parent:
        return "legacy-fork-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(blob.get("id") or "fork"))[:48]
    return sid or ("snapshot-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(blob["id"]))[:48]
                   if blob.get("id") else "")


@contextmanager
def guard(cwd):
    # flock alone does not serialize threads reliably; nested saves share the
    # outer process lock instead of opening a second descriptor and deadlocking.
    with _mutex:
        key = (str(paths.SESSIONS_DIR), str(cwd))
        held = getattr(_local, "held", set())
        if key in held:
            yield
            return
        paths.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        with (paths.SESSIONS_DIR / f"{_key(cwd)}_lifecycle.lock").open("a") as lock:
            if os.name == "nt":
                if lock.tell() == 0:
                    lock.write(" ")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX)
            _local.held = held | {key}
            try:
                yield
            finally:
                _local.held = held
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)


def deleted_ids(cwd):
    path = paths.SESSIONS_DIR / f"{_key(cwd)}_deleted.json"
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return set()


def is_deleted(cwd, blob):
    return identity(blob) in deleted_ids(cwd)


def mark_deleted(cwd, ids):
    """Caller holds guard; persist before unlinking so partial failure is safe."""
    dest = paths.SESSIONS_DIR / f"{_key(cwd)}_deleted.json"
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(sorted(deleted_ids(cwd) | set(ids))), encoding="utf-8")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
