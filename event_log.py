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

#: Event types that recovery actually reads (last_incomplete_task matches on
#: prompt_admitted / turn_ended). Only these need to survive a hard crash:
#: fsync costs milliseconds per event and every tool_call/tool_result used to
#: pay it. Everything else still gets flush() — visible to any reader, lost
#: only on OS crash mid-turn, and rebuildable from session files anyway.
#: LAINTAS_EVENT_FSYNC=1 forces the old fsync-everything behaviour.
_FSYNC_EVENT_TYPES = frozenset({"prompt_admitted", "turn_ended"})


def _fsync_needed(event_type: str) -> bool:
    if os.environ.get("LAINTAS_EVENT_FSYNC", "").strip() not in ("", "0"):
        return True
    return event_type in _FSYNC_EVENT_TYPES


def _log_path() -> Path:
    return Path(paths.project_dir()) / "events.jsonl"


#: Tail windows tried, in order, when recovering the last sequence number.
#: The number lives on the final line, so the first window almost always
#: answers it. Reading the whole file instead cost 1.8 SECONDS on a 41 MB log
#: (57k events) — once per process, but paid at the first event of every
#: session, and the log only grows.
_SEQ_TAIL_WINDOWS = (64 * 1024, 1024 * 1024)


def _last_seq_in(path: Path) -> int:
    """The highest `seq` recorded in a log, read from its tail."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    for window in (*_SEQ_TAIL_WINDOWS, size):
        try:
            with open(path, "rb") as fh:
                if window < size:
                    fh.seek(size - window)
                    fh.readline()   # drop the partial line the seek landed in
                chunk = fh.read()
        except OSError:
            return 0
        for line in reversed(chunk.decode("utf-8", "replace").splitlines()):
            try:
                found = int(json.loads(line).get("seq") or 0)
            except (AttributeError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if found:
                return found
        if window >= size:
            break
    return 0


def _next_seq(path: Path) -> int:
    """Return a process-safe, restart-safe advisory sequence for one log."""
    key = str(path.resolve())
    if key not in _SEQ_BY_PATH:
        _SEQ_BY_PATH[key] = _last_seq_in(path)
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
    # Intent alignment. "Which agent, and what did the anchoring gate throw
    # away" is the whole diagnostic value: a rising dropped_anchors count is
    # how you find out the auxiliary model has started inventing requirements.
    "intent_started": ("agent_id", "run_id"),
    "intent_spec_built": ("agent_id", "run_id", "requirements",
                          "dropped_anchors"),
    "intent_failure": ("agent_id", "run_id", "reason"),
    "intent_questions_injected": ("agent_id", "run_id", "count"),
    "intent_tasks_created": ("agent_id", "run_id", "count"),
    "intent_compared": ("agent_id", "run_id", "severity"),
    "intent_correction_injected": ("agent_id", "run_id", "severity"),
    # Who won, and after how many rounds. Without the verdict this event
    # cannot answer the only question worth asking of a debate mechanism:
    # whether the cheap judge is overruling the expensive one.
    "intent_resolved": ("agent_id", "run_id", "verdict", "debate_round"),
    # Which directory vanished, and where the run continued from.
    "cwd_recovered": ("gone", "now"),
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
                if _fsync_needed(event_type):
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

    Reads the log BACKWARDS in expanding tail windows (same pattern as
    _last_seq_in): scanning from the end, the first prompt_admitted whose
    run_id has no turn_ended after it is the most recent pending admission —
    which is exactly the crash-recovery case, answered from the first window
    without touching the rest of a multi-MB log. Only when nothing is pending
    does the scan walk back through the whole file. A legacy no-run_id
    admission counts only if it is the last legacy one and no legacy
    turn_ended followed it. One deliberate difference from the old forward
    read: when both a legacy and a run_id admission are pending, the one
    later in the file wins (the old code always preferred the legacy one).

    Returns None if the last task completed cleanly or the log is
    empty/unreadable.
    """
    try:
        p = _log_path()
        if not p.exists():
            return None
        size = p.stat().st_size
    except OSError:
        return None
    closed_runs: set[str] = set()
    legacy_closed = False
    legacy_done = False
    consumed_end = size
    try:
        for window in (*_SEQ_TAIL_WINDOWS, size):
            start = size - window
            if start < 0:
                start = 0
            if start >= consumed_end:
                continue
            with open(p, "rb") as fh:
                fh.seek(start)
                if start > 0:
                    fh.readline()   # drop the partial line the seek landed in
                new_end = fh.tell()
                if new_end >= consumed_end:
                    # The window started inside the last unread line (one
                    # event longer than the window — a prompt with a pasted
                    # file). Nothing whole to read yet; leave consumed_end
                    # alone so the next, larger window reads that line in
                    # full instead of a truncated prefix that fails to parse.
                    continue
                data = fh.read(consumed_end - new_end)
            consumed_end = new_end
            if not data:
                continue
            for line in reversed(data.decode("utf-8", "replace").splitlines()):
                try:
                    evt = json.loads(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                etype = evt.get("type")
                if etype == "turn_ended":
                    rid = str(evt.get("run_id") or "")
                    if rid:
                        closed_runs.add(rid)
                    else:
                        legacy_closed = True
                elif etype == "prompt_admitted":
                    rid = str(evt.get("run_id") or "")
                    if rid:
                        if rid not in closed_runs:
                            return evt
                    elif not legacy_done:
                        if not legacy_closed:
                            return evt
                        # The last legacy admission was closed by a legacy
                        # turn_ended; older legacy admissions were already
                        # superseded by it in the forward-read semantics.
                        legacy_done = True
        return None
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
