"""Agent state persistence — survives across sessions.

Each hired agent is serialized to `~/.laintas/agents/<agent_id>.json` after
significant state changes. A hired agent is owned by exactly one deployment
terminal; ending that terminal ends the agent and removes its persisted record.

Threading model:
- Only the process that owns the agent (parent for pool agents, child for
  deployed agents inside a sub-terminal) writes the file.
- Other processes treat the file as read-only metadata.
- Writes are atomic via tmp-file + os.replace.

What is persisted:
- id, name, role, depth, parent_id
- parent_terminal, home_terminal, stationed_terminal
- chat_history, state (shortTermMemory / lastReply / lastOutput)
- employee profile, tool policy, assignment history
- created_at, last_saved

What is NOT persisted (runtime-only):
- inbox (Queue), thread, abort_event, status, child_ids
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_loop import AgentInfo


import json_store
import paths

AGENTS_DIR = paths.AGENTS_DIR
_MAX_HISTORY_TURNS = 200  # truncate older turns to keep files manageable


def _ensure_dir() -> None:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(AGENTS_DIR, 0o700)
    except OSError:
        pass


def _agent_file(agent_id: str) -> Path:
    return AGENTS_DIR / f"{agent_id}.json"


def save_agent_state(agent: "AgentInfo") -> bool:
    """Atomically write agent state to disk. Returns True on success."""
    try:
        _ensure_dir()
    except OSError:
        return False

    history = list(getattr(agent, "chat_history", []) or [])
    if len(history) > _MAX_HISTORY_TURNS:
        history = history[-_MAX_HISTORY_TURNS:]

    data = {
        "id": agent.id,
        "name": agent.name,
        "role": getattr(agent, "role", "pool"),
        "depth": getattr(agent, "depth", 0),
        "parent_id": getattr(agent, "parent_id", None),
        "parent_terminal": getattr(agent, "parent_terminal", None),
        "home_terminal": getattr(agent, "home_terminal", None),
        "stationed_terminal": getattr(agent, "stationed_terminal", None),
        "chat_history": history,
        "state": dict(getattr(agent, "state", {}) or {}),
        "profile": _serialize_profile(getattr(agent, "profile", None)),
        "assignment_history": list(
            getattr(agent, "assignment_history", []) or [])[-100:],
        "active_assignment": _serialize_assignment(
            getattr(agent, "active_assignment", None)),
        "created_at": getattr(agent, "created_at", time.time()),
        "last_saved": time.time(),
    }

    try:
        json_store.save_json_atomic(_agent_file(agent.id), data,
                                    mode=stat.S_IRUSR | stat.S_IWUSR)
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_agent_state(agent_id: str) -> Optional[dict]:
    """Read and return the persisted dict; None if not found or unreadable."""
    data = json_store.load_json(_agent_file(agent_id))
    return data if isinstance(data, dict) else None


def delete_agent_state(agent_id: str) -> bool:
    """Remove the persisted file for an agent. Returns True if it existed."""
    target = _agent_file(agent_id)
    try:
        if target.exists():
            target.unlink()
            return True
    except OSError:
        pass
    return False


def _serialize_profile(profile) -> dict:
    if profile is None:
        return {}
    policy = getattr(profile, "tool_policy", None)
    return {
        "title": getattr(profile, "title", "General Agent"),
        "description": getattr(
            profile, "description", "General-purpose autonomous employee"),
        "specialist_role": getattr(profile, "specialist_role", None),
        "prompt": getattr(profile, "prompt", ""),
        "capability_tags": list(
            getattr(profile, "capability_tags", []) or []),
        "tool_policy": {
            "allowed_tools": (
                list(policy.allowed_tools)
                if policy is not None and policy.allowed_tools is not None
                else None
            ),
            "denied_tools": list(
                getattr(policy, "denied_tools", []) or []),
        },
    }


def _serialize_assignment(assignment) -> Optional[dict]:
    if assignment is None:
        return None
    return {
        key: getattr(assignment, key, None)
        for key in (
            "id", "task", "terminal_name", "status", "created_at",
            "started_at", "completed_at", "result", "error",
        )
    }


def list_persisted_agents() -> list[dict]:
    """List all persisted agents (metadata only, chat_history omitted)."""
    if not AGENTS_DIR.exists():
        return []
    out = []
    for p in sorted(AGENTS_DIR.glob("*.json")):
        data = json_store.load_json(p)
        if isinstance(data, dict) and data.get("id"):
            meta = {k: v for k, v in data.items() if k != "chat_history"}
            meta["history_turns"] = len(data.get("chat_history", []))
            out.append(meta)
    return out


def apply_persisted_state(agent: "AgentInfo", data: dict) -> None:
    """Hydrate a freshly registered AgentInfo from a persisted dict.

    Only restores fields that make sense across processes — runtime-only
    fields (inbox, thread, abort_event, status) are left at their defaults.
    """
    for key in ("name", "role", "parent_id", "parent_terminal",
                "home_terminal", "stationed_terminal", "created_at"):
        if key in data and data[key] is not None:
            try:
                setattr(agent, key, data[key])
            except AttributeError:
                pass
    if isinstance(data.get("chat_history"), list):
        agent.chat_history = list(data["chat_history"])
    if isinstance(data.get("state"), dict):
        merged = dict(agent.state or {})
        merged.update(data["state"])
        agent.state = merged
    if str(data.get("role") or "") in {"pool", "deployed"}:
        # Legacy employee files predate the explicit lifecycle marker. Once
        # restored, they follow the deployment terminal just like new hires.
        agent.state.setdefault("_persisted_employee", True)
    profile_data = data.get("profile")
    if isinstance(profile_data, dict) and profile_data:
        # Import lazily to keep this persistence module free of an import cycle.
        from agent_loop import AgentToolPolicy, EmployeeProfile
        policy_data = profile_data.get("tool_policy") or {}
        allowed = policy_data.get("allowed_tools")
        agent.profile = EmployeeProfile(
            title=str(profile_data.get("title") or "General Agent"),
            description=str(profile_data.get("description") or ""),
            specialist_role=profile_data.get("specialist_role") or None,
            prompt=str(profile_data.get("prompt") or ""),
            capability_tags=[str(item) for item in
                             profile_data.get("capability_tags", [])],
            tool_policy=AgentToolPolicy(
                allowed_tools=(
                    [str(item) for item in allowed]
                    if isinstance(allowed, list) else None),
                denied_tools=[str(item) for item in
                              policy_data.get("denied_tools", [])],
            ),
        )
        active_data = data.get("active_assignment")
        if isinstance(active_data, dict) and active_data.get("id"):
            # A process restart cannot resume the old thread automatically.
            active_data = dict(active_data)
            active_data["status"] = "interrupted"
            agent.assignment_history.append(active_data)
            agent.active_assignment = None
    if isinstance(data.get("assignment_history"), list):
        existing_ids = {
            str(item.get("id")) for item in agent.assignment_history
            if isinstance(item, dict)
        }
        agent.assignment_history.extend(
            item for item in data["assignment_history"]
            if isinstance(item, dict) and str(item.get("id")) not in existing_ids
        )
        agent.assignment_history = agent.assignment_history[-100:]
