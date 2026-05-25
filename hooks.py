"""
Hooks system for laintas_cli — extensibility without modifying core code.

Replaces and extends the static .extra_command.py / .loop_command.py pattern
with a richer event-driven hook system.

Hook types:
  - pre_command: before shell command execution (can block)
  - post_command: after shell command execution
  - pre_tool: before tool invocation (can block)
  - post_tool: after tool invocation
  - on_session_start: when REPL starts
  - on_session_end: when REPL exits
  - on_error: when agent loop encounters an error
  - on_memory_change: when .helpwo is modified

Config: ~/.laintas_cli_hooks.json
Each hook specifies: type, command (shell), and optional condition (Python expression).
Hooks can be Python functions in ~/.laintas_cli_hooks.py (loaded dynamically).

Like .extra_command.py, the Python hooks file is mtime-cached for zero-restart updates.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


CONFIG_PATH = Path.home() / ".laintas_cli_hooks.json"
PYTHON_HOOKS_PATH = Path.home() / ".laintas_cli_hooks.py"

# ── Module-level cache ────────────────────────────────────────────────────
_hooks_config: list[dict] | None = None
_hooks_config_mtime: float = 0.0
_python_hooks: dict | None = None
_python_hooks_mtime: float = 0.0
_lock = threading.RLock()


def _load_config(force: bool = False) -> list[dict]:
    """Load hooks config from disk, mtime-cached."""
    global _hooks_config, _hooks_config_mtime
    with _lock:
        if not force and CONFIG_PATH.exists():
            try:
                mtime = CONFIG_PATH.stat().st_mtime
                if _hooks_config is not None and mtime == _hooks_config_mtime:
                    return _hooks_config
                _hooks_config_mtime = mtime
            except OSError:
                pass

        if not CONFIG_PATH.exists():
            _write_default_config()
            _hooks_config = []
            return _hooks_config

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _hooks_config = data.get("hooks", [])
            elif isinstance(data, list):
                _hooks_config = data
            else:
                _hooks_config = []
        except (OSError, json.JSONDecodeError):
            _hooks_config = []
        return _hooks_config


def _write_default_config() -> None:
    """Write a default hooks config with examples."""
    template = {
        "hooks": [
            {
                "type": "pre_command",
                "comment": "Example: log every command before execution",
                "command": None,
                "enabled": False,
            },
            {
                "type": "post_command",
                "comment": "Example: notify on long-running commands",
                "command": None,
                "condition": "duration_ms > 5000",
                "enabled": False,
            },
            {
                "type": "on_error",
                "comment": "Example: log errors to a file",
                "command": "echo '[{timestamp}] Error: {error}' >> ~/laintas_errors.log",
                "enabled": False,
            },
        ]
    }
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _load_python_hooks() -> dict:
    """Load ~/.laintas_cli_hooks.py dynamically. mtime-cached."""
    global _python_hooks, _python_hooks_mtime
    if not PYTHON_HOOKS_PATH.exists():
        return {}

    try:
        mtime = PYTHON_HOOKS_PATH.stat().st_mtime
        if _python_hooks is not None and mtime == _python_hooks_mtime:
            return _python_hooks
        _python_hooks_mtime = mtime

        with open(PYTHON_HOOKS_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        ns: dict = {}
        exec(compile(src, str(PYTHON_HOOKS_PATH), "exec"), ns)

        hooks = {}
        for hook_name in ("pre_command", "post_command", "pre_tool", "post_tool",
                          "on_session_start", "on_session_end", "on_error",
                          "on_memory_change"):
            fn = ns.get(hook_name)
            if callable(fn):
                hooks[hook_name] = fn

        _python_hooks = hooks
        return hooks
    except Exception:
        return {}


def _eval_condition(condition: str, ctx: dict) -> bool:
    """Evaluate a simple condition expression against the context.

    Supports: ==, !=, >, <, >=, <=, 'in', 'not in', 'contains', boolean vars.
    Uses the context dict as the namespace.
    """
    if not condition:
        return True

    # Allow simple Python expressions in a safe namespace
    safe_ns = {k: v for k, v in ctx.items()
               if isinstance(v, (str, int, float, bool, type(None)))}
    safe_ns["__builtins__"] = {
        "True": True, "False": False, "None": None,
        "len": len, "str": str, "int": int, "float": float, "bool": bool,
        "isinstance": isinstance,
    }

    try:
        result = eval(condition, {"__builtins__": safe_ns["__builtins__"]}, safe_ns)
        return bool(result)
    except Exception:
        return True  # if condition is broken, don't block the hook


def _run_shell_hook(hook: dict, ctx: dict) -> Optional[str]:
    """Execute a shell command hook. Returns stdout or None on failure."""
    cmd_template = hook.get("command", "")
    if not cmd_template:
        return None

    # Substitute template variables
    cmd = cmd_template
    for key, val in ctx.items():
        if isinstance(val, (str, int, float, bool)):
            cmd = cmd.replace(f"{{{key}}}", str(val))

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=hook.get("timeout", 10),
            cwd=os.getcwd(),
        )
        return (result.stdout + result.stderr).strip() or None
    except Exception:
        return None


def _run_python_hook(hook_name: str, ctx: dict) -> Optional[bool]:
    """Execute a Python hook function. Returns False to block, True to allow, None if no hook."""
    hooks = _load_python_hooks()
    fn = hooks.get(hook_name)
    if fn is None:
        return None
    try:
        result = fn(ctx)
        if result is False:
            return False
        return True
    except Exception:
        return True  # Don't block on hook errors


def trigger(hook_type: str, ctx: dict = None) -> tuple[bool, list[str]]:
    """Trigger all enabled hooks of a given type.

    Returns (allowed: bool, messages: list[str]).
    If allowed is False, pre_ hooks should block execution.
    Messages are hook outputs for logging/display.
    """
    ctx = dict(ctx or {})
    ctx.setdefault("timestamp", datetime.now().isoformat())
    ctx.setdefault("cwd", os.getcwd())

    config_hooks = _load_config()
    messages: list = []

    # ── Python hooks first (take precedence) ──
    if hook_type in ("pre_command", "pre_tool"):
        result = _run_python_hook(hook_type, ctx)
        if result is False:
            return False, ["Blocked by Python hook"]

    # ── Config shell hooks ──
    for hook in config_hooks:
        if hook.get("type") != hook_type:
            continue
        if not hook.get("enabled", True):
            continue

        condition = hook.get("condition", "")
        if condition and not _eval_condition(condition, ctx):
            continue

        output = _run_shell_hook(hook, ctx)
        if output:
            messages.append(output)

        # pre_ hooks can block via exit code
        if hook_type.startswith("pre_") and hook.get("block_on_failure"):
            # If a pre_ hook's command exits non-zero, consider it a block
            # (We can't check exit code here, but the hook can output "BLOCK")
            if output and "BLOCK" in output:
                return False, messages + ["Blocked by hook"]

    # ── Python post hooks ──
    if hook_type not in ("pre_command", "pre_tool"):
        _run_python_hook(hook_type, ctx)

    return True, messages


def reload() -> dict:
    """Force-reload hooks config and Python hooks. Returns status info."""
    global _hooks_config, _python_hooks
    _hooks_config = None
    _python_hooks = None
    config = _load_config(force=True)
    py = _load_python_hooks()
    return {
        "shell_hooks": len(config),
        "python_hooks": list(py.keys()),
    }
