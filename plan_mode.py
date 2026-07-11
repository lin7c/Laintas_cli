"""
Plan Mode for laintas_cli — structured planning before execution.

Plan Mode: before executing complex tasks, the AI
enters a planning phase where it explores, designs, and documents an approach.
The plan is saved to disk, reviewed by the user, and then executed.

Workflow:
  1. /plan enter "task description" → AI enters plan mode
  2. AI explores codebase, analyzes, designs
  3. AI writes structured plan to ~/.laintas/plans/<name>.md
  4. /plan approve → exits plan mode, begins execution
  5. AI executes the plan step by step

Plans are persistent — they survive crashes and sessions.
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

PLANS_DIR = paths.PLANS_DIR
_STATE_PATH = paths.PLANS_STATE

_lock = threading.RLock()

# Current plan state (in-memory, synced to disk)
_current_plan: Optional[dict] = None
_plan_mode: bool = False
_pending_task: bool = False
_loaded_cwd: Optional[str] = None


def ensure_plans_dir() -> Path:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    return PLANS_DIR


def _load_state() -> dict:
    """Load plan mode state from disk."""
    project_state = workgraph.get_project_value("plan_mode_state")
    if isinstance(project_state, dict):
        return project_state
    data = json_store.load_json(_STATE_PATH, default=dict)
    if isinstance(data, dict) and isinstance(data.get("projects"), dict):
        return data["projects"].get(str(Path.cwd().resolve()), {})
    # Backward compatibility with the original single-project state.
    if isinstance(data, dict):
        plan = data.get("current_plan") or {}
        plan_cwd = plan.get("cwd") if isinstance(plan, dict) else None
        if not plan_cwd or Path(plan_cwd).resolve() == Path.cwd().resolve():
            return data
    return {}


def _save_state(state: dict) -> bool:
    """Save plan mode state to disk."""
    try:
        workgraph.set_project_value("plan_mode_state", state)
    except workgraph.WorkGraphError:
        pass
    ensure_plans_dir()
    all_state = json_store.load_json(_STATE_PATH, default=dict)
    if not (isinstance(all_state, dict) and isinstance(all_state.get("projects"), dict)):
        all_state = {}
    projects = all_state.setdefault("projects", {})
    projects[str(Path.cwd().resolve())] = state
    try:
        json_store.save_json_atomic(_STATE_PATH, all_state)
        return True
    except OSError:
        return False


def _restore_state() -> None:
    """Restore an active plan for this project after a process restart."""
    global _plan_mode, _current_plan, _pending_task, _loaded_cwd
    _loaded_cwd = str(Path.cwd().resolve())
    _plan_mode = False
    _pending_task = False
    _current_plan = None
    state = _load_state()
    if state.get("plan_mode") and state.get("pending_task"):
        _plan_mode = True
        _pending_task = True
        return
    plan = state.get("current_plan")
    if not state.get("plan_mode") or not isinstance(plan, dict):
        return
    plan_file = plan.get("file")
    plan_cwd = plan.get("cwd")
    try:
        same_project = not plan_cwd or Path(plan_cwd).resolve() == Path.cwd().resolve()
    except OSError:
        same_project = False
    if not same_project or not plan_file or not Path(plan_file).is_file():
        return
    if not plan.get("work_id"):
        try:
            content = Path(plan_file).read_text(encoding="utf-8")
            work = workgraph.create_work(plan.get("task") or "Imported plan")
            revision = workgraph.add_revision(
                work["id"], content, author="migration")
            plan = dict(plan)
            plan.update({
                "work_id": work["id"], "revision": revision["revision"],
                "content_sha": revision["content_sha"],
            })
            _save_state({"plan_mode": True, "current_plan": plan})
        except (OSError, workgraph.WorkGraphError):
            return
    _current_plan = dict(plan)
    _plan_mode = True
    try:
        if plan.get("work_id"):
            workgraph.set_active_work(plan["work_id"])
    except workgraph.WorkGraphError:
        _current_plan = None
        _plan_mode = False


_restore_state()


def _ensure_project_state() -> None:
    if _loaded_cwd != str(Path.cwd().resolve()):
        _restore_state()


def is_plan_mode() -> bool:
    """Check if the agent is currently in plan mode."""
    global _plan_mode
    _ensure_project_state()
    return _plan_mode


def is_pending_task() -> bool:
    """Return True when PLAN is armed and waiting for the next user message."""
    _ensure_project_state()
    return _plan_mode and _pending_task and _current_plan is None


def arm_plan_mode() -> None:
    """Enter PLAN without creating a plan until the next task is provided."""
    global _plan_mode, _pending_task, _current_plan
    with _lock:
        _ensure_project_state()
        _plan_mode = True
        _pending_task = True
        _current_plan = None
        _save_state({
            "plan_mode": True, "pending_task": True, "current_plan": None,
        })


_PLAN_ALLOWED_TOOLS = {
    "fs.read", "fs.ls", "fs.grep", "fs.glob",
    "web.search", "web.fetch", "time.now",
    "plan.read", "plan.update", "plan.list", "plan.submit",
    "task.list", "task.get",
    "skill.list", "skill.reference",
    "agent.spawn", "agent.tell", "agent.list", "agent.wait", "agent.inbox",
    "task.continue", "task.complete",
}


def is_tool_allowed(tool_name: str) -> bool:
    """Enforce read-only exploration while Plan Mode is active."""
    if not is_plan_mode():
        return True
    return tool_name in _PLAN_ALLOWED_TOOLS


def enter_plan_mode(task: str) -> dict:
    """Enter plan mode for a given task.

    Creates a new plan file and sets plan mode active.
    Returns the plan metadata.
    """
    global _plan_mode, _current_plan, _pending_task
    with _lock:
        _ensure_project_state()
        ensure_plans_dir()

        # Generate a plan name from the task
        import re as _re
        name = _re.sub(r'[^a-z0-9]+', '-', task.lower().strip())[:50]
        name = name.strip('-') or "plan"

        plan_file = PLANS_DIR / f"{name}.md"
        # If name collision, append number
        counter = 1
        while plan_file.exists():
            plan_file = PLANS_DIR / f"{name}-{counter}.md"
            counter += 1

        now = datetime.now(timezone.utc).isoformat()
        plan = {
            "name": plan_file.stem,
            "task": task,
            "file": str(plan_file),
            "created": now,
            "updated": now,
            "status": "drafting",
            "approved": False,
            "cwd": str(Path.cwd().resolve()),
        }

        # Write template
        content = _plan_template(task, now)
        plan_file.write_text(content, encoding="utf-8")

        work = workgraph.create_work(task, cwd=str(Path.cwd().resolve()))
        revision = workgraph.add_revision(
            work["id"], content, cwd=str(Path.cwd().resolve()), author="system")
        plan.update({
            "work_id": work["id"],
            "revision": revision["revision"],
            "content_sha": revision["content_sha"],
        })

        _current_plan = plan
        _plan_mode = True
        _pending_task = False
        _save_state({"plan_mode": True, "current_plan": plan})

        return dict(plan)


def exit_plan_mode(approve: bool = False) -> Optional[dict]:
    """Exit plan mode. If approved, the plan is locked and ready for execution.

    Returns the final plan or None.
    """
    global _plan_mode, _current_plan, _pending_task
    with _lock:
        _ensure_project_state()
        plan = dict(_current_plan) if _current_plan else None
        if plan and approve:
            work_id = plan.get("work_id")
            if work_id:
                try:
                    snapshot = workgraph.submit_plan(work_id, cwd=plan.get("cwd"))
                    rev = snapshot["revision"]
                    workgraph.approve_plan(
                        work_id, rev["revision"], rev["content_sha"],
                        cwd=plan.get("cwd"))
                except workgraph.WorkGraphError:
                    return None
                plan["revision"] = rev["revision"]
                plan["content_sha"] = rev["content_sha"]
            plan["status"] = "approved"
            plan["approved"] = True
            plan["updated"] = datetime.now(timezone.utc).isoformat()
            # Update the plan file
            try:
                p = Path(plan["file"])
                content = p.read_text(encoding="utf-8")
                content = content.replace("Status:** drafting", "Status:** approved ✅")
                content = content.replace("Approved:** no", "Approved:** yes")
                p.write_text(content, encoding="utf-8")
            except OSError:
                pass

        _plan_mode = False
        _pending_task = False
        _current_plan = None
        _save_state({"plan_mode": False, "current_plan": None})

        return plan


def get_current_plan() -> Optional[dict]:
    """Get the current plan, if any."""
    _ensure_project_state()
    return dict(_current_plan) if _current_plan else None


def read_plan(name: str = None) -> Optional[str]:
    """Read a plan file. If name is None, reads the current plan."""
    _ensure_project_state()
    if name:
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            return None
        work = workgraph.get_work(name)
        if work and work.get("current_revision"):
            revision = workgraph.get_revision(name, work["current_revision"])
            return (revision or {}).get("content")
        p = PLANS_DIR / f"{name}.md"
    elif _current_plan:
        p = Path(_current_plan["file"])
    else:
        return None

    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _update_plan_impl(content: str) -> bool:
    """Overwrite the current plan file. Returns True on success."""
    _ensure_project_state()
    if not _current_plan:
        return False
    try:
        p = Path(_current_plan["file"])
        work_id = _current_plan.get("work_id")
        if work_id:
            revision = workgraph.add_revision(
                work_id, content, cwd=_current_plan.get("cwd"), author="ai")
            _current_plan["revision"] = revision["revision"]
            _current_plan["content_sha"] = revision["content_sha"]
            _current_plan["status"] = "drafting"
            _current_plan["approved"] = False
        tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        _current_plan["updated"] = datetime.now(timezone.utc).isoformat()
        _save_state({"plan_mode": True, "current_plan": _current_plan})
        return True
    except (OSError, workgraph.WorkGraphError):
        return False


def update_plan(content: str) -> bool:
    """Atomically create a revision and refresh its Markdown projection."""
    with _lock:
        return _update_plan_impl(content)


def submit_current_plan() -> Optional[dict]:
    """Mark the current immutable revision ready for user review."""
    _ensure_project_state()
    if not _current_plan or not _current_plan.get("work_id"):
        return None
    if _current_plan.get("status") == "review_pending":
        return get_review_snapshot()
    try:
        snapshot = workgraph.submit_plan(
            _current_plan["work_id"], cwd=_current_plan.get("cwd"))
    except workgraph.WorkGraphError:
        return None
    _current_plan["status"] = "review_pending"
    _current_plan["revision"] = snapshot["revision"]["revision"]
    _current_plan["content_sha"] = snapshot["revision"]["content_sha"]
    _save_state({"plan_mode": True, "current_plan": _current_plan})
    return snapshot


def approve_submitted_plan(revision: int, content_sha: str) -> Optional[dict]:
    """Approve exactly the revision shown in the confirmation UI."""
    global _plan_mode, _current_plan, _pending_task
    with _lock:
        _ensure_project_state()
        if not _current_plan or not _current_plan.get("work_id"):
            return None
        plan = dict(_current_plan)
        try:
            workgraph.approve_plan(
                plan["work_id"], int(revision), content_sha,
                cwd=plan.get("cwd"))
        except workgraph.WorkGraphError:
            return None
        plan.update({
            "status": "approved", "approved": True,
            "revision": int(revision), "content_sha": content_sha,
            "updated": datetime.now(timezone.utc).isoformat(),
        })
        _write_projection_status(plan, approved=True)
        _plan_mode = False
        _pending_task = False
        _current_plan = None
        _save_state({"plan_mode": False, "current_plan": None})
        return plan


def reject_submitted_plan(revision: int, content_sha: str) -> Optional[dict]:
    """Return a submitted plan to DRAFT without losing its context."""
    _ensure_project_state()
    if not _current_plan or not _current_plan.get("work_id"):
        return None
    try:
        workgraph.reject_plan(
            _current_plan["work_id"], int(revision), content_sha,
            cwd=_current_plan.get("cwd"))
    except workgraph.WorkGraphError:
        return None
    _current_plan["status"] = "drafting"
    _current_plan["approved"] = False
    _save_state({"plan_mode": True, "current_plan": _current_plan})
    return dict(_current_plan)


def _write_projection_status(plan: dict, approved: bool) -> None:
    try:
        p = Path(plan["file"])
        content = p.read_text(encoding="utf-8")
        if approved:
            content = content.replace("**Status:** drafting", "**Status:** approved ✅")
            content = content.replace("**Status:** review_pending", "**Status:** approved ✅")
            content = content.replace("**Approved:** no", "**Approved:** yes")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def get_review_snapshot() -> Optional[dict]:
    _ensure_project_state()
    if not _current_plan or not _current_plan.get("work_id"):
        return None
    try:
        return workgraph.review_snapshot(
            _current_plan["work_id"], cwd=_current_plan.get("cwd"))
    except workgraph.WorkGraphError:
        return None


def attach_work(work_id: str) -> Optional[dict]:
    """Attach an existing draft/review WorkGraph to Plan Mode."""
    global _plan_mode, _current_plan, _pending_task, _loaded_cwd
    with _lock:
        work = workgraph.get_work(work_id)
        if not work:
            return None
        revision = workgraph.get_revision(work_id, work.get("current_revision"))
        if not revision:
            return None
        ensure_plans_dir()
        plan_file = PLANS_DIR / f"{work_id}.md"
        plan_file.write_text(revision["content"], encoding="utf-8")
        _current_plan = {
            "name": work_id,
            "task": work["objective"],
            "file": str(plan_file),
            "created": datetime.fromtimestamp(
                work["created_at"], timezone.utc).isoformat(),
            "updated": datetime.fromtimestamp(
                work["updated_at"], timezone.utc).isoformat(),
            "status": ("review_pending" if work["status"] == "REVIEW_PENDING"
                       else "drafting"),
            "approved": False,
            "cwd": str(Path.cwd().resolve()),
            "work_id": work_id,
            "revision": revision["revision"],
            "content_sha": revision["content_sha"],
        }
        _plan_mode = work["status"] in {"DRAFT", "REVIEW_PENDING", "NEEDS_USER", "BLOCKED"}
        _pending_task = False
        _loaded_cwd = str(Path.cwd().resolve())
        workgraph.set_active_work(work_id)
        _save_state({"plan_mode": _plan_mode, "current_plan": _current_plan if _plan_mode else None})
        return dict(_current_plan)


def begin_amendment() -> Optional[dict]:
    """Fork the active approved revision into a new DRAFT revision."""
    work = workgraph.get_active_work()
    if not work or work.get("status") not in {"APPROVED", "EXECUTING", "VERIFYING"}:
        return None
    revision_no = work.get("approved_revision") or work.get("current_revision")
    revision = workgraph.get_revision(work["id"], revision_no)
    if not revision:
        return None
    try:
        workgraph.add_revision(
            work["id"], revision["content"], author="amendment")
    except workgraph.WorkGraphError:
        return None
    return attach_work(work["id"])


def list_plans() -> list[dict]:
    """List all saved plans."""
    graph_plans = []
    for work in workgraph.list_work():
        if not work.get("current_revision"):
            continue
        revision = workgraph.get_revision(work["id"], work["current_revision"])
        graph_plans.append({
            "name": work["id"],
            "title": work["objective"],
            "file": str(PLANS_DIR / f"{work['id']}.md"),
            "mtime": work["updated_at"],
            "size": len((revision or {}).get("content", "")),
            "status": work["status"],
            "revision": work["current_revision"],
        })
    if graph_plans:
        return graph_plans
    ensure_plans_dir()
    plans = []
    for f in sorted(PLANS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            content = f.read_text(encoding="utf-8")
            # Extract first heading as title
            title = f.stem
            for line in content.split('\n'):
                if line.startswith('# '):
                    title = line[2:].strip()
                    break
            plans.append({
                "name": f.stem,
                "title": title,
                "file": str(f),
                "mtime": f.stat().st_mtime,
                "size": len(content),
            })
        except OSError:
            pass
    return plans


def get_plan_prompt() -> str:
    """Return the plan mode instructions for the AI system prompt."""
    plan = get_current_plan()
    if not plan:
        return (
            "[PLAN MODE ACTIVE]\n"
            "Plan mode is waiting for the user to provide the task. Do not take "
            "implementation actions until a task has been bound."
            if is_pending_task() else ""
        )

    plan_content = read_plan() or ""
    return f"""[PLAN MODE ACTIVE]
