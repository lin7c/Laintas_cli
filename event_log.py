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
  - critic_assessment — periodic quality score + on_track flag

The log lives in `.laintas/events.jsonl` (per-cwd). It is append-only and
synchronously flushed. ``event_id`` is the durable identity; ``seq`` is an
advisory ordering aid for one local writer and survives normal restarts.

Recovery: `last_incomplete_task()` returns the most recent prompt_admitted
without a matching turn_ended, or None if the last task completed cleanly.

This is a local recovery journal, not a trusted training-data source. A user
controls the machine and can modify both this file and the client code.
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


#: What each event type must carry to be worth writing down.
#:
#: An event log with no schema records whatever the call site happened to pass,
#: and the gap only shows up when a question cannot be answered from it. That
#: happened: `critic_assessment` was written without an agent id, so after a
#: six-agent batch the log held nineteen verdicts and could not say which agent
#: any of them judged — the one question the log existed to answer.
#:
#: Enforcement is deliberately asymmetric with how much a caller can break: a
#: missing field is recorded as `null` plus a `_schema_gap` marker rather than
#: dropped or raised, because losing the event is worse than logging an
#: incomplete one. The test suite treats any `_schema_gap` as a failure, so the
#: gap is caught where it can still be fixed instead of in a post-mortem.
REQUIRED_FIELDS: dict = {
    "prompt_admitted": ("text",),
    "ai_response": ("loop",),
    "tool_call": ("name", "call_id"),
    "tool_result": ("name", "call_id", "ok"),
    "turn_ended": ("reason",),
    # Anything that judges or supervises an agent must say WHICH agent.
    "critic_assessment": ("agent_id", "run_id"),
    "critic_failure": ("agent_id", "reason"),
    "contract_checked": ("agent_id", "ok"),
    "child_help_requested": ("agent_id", "request_id"),
    "child_help_answered": ("agent_id", "request_id"),
    "child_help_timeout": ("agent_id", "request_id"),
    "branch_opened": ("branch_id", "owner"),
    "branch_closed": ("branch_id", "owner", "reason"),
    "member_settled": ("branch_id", "agent_id", "outcome"),
    # A crash record with no error text is a record that something went wrong
    # and nothing about what.
    "turn_crashed": ("error", "agent_id"),
    "context_compacted": ("before_tokens", "after_tokens"),
    "critic_prompt_warning": ("error",),
}


def schema_gaps(event_type: str, fields: dict) -> list:
    """Required fields this event is missing. Empty when the event is complete."""
    return [name for name in REQUIRED_FIELDS.get(event_type, ())
            if fields.get(name) in (None, "")]


def append(event_type: str, **fields) -> int:
    """Append an event to the durable log. Returns the sequence number.

    Never raises — a logging failure is swallowed (the loop must not break
    because the event log is unwritable). Returns -1 on failure.
    """
    _gaps = schema_gaps(event_type, fields)
    if _gaps:
        for _name in _gaps:
            fields.setdefault(_name, None)
        fields["_schema_gap"] = _gaps
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
            paths.ensure_project_dir()
            if not paths.ensure_private_file(p):
                return -1
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            paths.ensure_private_file(p)
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
