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

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import json_store


import paths
import workgraph

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
_session_key: str = uuid.uuid4().hex[:16]
_LEGACY_IMPORT_KEY = "legacy_tasks_imported"


def _active_work(cwd: str = None) -> dict:
    work = workgraph.ensure_active_work(cwd=cwd)
    # One-way compatibility import. The legacy file is retained as an archive;
    # a non-empty WorkGraph prevents repeated imports.
    legacy_imported = bool(workgraph.get_project_value(
        _LEGACY_IMPORT_KEY, cwd=cwd))
    if (not legacy_imported
            and not work.get("current_revision")
            and not workgraph.list_steps(work["id"], cwd=cwd, include_deleted=True)):
        legacy = _load(cwd=cwd)
        if legacy:
            id_map = {}
            for item in legacy:
                try:
                    step = workgraph.create_step(
                        work["id"], item.get("subject") or "(untitled task)",
                        item.get("description") or "", cwd=cwd,
                        metadata=item.get("metadata") or {},
                        session_only=bool(item.get("session_only")))
                    id_map[str(item.get("id"))] = step["id"]
                    fields = {
                        "status": item.get("status", "pending"),
                        "progress": item.get("progress", 0),
                    }
                    if item.get("notes"):
                        fields["notes"] = " | ".join(map(str, item["notes"]))
                    workgraph.update_step(work["id"], step["id"], cwd=cwd, **fields)
                except workgraph.WorkGraphError:
                    continue
            for item in legacy:
                step_id = id_map.get(str(item.get("id")))
                if not step_id:
                    continue
                for blocker in item.get("blockedBy", []) or []:
                    blocker_id = id_map.get(str(blocker))
                    if blocker_id:
                        try:
                            workgraph.add_dependency(
                                work["id"], step_id, blocker_id, cwd=cwd)
                        except workgraph.WorkGraphError:
                            pass
    if not legacy_imported:
        workgraph.set_project_value(_LEGACY_IMPORT_KEY, True, cwd=cwd)
    return work


def _read_work(cwd: str = None) -> Optional[dict]:
    work = workgraph.get_active_work(cwd=cwd)
    if work:
        return work
    # Reading must not create an empty WorkGraph, but it may trigger one-way
    # migration when real legacy tasks exist.
    if (_load(cwd=cwd)
            and not workgraph.get_project_value(_LEGACY_IMPORT_KEY, cwd=cwd)):
        return _active_work(cwd)
    return None


def _compat(step: dict) -> dict:
    """Project a WorkGraph Step through the legacy task.* shape."""
    item = dict(step)
    item.setdefault("created", item.get("created_at"))
    item.setdefault("updated", item.get("updated_at"))
    # Legacy `/task subtask` displayed children through `blocks`; preserve the
    # view without treating hierarchy as an execution dependency.
    item["blocks"] = list(dict.fromkeys(
        list(item.get("blocks") or []) + list(item.get("children") or [])))
    item.setdefault("blockedBy", [])
    item.setdefault("metadata", {})
    notes = []
    for note in item.get("notes") or []:
        if isinstance(note, dict):
            notes.append(str(note.get("text") or ""))
        else:
            notes.append(str(note))
    item["notes"] = notes
    return item


class TaskStorageError(RuntimeError):
    """Raised when task persistence fails instead of reporting false success."""


def _project_path(cwd: str) -> Path:
    """Return the project-level tasks.json path for a given cwd."""
    return Path(cwd) / ".laintas" / "tasks.json"


def _load(cwd: str = None) -> list[dict]:
    """Load tasks from disk. cwd selects project-level file; None uses global."""
    path = _project_path(cwd) if cwd else TASKS_PATH
    data = json_store.load_json(path, default=list)
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


