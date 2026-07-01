"""Durable recovery journal for prompt admission and turn tracking.

Inspired by opencode's event-sourcing model (packages/core/src/event.ts):
important run boundaries are appended before/after execution.  Session files
remain the conversation source of truth; this journal detects an admitted run
that did not reach a terminal boundary and records tool diagnostics.

Event types:
  - prompt_admitted  — user's prompt, written BEFORE the agent loop runs
  - ai_response       — model reply + tool_calls, after backend returns
  - tool_call         — tool name + arguments, before dispatch
  - tool_result       — tool name + ok/error, after tool execution
  - turn_ended        — exit reason, written when the loop exits

The log lives in `.laintas/events.jsonl` (per-cwd). It is append-only and
synchronously flushed. ``event_id`` is the durable identity; ``seq`` is an
advisory ordering aid for one local writer and survives normal restarts.

Recovery: `last_incomplete_task()` returns the most recent prompt_admitted
without a matching turn_ended, or None if the last task completed cleanly.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import paths

_SEQ_BY_PATH: dict[str, int] = {}
_LOCK = threading.RLock()


def _log_path() -> Path:
    return Path(paths.project_dir()) / "events.jsonl"


def _next_seq(path: Path) -> int:
    """Return a process-safe, restart-safe advisory sequence for one log."""
    key = str(path.resolve())
    if key not in _SEQ_BY_PATH:
        last = 0
        try:
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    last = int(json.loads(line).get("seq") or 0)
                    if last:
                        break
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        except OSError:
            pass
        _SEQ_BY_PATH[key] = last
    _SEQ_BY_PATH[key] += 1
    return _SEQ_BY_PATH[key]


def append(event_type: str, **fields) -> int:
    """Append an event to the durable log. Returns the sequence number.

    Never raises — a logging failure is swallowed (the loop must not break
    because the event log is unwritable). Returns -1 on failure.
    """
    try:
        p = _log_path()
        with _LOCK:
            entry = {
                "event_id": uuid.uuid4().hex,
                "seq": _next_seq(p),
                "type": event_type,
                "ts": time.time(),
                **fields,
            }
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        return entry["seq"]
    except Exception:
        return -1


def last_incomplete_task() -> Optional[dict]:
    """Return the most recent prompt_admitted event without a turn_ended.

    Reads the log backwards. If the last non-tool_result event is a
    prompt_admitted or ai_response (i.e., no turn_ended followed it),
    returns that prompt_admitted event. Returns None if the last task
    completed cleanly or the log is empty/unreadable.
    """
    try:
        p = _log_path()
        if not p.exists():
            return None
        lines = p.read_text(encoding="utf-8").splitlines()
        pending: dict[str, dict] = {}
        legacy_pending = None
        order: dict[str, int] = {}
        for index, line in enumerate(lines):
            try:
                evt = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if evt.get("type") == "prompt_admitted":
                run_id = str(evt.get("run_id") or "")
                if run_id:
                    pending[run_id] = evt
                    order[run_id] = index
                else:
                    legacy_pending = evt
            elif evt.get("type") == "turn_ended":
                run_id = str(evt.get("run_id") or "")
                if run_id:
                    pending.pop(run_id, None)
                    order.pop(run_id, None)
                else:
                    legacy_pending = None
        candidates = [
            (order.get(run_id, -1), evt)
            for run_id, evt in pending.items()
        ]
        if legacy_pending is not None:
            candidates.append((len(lines), legacy_pending))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None
    except Exception:
        return None


def acknowledge_incomplete(event: dict, reason: str = "crash_recovered") -> int:
    """Close a recovered admission so it is not offered on every restart."""
    fields = {
        "reason": reason,
        "session_id": str((event or {}).get("session_id") or ""),
        "recovered_event_id": str((event or {}).get("event_id") or ""),
    }
    run_id = str((event or {}).get("run_id") or "")
    if run_id:
        fields["run_id"] = run_id
    return append("turn_ended", **fields)


def owner_process_is_alive(event: dict) -> bool:
    """Best-effort guard against recovering another live CLI process's run."""
    host = str((event or {}).get("hostname") or "")
    if host and host != socket.gethostname():
        return True
    try:
        pid = int((event or {}).get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def clear() -> None:
    """Truncate the event log (called on /clear or explicit reset)."""
    try:
        p = _log_path()
        with _LOCK:
            if p.exists():
                p.write_text("", encoding="utf-8")
            _SEQ_BY_PATH.pop(str(p.resolve()), None)
    except Exception:
        pass
