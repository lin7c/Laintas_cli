from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import uuid
from typing import Optional

import paths
import session_lifecycle

_LAST_ERROR = ""
_LAST_WRITE_FINGERPRINTS: dict[str, str] = {}

CONTINUABLE_REASONS = {
    "max_loops",
    "max_loops_wrapup",
    "interrupted",
    "backend_error",
    "provider_error",
    "silent_failure",
    "truncated",
    "parse_failed",
    "repair_gave_up",
    "parse_gave_up",
    "repetition",
    "warning_force_exit",
    "staleness",
    "aborted",
    "crash_recovery",
    "runtime_error",
}

def _session_key(cwd: str) -> str:
    return hashlib.sha256(str(cwd).encode()).hexdigest()[:16]


def _safe_id(value: object) -> str:
    raw = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", raw)[:64]
    return safe or uuid.uuid4().hex[:16]


def _terminal_id(value: object = None) -> str:
    return _safe_id(value or getattr(paths, "TERMINAL_ID", "terminal-default"))


def _agent_ns(agent_id: str) -> str:
    """Filename namespace segment for one agent's live/current session files.

    "primary" keeps the legacy filenames (zero migration); every other
    persistent agent gets its own ``__ag_<id>_`` segment so its live copies
    and current pointer can never be recovered as another agent's session.
    The primary patterns never match agent-scoped names (``_live`` would
    have to follow the key directly, but ``__ag_`` is there instead).
    """
    aid = str(agent_id or "").strip()
    if not aid or aid == "primary":
        return ""
    return f"__ag_{_safe_id(aid)}"


def _current_path(cwd: str, agent_id: str = "primary"):
    return paths.SESSIONS_DIR / (
        f"{_session_key(cwd)}{_agent_ns(agent_id)}"
        f"_current_{_terminal_id()}.json")


def _legacy_current_path(cwd: str):
    return paths.SESSIONS_DIR / f"{_session_key(cwd)}_current.json"


def _session_path(cwd: str, session_id: str, agent_id: str = "primary"):
    return paths.SESSIONS_DIR / (
        f"{_session_key(cwd)}{_agent_ns(agent_id)}"
        f"_live_{_safe_id(session_id)}.json")


def _atomic_write_json(dest, payload: dict) -> None:
    _atomic_write_json_if_changed(dest, payload, skip_if_unchanged=False)


#: Top-level fields that change on every save without changing the session.
_VOLATILE_KEYS = ("timestamp", "updated_at")


class SerializedJson:
    """A payload serialized once: its fingerprint, and the chunks to write.

    The chunks are what ``json.dumps`` joins internally. Keeping them apart
    means a 15MB session is never copied into one string again just to put
    two timestamps in front of it.
    """

    __slots__ = ("head", "chunks", "fingerprint")

    def __init__(self, head: str, chunks: list, fingerprint: str):
        self.head = head
        self.chunks = chunks
        self.fingerprint = fingerprint

    def write_to(self, fh) -> None:
        if self.head:
            fh.write(self.head)
            first = self.chunks[0][1:]           # drop the body's own "{"
            if first != "}":
                fh.write(",")
            fh.write(first)
            fh.writelines(self.chunks[1:])
        else:
            fh.writelines(self.chunks)

    def text(self) -> str:
        import io
        buf = io.StringIO()
        self.write_to(buf)
        return buf.getvalue()


_ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))


