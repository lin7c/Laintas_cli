"""Declarative agent modes for laintas_cli.

Plan mode remains owned by :mod:`plan_mode` because it has a reviewed-plan
lifecycle.  This module manages lightweight modes that add prompt guidance and
optionally restrict the tool catalog.  Restrictions are intersected with the
global security policy, workflow and role restrictions; they can never grant
additional access.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Optional

import paths


_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Per-mode approval posture. Activating a mode sets the session auto-approve
# flags accordingly; hard `deny` policy rules always still apply.
_AUTO_APPROVE = ("none", "writes", "commands", "all")


def _normalize_tool_list(value: object) -> Optional[list[str]]:
    """Coerce a tool allow/deny value into a clean list, or None if invalid/empty.

    Entries are tool names or fnmatch globs (e.g. ``fs.*``, ``shell.exec``).
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    items = [item.strip() for item in value
             if isinstance(item, str) and item.strip()]
    return list(dict.fromkeys(items)) or None


def _matches_tool(name: str, patterns: Optional[list[str]]) -> bool:
    """True if ``name`` matches any exact name or fnmatch glob in ``patterns``."""
    if not patterns:
        return False
    for pat in patterns:
        if name == pat or fnmatch.fnmatchcase(name, pat):
            return True
    return False
_CACHE_LOCK = threading.RLock()
_CACHE_KEY: tuple[str, Optional[int]] | None = None
_CACHE_CONFIG: Optional[dict] = None
_INSTANCE_ACTIVE_MODE: Optional[str] = None

_READ_ONLY_TOOLS = [
    "fs.read", "fs.ls", "fs.grep", "fs.glob",
    "web.search", "web.fetch", "time.now",
    "task.list", "task.get",
    "skill.list", "skill.reference",
    "agent.spawn", "agent.tell", "agent.list", "agent.wait", "agent.inbox",
    "task.continue", "task.complete",
]

_BUILTINS = {
    "act": {
        "name": "act",
        "description": "Normal execution mode",
        "instructions": "",
        "allowed_tools": None,
        "denied_tools": None,
        "auto_approve": "none",
        "builtin": True,
    },
    "review": {
        "name": "review",
        "description": "Read-only code and design review",
        "instructions": (
            "Review the requested code or design. Identify concrete defects, risks, "
            "missing tests, and maintainability problems. Cite relevant files and "
            "prioritize findings. Do not modify files or the system."
        ),
        "allowed_tools": list(_READ_ONLY_TOOLS),
        "denied_tools": None,
        "auto_approve": "none",
        "builtin": True,
    },
}


def config_path() -> Path:
    return paths.project_file("modes.json")


def _instance_mode_path() -> Path:
    """Store the selected mode per CLI instance, not in project config."""
    instance_id = getattr(paths, "INSTANCE_ID", f"pid-{os.getpid()}")
    return paths.SESSIONS_DIR / f"{instance_id}_mode.json"


def _load_instance_active_mode() -> Optional[str]:
    global _INSTANCE_ACTIVE_MODE
    if _INSTANCE_ACTIVE_MODE is not None:
        return _INSTANCE_ACTIVE_MODE
    path = _instance_mode_path()
    try:
        if not path.exists() or not paths.ensure_private_file(path):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("active") if isinstance(data, dict) else None
        _INSTANCE_ACTIVE_MODE = value if isinstance(value, str) else ""
    except (OSError, ValueError):
        _INSTANCE_ACTIVE_MODE = ""
    return _INSTANCE_ACTIVE_MODE or None


def _save_instance_active_mode(name: str) -> None:
    global _INSTANCE_ACTIVE_MODE
    path = _instance_mode_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "active": name}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        _INSTANCE_ACTIVE_MODE = name
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _default_config() -> dict:
    return {"version": 1, "active": "act", "modes": {}}


def _normalize_mode(name: str, value: object) -> Optional[dict]:
    if not _NAME.fullmatch(name) or name in _BUILTINS or name == "plan":
        return None
    if not isinstance(value, dict):
        return None
    instructions = value.get("instructions", "")
    description = value.get("description", "")
    if not isinstance(instructions, str) or not instructions.strip():
        return None
    if not isinstance(description, str):
        return None
    # allowed_tools: None = all; a list restricts (names or fnmatch globs).
    # An explicitly-provided-but-invalid list is a hard reject; None stays None.
    allowed = value.get("allowed_tools")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(
                isinstance(item, str) and item.strip() for item in allowed):
            return None
        allowed = _normalize_tool_list(allowed)
    denied = _normalize_tool_list(value.get("denied_tools"))
    auto_approve = value.get("auto_approve", "none")
    if auto_approve not in _AUTO_APPROVE:
        auto_approve = "none"
    return {
        "name": name,
        "description": description.strip() or f"Custom mode: {name}",
        "instructions": instructions.strip(),
        "allowed_tools": allowed,
        "denied_tools": denied,
        "auto_approve": auto_approve,
        "builtin": False,
    }


def load_config() -> dict:
    global _CACHE_KEY, _CACHE_CONFIG
    path = config_path()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    cache_key = (str(path), mtime_ns)
    with _CACHE_LOCK:
        if _CACHE_KEY == cache_key and _CACHE_CONFIG is not None:
            return copy.deepcopy(_CACHE_CONFIG)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = _default_config()
    else:
        if not isinstance(data, dict) or data.get("version") != 1:
            config = _default_config()
        else:
            raw_modes = data.get("modes")
            modes = {}
            if isinstance(raw_modes, dict):
                for name, value in raw_modes.items():
                    if isinstance(name, str):
                        normalized = _normalize_mode(name, value)
                        if normalized:
                            modes[name] = {
                                k: normalized[k] for k in (
                                    "description", "instructions",
                                    "allowed_tools", "denied_tools",
                                    "auto_approve")
                            }
            active = data.get("active", "act")
            if active not in _BUILTINS and active not in modes:
                active = "act"
            config = {"version": 1, "active": active, "modes": modes}
    with _CACHE_LOCK:
        _CACHE_KEY = cache_key
        _CACHE_CONFIG = copy.deepcopy(config)
    return config


