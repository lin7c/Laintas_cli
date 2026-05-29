"""
Structured task tracking system for laintas_cli.

Persists tasks to ~/.laintas_cli_tasks.json. Tasks follow a status workflow:
  pending → in_progress → completed (or deleted)

Tasks can have dependencies (blocks/blockedBy) to enforce ordering.
Metadata is an arbitrary JSON dict for custom data.

Session-level tasks (not persisted) are also supported for ephemeral
workflow tracking. They live only in memory and are lost on exit.

Integrated as tools: task.create, task.update, task.list, task.get

Enhancements:
  - progress: 0-100 percentage
  - notes: append-only progress log
  - addSubtask: auto-create child task with dependency
  - session tasks: in-memory only, for workflow phases
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


TASKS_PATH = Path.home() / ".laintas_cli_tasks.json"
_lock = threading.RLock()

# Valid status transitions
_STATUS_FLOW = {
    "pending": {"in_progress", "deleted"},
    "in_progress": {"completed", "pending", "deleted"},
    "completed": {"in_progress", "deleted"},  # can re-open
    "deleted": set(),  # terminal state
}

# ── Session-level tasks (in-memory, not persisted) ──────────────────────

_session_tasks: list[dict] = []
_session_id_counter: int = 0


def _load() -> list[dict]:
    """Load all tasks from disk. Returns [] on any error."""
    if not TASKS_PATH.exists():
        return []
    try:
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(tasks: list[dict]) -> None:
    """Atomically write tasks to disk."""
    tmp = TASKS_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        tmp.replace(TASKS_PATH)
    except OSError:
        pass


def _next_id(tasks: list[dict]) -> str:
    """Generate the next task ID. Short numeric string."""
    max_id = 0
    for t in tasks:
        try:
            max_id = max(max_id, int(t.get("id", "0")))
        except (ValueError, TypeError):
            pass
    return str(max_id + 1)


def create_task(subject: str, description: str = "",
                metadata: dict = None,
                session_only: bool = False,
                parent_task_id: str = None) -> dict:
    """Create a new task. Returns the task dict.

    If session_only=True, the task lives only in memory (not persisted).
    If parent_task_id is set, the new task is auto-linked as blockedBy parent.
    """
    global _session_id_counter
    with _lock:
        now = datetime.now(timezone.utc).isoformat()

        if session_only:
            _session_id_counter += 1
            task_id = f"s{_session_id_counter}"
            task = {
                "id": task_id,
                "subject": subject,
                "description": description,
                "status": "pending",
                "created": now,
                "updated": now,
                "metadata": metadata or {},
                "blocks": [],
                "blockedBy": [],
                "progress": 0,
                "notes": [],
                "session_only": True,
            }
            if parent_task_id:
                task["blockedBy"].append(str(parent_task_id))
            _session_tasks.append(task)
            return dict(task)

        tasks = _load()
        task_id = _next_id(tasks)
        task = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "created": now,
            "updated": now,
            "metadata": metadata or {},
            "blocks": [],
            "blockedBy": [],
            "progress": 0,
            "notes": [],
        }
        if parent_task_id:
            task["blockedBy"].append(str(parent_task_id))
            # Also add this task to parent's blocks list
            for t in tasks:
                if str(t.get("id")) == str(parent_task_id):
                    if task_id not in t.get("blocks", []):
                        t.setdefault("blocks", []).append(task_id)
                    break
        tasks.append(task)
        _save(tasks)
        return dict(task)


def create_session_task(subject: str, description: str = "",
                        parent_task_id: str = None) -> dict:
    """Create a session-only task (not persisted). Convenience wrapper."""
    return create_task(subject, description,
                       session_only=True, parent_task_id=parent_task_id)


def update_task(task_id: str, **kwargs) -> tuple[bool, str, Optional[dict]]:
    """Update a task's fields. Validates status transitions.

    Accepted kwargs: status, subject, description, metadata,
                     addBlocks, addBlockedBy, removeBlocks, removeBlockedBy,
                     progress, notes, addSubtask.
    Returns (ok, message, updated_task).
    """
    with _lock:
        task_id_str = str(task_id)

        # Check session tasks first
        target = None
        is_session = False
        for t in _session_tasks:
            if str(t.get("id")) == task_id_str:
                target = t
                is_session = True
                break

        if target is None:
            tasks = _load()
            for t in tasks:
                if str(t.get("id")) == task_id_str:
                    target = t
                    break

        if target is None:
            return False, f"Task '{task_id}' not found", None

        # Status validation
        new_status = kwargs.get("status")
        if new_status is not None:
            current = target.get("status", "pending")
            allowed = _STATUS_FLOW.get(current, set())
            if new_status not in allowed:
                return False, f"Invalid status transition: {current} → {new_status}. Allowed: {allowed}", None
            target["status"] = new_status

        if "subject" in kwargs:
            target["subject"] = kwargs["subject"]
        if "description" in kwargs:
            target["description"] = kwargs["description"]
        if "metadata" in kwargs and kwargs["metadata"] is not None:
            target["metadata"] = {**target.get("metadata", {}), **kwargs["metadata"]}

        # Progress tracking (0-100)
        if "progress" in kwargs:
            try:
                p = max(0, min(100, int(kwargs["progress"])))
                target["progress"] = p
            except (ValueError, TypeError):
                pass

        # Notes (append-only log)
        if "notes" in kwargs:
            note = str(kwargs["notes"])
            if note:
                now = datetime.now(timezone.utc).strftime("%H:%M:%S")
                target.setdefault("notes", []).append(f"[{now}] {note}")

        # Subtask creation
        if "addSubtask" in kwargs:
            subtask_info = kwargs["addSubtask"]
            if isinstance(subtask_info, dict):
                subject = subtask_info.get("subject", "Subtask")
                desc = subtask_info.get("description", "")
            elif isinstance(subtask_info, str):
                subject = subtask_info
                desc = ""
            else:
                subject = str(subtask_info)
                desc = ""
            subtask = create_task(subject, desc, parent_task_id=task_id_str,
                                   session_only=is_session)
            target.setdefault("blocks", []).append(subtask["id"])

        # Dependency management
        for key, op in [("addBlocks", "blocks"), ("addBlockedBy", "blockedBy")]:
            ids = kwargs.get(key, [])
            if ids:
                existing = set(target.get(op, []))
                target[op] = list(existing | {str(i) for i in ids})

        for key, op in [("removeBlocks", "blocks"), ("removeBlockedBy", "blockedBy")]:
            ids = kwargs.get(key, [])
            if ids:
                existing = set(target.get(op, []))
                target[op] = list(existing - {str(i) for i in ids})

        target["updated"] = datetime.now(timezone.utc).isoformat()

        if not is_session:
            _save(tasks)

        return True, f"Updated task {task_id}", dict(target)


def get_task(task_id: str) -> Optional[dict]:
    """Get a single task by ID (checks both persisted and session tasks)."""
    task_id_str = str(task_id)
    for t in _session_tasks:
        if str(t.get("id")) == task_id_str:
            return dict(t)
    tasks = _load()
    for t in tasks:
        if str(t.get("id")) == task_id_str:
            return dict(t)
    return None


def list_tasks(status: str = None, blocked_by: str = None,
               include_session: bool = True) -> list[dict]:
    """List tasks, optionally filtered by status or dependency."""
    tasks = _load()
    if include_session:
        tasks = tasks + list(_session_tasks)
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if blocked_by:
        blocked_by = str(blocked_by)
        tasks = [t for t in tasks if blocked_by in t.get("blockedBy", [])]
    # Sort: session tasks first (by id), then persisted (numerically)
    tasks.sort(key=lambda t: (0 if t.get("session_only") else 1, str(t.get("id", "0"))))
    return tasks


def get_available_tasks() -> list[dict]:
    """Get tasks ready to work on: pending, not blocked by any incomplete task."""
    all_tasks = _load() + list(_session_tasks)
    incomplete_ids = {t["id"] for t in all_tasks if t.get("status") not in ("completed", "deleted")}

    available = []
    for t in all_tasks:
        if t.get("status") != "pending":
            continue
        blocked = set(t.get("blockedBy", []))
        if blocked & incomplete_ids:
            continue
        available.append(t)
    return available


def get_active_tasks_snapshot() -> str:
    """Render a compact summary of in_progress tasks for prompt injection.

    Returns empty string if no active tasks.
    """
    all_tasks = _load() + list(_session_tasks)
    active = [t for t in all_tasks if t.get("status") == "in_progress"]
    if not active:
        return ""

    lines = ["Active tasks:"]
    for t in active[:10]:  # cap at 10
        progress = t.get("progress", 0)
        progress_str = f" ({progress}%)" if progress > 0 else ""
        blocked_by = t.get("blockedBy", [])
        blocked_str = f" [blocked by: {', '.join(blocked_by)}]" if blocked_by else ""
        lines.append(f"  [{t['id']}] {t['subject']}{progress_str}{blocked_str}")
        # Show last 2 notes if any
        notes = t.get("notes", [])
        for note in notes[-2:]:
            lines.append(f"    ↳ {note[:100]}")
    return "\n".join(lines)


def clear_session_tasks() -> None:
    """Remove all session-level tasks."""
    global _session_tasks
    with _lock:
        _session_tasks.clear()