You are in PLAN MODE. Your goal is to design an implementation approach — do NOT execute code yet.

Task: {plan['task']}

Work ID: {plan.get('work_id', '(legacy)')}
Revision: {plan.get('revision', 0)}

Current Plan:
{plan_content[:12000]}

Instructions:
1. Explore the codebase to understand the current architecture
2. Identify which files need to change and how
3. Design the implementation approach step by step
4. Update the plan file with your findings using the plan tools
5. When the plan is complete and thorough, call plan.submit. This is the only
   readiness signal and will open a user review; ending your turn is not approval.

DO NOT execute commands that modify files or the system. Use read-only exploration tools (fs.read, fs.grep, fs.glob, fs.ls) to understand the codebase.
"""


def _plan_template(task: str, created: str) -> str:
    return f"""# Plan: {task}

**Created:** {created}
**Status:** drafting
**Approved:** no

---

## Context

[What problem does this task solve? Why is it needed?]

## Exploration

[Files to examine, code to understand, dependencies to map]

## Architecture

[How should this be implemented? What's the design?]

## Implementation Steps

1. [Step 1 — be specific: which files, what changes]
2. [Step 2]
3. ...

## Risks & Edge Cases

[What could go wrong? What edge cases need handling?]

## Test Plan

[How will you verify the implementation works?]
"""
