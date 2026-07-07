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

_LAST_ERROR = ""
_LAST_WRITE_FINGERPRINTS: dict[str, str] = {}

CONTINUABLE_REASONS = {
    "max_loops",
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
}

def _session_key(cwd: str) -> str:
    return hashlib.sha256(str(cwd).encode()).hexdigest()[:16]


def _safe_id(value: object) -> str:
    raw = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", raw)[:64]
    return safe or uuid.uuid4().hex[:16]


def _instance_id(value: object = None) -> str:
    return _safe_id(value or getattr(paths, "INSTANCE_ID", "default"))


def _current_path(cwd: str):
    return paths.SESSIONS_DIR / f"{_session_key(cwd)}_current_{_instance_id()}.json"


def _legacy_current_path(cwd: str):
    return paths.SESSIONS_DIR / f"{_session_key(cwd)}_current.json"


def _session_path(cwd: str, session_id: str):
    return paths.SESSIONS_DIR / f"{_session_key(cwd)}_live_{_safe_id(session_id)}.json"


def _atomic_write_json(dest, payload: dict) -> None:
    _atomic_write_json_if_changed(dest, payload, skip_if_unchanged=False)


def _fingerprint_payload(payload: dict) -> str:
    stable = copy.deepcopy(payload)
    if isinstance(stable, dict):
        stable.pop("timestamp", None)
        stable.pop("updated_at", None)
    raw = json.dumps(
        stable, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_write_json_if_changed(
        dest, payload: dict, *, skip_if_unchanged: bool = True) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cache_key = str(dest)
    if skip_if_unchanged:
        fp = _fingerprint_payload(payload)
        if _LAST_WRITE_FINGERPRINTS.get(cache_key) == fp:
            return False
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(dest))
        if skip_if_unchanged:
            _LAST_WRITE_FINGERPRINTS[cache_key] = fp
        return True
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _record_error(message: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = str(message or "")


def consume_last_error() -> str:
    """Return and clear the latest recoverable persistence warning."""
    global _LAST_ERROR
    message = _LAST_ERROR
    _LAST_ERROR = ""
    return message


def _recover_latest_live(cwd: str) -> Optional[dict]:
    """Recover the newest valid unclosed live copy for a working directory."""
    pattern = f"{_session_key(cwd)}_live_*.json"
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
            if data.get("cwd") == cwd and not data.get("closed_at"):
                return data
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


def is_continuable_reason(reason: str) -> bool:
    return str(reason or "") in CONTINUABLE_REASONS


def create_session(cwd: str, state: Optional[dict] = None, chat_history: Optional[list] = None) -> dict:
    now = time.time()
    session_id = _safe_id((state or {}).get("_session_id") or uuid.uuid4().hex[:16])
    session = {
        "id": session_id,
        "session_id": session_id,
        "kind": "live",
        "instance_id": _instance_id(),
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
        "chat_history": copy.deepcopy(chat_history or []),
        "state": copy.deepcopy(state or {}),
        "agent_state": copy.deepcopy(state or {}),
        "tasks": [],
    }
    if isinstance(session["state"], dict):
        session["state"]["_session_id"] = session_id
        session["agent_state"] = copy.deepcopy(session["state"])
    try:
        import workgraph
        active = workgraph.get_active_work(cwd=cwd)
        if active:
            session["active_work_id"] = active["id"]
    except Exception:
        pass
    save_session(session)
    return session


def load_current_session(cwd: str) -> Optional[dict]:
    path = _current_path(cwd)
    try:
        if not path.exists():
            legacy = _legacy_current_path(cwd)
            if legacy.exists():
                try:
                    os.replace(str(legacy), str(path))
                except OSError:
                    pass
            if not path.exists():
                # A missing current pointer is the durable signal that the prior
                # session was intentionally closed. Recover live copies only when
                # the pointer exists but is corrupt; otherwise an older orphan can
                # resurrect after /q or /new.
                return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("cwd") != cwd or data.get("closed_at"):
            return None
        data.setdefault("id", data.get("session_id") or uuid.uuid4().hex[:16])
        data.setdefault("session_id", data.get("id"))
        data.setdefault("kind", "live")
        data.setdefault("instance_id", _instance_id())
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
        recovered = _recover_latest_live(cwd)
        if recovered is not None:
            _record_error(
                f"Current session index was unreadable ({exc}); recovered its live backup.")
            try:
                _atomic_write_json(path, recovered)
            except OSError:
                return recovered
            # Re-enter the normal validation/default path now that the pointer
            # has been rebuilt.
            return load_current_session(cwd) or recovered
        _record_error(f"Current session could not be loaded: {exc}")
        return None


def ensure_current_session(cwd: str, state: Optional[dict] = None, chat_history: Optional[list] = None) -> dict:
    return load_current_session(cwd) or create_session(cwd, state, chat_history)


def save_session(session: dict) -> None:
    if not session:
        return
    now = time.time()
    session["updated_at"] = now
    session["timestamp"] = now
    session_id = _safe_id(session.get("session_id") or session.get("id"))
    session["id"] = session_id
    session["session_id"] = session_id
    session["instance_id"] = _instance_id(session.get("instance_id"))
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
    cwd = session.get("cwd") or os.getcwd()
    paths.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json_if_changed(_session_path(cwd, session_id), session)
    current = _current_path(cwd)
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
        _atomic_write_json_if_changed(current, session)


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
        active = workgraph.get_active_work(cwd=session.get("cwd") or cwd)
        session["active_work_id"] = active["id"] if active else ""
    except Exception:
        pass
    save_session(session)
    return session