def _save(tasks: list[dict], cwd: str = None) -> None:
    """Atomically write tasks to disk. cwd selects project-level file."""
    path = _project_path(cwd) if cwd else TASKS_PATH
    try:
        json_store.save_json_atomic(path, tasks)
    except OSError as exc:
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
    with _lock:
        try:
            work = _active_work(cwd)
            step = workgraph.create_step(
                work["id"], subject, description, cwd=cwd,
                metadata=({**(metadata or {}), "_session_key": _session_key}
                          if session_only else metadata), session_only=session_only,
                parent_id=str(parent_task_id) if parent_task_id else None)
            return _compat(step)
        except workgraph.WorkGraphError as exc:
            raise TaskStorageError(str(exc)) from exc


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
    with _lock:
        try:
            work = _active_work(cwd)
            target = workgraph.get_step(work["id"], str(task_id), cwd=cwd)
            if target is None:
                return False, f"Task '{task_id}' not found", None

            if "addSubtask" in kwargs:
                value = kwargs["addSubtask"]
                subject = value.get("subject", "Subtask") if isinstance(value, dict) else str(value)
                description = value.get("description", "") if isinstance(value, dict) else ""
                workgraph.create_step(
                    work["id"], subject, description, cwd=cwd,
                    metadata=({"_session_key": _session_key}
                              if target.get("session_only") else None),
                    session_only=bool(target.get("session_only")), parent_id=str(task_id))

            for blocker in kwargs.get("addBlockedBy", []) or []:
                workgraph.add_dependency(work["id"], str(task_id), str(blocker), cwd=cwd)
            for blocked in kwargs.get("addBlocks", []) or []:
                workgraph.add_dependency(work["id"], str(blocked), str(task_id), cwd=cwd)
            for blocker in kwargs.get("removeBlockedBy", []) or []:
                workgraph.remove_dependency(work["id"], str(task_id), str(blocker), cwd=cwd)
            for blocked in kwargs.get("removeBlocks", []) or []:
                workgraph.remove_dependency(work["id"], str(blocked), str(task_id), cwd=cwd)

            fields = {key: kwargs[key] for key in
                      ("status", "subject", "description", "metadata", "progress", "notes")
                      if key in kwargs}
            updated = workgraph.update_step(
                work["id"], str(task_id), cwd=cwd, **fields) if fields else (
                    workgraph.get_step(work["id"], str(task_id), cwd=cwd) or {})
            remaining = [item for item in workgraph.list_steps(work["id"], cwd=cwd)
                         if item.get("status") not in {"completed", "skipped", "deleted"}]
            if not remaining and work.get("status") == "EXECUTING":
                workgraph.update_work(work["id"], cwd=cwd, status="VERIFYING")
            return True, f"Updated task {task_id}", _compat(updated)
        except (workgraph.WorkGraphError, workgraph.WorkGraphConflict) as exc:
            return False, str(exc), None


def get_task(task_id: str, *, cwd: str = None) -> Optional[dict]:
    """Get a single task by ID (checks both persisted and session tasks)."""
    work = _read_work(cwd)
    if not work:
        return None
    step = workgraph.get_step(work["id"], str(task_id), cwd=cwd)
    return _compat(step) if step else None


def list_tasks(status: str = None, blocked_by: str = None,
               include_session: bool = True, *, cwd: str = None) -> list[dict]:
    """List tasks, optionally filtered by status or dependency."""
    work = _read_work(cwd)
    if not work:
        return []
    tasks = [_compat(item) for item in workgraph.list_steps(
        work["id"], cwd=cwd, include_deleted=True)]
    if not include_session:
        tasks = [task for task in tasks if not task.get("session_only")]
    else:
        tasks = [task for task in tasks
                 if (not task.get("session_only")
                     or task.get("metadata", {}).get("_session_key") == _session_key)]
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
    tasks = list_tasks(cwd=cwd)
    status = {str(item["id"]): item.get("status") for item in tasks}
    return [item for item in tasks
            if item.get("status") == "pending"
            and all(status.get(str(blocker)) in ("completed", "skipped", "deleted")
                    for blocker in item.get("blockedBy", []))]


def get_active_tasks_snapshot(*, cwd: str = None) -> str:
    """Render a compact summary of the open task list for prompt injection.

    Includes both in_progress (shown first) and pending tasks so the model's
    own plan stays visible every turn and can anchor a "continue" request.
    Returns empty string if nothing is open.
    """
    all_tasks = list_tasks(cwd=cwd)
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


def clear_session_tasks(*, cwd: str = None) -> None:
    """Remove all session-level tasks."""
    global _session_tasks, _session_id_counter
    with _lock:
        _session_tasks.clear()
        _session_id_counter = 0
        try:
            if workgraph.db_path(cwd).exists():
                workgraph.clear_session_steps(cwd=cwd)
        except workgraph.WorkGraphError:
            pass


def detach_active_tasks(*, cwd: str = None) -> None:
    """Detach all task context when starting a fresh session.

    Persisted WorkGraph history remains available for explicit /resume or
    /work resume, while the retained legacy tasks.json archive is marked as
    already migrated so it cannot silently reactivate the old task list.
    """
    clear_session_tasks(cwd=cwd)
    workgraph.set_project_value(_LEGACY_IMPORT_KEY, True, cwd=cwd)
    workgraph.set_active_work(None, cwd=cwd)


def export_active_tasks(*, cwd: str = None) -> list[dict]:
    """Export the open plan (in_progress + pending tasks, persisted + session).

    Used to snapshot the working plan into a /resume blob so it survives a
    restart — mirrors how Claude restores todos / Codex restores its plan.
    Returns deep-ish copies so callers can serialize without mutating state.
    """
    with _lock:
        all_tasks = list_tasks(cwd=cwd)
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
    if not isinstance(tasks, list):
        return 0
    with _lock:
        work = _active_work(cwd)
        try:
            return workgraph.import_session_steps(
                work["id"], tasks, cwd=cwd, session_key=_session_key)
        except workgraph.WorkGraphError:
            return 0