def _save_config(config: dict) -> None:
    global _CACHE_KEY, _CACHE_CONFIG
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("refusing to replace symlinked modes.json")
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        with _CACHE_LOCK:
            _CACHE_KEY = None
            _CACHE_CONFIG = None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def list_modes() -> list[dict]:
    config = load_config()
    result = [dict(value) for value in _BUILTINS.values()]
    for name, value in sorted(config["modes"].items()):
        normalized = _normalize_mode(name, value)
        if normalized:
            result.append(normalized)
    active = _load_instance_active_mode() or "act"
    for item in result:
        item["active"] = item["name"] == active
    return result


def get_mode(name: str) -> Optional[dict]:
    name = (name or "").strip().lower()
    if name in _BUILTINS:
        return dict(_BUILTINS[name])
    value = load_config()["modes"].get(name)
    return _normalize_mode(name, value) if value is not None else None


def get_active_mode() -> dict:
    return get_mode(_load_instance_active_mode() or "act") or dict(_BUILTINS["act"])


def activate(name: str) -> tuple[bool, str]:
    name = (name or "").strip().lower()
    if get_mode(name) is None:
        return False, f"Unknown mode: {name}"
    try:
        _save_instance_active_mode(name)
    except OSError as exc:
        return False, f"Could not activate mode: {exc}"
    return True, f"Switched to {name.upper()} mode."


def create_mode(name: str, instructions: str, *, read_only: bool = False,
                description: str = "", allowed_tools: Optional[list] = None,
                denied_tools: Optional[list] = None,
                auto_approve: str = "none") -> tuple[bool, str]:
    name = (name or "").strip().lower()
    if not _NAME.fullmatch(name):
        return False, "Mode names must start with a letter and contain only a-z, 0-9, _ or -."
    if name in _BUILTINS or name == "plan":
        return False, f"Mode name is reserved: {name}"
    if not (instructions or "").strip():
        return False, "Mode instructions cannot be empty."
    auto_approve = (auto_approve or "none").strip().lower()
    if auto_approve not in _AUTO_APPROVE:
        return False, f"auto_approve must be one of: {', '.join(_AUTO_APPROVE)}."
    # Explicit --tools wins; --read-only is sugar for the read-only tool set.
    allowed = _normalize_tool_list(allowed_tools)
    if allowed is None and read_only:
        allowed = list(_READ_ONLY_TOOLS)
    denied = _normalize_tool_list(denied_tools)
    config = load_config()
    if name in config["modes"]:
        return False, f"Mode already exists: {name}"
    config["modes"][name] = {
        "description": (description or f"Custom mode: {name}").strip(),
        "instructions": instructions.strip(),
        "allowed_tools": allowed,
        "denied_tools": denied,
        "auto_approve": auto_approve,
    }
    try:
        _save_config(config)
    except OSError as exc:
        return False, f"Could not create mode: {exc}"
    return True, f"Created mode {name}. Activate it with /mode {name}."


def delete_mode(name: str) -> tuple[bool, str]:
    name = (name or "").strip().lower()
    if name in _BUILTINS or name == "plan":
        return False, f"Built-in mode cannot be deleted: {name}"
    config = load_config()
    if name not in config["modes"]:
        return False, f"Unknown mode: {name}"
    del config["modes"][name]
    if _load_instance_active_mode() == name:
        try:
            _save_instance_active_mode("act")
        except OSError as exc:
            return False, f"Could not reset active mode: {exc}"
    try:
        _save_config(config)
    except OSError as exc:
        return False, f"Could not delete mode: {exc}"
    return True, f"Deleted mode {name}."


def is_tool_allowed(tool_name: str) -> bool:
    """Whether the active mode permits ``tool_name``.

    Deny-first: a match in denied_tools blocks even if allowed_tools would
    permit it. allowed_tools is None → all tools pass (subject to deny).
    Both lists support exact names and fnmatch globs (e.g. ``fs.*``). This is
    only ever a *narrowing* — it is intersected with the security policy,
    workflow and role restrictions and can never grant extra access.
    """
    mode = get_active_mode()
    if _matches_tool(tool_name, mode.get("denied_tools")):
        return False
    allowed = mode.get("allowed_tools")
    if allowed is None:
        return True
    return _matches_tool(tool_name, allowed)


def get_auto_approve(mode: Optional[dict] = None) -> str:
    """Return the active (or given) mode's auto-approve posture.

    One of: 'none' | 'writes' | 'commands' | 'all'.
    """
    m = mode if mode is not None else get_active_mode()
    aa = (m or {}).get("auto_approve", "none")
    return aa if aa in _AUTO_APPROVE else "none"


def render_prompt_section() -> str:
    mode = get_active_mode()
    if mode["name"] == "act":
        return ""
    allowed = mode.get("allowed_tools")
    denied = mode.get("denied_tools")
    parts = []
    if allowed is not None:
        parts.append("Tools are restricted to: " + ", ".join(allowed))
    if denied:
        parts.append("These tools are blocked: " + ", ".join(denied))
    restriction = (" ".join(parts) if parts else
                   "This mode does not add tool restrictions; the global policy still applies.")
    return (
        f"[AGENT MODE: {mode['name'].upper()}]\n"
        f"{mode['instructions']}\n\n{restriction}"
    )
