"""Durable event log for crash-safe prompt admission and turn tracking.

Inspired by opencode's event-sourcing model (packages/core/src/event.ts):
every state change is an event with a monotonic sequence number, written to
an append-only JSONL file BEFORE execution begins. A crash never loses the
prompt — the event log is the source of truth for "what was the user doing
when it died?"

Event types:
  - prompt_admitted  — user's prompt, written BEFORE the agent loop runs
  - ai_response       — model reply + tool_calls, after backend returns
  - tool_result       — tool name + ok/error, after tool execution
  - turn_ended        — exit reason, written when the loop exits

The log lives in `.laintas/events.jsonl` (per-cwd). It is append-only and
line-buffered — writes are synchronous but fast (one line append).

Recovery: `last_incomplete_task()` returns the most recent prompt_admitted
without a matching turn_ended, or None if the last task completed cleanly.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import paths

_SEQ = 0


def _log_path() -> Path:
    return Path(paths.project_dir()) / "events.jsonl"


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def append(event_type: str, **fields) -> int:
    """Append an event to the durable log. Returns the sequence number.

    Never raises — a logging failure is swallowed (the loop must not break
    because the event log is unwritable). Returns -1 on failure.
    """
    try:
        entry = {
            "seq": _next_seq(),
            "type": event_type,
            "ts": time.time(),
            **fields,
        }
        p = _log_path()
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
        # Scan backwards for the last prompt_admitted and check if a
        # turn_ended came after it.
        last_admitted = None
        for line in reversed(lines):
            try:
                evt = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if evt.get("type") == "turn_ended":
                return None  # last task completed cleanly
            if evt.get("type") == "prompt_admitted":
                last_admitted = evt
                break
        return last_admitted
    except Exception:
        return None


def clear() -> None:
    """Truncate the event log (called on /clear or explicit reset)."""
    try:
        p = _log_path()
        if p.exists():
            p.write_text("", encoding="utf-8")
    except Exception:
        pass