def serialize_session_json(payload: dict,
                           volatile: tuple = _VOLATILE_KEYS) -> SerializedJson:
    """Serialize *payload* once, fingerprinting everything but *volatile*.

    The fingerprint lets a rewrite that only moved the clock be skipped. It
    used to be a second, key-sorted ``json.dumps`` of a deep copy — on a
    1,000-message session ~0.9s per file, paid for the live copy and again for
    the current pointer after every turn. Now the serialization that is written
    is the one that is hashed. The volatile fields are serialized apart and
    written first, which keeps them ahead of ``chat_history`` for
    ``agent_loop._read_session_header``.
    """
    head = {k: payload[k] for k in volatile if k in payload}
    rest = {k: v for k, v in payload.items() if k not in head}
    # iterencode(_one_shot=True) is exactly what json.dumps runs before its
    # final "".join — the C encoder's chunk list.
    chunks = list(_ENCODER.iterencode(rest, _one_shot=True))
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.encode("utf-8"))
    head_text = _ENCODER.encode(head)[:-1] if head else ""
    return SerializedJson(head_text, chunks, digest.hexdigest())


def _fingerprint_payload(payload: dict) -> str:
    return serialize_session_json(payload).fingerprint


def _atomic_write_json_if_changed(
        dest, payload: dict, *, skip_if_unchanged: bool = True,
        serialized: Optional[SerializedJson] = None) -> bool:
    cwd = payload.get("cwd") or os.getcwd()
    with session_lifecycle.guard(cwd):
        if session_lifecycle.is_deleted(cwd, payload):
            return False
        return _write_session_json(dest, payload, skip_if_unchanged=skip_if_unchanged,
                                   serialized=serialized)


def write_json_atomically(dest, serialized: SerializedJson) -> None:
    """Write *serialized* to *dest* through a fsynced temp file and a rename."""
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            serialized.write_to(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(dest))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_session_json(dest, payload: dict, *, skip_if_unchanged: bool = True,
                        serialized: Optional[SerializedJson] = None) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cache_key = str(dest)
    serialized = serialized or serialize_session_json(payload)
    if skip_if_unchanged:
        if (_LAST_WRITE_FINGERPRINTS.get(cache_key) == serialized.fingerprint
                and dest.exists()):
            return False
    write_json_atomically(dest, serialized)
    if skip_if_unchanged:
        _LAST_WRITE_FINGERPRINTS[cache_key] = serialized.fingerprint
    return True


