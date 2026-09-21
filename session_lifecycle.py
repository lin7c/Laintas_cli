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


# path -> ((ino, mtime_ns, size), ids). Session listing probes is_deleted
# once per session file; re-reading and re-parsing the registry each time
# multiplied the reads of every resume probe. mark_deleted lands a new inode
# via os.replace, so a stale hit is not possible.
_deleted_cache: dict = {}


def deleted_ids(cwd):
    path = paths.SESSIONS_DIR / f"{_key(cwd)}_deleted.json"
    try:
        st = path.stat()
    except OSError:                       # absent (the common case) or unreadable
        return set()
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    cached = _deleted_cache.get(str(path))
    if cached is not None and cached[0] == sig:
        return set(cached[1])
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # A torn write (crash mid-save, power loss) must not brick every
        # future session save: quarantine the corrupt file so it can be
        # inspected, then treat deletion memory as empty.
        try:
            path.rename(path.with_name(
                f"{path.name}.corrupt-{uuid.uuid4().hex[:8]}"))
        except OSError:
            pass
        return set()
    if isinstance(payload, list):
        ids = frozenset(i for i in payload if isinstance(i, str))
    else:
        # A dict payload is not a format we ever wrote; keys are not identities.
        ids = frozenset()
    _deleted_cache[str(path)] = (sig, ids)
    return set(ids)


def is_deleted(cwd, blob):
    return identity(blob) in deleted_ids(cwd)


def mark_deleted(cwd, ids):
    """Caller holds guard; persist before unlinking so partial failure is safe."""
    dest = paths.SESSIONS_DIR / f"{_key(cwd)}_deleted.json"
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(sorted(deleted_ids(cwd) | set(ids))))
            # D7 (bughunt): fsync before replace. The session files are
            # unlinked right after this returns; if the deletion registry
            # is still in the page cache when power dies, the unlinks are
            # durable but the record of them is not — deleted sessions
            # resurrect on the next boot. Cost: one fsync per close.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
