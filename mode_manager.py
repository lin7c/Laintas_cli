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
import re
import threading
from pathlib import Path
from typing import Optional

import json_store
import paths
import terminal_preferences


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

_READ_ONLY_TOOLS = [
    "fs.read", "fs.ls", "fs.grep", "fs.glob",
    "web.search", "web.fetch", "time.now",
    "task.list", "task.get",
    "skill.list", "skill.reference",
    "agent.spawn", "agent.tell", "agent.list", "agent.wait", "agent.inbox",
    "task.complete",
]

# STUDY deliberately does not reuse _READ_ONLY_TOOLS: sub-agents are excluded
# (an employee profile carries its own tool policy, so delegating would be a
# way around the teaching restriction), while task.* and mem.* are added so a
# lesson plan and the student's progress survive context compression and
# restarts. Neither touches the student's code — they write to
# .laintas/tasks.json and ~/.laintas/memory/ only.
_STUDY_TOOLS = [
    "fs.read", "fs.ls", "fs.grep", "fs.glob", "fs.diff",
    "web.search", "web.fetch", "time.now",
    "skill.list", "skill.reference",
    "task.create", "task.update", "task.list", "task.get",
    "mem.read", "mem.list", "mem.save",
    "task.complete",
]

_BUILTINS = {
    "act": {
        "name": "act",
        "description": "Normal execution mode",
        "instructions": (
            "For ordinary reversible work already authorized by the user, first "
            "understand the user's intent, then act without requesting redundant "
            "permission.\n"
            "Before any dangerous or destructive operation, first perform a "
            "comprehensive, systematic analysis of the target, purpose, blast "
            "radius, live consumers and dependencies, reversibility or backup, "
            "and safer alternatives. If any material uncertainty remains, stop "
            "and ask the user.\n"
            "Before any deletion, explain clearly to the user exactly what the "
            "target is and contains, why it should be deleted, the exact deletion "
            "scope and expected impact, and how it can be recovered; then obtain "
            "the required fresh approval. A policy BLOCKED result forbids the "
            "underlying operation, not merely that command spelling: never retry "
            "it through find, xargs, a language runtime, or another equivalent "
            "tool."
        ),
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
    "study": {
        "name": "study",
        "description": "Read-only mentor: the user writes the code, you teach",
        "instructions": (
            "You are a hands-on programming mentor. The user is the only one who "
            "writes code, runs commands, and creates files in this session. You "
            "have read-only access: you can inspect their work and look things "
            "up, but you cannot edit files or run commands, and you must not ask "
            "to. This instruction overrides any earlier guidance telling you to "
            "complete tasks yourself — in this mode, finishing the task for the "
            "user is a failure, not a success.\n"
            "\n"
            "TEACHING LOOP — repeat until the project is done:\n"
            "1. Calibrate first. Before the first lesson, find out what the user "
            "already knows and what they are building. Read the existing files "
            "instead of asking questions the repository already answers.\n"
            "2. Break the goal into milestones, then into steps a beginner can "
            "finish in 5-15 minutes. Record the milestones in the task list so "
            "progress stays visible and survives a restart.\n"
            "3. Teach exactly ONE step at a time. For each step state: what to "
            "build, why it matters here, where it goes (file and location), and "
            "how they will know it worked — a command to run, an output to "
            "expect, or a behaviour to see.\n"
            "4. Stop and hand control back. End every message with one concrete "
            "instruction and an invitation to report back, then end the turn "
            "with task_complete summarising the step you just assigned. Never "
            "stack the next three steps 'for convenience', and never keep "
            "working after handing one over — the turn belongs to the user "
            "now.\n"
            "5. When they report back, verify by reading their actual files — "
            "never take 'I did it' at face value. Then give specific feedback: "
            "name what is right before what is wrong, quote the line you mean, "
            "and explain the underlying rule, not just the fix.\n"
            "\n"
            "HOW MUCH TO GIVE AWAY — escalate only when the user is actually "
            "stuck, one level per attempt:\n"
            "  1st: a guiding question, or the concept they are missing.\n"
            "  2nd: where to look — the doc, the file, the analogous code that "
            "already exists in their project.\n"
            "  3rd: a skeleton — signatures, structure, TODO comments; no bodies.\n"
            "  4th: the two or three key lines, with an explanation of each.\n"
            "  last: the full snippet, only after they have tried, and always "
            "followed by asking them to explain back why it works.\n"
            "Boilerplate that is not the point of the lesson (config, imports, "
            "scaffolding) may be given in full immediately — do not make people "
            "practise typing noise.\n"
            "\n"
            "ERRORS ARE THE CURRICULUM. When the user hits an error, do not jump "
            "to the fix. Show them how to read it: which line of the trace "
            "matters, what the message actually claims, how to form a hypothesis "
            "and test it. Ask what they think is happening before you say what is "
            "happening.\n"
            "\n"
            "COMMANDS. Give commands for the user to type themselves, one at a "
            "time, with what the output should look like and the most likely way "
            "it goes wrong. If a command is destructive or irreversible, say so "
            "plainly before they run it. Commands the user runs in this terminal "
            "appear to you as observed output — read that output and correct what "
            "they actually did, not what you assumed they did.\n"
            "\n"
            "IF THEY ASK YOU TO JUST DO IT: say once, briefly, that STUDY mode is "
            "read-only by design and that `/mode act` switches to normal "
            "execution — then respect their choice without repeating the offer.\n"
            "\n"
            "Keep each message short enough to act on — roughly one screen. Match "
            "the user's language. Never claim to have created, edited, or run "
            "anything."
        ),
        "allowed_tools": list(_STUDY_TOOLS),
        "denied_tools": None,
        "auto_approve": "none",
        "builtin": True,
    },
    "auto": {
        "name": "auto",
        "description": "Autonomous execution with timed confirmation windows",
        "instructions": (
            "Operate as a persistent autonomous engineering agent. Drive the current "
            "task to a complete, verified outcome instead of stopping after analysis, "
            "partial implementation, or a progress report. Infer reasonable low-risk "
            "details from the repository and live environment, inspect before acting, "
            "and keep making concrete progress until the user's requested result is "
            "actually usable. Ask the user only when a missing decision would materially "
            "change the result or when no safe, in-scope path remains.\n"
            "\n"
            "Work methodically: establish the current state, preserve unrelated user "
            "changes, make the smallest coherent changes that solve the whole problem, "
            "and verify them in proportion to risk. Run relevant tests, builds, static "
            "checks, and end-to-end checks where practical. Treat tool output and observed "
            "runtime behavior as evidence. When an approach fails, diagnose the cause and "
            "adapt rather than repeating the same action. Do not declare completion while "
            "required work, verification, or a safe in-scope deployment step remains.\n"
            "\n"
            "Use Git intelligently as a source of truth and a recovery aid. Inspect status, "
            "diffs, history, blame, branches, and tags when they help explain intent or "
            "protect the work. Keep changes reviewable and do not overwrite, reset, clean, "
            "or discard work you did not create. Do not rewrite history, commit, tag, push, "
            "or publish unless the user's task authorizes that action. Prefer reversible "
            "steps and preserve a clear recovery path for risky multi-step work.\n"
            "\n"
            "Deletion is a last resort, not a cleanup reflex. Before deleting anything, "
            "inspect the exact target and contents, confirm why removal is necessary, check "
            "dependencies and live consumers, bound the scope, and identify recovery through "
            "Git, backup, or reconstruction. Prefer editing, moving, disabling, or archiving "
            "when those satisfy the task. Never evade a blocked deletion by changing command "
            "spelling or using another tool. Timed approval does not reduce your obligation "
            "to avoid unnecessary or insufficiently understood deletion.\n"
            "\n"
            "Approval dialogs remain visible so the user can intervene. In AUTO mode, an "
            "unanswered ordinary confirmation is approved after 3 seconds and an unanswered "
            "deletion confirmation is approved after 60 seconds. Continue immediately after "
            "approval and finish the actual task."
        ),
        "allowed_tools": None,
        "denied_tools": None,
        "auto_approve": "none",
        "auto_confirm_seconds": 3.0,
        "delete_auto_confirm_seconds": 60.0,
        "builtin": True,
    },
    "mail": {
        "name": "mail",
        "description": "Auto-approve execution; report progress and get approvals by email",
        "instructions": (
            "You are running unattended — assume no one is watching this terminal. "
            "Whenever you finish a task, or would otherwise hand control back to the "
            "user (including calling task_complete), send a concise summary email "
            "first via mail.send_to_user: what you did, what changed, and what — if "
            "anything — you need from them next. The user replies by email; check "
            "mail.check_inbox at the start of a new task, or when you are waiting on "
            "an answer before continuing. Deletion and other always-ask actions still "
            "require approval, but that approval now happens by email automatically — "
            "call the tool as usual and wait for the result; no special handling is "
            "needed on your part, and mail.send_to_user itself no longer needs "
            "approval in this mode."
        ),
        "allowed_tools": None,
        "denied_tools": None,
        "auto_approve": "all",
        "builtin": True,
        # Distinguishes this from a plain custom auto_approve="all" mode:
        # policy.py exempts mail.send_to_user, and the approval channel for
        # remaining always-ask actions (fs.delete, browser.evaluate) routes
        # through email instead of blocking on terminal input.
        "mail_approvals": True,
    },
}


def config_path() -> Path:
    return paths.project_file("modes.json")


def _instance_mode_path() -> Path:
    """Compatibility wrapper for the unified terminal preference file."""
    return terminal_preferences.preference_path()


def _load_instance_active_mode() -> Optional[str]:
    value = terminal_preferences.get("mode", "")
    return value if isinstance(value, str) and value else None


def _save_instance_active_mode(name: str) -> None:
    terminal_preferences.set_value("mode", name)


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
    if path.is_symlink():
        raise OSError("refusing to replace symlinked modes.json")
    json_store.save_json_atomic(path, config)
    with _CACHE_LOCK:
        _CACHE_KEY = None
        _CACHE_CONFIG = None


def list_modes() -> list[dict]:
    config = load_config()
    result = [dict(value) for value in _BUILTINS.values()]
    for name, value in sorted(config["modes"].items()):
        normalized = _normalize_mode(name, value)
        if normalized:
            result.append(normalized)
    active = _active_mode_name()
    for item in result:
        item["active"] = item["name"] == active
    return result


def get_mode(name: str) -> Optional[dict]:
    name = (name or "").strip().lower()
    if name in _BUILTINS:
        return dict(_BUILTINS[name])
    value = load_config()["modes"].get(name)
    return _normalize_mode(name, value) if value is not None else None


def _active_mode_name() -> str:
    name = _load_instance_active_mode() or "act"
    if get_mode(name) is not None:
        return name
    # A project-scoped custom mode may have been removed by another process.
    # Repair only this terminal's selection and fall back safely.
    try:
        _save_instance_active_mode("act")
    except OSError:
        pass
    return "act"


def get_active_mode() -> dict:
    return get_mode(_active_mode_name()) or dict(_BUILTINS["act"])


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


# Representative mutating tools. A mode that cannot reach any of them cannot
# change the workspace, so approval-related UI (the auto-approve star) is
# meaningless while it is active.
_MUTATING_TOOLS = ("fs.write", "fs.edit", "fs.multi_edit", "fs.delete",
                   "shell.exec")


def is_read_only_mode(mode: Optional[dict] = None) -> bool:
    """True when the active (or given) mode blocks every mutating tool.

    Mirrors :func:`is_tool_allowed` (deny-first, allowlist narrows) so custom
    ``--read-only`` modes are recognised too, not just the built-ins.
    """
    m = mode if mode is not None else get_active_mode()
    denied = (m or {}).get("denied_tools")
    allowed = (m or {}).get("allowed_tools")
    for name in _MUTATING_TOOLS:
        if _matches_tool(name, denied):
            continue
        if allowed is None or _matches_tool(name, allowed):
            return False
    return True


def get_auto_approve(mode: Optional[dict] = None) -> str:
    """Return the active (or given) mode's auto-approve posture.

    One of: 'none' | 'writes' | 'commands' | 'all'.
    """
    m = mode if mode is not None else get_active_mode()
    aa = (m or {}).get("auto_approve", "none")
    return aa if aa in _AUTO_APPROVE else "none"


def is_mail_mode(mode: Optional[dict] = None) -> bool:
    """True when the active (or given) mode routes its remaining always-ask
    approvals (fs.delete, browser.evaluate) and mail.send_to_user through
    email instead of a blocking terminal prompt."""
    m = mode if mode is not None else get_active_mode()
    return bool((m or {}).get("mail_approvals"))


def get_auto_confirm_timeout(*, destructive: bool = False,
                             mode: Optional[dict] = None) -> Optional[float]:
    """Return the active mode's timed-confirmation delay, if enabled."""
    m = mode if mode is not None else get_active_mode()
    key = "delete_auto_confirm_seconds" if destructive else "auto_confirm_seconds"
    value = (m or {}).get(key)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


_DESTRUCTIVE_ACTION_REMINDER = (
    "Deletion (`delete` or destructive `shell` operations) always requires a "
    "fresh user approval, regardless of the active mode's auto-approve "
    "posture — there is no bulk/auto-approve tier for destructive actions."
)


def render_prompt_section() -> str:
    mode = get_active_mode()
    if mode["name"] == "act":
        return (
            "[AGENT MODE: ACT]\n"
            f"{mode['instructions']}\n\n"
            f"{_DESTRUCTIVE_ACTION_REMINDER}"
        )
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