def _record_error(message: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = str(message or "")


def consume_last_error() -> str:
    """Return and clear the latest recoverable persistence warning."""
    global _LAST_ERROR
    message = _LAST_ERROR
    _LAST_ERROR = ""
    return message


def _recover_latest_live(cwd: str,
                          agent_id: str = "primary") -> Optional[dict]:
    """Recover the newest valid unclosed live copy for a working directory."""
    pattern = f"{_session_key(cwd)}{_agent_ns(agent_id)}_live_*.json"
    try:
        candidates = sorted(
            paths.SESSIONS_DIR.glob(pattern),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            owner = data.get("terminal_id") or data.get("instance_id")
            if (data.get("cwd") == cwd and not session_lifecycle.is_deleted(cwd, data)
                    and not data.get("closed_at")
                    and owner == _terminal_id()
                    and str(data.get("agent_id") or "primary")
                    == str(agent_id or "primary")):
                return data
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


def is_continuable_reason(reason: str) -> bool:
    return str(reason or "") in CONTINUABLE_REASONS


def create_session(cwd: str, state: Optional[dict] = None,
                   chat_history: Optional[list] = None,
                   agent_id: str = "primary") -> dict:
    now = time.time()
    session_id = _safe_id((state or {}).get("_session_id") or uuid.uuid4().hex[:16])
    # The runtime, autosave and lease must name the same session from its
    # first turn; otherwise the first autosave invents a second unleased ID.
    if isinstance(state, dict):
        state["_session_id"] = session_id
    session = {
        "id": session_id,
        "session_id": session_id,
        "kind": "live",
        # Travels inside the payload: save_session re-derives the file
        # namespace from it, so sync/close callers never need to pass it.
        # Legacy files without it are primary's.
        "agent_id": str(agent_id or "primary"),
        "instance_id": _terminal_id(),
        "terminal_id": _terminal_id(),
        "cwd": cwd,
        "created_at": now,
        "updated_at": now,
        "timestamp": now,
        "closed_at": None,
        "status": "idle",
        "objective": str((state or {}).get("objective") or "").strip(),
        "active_work_id": str((state or {}).get("_work_id") or ""),
        "last_user_input": "",
        "last_original_input": "",
        "last_exit_reason": "",
        "pending_continuation": False,
        "turn_count": 0,
        "state": copy.deepcopy(state or {}),
        "agent_state": copy.deepcopy(state or {}),
        "tasks": [],
        # Last on purpose, and it must stay last: agent_loop._read_session_header
        # answers the session walk from a bounded head read, which only works
        # while every field it needs precedes the whole conversation. A live
        # copy is the biggest file of the lot and the one that optimization
        # exists for.
        "chat_history": copy.deepcopy(chat_history or []),
    }
    if isinstance(session["state"], dict):
        session["state"]["_session_id"] = session_id
        session["agent_state"] = copy.deepcopy(session["state"])
    try:
        import workgraph
        active = workgraph.get_active_work(cwd=cwd, session_id=session_id)
        if active:
            session["active_work_id"] = active["id"]
    except Exception:
        pass
    save_session(session)
    return session


def load_current_session(cwd: str, agent_id: str = "primary") -> Optional[dict]:
    path = _current_path(cwd, agent_id)
    try:
        if not path.exists() and str(agent_id or "primary") == "primary":
            legacy = _legacy_current_path(cwd)
            if legacy.exists():
                try:
                    os.replace(str(legacy), str(path))
                except OSError:
                    pass
        if not path.exists():
            # This applies to every agent: a missing pointer means the prior
            # session was intentionally closed, not that recovery is needed.
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("cwd") != cwd or data.get("closed_at")
                or str(data.get("agent_id") or "primary") != str(agent_id or "primary")
                or session_lifecycle.is_deleted(cwd, data)):
            return None
        data.setdefault("id", data.get("session_id") or uuid.uuid4().hex[:16])
        data.setdefault("session_id", data.get("id"))
        data.setdefault("kind", "live")
        data.setdefault("terminal_id", _terminal_id())
        data["instance_id"] = data["terminal_id"]
        data.setdefault("chat_history", [])
        data.setdefault("state", data.get("agent_state") or {})
        data.setdefault("agent_state", data.get("state") or {})
        data.setdefault("pending_continuation", False)
        data.setdefault("last_exit_reason", "")
        data.setdefault("status", "idle")
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        # Preserve the unreadable pointer for diagnosis, then fall back to the
        # independently written per-session live copy.
        try:
            if path.exists():
                corrupt = path.with_name(
                    f"{path.name}.corrupt-{int(time.time())}")
                os.replace(str(path), str(corrupt))
        except OSError:
            pass
        recovered = _recover_latest_live(cwd, agent_id)
        if recovered is not None:
            _record_error(
                f"Current session index was unreadable ({exc}); recovered its live backup.")
            try:
                _atomic_write_json(path, recovered)
            except OSError:
                return recovered
            # Re-enter the normal validation/default path now that the pointer
            # has been rebuilt.
            return load_current_session(cwd, agent_id=agent_id) or recovered
        _record_error(f"Current session could not be loaded: {exc}")
        return None


def ensure_current_session(cwd: str, state: Optional[dict] = None,
                           chat_history: Optional[list] = None,
                           agent_id: str = "primary") -> dict:
    return (load_current_session(cwd, agent_id)
            or create_session(cwd, state, chat_history, agent_id))


def save_session(session: dict) -> None:
    if not session:
        return
    now = time.time()
    session["updated_at"] = now
    session["timestamp"] = now
    session_id = _safe_id(session.get("session_id") or session.get("id"))
    session["id"] = session_id
    session["session_id"] = session_id
    session["terminal_id"] = _terminal_id(session.get("terminal_id"))
    session["instance_id"] = session["terminal_id"]
    state = session.get("state")
    if state is None:
        state = session.get("agent_state")
    if state is None:
        state = {}
    if isinstance(state, dict):
        state = copy.deepcopy(state)
        state["_session_id"] = session_id
        session["state"] = state
        session["agent_state"] = copy.deepcopy(state)
    # Keep the conversation last on every rewrite, not just on the files
    # create_session made: agent_loop._read_session_header can only answer the
    # session walk from a bounded head read while all of its fields precede
    # chat_history, and sessions written before that ordering existed are
    # exactly the long-lived, largest ones.
    if "chat_history" in session and next(reversed(session)) != "chat_history":
        session["chat_history"] = session.pop("chat_history")
    cwd = session.get("cwd") or os.getcwd()
    agent_id = str(session.get("agent_id") or "primary")
    paths.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    # The live copy and the current pointer hold the same bytes: serialize once.
    serialized = serialize_session_json(session)
    _atomic_write_json_if_changed(
        _session_path(cwd, session_id, agent_id), session, serialized=serialized)
    # D1 (bughunt): the current-pointer update is a read-compare-write/unlink
    # that used to run unguarded. A concurrent close+save could interleave
    # (close reads current=X, save writes current=Y, close unlinks current)
    # and lose the new session's pointer — or resurrect a closed one. The
    # session file write above is safe (unique per-id path); only the shared
    # pointer needs the lifecycle guard, which is reentrant per-thread.
    with session_lifecycle.guard(cwd):
        current = _current_path(cwd, agent_id)
        if session.get("closed_at"):
            try:
                existing = json.loads(current.read_text(encoding="utf-8")) if current.exists() else {}
                if existing.get("session_id") == session_id or existing.get("id") == session_id:
                    current.unlink(missing_ok=True)
                legacy = _legacy_current_path(cwd)
                if legacy.exists():
                    existing = json.loads(legacy.read_text(encoding="utf-8"))
                    if existing.get("session_id") == session_id or existing.get("id") == session_id:
                        legacy.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            _atomic_write_json_if_changed(current, session, serialized=serialized)


def close_session(session: dict) -> dict:
    if not session:
        return session
    session["closed_at"] = time.time()
    session["status"] = "closed"
    session["pending_continuation"] = False
    save_session(session)
    return session


def sync_runtime(session: dict, state: dict, chat_history: list, *, cwd: str = None,
                 objective: str = None, last_user_input: str = None,
                 exit_reason: str = None, tasks: list = None) -> dict:
    if not session:
        session = create_session(cwd or os.getcwd(), state, chat_history)
    if cwd:
        session["cwd"] = cwd
    if state is not None:
        session["state"] = copy.deepcopy(state)
        session["agent_state"] = copy.deepcopy(state)
        if state.get("_work_id"):
            session["active_work_id"] = str(state["_work_id"])
    if chat_history is not None:
        session["chat_history"] = copy.deepcopy(chat_history)
        session["turn_count"] = len([m for m in chat_history if isinstance(m, dict) and m.get("role") == "user"])
    if objective is not None and str(objective).strip():
        session["objective"] = str(objective).strip()
    elif state and str(state.get("objective") or "").strip():
        session["objective"] = str(state.get("objective") or "").strip()
    if last_user_input is not None:
        session["last_user_input"] = str(last_user_input)
        session["last_original_input"] = str(last_user_input)
    if exit_reason is not None:
        session["last_exit_reason"] = str(exit_reason or "")
        pending = is_continuable_reason(exit_reason)
        session["pending_continuation"] = pending
        session["status"] = str(exit_reason or "idle") if pending else "idle"
    if tasks is not None:
        session["tasks"] = copy.deepcopy(tasks)
    try:
        import workgraph
        active = workgraph.get_active_work(cwd=session.get("cwd") or cwd,
                                          session_id=session.get("session_id"))
        session["active_work_id"] = active["id"] if active else ""
    except Exception:
        pass
    save_session(session)
    return session
