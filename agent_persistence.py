"""Agent state persistence — survives across sessions.

Each agent is serialized to `~/.laintas/agents/<agent_id>.json` after every
significant state change (chat turn, station/unstation, terminate). Sub-terminal
processes load their assigned agent's state at startup, so re-stationing a
previously-deployed agent restores its conversation history.

Threading model:
- Only the process that owns the agent (parent for pool agents, child for
  deployed agents inside a sub-terminal) writes the file.
- Other processes treat the file as read-only metadata.
- Writes are atomic via tmp-file + os.replace.

What is persisted:
- id, name, role, depth, parent_id
- parent_terminal, home_terminal, stationed_terminal
- chat_history, state (shortTermMemory / lastReply / lastOutput)
- created_at, last_saved

What is NOT persisted (runtime-only):
- inbox (Queue), thread, abort_event, status, child_ids
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_loop import AgentInfo


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
        "created_at": getattr(agent, "created_at", time.time()),
        "last_saved": time.time(),
    }

    target = _agent_file(agent.id)
    tmp = target.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def load_agent_state(agent_id: str) -> Optional[dict]:
    """Read and return the persisted dict; None if not found or unreadable."""
    target = _agent_file(agent_id)
    if not target.exists():
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, ValueError):
        return None


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


def list_persisted_agents() -> list[dict]:
    """List all persisted agents (metadata only, chat_history omitted)."""
    if not AGENTS_DIR.exists():
        return []
    out = []
    for p in sorted(AGENTS_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("id"):
                meta = {k: v for k, v in data.items() if k != "chat_history"}
                meta["history_turns"] = len(data.get("chat_history", []))
                out.append(meta)
        except (OSError, ValueError):
            continue
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
