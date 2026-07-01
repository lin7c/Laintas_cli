"""
Structured task tracking system for laintas_cli.

Persists tasks to $CWD/.laintas/tasks.json (project-level) or
~/.laintas/tasks.json (global, when cwd=None). Tasks follow a status workflow:
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


import paths

TASKS_PATH = paths.TASKS_FILE
_lock = threading.RLock()

# Valid status transitions
_STATUS_FLOW = {
    "pending": {"in_progress", "completed", "deleted"},
    "in_progress": {"completed", "pending", "deleted"},
    "completed": {"in_progress", "deleted"},  # can re-open
    "deleted": set(),  # terminal state
}

# ── Session-level tasks (in-memory, not persisted) ──────────────────────

_session_tasks: list[dict] = []
_session_id_counter: int = 0


class TaskStorageError(RuntimeError):
    """Raised when task persistence fails instead of reporting false success."""


def _project_path(cwd: str) -> Path:
    """Return the project-level tasks.json path for a given cwd."""
    return Path(cwd) / ".laintas" / "tasks.json"


def _load(cwd: str = None) -> list[dict]:
    """Load tasks from disk. cwd selects project-level file; None uses global."""
    path = _project_path(cwd) if cwd else TASKS_PATH
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        normalized = []
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            task = dict(item)
            task.setdefault("subject", "(untitled task)")
            task.setdefault("description", "")
            task.setdefault("status", "pending")
            task.setdefault("metadata", {})
            task.setdefault("blocks", [])
            task.setdefault("blockedBy", [])
            task.setdefault("progress", 0)
            task.setdefault("notes", [])
            normalized.append(task)
        return normalized
    except (OSError, json.JSONDecodeError):
        return []


def _save(tasks: list[dict], cwd: str = None) -> None:
    """Atomically write tasks to disk. cwd selects project-level file."""
    path = _project_path(cwd) if cwd else TASKS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise TaskStorageError(f"Failed to save tasks to {path}: {exc}") from exc


def _new_task_record(task_id: str, subject: str, description: str = "",
                     metadata: dict = None, *, session_only: bool = False,
                     parent_task_id: str = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    task = {
        "id": task_id,
        "subject": subject,
        "description": description,
        "status": "pending",
        "created": now,
        "updated": now,
        "metadata": metadata or {},
        "blocks": [],
        "blockedBy": [str(parent_task_id)] if parent_task_id else [],
        "progress": 0,
        "notes": [],
    }
    if session_only:
        task["session_only"] = True
    return task


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
                parent_task_id: str = None,
                *, cwd: str = None) -> dict:
    """Create a new task. Returns the task dict.

    If session_only=True, the task lives only in memory (not persisted).
    If parent_task_id is set, the new task is auto-linked as blockedBy parent.
    cwd selects the project-level tasks file (None = global ~/.laintas/).
    """
    global _session_id_counter
    with _lock:
        if session_only:
            _session_id_counter += 1
            task_id = f"s{_session_id_counter}"
            task = _new_task_record(
                task_id, subject, description, metadata,
                session_only=True, parent_task_id=parent_task_id)
            _session_tasks.append(task)
            return dict(task)

        tasks = _load(cwd=cwd)
        task_id = _next_id(tasks)
        task = _new_task_record(
            task_id, subject, description, metadata,
            parent_task_id=parent_task_id)
        if parent_task_id:
            # Also add this task to parent's blocks list
            for t in tasks:
                if str(t.get("id")) == str(parent_task_id):
                    if task_id not in t.get("blocks", []):
                        t.setdefault("blocks", []).append(task_id)
                    break
        tasks.append(task)
        _save(tasks, cwd=cwd)
        return dict(task)


def create_session_task(subject: str, description: str = "",
                        parent_task_id: str = None) -> dict:
    """Create a session-only task (not persisted). Convenience wrapper."""
    return create_task(subject, description,
                       session_only=True, parent_task_id=parent_task_id)


def update_task(task_id: str, *, cwd: str = None, **kwargs) -> tuple[bool, str, Optional[dict]]:
    """Update a task's fields. Validates status transitions.

    Accepted kwargs: status, subject, description, metadata,
                     addBlocks, addBlockedBy, removeBlocks, removeBlockedBy,
                     progress, notes, addSubtask.
    cwd selects the project-level tasks file for persisted tasks.
    Returns (ok, message, updated_task).
    """
    global _session_id_counter
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
            tasks = _load(cwd=cwd)
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
            if new_status == "in_progress":
                all_tasks = (_load(cwd=cwd) + list(_session_tasks))
                incomplete_ids = {
                    str(item.get("id")) for item in all_tasks
                    if item.get("status") not in ("completed", "deleted")
                }
                blockers = sorted(
                    set(map(str, target.get("blockedBy", []))) & incomplete_ids)
                if blockers:
                    return False, (
                        f"Task '{task_id}' is blocked by incomplete task(s): "
                        f"{', '.join(blockers)}. Complete or unlink them first."
                    ), None
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
                return False, "progress must be an integer between 0 and 100", None

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
            if is_session:
                _session_id_counter += 1
                subtask_id = f"s{_session_id_counter}"
                subtask = _new_task_record(
                    subtask_id, subject, desc, session_only=True,
                    parent_task_id=task_id_str)
                _session_tasks.append(subtask)
            else:
                subtask_id = _next_id(tasks)
                subtask = _new_task_record(
                    subtask_id, subject, desc,
                    parent_task_id=task_id_str)
                tasks.append(subtask)
            if subtask_id not in target.setdefault("blocks", []):
                target["blocks"].append(subtask_id)

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
            _save(tasks, cwd=cwd)

        return True, f"Updated task {task_id}", dict(target)


def get_task(task_id: str, *, cwd: str = None) -> Optional[dict]:
    """Get a single task by ID (checks both persisted and session tasks)."""
    task_id_str = str(task_id)
    for t in _session_tasks:
        if str(t.get("id")) == task_id_str:
            return dict(t)
    tasks = _load(cwd=cwd)
    for t in tasks:
        if str(t.get("id")) == task_id_str:
            return dict(t)
    return None


def list_tasks(status: str = None, blocked_by: str = None,
               include_session: bool = True, *, cwd: str = None) -> list[dict]:
    """List tasks, optionally filtered by status or dependency."""
    tasks = _load(cwd=cwd)
    if include_session:
        tasks = tasks + list(_session_tasks)
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if blocked_by:
        blocked_by = str(blocked_by)
        tasks = [t for t in tasks if blocked_by in t.get("blockedBy", [])]
    # Sort session tasks first, then numeric persisted IDs without 10-before-2.
    def _sort_key(task: dict):
        task_id = str(task.get("id", ""))
        numeric = task_id[1:] if task_id.startswith("s") else task_id
        try:
            number = int(numeric)
            fallback = ""
        except (TypeError, ValueError):
            number = 10**18
            fallback = task_id
        return (0 if task.get("session_only") else 1, number, fallback)

    tasks.sort(key=_sort_key)
    return tasks


def get_available_tasks(*, cwd: str = None) -> list[dict]:
    """Get tasks ready to work on: pending, not blocked by any incomplete task."""
    all_tasks = _load(cwd=cwd) + list(_session_tasks)
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


def get_active_tasks_snapshot(*, cwd: str = None) -> str:
    """Render a compact summary of the open task list for prompt injection.

    Includes both in_progress (shown first) and pending tasks so the model's
    own plan stays visible every turn and can anchor a "continue" request.
    Returns empty string if nothing is open.
    """
    all_tasks = _load(cwd=cwd) + list(_session_tasks)
    in_progress = [t for t in all_tasks if t.get("status") == "in_progress"]
    pending = [t for t in all_tasks if t.get("status") == "pending"]
    ordered = in_progress + pending
    if not ordered:
        return ""

    lines = ["Open tasks (reference only — do not auto-resume; wait for the user to ask):"]
    for t in ordered[:10]:  # cap at 10
        status_mark = "▶" if t.get("status") == "in_progress" else "○"
        progress = t.get("progress", 0)
        progress_str = f" ({progress}%)" if progress > 0 else ""
        blocked_by = t.get("blockedBy", [])
        blocked_str = f" [blocked by: {', '.join(blocked_by)}]" if blocked_by else ""
        lines.append(f"  {status_mark} [{t['id']}] {t['subject']}{progress_str}{blocked_str}")
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


def export_active_tasks(*, cwd: str = None) -> list[dict]:
    """Export the open plan (in_progress + pending tasks, persisted + session).

    Used to snapshot the working plan into a /resume blob so it survives a
    restart — mirrors how Claude restores todos / Codex restores its plan.
    Returns deep-ish copies so callers can serialize without mutating state.
    """
    with _lock:
        all_tasks = _load(cwd=cwd) + list(_session_tasks)
    out = []
    for t in all_tasks:
        if t.get("status") in ("in_progress", "pending"):
            out.append(dict(t))
    return out


def import_session_tasks(tasks: list[dict], *, cwd: str = None) -> int:
    """Rehydrate a plan from a /resume blob as session tasks.

    Replaces the current session task list with the saved one (the resume blob
    is the source of truth for the resumed session). Persisted tasks are left
    untouched; an imported task that duplicates a still-open persisted task by
    subject is skipped so the plan isn't shown twice. Returns the count loaded.
    """
    global _session_tasks
    if not isinstance(tasks, list):
        return 0
    with _lock:
        persisted_open = {
            (t.get("subject") or "").strip()
            for t in _load(cwd=cwd)
            if t.get("status") in ("in_progress", "pending")
        }
        restored = []
        for t in tasks:
            if not isinstance(t, dict) or not t.get("subject"):
                continue
            if (t.get("subject") or "").strip() in persisted_open:
                continue
            task = dict(t)
            task["session_only"] = True
            task.setdefault("status", "pending")
            task.setdefault("blocks", [])
            task.setdefault("blockedBy", [])
            task.setdefault("notes", [])
            task.setdefault("progress", 0)
            restored.append(task)
        _session_tasks = restored
        return len(restored)
