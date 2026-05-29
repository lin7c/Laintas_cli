"""
Plan Mode for laintas_cli — structured planning before execution.

Inspired by Claude Code's Plan Mode: before executing complex tasks, the AI
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

import os
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


import paths

PLANS_DIR = paths.PLANS_DIR
_STATE_PATH = paths.PLANS_STATE

_lock = threading.RLock()

# Current plan state (in-memory, synced to disk)
_current_plan: Optional[dict] = None
_plan_mode: bool = False


def ensure_plans_dir() -> Path:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    return PLANS_DIR


def _load_state() -> dict:
    """Load plan mode state from disk."""
    if not _STATE_PATH.exists():
        return {}
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    """Save plan mode state to disk."""
    ensure_plans_dir()
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def is_plan_mode() -> bool:
    """Check if the agent is currently in plan mode."""
    global _plan_mode
    return _plan_mode


def enter_plan_mode(task: str) -> dict:
    """Enter plan mode for a given task.

    Creates a new plan file and sets plan mode active.
    Returns the plan metadata.
    """
    global _plan_mode, _current_plan
    with _lock:
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
        }

        # Write template
        plan_file.write_text(_plan_template(task, now), encoding="utf-8")

        _current_plan = plan
        _plan_mode = True
        _save_state({"plan_mode": True, "current_plan": plan})

        return dict(plan)


def exit_plan_mode(approve: bool = False) -> Optional[dict]:
    """Exit plan mode. If approved, the plan is locked and ready for execution.

    Returns the final plan or None.
    """
    global _plan_mode, _current_plan
    with _lock:
        plan = dict(_current_plan) if _current_plan else None
        if plan and approve:
            plan["status"] = "approved"
            plan["approved"] = True
            plan["updated"] = datetime.now(timezone.utc).isoformat()
            # Update the plan file
            try:
                p = Path(plan["file"])
                content = p.read_text(encoding="utf-8")
                content = content.replace("Status: drafting", "Status: approved ✅")
                p.write_text(content, encoding="utf-8")
            except OSError:
                pass

        _plan_mode = False
        _current_plan = None
        _save_state({"plan_mode": False, "current_plan": None})

        return plan


def get_current_plan() -> Optional[dict]:
    """Get the current plan, if any."""
    return dict(_current_plan) if _current_plan else None


def read_plan(name: str = None) -> Optional[str]:
    """Read a plan file. If name is None, reads the current plan."""
    if name:
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


def update_plan(content: str) -> bool:
    """Overwrite the current plan file. Returns True on success."""
    if not _current_plan:
        return False
    try:
        p = Path(_current_plan["file"])
        p.write_text(content, encoding="utf-8")
        _current_plan["updated"] = datetime.now(timezone.utc).isoformat()
        _save_state({"plan_mode": True, "current_plan": _current_plan})
        return True
    except OSError:
        return False


def list_plans() -> list[dict]:
    """List all saved plans."""
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
        return ""

    plan_content = read_plan() or ""
    return f"""[PLAN MODE ACTIVE]
You are in PLAN MODE. Your goal is to design an implementation approach — do NOT execute code yet.

Task: {plan['task']}

Current Plan:
{plan_content[:3000]}

Instructions:
1. Explore the codebase to understand the current architecture
2. Identify which files need to change and how
3. Design the implementation approach step by step
4. Update the plan file with your findings using the plan tools
5. When your plan is complete and thorough, tell the user to run /plan approve

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
