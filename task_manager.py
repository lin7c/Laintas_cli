"""
Structured task tracking system for laintas_cli.

Persists tasks to ~/.laintas_cli_tasks.json. Tasks follow a status workflow:
  pending → in_progress → completed (or deleted)

Tasks can have dependencies (blocks/blockedBy) to enforce ordering.
Metadata is an arbitrary JSON dict for custom data.

Integrated as tools: task.create, task.update, task.list, task.get
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
                metadata: dict = None) -> dict:
    """Create a new task. Returns the task dict."""
    with _lock:
        tasks = _load()
        now = datetime.now(timezone.utc).isoformat()
        task = {
            "id": _next_id(tasks),
            "subject": subject,
            "description": description,
            "status": "pending",
            "created": now,
            "updated": now,
            "metadata": metadata or {},
            "blocks": [],       # task IDs that this task blocks
            "blockedBy": [],    # task IDs that block this task
        }
        tasks.append(task)
        _save(tasks)
        return dict(task)


def update_task(task_id: str, **kwargs) -> tuple[bool, str, Optional[dict]]:
    """Update a task's fields. Validates status transitions.

    Accepted kwargs: status, subject, description, metadata,
                     addBlocks, addBlockedBy, removeBlocks, removeBlockedBy.
    Returns (ok, message, updated_task).
    """
    with _lock:
        tasks = _load()
        target = None
        for t in tasks:
            if str(t.get("id")) == str(task_id):
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
        _save(tasks)
        return True, f"Updated task {task_id}", dict(target)


def get_task(task_id: str) -> Optional[dict]:
    """Get a single task by ID."""
    tasks = _load()
    for t in tasks:
        if str(t.get("id")) == str(task_id):
            return dict(t)
    return None


def list_tasks(status: str = None, blocked_by: str = None) -> list[dict]:
    """List tasks, optionally filtered by status or dependency."""
    tasks = _load()
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if blocked_by:
        blocked_by = str(blocked_by)
        tasks = [t for t in tasks if blocked_by in t.get("blockedBy", [])]
    # Sort by ID numerically
    tasks.sort(key=lambda t: int(t.get("id", "0")))
    return tasks


def get_available_tasks() -> list[dict]:
    """Get tasks ready to work on: pending, not blocked by any incomplete task."""
    all_tasks = _load()
    incomplete_ids = {t["id"] for t in all_tasks if t.get("status") not in ("completed", "deleted")}

    available = []
    for t in all_tasks:
        if t.get("status") != "pending":
            continue
        blocked = set(t.get("blockedBy", []))
        if blocked & incomplete_ids:
            continue  # still blocked
        available.append(t)
    available.sort(key=lambda t: int(t.get("id", "0")))
    return available
