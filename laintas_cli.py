#!/usr/bin/env python3
"""
laintas_cli — Autonomous AI agent for your terminal.
Same agent loop as Helpwo, but executes real system commands.

Usage:
    laintas-cli                    # Start interactive session in cwd
    laintas-cli --resume           # Resume saved conversation for cwd
    laintas-cli --name my-server   # Set agent name
    laintas-cli --backend URL      # Custom backend URL
"""

import os
import re
import sys
import json
import time
import uuid
import errno
import queue
import shlex
import base64
import hashlib
import signal
import socket
import tempfile
import platform
import webbrowser
import threading
import subprocess
import difflib
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

# ── OS Detection (must come before Unix-specific imports) ────────────────
SYSTEM = platform.system()  # "Linux", "Windows", "Darwin"
IS_WINDOWS = SYSTEM == "Windows"

# Resolved once, at import time, while cwd is still the launch directory.
# sys.argv[0] is often relative (e.g. "laintas_cli.py") — os.execv() resolves
# a relative path against the CURRENT cwd, not the launch cwd, so /reload
# would break after a real `cd` moved the process elsewhere. Capturing the
# absolute path now makes restart correct regardless of later cwd changes.
_LAUNCH_SCRIPT_PATH = os.path.abspath(sys.argv[0]) if sys.argv else __file__

# Windows cmd.exe internal commands — not on PATH but always available
_WINDOWS_CMD_BUILTINS = {
    # Navigation
    "dir", "cd", "chdir", "md", "mkdir", "rd", "rmdir", "tree",
    "pushd", "popd", "dironly",
    # Files
    "copy", "del", "erase", "type", "ren", "rename", "move",
    "attrib", "icacls", "replace", "robocopy", "xcopy",
    # Text
    "find", "findstr", "more", "sort", "comp", "fc",
    # System
    "cls", "ver", "vol", "date", "time", "tasklist", "taskkill",
    "shutdown", "systeminfo", "driverquery",
    # Shell
    "echo", "set", "prompt", "title", "color", "exit", "start",
    "call", "cmd", "doskey", "path", "pause", "rem",
    # Utility
    "where", "whoami", "assoc", "ftype", "chkdsk", "mklink",
    "help", "print", "clip",
}

# Unix-only modules (don't exist on Windows)
if not IS_WINDOWS:
    import pty
    import select
    import fcntl
    import termios
    import tty

import requests
from rich.console import Console, Group
from rich.panel import Panel
from rich.padding import Padding
from rich.markdown import Markdown
from rich.table import Table
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.theme import Theme
from rich import box

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.filters import Condition

# ── Central UI theme ──────────────────────────────────────────────────
# Minimal palette: one accent, muted secondaries, semantic status colors.
# Use these names instead of inline literals so the whole UI restyles here.
LAINTAS_THEME = Theme({
    "accent":   "#7aa2f7",          # primary brand-ish accent (soft blue)
    "accent.dim": "#5a7bbf",
    "success":  "#9ece6a",
    "error":    "bold #f7768e",
    "warning":  "#e0af68",
    "muted":    "#6b7280",
    "agent":    "#bb9af7",          # agent / orchestration (soft violet)
    "path":     "bold #7aa2f7",
    "glyph":    "#7aa2f7",
    "rule":     "#3b4261",
})

console = Console(theme=LAINTAS_THEME)


def _ptk_fragments(pairs):
    """Expand a list of (base_style, text) pairs into prompt_toolkit fragments.

    ``text`` may embed Rich markup (``[bold]``, ``[green]``, ``[dim]`` …).
    prompt_toolkit's ``FormattedTextControl`` does not understand Rich markup —
    left as-is it prints literally (e.g. a raw ``[bold][/bold]``). This resolves
    the markup via Rich (honoring ``LAINTAS_THEME``) and emits proper
    prompt_toolkit ``(style_str, text)`` fragments, folding each ``base_style``
    (e.g. ``"class:selected"``) onto every produced segment.
    """
    from rich.text import Text as _RichText

    out = []
    for base_style, text in pairs:
        base = base_style or ""
        if "[" not in text:
            out.append((base, text))
            continue
        try:
            rt = _RichText.from_markup(text)
        except Exception:
            out.append((base, text))
            continue
        for seg in rt.render(console):
            parts = []
            if base:
                parts.append(base)
            st = seg.style
            if st is not None:
                if st.bold:
                    parts.append("bold")
                if st.dim:
                    parts.append("dim")
                if st.italic:
                    parts.append("italic")
                if st.underline:
                    parts.append("underline")
                if st.reverse:
                    parts.append("reverse")
                if st.color is not None:
                    try:
                        parts.append("#" + st.color.get_truecolor().hex.lstrip("#"))
                    except Exception:
                        pass
                if st.bgcolor is not None:
                    try:
                        parts.append("bg:#" + st.bgcolor.get_truecolor().hex.lstrip("#"))
                    except Exception:
                        pass
            out.append((" ".join(parts), seg.text))
    return out


# ── Reusable selection dialog ──────────────────────────────────────────
# A single component covering single-select, multi-select, inline (non-full-
# screen), full-screen, fuzzy-search, and action-key variants.  Supersedes
# the ~40-line scaffold duplicated across the 7 hand-rolled pickers.


def _fuzzy_match(text: str, pattern: str) -> bool:
    """Return True if all chars of pattern appear in text in order (fuzzy match)."""
    it = iter(text)
    return all(c in it for c in pattern)


def select_dialog(
    items,
    *,
    title: str = "",
    multi: bool = False,
    full_screen: bool = True,
    selected_index: int = 0,
    checked=None,
    search: bool = False,
    hint: str = "",
    action_keys=None,
    enter_action: str = "",
    letter_shortcuts: bool = False,
    refresh_interval: float = 0.05,
):
    """Interactive arrow-key selector — single or multi-select.

    Parameters
    ----------
    items : list[str] | list[tuple[str, str]]
        Options to choose from.  Each entry is either a bare label or a
        ``(label, description)`` tuple; both are rendered with Rich markup.
    title : str
        Bold header line (rendered with Rich markup).
    multi : bool
        ``False`` (default) → single-select: Enter returns the chosen item.
        ``True`` → multi-select: Space toggles a checkbox, Enter returns the
        list of checked items.
    full_screen : bool
        ``True`` (default) → takes over the alternate screen buffer with
        terminal-height-aware pagination.  ``False`` → inline non-full-screen
        renderer (for approval gates that keep body content in scrollback).
    selected_index : int
        Initial highlighted row.
    checked : set[int] | None
        (multi only) Pre-checked row indices.
    search : bool
        Show a fuzzy-filter input box above the list (typed text filters
        items by subsequence match on the label).
    hint : str
        Footer hint line.  Auto-derived from mode when empty.
    action_keys : dict[str, str] | None
        Map of ``key → action_name``.  When a key is pressed the dialog exits
        returning ``(action_name, absolute_index)`` instead of an item.  The
        caller can loop and re-invoke for multi-step pickers (like the resume
        picker's d/x keys).
    enter_action : str
        When non-empty (and ``action_keys`` is also set), Enter returns
        ``(enter_action, idx)`` instead of the raw item.  Useful for pickers
        where Enter triggers an action (e.g. "resume", "toggle") rather than
        returning a value.
    letter_shortcuts : bool
        When True, pressing a letter jumps to the first option whose label
        starts with that letter and confirms (muscle-memory compat for
        y/n/a approval gates).  Ignored in multi mode.
    refresh_interval : float
        Application refresh interval in seconds.

    Returns
    -------
    Single-select → the chosen item (str or tuple) or ``None`` (cancelled).
    Multi-select  → list of checked items, or ``None`` (cancelled).
    action_keys   → ``(action_name, absolute_index)`` or ``(None, -1)``.
    """
    if not items:
        return None

    # ── Normalise items into (label, desc) pairs ──────────────────
    norm: list[tuple[str, str]] = []
    for it in items:
        if isinstance(it, (tuple, list)):
            norm.append((str(it[0]), str(it[1]) if len(it) > 1 else ""))
        else:
            norm.append((str(it), ""))

    sel = [max(0, min(selected_index, len(norm) - 1))]
    chk: set[int] = set(checked) if (multi and checked) else set()
    filter_buf = Buffer() if search else None
    act_keys: dict[str, str] = action_keys or {}

    def _visible():
        """Return (list_of_(orig_idx, label, desc)) after filtering."""
        if not filter_buf:
            return list(enumerate(norm))
        f = filter_buf.text.strip().lower()
        if not f:
            return list(enumerate(norm))
        out = []
        for oi, (lab, _desc) in enumerate(norm):
            plain = re.sub(r"\[/?[^\]]+\]", "", lab).lower()
            if _fuzzy_match(plain, f):
                out.append((oi, lab, _desc))
        return out

    def _clamp_sel():
        vis = _visible()
        if not vis:
            sel[0] = 0
            return vis
        if sel[0] >= vis[-1][0]:
            sel[0] = vis[-1][0]
        if sel[0] < vis[0][0]:
            sel[0] = vis[0][0]
        return vis

    def _build_lines():
        lines = []
        if title:
            lines.append(("bold cyan", f"{title}\n"))

        vis = _clamp_sel()

        # ── Pagination (full-screen only) ──
        start, end = 0, len(vis)
        if full_screen and vis:
            import shutil
            term_h = shutil.get_terminal_size().lines
            # Reserve lines for title, search, header/footer hints
            reserve = (3 if title else 0) + (3 if search else 0) + 3
            list_h = max(4, term_h - reserve)
            if len(vis) > list_h:
                cur_pos = next((i for i, (oi, _, _) in enumerate(vis)
                                if oi == sel[0]), 0)
                half = list_h // 2
                start = max(0, min(cur_pos - half, len(vis) - list_h))
                end = min(start + list_h, len(vis))

        if start > 0:
            lines.append(("dim", f"  ... {start} more above ...\n"))

        for vi in range(start, end):
            oi, lab, desc = vis[vi]
            is_sel = (oi == sel[0])
            prefix = "▶" if is_sel else " "
            if multi:
                mark = "☑" if oi in chk else "☐"
                row_text = f" {prefix} {mark}  {lab}"
            else:
                row_text = f" {prefix} {lab}"
            if desc:
                row_text += f"  [dim]{desc}[/dim]"
            style = "class:selected" if is_sel else ""
            lines.append((style, row_text + "\n"))

        if end < len(vis):
            lines.append(("dim", f"  ... {len(vis) - end} more below ...\n"))

        # ── Footer hint ──
        lines.append(("", "\n"))
        if not hint:
            if multi:
                parts = ["Space toggle", "Enter confirm", "Esc cancel"]
            else:
                parts = ["Enter select", "Esc cancel"]
            if search:
                parts.insert(0, "Type to filter")
            if act_keys:
                parts.insert(0, "  ".join(f"{k}={a}" for k, a in act_keys.items()))
            lines.append(("dim", "  " + "  ".join(parts)))
        else:
            lines.append(("dim", "  " + hint))
        return lines

    # ── Key bindings ──────────────────────────────────────────────
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        vis = _visible()
        if not vis:
            return
        cur_vi = next((i for i, (oi, _, _) in enumerate(vis) if oi == sel[0]), 0)
        if cur_vi > 0:
            sel[0] = vis[cur_vi - 1][0]

    @kb.add("down")
    def _(event):
        vis = _visible()
        if not vis:
            return
        cur_vi = next((i for i, (oi, _, _) in enumerate(vis) if oi == sel[0]), 0)
        if cur_vi < len(vis) - 1:
            sel[0] = vis[cur_vi + 1][0]

    @kb.add("home")
    def _(event):
        vis = _visible()
        if vis:
            sel[0] = vis[0][0]

    @kb.add("end")
    def _(event):
        vis = _visible()
        if vis:
            sel[0] = vis[-1][0]

    if multi:
        @kb.add("space")
        def _(event):
            if sel[0] in chk:
                chk.discard(sel[0])
            else:
                chk.add(sel[0])

    @kb.add("enter")
    def _(event):
        vis = _clamp_sel()
        if not vis:
            if act_keys:
                event.app.exit(result=(None, -1))
            else:
                event.app.exit(result=None)
            return
        if multi:
            checked_items = [items[oi] for oi in sorted(chk)
                             if oi < len(items)]
            event.app.exit(result=checked_items)
        elif enter_action and act_keys:
            event.app.exit(result=(enter_action, sel[0]))
        else:
            event.app.exit(result=items[sel[0]])

    # Action keys (e.g. d=details, x=delete in resume picker)
    for _key, _action in list(act_keys.items()):

        @kb.add(_key)
        def _ak(event, _a=_action):
            vis = _clamp_sel()
            if vis and 0 <= sel[0] < len(items):
                event.app.exit(result=(_a, sel[0]))
            else:
                event.app.exit(result=(_a, -1))

    # Letter shortcuts (y/n/a for approval gates)
    if letter_shortcuts and not multi:
        # Collect the first letter of each label (lowercased).
        _first_letters = set()
        for lab, _desc in norm:
            fl = lab.strip()[:1].lower()
            if fl:
                _first_letters.add(fl)

        for _letter in sorted(_first_letters):

            @kb.add(_letter)
            @kb.add(_letter.upper())
            def _lk(event, _l=_letter):
                for i, (lab, _desc) in enumerate(norm):
                    if lab.strip()[:1].lower() == _l:
                        sel[0] = i
                        event.app.exit(result=items[i])
                        return

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _(event):
        if multi:
            event.app.exit(result=None)
        elif act_keys:
            event.app.exit(result=(None, -1))
        else:
            event.app.exit(result=None)

    # ── Layout ────────────────────────────────────────────────────
    windows = []
    if search:
        windows.append(Window(content=BufferControl(buffer=filter_buf), height=1))
    list_ctrl = FormattedTextControl(lambda: _ptk_fragments(_build_lines()))
    if full_screen:
        windows.append(Window(content=list_ctrl))
    else:
        n = len(norm)
        windows.append(Window(content=list_ctrl, always_hide_cursor=True, height=n))

    layout = Layout(HSplit(windows))
    style = Style.from_dict({
        "selected": "reverse",
        "option": "",
    })

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=full_screen,
        refresh_interval=refresh_interval,
    )
    return app.run()


try:
    from version import __version__
except Exception:
    __version__ = "0.0.0"

# ── Agent Loop (extracted module) ─────────────────────────────────────
from agent_loop import (
    MAX_LOOPS, MAX_TOKENS, MAX_DEBUG_ENTRIES,
    DebugEntry, TerminalInfo, AgentInfo,
    add_debug_log, clear_debug_logs,
    next_debug_loop, get_debug_logs,
    run_agent_loop, LoopDeps,
    register_terminal, unregister_terminal,
    get_terminal, get_all_terminals, close_all_terminals,
    rename_terminal,
    register_agent, unregister_agent,
    get_agent, get_all_agents, get_current_agent,
    get_pool_agents, get_deployed_agents, get_or_hire_pool_agent,
    switch_to_agent, set_current_agent_id,
    rename_agent, station_agent, unstation_agent,
    close_all_agents,
    spawn_subagent, send_to_agent, recv_from_inbox, drain_inbox,
    abort_agent, wait_for_agent, build_agents_tree,
    get_runtime_config, set_runtime_config,
    list_runtime_config, describe_runtime_config,
    reset_runtime_config, apply_max_config,
    prepare_state_for_repl,
    get_user_interrupt_event, get_user_message_queue,
    clear_loop_command_cache,
    stop_trigger_scanner,
    save_session_snapshot, load_session_snapshot,
    save_resume_state, load_resume_state, save_resume_checkpoint, list_resume_states,
    delete_resume_state,
)

import tools as tools_mod    # noqa: E402 — load after agent_loop so registry inits once
import skills as skills_mod  # noqa: E402
import task_manager          # noqa: E402 — resume blob rehydrates the task plan
import paths                 # Centralized path management
import migrate as migrate_mod  # Auto-migration from old layout
import hwo_ui as hwo_ui_mod  # /hwo orchestration UI
import browser_session as browser_mod  # headless-browser live-view stack
import session_store             # durable live current-session state
import event_log                 # prompt admission + interrupted-run recovery
import prompt_lab                # project-scoped prompt diagnosis/testing branches
import evolution_lab             # project-scoped feature/extension evolution
import extension_runtime         # hot-loaded project extension runtime
import workgraph                 # unified objective/plan/steps/workflow state
import hooks as hooks_mod        # trusted Python hooks + argv hooks
import backend_profiles          # backend trust domains + credential isolation
import trust_store               # workspace trust for executable customization
import usage_tracker             # local AI token/cost accounting (/usage)
import mode_manager              # declarative user-selectable agent modes

# MCP client: lazy import (saves ~1.8s on startup)
_mcp_mod = None
def _get_mcp_mod():
    global _mcp_mod
    if _mcp_mod is None:
        import mcp_client as _m
        _mcp_mod = _m
    return _mcp_mod

# ── ANSI escape sequence stripping ─────────────────────────────────────
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-~]|\x1b[()][AB12]|\x0d')
def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences and carriage returns from text."""
    return _ANSI_RE.sub('', text).replace('\r', '')

# ── Configuration ──────────────────────────────────────────────────────
def _detect_backend() -> str:
    """Auto-detect the Helpwo backend URL. Prefers local if running."""
    local = "http://127.0.0.1:2913"
    try:
        r = requests.get(f"{local}/api/agents", timeout=1.5)
        if r.status_code in (200, 401):  # 401 = endpoint exists, just needs auth
            return local
    except requests.RequestException:
        pass
    return "https://laintas.com"

BACKEND_URL = os.environ.get("LAINTAS_BACKEND") or "https://laintas.com"
# Authentication origins are fixed audiences. Backend customization must never
# redirect account cookies, passwords, or OAuth codes to another host.
LAINTAS_BASE = "https://laintas.com"
ACCOUNTS_BASE = "https://accounts.laintas.com"
SESSION_FILE = paths.SESSION_FILE
CONFIG_FILE = paths.CONFIG_FILE
HEARTBEAT_INTERVAL = 30


def get_backend_profile() -> backend_profiles.BackendProfile:
    """Resolve the active backend without ever promoting custom URLs to official."""
    try:
        return backend_profiles.resolve(BACKEND_URL)
    except ValueError:
        # Fail closed to the exact official origin; never guess that a malformed
        # custom URL is entitled to receive the official session.
        return backend_profiles.BackendProfile(
            "safe-fallback", "official", "https://laintas.com")


def get_backend_url() -> str:
    return get_backend_profile().base_url

# ── PTY-based Command Execution ──────────────────────────────────────────
# Commands are executed inside a pseudo-terminal so they get a real TTY:
# colors, progress bars, and interactive programs work correctly.
# Output is streamed to the real terminal AND captured for the agent loop.

def execute_command_pty(command: str, timeout: int = 120) -> dict:
    """Execute a shell command in a PTY. Output streams to terminal AND is captured.

    Returns {stdout, stderr, returncode, success}.
    On Windows falls back to subprocess.run with capture_output.
    """
    if IS_WINDOWS:
        return _execute_windows(command, timeout)

    session = InteractiveSession(command, timeout=timeout, stream_output=True)
    try:
        return session.run_to_completion()
    finally:
        session.close()


def pty_passthrough(command: str, timeout: int = 120) -> dict:
    """Run a command with full terminal passthrough.

    Forks and execs the command so the child directly inherits the real
    terminal — no new PTY is created. This is identical to running the
    command from bash: interactive apps (codex, claude, vim, etc.) get the
    same terminal environment the user sees.

    Returns {stdout, stderr, returncode, success}.
    On Windows falls back to subprocess.run.
    """
    if IS_WINDOWS:
        return _execute_windows(command, timeout)

    fd = sys.stdin.fileno()
    old_tcattr = termios.tcgetattr(fd)
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigquit = signal.getsignal(signal.SIGQUIT)

    pid = os.fork()
    if pid == 0:
        # Child: restore default signal handlers and exec directly into the
        # current terminal (no new PTY — stdin/stdout/stderr are inherited).
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGQUIT, signal.SIG_DFL)
            os.execve(DEFAULT_SHELL, [DEFAULT_SHELL, "-c", command], _child_env())
        except Exception:
            pass
        os._exit(127)

    # Parent: let SIGINT reach the child's process group, just wait.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGQUIT, signal.SIG_IGN)

    returncode = -1
    try:
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            returncode = -os.WTERMSIG(status)
    except ChildProcessError:
        pass
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGQUIT, old_sigquit)
        # Restore terminal settings the child may have left in a dirty state.
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_tcattr)
        except (termios.error, OSError):
            pass

    return {
        "stdout": "",
        "stderr": "",
        "returncode": returncode,
        "success": returncode == 0,
    }




def _marker_poll_exec(session, command: str, timeout: int = 60,
                      strip_ansi_codes: bool = True) -> dict:
    """Serialize a command on a persistent terminal session."""
    lock = getattr(session, "command_lock", None)
    if lock is None:
        return _marker_poll_exec_unlocked(
            session, command, timeout, strip_ansi_codes)
    with lock:
        return _marker_poll_exec_unlocked(
            session, command, timeout, strip_ansi_codes)


def _marker_poll_exec_unlocked(session, command: str, timeout: int = 60,
                               strip_ansi_codes: bool = True) -> dict:
    """Execute a command through a persistent bash session via marker-poll.

    Returns {stdout, stderr, returncode, success}.
    Same mechanism as tools.py _bi_shell_exec marker-poll path.

    When strip_ansi_codes=False, ANSI escape sequences are preserved in stdout
    (needed for commands like `clear` whose output IS escape sequences).
    """
    if session is None or not session.is_alive():
        return {"stdout": "", "stderr": "session not alive", "returncode": -1, "success": False}

    import uuid as _uuid
    import re as _re

    marker_id = _uuid.uuid4().hex[:8]
    start_marker = f"__CMD_BEGIN_{marker_id}__"
    end_marker = f"__CMD_END_{marker_id}__"
    wrapped = f"echo {start_marker}; {command} 2>&1; __laintas_rc=$?; echo {end_marker}:$__laintas_rc"

    try:
        old_len = len(session.raw_output)
    except AttributeError:
        old_len = len(session.full_output)

    session.send_keys(wrapped + "\n")
    poll_start = time.time()
    cmd_output = ""
    returncode = -1
    new_content = ""

    while time.time() - poll_start < timeout:
        time.sleep(0.08)
        session.read_output(timeout=0.1)
        try:
            raw = session.raw_output
        except AttributeError:
            raw = session.full_output
        new_content = raw[old_len:] if old_len > 0 else raw

        end_match = _re.search(rf'{_re.escape(end_marker)}:(\d+)', new_content)
        if end_match:
            returncode = int(end_match.group(1))
            starts = list(_re.finditer(
                rf'{_re.escape(start_marker)}(?=[\r\n]|$)', new_content))
            if starts:
                valid = [m for m in starts if m.end() < end_match.start()]
                chosen = valid[-1] if valid else starts[-1]
                body_start = chosen.end()
                while body_start < len(new_content) and new_content[body_start] in '\r\n':
                    body_start += 1
                cmd_output = new_content[body_start:end_match.start()]
                cmd_output = cmd_output.rstrip('\r\n').strip()
            else:
                parts = new_content.rsplit(start_marker, 1)
                if len(parts) > 1:
                    tail = parts[1].split(end_marker, 1)[0]
                    cmd_output = tail.strip('\r\n').strip()
            break
        if not session.is_alive():
            cmd_output = new_content
            break

    if returncode == -1 and not cmd_output:
        cmd_output = new_content

    if strip_ansi_codes:
        cmd_output = strip_ansi(cmd_output)
    return {
        "stdout": cmd_output,
        "stderr": "",
        "returncode": returncode,
        "success": returncode == 0,
    }


def _quick_pwd(session, timeout: float = 2.0) -> str:
    """Run pwd via marker-poll and return the result."""
    if session is None or not session.is_alive():
        return os.getcwd()
    try:
        result = _marker_poll_exec(session, "pwd", timeout=int(timeout))
        output = result.get("stdout", "").strip()
        return output if output else os.getcwd()
    except Exception:
        return os.getcwd()


def _sync_cwd_from_term0(session) -> None:
    """Sync the parent process CWD to match term0's bash session CWD."""
    if session is None or not session.is_alive():
        return
    try:
        term0_cwd = _quick_pwd(session)
        if term0_cwd and os.path.isdir(term0_cwd) and term0_cwd != os.getcwd():
            os.chdir(term0_cwd)
    except Exception:
        pass


def _ensure_term0_alive() -> None:
    """Health-check term0's bash session. Respawn if dead."""
    term0_info = get_terminal("term0")
    if term0_info is None or term0_info.session is None or not term0_info.session.is_alive():
        try:
            _term0 = InteractiveSession(
                DEFAULT_SHELL, timeout=0, stream_output=False, persistent=True)
            _term0.start()
            time.sleep(0.08)
            if _term0.is_alive():
                _term0.read_output(timeout=0.1)
            register_terminal(_term0, DEFAULT_SHELL, 0, name="term0")
        except Exception:
            pass


# ── Interactive-terminal whitelist ──────────────────────────────────────
# Commands whose first word is in this set need a real PTY (pty_passthrough)
# instead of term0/marker-poll, because they take over the whole screen and
# need raw keystrokes (vim, claude) or block waiting on a live remote tty
# (ssh). Everything else routes through term0 so cd/export/pushd persist
# across commands (term0's bash state IS the parent shell's state).
_DEFAULT_INTERACTIVE_COMMANDS = {
    "vim", "vi", "nano", "pico", "emacs",
    "less", "more", "man",
    "htop", "top",
    "python", "python3", "ipython", "node", "irb", "ruby",
    "mysql", "psql", "sqlite3",
    "ssh", "telnet",
    "tmux", "screen", "mc",
    "claude", "codex",
}

_interactive_commands_cache: Optional[set] = None
_interactive_commands_mtime: float = 0.0


def get_interactive_commands() -> set:
    """Merge built-in defaults with user add/remove overrides.

    Persisted in ~/.laintas/interactive_commands.json, mtime-cached so /bash
    add/remove takes effect immediately without restarting — same pattern as
    policy.py/hooks.py.
    """
    global _interactive_commands_cache, _interactive_commands_mtime
    path = paths.INTERACTIVE_COMMANDS_FILE
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    if _interactive_commands_cache is not None and mtime == _interactive_commands_mtime:
        return _interactive_commands_cache

    added, removed = [], []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            added = data.get("add", [])
            removed = data.get("remove", [])
        except (OSError, json.JSONDecodeError):
            pass

    result = (set(_DEFAULT_INTERACTIVE_COMMANDS) | set(added)) - set(removed)
    _interactive_commands_cache = result
    _interactive_commands_mtime = mtime
    return result


def _modify_interactive_commands(command: str, add: bool) -> None:
    """Persist a /bash add|remove override to ~/.laintas/interactive_commands.json."""
    path = paths.INTERACTIVE_COMMANDS_FILE
    data = {"add": [], "remove": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    data.setdefault("add", [])
    data.setdefault("remove", [])
    if add:
        data["remove"] = [c for c in data["remove"] if c != command]
        if command not in data["add"] and command not in _DEFAULT_INTERACTIVE_COMMANDS:
            data["add"].append(command)
    else:
        data["add"] = [c for c in data["add"] if c != command]
        if command not in data["remove"] and command in _DEFAULT_INTERACTIVE_COMMANDS:
            data["remove"].append(command)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class SubTerminalSession:
    """A sub-terminal session that runs a command in a separate terminal context.

    When inside tmux: creates a new tmux window (terminal N+1) so the command
    has full native terminal passthrough while the parent terminal continues
    running the AI loop.

    When NOT inside tmux: falls back to a background InteractiveSession (PTY).

    Implements the same interface as InteractiveSession so it can be used
    interchangeably in the AI loop (start, send_keys, read_output, is_alive,
    close, full_output, returncode, command).
    """

    def __init__(self, command: str, timeout: int = 120):
        self.command = command
        self.timeout = timeout
        self._use_tmux = "TMUX" in os.environ and not IS_WINDOWS
        self._tmux_window: str = ""
        self._pty: Optional[InteractiveSession] = None
        self._alive: bool = False
        self._output_buf: list[str] = []
        self._start_time: float = 0.0
        # Commands routed through one shell must be atomic. Without this,
        # marker-poll executions from multiple agents can interleave.
        self.command_lock = threading.RLock()

    # ── start (non-blocking) ─────────────────────────────────~~~~~~~~~

    def start(self) -> None:
        """Start the command in a sub-terminal. Non-blocking."""
        if self._alive:
            return
        self._start_time = time.time()

        if self._use_tmux:
            self._tmux_window = f"laintas-{os.getpid()}-{uuid.uuid4().hex[:6]}"
            # -d: don't switch to the new window (terminal 0 stays active)
            try:
                result = subprocess.run(
                    ["tmux", "new-window", "-d", "-n", self._tmux_window,
                     f"{shlex.quote(DEFAULT_SHELL)} -c {shlex.quote(self.command)}"],
                    capture_output=True, text=True, timeout=5,
                )
                self._alive = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._alive = False
            if self._alive:
                return
            else:
                self._tmux_window = ""
                self._use_tmux = False
        if not self._use_tmux:
            self._pty = InteractiveSession(self.command, timeout=self.timeout, stream_output=False)
            self._pty.start()
            self._alive = self._pty.is_alive()

    # ── output ─────────────────────────────────────────────────~~~~~~

    def read_output(self, timeout: float = 0.3) -> str:
        """Read current output. For tmux: captures pane content."""
        if not self._alive:
            return ""
        if self._use_tmux:
            import subprocess as _sp
            try:
                result = _sp.run(
                    ["tmux", "capture-pane", "-p", "-t", self._tmux_window, "-S", "-500"],
                    capture_output=True, text=True, timeout=5,
                )
                new_output = result.stdout or ""
            except Exception:
                new_output = ""
            # Return only what's new since last read
            old_len = sum(len(c) for c in self._output_buf)
            if len(new_output) > old_len:
                delta = new_output[old_len:]
                self._output_buf.append(delta)
                return delta
            return ""
        else:
            if self._pty:
                return self._pty.read_output(timeout=timeout)
            return ""

    # ── stdin ─────────────────────────────────────────────────~~~~~~

    def send_keys(self, text: str) -> None:
        """Send keystrokes to the sub-terminal."""
        if not self._alive:
            return
        decoded = _decode_send_keys(text)
        if self._use_tmux:
            try:
                lines = decoded.replace('\r', '\n').split('\n')
                if lines and lines[-1] == "":
                    lines.pop()
                for line in lines:
                    if line:
                        # Send literal text
                        subprocess.run(
                            ["tmux", "send-keys", "-t", self._tmux_window,
                             "-l", line],
                            capture_output=True, timeout=5,
                        )
                    subprocess.run(
                        ["tmux", "send-keys", "-t", self._tmux_window, "Enter"],
                        capture_output=True, timeout=5,
                    )
            except (OSError, subprocess.SubprocessError):
                self._alive = False
        else:
            if self._pty:
                self._pty.send_keys(text)

    # ── liveness ─────────────────────────────────────────────────~~~

    def is_alive(self) -> bool:
        """Check if the sub-terminal process is still running."""
        if not self._alive:
            return False
        if self._use_tmux:
            import subprocess as _sp
            try:
                result = _sp.run(
                    ["tmux", "list-windows", "-F", "#{window_name}"],
                    capture_output=True, text=True, timeout=5,
                )
                alive = self._tmux_window in result.stdout
                if not alive:
                    self._alive = False
                return alive
            except Exception:
                return False
        else:
            if self._pty:
                alive = self._pty.is_alive()
                if not alive:
                    self._alive = False
                return alive
            return False

    # ── cleanup ─────────────────────────────────────────────────~~~

    def close(self) -> None:
        """Close the sub-terminal."""
        if self._use_tmux and self._tmux_window:
            try:
                subprocess.run(
                    ["tmux", "kill-window", "-t", self._tmux_window],
                    capture_output=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            self._tmux_window = ""
        if self._pty:
            self._pty.close()
            self._pty = None
        self._alive = False

    # ── accumulated state ──────────────────────────────────────────

    @property
    def full_output(self) -> str:
        """Full accumulated output."""
        if self._use_tmux:
            # Re-read full pane to get complete output
            import subprocess as _sp
            try:
                result = _sp.run(
                    ["tmux", "capture-pane", "-p", "-t", self._tmux_window, "-S", "-2000"],
                    capture_output=True, text=True, timeout=5,
                )
                return result.stdout or ""
            except Exception:
                return "".join(self._output_buf)
        else:
            if self._pty:
                return self._pty.full_output
            return "".join(self._output_buf)

    @property
    def raw_output(self) -> str:
        """Full accumulated output including ANSI escape codes."""
        if self._use_tmux:
            import subprocess as _sp
            try:
                result = _sp.run(
                    ["tmux", "capture-pane", "-p", "-t", self._tmux_window, "-S", "-2000"],
                    capture_output=True, text=True, timeout=5,
                )
                return result.stdout or ""
            except Exception:
                return "".join(self._output_buf)
        else:
            if self._pty:
                return self._pty.raw_output
            return "".join(self._output_buf)

    @property
    def master_fd(self) -> int:
        """PTY master file descriptor for raw I/O (non-tmux only)."""
        if self._pty:
            return self._pty.master_fd
        return -1

    @property
    def returncode(self) -> int:
        """Return code. -1 while running."""
        if self._pty:
            return self._pty.returncode
        return -1


def _build_subterminal_cmd(agent_id: Optional[str] = None,
                           agent_name: Optional[str] = None,
                           agent_role: Optional[str] = None,
                           terminal_name: Optional[str] = None,
                           parent_terminal: Optional[str] = None,
                           parent_agent_id: Optional[str] = None) -> str:
    """Build the laintas-cli command string for spawning a sub-terminal.

    All identity is passed via CLI flags so the child process can register
    its agent with the right context.
    """
    parts = [shlex.quote(sys.executable),
             shlex.quote(os.path.abspath(__file__)),
             "--simple-prompt"]
    if agent_id:
        parts += ["--agent-id", shlex.quote(agent_id)]
    if agent_name and agent_name != agent_id:
        parts += ["--agent-name", shlex.quote(agent_name)]
    if agent_role:
        parts += ["--agent-role", shlex.quote(agent_role)]
    if terminal_name:
        parts += ["--terminal-name", shlex.quote(terminal_name)]
    if parent_terminal:
        parts += ["--parent-terminal", shlex.quote(parent_terminal)]
    if parent_agent_id:
        parts += ["--parent-agent-id", shlex.quote(parent_agent_id)]
    return " ".join(parts)


def _build_connected_subterminal_cmd(terminal_name: str,
                                     remote_parent_id: Optional[str] = None,
                                     auto_connect: bool = False) -> str:
    """Command line for a user-facing sub-terminal running a nested CLI.

    Carries the terminal's identity (name + remote parent agent id) so that
    running /connect inside it can hand exactly this terminal to Helpwo.
    auto_connect=True (used by Helpwo's term-new) registers at startup.
    """
    parts = [shlex.quote(sys.executable),
             shlex.quote(os.path.abspath(__file__)),
             "--depth", "1",
             "--terminal-name", shlex.quote(terminal_name),
             "--parent-terminal", "term0"]
    if remote_parent_id:
        parts += ["--remote-parent-id", shlex.quote(remote_parent_id)]
    if auto_connect:
        parts.append("--connect")
    return " ".join(parts)


def connect_terminal_to_helpwo(agent_registry: "AgentRegistry", session: dict,
                               quiet: bool = False, name: str = None) -> bool:
    """CLI side of the two-end handshake with Helpwo.

    Works in BOTH terminals: at depth 0 it links the primary CLI itself
    (Helpwo needs the linked primary before it can create sub-terminals from
    its UI); at depth ≥ 1 it hands this sub-terminal over. `name` optionally
    sets a custom display/terminal name; if already connected under a
    different name, the agent reconnects under the new one.
    Starts heartbeat + message poll so Helpwo can chat / term-new / term-close.
    """
    if (get_backend_profile().sends_laintas_credentials
            and not session.get("userId")):
        if not quiet:
            console.print("[red]/connect requires login. Run /login first.[/red]")
        return False
    is_sub = agent_registry.depth > 0
    meta = agent_registry.terminal_meta if is_sub else None
    current = (meta or {}).get("name") if is_sub else agent_registry.agent_name

    if agent_registry.agent_id:
        if not name or name == current:
            if not quiet:
                console.print(Panel(
                    f"[green]Already connected to Helpwo[/green]\n"
                    f"{'Terminal' if is_sub else 'Primary CLI'}: [bold]{current}[/bold]\n"
                    f"Agent ID: {agent_registry.agent_id}\n\n"
                    f"[dim]/connect <name> reconnects under a custom name; "
                    f"/disconnect withdraws.[/dim]",
                    title="Connected", border_style="green",
                ))
            return True
        # Custom name given while connected — reconnect under the new name.
        agent_registry.unregister()
        agent_registry.agent_id = None
        agent_registry.agent_secret = ""

    if name and is_sub and meta is not None:
        meta["name"] = name
    reg_name = name or ((meta or {}).get("name") if is_sub else None)
    ok = agent_registry.register(session, name=reg_name, quiet=True)
    if not ok:
        if not quiet:
            console.print("[red]Could not reach the Helpwo backend — not connected.[/red]")
        return False
    agent_registry.start_heartbeat()
    agent_registry.start_message_poll(
        agent_registry._state_cb or (lambda: {}),
        agent_registry._chat_cb or (lambda: []),
    )
    if not quiet:
        if is_sub:
            console.print(Panel(
                f"[green]Sub-terminal handed over to Helpwo[/green]\n"
                f"Terminal: [bold]{(meta or {}).get('name', agent_registry.agent_name)}[/bold]\n"
                f"Created by: {(meta or {}).get('createdBy', 'term0')}\n"
                f"Agent ID: {agent_registry.agent_id}\n\n"
                f"[dim]Helpwo can now read this terminal, send it input, and close it.\n"
                f"Run /disconnect to withdraw it.[/dim]",
                title="Connected", border_style="green",
            ))
        else:
            console.print(Panel(
                f"[green]Primary CLI linked to Helpwo[/green]\n"
                f"Name: [bold]{agent_registry.agent_name}[/bold]\n"
                f"Agent ID: {agent_registry.agent_id}\n\n"
                f"[dim]Helpwo can now create sub-terminals here from its UI.\n"
                f"Share an existing one: /term <name>, then /connect inside it.\n"
                f"Run /disconnect to go offline.[/dim]",
                title="Connected", border_style="green",
            ))
    return True


def _drain_fd(fd: int, chunks: list) -> None:
    """Read any remaining data from fd after child exits."""
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                break
            data = os.read(fd, 4096)
            if not data:
                break
            decoded = data.decode("utf-8", errors="replace")
            chunks.append(decoded)
            sys.stdout.write(decoded)
            sys.stdout.flush()
        except OSError:
            break


def _execute_windows(command: str, timeout: int) -> dict:
    """Fallback subprocess execution on Windows."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out", "returncode": -1, "success": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}


def _child_env() -> dict:
    """Build child process environment with TERM and color vars set for PTY."""
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("CLICOLOR", "1")
    env.setdefault("CLICOLOR_FORCE", "1")
    env["FORCE_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text (delegates to strip_ansi)."""
    return strip_ansi(text)


# ── Interactive Session (PTY with stdin support) ─────────────────────────

import codecs


def _decode_send_keys(text: str) -> str:
    """Decode Python-style escape sequences in send_keys text.

    Converts \\n -> newline, \\t -> tab, \\r -> CR, \\x1b -> ESC, etc.
    Preserves non-ASCII characters (codecs.decode with 'unicode_escape'
    corrupts them by round-tripping through Latin-1).
    """
    result: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text):
            c = text[i + 1]
            if c == 'n':
                result.append('\n'); i += 2
            elif c == 't':
                result.append('\t'); i += 2
            elif c == 'r':
                result.append('\r'); i += 2
            elif c == '\\':
                result.append('\\'); i += 2
            elif c == 'x' and i + 3 < len(text):
                try:
                    result.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                except ValueError:
                    result.append(text[i]); i += 1
            elif c == '0':
                result.append('\0'); i += 2
            else:
                result.append(text[i]); i += 1
        else:
            result.append(text[i]); i += 1
    return ''.join(result)


class InteractiveSession:
    """Manages an interactive process running in a pseudo-terminal (PTY).

    Supports starting a process, sending keystrokes (including arrow keys
    and other ANSI escape sequences), reading output, checking liveness,
    and graceful shutdown. Also provides run_to_completion() as a
    drop-in replacement for the old execute_command_pty().
    """

    def __init__(self, command: str, timeout: int = 120, stream_output: bool = False, persistent: bool = False):
        self.command = command
        self.timeout = timeout
        self.stream_output = stream_output
        self.persistent = persistent

        self.pid: int = -1
        self.master_fd: int = -1
        self._output_chunks: list[str] = []
        self._returncode: int = -1
        self._start_time: float = 0.0
        self._old_tcattr = None
        self._started: bool = False
        self._closed: bool = False
        self._eof_reached: bool = False
        self.command_lock = threading.RLock()

    # ── start ─────────────────────────────────────────────────~~~~~~~~~

    def start(self) -> None:
        """Fork + exec the command in a PTY. Idempotent."""
        if self._started:
            return
        self._started = True

        if IS_WINDOWS:
            self._returncode = 0
            self._eof_reached = True
            return

        # Save terminal attrs for restoration
        try:
            self._old_tcattr = termios.tcgetattr(sys.stdin.fileno())
        except (termios.error, OSError):
            self._old_tcattr = None

        master_fd, slave_fd = pty.openpty()

        # Get slave terminal attrs
        try:
            slave_attr = termios.tcgetattr(slave_fd)
        except termios.error:
            slave_attr = None

        pid = os.fork()
        if pid == 0:
            # ── Child ──
            os.close(master_fd)
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            try:
                if slave_attr:
                    termios.tcsetattr(0, termios.TCSANOW, slave_attr)
            except termios.error:
                pass
            try:
                s = termios.tcgetwinsize(sys.stdout.fileno())
                termios.tcsetwinsize(0, s)
            except (termios.error, OSError):
                pass
            if self.persistent:
                os.execve(DEFAULT_SHELL, [DEFAULT_SHELL], _child_env())
            else:
                os.execve(DEFAULT_SHELL, [DEFAULT_SHELL, "-c", self.command], _child_env())
            os._exit(127)

        # ── Parent ──
        os.close(slave_fd)
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self.master_fd = master_fd
        self.pid = pid
        self._start_time = time.time()

    # ── stdin ──────────────────────────────────────────────────────────

    def send_keys(self, text: str) -> None:
        """Send keystrokes to the process. Supports escape sequences.

        Use \\n for Enter, \\t for Tab, \\x1b for ESC,
        \\x1b[A for Up arrow, \\x1b[B for Down, etc.
        """
        if self._closed:
            return
        if not self._started:
            self.start()
        if self.master_fd < 0:
            return
        decoded = _decode_send_keys(text)
        # Terminals send CR (0x0d) when Enter is pressed, not LF (0x0a).
        # In cooked mode the line discipline's ICRNL converts CR→LF for the
        # process; in raw mode (prompt_toolkit, vim, codex, …) the process
        # receives CR directly and expects that as the Enter key.
        decoded = decoded.replace('\n', '\r')
        try:
            os.write(self.master_fd, decoded.encode("utf-8"))
        except OSError:
            pass

    # ── output ─────────────────────────────────────────────────~~~~~~~~

    def read_output(self, timeout: float = 0.15) -> str:
        """Non-blocking read from PTY. Returns newly-read text."""
        if self._closed:
            return ""
        if not self._started:
            self.start()
        if self.master_fd < 0:
            return ""

        new_chunks: list[str] = []
        try:
            r, _, _ = select.select([self.master_fd], [], [], timeout)
        except (select.error, ValueError):
            return ""

        if not r:
            return ""

        try:
            data = os.read(self.master_fd, 4096)
        except OSError as e:
            if e.errno in (errno.EIO,):
                self._eof_reached = True
                self._reap_child()
                return ""
            if e.errno != errno.EAGAIN:
                self._eof_reached = True
                return ""
            return ""

        if data:
            decoded = data.decode("utf-8", errors="replace")
            new_chunks.append(decoded)
            self._output_chunks.append(decoded)
            if self.stream_output:
                sys.stdout.write(decoded)
                sys.stdout.flush()
        else:
            self._eof_reached = True
            self._reap_child()

        self._check_child()
        return "".join(new_chunks)

    # ── lifecycle ─────────────────────────────────────────────────~~~~~

    def _check_child(self) -> None:
        """Non-blocking check if child exited. Updates _returncode."""
        if self._returncode != -1 or self.pid < 0:
            return
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
            if wpid == self.pid:
                if os.WIFEXITED(status):
                    self._returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    self._returncode = 128 + os.WTERMSIG(status)
                self._drain_remaining()
                self._eof_reached = True
        except ChildProcessError:
            self._returncode = 0
            self._eof_reached = True

    def _reap_child(self) -> None:
        """Blocking wait for child process."""
        if self._returncode != -1 or self.pid < 0 or self.master_fd < 0:
            return
        try:
            wpid, status = os.waitpid(self.pid, 0)
            if os.WIFEXITED(status):
                self._returncode = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                self._returncode = 128 + os.WTERMSIG(status)
        except (OSError, ChildProcessError):
            pass

    def _drain_remaining(self) -> None:
        """Read any leftover data from master fd after child exits."""
        if self.master_fd < 0:
            return
        while True:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
                if not r:
                    break
                data = os.read(self.master_fd, 4096)
                if not data:
                    break
                decoded = data.decode("utf-8", errors="replace")
                self._output_chunks.append(decoded)
                if self.stream_output:
                    sys.stdout.write(decoded)
                    sys.stdout.flush()
            except OSError:
                break

    def is_alive(self) -> bool:
        """Check if the process is still running."""
        if self._closed or not self._started or self._eof_reached:
            return False
        self._check_child()
        return self._returncode == -1

    # ── properties ─────────────────────────────────────────────────~~~~

    @property
    def full_output(self) -> str:
        """All accumulated output with ANSI escape codes stripped."""
        return _strip_ansi("".join(self._output_chunks))

    @property
    def raw_output(self) -> str:
        """All accumulated output including ANSI escape codes."""
        return "".join(self._output_chunks)

    @property
    def returncode(self) -> int:
        return self._returncode

    @property
    def success(self) -> bool:
        return self._returncode == 0

    # ── one-shot execution ─────────────────────────────────────────~~~~

    def run_to_completion(self, timeout: int = None) -> dict:
        """Block until the process exits. Returns {stdout, stderr, returncode, success}.

        Same return shape as the old execute_command_pty() for drop-in compatibility.
        """
        if not self._started:
            self.start()

        deadline = self._start_time + (timeout if timeout is not None else self.timeout)

        while not self._eof_reached:
            elapsed = time.time() - self._start_time
            if elapsed > (timeout if timeout is not None else self.timeout):
                try:
                    os.kill(self.pid, signal.SIGTERM)
                    time.sleep(0.3)
                    os.kill(self.pid, signal.SIGKILL)
                except OSError:
                    pass
                self._returncode = -1
                break

            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.15)
            except (select.error, ValueError):
                break

            if r:
                try:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        decoded = data.decode("utf-8", errors="replace")
                        self._output_chunks.append(decoded)
                        if self.stream_output:
                            sys.stdout.write(decoded)
                            sys.stdout.flush()
                    else:
                        self._eof_reached = True
                except OSError as e:
                    if e.errno in (errno.EIO,):
                        self._eof_reached = True
                    elif e.errno != errno.EAGAIN:
                        self._eof_reached = True
            else:
                self._check_child()

        # Ensure child is reaped
        self._reap_child()
        self._drain_remaining()

        # Clean up fd and terminal
        if self.master_fd >= 0:
            os.close(self.master_fd)
            self.master_fd = -1
        try:
            if self._old_tcattr:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self._old_tcattr)
        except (termios.error, OSError):
            pass

        stdout = _strip_ansi("".join(self._output_chunks)).strip()
        return {
            "stdout": stdout,
            "stderr": "",
            "returncode": self._returncode,
            "success": self._returncode == 0,
        }

    # ── cleanup ─────────────────────────────────────────────────~~~~~~~

    def close(self) -> None:
        """Kill the process, reap it, close fd, restore terminal."""
        if self._closed:
            return

        if self.is_alive():
            try:
                os.kill(self.pid, signal.SIGTERM)
                time.sleep(0.3)
                if self.is_alive():
                    os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass

        self._closed = True

        if self.master_fd >= 0:
            self._reap_child()
            self._drain_remaining()
            os.close(self.master_fd)
            self.master_fd = -1

        try:
            if self._old_tcattr:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self._old_tcattr)
        except (termios.error, OSError):
            pass

    def __del__(self):
        self.close()


# ── Collapsed Output Display ─────────────────────────────────────────~~~

def _fmt_elapsed(elapsed: float) -> str:
    if elapsed <= 0:
        return ""
    if elapsed < 1:
        return f"{elapsed * 1000:.0f}ms"
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    m, s = divmod(int(elapsed), 60)
    return f"{m}m{s}s"


def _emit_block(title: str, status_label: str, status_style: str,
                meta: str, preview_lines: list, depth: int,
                line_style: str = "muted") -> None:
    """Render a borderless, minimal output block.

    A status-colored left bar marks the header; preview lines are dimmed and
    indented. No box — keeps the transcript clean and scannable.
    """
    pad = "  " * depth
    bar = "[%s]▍[/%s]" % (status_style, status_style)
    head = f"{pad}{bar} [bold]{title[:80]}[/bold]"
    if status_label:
        head += f"  [{status_style}]{status_label}[/{status_style}]"
    if meta:
        head += f"  [muted]{meta}[/muted]"
    console.print(head)
    inner = pad + "  "
    for ln in preview_lines:
        console.print(f"{inner}[{line_style}]{ln}[/{line_style}]")


def display_command_output(command: str, returncode: int, output: str, depth: int = 0, elapsed: float = 0.0) -> None:
    """Display command output as a compact, borderless block.

    Shows command, exit status, elapsed time and a short preview. Full output
    is stored in agent state and viewable via /debug.
    """
    lines = output.split("\n") if output else []
    line_count = len(lines)
    byte_count = len(output.encode("utf-8", errors="replace"))

    if returncode == 0:
        status_label, status_style = "OK", "success"
    elif returncode == -1:
        status_label, status_style = "RUNNING", "warning"
    else:
        status_label, status_style = f"EXIT {returncode}", "error"

    t = _fmt_elapsed(elapsed)
    status_label = f"{status_label} · {t}" if t else status_label

    preview = output.strip()[:200].split("\n")
    preview = [p for p in preview if p.strip()][:3]
    if line_count == 0 and byte_count == 0:
        meta = "no output"
        preview = []
    else:
        meta = f"{line_count}L {byte_count}B · /debug"

    _emit_block(command, status_label, status_style, meta, preview, depth)


def display_sub_terminal_preview(command: str, output: str, depth: int = 0, alive: bool = True) -> None:
    """Show a compact, borderless preview of sub-terminal output (tail)."""
    clean = strip_ansi(output) if output else ""
    all_lines = [l for l in clean.split("\n") if l.strip()] if clean else []
    total_lines = len(all_lines)

    if total_lines > 6:
        preview = all_lines[-6:]
        meta = f"running · {total_lines}L" if alive else f"exited · {total_lines}L"
    elif all_lines:
        preview = all_lines
        meta = "running" if alive else "exited"
    else:
        preview = []
        meta = "running · no output" if alive else "exited"

    status_label = "RUNNING" if alive else "EXITED"
    status_style = "warning" if alive else "muted"
    _emit_block(command, status_label, status_style, meta, preview, depth)


def display_file_diff(path: str, diff_text: str, depth: int = 0) -> None:
    """Display a compact, borderless unified diff preview (+/- colorized)."""
    diff_lines = diff_text.splitlines() if diff_text else []
    line_count = len(diff_lines)
    preview_limit = 40
    shown = diff_lines[:preview_limit]

    pad = "  " * depth
    console.print(f"{pad}[accent]▍[/accent] [bold]{path[:80]}[/bold]  "
                  f"[accent]DIFF[/accent]  [muted]{line_count}L[/muted]")
    inner = pad + "  "
    for ln in shown:
        if ln.startswith("+") and not ln.startswith("+++"):
            console.print(f"{inner}[success]{ln}[/success]")
        elif ln.startswith("-") and not ln.startswith("---"):
            console.print(f"{inner}[error]{ln}[/error]")
        elif ln.startswith("@@"):
            console.print(f"{inner}[accent.dim]{ln}[/accent.dim]")
        else:
            console.print(f"{inner}[muted]{ln}[/muted]")
    if line_count > preview_limit:
        console.print(f"{inner}[muted]… {line_count - preview_limit} more lines[/muted]")


# ── prompt_toolkit Input Setup ──────────────────────────────────────────

@dataclass(frozen=True)
class CommandSpec:
    """Single source of truth for slash-command discovery and help."""

    name: str
    description: str
    group: str
    usage: str = ""
    aliases: tuple[str, ...] = ()
    palette: bool = True
    subcommands: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("/help", "Show command help", "Basics", "/help [command]"),
    CommandSpec("/clear", "Clear the screen", "Basics"),
    CommandSpec("/cwd", "Show the working directory", "Basics"),
    CommandSpec("/scan", "List user-facing PATH commands", "Basics"),
    CommandSpec("/login", "Re-authenticate with Laintas", "Account & Session"),
    CommandSpec("/usage", "Show AI usage — local token stats + Laintas backend usage", "Account & Session", "/usage [7d|30d|90d|local]", subcommands=("local",)),
    CommandSpec("/resume", "Resume a saved session (picker; echo last N messages, default 20)", "Account & Session", "/resume [N|all|latest]"),
    CommandSpec("/new", "Start a new live session", "Account & Session", "/new"),
    CommandSpec("/exit", "Log out and exit", "Account & Session"),
    CommandSpec("/quit", "Exit without logging out", "Account & Session", aliases=("/q",)),
    CommandSpec("/back", "Detach from a sub-terminal", "Account & Session"),
    CommandSpec("/version", "Show version or update", "Account & Session", "/version [check|update [--force]]", aliases=("/v", "/update"), subcommands=("check", "update")),
    CommandSpec("/name", "Show or set the current agent name", "Agents & Terminals", "/name [new-name]"),
    CommandSpec("/hire", "Create an idle agent", "Agents & Terminals"),
    CommandSpec("/agents", "List, switch, or rename agents", "Agents & Terminals", "/agents [tree|agent-id|name <new-name>]", subcommands=("tree", "name")),
    CommandSpec("/term", "List, create, or rename terminals", "Agents & Terminals", "/term [name|rename <old> <new>]", aliases=("/t",), subcommands=("rename",)),
    CommandSpec("/connect", "Link this terminal to Helpwo (primary or sub-terminal; optional custom name)", "Agents & Terminals", "/connect [name]"),
    CommandSpec("/disconnect", "Withdraw this terminal from Helpwo", "Agents & Terminals"),
    CommandSpec("/station", "Deploy an agent to a terminal", "Agents & Terminals", "/station [agent-id] [terminal]", aliases=("/st",)),
    CommandSpec("/terminate", "Close a terminal", "Agents & Terminals", "/terminate <name>"),
    CommandSpec("/send", "Send input to a terminal", "Agents & Terminals", "/send <name> [--wait <seconds>] <command>"),
    CommandSpec("/spawn", "Spawn a sub-agent", "Agents & Terminals", "/spawn [name:] <task>"),
    CommandSpec("/tell", "Send a message to an agent", "Agents & Terminals", "/tell <agent-id> <message|json>"),
    CommandSpec("/abort", "Abort an agent", "Agents & Terminals", "/abort <agent-id>"),
    CommandSpec("/hwo", "Open or run an orchestration workflow", "Planning & Tasks", "/hwo [file|run <file>|compile <file>]", subcommands=("run", "compile")),
    CommandSpec("/mode", "Show, switch, or create agent modes", "Planning & Tasks", "/mode [act|plan [task]|review|list|create|delete]", subcommands=("act", "plan", "review", "list", "create", "delete")),
    CommandSpec("/plan", "Create, revise, review, or approve versioned plans", "Planning & Tasks", "/plan {enter|submit|revise|approve|exit|status|list}", subcommands=("enter", "submit", "revise", "approve", "exit", "status", "list")),
    CommandSpec("/prompt", "Open Prompt Lab or manage tested prompt overlays", "Planning & Tasks", "/prompt [issue|subcommand]", subcommands=("status", "branches", "open", "chat", "review", "test", "activate", "disable", "patches", "profiles", "profile", "use", "rollback", "feedback", "fail", "optimize", "apply", "discard", "list", "skill", "export", "install", "publish")),
    CommandSpec("/evolve", "Create, improve, test, and hot-load project extensions", "Planning & Tasks", "/evolve [idea|subcommand]", subcommands=("status", "branches", "open", "chat", "review", "test", "activate", "disable", "candidates", "profiles", "profile", "use", "rollback", "list", "help")),
    CommandSpec("/task", "Track project tasks", "Planning & Tasks", "/task [list|add|show|start|done|del|progress|note|subtask]", subcommands=("list", "add", "show", "start", "done", "del", "progress", "note", "subtask")),
    CommandSpec("/work", "Inspect or resume unified WorkGraph state", "Planning & Tasks", "/work [status|list|resume|history]", subcommands=("status", "list", "resume", "history")),
    CommandSpec("/workflow", "Run a multi-phase workflow", "Planning & Tasks", "/workflow {start|status|advance|approve|end|list}", subcommands=("start", "status", "advance", "approve", "end", "list")),
    CommandSpec("/model", "List or select the backend model", "Config & Tools", "/model [id|reset]", subcommands=("reset", "clear", "default")),
    CommandSpec("/config", "View or set runtime configuration", "Config & Tools", "/config [key [value]|reset]", subcommands=("reset",)),
    CommandSpec("/policy", "Show or set security policy", "Config & Tools", "/policy [audit|enforce|disabled [--yes]|reset]", subcommands=("audit", "enforce", "disabled", "reset")),
    CommandSpec("/trust", "Review or change workspace trust", "Config & Tools", "/trust [status|allow|revoke]", subcommands=("status", "allow", "revoke")),
    CommandSpec("/hooks", "Manage executable hooks", "Config & Tools", "/hooks [status|trust|revoke|reload]", subcommands=("status", "trust", "revoke", "reload")),
    CommandSpec("/backend", "Manage backend trust profiles", "Config & Tools", "/backend [status|list|use <name>|config]", subcommands=("status", "list", "use", "config")),
    CommandSpec("/max", "Lift runtime limits for this process", "Config & Tools"),
    CommandSpec("/tools", "List registered tools", "Config & Tools"),
    CommandSpec("/tool", "Invoke a tool directly", "Config & Tools", "/tool <name> [json-params]"),
    CommandSpec("/skill", "Manage skills", "Config & Tools", "/skill [manager|list|trust|revoke|load|unload|reload|new|dir]", subcommands=("manager", "list", "trust", "revoke", "load", "unload", "reload", "new", "dir")),
    CommandSpec("/mcp", "Manage MCP servers", "Config & Tools", "/mcp {list|trust|revoke|connect|disconnect|reload|tools|init|config}", subcommands=("list", "trust", "revoke", "connect", "disconnect", "reload", "tools", "init", "config")),
    CommandSpec("/bash", "Run a command through term0", "Config & Tools", "/bash <command>|list|add <command>|remove <command>", subcommands=("list", "add", "remove")),
    CommandSpec("/memory", "Inspect project and persistent memory", "Config & Tools", "/memory [project|persistent|show <id|name>]", subcommands=("project", "persistent", "global", "show")),
    CommandSpec("/prop", "View .laintas/cli.prop prompt template", "Config & Tools"),
    CommandSpec("/debug", "Browse or export debug entries", "Config & Tools", "/debug [clear|N|N <file> [--raw]]", subcommands=("clear",)),
    CommandSpec("/detail", "Toggle full vs simplified progress rendering", "Config & Tools", "/detail [on|off]", subcommands=("on", "off")),
    CommandSpec("/undo", "Restore a git checkpoint", "History", "/undo [sha]"),
    CommandSpec("/snapshot", "Create a git checkpoint", "History", "/snapshot [label]"),
    CommandSpec("/snapshots", "List git checkpoints", "History"),
    CommandSpec("/continue", "Continue the current live session", "History"),
    CommandSpec("/told", "Show what you last asked the AI", "History",
                "/told [N|all|reply [N]|log [N]]",
                subcommands=("all", "reply", "log")),
    # Keep /reload discoverable, but its existing handler and behavior stay untouched.
    CommandSpec("/reload", "Reload default files and restart", "History"),
)


def _slash_command_names() -> list[str]:
    names = {name for spec in COMMAND_SPECS for name in spec.all_names}
    try:
        names.update(extension_runtime.get_runtime().command_names())
    except Exception:
        pass
    return sorted(names)


def _find_command_spec(name: str) -> Optional[CommandSpec]:
    normalized = name if name.startswith("/") else f"/{name}"
    normalized = normalized.lower()
    return next(
        (spec for spec in COMMAND_SPECS
         if normalized in {item.lower() for item in spec.all_names}),
        None,
    )


class MetaCompleter(Completer):
    """Context-aware completer: /-commands, shell commands from PATH, and paths."""

    META_COMMANDS = _slash_command_names()

    def __init__(self):
        self._path = PathCompleter(expanduser=True)
        self._cmd_completer: WordCompleter | None = None
        self._cmd_words: list[str] = []
        self._cmd_mtime: float = 0.0

    def _refresh_commands(self):
        """Refresh the cached command list if PATH has changed."""
        now = time.time()
        if now - self._cmd_mtime < 5 and self._cmd_completer is not None:
            return
        words = set(_builtins_for_platform())
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            p = Path(path_dir)
            if not p.is_dir():
                continue
            try:
                for entry in p.iterdir():
                    if entry.is_file() and os.access(entry, os.X_OK):
                        words.add(entry.name)
            except (PermissionError, OSError):
                continue
        self._cmd_words = sorted(words)
        self._cmd_completer = WordCompleter(self._cmd_words, ignore_case=True, sentence=True)
        self._cmd_mtime = now

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text:
            return
        # /-command completion — auto-popup while typing (complete_while_typing)
        if text.startswith("/"):
            if any(ch.isspace() for ch in text):
                head, _, tail = text.partition(" ")
                spec = _find_command_spec(head)
                partial = tail.lstrip()
                if spec and " " not in partial:
                    for subcommand in spec.subcommands:
                        if subcommand.startswith(partial.lower()):
                            yield Completion(
                                subcommand, start_position=-len(partial))
                return
            for cmd in self.META_COMMANDS:
                if cmd.startswith(text):
                    _spec = _find_command_spec(cmd)
                    yield Completion(
                        cmd,
                        start_position=-len(text),
                        display_meta=_spec.description if _spec else "",
                    )
            return

        # For non-/-prefixed input, only show completions on explicit Tab —
        # avoids a noisy menu popping up on every keystroke while typing
        # natural-language input.
        if not complete_event.completion_requested:
            return

        # First word — complete from PATH + builtins
        stripped = document.text_before_cursor.lstrip()
        cursor_in_first_word = " " not in stripped
        if cursor_in_first_word:
            self._refresh_commands()
            yield from self._cmd_completer.get_completions(document, complete_event)
            return

        # After a command — path/file completion.
        # PathCompleter uses document.text_before_cursor (the full line) as the
        # path prefix, which breaks for sentence input like "vim laint".  Create
        # a sub-document that only contains the last whitespace-delimited word
        # so that PathCompleter sees a bare path fragment.
        text_before = document.text_before_cursor
        last_space = text_before.rfind(" ")
        if last_space != -1:
            path_text = text_before[last_space + 1:]
            sub_doc = Document(path_text, len(path_text))
        else:
            sub_doc = document
        yield from self._path.get_completions(sub_doc, complete_event)


def _build_prompt_style() -> Style:
    """Build prompt_toolkit Style for the prompt, completion menu, and status bar."""
    return Style.from_dict({
        "prompt-path": "bold #7aa2f7",
        "separator": "#bb9af7",
        "paste-placeholder": "bold #7aa2f7 bg:#2a2a37",
        # Completion menu (Tokyo Night palette)
        "completion-menu": "bg:#1a1b26",
        "completion-menu.completion": "bg:#1a1b26 #c0caf5",
        "completion-menu.completion.current": "bg:#364a82 #1a1b26 bold",
        "completion-menu.meta.completion": "bg:#16161e #565f89",
        "completion-menu.meta.completion.current": "bg:#16161e #7aa2f7",
        # Bottom status bar
        "bottom-toolbar": "bg:#16161e #9aa5ce",
        "stbar-sep": "bg:#16161e #2a2a37",
        "stbar-model": "bg:#16161e #7aa2f7 bold",
        "stbar-mode-act": "bg:#16161e #9ece6a bold",
        "stbar-mode-plan": "bg:#16161e #e0af68 bold",
        "stbar-tokens": "bg:#16161e #bb9af7",
        "stbar-time": "bg:#16161e #565f89",
        "stbar-dot-act": "bg:#16161e #9ece6a bold",
        "stbar-dot-plan": "bg:#16161e #e0af68 bold",
        # rprompt (right side of prompt line — no background)
        "rprompt-mode-act": "#9ece6a bold",
        "rprompt-mode-plan": "#e0af68 bold",
        "rprompt-sep": "#2a2a37",
        "rprompt-model": "#7aa2f7",
    })


# ── Paste summarization ───────────────────────────────────────────────
# Large pastes (many lines or a long single line) are collapsed into a
# compact placeholder shown in the prompt buffer; the real content is kept
# off-screen and substituted back when the line is submitted. Mirrors
# opencode's pasteText/pasteInputText, adapted to prompt_toolkit.
_paste_registry: dict = {}
_paste_counter = 0


def _reset_paste_registry() -> None:
    """Clear pending placeholder→content mappings (called after each submit)."""
    global _paste_registry, _paste_counter
    _paste_registry = {}
    _paste_counter = 0


def _maybe_summarize_paste(data: str):
    """Decide whether a pasted blob should be collapsed into a placeholder.

    Returns (placeholder, line_count). When the paste is small or the feature
    is disabled, placeholder is None and the caller should insert data as-is.
    On summarization the placeholder→real-content mapping is registered.
    """
    global _paste_counter
    if data is None:
        return None, 0
    normalized = data.replace("\r\n", "\n").replace("\r", "\n")
    line_count = normalized.count("\n") + 1 if normalized else 0
    try:
        enabled = bool(get_runtime_config("paste_summary"))
    except Exception:
        enabled = True
    if not enabled:
        return None, line_count
    try:
        min_lines = int(get_runtime_config("paste_summary_min_lines"))
    except Exception:
        min_lines = 3
    try:
        min_chars = int(get_runtime_config("paste_summary_min_chars"))
    except Exception:
        min_chars = 150
    if line_count >= min_lines or len(normalized) >= min_chars:
        _paste_counter += 1
        placeholder = f"[Pasted #{_paste_counter} ~{line_count} lines]"
        _paste_registry[placeholder] = normalized
        return placeholder, line_count
    return None, line_count


def _expand_pastes(text: str) -> str:
    """Replace any registered placeholders in text with their real content."""
    if not text or not _paste_registry:
        return text
    for placeholder, real in _paste_registry.items():
        if placeholder in text:
            text = text.replace(placeholder, real)
    return text


_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted #\d+ ~\d+ lines\]")


def _paste_span_at(text: str, pos: int):
    """Return (start, end) of a placeholder covering/adjacent to pos, else None.

    Used for whole-segment deletion: Backspace treats pos as the cursor
    position (checks the span ending at pos), Delete checks the span
    starting at pos.
    """
    if not text:
        return None
    for m in _PASTE_PLACEHOLDER_RE.finditer(text):
        if m.start() <= pos <= m.end():
            return m.start(), m.end()
    return None


class _PastePlaceholderLexer(Lexer):
    """Colorize [Pasted #N ~L lines] placeholders in the input buffer."""

    def lex_document(self, document):
        placeholder_style = "class:paste-placeholder"

        def get_line(lineno):
            line = document.lines[lineno]
            if not line:
                return []
            spans = list(_PASTE_PLACEHOLDER_RE.finditer(line))
            if not spans:
                return [("", line)]
            fragments = []
            idx = 0
            for m in spans:
                if m.start() > idx:
                    fragments.append(("", line[idx:m.start()]))
                fragments.append((placeholder_style, line[m.start():m.end()]))
                idx = m.end()
            if idx < len(line):
                fragments.append(("", line[idx:]))
            return fragments

        return get_line


class _PasteGuardBuffer(Buffer):
    """Buffer whose cursor cannot enter a paste-placeholder span.

    [Pasted #N ~L lines] tokens are atomic and non-editable: arrow keys and
    mouse clicks skip over the whole token instead of landing inside it.

    The cursor_position setter is the single chokepoint — Buffer.cursor_left/
    right/up/down all go through ``self.cursor_position += ...``, and
    BufferControl's mouse handler sets ``buffer.cursor_position`` directly,
    so overriding the setter covers both keyboard and mouse in one place.
    """

    @property
    def cursor_position(self) -> int:
        return Buffer.cursor_position.fget(self)

    @cursor_position.setter
    def cursor_position(self, value: int) -> None:
        span = _paste_span_at(self.text, value)
        if span is not None and span[0] < value < span[1]:
            current = Buffer.cursor_position.fget(self)
            value = span[1] if value >= current else span[0]
        Buffer.cursor_position.fset(self, value)


def _build_keybindings() -> KeyBindings:
    """Build custom keybindings for the prompt."""
    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste)
    def _(event):
        """Collapse large pastes into a compact placeholder (expanded on submit)."""
        placeholder, _lc = _maybe_summarize_paste(event.data)
        if placeholder is not None:
            event.current_buffer.insert_text(placeholder)
        else:
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            event.current_buffer.insert_text(data)

    @kb.add("backspace")
    def _(event):
        """Backspace: delete a paste placeholder as one unit, else normal."""
        buf = event.current_buffer
        span = _paste_span_at(buf.text, buf.cursor_position)
        if span is not None and span[1] == buf.cursor_position and span[0] != span[1]:
            start, end = span
            buf.text = buf.text[:start] + buf.text[end:]
            buf.cursor_position = start
        else:
            buf.delete_before_cursor(count=event.arg)

    @kb.add("delete")
    def _(event):
        """Delete: remove a paste placeholder as one unit, else normal."""
        buf = event.current_buffer
        span = _paste_span_at(buf.text, buf.cursor_position)
        if span is not None and span[0] == buf.cursor_position and span[0] != span[1]:
            start, end = span
            buf.text = buf.text[:start] + buf.text[end:]
            buf.cursor_position = start
        else:
            buf.delete(count=event.arg)

    @kb.add("c-d")
    def _(event):
        """Ctrl+D exits if input is empty, otherwise deletes forward."""
        if not event.current_buffer.text:
            event.app.exit(result="/exit")

    @kb.add("tab")
    def _(event):
        """Tab: accept suggestion → cycle completions → start completion."""
        buf = event.current_buffer
        if buf.suggestion and buf.document.is_cursor_at_the_end and not buf.complete_state:
            buf.insert_text(buf.suggestion.text)
        elif buf.complete_state:
            buf.complete_next()
        else:
            buf.start_completion()

    @kb.add("c-f")
    def _(event):
        """Ctrl+F: accept auto-suggest ghost text (fish-style)."""
        buf = event.current_buffer
        if buf.suggestion:
            buf.insert_text(buf.suggestion.text)

    @kb.add("c-e")
    def _(event):
        """Ctrl+E: accept auto-suggest ghost text."""
        buf = event.current_buffer
        if buf.suggestion:
            buf.insert_text(buf.suggestion.text)

    return kb


# ── Status bar (bottom toolbar) ───────────────────────────────────────
# Lightweight session-status cache read by the bottom_toolbar callable.
# Updated at key moments (startup, /model, after backend call, after agent
# loop) so the toolbar callback itself never touches disk — only reads this
# in-memory dict + usage_tracker._SESSION (also in-memory).
_status_cache: dict = {
    "model": "",
    "last_thinking_time": 0.0,
}


def _update_status_cache(**kwargs) -> None:
    """Patch one or more fields in the module-level status cache."""
    _status_cache.update(kwargs)


def _fmt_tokens(n: int) -> str:
    """Compact token count: 856 → '856', 12345 → '12.3k'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _session_token_totals() -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) for this process session."""
    try:
        import usage_tracker as _ut
        with _ut._LOCK:
            total_in = sum(r.get("in", 0) for r in _ut._SESSION)
            total_out = sum(r.get("out", 0) for r in _ut._SESSION)
        return total_in, total_out
    except Exception:
        return 0, 0


def _render_rprompt():
    """Formatted text for the right side of the prompt line (mode + model)."""
    try:
        import plan_mode as _pm
        _is_plan = _pm.is_plan_mode()
    except Exception:
        _is_plan = False
    _mode_label = (
        "PLAN" if _is_plan else mode_manager.get_active_mode()["name"].upper()
    )
    _mode_cls = "rprompt-mode-plan" if _is_plan else "rprompt-mode-act"
    _model = _status_cache.get("model", "") or "default"
    return [
        ("class:" + _mode_cls, _mode_label),
        ("class:rprompt-sep", " · "),
        ("class:rprompt-model", _model),
    ]


def _render_bottom_toolbar():
    """prompt_toolkit bottom_toolbar callable — single-line status bar.

    Invoked on every keystroke; must be fast (all reads are in-memory).
    Shows: last thinking time | session tokens.
    Mode and model are displayed in the rprompt (right side of prompt line).
    """
    _tin, _tout = _session_token_totals()
    _think = _status_cache.get("last_thinking_time", 0.0)
    _think_str = _fmt_elapsed(_think) if _think > 0 else "—"

    sep = ("class:stbar-sep", "  │  ")
    return [
        ("class:stbar-time", f"last {_think_str}"),
        sep,
        ("class:stbar-tokens", f"↑ {_fmt_tokens(_tin)}  ↓ {_fmt_tokens(_tout)}"),
    ]


_prompt_session: Optional[PromptSession] = None


def _interrupt_prompt():
    """Force prompt_toolkit to return from another thread.

    Called by the poll thread when a remote message arrives while the main
    loop is blocked on pt_prompt(). Forces the prompt to return with an empty
    string so the main loop re-checks the injection queue immediately.
    """
    global _prompt_session
    if _prompt_session is not None:
        try:
            app = _prompt_session.app
            if app.is_running:
                app.exit(result="")
        except Exception:
            pass


def get_prompt_session() -> PromptSession:
    """Get or create the persistent prompt_toolkit session."""
    global _prompt_session
    if _prompt_session is None:
        hist_file = paths.HISTORY_FILE
        _prompt_session = PromptSession(
            history=FileHistory(str(hist_file)),
            completer=MetaCompleter(),
            auto_suggest=AutoSuggestFromHistory(),
            style=_build_prompt_style(),
            key_bindings=_build_keybindings(),
            lexer=_PastePlaceholderLexer(),
            enable_history_search=False,
            vi_mode=False,
            complete_while_typing=True,
            mouse_support=Condition(lambda: bool(get_runtime_config("enable_mouse"))),
        )
        # Upgrade the default buffer so the cursor snaps out of paste
        # placeholders instead of entering them (arrow keys + mouse clicks).
        try:
            _prompt_session.default_buffer.__class__ = _PasteGuardBuffer
        except TypeError:
            pass
    return _prompt_session


def pt_prompt(cwd: str) -> str:
    """Read user input with prompt_toolkit (PTY-based terminal input)."""
    session = get_prompt_session()
    disp = _shorten_path(cwd, max_len=60)
    try:
        user_input = session.prompt(
            [("class:prompt-path", disp),
             ("", "\n"),
             ("class:separator", "❯ ")],
            style=_build_prompt_style(),
            multiline=False,
            rprompt=_render_rprompt(),
            complete_while_typing=True,
        )
        expanded = _expand_pastes(user_input) if user_input else user_input
        _reset_paste_registry()
        return expanded.strip() if expanded else ""
    except (KeyboardInterrupt, EOFError):
        _reset_paste_registry()
        return ""
    except Exception:
        _reset_paste_registry()
        return ""


# ── Dynamic Command Discovery ──────────────────────────────────────────
# Routing decision (system command vs natural language) is made at runtime
# via shutil.which() plus a fixed set of shell builtins. No persisted
# snapshot — newly-installed binaries are picked up immediately.

import re
import shutil

# bash/sh/zsh builtins that aren't on PATH but should still route as commands.
_POSIX_SHELL_BUILTINS = {
    "alias", "bg", "break", "builtin", "case", "cd", "command", "compgen",
    "complete", "continue", "declare", "dirs", "disown", "echo", "enable",
    "eval", "exec", "exit", "export", "false", "fg", "for", "function",
    "getopts", "hash", "help", "history", "if", "jobs", "kill", "let",
    "local", "logout", "popd", "printf", "pushd", "pwd", "read", "readonly",
    "return", "select", "set", "shift", "shopt", "source", "suspend", "test",
    "time", "times", "trap", "true", "type", "typeset", "ulimit", "umask",
    "unalias", "unset", "until", "wait", "while", ".", ":",
}


def _builtins_for_platform() -> set:
    return _WINDOWS_CMD_BUILTINS if IS_WINDOWS else _POSIX_SHELL_BUILTINS


def extract_first_word(user_input: str) -> str:
    """Extract the first shell word from user input."""
    m = re.match(r'^\s*(\S+)', user_input)
    if not m:
        return ""
    return m.group(1).strip("'\"`;&|")


def is_system_command(user_input: str) -> bool:
    """True if the first word is a shell builtin or resolvable on PATH."""
    first = extract_first_word(user_input)
    if not first:
        return False
    if first in _builtins_for_platform():
        return True
    return shutil.which(first) is not None


def list_path_commands() -> list:
    """Enumerate user-facing commands currently on PATH (for /scan display)."""
    commands = set()
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    if IS_WINDOWS:
        pathext = [ext.upper() for ext in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT;.COM").split(";") if ext]
        for path_dir in path_dirs:
            p = Path(path_dir)
            if not p.is_dir():
                continue
            try:
                for entry in p.iterdir():
                    if entry.is_file() and entry.suffix.upper() in pathext:
                        commands.add(entry.stem)
            except (PermissionError, OSError):
                pass
        commands.update(_WINDOWS_CMD_BUILTINS)
    else:
        for path_dir in path_dirs:
            p = Path(path_dir)
            if not p.is_dir():
                continue
            try:
                for entry in p.iterdir():
                    if entry.is_file() and os.access(entry, os.X_OK):
                        commands.add(entry.name)
            except (PermissionError, OSError):
                pass

    result = []
    for c in sorted(commands):
        if len(c) < 2 or c[0].isupper():
            continue
        if "." in c or ":" in c or c.startswith("_"):
            continue
        if c.startswith("systemd-") or c.startswith("dbus-") or c.startswith("ksvgtop"):
            continue
        result.append(c)
    return result


# ── Shell Detection ──────────────────────────────────────────────────────

if IS_WINDOWS:
    DEFAULT_SHELL = os.environ.get("COMSPEC", "cmd.exe")
    SHELL_NAME = "cmd"
else:
    DEFAULT_SHELL = os.environ.get("SHELL", "/bin/bash")
    SHELL_NAME = "bash" if "bash" in DEFAULT_SHELL else ("zsh" if "zsh" in DEFAULT_SHELL else "sh")

# ── Session Management ─────────────────────────────────────────────────

def load_session() -> Optional[dict]:
    """Load saved session token from ~/.laintas/session.json."""
    if SESSION_FILE.exists():
        if not paths.ensure_private_file(SESSION_FILE):
            return None
        try:
            return json.loads(SESSION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_session(session: dict) -> None:
    """Save session token to ~/.laintas/session.json."""
    _atomic_private_json(SESSION_FILE, session)


def clear_session() -> None:
    """Remove saved session."""
    SESSION_FILE.unlink(missing_ok=True)


def load_config() -> dict:
    """Load CLI config (agent name, backend url, etc.)."""
    if CONFIG_FILE.exists():
        if not paths.ensure_private_file(CONFIG_FILE):
            return {}
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict) -> None:
    """Save CLI config."""
    _atomic_private_json(CONFIG_FILE, config)


def _atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def get_selected_model() -> str:
    """Return the configured model name, if any."""
    val = load_config().get("model", "")
    return str(val).strip() if val else ""


def set_selected_model(model: str) -> None:
    """Persist the selected backend model name."""
    config = load_config()
    model = model.strip()
    if model:
        config["model"] = model
    else:
        config.pop("model", None)
    save_config(config)


def get_selected_provider() -> str:
    """Return the configured provider id, if any."""
    val = load_config().get("provider", "")
    return str(val).strip() if val else ""


def set_selected_provider(provider: str) -> None:
    """Persist the selected provider id."""
    config = load_config()
    provider = provider.strip()
    if provider:
        config["provider"] = provider
    else:
        config.pop("provider", None)
    save_config(config)


def _normalize_model_entry(item) -> dict:
    """Normalize common model-list response shapes into displayable rows."""
    if isinstance(item, str):
        return {"id": item, "name": item, "description": ""}
    if not isinstance(item, dict):
        return {"id": str(item), "name": str(item), "description": ""}
    model_id = (
        item.get("id") or item.get("model") or item.get("name") or
        item.get("value") or item.get("slug") or ""
    )
    name = item.get("name") or item.get("displayName") or item.get("label") or model_id
    desc = item.get("description") or item.get("desc") or item.get("provider") or ""
    return {"id": str(model_id), "name": str(name), "description": str(desc)}


def _extract_model_entries(data) -> list[dict]:
    """Extract model rows from backend model-list response shapes."""
    if isinstance(data, dict) and isinstance(data.get("providers"), list):
        rows = []
        for provider in data["providers"]:
            if not isinstance(provider, dict):
                continue
            provider_id = str(provider.get("id") or "")
            provider_label = str(provider.get("label") or provider_id)
            for model in provider.get("models") or []:
                row = _normalize_model_entry(model)
                if row.get("id"):
                    row["provider"] = provider_id
                    row["description"] = row.get("description") or provider_label
                    rows.append(row)
        return rows

    raw_models = data
    if isinstance(data, dict):
        raw_models = (
            data.get("models") or data.get("data") or data.get("items") or
            data.get("result") or []
        )
    if isinstance(raw_models, dict):
        raw_models = list(raw_models.values())
    if not isinstance(raw_models, list):
        return []
    return [_normalize_model_entry(item) for item in raw_models]


def fetch_available_models(session: dict) -> tuple[list[dict], str]:
    """Fetch available models from the backend.

    Returns (models, endpoint). Tries the current endpoint first and keeps a
    few fallbacks for older backend builds.
    """
    backend_profile = get_backend_profile()
    backend_url = backend_profile.base_url
    headers, cookies = backend_profiles.request_auth(backend_profile, session)
    endpoints = (
        "/api/models",
        "/api/chat/models",
        "/api/ai/models",
        "/api/model",
    )

    last_error = ""
    for endpoint in endpoints:
        try:
            resp = requests.get(
                f"{backend_url}{endpoint}",
                headers=headers,
                cookies=cookies,
                timeout=20,
                allow_redirects=False,
            )
        except requests.RequestException as e:
            last_error = str(e)
            continue

        if resp.status_code == 404:
            last_error = f"HTTP 404 from {endpoint}"
            continue
        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            continue

        try:
            data = resp.json()
        except ValueError:
            last_error = f"Non-JSON response from {endpoint}: {resp.text[:200]}"
            continue

        models = _extract_model_entries(data)
        if not models and not (
            isinstance(data, dict) and (
                "models" in data or "data" in data or "items" in data or
                "result" in data or "providers" in data
            )
        ):
            last_error = f"Unexpected response shape from {endpoint}"
            continue
        models = [m for m in models if m.get("id")]
        return models, endpoint

    raise RuntimeError(last_error or "No model endpoint responded")


def show_model_selector(models: list[dict], current: str = "") -> Optional[str]:
    """Interactive model selector. Returns selected model id or None."""
    if not models:
        return None
    labels = []
    sel_idx = 0
    for i, model in enumerate(models):
        model_id = model.get("id", "")
        provider = model.get("description") or model.get("provider") or ""
        mark = " *" if current and model_id == current else "  "
        labels.append(f"{mark}[cyan]{model_id:30}[/cyan] {provider}")
        if current and model_id == current:
            sel_idx = i
    chosen = select_dialog(
        labels,
        title="Models — choose with ↑↓ and Enter",
        full_screen=True,
        selected_index=sel_idx,
        hint="↑↓ navigate  ↵ select  Esc/q cancel",
    )
    if chosen is None:
        return None
    return models[labels.index(chosen)].get("id")


# ── Authentication ──────────────────────────────────────────────────────

def verify_session(session: dict) -> Optional[dict]:
    """Verify a saved session token with laintas.com. Returns {id, name, email} or None."""
    cookies = session.get("cookies", {})
    headers = session.get("headers", {})
    token = session.get("token", "")
    # Call get-session to get full user info.
    req_args = None
    # Current SSO cookie first, then legacy names for a smooth migration.
    for cookie_name in [
        "__Secure-laintas-v2.session_token",
        "laintas-v2.session_token",
        "__Secure-better-auth.session_token",
        "better-auth.session_token",
    ]:
        tok = cookies.get(cookie_name, "")
        if tok:
            req_args = {"cookies": {cookie_name: tok}}
            break
    if not req_args and headers.get("Authorization"):
        req_args = {"headers": headers}

    if req_args:
        try:
            resp = requests.get(f"{LAINTAS_BASE}/api/auth/get-session",
                                timeout=5, allow_redirects=False, **req_args)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, dict):
                    user = data.get("user") or {}
                    uid = user.get("id")
                    if uid:
                        return {
                            "id": uid,
                            "name": user.get("name", ""),
                            "email": user.get("email", ""),
                        }
        except requests.RequestException:
            pass

    return None


def resolve_session_from_token(token: str, resp_cookies=None) -> Optional[dict]:
    """Turn a raw token (and optional response cookies) into a verified session.

    Better Auth's get-session needs a *signed* cookie value or a Bearer token.
    The JSON `token` from sign-in / cli-exchange is unsigned, so a single guess
    at the cookie name is fragile. Try, in order:
      1. the signed cookie returned in the HTTP response (most reliable),
      2. the token as __Secure- / non-prefixed session cookie,
      3. the token as an Authorization: Bearer header.
    Returns a verified session dict (with userId/userName/userEmail) or None.
    """
    token = (token or "").strip()
    if not token:
        return None

    candidates = []
    # 1. Signed cookie straight from the response, if the server set one.
    if resp_cookies:
        for cookie_name in (
            "__Secure-laintas-v2.session_token",
            "laintas-v2.session_token",
            "__Secure-better-auth.session_token",
            "better-auth.session_token",
        ):
            signed = resp_cookies.get(cookie_name, "")
            if signed:
                candidates.append({"cookies": {cookie_name: signed}, "headers": {}})
    # 2. The raw token as each cookie name.
    candidates.append({"cookies": {"__Secure-laintas-v2.session_token": token}, "headers": {}})
    candidates.append({"cookies": {"laintas-v2.session_token": token}, "headers": {}})
    candidates.append({"cookies": {"__Secure-better-auth.session_token": token}, "headers": {}})
    candidates.append({"cookies": {"better-auth.session_token": token}, "headers": {}})
    # 3. The raw token as a Bearer header.
    candidates.append({"cookies": {}, "headers": {"Authorization": f"Bearer {token}"}})

    for candidate in candidates:
        candidate["token"] = token
        user_info = verify_session(candidate)
        if user_info:
            candidate["userId"] = user_info["id"]
            candidate["userName"] = user_info.get("name", "")
            candidate["userEmail"] = user_info.get("email", "")
            return candidate

    return None


def solve_captcha_challenge() -> Optional[str]:
    """Return an ALTCHA-compatible captcha response for local CLI login."""
    try:
        resp = requests.get(
            f"{ACCOUNTS_BASE}/api/captcha-challenge", timeout=10,
            allow_redirects=False)
        if resp.status_code != 200:
            return None
        challenge = resp.json()
    except (requests.RequestException, ValueError):
        return None

    algorithm = str(challenge.get("algorithm", "")).upper()
    target = str(challenge.get("challenge", ""))
    salt = str(challenge.get("salt", ""))
    try:
        maxnumber = int(challenge.get("maxnumber", 0))
    except (TypeError, ValueError):
        return None

    if algorithm != "SHA-256" or not target or not salt or maxnumber < 0:
        return None

    started = time.time()
    for number in range(maxnumber + 1):
        digest = hashlib.sha256(f"{salt}{number}".encode("utf-8")).hexdigest()
        if digest == target:
            payload = {
                "algorithm": challenge.get("algorithm", "SHA-256"),
                "challenge": target,
                "number": number,
                "salt": salt,
                "signature": challenge.get("signature", ""),
                "took": int((time.time() - started) * 1000),
            }
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            return base64.b64encode(raw).decode("ascii")

    return None


def build_login_payload(username: str, password: str, captcha_response: str) -> dict:
    """Build a login payload compatible with current and older captcha checks."""
    return {
        "username": username.strip(),
        "password": password,
        # The backend has used both names across versions. Keep both so old
        # Windows builds and the current web auth middleware can agree.
        "captchaResponse": captcha_response,
        "captcha": captcha_response,
    }


def login_interactive() -> Optional[dict]:
    """Interactive login flow — username+password via Better Auth. Returns session dict or None."""
    console.print(Panel(
        "[bold]Login to Laintas[/bold]\n\n"
        "Enter your laintas.com username and password.\n"
        f"Don't have an account? Visit [link={ACCOUNTS_BASE}/register]{ACCOUNTS_BASE}/register[/link] to sign up.",
        title="Laintas Auth"
    ))

    # ── Method 1: Username + Password ──
    username = input("Username: ").strip()
    if not username.strip():
        return None

    import getpass
    password = getpass.getpass("Password: ")
    if not password.strip():
        return None

    try:
        captcha_response = solve_captcha_challenge()
        if not captcha_response:
            console.print("[red]Login failed: could not fetch or solve captcha challenge.[/red]")
            console.print("[dim]Try remote login [1], or paste a browser session token below.[/dim]")
            raise RuntimeError("captcha unavailable")

        headers = {"Content-Type": "application/json", "Origin": f"{ACCOUNTS_BASE}"}
        headers["X-Captcha-Response"] = captcha_response

        resp = requests.post(
            f"{ACCOUNTS_BASE}/api/auth/sign-in/username",
            json=build_login_payload(username, password, captcha_response),
            headers=headers,
            timeout=10,
            allow_redirects=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token", "")
            user = data.get("user", {})
            user_id = user.get("id", "")
            if token:
                # Get the actual signed cookie from response (token in JSON is unsigned)
                cookie_val = (resp.cookies.get("__Secure-laintas-v2.session_token", "")
                              or resp.cookies.get("laintas-v2.session_token", "")
                              or resp.cookies.get("__Secure-better-auth.session_token", ""))
                session = {
                    "token": token,
                    "userId": user_id,
                    "userName": user.get("name", ""),
                    "userEmail": user.get("email", ""),
                    "cookies": {"__Secure-laintas-v2.session_token": cookie_val} if cookie_val else {"laintas-v2.session_token": token},
                    "headers": {},
                }
                save_session(session)
                console.print(f"[green]Logged in as {username.strip()} ({user.get('email', '')})[/green]")
                return session
        else:
            try:
                err = resp.json()
                msg = err.get("message", "") or err.get("error", "") or resp.text[:200]
            except Exception:
                msg = resp.text[:200]
            console.print(f"[red]Login failed: {msg}[/red]")
    except RuntimeError:
        pass
    except requests.RequestException as e:
        console.print(f"[yellow]Cannot reach {ACCOUNTS_BASE}: {e}[/yellow]")

    # ── Method 2: Paste session token (fallback) ──
    console.print("\n[dim]Or paste a session token from your browser cookies.[/dim]")
    token = input("Session token (or press Enter to cancel): ").strip()
    if not token.strip():
        return None

    session = resolve_session_from_token(token)
    if session:
        save_session(session)
        display = (session.get("userEmail") or session.get("userName")
                   or session.get("userId"))
        console.print(f"[green]Logged in as {display}[/green]")
        return session

    console.print("[red]Invalid session token.[/red]")
    return None


def login_via_browser() -> Optional[dict]:
    """Authenticate through accounts.laintas.com using OAuth Code + PKCE."""
    # Find a free port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    callback_url = f"http://127.0.0.1:{port}/callback"
    result = {"code": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/callback":
                self._respond(404, "<h1>Not Found</h1>")
                return
            if params.get("state", [None])[0] != state:
                result["error"] = "OAuth state validation failed"
                self._respond(400, "<h1>Login Failed</h1><p>Invalid OAuth state.</p>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            oauth_error = params.get("error", [None])[0]
            if oauth_error:
                result["error"] = f"Authorization failed: {oauth_error}"
                self._respond(403, "<h1>Login Cancelled</h1><p>Authorization was not completed.</p>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            code = params.get("code", [None])[0]
            if code:
                result["code"] = code
                self._respond(200, "<h1>Login Successful</h1><p>You can close this tab and return to the terminal.</p>")
            else:
                result["error"] = "No authorization code in OAuth callback"
                self._respond(400, "<h1>Error</h1><p>No authorization code was received.</p>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _respond(self, http_code, body):
            self.send_response(http_code)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif;text-align:center;padding-top:3em'>{body}</body></html>".encode())

        def log_message(self, format, *args):
            pass

    _oauth_params = urlencode({
        'client_id': 'laintas-cli',
        'redirect_uri': callback_url,
        'response_type': 'code',
        'scope': 'openid profile email',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })
    login_url = f"{ACCOUNTS_BASE}/api/auth/oauth2/authorize?{_oauth_params}"

    console.print(Panel(
        f"[bold]Opening browser for login[/bold]\n\n"
        f"If your browser doesn't open automatically, visit:\n"
        f"[link={login_url}]{login_url}[/link]\n\n"
        f"[dim]Waiting for authentication... (timeout in 2 minutes)[/dim]",
        title="Laintas Auth"
    ))

    webbrowser.open(login_url)

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    server.timeout = 120

    try:
        server.handle_request()
    except Exception:
        pass
    finally:
        server.server_close()

    if result["code"]:
        # Exchange the OAuth code, then mint a standard short-lived CLI session.
        try:
            resp = requests.post(
                f"{ACCOUNTS_BASE}/api/auth/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": result["code"],
                    "code_verifier": verifier,
                    "redirect_uri": callback_url,
                    "client_id": "laintas-cli",
                },
                timeout=10,
                allow_redirects=False,
            )
            if resp.status_code == 200:
                try:
                    oauth_data = resp.json()
                except ValueError:
                    oauth_data = {}
                access_token = oauth_data.get("access_token", "")
                if not access_token:
                    console.print("[red]OAuth exchange returned no access token.[/red]")
                    return None

                session_resp = requests.post(
                    f"{ACCOUNTS_BASE}/api/sso/cli-session",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"idToken": oauth_data.get("id_token", "")},
                    timeout=10,
                    allow_redirects=False,
                )
                if session_resp.status_code != 200:
                    console.print(f"[red]CLI session exchange failed (HTTP {session_resp.status_code}).[/red]")
                    return None
                data = session_resp.json()
                token = data.get("token", "")
                user = data.get("user") or {}
                signed = (session_resp.cookies.get("__Secure-laintas-v2.session_token")
                          or session_resp.cookies.get("laintas-v2.session_token")
                          or "")
                if not token or not signed:
                    console.print("[red]CLI session exchange returned an incomplete session.[/red]")
                    return None
                session = {
                    "token": token,
                    "cookies": {"__Secure-laintas-v2.session_token": signed},
                    "headers": {},
                    "userId": user.get("id", ""),
                    "userName": user.get("name", ""),
                    "userEmail": user.get("email", ""),
                }
                if not verify_session(session):
                    console.print("[red]The new CLI session could not be verified.[/red]")
                    return None
                save_session(session)
                display = (session.get("userEmail") or session.get("userName")
                           or session.get("userId") or "Laintas user")
                console.print(f"[green]Logged in as {display}[/green]")
                return session
            else:
                console.print(f"[red]OAuth code exchange failed (HTTP {resp.status_code}).[/red]")
                return None
        except requests.RequestException as e:
            console.print(f"[red]Cannot reach {ACCOUNTS_BASE} for token exchange: {e}[/red]")
            return None

    if result["error"]:
        console.print(f"[red]{result['error']}[/red]")
    else:
        console.print("[yellow]Login timed out.[/yellow]")
    return None


def ensure_auth() -> Optional[dict]:
    """Ensure the user is authenticated. Returns session dict, None, or exits.

    Flow: cached session → return if valid.
          No cache → ask for login method → browser or terminal.
          Expired cache → clear and return None (use /login to re-auth).
    """
    # 1. Try cached session
    session = load_session()
    if session:
        user_info = verify_session(session)
        if user_info:
            session["userId"] = user_info["id"]
            session["userName"] = user_info.get("name", "")
            session["userEmail"] = user_info.get("email", "")
            return session
        else:
            console.print("[yellow]Session expired. Use /login to re-authenticate.[/yellow]")
            clear_session()
            return None

    # 2. No cached account — ask for login method
    console.print()
    console.print(Panel(
        "[bold]Login to Laintas[/bold]\n\n"
        "[1] [bold]Remote login[/bold] — opens accounts.laintas.com\n"
        "    (works from any device, no password typing)\n\n"
        "[2] [bold]Local login[/bold] — username + password in terminal\n"
        "    (no browser needed)",
        title="Choose login method"
    ))

    for _ in range(3):
        choice = input("Choose [1] or [2] (default 1): ").strip() or "1"
        if choice == "1":
            session = login_via_browser()
            if session:
                return session
            console.print("[yellow]Remote login failed. Try method [2]?[/yellow]")
        elif choice == "2":
            session = login_interactive()
            if session:
                return session
        else:
            console.print("[red]Invalid choice. Enter 1 or 2.[/red]")
            continue

    console.print("[red]Authentication failed. Exiting.[/red]")
    sys.exit(1)


# ── CLI Prompt Template (.laintas/cli.prop) ──────────────────────────────

EXTRA_COMMAND_TEMPLATE = '''# .laintas/commands.py — define custom slash commands for the REPL
# context keys: session, interactive_session, agent_registry, console,
#   get_terminal, get_all_terminals, unregister_terminal, register_terminal,
#   rename_terminal, get_agent, get_all_agents, get_current_agent,
#   station_agent, unstation_agent,
#   SubTerminalSession, observe_session, _show_terminal_detail,
#   get_config, set_config, list_config, reset_config, reload_default_files


def handle_extra_command(action, parts, ctx):
    """Return True if handled, False to pass through."""
    console = ctx["console"]

    if action == "/config":
        # /config                → show all
        # /config <key>          → show one
        # /config <key> <value>  → set value
        # /config reset          → reset all
        get_cfg = ctx["get_config"]
        set_cfg = ctx["set_config"]
        list_cfg = ctx["list_config"]
        reset_cfg = ctx["reset_config"]

        labels = {
            "max_loops": "Max AI loops",
            "max_tokens": "Max API tokens",
            "max_debug_entries": "Max debug entries",
            "loop_delay": "Loop delay (s)",
            "output_truncate": "Output truncate (chars)",
            "poll_timeout": "Poll timeout (s)",
            "terminal_tail_lines": "Sub-terminal tail lines",
            "heartbeat_interval": "Heartbeat interval (s)",
        }

        if len(parts) == 1:
            # /config — show all
            from rich.table import Table
            t = Table(title="Runtime Config")
            t.add_column("Key", style="cyan")
            t.add_column("Value")
            t.add_column("Description")
            for k, v in list_cfg().items():
                t.add_row(k, str(v), labels.get(k, ""))
            console.print(t)

        elif len(parts) == 2:
            if parts[1] == "reset":
                reset_cfg()
                console.print("[green]Config reset to defaults.[/green]")
            else:
                key = parts[1]
                val = get_cfg(key)
                if val is not None:
                    console.print(f"{labels.get(key, key)}: [bold]{val}[/bold]")
                else:
                    console.print(f"[red]Unknown key: {key}[/red]")

        elif len(parts) >= 3:
            key = parts[1]
            raw = " ".join(parts[2:])
            # coerce type
            defaults = list_cfg()
            if key not in defaults:
                console.print(f"[red]Unknown key: {key}[/red]")
                return True
            old = get_cfg(key)
            try:
                if isinstance(defaults[key], bool):
                    val = raw.lower() in ("true", "1", "yes", "on")
                elif isinstance(defaults[key], int):
                    val = int(raw)
                else:
                    val = float(raw)
            except ValueError:
                console.print(f"[red]Invalid value for {key}: {raw}[/red]")
                return True
            set_cfg(key, val)
            console.print(f"[green]{labels.get(key, key)}: {old} → [bold]{val}[/bold][/green]")

        return True

    if action == "/reload":
        reload = ctx.get("reload_default_files")
        if reload:
            reload()
        else:
            console.print("[red]reload_default_files not available[/red]")
        return True

    return False
'''

LOOP_COMMAND_TEMPLATE = """import os
import re
import json
import subprocess
import sys


def handle_loop_command(command, ctx):
    \"\"\"Handle custom loop commands defined in .laintas/loop.py.\"\"\"

    # parent(<shell command>) \\u2014 execute in parent terminal context (side effects like cd/clear)
    m = re.match(r'^parent\\((.+)\\)\\s*$', command)
    if m:
        parent_cmd = m.group(1).strip()
        if ctx.get("depth", 0) == 0:
            return _execute_parent_command(parent_cmd)
        else:
            return f"__PARENT_CMD__:{parent_cmd}"

    # learn(<text|path|url>) — extract knowledge into conversation history as [KNOWLEDGE]
    m = re.match(r'^learn\\((.+)\\)\\s*$', command)
    if m:
        return _learn(m.group(1).strip(), ctx)

    # forget(N) — trim chat_history, keeping knowledge + last N messages
    m = re.match(r'^forget\\((\\d+)\\)\\s*$', command)
    if m:
        return _forget(int(m.group(1)), ctx)

    return None


def _execute_parent_command(cmd):
    \"\"\"Execute a command in the parent process context so side effects
    (cd, clear) apply to the parent terminal, not a child PTY.\"\"\"
    stripped = cmd.strip()
    if stripped in ("cd",) or stripped.startswith("cd "):
        path = stripped[3:].strip() if stripped.startswith("cd ") else os.path.expanduser("~")
        try:
            os.chdir(path)
            return f"cd -> {os.getcwd()}"
        except Exception as e:
            return f"cd error: {e}"
    if stripped in ("clear",):
        sys.stdout.write("\\033[2J\\033[H")
        sys.stdout.flush()
        return ""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
            cwd=os.getcwd(),
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return f"Parent command timed out: {cmd}"
    except Exception as e:
        return f"Parent command error: {e}"


def _learn(target, ctx):
    \"\"\"Extract text from a file, URL, image (OCR), or raw string and add to conversation history as [KNOWLEDGE].\"\"\"
    chat_history = ctx.get("chat_history", [])

    # 1. URL
    if target.startswith(('http://', 'https://')):
        try:
            result = subprocess.run(
                ['curl', '-sL', '--max-time', '30', target],
                capture_output=True, text=True, timeout=35
            )
            text = result.stdout[:50000]
            source = target
        except Exception as e:
            return f"learn() failed to fetch URL: {e}"

    # 2. Existing file
    elif os.path.isfile(target):
        path = target
        ext = os.path.splitext(path)[1].lower()
        source = os.path.abspath(path)

        # Text files (code, config, logs, data)
        if ext in ('.txt', '.md', '.py', '.js', '.ts', '.jsx', '.tsx', '.json',
                     '.xml', '.html', '.css', '.scss', '.less',
                     '.yaml', '.yml', '.toml', '.cfg', '.ini', '.conf',
                     '.sh', '.bash', '.zsh', '.fish', '.log', '.csv', '.tsv',
                     '.sql', '.r', '.rb', '.go', '.rs', '.java', '.c', '.cpp',
                     '.h', '.hpp', '.php', '.swift', '.kt', '.scala', '.clj',
                     '.env', '.gitignore', '.dockerignore', '.editorconfig'):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()[:50000]
            except Exception as e:
                return f"learn() failed to read file: {e}"

        # PDF
        elif ext == '.pdf':
            try:
                result = subprocess.run(
                    ['pdftotext', path, '-', '-l', '50'],
                    capture_output=True, text=True, timeout=30
                )
                text = result.stdout[:50000]
                if not text.strip():
                    return "learn() could not extract text from PDF (empty or image-based, try OCR)"
            except FileNotFoundError:
                return "learn() needs 'pdftotext' (poppler-utils). Install: apt install poppler-utils"
            except Exception as e:
                return f"learn() failed on PDF: {e}"

        # Images — OCR
        elif ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp'):
            try:
                result = subprocess.run(
                    ['tesseract', path, 'stdout'],
                    capture_output=True, text=True, timeout=30
                )
                text = result.stdout.strip()[:50000]
                if not text:
                    return "learn() OCR found no text in image"
            except FileNotFoundError:
                return "learn() needs 'tesseract' for images. Install: apt install tesseract-ocr"
            except Exception as e:
                return f"learn() failed on image: {e}"

        # Video — metadata + subtitle scan
        elif ext in ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv'):
            try:
                result = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                     '-show_format', '-show_streams', path],
                    capture_output=True, text=True, timeout=15
                )
                info = json.loads(result.stdout)
                sub_lines = []
                for s in info.get('streams', []):
                    if s.get('codec_type') == 'subtitle':
                        lang = s.get('tags', {}).get('language', 'unknown')
                        sub_lines.append(
                            f"  Subtitle: {s.get('codec_name', '?')} lang={lang}"
                        )
                meta = info.get('format', {}).get('tags', {})
                meta_text = '\\n'.join(f"  {k}: {v}" for k, v in meta.items())

                text = f"Video: {os.path.basename(path)}\\n"
                if meta_text:
                    text += f"Metadata:\\n{meta_text}\\n"
                if sub_lines:
                    text += f"Subtitle tracks:\\n" + '\\n'.join(sub_lines) + "\\n"
                    text += "(Extract with: ffmpeg -i <video> -map 0:s:<N> subs.srt)"
                else:
                    text += "No embedded subtitles found. To learn from audio:\\n"
                    text += "  ffmpeg -i <video> -vn -acodec pcm_s16le audio.wav\\n"
                    text += "  whisper audio.wav --model small"
                text = text[:50000]
            except FileNotFoundError:
                return "learn() needs 'ffprobe' (ffmpeg). Install: apt install ffmpeg"
            except Exception as e:
                return f"learn() failed on video: {e}"

        # Unknown extension — try as text
        else:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()[:50000]
            except Exception as e:
                return f"learn() cannot handle '{ext}' files. Try .txt, .md, .pdf, .png, .mp4, etc."

    # 3. Raw text (user's sentence / knowledge)
    else:
        text = target[:50000]
        source = "user-provided knowledge"

    # Append knowledge to conversation history (short-term, cleared on session end)
    knowledge_text = text[:5000]  # cap per entry to keep prompts manageable
    chat_history.append({
        "role": "knowledge",
        "content": f"(Source: {source})\\n{knowledge_text}"
    })

    preview = text[:300].replace('\\n', ' ')
    return f"Learned ({len(text)} chars) from: {source}\\nPreview: {preview}..."


def _forget(keep_n, ctx):
    \"\"\"Trim chat_history: keep all [KNOWLEDGE] entries + last N regular messages.\"\"\"
    chat_history = ctx.get("chat_history", [])
    if not chat_history or keep_n < 0:
        return f"Nothing to forget (history has {len(chat_history)} messages)"

    # Separate knowledge entries from regular ones
    knowledge_entries = [m for m in chat_history if m.get('role') == 'knowledge']
    regular_entries = [m for m in chat_history if m.get('role') != 'knowledge']

    # Keep last N regular entries
    trimmed_regular = regular_entries[-keep_n:] if keep_n > 0 else []

    # Rebuild: knowledge first, then recent regular messages
    trimmed = knowledge_entries + trimmed_regular
    removed = len(chat_history) - len(trimmed)

    # Mutate in-place
    chat_history.clear()
    chat_history.extend(trimmed)

    return f"Forgot {removed} messages ({len(knowledge_entries)} knowledge kept, {len(trimmed_regular)} recent kept, {len(chat_history)} total)"
"""


def generate_cli_prop_template() -> str:
    """Generate the .laintas/cli.prop system prompt template for the current OS.

    The template uses XML-style sections and teaches the
    agent the full surface: shell, /tool dispatch, /term, /spawn, memory.

    Variables substituted at run time (see agent_loop.run_agent_loop):
      {{agentName}} {{agentId}} {{currentPath}} {{depth}}
      {{globalMemory}} {{persistentMemory}} {{lastSession}}
      {{planMode}} {{tools}} {{inbox}} {{parallelResults}} {{children}} {{parent}}
      {{terminalName}} {{parentTerminal}} {{deploymentStatus}}
      {{workflowPhase}} {{rolePrompt}} {{confidenceGuidance}}
      {{skillContext}} {{promptOpt}}

    {{nextDepth}}, {{activeFile}}, and {{behaviorDiagnostics}} are still computed
    and .replace()'d by run_agent_loop for backward compatibility, but are not
    referenced by this template — {{activeFile}} only ever resolves to the literal
    string "None" and {{behaviorDiagnostics}} is always "", so neither carries
    real signal yet. Add them back here if/when they're wired to real values.
    """
    shell_info = "cmd.exe" if IS_WINDOWS else SHELL_NAME

    return f"""<role>
You are {{{{agentName}}}} (id: {{{{agentId}}}}, role: {{{{deploymentStatus}}}}), an autonomous coding agent running in laintas-cli.
Solve real engineering tasks by reading the repo, using tools, editing files, running commands, verifying results, and reporting briefly.
</role>

<environment>
- OS: {SYSTEM} | Shell: {shell_info} | CWD: {{{{currentPath}}}}
- Terminal: {{{{terminalName}}}} | Parent terminal: {{{{parentTerminal}}}}
- Depth: {{{{depth}}}} | Parent agent: {{{{parent}}}} | Children: {{{{children}}}}
- Inbox: {{{{inbox}}}}
{{{{parallelResults}}}}
- Plan mode: {{{{planMode}}}}
- Current date/time is appended by the runtime.
</environment>

<memory>
Persistent memory:
{{{{persistentMemory}}}}

Project rules:
{{{{globalMemory}}}}

Last session:
{{{{lastSession}}}}
</memory>

<skills>
{{{{skills}}}}

If a skill looks relevant, call `skill_load` with its name before doing specialized work.
Do not assume unloaded skill instructions. After loading, continue using the instructions below.
When done with that specialized work, call `skill_unload` with its name to free context; skills can be re-loaded later.

Loaded skill instructions:
{{{{skillContext}}}}
</skills>

<tools>
Use `shell` for shell commands and meta-commands; use structured tools for file, memory, task, plan, web, agent, terminal, and time operations.
How to use the shared core tools (reading/editing/verifying files, shell, memory, tasks, web) is in the injected <core_tool_usage> block; general operating discipline (act-first, batching, failure handling, scope, safety, verification, skills) is in the injected <agent_conduct> block — follow both.
Tools beyond that core (sub-terminals, parallel agent orchestration, plan files, …) have their detailed usage in skills: call `skill_list` to see them and `skill_load` (with `name`) to load one before that specialized work.
The catalog below documents each tool's purpose and parameters:
{{{{tools}}}}
</tools>

<workflow>
- Track approved work with steps. In ACT mode, use `task.create`/`task.update` for concrete execution steps. In PLAN mode, update the versioned plan and call `plan.submit`; do not create execution steps before approval. Keep one step in progress per agent (parallel owners may each hold one), and complete steps only after verification. `<approved_work_plan>` is authoritative; `<active_tasks>` is its execution view.
- Resuming: the session stays alive until `/q`. If the user asks to continue/resume prior work ("继续", "continue", "接着", etc.), call `session.continue` to resume the latest interrupted run and in_progress `<active_tasks>` — do NOT create a new task. The session's full context is already in your thread; just keep going.
- If the user asks a clear read/edit/build/test/investigate task, act with tools. Do not ask for permission to do exactly what was asked.
- Ask one concise clarifying question only when the target or intent is genuinely ambiguous, destructive, impossible to infer safely, or blocked on information you cannot discover yourself.
- If there are multiple reasonable approaches with materially different tradeoffs, stop and present 2-3 labeled options. State the consequence of each option briefly, then wait for the user's choice.
{{{{workflowPhase}}}}
{{{{rolePrompt}}}}
{{{{confidenceGuidance}}}}
</workflow>

<output_rules>
(General act-first / no-transitional-narration / batching / language rules are in the injected <agent_conduct> block. The rules below are laintas loop-control specifics.)
- Your reply is OPTIONAL. Leave it empty on ordinary execution steps and just emit the tool call(s). Write user-facing text ONLY when: (a) you are giving the final answer/result, (b) you must ask the user a clarifying question, or (c) a non-obvious decision needs a one-line rationale. When you do write, cite files as path:line.
- Completion is an explicit act: call `task_complete` with a `summary` when — and only when — the task is fully finished. Do NOT stop just to narrate progress; if more work remains, include the next tool call in the SAME turn and keep going.
- If you have nothing concrete to run this turn but the task is NOT finished (still reasoning or planning), call `task_continue` so the loop keeps going. Never end mid-task with no tool call.
- Ending your turn with no tool call is only for asking the user something or handing back a final answer. It does not by itself mean the task is done.
</output_rules>

<safety>
Do not bypass policy.py decisions. Do not invent paths, APIs, files, or results. (General safety — reversibility/blast-radius, destructive-action confirmation, investigate-before-overwrite, no-vulnerabilities — is in the injected <agent_conduct> block.)
</safety>
{{{{promptOpt}}}}
"""

# ── File Helpers ───────────────────────────────────────────────────────

def _format_time_ago(ts: float) -> str:
    """Render a timestamp as a coarse 'N min/hours/days ago' string."""
    try:
        delta = max(0, int(time.time() - (ts or 0)))
    except (TypeError, ValueError):
        return "unknown"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} hour(s) ago"
    return f"{delta // 86400} day(s) ago"


def _resume_turn_count(blob: Optional[dict]) -> int:
    """Count user turns in a resume blob for user-facing status text."""
    if not blob:
        return 0
    return len([m for m in (blob.get("chat_history") or []) if m.get("role") == "user"])


def _restore_resume_blob(blob: dict, chat_history: list) -> dict:
    """Restore full-fidelity conversation state from a per-cwd resume blob.

    Restores conversation + working state + the task plan, mirroring how
    Codex/Claude bring back a session: the transcript, the older-turn summary
    (so long sessions don't lose early goals), and the open todo/plan list.
    """
    chat_history.clear()
    # Prepend a digest of turns that were dropped past the resume window so the
    # early goals/instructions aren't lost on a long session.
    older = (blob.get("older_summary") or "").strip()
    if older:
        chat_history.append({"role": "knowledge", "content": f"[resumed session context]\n{older}"})
    chat_history.extend(blob.get("chat_history") or [])
    try:
        if blob.get("active_work_id"):
            workgraph.set_active_work(
                str(blob["active_work_id"]), cwd=blob.get("cwd"))
    except workgraph.WorkGraphError:
        pass
    # Rehydrate the plan (in_progress + pending tasks) as session tasks so
    # <active_tasks> shows it immediately and "continue" can resume it.
    try:
        task_manager.import_session_tasks(blob.get("tasks") or [],
                                          cwd=blob.get("cwd"))
    except Exception:
        pass
    return prepare_state_for_repl(blob.get("state") or {})


def _resume_choices(cwd: str) -> list:
    """Selectable resume states for this cwd, newest first.

    Returns all states (checkpoints + autosaves) so the user can resume from
    any saved point. Previously this filtered to only checkpoints when present,
    which hid newer autosaves created after a /q checkpoint.
    """
    return list_resume_states(cwd)


def _resolve_resume_selector(choices: list, selector: str) -> Optional[dict]:
    """Non-interactive resume selection by 'latest'/'last' or 1-based index.

    Returns the chosen blob, or None (printing a reason) when the selector is
    empty/invalid. Used by `--resume`/`--continue` and `/resume <N>`.
    """
    if not choices:
        return None
    selector = (selector or "").strip().lower()
    if selector in ("", "latest", "last"):
        return choices[0]
    if selector.isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(choices):
            return choices[idx]
        console.print(f"[red]Invalid resume number: {selector}[/red]")
        return None
    console.print(f"[red]Invalid resume selector: {selector}[/red]")
    return None


def _choose_resume_blob(cwd: str, selector: str = "") -> Optional[dict]:
    """Resolve a saved resume state non-interactively (CLI-flag entry point)."""
    return _resolve_resume_selector(_resume_choices(cwd), selector)


def show_resume_picker(cwd: str) -> Optional[dict]:
    """Full-screen `/t`-style picker for saved resume sessions.

    Arrow keys to navigate, Enter to resume the highlighted session, d for a
    details preview, x to delete a checkpoint, q/Esc to cancel. Returns the
    chosen blob, or None if cancelled.
    """
    choices = _resume_choices(cwd)
    if not choices:
        console.print("[yellow]No saved session to resume in this directory.[/yellow]")
        return None

    def _build_labels():
        labels = []
        for item in choices:
            kind = item.get("kind", "session")
            badge = "[magenta]◆ checkpoint[/magenta]" if kind == "checkpoint" else "[blue]○ autosave[/blue]"
            turns = item.get("turn_count") or _resume_turn_count(item)
            ago = _format_time_ago(item.get("timestamp", 0))
            title = str(item.get("title") or "Untitled session")[:60].replace("\n", " ")
            labels.append(f"{badge}  [dim]{ago}[/dim]  {turns} turn(s)  [bold]{title}[/bold]")
        return labels

    sel_idx = 0
    while choices:
        labels = _build_labels()
        result = select_dialog(
            labels,
            title="Resume Session",
            full_screen=True,
            selected_index=sel_idx,
            action_keys={"d": "details", "x": "delete"},
            enter_action="resume",
            hint="↑↓ navigate  ↵ resume  d details  x delete  q cancel",
        )
        if result is None:
            return None
        action, idx = result
        if action is None or idx < 0 or idx >= len(choices):
            return None
        item = choices[idx]
        if action == "resume":
            return item
        if action == "details":
            _show_resume_detail(item)
            _print_resume_transcript(item, 20)
            input("\n[dim]Press Enter to continue...[/dim]")
            sel_idx = idx
        elif action == "delete":
            delete_resume_state(cwd, item)
            del choices[idx]
            console.print(f"\n[green]Deleted saved session.[/green]")
            if not choices:
                console.print("[dim]No more saved sessions.[/dim]")
                return None
            sel_idx = min(idx, len(choices) - 1)
    return None


def _show_resume_detail(item: dict) -> None:
    """Print a rich summary of one resume blob for the picker's details view."""
    console.print()
    console.print(f"[bold cyan]{item.get('title') or 'Untitled session'}[/bold cyan]")
    console.print(f"[dim]Type:[/dim] {item.get('kind', 'session')}   "
                  f"[dim]When:[/dim] {_format_time_ago(item.get('timestamp', 0))}   "
                  f"[dim]Turns:[/dim] {item.get('turn_count') or _resume_turn_count(item)}")
    history = item.get("chat_history") or []
    console.print(f"[dim]Messages:[/dim] {len(history)}\n")
    for msg in history[-6:]:
        role = msg.get("role", "?")
        text = str(msg.get("content") or "").replace("\n", " ")[:100]
        color = "green" if role == "user" else "blue"
        console.print(f"  [{color}]{role:>9}[/{color}]  [dim]{text}[/dim]")


def _resume_role_style(role: str) -> str:
    """Map a chat role to a rich color for transcript rendering."""
    if role == "user":
        return "green"
    if role == "assistant":
        return "blue"
    if role == "knowledge":
        return "yellow"
    return "cyan"


def _print_resume_transcript(blob: dict, limit: Optional[int]) -> None:
    """Echo a resumed session's conversation (read-only).

    ``limit`` is the number of most-recent messages to show; ``None`` shows the
    whole transcript, and any value <= 0 prints nothing. Does not mutate the
    passed blob or any live chat history. The dropped-turn digest
    (``older_summary``) is shown only when the window reaches the start.
    """
    if limit is not None and limit <= 0:
        return
    history = blob.get("chat_history") or []
    total = len(history)
    if limit is None:
        window = list(history)
        at_start = True
    else:
        window = history[-limit:]
        at_start = len(window) >= total
    console.print()
    console.print(
        f"[dim]── conversation ── {len(window)}/{total} message(s) "
        f"{'(all)' if at_start else '(most recent)'} ──[/dim]"
    )
    older = (blob.get("older_summary") or "").strip()
    if at_start and older:
        console.print(f"[dim yellow][earlier session context]\n{older}[/dim yellow]\n")
    for msg in window:
        role = str(msg.get("role", "?"))
        content = str(msg.get("content") or "")
        color = _resume_role_style(role)
        console.print(f"[bold {color}]{role}[/bold {color}]")
        console.print(content or "[dim](empty)[/dim]")
        console.print()


def read_file(path: str) -> Optional[str]:
    """Read a file, return None if not found."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


def write_file(path: str, content: str) -> None:
    """Write content to a file."""
    Path(path).write_text(content, encoding="utf-8")


def append_file(path: str, content: str) -> None:
    """Append content to a file."""
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + content + "\n")


def ensure_files_exist() -> None:
    """Create .laintas/ project directory with cli.prop, memory.json, commands.py, loop.py."""
    proj = paths.ensure_project_dir()

    cli_prop_path = paths.project_file(paths.CWD_CLI_PROP)
    memory_path = paths.project_file(paths.CWD_MEMORY)
    commands_path = paths.project_file(paths.CWD_COMMANDS)
    loop_path = paths.project_file(paths.CWD_LOOP)

    if not cli_prop_path.exists():
        template = generate_cli_prop_template()
        cli_prop_path.write_text(template, encoding="utf-8")
        console.print(f"[dim]Created {cli_prop_path}[/dim]")

    if not memory_path.exists():
        memory_path.write_text("", encoding="utf-8")
        console.print(f"[dim]Created {memory_path}[/dim]")

    if not commands_path.exists():
        commands_path.write_text(EXTRA_COMMAND_TEMPLATE, encoding="utf-8")
        console.print(f"[dim]Created {commands_path}[/dim]")

    if not loop_path.exists():
        loop_path.write_text(LOOP_COMMAND_TEMPLATE, encoding="utf-8")
        console.print(f"[dim]Created {loop_path}[/dim]")

    # Generated defaults are safe to execute until their bytes change. This
    # also migrates existing installations whose files still match defaults.
    trust_store.record_generated_file(commands_path, EXTRA_COMMAND_TEMPLATE)
    trust_store.record_generated_file(loop_path, LOOP_COMMAND_TEMPLATE)


def reload_default_files() -> None:
    """Delete all project files in .laintas/ and restart laintas_cli."""
    proj = paths.project_dir()
    existing = [proj / name for name in paths._ALL_CWD_FILES
                if (proj / name).exists()]
    if existing:
        import policy as _policy
        decision = _policy.evaluate_file_delete(str(proj), os.getcwd())
        if decision.action == "deny":
            console.print(f"[red]Blocked by policy: {decision.reason}[/red]")
            return
        if decision.action == "needs_approval":
            preview = "DELETE generated project files\n" + "\n".join(
                f"  {path}" for path in existing)
            if not request_file_delete_approval(
                    str(proj), preview, decision.reason):
                console.print("[yellow]Reload cancelled; no files were deleted.[/yellow]")
                return
    for name in paths._ALL_CWD_FILES:
        f = proj / name
        if f.exists():
            f.unlink()
            console.print(f"[dim]Deleted {f}[/dim]")
    # Remove .laintas/ directory if empty
    try:
        if proj.exists() and not any(proj.iterdir()):
            proj.rmdir()
    except OSError:
        pass
    console.print("[yellow]Restarting laintas_cli...[/yellow]")
    os.execv(_LAUNCH_SCRIPT_PATH, [_LAUNCH_SCRIPT_PATH] + sys.argv[1:])


# ── Backend API ────────────────────────────────────────────────────────

def get_auth_cookies(session: dict) -> dict:
    """Build cookies for backend API requests from session."""
    return session.get("cookies", {})


def get_auth_headers(session: dict) -> dict:
    """Build headers for backend API requests from session."""
    headers = {"Content-Type": "application/json"}
    if session.get("headers", {}).get("Authorization"):
        headers["Authorization"] = session["headers"]["Authorization"]
    return headers


def _repair_json_candidate(candidate: str) -> str:
    """Best-effort repair of the most common model JSON defects.

    `json.loads` is strict, but models reliably make two harmless mistakes that
    a coding agent triggers constantly:
      1. Literal control characters (newlines/tabs/carriage returns) inside
         string values — e.g. a multi-line code block or shell command dropped
         into `reply`/`command` without escaping to \\n. JSON forbids raw
         control chars in strings.
      2. Trailing commas before } or ].
    Both are losslessly fixable. We walk the text tracking string state so we
    only escape control chars *inside* strings (structural whitespace between
    tokens is left alone), then strip trailing commas. Unescaped inner quotes
    are NOT handled (ambiguous) and still fall through to a retry nudge.
    """
    out = []
    in_str = False
    esc = False
    for c in candidate:
        if in_str:
            if esc:
                out.append(c)
                esc = False
            elif c == "\\":
                out.append(c)
                esc = True
            elif c == '"':
                out.append(c)
                in_str = False
            elif c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            else:
                out.append(c)
        else:
            if c == '"':
                in_str = True
            out.append(c)
    repaired = "".join(out)
    # Strip trailing commas: ",}" / ",]" (with optional whitespace).
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    return repaired


def _native_to_tool_calls(frags: dict, name_map: Optional[dict] = None) -> list:
    """Reassemble OpenAI-native streamed tool_calls into laintas tool_calls.

    Some providers emit tool calls through the function-calling channel
    (`delta.tool_calls`) instead of inside the JSON body. Those fragments stream
    incrementally, keyed by `index`; each carries a partial `function.name` and a
    slice of `function.arguments`. We reassemble per index and parse the
    arguments JSON. Without this they are dropped and the model's action never
    runs — the model then re-requests the same read/edit forever.

    `name_map` un-mangles provider-safe names (e.g. `fs_write` → `fs.write`)
    back to canonical tool names — see ToolRegistry.to_openai_tools.
    """
    name_map = name_map or {}
    out = []
    for idx in sorted(frags):
        slot = frags[idx]
        name = (slot.get("name") or "").strip()
        # Some providers leave a bare name (e.g. "exec") but encode the
        # fully-qualified tool in the id, e.g. "functions.shell.exec:0".
        m = re.match(r"functions\.(.+?)(?::\d+)?$", slot.get("id") or "")
        if m and "." in m.group(1):
            name = m.group(1)
        # Un-mangle provider-safe names back to canonical (fs_write → fs.write).
        name = name_map.get(name, name)
        if not name:
            continue
        args_raw = (slot.get("arguments") or "").strip()
        if not args_raw:
            args = {}
        else:
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                try:
                    args = json.loads(_repair_json_candidate(args_raw))
                except json.JSONDecodeError:
                    args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        out.append({"name": name, "arguments": args})
    return out


def _extract_tagged_tool_calls(text: str) -> Optional[list]:
    """Best-effort compatibility for malformed `<tool_calls>...</tool_calls>` output.

    The prompt forbids this syntax, but older/model-regressed turns may emit it.
    Convert it to the canonical tool_calls list so the loop can continue.
    """
    if not text:
        return None
    m = re.search(r"<tool_calls>\s*(.*?)\s*</tool_calls>", text, re.I | re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        args = item.get("arguments") or {}
        out.append({"name": name, "arguments": args if isinstance(args, dict) else {"value": args}})
    return out or None


def call_backend_stream(
    session: dict,
    message: str,
    system_prompt: str,
    current_path: str,
    history: list = None,
    lang: str = "EN",
    on_chunk: Optional[Callable[[str, str], None]] = None,
    interrupt_event: Optional[threading.Event] = None,
    messages: Optional[list] = None,
    tools_enabled: bool = True,
) -> dict:
    """Call Helpwo backend /api/chat/stream, same as Helpwo frontend.
    Returns parsed {reply, command, memory, done, _billing} dict.

    If interrupt_event is provided, it is checked between SSE chunks so the
    request can be aborted gracefully on Ctrl+C.

    If `messages` is provided (native message-thread mode), it is sent as the
    OpenAI-format `messages` array and the backend uses it verbatim (prepending
    systemPrompt as the system message). The legacy `message`/`history` fields
    are then ignored by the backend, so the model resumes a real conversation
    thread (assistant tool_calls + role:tool results) instead of a re-synthesized
    state-dump user turn.
    """
    backend_profile = get_backend_profile()
    backend_url = backend_profile.base_url

    payload = {
        "message": message,
        "history": history or [],
        "currentPath": current_path,
        "systemPrompt": system_prompt,
        "lang": lang,
        "maxTokens": int(get_runtime_config("max_tokens")),
        # Billing attribution: without this the gateway books the call under
        # its default product ("helpwo") — quota and /usage stats then miss it.
        "source": "cli",
        # Core-tool usage ("how to read/edit/verify files", etc.) is the
        # gateway's single source of truth — it appends the canonical guide to
        # the system message. The base prompt below intentionally omits it.
        "injectToolGuide": True,
    }
    if messages:
        payload["messages"] = messages
    selected_model = get_selected_model()
    if selected_model:
        payload["model"] = selected_model
    selected_provider = get_selected_provider()
    if selected_provider:
        payload["provider"] = selected_provider

    # Native function-calling: advertise the tool registry as OpenAI-style
    # `tools` schemas so the model emits grammar-constrained `delta.tool_calls`
    # instead of hand-serialized JSON in its reply text. `tool_name_map`
    # un-mangles provider-safe names on the way back. The backend already
    # forwards `tools`/`tool_choice` to the provider and streams tool_calls.
    # Summary/compaction calls pass tools_enabled=False: a tool-less, text-only
    # completion (no tool schemas, no core-tool guide) so the model just writes
    # the requested summary instead of trying to act.
    tool_name_map: dict = {}
    if not tools_enabled:
        payload["injectToolGuide"] = False
    else:
        try:
            _unified_catalog = bool(get_runtime_config("use_unified_catalog"))
            _openai_tools, tool_name_map = tools_mod.get_registry().to_openai_tools(unified=_unified_catalog)
            if _openai_tools:
                payload["tools"] = _openai_tools
                payload["tool_choice"] = "auto"
        except Exception:
            tool_name_map = {}

    headers, cookies = backend_profiles.request_auth(backend_profile, session)

    try:
        # ── Retry loop for transient failures (opencode retry.ts pattern) ──
        # Retries on: Timeout, ConnectionError, HTTP 429, HTTP 5xx.
        # Does NOT retry on: 4xx (except 429), context-overflow (handled by
        # reactive compaction in agent_loop), or InterruptedError.
        # Honors `retry-after` header; exponential backoff: 2s, 4s, 8s, cap 30s.
        _MAX_RETRIES = 3
        _RETRY_BASE = 2.0
        _RETRY_CAP = 30.0
        response = None
        for _attempt in range(_MAX_RETRIES + 1):
            if interrupt_event is not None and interrupt_event.is_set():
                raise InterruptedError("interrupted before request")
            try:
                response = requests.post(
                    f"{backend_url}/api/chat/stream",
                    json=payload,
                    headers=headers,
                    cookies=cookies,
                    stream=True,
                    timeout=120,
                    allow_redirects=False,
                )
            except requests.Timeout:
                if _attempt < _MAX_RETRIES:
                    _delay = min(_RETRY_BASE * (2 ** _attempt), _RETRY_CAP)
                    if interrupt_event is not None:
                        if interrupt_event.wait(timeout=_delay):
                            raise InterruptedError("interrupted during retry delay")
                    else:
                        time.sleep(_delay)
                    continue
                return {"reply": "Request timed out after retries. Please try again.", "tool_calls": [], "done": True, "error": True}
            except requests.ConnectionError:
                if _attempt < _MAX_RETRIES:
                    _delay = min(_RETRY_BASE * (2 ** _attempt), _RETRY_CAP)
                    if interrupt_event is not None:
                        if interrupt_event.wait(timeout=_delay):
                            raise InterruptedError("interrupted during retry delay")
                    else:
                        time.sleep(_delay)
                    continue
                return {"reply": f"Cannot connect to backend ({backend_url}). Check your network.", "tool_calls": [], "done": True, "error": True}

            if response.status_code == 200:
                break

            if 300 <= response.status_code < 400:
                return {
                    "reply": "Backend redirect refused: cross-origin credential forwarding is disabled.",
                    "tool_calls": [], "done": True, "error": True,
                }

            # Check if retryable (429 or 5xx)
            _retryable = response.status_code == 429 or response.status_code >= 500
            if _retryable and _attempt < _MAX_RETRIES:
                _delay = _RETRY_BASE * (2 ** _attempt)
                # Honor retry-after header (seconds or HTTP-date)
                _ra = response.headers.get("retry-after")
                if _ra:
                    try:
                        _delay = float(_ra)
                    except ValueError:
                        pass
                _delay = min(_delay, _RETRY_CAP)
                if interrupt_event is not None:
                    if interrupt_event.wait(timeout=_delay):
                        raise InterruptedError("interrupted during retry delay")
                else:
                    time.sleep(_delay)
                continue

            # Non-retryable error — return immediately
            try:
                err_data = response.json()
                return {"reply": f"Server Error: {err_data.get('detail', response.text[:200])}", "command": "", "rules": "", "done": True, "error": True}
            except Exception:
                return {"reply": f"Server Error: HTTP {response.status_code}", "command": "", "rules": "", "done": True, "error": True}

        # Parse SSE stream. Backend pass-through DeepSeek's OpenAI-compatible
        # chunks: each event is `{"choices":[{"delta":{"content":"..."}}]}`.
        # Accumulate deltas into one string, then parse JSON {reply,command,...}.
        accumulated = ""
        billing_info: dict = {}
        streamed_model: str = ""
        got_any_event = False
        finish_reason: Optional[str] = None  # last non-null choices[0].finish_reason
        _diag_events: list = []  # diagnostic: capture non-content fields
        prev_reply_for_chunks = ""
        prev_command_for_chunks = ""
        native_tc_frags: dict = {}  # index -> {id,name,arguments} for native tool_calls
        for line in response.iter_lines(decode_unicode=True):
            # Check for soft-interrupt between SSE chunks
            if interrupt_event is not None and interrupt_event.is_set():
                response.close()
                return {
                    "reply": accumulated or "(interrupted)",
                    "tool_calls": [],
                    "done": True,
                    "error": False,
                    "_interrupted": True,
                }
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                evt = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            got_any_event = True
            if evt.get("error"):
                if accumulated:
                    # Backend post-processing failed but content was already streamed — use it.
                    break
                return {"reply": f"Server Error: {evt['error']}", "tool_calls": [], "done": True, "error": True}
            if "_billing" in evt:
                billing_info = dict(evt["_billing"] or {})
                billing_info["billingDomain"] = backend_profile.kind
                billing_info["official"] = backend_profile.sends_laintas_credentials
                continue
            # Capture the actual model name streamed by the backend (first
            # non-empty occurrence wins) for the status-bar display.
            if not streamed_model:
                _m = evt.get("model")
                if _m and isinstance(_m, str):
                    streamed_model = _m
            # Capture diagnostic info: any top-level keys beyond choices
            for k in evt.keys():
                if k not in ("choices", "id", "object", "created", "model", "system_fingerprint") and k not in _diag_events:
                    _diag_events.append(k)
            _choices = evt.get("choices")
            # Capture the OpenAI-native finish_reason (provider passes it through
            # verbatim — "stop" / "tool_calls" / "length"). Authoritative signal
            # for whether the turn ended vs. expects tool results back.
            if isinstance(_choices, list) and _choices:
                _fr = _choices[0].get("finish_reason")
                if _fr:
                    finish_reason = _fr
            delta_content = (
                _choices[0].get("delta", {}).get("content")
                if isinstance(_choices, list) and _choices
                else None
            )
            # OpenAI-style native tool_calls in delta: reassemble streamed
            # fragments (keyed by index) instead of dropping them.
            _delta = _choices[0].get("delta", {}) if isinstance(_choices, list) and _choices else {}
            if _delta.get("tool_calls"):
                for _frag in _delta["tool_calls"]:
                    if not isinstance(_frag, dict):
                        continue
                    _i = _frag.get("index", 0)
                    _slot = native_tc_frags.setdefault(_i, {"id": "", "name": "", "arguments": ""})
                    if _frag.get("id"):
                        _slot["id"] = _frag["id"]
                    _fn = _frag.get("function") or {}
                    if _fn.get("name"):
                        _slot["name"] = _fn["name"]
                    if _fn.get("arguments"):
                        _slot["arguments"] += _fn["arguments"]
                # Native mode: surface the tool name as a command preview as it
                # streams (un-mangled), so the UI shows what's about to run.
                if on_chunk is not None and native_tc_frags:
                    _first_idx = min(native_tc_frags)
                    _nm = (native_tc_frags[_first_idx].get("name") or "").strip()
                    _nm = tool_name_map.get(_nm, _nm)
                    if _nm:
                        _np = _nm
                        if len(native_tc_frags) > 1:
                            _np = f"{_nm} (+{len(native_tc_frags)-1} more)"
                        if _np != prev_command_for_chunks:
                            try: on_chunk("command", _np)
                            except Exception: pass
                            prev_command_for_chunks = _np
            if delta_content:
                accumulated += delta_content
                if on_chunk is not None:
                    # Content is plain prose — stream it straight through as the
                    # reply. Tool calls arrive natively via delta.tool_calls
                    # (handled above), never by parsing content.
                    try: on_chunk("reply", delta_content)
                    except Exception: pass
                    prev_reply_for_chunks = accumulated.strip()

        if not got_any_event:
            return {"reply": "No response from AI", "tool_calls": [], "done": True, "error": True}

        # Native function-calls emitted out-of-band (delta.tool_calls), if any.
        native_calls = _native_to_tool_calls(native_tc_frags, tool_name_map)

        # Output-truncation signal: the model ran right up against the token
        # ceiling. When a big single-response write (e.g. a whole-file fs.write)
        # exceeds max_tokens, the JSON never closes and parsing fails — but the
        # cause is length, not formatting, so it needs a different nudge.
        # finish_reason == "length" is the provider's own truncation signal.
        _completion_tokens = int((billing_info or {}).get("completionTokens", 0) or 0)
        _max_tokens = int(get_runtime_config("max_tokens") or 0)
        _hit_ceiling = _max_tokens > 0 and _completion_tokens >= _max_tokens * 0.95
        _truncated_turn = _hit_ceiling or finish_reason == "length"

        # ── Local usage accounting (/usage) — every completed call lands here
        # regardless of tool/prose outcome. Backends that send no _billing
        # (external/unmetered) get chars/4 estimates so stats still move.
        if billing_info:
            usage_tracker.record(
                model=selected_model,
                prompt_tokens=(billing_info or {}).get("promptTokens", 0),
                completion_tokens=_completion_tokens,
                cost_cents=(billing_info or {}).get("costCents", 0),
                official=bool(billing_info.get("official")),
                backend_kind=backend_profile.kind,
            )
        elif accumulated:
            usage_tracker.record(
                model=selected_model,
                prompt_tokens=usage_tracker.estimate_tokens(
                    json.dumps(payload, ensure_ascii=False)),
                completion_tokens=usage_tracker.estimate_tokens(accumulated),
                official=False,
                backend_kind=backend_profile.kind,
                estimated=True,
            )

        # Update the REPL status bar with the actual model used this call.
        # Falls back to the user's configured selection if the backend
        # didn't echo a model name in the SSE stream.
        _effective_model = streamed_model or selected_model
        if _effective_model:
            _update_status_cache(model=_effective_model)

        raw_text = accumulated.strip()
        # Content is always prose now (tool calls come natively); surface it.
        prose_reply = raw_text

        # ── 1. NATIVE PATH (primary) ───────────────────────────────────────
        # Model invoked tools through the OpenAI function-calling channel
        # (delta.tool_calls). This is the normal end-to-end mode: the backend
        # passes the provider's native tool_calls straight through.
        if native_calls:
            return {
                "reply": prose_reply,
                "tool_calls": native_calls,
                "finish_reason": finish_reason or "tool_calls",
                "done": False,
                "error": False,
                "_billing": billing_info,
                "_diag_events": _diag_events + ["parsed_native_tool_calls"],
            }

        # ── 2. Tagged-tool-call compat ─────────────────────────────────────
        # Older/regressed models may emit <tool_calls>...</tool_calls> as text
        # instead of using the native channel. Convert so the loop continues.
        tagged_tool_calls = _extract_tagged_tool_calls(accumulated)
        if tagged_tool_calls:
            return {
                "reply": "",
                "tool_calls": tagged_tool_calls,
                "finish_reason": finish_reason or "tool_calls",
                "done": False,
                "error": False,
                "_billing": billing_info,
                "_diag_events": _diag_events + ["parsed_tagged_tool_calls"],
            }

        # ── 3. PROSE FINAL ─────────────────────────────────────────────────
        # No tool calls anywhere: return the provider text verbatim.  Do not
        # fabricate a "(no response)" reply because that converts an empty or
        # malformed provider turn into a successful prose completion.
        return {
            "reply": raw_text,
            "tool_calls": [],
            "finish_reason": finish_reason or "stop",
            "done": False,
            "error": False,
            "_truncated": _truncated_turn,
            "_billing": billing_info,
            "_diag_events": _diag_events,
        }

    except requests.Timeout:
        return {"reply": "Request timed out. Please try again.", "tool_calls": [], "done": True, "error": True}
    except requests.ConnectionError:
        return {"reply": f"Cannot connect to backend ({backend_url}). Check your network.", "tool_calls": [], "done": True, "error": True}
    except InterruptedError:
        # Soft-interrupt from _on_chunk callback during streaming
        _partial = ""
        try:
            _partial = accumulated
        except NameError:
            pass
        return {
            "reply": _partial or "(interrupted)",
            "tool_calls": [],
            "done": True,
            "error": False,
            "_interrupted": True,
        }
    except Exception as e:
        return {"reply": f"Error: {e}", "tool_calls": [], "done": True, "error": True}


# ── Agent Registration with Helpwo Backend ─────────────────────────────

class TerminalSession:
    """A real PTY shell bridged to the browser over a WebSocket relay.

    Two threads share one sync WebSocket (the documented one-reader /
    one-writer pattern): the reader thread pumps PTY output up, the main
    thread pumps browser input + resizes down into the PTY. Frames are JSON
    text; payload bytes are base64 so arbitrary/binary output survives intact:
      host → browser : {"t":"o","d":<b64>}  output    {"t":"exit"}
      browser → host : {"t":"i","d":<b64>}  input     {"t":"resize","cols","rows"}
    """

    def __init__(self, backend_url, agent_id, agent_secret, session_id, cols, rows):
        self.backend_url = backend_url
        self.agent_id = agent_id
        self.agent_secret = agent_secret
        self.session_id = session_id
        self.cols = cols
        self.rows = rows
        self.fd = None
        self.pid = None
        self._ws = None
        self._closed = threading.Event()

    def _ws_url(self):
        base = self.backend_url.replace("https://", "wss://").replace("http://", "ws://")
        return (f"{base}/api/agents/{self.agent_id}/term"
                f"?sessionId={self.session_id}&role=host")

    def _set_winsize(self, cols, rows):
        import fcntl, termios, struct
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name=f"laintas-term-{self.session_id}").start()

    def _run(self):
        import pty
        from websockets.sync.client import connect

        shell = os.environ.get("SHELL") or "/bin/bash"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # Child: become the shell with a sane terminal identity.
            os.environ["TERM"] = "xterm-256color"
            try:
                os.execvp(shell, [shell, "-l"] if os.path.basename(shell) in ("bash", "zsh", "sh") else [shell])
            except Exception:
                os._exit(127)
            return

        # Parent: size the PTY, then connect the relay.
        self._set_winsize(self.cols, self.rows)
        try:
            self._ws = connect(
                self._ws_url(),
                additional_headers={"Authorization": f"Agent {self.agent_secret}"},
                open_timeout=10,
                max_size=64 * 1024,
            )
        except Exception as e:
            console.print(f"[red]Terminal relay connect failed: {e}[/red]")
            self._cleanup()
            return

        threading.Thread(target=self._pump_output, daemon=True,
                         name=f"laintas-term-out-{self.session_id}").start()
        try:
            self._read_input_loop()
        finally:
            self._cleanup()

    def _read_input_loop(self):
        """Main thread: browser → PTY (input + resize)."""
        import base64
        for message in self._ws:  # iterates until the socket closes
            try:
                msg = json.loads(message)
            except (ValueError, TypeError):
                continue
            t = msg.get("t")
            if t == "i":
                try:
                    os.write(self.fd, base64.b64decode(msg.get("d", "")))
                except OSError:
                    break
            elif t == "resize":
                try:
                    self._set_winsize(int(msg["cols"]), int(msg["rows"]))
                except (KeyError, ValueError, TypeError):
                    pass

    def _pump_output(self):
        """Reader thread: PTY → browser. Sole writer of the WebSocket."""
        import base64
        while not self._closed.is_set():
            try:
                # 32 KiB stays below the relay's 64 KiB JSON/base64 frame cap.
                data = os.read(self.fd, 32768)
            except OSError:
                break
            if not data:
                break  # shell exited / PTY closed
            try:
                self._ws.send(json.dumps({"t": "o", "d": base64.b64encode(data).decode("ascii")}))
            except Exception:
                break
        # Tell the browser the shell ended, then close the relay.
        try:
            self._ws.send(json.dumps({"t": "exit"}))
        except Exception:
            pass
        self._cleanup()

    def _cleanup(self):
        if self._closed.is_set():
            return
        self._closed.set()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass


class AgentRegistry:
    """Manages remote agent registration with Helpwo backend."""

    def __init__(self):
        self.agent_id: Optional[str] = None
        self.agent_secret: str = ""
        self.agent_name: str = ""
        # ── Sub-terminal identity (two-end handshake with Helpwo) ────────
        # depth 0 = primary CLI (auto-registers = "online"). depth ≥ 1 = a
        # nested CLI inside a sub-terminal: it registers ONLY when the user
        # runs /connect there (or Helpwo asked for it via term-new), carrying
        # terminal_meta so Helpwo can show the CLI-side name/definition.
        self.depth: int = 0
        self.parent_remote_id: Optional[str] = None
        self.terminal_meta: Optional[dict] = None
        # REPL state callbacks, stashed by main() so /connect can start the
        # message poll outside main's scope.
        self._state_cb = None
        self._chat_cb = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._message_poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._session: Optional[dict] = None
        self._processing_message = threading.Event()
        self._pending_responses: list = []  # thread-safe queue for responses to send
        self._registration_lock = threading.Lock()
        self._last_reregister = 0.0

        # ── Async event bus (Phase 1) ───────────────────────────────────
        # _event_q holds batches of events (each item is a list[dict]).
        # _sender_thread coalesces batches into single POSTs in the background
        # so the REPL/handlers never block on HTTP. _sender_stop tells it to
        # finish draining and exit.
        self._event_q: "queue.Queue[list]" = queue.Queue(maxsize=10000)
        self._sender_thread: Optional[threading.Thread] = None
        self._sender_stop = threading.Event()

        # ── Active request tracking (Phase A1) ───────────────────────────
        # Maps reqId → threading.Event for abort signalling.
        self._active_requests: dict[str, threading.Event] = {}
        # Maps reqId → threading.Event + decision for approval flow.
        self._pending_approvals: dict[str, tuple[threading.Event, dict]] = {}
        self._active_req_lock = threading.RLock()

        # ── WebRTC peer-to-peer file channel (lazy) ─────────────────────
        self._webrtc = None  # WebrtcManager | False(unavailable) | None(not yet)

    def _ensure_webrtc(self):
        """Lazily create the WebRTC manager. Returns it, or None if aiortc is
        unavailable (file ops then fall back to the relay path)."""
        if self._webrtc is None:
            try:
                from webrtc_channel import WebrtcManager
                if not WebrtcManager.available():
                    console.print(f"[dim]WebRTC disabled (aiortc not importable: "
                                  f"{WebrtcManager.import_error()})[/dim]")
                    self._webrtc = False
                else:
                    self._webrtc = WebrtcManager(
                        lambda sid, typ, meta: self._push(sid, typ, "", meta),
                    )
            except Exception as e:
                console.print(f"[dim]WebRTC unavailable: {e}[/dim]")
                self._webrtc = False
        return self._webrtc or None

    def _agent_auth_headers(self) -> dict:
        """Bearer-style CLI credential; never place this secret in a URL."""
        return {"Authorization": f"Agent {self.agent_secret}"}

    def _recover_registration(self) -> None:
        """Re-register after a server-side credential rotation or state loss."""
        if ((get_backend_profile().sends_laintas_credentials and not self._session)
                or time.time() - self._last_reregister < 10):
            return
        if not self._registration_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self._last_reregister < 10:
                return
            self._last_reregister = time.time()
            if self.register(self._session, self.agent_name or None, quiet=True):
                console.print("[green]Remote agent credentials refreshed.[/green]")
        finally:
            self._registration_lock.release()

    def register(self, session: dict, name: str = None, quiet: bool = False) -> bool:
        """Register this CLI as a remote agent with Helpwo backend."""
        self._session = session
        hostname = socket.gethostname()
        cwd = os.getcwd()

        config = load_config()
        if name:
            self.agent_name = name
        else:
            self.agent_name = config.get("agentName", hostname)

        user_email = session.get("userEmail", "")
        user_name = session.get("userName", "")

        profile = get_backend_profile()
        payload = {
            "name": self.agent_name,
            "hostname": hostname,
            "os": SYSTEM,
            "shell": SHELL_NAME,
            "cwd": cwd,
            "goal": f"CLI agent '{self.agent_name}' on {hostname} ({SYSTEM})",
        }
        if profile.sends_laintas_credentials:
            payload["userEmail"] = user_email
            payload["userName"] = user_name
        if self.parent_remote_id:
            payload["parentId"] = self.parent_remote_id
        if self.terminal_meta:
            payload["terminal"] = self.terminal_meta
            payload["goal"] = (f"Sub-terminal '{self.terminal_meta.get('name', '')}'"
                               f" on {hostname}")

        backend_url = profile.base_url
        headers, cookies = backend_profiles.request_auth(profile, session)

        try:
            resp = requests.post(
                f"{backend_url}/api/agents/register",
                json=payload,
                headers=headers,
                cookies=cookies,
                timeout=5,
                allow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.agent_id = data.get("agentId", "")
                self.agent_secret = data.get("agentSecret", "")
                if not quiet:
                    console.print(Panel(
                        f"[green]Agent linked to Helpwo AGENTS[/green]\n"
                        f"Name: [bold]{self.agent_name}[/bold]\n"
                        f"ID: {self.agent_id}\n"
                        f"Backend: {backend_url}",
                        title="Agent Registered",
                        border_style="green",
                    ))
                return True
            else:
                if not quiet:
                    console.print(Panel(
                        f"Backend: {backend_url}\n"
                        f"Response: HTTP {resp.status_code}\n\n"
                        f"[dim]Agent won't appear in Helpwo AGENTS panel. You can still use all features.[/dim]",
                        title="[yellow]Agent Not Linked[/yellow]",
                        border_style="yellow",
                    ))
                return False
        except requests.RequestException as e:
            if not quiet:
                console.print(Panel(
                    f"Backend: {backend_url}\n"
                    f"Error: {e}\n\n"
                    f"[dim]Is the Helpwo backend running? Agent won't appear in Helpwo AGENTS panel.[/dim]\n"
                    f"[dim]You can still use all features normally.[/dim]",
                    title="[yellow]Agent Not Linked[/yellow]",
                    border_style="yellow",
                ))
            return False

    def start_heartbeat(self):
        """Start heartbeat thread."""
        if not self.agent_id:
            return
        self._running = True
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
        # Start the async event sender alongside heartbeat — _push_events
        # becomes non-blocking only once this thread is alive.
        self._start_event_sender()

    def _start_event_sender(self):
        """Start the background event-sender thread if not already running."""
        if self._sender_thread is not None and self._sender_thread.is_alive():
            return
        self._sender_stop.clear()
        self._sender_thread = threading.Thread(
            target=self._event_sender_loop, daemon=True,
            name="laintas-event-sender",
        )
        self._sender_thread.start()

    def _event_sender_loop(self):
        """Drain _event_q, coalesce up to 50 batches within 200ms, POST once.

        Runs until _sender_stop is set AND the queue is empty, so a graceful
        shutdown can request the loop drain remaining events before exiting.
        """
        while True:
            try:
                first = self._event_q.get(timeout=0.5)
            except queue.Empty:
                if self._sender_stop.is_set():
                    return
                continue

            batch = list(first)
            deadline = time.time() + 0.1
            while len(batch) < 50:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    more = self._event_q.get(timeout=remaining)
                    batch.extend(more)
                except queue.Empty:
                    break

            try:
                self._do_post_events(batch)
            except Exception:
                # Never let a POST exception kill the sender — that would
                # silently break every subsequent _push_events call.
                pass

    def _do_post_events(self, events: list):
        """Synchronous POST. Called only from the sender thread."""
        if not self.agent_id or not events:
            return
        backend_url = get_backend_url()
        headers = self._agent_auth_headers()
        try:
            requests.post(
                f"{backend_url}/api/agents/{self.agent_id}/events",
                json={
                    "events": events,
                    "state": {"cwd": os.getcwd(), "status": "running"},
                },
                headers=headers,
                timeout=5,
                allow_redirects=False,
            )
        except requests.RequestException:
            pass

    def _flush_events(self, timeout: float = 2.0):
        """Block until the sender drains _event_q, capped at timeout seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._event_q.empty():
                return
            time.sleep(0.05)

    def _heartbeat_loop(self):
        """Send heartbeat every HEARTBEAT_INTERVAL seconds with extended state."""
        backend_url = get_backend_url()

        # Try to import psutil for metrics (optional dependency)
        try:
            import psutil as _psutil_mod
            _has_psutil = True
        except ImportError:
            _has_psutil = False

        while self._running and self.agent_id:
            try:
                payload = {
                    "agentId": self.agent_id,
                    "cwd": os.getcwd(),
                    "shell": SHELL_NAME,
                }

                # Running terminals snapshot
                terminals = get_all_terminals()
                running_terms = []
                for t in terminals:
                    running_terms.append({
                        "name": t.name,
                        "alive": bool(t.session.is_alive()) if t.session else False,
                        "cmd": t.command[:120],
                    })
                payload["runningTerminals"] = running_terms

                # System metrics
                if _has_psutil:
                    try:
                        load = _psutil_mod.getloadavg()
                        mem = _psutil_mod.virtual_memory()
                        disk = _psutil_mod.disk_usage(os.getcwd())
                        payload["metrics"] = {
                            "loadAvg": [round(load[0], 2), round(load[1], 2), round(load[2], 2)],
                            "memFreeMB": round(mem.available / (1024 * 1024), 1),
                            "memTotalMB": round(mem.total / (1024 * 1024), 1),
                            "diskFreeGB": round(disk.free / (1024**3), 1),
                        }
                    except Exception:
                        pass

                response = requests.post(
                    f"{backend_url}/api/agents/heartbeat",
                    json=payload,
                    headers=self._agent_auth_headers(),
                    timeout=5,
                    allow_redirects=False,
                )
                if response.status_code in (403, 404):
                    self._recover_registration()
            except requests.RequestException:
                pass  # heartbeat failures are silent

            time.sleep(float(get_runtime_config("heartbeat_interval")))

    def start_message_poll(self, agent_state_cb, chat_history_cb):
        """Start background thread to poll for incoming messages from Helpwo UI.

        Args:
            agent_state_cb: callable() → dict — returns current agent state
            chat_history_cb: callable() → list — returns current chat history
        """
        if (not self.agent_id or
                (get_backend_profile().sends_laintas_credentials and not self._session)):
            return
        # Reconnect (/connect <new-name>, /name) must not stack a second poll
        # thread — the existing loop re-reads self.agent_id each iteration.
        if self._message_poll_thread is not None and self._message_poll_thread.is_alive():
            return
        self._message_poll_thread = threading.Thread(
            target=self._poll_loop,
            args=(agent_state_cb, chat_history_cb),
            daemon=True,
        )
        self._message_poll_thread.start()
        console.print("[dim]Listening for remote messages from Helpwo...[/dim]")

    def _poll_loop(self, agent_state_cb, chat_history_cb):
        """Poll backend for incoming messages every 2 seconds."""
        backend_url = get_backend_url()

        while self._running and self.agent_id:
            try:
                resp = requests.get(
                    f"{backend_url}/api/agents/{self.agent_id}/poll",
                    headers=self._agent_auth_headers(),
                    timeout=5,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    messages = data.get("inputs", [])
                    for msg in messages:
                        self._handle_remote_message(
                            msg, agent_state_cb, chat_history_cb,
                        )
                elif resp.status_code in (403, 404):
                    self._recover_registration()
            except requests.RequestException:
                pass  # poll failures are silent, retry next cycle

            time.sleep(2)

    def _handle_remote_message(self, msg: dict, agent_state_cb, chat_history_cb):
        """Dispatch an incoming message by 'kind' per HelpwoAI protocol.

        Backwards-compatible: if a message has no 'kind' field, it is treated
        as a legacy chat message (content → run_agent_loop / system command).
        Exactly one 'final' event must be pushed per reqId — handlers below
        enforce this; the catch-all also pushes a fail-final on exception.
        """
        self._processing_message.set()
        req_id = msg.get("reqId") or msg.get("id")
        try:
            kind = msg.get("kind")
            payload = msg.get("payload") or {}

            if not kind:
                # Legacy chat message: {id, content, timestamp}
                kind = "chat"
                payload = {"message": msg.get("content", "")}

            if kind == "exec":
                self._handle_exec(req_id, payload)
            elif kind == "query":
                self._handle_query(req_id, payload)
            elif kind == "chat":
                self._handle_chat(req_id, payload, agent_state_cb, chat_history_cb)
            elif kind == "delegate":
                self._handle_delegate(req_id, payload, agent_state_cb, chat_history_cb)
            elif kind == "term-open":
                self._handle_term_open(req_id, payload)
            elif kind == "term-new":
                self._handle_term_new(req_id, payload)
            elif kind == "term-close":
                self._handle_term_close(req_id, payload)
            elif kind == "abort":
                self._handle_abort(req_id, payload)
            elif kind == "approval-response":
                self._handle_approval_response(req_id, payload)
            elif kind == "rtc-offer":
                self._handle_rtc_offer(req_id, payload)
            elif kind == "rtc-ice":
                # Non-trickle in Layer 1: candidates are embedded in the SDP,
                # so standalone ICE messages are ignored for now.
                pass
            elif kind == "rtc-close":
                mgr = self._webrtc or None
                if mgr:
                    mgr.handle_close(req_id)
            else:
                self._push_final(req_id, "fail", f"unknown kind '{kind}'")
        except Exception as e:
            console.print(f"[red]Error handling remote message: {e}[/red]")
            if req_id:
                self._push_final(req_id, "fail", f"handler exception: {e}")
        finally:
            self._processing_message.clear()

    def _handle_rtc_offer(self, req_id: str, payload: dict):
        """Accept a WebRTC offer from the browser and answer it, establishing a
        peer-to-peer DataChannel so file transfers bypass the relay server.
        Does NOT push a 'final' — the answer/error is delivered as its own event
        (rtc-answer / rtc-error / rtc-unavailable) keyed to this reqId."""
        mgr = self._ensure_webrtc()
        if mgr is None:
            self._push(req_id, "rtc-unavailable", "", {"reason": "aiortc not installed on host"})
            return
        sdp = payload.get("sdp")
        if not sdp:
            self._push(req_id, "rtc-error", "", {"error": "missing sdp"})
            return
        mgr.handle_offer(req_id, sdp)

    def _handle_chat(self, req_id: str, payload: dict, agent_state_cb, chat_history_cb):
        """Inject a remote chat message into the main REPL loop.

        The message goes through the exact same input→route→execute pipeline as
        local user input. We block the poll thread until the main loop finishes
        processing, then push the terminal 'final' event.
        """
        content = payload.get("message", "")

        console.print(Panel(
            f"[bold cyan]Remote message from Helpwo:[/bold cyan]\n{content}",
            title="Incoming",
            border_style="cyan",
        ))

        done = threading.Event()
        _inject_input(content, done)

        if not done.wait(timeout=120):
            self._push_final(req_id, "fail", "processing timeout — main loop busy")
            return

        state = agent_state_cb() if callable(agent_state_cb) else {}
        summary = state.get("lastReply", "") or state.get("lastOutput", "") or "done"
        self._push_final(req_id, "success", summary[:2000])

    def _handle_exec(self, req_id: str, payload: dict):
        """Run a shell command in a PTY and stream stdout under req_id.

        Pushes cmd-start → stdout chunks → cmd-end → final. PTY merges
        stderr into stdout; splitting is deferred to a later phase.
        """
        cmd = payload.get("command")
        cwd = payload.get("cwd") or os.getcwd()
        try:
            timeout = int(payload.get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        if not cmd:
            self._push_final(req_id, "fail", "missing 'command' in payload")
            return

        # ── Security policy check ──────────────────────────────────────
        import policy as _policy
        decision = _policy.evaluate(cmd, cwd, req_id=req_id,
                                    agent_id=self.agent_id)
        if decision.action == "deny":
            self._push_final(req_id, "fail", f"Blocked by policy: {decision.reason}")
            console.print(f"[red]BLOCKED remote exec: {cmd[:100]} — {decision.reason}[/red]")
            return
        if (decision.action == "needs_approval"
                or not get_runtime_config("allow_remote_exec_without_approval")):
            approval = self._request_approval(req_id, cmd, cwd, timeout=300)
            if approval != "approve":
                self._push_final(req_id, "aborted",
                                 f"User {approval}: {cmd[:100]}")
                return

        console.print(Panel(
            f"[bold yellow]remote_exec[/bold yellow] {cmd}\n[dim]cwd={cwd} timeout={timeout}s[/dim]",
            title=f"Incoming exec ({req_id})",
            border_style="yellow",
        ))

        self._push(req_id, "cmd-start", "", {"command": cmd, "cwd": cwd})
        sess = InteractiveSession(cmd, timeout=timeout)
        old_cwd = os.getcwd()
        start = time.time()
        try:
            try:
                os.chdir(cwd)
            except OSError as e:
                self._push_final(req_id, "fail", f"cd {cwd} failed: {e}")
                return
            try:
                sess.start()
            finally:
                try:
                    os.chdir(old_cwd)
                except OSError:
                    pass

            # Line-buffered streaming: hold a partial-line buffer between
            # PTY reads so the UI never displays a half-formed line.
            # Backend can still see real-time progress because we flush on
            # every '\n'. Any final partial line is flushed after cmd-end.
            line_buf = ""
            while sess.is_alive():
                chunk = sess.read_output(timeout=0.2)
                if chunk:
                    line_buf += chunk
                    if "\n" in line_buf:
                        complete, line_buf = line_buf.rsplit("\n", 1)
                        self._push(req_id, "stdout", complete + "\n")
                if time.time() - start > timeout:
                    sess.close()
                    if line_buf:
                        self._push(req_id, "stdout", line_buf)
                        line_buf = ""
                    self._push(req_id, "cmd-end", "", {
                        "exitCode": -1,
                        "durationMs": int((time.time() - start) * 1000),
                    })
                    self._push_final(req_id, "fail", f"timeout after {timeout}s")
                    return

            # Drain any output buffered after process exit.
            chunk = sess.read_output(timeout=0.2)
            if chunk:
                line_buf += chunk
            if line_buf:
                # Final flush — may not end in newline (e.g. trailing prompt).
                self._push(req_id, "stdout", line_buf)
            sess.close()
            rc = sess._returncode
            duration_ms = int((time.time() - start) * 1000)
            self._push(req_id, "cmd-end", "", {
                "exitCode": rc, "durationMs": duration_ms,
            })
            status = "success" if rc == 0 else "fail"
            self._push_final(req_id, status, f"exit={rc}")
        except Exception as e:
            try:
                sess.close()
            except Exception:
                pass
            self._push_final(req_id, "fail", f"exec error: {e}")

    def _handle_query(self, req_id: str, payload: dict):
        """Read-only reconnaissance. Returns one final with data in meta."""
        what = payload.get("what")
        target = payload.get("target")
        try:
            if what == "cwd":
                data = os.getcwd()
            elif what == "files":
                data = os.listdir(os.getcwd())
            elif what == "env":
                # Never export the process environment wholesale: it commonly
                # contains API keys, session tokens and cloud credentials.
                safe_names = ("LANG", "LC_ALL", "SHELL", "TERM", "COLORTERM")
                data = {name: os.environ[name] for name in safe_names if name in os.environ}
            elif what == "processes":
                # Stub: psutil dependency deferred to Phase B1.
                data = []
            elif what == "term-snapshot":
                term = get_terminal(target) if target else None
                if term is None:
                    data = None
                else:
                    n = int(get_runtime_config("terminal_tail_lines") or 20)
                    output = getattr(term.session, "full_output", "") or ""
                    data = "\n".join(output.split("\n")[-n:])
            else:
                self._push_final(req_id, "fail", f"unknown query.what={what!r}")
                return
            self._push_final(
                req_id, "success", f"query {what}",
                meta={"what": what, "data": data},
            )
        except Exception as e:
            self._push_final(req_id, "fail", f"query error: {e}")

    def _handle_term_open(self, req_id: str, payload: dict):
        """Spawn a real PTY shell and dial into the backend terminal relay.

        Gives the Helpwo browser a live shell (vim/htop/colors/Ctrl-C all
        work) — same trust level as remote_exec, which already runs arbitrary
        commands. It is disabled by default and can only be enabled in the
        CLI's local runtime configuration.
        """
        if IS_WINDOWS:
            return  # PTY relay is Unix-only for now
        if get_runtime_config("disable_remote_terminal"):
            console.print("[yellow]Remote terminal request ignored (disable_remote_terminal is set).[/yellow]")
            return
        session_id = (payload.get("sessionId") or "").strip()
        if not session_id:
            return
        try:
            cols = max(1, int(payload.get("cols") or 80))
            rows = max(1, int(payload.get("rows") or 24))
        except (TypeError, ValueError):
            cols, rows = 80, 24

        backend_url = get_backend_url()
        console.print(Panel(
            f"[bold cyan]Browser opened a terminal[/bold cyan]\n[dim]session {session_id} · {cols}×{rows}[/dim]",
            title="Remote Terminal", border_style="cyan",
        ))
        try:
            term = TerminalSession(backend_url, self.agent_id, self.agent_secret,
                                   session_id, cols, rows)
            term.start()
        except Exception as e:
            console.print(f"[red]Failed to open remote terminal: {e}[/red]")

    def _handle_term_new(self, req_id: str, payload: dict):
        """Helpwo's 添加终端 → create a named sub-terminal here (same path as
        /term <name>) running a nested laintas_cli that auto-/connects, so it
        registers itself back to Helpwo as a managed terminal."""
        if IS_WINDOWS:
            self._push_final(req_id, "fail", "sub-terminals are unavailable on Windows")
            return
        if self.depth > 0:
            self._push_final(req_id, "fail", "term-new must target the primary CLI (depth 0)")
            return
        raw = (payload.get("name") or "").strip()
        if raw and (not re.fullmatch(r"[A-Za-z0-9._-]+", raw) or raw.lower() == "term0"):
            self._push_final(req_id, "fail",
                             "invalid terminal name (letters, numbers, dot, underscore, hyphen; term0 reserved)")
            return
        name = raw
        if not name:
            i = 1
            while get_terminal(f"helpwo{i}") is not None:
                i += 1
            name = f"helpwo{i}"
        existing = get_terminal(name)
        if existing and existing.session and not existing.session.is_alive():
            unregister_terminal(name)
            existing = None
        if existing is not None:
            self._push_final(req_id, "fail", f"terminal '{name}' already exists")
            return
        lain_cmd = _build_connected_subterminal_cmd(name, self.agent_id, auto_connect=True)
        sub = SubTerminalSession(lain_cmd)
        sub.start()
        time.sleep(0.1)
        if not sub.is_alive():
            self._push_final(req_id, "fail", f"could not start terminal '{name}'")
            return
        sub.read_output(timeout=0.1)
        register_terminal(sub, "laintas-cli", 0, name=name)
        console.print(Panel(
            f"[bold cyan]Helpwo created sub-terminal [bold]{name}[/bold][/bold cyan]\n"
            f"[dim]It will hand itself over to Helpwo once it finishes starting.[/dim]",
            title="Remote Terminal", border_style="cyan",
        ))
        self._push_final(req_id, "success", name)

    def _handle_term_close(self, req_id: str, payload: dict):
        """Helpwo's 关闭终端 → close a sub-terminal.

        Sent to the primary CLI with a name: closes that registered
        sub-terminal (kills the nested CLI with it). Sent to a sub-terminal
        CLI itself (no name / own name): the nested CLI shuts down gracefully,
        which closes its terminal window.
        """
        name = (payload.get("name") or "").strip()
        own_name = (self.terminal_meta or {}).get("name") if self.terminal_meta else None
        if self.depth > 0 and (not name or name == own_name):
            self._push_final(req_id, "success", own_name or "closing")
            self._flush_events(timeout=2.0)
            console.print("[yellow]Helpwo closed this sub-terminal.[/yellow]")
            def _die():
                time.sleep(0.3)
                try:
                    self.unregister()
                except Exception:
                    pass
                os._exit(0)
            threading.Thread(target=_die, daemon=True).start()
            return
        if not name:
            self._push_final(req_id, "fail", "missing 'name' in term-close payload")
            return
        if name.lower() == "term0":
            self._push_final(req_id, "fail", "term0 (primary) cannot be closed remotely")
            return
        info = get_terminal(name)
        if info is None:
            self._push_final(req_id, "fail", f"no sub-terminal named '{name}'")
            return
        # PTY-backed nested CLIs can survive a polite close when their stdin
        # EOF handling wedges — grab the child pid first and force-kill after.
        child_pid = None
        try:
            child_pid = getattr(getattr(info.session, "_pty", None), "pid", None)
        except Exception:
            pass
        unregister_terminal(name)
        if child_pid and child_pid > 0:
            def _reap(pid=child_pid):
                time.sleep(1.0)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            threading.Thread(target=_reap, daemon=True).start()
        console.print(Panel(
            f"[yellow]Helpwo closed sub-terminal [bold]{name}[/bold][/yellow]",
            title="Remote Terminal", border_style="yellow",
        ))
        self._push_final(req_id, "success", name)

    def _handle_delegate(self, req_id: str, payload: dict,
                         agent_state_cb, chat_history_cb):
        """Launch a local agent loop for a delegated task.

        Reuses run_agent_loop with events_cb wired to push ai-reply/ai-command
        events under reqId. On completion, pushes final.
        """
        goal = payload.get("goal", "").strip()
        if not goal:
            self._push_final(req_id, "fail", "missing 'goal' in delegate payload")
            return

        if not get_runtime_config("allow_remote_exec_without_approval"):
            approval = self._request_approval(
                req_id, f"DELEGATE: {goal[:500]}", os.getcwd(), timeout=300,
            )
            if approval != "approve":
                self._push_final(req_id, "aborted", f"User {approval}: delegation")
                return

        max_loops_val = int(payload.get("maxLoops", get_runtime_config("max_loops")))
        max_loops_val = max(1, min(max_loops_val, 20))

        # Context briefing from the caller (HelpwoAI conversation summary).
        # Injected ahead of the goal so the delegated loop knows WHY it was
        # asked, while the goal stays the authoritative task.
        context = str(payload.get("context") or "").strip()
        loop_input = goal
        if context:
            loop_input = (
                "[BRIEFING FROM HELPWO — background from the caller's conversation]\n"
                + context[:6000]
                + "\n[END BRIEFING] The briefing is background only. "
                  "Your task is ONLY the goal below.\n\n"
                + goal
            )

        console.print(Panel(
            f"[bold magenta]delegate[/bold magenta] {goal[:200]}\n"
            f"[dim]maxLoops={max_loops_val}"
            + (f", context={len(context)} chars" if context else "")
            + "[/dim]",
            title=f"Incoming delegate ({req_id})",
            border_style="magenta",
        ))

        # Track this request for abort support
        abort_ev = threading.Event()
        with self._active_req_lock:
            self._active_requests[req_id] = abort_ev

        # Build deps for agent_loop
        deps = LoopDeps(
            read_file=read_file, append_file=append_file,
            write_file=write_file, strip_ansi=strip_ansi,
            generate_prompt=generate_cli_prop_template,
            call_backend=lambda **kw: call_backend_stream(**kw),
            SubTerminalSession=SubTerminalSession,
            display_command_output=display_command_output,
            display_sub_terminal_preview=display_sub_terminal_preview,
            display_file_diff=display_file_diff,
            console=console, Markdown=Markdown,
            pty_passthrough=pty_passthrough,
            request_command_approval=lambda cmd, reason: self._request_approval(
                req_id, cmd, os.getcwd()) == "approve",
            request_file_write_approval=lambda path, diff, reason: self._request_approval(
                req_id, f"WRITE {path} — {reason}", os.getcwd()) == "approve",
            request_file_delete_approval=lambda path, preview, reason: self._request_approval(
                req_id, f"DELETE {path} — {reason}\n{preview}", os.getcwd()) == "approve",
        )

        session = self._session or {}
        # Isolation: a delegated loop should see only its goal + the caller's
        # briefing — NOT whatever conversation happens to sit in this CLI's
        # local REPL. Mirrors how local Helpwo sub-agents are isolated from
        # their parent's history. (chat kind still uses the local history.)
        chat_history = []

        def _delegate_events(events):
            """Push loop events to backend with reqId."""
            if abort_ev.is_set():
                raise InterruptedError("delegate aborted")
            for ev in events:
                ev["reqId"] = req_id
            self._push_events(events, req_id=req_id)

        try:
            # Run in a thread so we can monitor abort
            result = {"success": False, "msg": "", "state": {}}
            run_done = threading.Event()

            def _run():
                nonlocal result
                try:
                    result = run_agent_loop(
                        deps=deps, original_input=loop_input,
                        session=session, state={},
                        chat_history=chat_history,
                        events_cb=_delegate_events,
                        depth=0,
                        interrupt_event=abort_ev,
                        max_loops_override=max_loops_val,
                    )
                except InterruptedError:
                    result = {"success": False, "msg": "aborted", "state": {}}
                except Exception as e:
                    result = {"success": False, "msg": str(e), "state": {}}
                finally:
                    run_done.set()

            t = threading.Thread(target=_run, daemon=True,
                                 name=f"laintas-delegate-{req_id}")
            t.start()

            # Wait for completion or abort
            while not run_done.wait(timeout=0.5):
                if abort_ev.is_set():
                    self._push_final(req_id, "aborted", "delegate aborted by remote")
                    return

            reply = result.get("msg", "") or (
                result.get("state", {}).get("lastReply", "") if isinstance(result, dict) else ""
            )
            status = "success" if result.get("success") else "fail"
            self._push_final(req_id, status, reply[:2000])

        except Exception as e:
            self._push_final(req_id, "fail", f"delegate error: {e}")
        finally:
            with self._active_req_lock:
                self._active_requests.pop(req_id, None)

    def _handle_abort(self, req_id: str, payload: dict):
        """Abort a running request identified by targetReqId.

        Sets the abort event for the target request; handlers check it at
        each loop/poll iteration boundary.
        """
        target = payload.get("targetReqId", "")
        reason = payload.get("reason", "")
        if not target:
            self._push_final(req_id, "fail", "missing 'targetReqId' in abort payload")
            return

        console.print(f"[dim yellow]Abort request for {target}: {reason or '(no reason)'}[/dim yellow]")

        with self._active_req_lock:
            abort_ev = self._active_requests.get(target)

        if abort_ev:
            abort_ev.set()
            self._push_final(req_id, "success", f"abort signal sent to {target}")
        else:
            # The request may have already completed or doesn't support abort
            self._push_final(req_id, "fail", f"request '{target}' not found or not abortable")

    def _handle_approval_response(self, req_id: str, payload: dict):
        """Handle user's response to a needs-approval event.

        Sets the decision on the pending approval, unblocking the waiting handler.
        """
        target = payload.get("targetReqId", "")
        decision = payload.get("decision", "reject")  # approve | reject | modify
        feedback = payload.get("feedback", "")

        if not target:
            self._push_final(req_id, "fail", "missing 'targetReqId' in approval-response")
            return

        with self._active_req_lock:
            entry = self._pending_approvals.get(target)

        if entry is None:
            self._push_final(req_id, "fail", f"no pending approval for '{target}'")
            return

        approval_ev, response_dict = entry
        response_dict["decision"] = decision
        response_dict["feedback"] = feedback
        approval_ev.set()

        console.print(
            f"[dim green]Approval response for {target}: "
            f"{decision}" + (f" — {feedback}" if feedback else "") + "[/dim green]"
        )
        self._push_final(req_id, "success", f"approval {decision} applied to {target}")

    def _request_approval(self, req_id: str, command: str, cwd: str,
                          timeout: float = 300.0) -> str:
        """Push needs-approval event and block until user responds.

        Returns "approve", "reject", or "modify". Timeout defaults to 5 min.
        """
        approval_ev = threading.Event()
        response_dict: dict = {}

        with self._active_req_lock:
            self._pending_approvals[req_id] = (approval_ev, response_dict)

        self._push_events(
            [{
                "type": "needs-approval",
                "content": f"Approve execution of: {command}",
                "meta": {
                    "summary": f"Execute: {command[:200]}",
                    "command": command,
                    "cwd": cwd,
                    "targetReqId": req_id,
                },
            }],
            req_id=req_id,
        )

        approved = approval_ev.wait(timeout=timeout)

        with self._active_req_lock:
            self._pending_approvals.pop(req_id, None)

        if not approved:
            return "reject"  # timeout = reject
        return response_dict.get("decision", "reject")


    def _push_events(self, events: list, req_id: str = None):
        """Enqueue events for the background sender to POST.

        Non-blocking: returns immediately after enqueueing. The background
        _event_sender_loop coalesces batches and does the actual HTTP. If
        the queue is full (>10000 batches buffered), drop the oldest batch.

        If req_id is given, every event missing 'reqId' gets it injected,
        and every event missing 'meta' gets an empty dict. Events that
        already carry their own reqId/meta are left alone.
        """
        if not self.agent_id or not events:
            return
        if req_id:
            for ev in events:
                ev.setdefault("reqId", req_id)
                ev.setdefault("meta", {})
        try:
            self._event_q.put_nowait(events)
        except queue.Full:
            # Drop oldest to make room — recovering connectivity matters more
            # than preserving stale telemetry.
            try:
                self._event_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._event_q.put_nowait(events)
            except queue.Full:
                pass

    # ── Per-request push helpers (HelpwoAI protocol) ─────────────────────
    def _push(self, req_id: str, type_: str, content: str = "", meta: dict = None):
        """Push a single typed event under a request id."""
        self._push_events(
            [{"type": type_, "content": content, "meta": meta or {}}],
            req_id=req_id,
        )

    def _push_final(self, req_id: str, status: str, summary: str,
                    artifacts: list = None, meta: dict = None):
        """Push the terminal 'final' event for a request. Exactly one per reqId."""
        final_meta = dict(meta or {})
        final_meta["status"] = status
        final_meta["summary"] = summary
        if artifacts:
            final_meta["artifacts"] = artifacts
        self._push_events(
            [{"type": "final", "content": summary, "meta": final_meta}],
            req_id=req_id,
        )

    def unregister(self):
        """Unregister agent on exit. Drains the event queue first so in-flight
        finals/outputs make it to the backend before the process exits.
        """
        self._running = False

        # Flush queued events (≤2s) then stop the sender thread so the
        # final unregister POST is the last thing we do.
        if self._sender_thread is not None and self._sender_thread.is_alive():
            self._flush_events(timeout=2.0)
            self._sender_stop.set()
            try:
                self._sender_thread.join(timeout=1.0)
            except RuntimeError:
                pass

        if not self.agent_id:
            return

        backend_url = get_backend_url()
        headers = self._agent_auth_headers()

        try:
            requests.post(
                f"{backend_url}/api/agents/unregister",
                json={"agentId": self.agent_id},
                headers=headers,
                timeout=5,
                allow_redirects=False,
            )
        except requests.RequestException:
            pass
        self.agent_id = None
        self.agent_secret = ""


# ── Debug Display ───────────────────────────────────────────────────────

def show_debug_browser_interactive() -> None:
    """Interactive debug browser — arrow keys to select, Enter to view detail, q/Esc to exit."""
    while True:
        entries = get_debug_logs()[:20]
        if not entries:
            console.print("[dim]No debug entries yet. Run an AI command to see data.[/dim]")
            return

        labels = []
        for i, e in enumerate(entries):
            ts = e.timestamp[-19:] if len(e.timestamp) > 19 else e.timestamp
            if e.request_body:
                tag = "AI "
                summary = e.reply[:60] or e.command[:60] or "(no response)"
            else:
                tag = "CMD"
                summary = e.exec_command[:60] or "(no output)"
            summary = summary.replace("\n", " ")[:60]
            labels.append(f"#{i+1:2d}  {ts}  [{tag}]  {summary}")

        chosen = select_dialog(
            labels,
            title="Debug Log — Last 20 Entries",
            full_screen=True,
            hint="↑↓ navigate  ↵ view detail  q/Esc back",
        )
        if chosen is None:
            return
        idx = labels.index(chosen)
        show_debug_detail(idx)
        input("\n[dim]Press Enter to return to debug browser...[/dim]")


def show_debug_detail(index: int) -> None:
    """Show full detail for a single debug entry by index."""
    if index < 0 or index >= len(get_debug_logs()):
        console.print(f"[red]Entry #{index + 1} not found.[/red]")
        return

    e = get_debug_logs()[index]
    ts = e.timestamp

    # Build a multi-panel detail view
    header = (
        f"[bold cyan]#{e.loop}[/bold cyan] — {ts}   "
        f"Path: [dim]{e.current_path}[/dim]"
    )
    console.print(Panel(header, title=f"Debug Entry #{index + 1}"))

    # User input
    if e.user_input:
        console.print(Panel(e.user_input[:3000], title="User Input"))

    # Request body (AI entries only)
    if e.request_body:
        rb = e.request_body
        sections = []
        sections.append(f"[bold]Message:[/bold]\n{rb.get('message', '')[:2000]}")
        ctx = e.context_sizes
        sections.append(
            f"[bold]Context sizes:[/bold] "
            f"terminal={ctx.get('terminal', 0)} chars, "
            f"conversation={ctx.get('conversation', 0)} chars, "
            f"memory={ctx.get('memory', 0)} chars, "
            f"terminals={ctx.get('terminals', 0)} chars, "
            f"prompt={ctx.get('prompt', 0)} chars"
        )
        sections.append(f"[bold]Prompt Preview (first 500 chars):[/bold]\n{rb.get('promptPreview', '')[:500]}")
        console.print(Panel("\n\n".join(sections), title="Request Payload"))

    # AI response (AI entries only)
    if e.request_body:
        lines = []
        if e.reply:
            lines.append(f"[bold]Reply:[/bold]\n{e.reply[:1500]}")
        if e.command:
            lines.append(f"[bold]Command:[/bold] {e.command}")
        lines.append(f"[bold]Done:[/bold] {e.done}")
        if e.billing:
            cost = e.billing.get("costCents") or 0
            balance = e.billing.get("balanceCents") or 0
            lines.append(f"[bold]Billing:[/bold] ${cost / 100:.2f} (balance ${balance / 100:.2f})")
        if e.error:
            lines.append("[red]Error occurred[/red]")
        console.print(Panel("\n\n".join(lines), title="AI Response"))

        # Raw response JSON
        if e.response_raw:
            try:
                raw_preview = json.dumps(e.response_raw, ensure_ascii=False, indent=2)[:2000]
            except Exception:
                raw_preview = str(e.response_raw)[:2000]
            console.print(Panel(raw_preview, title="Raw Response JSON"))

    # Command execution
    if e.exec_command:
        lines = []
        lines.append(f"[bold]Executed:[/bold] {e.exec_command}")
        lines.append(f"[bold]Return code:[/bold] {e.exec_returncode}")
        if e.session_command:
            lines.append(f"[bold]Session:[/bold] {e.session_command}")
        if e.exec_stdout:
            lines.append(f"[bold]Stdout:[/bold]\n{e.exec_stdout}")
        if e.exec_stderr:
            lines.append(f"[bold]Stderr:[/bold]\n{e.exec_stderr}")
        console.print(Panel("\n\n".join(lines), title="Command Execution"))


# ── Terminal Manager ───────────────────────────────────────────────────

def _show_terminal_detail(name: str, cmd: str, sess, created: float, alive: bool) -> None:
    """Show detailed information about a terminal."""
    uptime = time.time() - created if created > 0 else 0
    if uptime < 60:
        uptime_str = f"{uptime:.0f}s"
    elif uptime < 3600:
        uptime_str = f"{uptime / 60:.1f}m"
    else:
        uptime_str = f"{uptime / 3600:.1f}h"

    output = sess.full_output if sess else ""
    output_preview = output[-1000:] if len(output) > 1000 else output

    detail_text = (
        f"[bold]Name:[/bold] {name}\n"
        f"[bold]Command:[/bold] {cmd[:200]}\n"
        f"[bold]Status:[/bold] {'[green]Alive[/green]' if alive else '[red]Dead[/red]'}\n"
        f"[bold]Uptime:[/bold] {uptime_str}\n"
        f"[bold]Return code:[/bold] {sess.returncode if sess else 'N/A'}\n"
        f"[bold]Output ({len(output)} bytes):[/bold]\n[dim]{output_preview}[/dim]"
    )
    console.print(Panel(detail_text, title=f"Terminal: {name}"))


def show_terminal_manager(primary_session=None) -> None:
    """Interactive terminal manager — list, enter, observe, close, view details.

    Shows the primary session (if alive) plus all registered terminals.
    Arrow keys to navigate, Enter to fully enter (interactive takeover),
    o to observe read-only, c to close, d for details, q to exit.
    """

    def _collect():
        out = []
        if primary_session is not None and primary_session.is_alive():
            out.append(("term0 (primary)", primary_session.command,
                        primary_session, 0.0, True))
        for term in get_all_terminals():
            if term.name == "term0":
                continue
            out.append((term.name, term.command, term.session,
                        term.created_at,
                        term.session is not None and term.session.is_alive()))
        return out

    def _build_labels(items):
        labels = []
        for name, cmd, _sess, created, alive in items:
            status = "[green]● alive[/green]" if alive else "[red]■ dead[/red]"
            uptime_str = ""
            if created > 0:
                uptime = time.time() - created
                if uptime < 60:
                    uptime_str = f" {uptime:.0f}s"
                elif uptime < 3600:
                    uptime_str = f" {uptime / 60:.1f}m"
                else:
                    uptime_str = f" {uptime / 3600:.1f}h"
            cmd_preview = cmd[:60].replace("\n", " ")
            labels.append(f"[bold]{name}[/bold]  {status}{uptime_str}  "
                          f"[dim]{cmd_preview}[/dim]")
        return labels

    sel_idx = 0
    first = True
    while True:
        items = _collect()
        if not items:
            if first:
                console.print("[dim]No sub-terminals. Use /t <name> to create one, "
                              "or let the AI spawn a command.[/dim]")
            else:
                console.print("\n[dim]No more terminals.[/dim]")
            return
        first = False
        sel_idx = min(sel_idx, len(items) - 1)

        labels = _build_labels(items)
        result = select_dialog(
            labels,
            title="Terminal Manager",
            full_screen=True,
            selected_index=sel_idx,
            action_keys={"o": "observe", "c": "close", "d": "details"},
            enter_action="enter",
            hint="↑↓ navigate  ↵ enter  o observe  c close  d details  q back",
        )
        if result is None:
            return
        action, idx = result
        if action is None or idx < 0 or idx >= len(items):
            return

        name, cmd, sess, created, alive = items[idx]

        if action == "enter":
            if not alive:
                console.print("\n[yellow]Session has already ended.[/yellow]")
                input("[dim]Press Enter to continue...[/dim]")
            else:
                enter_session(sess, display_name=name, display_cmd=cmd)
                time.sleep(0.1)
                if name != "term0 (primary)" and not sess.is_alive():
                    unregister_terminal(name)

        elif action == "observe":
            if not alive:
                console.print("\n[yellow]Session has already ended.[/yellow]")
                input("[dim]Press Enter to continue...[/dim]")
            else:
                observe_session(sess, display_name=name, display_cmd=cmd)

        elif action == "close":
            if name == "term0 (primary)":
                console.print("\n[yellow]Cannot close the primary session. "
                              "Use /exit or close the parent terminal.[/yellow]")
                input("[dim]Press Enter to continue...[/dim]")
            else:
                unregister_terminal(name)
                console.print(f"\n[green]Closed terminal [bold]{name}[/bold][/green]")

        elif action == "details":
            _show_terminal_detail(name, cmd, sess, created, alive)
            input("\n[dim]Press Enter to continue...[/dim]")

        sel_idx = idx


def _show_skill_detail(name: str) -> None:
    """Show detailed information about a skill (mirrors _show_terminal_detail)."""
    meta = skills_mod.get_all_metadata().get(name)
    state_list = [s for s in skills_mod.list_skills() if s["name"] == name]
    loaded = bool(state_list and state_list[0].get("loaded"))
    tools = tools_mod.get_registry().list_by_source().get(f"skill:{name}", [])

    lines = [
        f"[bold]Name:[/bold] {name}",
        f"[bold]Status:[/bold] {'[green]Loaded[/green]' if loaded else '[dim]Available[/dim]'}",
        f"[bold]Version:[/bold] {getattr(meta, 'version', '') or 'N/A'}",
        f"[bold]Path:[/bold] {getattr(meta, 'dir_path', '') or 'N/A'}",
        f"[bold]Description:[/bold] {getattr(meta, 'description', '') or '(none)'}",
    ]
    if tools:
        lines.append(f"[bold]Tools ({len(tools)}):[/bold]")
        for t in tools:
            lines.append(f"  [cyan]{t.name}[/cyan] — {t.description}")
    else:
        lines.append("[bold]Tools:[/bold] [dim](documentation-only / not loaded)[/dim]")
    console.print(Panel("\n".join(lines), title=f"Skill: {name}"))


def show_skill_manager() -> None:
    """Interactive skill manager — same style as the terminal manager.

    Lists every scanned skill with its loaded status; navigate with the arrow
    keys and load/unload/reload/inspect without leaving the page.
    ↑↓ navigate, ↵/space toggle load, l load, u unload, r reload all,
    d details, n hint for new, q back.
    """
    def _collect():
        groups = tools_mod.get_registry().list_by_source()
        out = []
        for s in skills_mod.list_skills():
            n = s["name"]
            out.append((n, s.get("description", ""), bool(s.get("loaded")),
                        len(groups.get(f"skill:{n}", []))))
        return out

    def _build_labels(items):
        labels = []
        for name, desc, loaded, ntools in items:
            badge = "[green]● loaded[/green]" if loaded else "[dim]○ available[/dim]"
            tool_str = f" [dim]({ntools} tool{'s' if ntools != 1 else ''})[/dim]" if ntools else ""
            desc_preview = (desc or "").replace("\n", " ")[:56]
            labels.append(f"[bold]{name}[/bold]  {badge}{tool_str}  "
                          f"[dim]{desc_preview}[/dim]")
        return labels

    sel_idx = 0
    status_msg = ""
    first = True
    while True:
        items = _collect()
        if not items:
            if first:
                console.print(f"[dim]No skills in {skills_mod.SKILLS_DIR}[/dim]")
                console.print("[dim]Create one with: /skill new <name>[/dim]")
            return
        first = False
        sel_idx = min(sel_idx, len(items) - 1)

        labels = _build_labels(items)
        hint = ("↑↓ navigate  ↵/space toggle  l load  u unload  "
                "r reload  d details  q back")
        if status_msg:
            hint = f"{status_msg}\n{hint}"

        result = select_dialog(
            labels,
            title="Skill Manager",
            full_screen=True,
            selected_index=sel_idx,
            action_keys={"l": "load", "u": "unload", "r": "reload",
                         "d": "details", "space": "toggle"},
            enter_action="toggle",
            hint=hint,
        )
        if result is None:
            return
        action, idx = result
        if action is None or idx < 0 or idx >= len(items):
            return

        name, desc, loaded, ntools = items[idx]
        status_msg = ""

        if action == "details":
            _show_skill_detail(name)
            input("\n[dim]Press Enter to continue...[/dim]")
        elif action == "reload":
            results = skills_mod.reload_all()
            status_msg = f"Reloaded: {len(results)} skill(s) re-scanned from disk."
        elif action in ("toggle", "load", "unload"):
            want_load = action == "load" or (action == "toggle" and not loaded)
            if action == "unload" or (action == "toggle" and loaded):
                want_load = False
            if want_load:
                ok, msg = skills_mod.load_skill(name)
            else:
                ok, msg = skills_mod.unload_skill(name)
            status_msg = ("[green]" if ok else "[red]") + msg + (
                "[/green]" if ok else "[/red]")

        sel_idx = idx


# ── Meta Commands ──────────────────────────────────────────────────────

def observe_session(session, display_name: str = "", display_cmd: str = "") -> None:
    """Read-only observation mode for a sub-terminal session.

    Full-screen display that shows the session output refreshing in real-time.
    Press 'q' or Escape to return to terminal 0 (the main REPL).
    No input is forwarded to the observed session.
    """
    if not session:
        console.print("[dim]No active sub-terminal session.[/dim]")
        return

    if not session.is_alive():
        console.print("[yellow]Session has already ended.[/yellow]")
        return

    kb = KeyBindings()

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result="quit")

    # Track whether session has died during observation
    _ended = [False]

    def _get_text():
        # Pull new output first (essential for PTY sessions)
        session.read_output(timeout=0.1)

        lines = []
        if display_name:
            cmd_display = f"{display_name}: {display_cmd[:60]}" if display_cmd else display_name
        else:
            # Try to extract original command from wrapper
            raw = session.command
            m = re.search(r"--execute\s+(.+)\s+--depth\s+\d+", raw)
            if m:
                cmd_display = m.group(1).strip("'\"")[:80]
            else:
                cmd_display = raw[:80]
        alive = session.is_alive()

        if alive:
            lines.append(("bold cyan", f"● Observing: {cmd_display}\n"))
        else:
            _ended[0] = True
            lines.append(("bold red", f"■ Session ended: {cmd_display}\n"))
        lines.append(("dim", "Read-only  ·  q/Esc to return to terminal 0\n"))
        lines.append(("dim", "─" * 60 + "\n\n"))

        output = session.full_output
        if not output:
            output = "(waiting for output...)"

        output = strip_ansi(output)
        output_lines = output.split("\n")
        # Show last 200 lines
        if len(output_lines) > 200:
            output_lines = output_lines[-200:]

        for line in output_lines:
            lines.append(("", line + "\n"))

        # Auto-exit after showing ended state for a moment
        if _ended[0]:
            lines.append(("\n", ""))
            lines.append(("dim", "Session ended. Press q/Esc to return.\n"))

        return lines

    text_control = FormattedTextControl(_get_text)
    window = Window(content=text_control, always_hide_cursor=True)
    layout = Layout(HSplit([window]))

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        refresh_interval=0.3,
    )

    app.run()


def enter_session(session, display_name: str = "", display_cmd: str = "") -> None:
    """Full interactive takeover of a sub-terminal session.

    All keystrokes are forwarded to the session. Type /back or /q in the
    sub-terminal to detach without closing it. Ctrl+\\ also detaches.

    Session output streams directly to the terminal — exactly like
    using a real terminal.
    """
    if not session:
        console.print("[dim]No active sub-terminal session.[/dim]")
        return

    if not session.is_alive():
        console.print("[yellow]Session has already ended.[/yellow]")
        return

    mfd = getattr(session, 'master_fd', -1)
    if mfd < 0:
        console.print("[yellow]Session does not support raw PTY I/O (tmux mode).[/yellow]")
        return

    fd = sys.stdin.fileno()

    # Display info
    if display_name:
        cmd_display = f"{display_name}: {display_cmd[:60]}" if display_cmd else display_name
    else:
        cmd_display = getattr(session, 'command', 'sub-terminal')[:80]

    # Save terminal state
    old_tcattr = termios.tcgetattr(fd)
    old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    old_sigquit = signal.getsignal(signal.SIGQUIT)
    old_sigwinch = signal.getsignal(signal.SIGWINCH)

    def _get_winsize():
        """Return (rows, cols, xpix, ypix) of the outer terminal."""
        import struct
        try:
            ws = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                             struct.pack('HHHH', 0, 0, 0, 0))
            return struct.unpack('HHHH', ws)
        except (OSError, struct.error):
            return (24, 80, 0, 0)

    def _sync_winsize():
        """Push the outer terminal size into the sub-terminal PTY."""
        import struct
        try:
            ws = _get_winsize()
            fcntl.ioctl(mfd, termios.TIOCSWINSZ, struct.pack('HHHH', *ws))
        except (OSError, struct.error):
            pass

    # Sync window size before entering so programs render correctly
    _sync_winsize()

    # Clear screen and show a minimal header (no pending-output replay —
    # stale ANSI absolute-position sequences cause cursor misalignment).
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(f"\033[2m● {cmd_display}  │  /back or /q detach  │  Ctrl+\\ force-detach\033[0m\n")
    sys.stdout.write("─" * 60 + "\n")
    sys.stdout.flush()

    # Trigger the sub-terminal to redraw its current content
    try:
        import os as _os
        _os.write(mfd, b'\x0c')   # Ctrl+L — most shells/apps redraw on this
    except OSError:
        pass

    detached = False
    session_died = False

    def _on_sigquit(signum, frame):
        nonlocal detached
        detached = True

    def _on_sigwinch(signum, frame):
        """Forward terminal resize to the sub-terminal PTY."""
        _sync_winsize()

    signal.signal(signal.SIGQUIT, _on_sigquit)
    signal.signal(signal.SIGWINCH, _on_sigwinch)

    # Detach marker: emitted by laintas-cli when user types /back
    DETACH_MARKER = b'\x1b]777;LAINTAS_DETACH\x07'
    partial_buf = b''

    try:
        # Ensure stdin is in blocking mode — prompt_toolkit may leave O_NONBLOCK
        # set after app.run() exits, which makes select() report fd as always
        # readable and os.read() raise EAGAIN, causing the I/O loop to exit
        # immediately (resulting in read-only "observe" behavior).
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags & ~os.O_NONBLOCK)
        tty.setraw(fd)

        while session.is_alive() and not detached:
            try:
                r, _, _ = select.select([fd, mfd], [], [], 0.1)
            except (select.error, ValueError):
                break

            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    # Transient EAGAIN — shouldn't happen after clearing
                    # O_NONBLOCK, but guard anyway.
                    continue
                except OSError:
                    break
                if not data:
                    break
                # Ctrl+\ (byte 0x1c) → force detach
                if data == b'\x1c':
                    detached = True
                    break
                try:
                    os.write(mfd, data)
                except OSError:
                    break

            if mfd in r:
                try:
                    data = os.read(mfd, 4096)
                except OSError:
                    break
                if data:
                    # Prepend any partial marker from previous read
                    data = partial_buf + data
                    partial_buf = b''

                    # Check for /back detach marker
                    if DETACH_MARKER in data:
                        data = data.replace(DETACH_MARKER, b'')
                        detached = True

                    # Keep suffix that might be a partial marker
                    for i in range(len(DETACH_MARKER) - 1, 0, -1):
                        if data.endswith(DETACH_MARKER[:i]):
                            partial_buf = data[-i:]
                            data = data[:-i]
                            break

                    if data:
                        try:
                            sys.stdout.buffer.write(data)
                            sys.stdout.flush()
                        except (OSError, BrokenPipeError):
                            break
                else:
                    # EOF — sub-terminal exited
                    session_died = True
                    break

    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old_tcattr)
        except termios.error:
            pass
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        except OSError:
            pass
        signal.signal(signal.SIGQUIT, old_sigquit)
        signal.signal(signal.SIGWINCH, old_sigwinch)

    if session_died:
        console.print(f"\n[dim]● Sub-terminal exited. Returned to term0[/dim]")
    else:
        console.print(f"\n[green]● Detached. Returned to term0[/green]")


_extra_cmd_handler_cache = None
_extra_cmd_mtime_cache = 0


def _load_extra_commands():
    """Load .laintas/commands.py and return handle_extra_command() if defined."""
    global _extra_cmd_handler_cache, _extra_cmd_mtime_cache
    path = paths.project_file(paths.CWD_COMMANDS)
    try:
        mtime = path.stat().st_mtime
        if _extra_cmd_handler_cache is not None and mtime == _extra_cmd_mtime_cache:
            return _extra_cmd_handler_cache
        allowed, reason = trust_store.is_execution_allowed(path)
        if not allowed:
            console.print(
                f"[yellow]Restricted Mode: not executing {path} ({reason}). "
                "Use /trust allow after reviewing it.[/yellow]")
            return None
        src = path.read_text(encoding="utf-8")
        ns = {}
        exec(compile(src, str(path), "exec"), ns)
        handler = ns.get("handle_extra_command")
        _extra_cmd_handler_cache = handler
        _extra_cmd_mtime_cache = mtime
        return handler
    except Exception:
        _extra_cmd_handler_cache = None
        _extra_cmd_mtime_cache = 0
        return None


def handle_version_command(parts: list) -> None:
    """Handle `/v` (show version + check) and `/v update` (self-update)."""
    import updater

    sub = parts[1].lower() if len(parts) > 1 else ""
    force = any(p in ("--force", "-f") for p in parts[2:])

    if sub not in ("", "update", "check"):
        console.print("[yellow]Usage: /v  |  /v check  |  /v update [--force][/yellow]")
        return

    console.print(f"[bold]laintas-cli[/bold] [cyan]v{updater.LOCAL_VERSION}[/cyan] "
                  f"([dim]{'binary' if updater.is_frozen() else 'source'} install[/dim])")

    # `/v` and `/v check` both just check; `/v update` also applies.
    try:
        manifest = updater.fetch_manifest()
    except Exception as e:
        console.print(f"[red]Could not reach the update server: {e}[/red]")
        return

    remote_ver = manifest.get("version", "?")
    channel_dir = os.environ.get("LAINTAS_UPDATE_CHANNEL", "latest")
    available = updater.is_newer(remote_ver, updater.LOCAL_VERSION)

    if available:
        console.print(f"[green]Update available:[/green] v{remote_ver}")
        if manifest.get("notes"):
            console.print(f"  [dim]{manifest['notes']}[/dim]")
    else:
        console.print(f"[dim]Latest is v{remote_ver} — you are up to date.[/dim]")

    if sub != "update":
        if available:
            console.print("[dim]Run [bold]/v update[/bold] to install it.[/dim]")
        return

    if not available and not force:
        console.print("[dim]Nothing to update. Use [bold]/v update --force[/bold] to re-apply the latest.[/dim]")
        return

    # ── apply ──
    if updater.is_frozen():
        console.print("[yellow]Binary install — replacing the whole executable "
                      "(partial update only applies to source installs).[/yellow]")
        new_path = updater.apply_frozen_update(manifest, channel_dir, console.print)
        if new_path:
            console.print(f"[green]Updated binary at {new_path}. Restarting...[/green]")
            stop_trigger_scanner()
            close_all_terminals()
            browser_mod.close_all_browser_sessions()
            os.execv(_LAUNCH_SCRIPT_PATH, [_LAUNCH_SCRIPT_PATH] + sys.argv[1:])
        return

    changed = updater.plan_changed_files(manifest)
    if not changed:
        console.print("[green]All files already match the latest — nothing to download.[/green]")
        return

    console.print(f"[bold]Downloading {len(changed)} changed file(s):[/bold]")
    ok = updater.apply_source_update(manifest, changed, channel_dir, console.print)
    if not ok:
        console.print("[red]Update failed — no changes applied.[/red]")
        return

    console.print(f"[green]Updated to v{remote_ver} "
                  f"({len(changed)} file(s) replaced). Restarting...[/green]")
    stop_trigger_scanner()
    close_all_terminals()
    browser_mod.close_all_browser_sessions()
    os.execv(_LAUNCH_SCRIPT_PATH, [_LAUNCH_SCRIPT_PATH] + sys.argv[1:])


class SlashCommandUsageError(ValueError):
    """A recoverable slash-command parsing/usage error."""


def _parse_slash_command(cmd: str) -> tuple[str, str, list[str]]:
    """Return (action, raw_args, argv) without losing raw argument spacing."""
    stripped = (cmd or "").strip()
    if not stripped:
        raise SlashCommandUsageError("Empty slash command. Run /help for available commands.")
    match = re.match(r"^(\S+)(?:\s+(.*))?$", stripped, re.DOTALL)
    if match is None:
        raise SlashCommandUsageError("Could not parse command. Run /help for usage.")
    action = match.group(1).lower()
    raw_args = match.group(2) or ""
    try:
        args = shlex.split(raw_args, posix=not IS_WINDOWS) if raw_args else []
    except ValueError as exc:
        raise SlashCommandUsageError(
            f"Invalid quoting: {exc}. Close the quote or escape it, then retry."
        ) from exc
    return action, raw_args, [action, *args]


def _raw_tail_after_word(text: str) -> tuple[str, str]:
    """Consume one whitespace-delimited identifier and preserve the raw tail."""
    stripped = (text or "").lstrip()
    if not stripped:
        return "", ""
    match = re.match(r"(\S+)(?:\s+(.*))?$", stripped, re.DOTALL)
    if match is None:
        return stripped, ""
    return match.group(1), match.group(2) or ""


def _decode_text_arg(text: str) -> str:
    """Decode a single quoted argument; otherwise preserve the original text."""
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        parsed = shlex.split(raw, posix=not IS_WINDOWS)
    except ValueError:
        return raw
    return parsed[0] if len(parsed) == 1 else raw


def _json_arg_candidates(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    decoded = _decode_text_arg(raw)
    return list(dict.fromkeys([raw, decoded]))


def _enqueue_user_input(text: str) -> bool:
    try:
        _inject_input(text, threading.Event())
        return True
    except (NameError, queue.Full):
        return False


_PROP_KNOWN_VARS = {
    "agentName", "agentId", "currentPath", "activeFile", "depth",
    "nextDepth", "globalMemory", "persistentMemory", "lastSession",
    "planMode", "tools", "skills", "skillContext", "inbox",
    "parallelResults", "children", "parent", "terminalName",
    "parentTerminal", "deploymentStatus", "workflowPhase", "rolePrompt",
    "confidenceGuidance", "behaviorDiagnostics", "promptOpt",
}
_PROP_REQUIRED_SECTIONS = ("role", "environment", "tools", "workflow", "safety")


def _load_project_memory_entries() -> tuple[list[dict], list[str], str]:
    path = paths.project_file(paths.CWD_MEMORY)
    raw = read_file(str(path)) or ""
    if not raw.strip():
        return [], [], raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"], raw
    if not isinstance(data, list):
        return [], ["Root value must be a JSON array."], raw
    entries: list[dict] = []
    errors: list[str] = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            errors.append(f"Entry {index} must be an object, got {type(item).__name__}.")
            continue
        if "id" not in item or "content" not in item:
            errors.append(f"Entry {index} must contain id and content fields.")
            continue
        if not isinstance(item.get("content"), str):
            errors.append(f"Entry {index} content must be a string.")
            continue
        entries.append(item)
    return entries, errors, raw


def _print_long_panel(content: str, title: str, border_style: str = "cyan") -> None:
    panel = Panel(content or "(empty)", title=title, border_style=border_style)
    if len(content) > 2000 and sys.stdin.isatty() and console.is_terminal:
        with console.pager(styles=True):
            console.print(panel)
    else:
        console.print(panel)


def _redact_sensitive_text(text: str) -> str:
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|token|secret|password|passwd|cookie)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)(\"(?:api[_-]?key|token|secret|password|passwd|cookie)\"\s*:\s*\")[^\"]+", r"\1[REDACTED]"),
    )
    redacted = text
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def _validate_prop_template(prop: str) -> tuple[list[str], list[str], list[str]]:
    raw_variables = re.findall(r"\{\{(.*?)\}\}", prop, re.DOTALL)
    variables = sorted({
        value for value in raw_variables
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value)
    })
    errors: list[str] = []
    warnings: list[str] = []
    invalid_variables = sorted({
        value for value in raw_variables
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value)
    })
    if invalid_variables:
        errors.append(
            "Malformed placeholders: "
            + ", ".join("{{" + value + "}}" for value in invalid_variables))
    if prop.count("{{") != prop.count("}}"):
        errors.append("Unbalanced placeholder braces")
    unknown = sorted(set(variables) - _PROP_KNOWN_VARS)
    if unknown:
        errors.append(f"Unknown placeholders: {', '.join(unknown)}")
    for section in _PROP_REQUIRED_SECTIONS:
        opens = prop.count(f"<{section}>")
        closes = prop.count(f"</{section}>")
        if opens == 0 or closes == 0:
            errors.append(f"Missing required <{section}>...</{section}> section")
        elif opens != closes:
            errors.append(f"Unbalanced <{section}> tags ({opens} open, {closes} close)")
        elif opens > 1:
            warnings.append(f"Required section <{section}> appears {opens} times")
    patch_count = prop.count("<prompt_opt_patch>")
    close_patch_count = prop.count("</prompt_opt_patch>")
    if patch_count != close_patch_count:
        errors.append("Unbalanced <prompt_opt_patch> tags")
    elif patch_count > 1:
        warnings.append(f"Multiple prompt optimization patches found ({patch_count})")
    if len(prop.strip()) < 200:
        warnings.append("Template is unusually short")
    return variables, errors, warnings


def _render_prop_effective(prop: str, redact: bool = True,
                           prompt_section: Optional[str] = None) -> str:
    import memory_system
    import plan_mode
    import workflow_engine

    current = get_current_agent()
    entries, memory_errors, _ = _load_project_memory_entries()
    global_memory = "\n".join(
        f"[{entry.get('id')}] {entry.get('content', '')}" for entry in entries
    ) or "(empty)"
    if memory_errors:
        global_memory = "(invalid project memory: " + "; ".join(memory_errors) + ")"
    groups = tools_mod.get_registry().list_by_source()
    tool_lines = [
        f"- {tool.name}: {tool.description}"
        for source in sorted(groups)
        for tool in groups[source]
    ]
    children = []
    if current:
        children = [cid for cid in current.child_ids if get_agent(cid)]
    values = {
        "globalMemory": global_memory,
        "persistentMemory": memory_system.get_memory_context(),
        "planMode": plan_mode.get_plan_prompt(),
        "promptOpt": (prompt_lab.get_prompt_lab_section()
                      if prompt_section is None else prompt_section),
        "agentName": current.name if current else "Laintas CLI",
        "agentId": current.id if current else "unknown",
        "currentPath": os.getcwd(),
        "activeFile": "None",
        "depth": str(current.depth if current else 0),
        "nextDepth": str((current.depth if current else 0) + 1),
        "inbox": "(empty)",
        "children": ", ".join(children) or "(none)",
        "parent": current.parent_id if current and current.parent_id else "(none)",
        "terminalName": (getattr(current, "home_terminal", None) if current else None) or "(none)",
        "parentTerminal": (getattr(current, "parent_terminal", None) if current else None) or "(none)",
        "deploymentStatus": getattr(current, "role", "primary") if current else "primary",
        "tools": "\n".join(tool_lines) or "(none)",
        "skills": skills_mod.describe_skills_for_prompt(),
        "skillContext": skills_mod.get_activated_skills_context(),
        "workflowPhase": workflow_engine.render_workflow_section(),
        "rolePrompt": "",
        "confidenceGuidance": "",
        "parallelResults": "",
        "behaviorDiagnostics": "",
        "lastSession": "",
    }
    effective = prop
    for name, value in values.items():
        effective = effective.replace("{{" + name + "}}", str(value or ""))
    custom_mode_section = (
        "" if plan_mode.is_plan_mode()
        else mode_manager.render_prompt_section()
    )
    if custom_mode_section:
        effective = effective.rstrip() + "\n\n" + custom_mode_section
    if values["promptOpt"] and "{{promptOpt}}" not in prop:
        effective = effective.rstrip() + "\n\n" + values["promptOpt"]
    return _redact_sensitive_text(effective) if redact else effective


def _prompt_lab_watch_worker(branch_id: str, agent_id: str,
                             purpose: str, prior_test_count: int = 0,
                             lab_root: Optional[str] = None) -> None:
    """Reconcile persisted lab state when a background worker terminates."""
    info = wait_for_agent(agent_id, timeout=1800)
    with prompt_lab.project_scope(lab_root):
        branch = prompt_lab.read_branch(branch_id)
        if branch is None:
            return
        if purpose == "diagnosis":
            if branch.get("candidate_patch_id"):
                return
            reply = (info.last_reply if info else "") or "Prompt Lab worker ended without a draft."
            prompt_lab.add_branch_note(branch_id, reply, kind="worker-result")
            prompt_lab.update_branch(
                branch_id,
                status="NEEDS_USER" if info and info.status == "done" else "FAILED",
            )
            return
        patch_id = str(branch.get("candidate_patch_id") or "")
        patch = prompt_lab.read_patch(patch_id) if patch_id else None
        if patch and len(patch.get("test_runs") or []) > prior_test_count:
            return
        reply = (info.last_reply if info else "") or "Prompt Lab test worker ended without a result."
        prompt_lab.add_branch_note(branch_id, reply, kind="test-worker-result")
        prompt_lab.update_branch(branch_id, status="FAILED")


def _prompt_lab_start_worker(branch_id: str, session: dict,
                             feedback: str = "") -> Optional[str]:
    parent = get_current_agent()
    if parent is None:
        return None
    task = prompt_lab.build_diagnosis_task(branch_id, feedback)
    lab_root = str(prompt_lab.project_root())
    child_id = spawn_subagent(
        parent_id=parent.id,
        task=task,
        deps=get_loop_deps(),
        name=f"prompt-lab-{branch_id[-8:]}",
        session=session,
        state_overrides={
            "_prompt_lab_branch": True,
            "_prompt_lab_root": lab_root,
        },
        report_to_parent=False,
    )
    if child_id:
        prompt_lab.update_branch(
            branch_id, status="DIAGNOSING", worker_agent_id=child_id)
        threading.Thread(
            target=_prompt_lab_watch_worker,
            args=(branch_id, child_id, "diagnosis", 0, lab_root),
            daemon=True,
            name=f"prompt-lab-watch-{branch_id[-8:]}",
        ).start()
    return child_id


def _evolution_lab_watch_worker(branch_id: str, agent_id: str,
                                lab_root: str) -> None:
    info = wait_for_agent(agent_id, timeout=1800)
    with evolution_lab.project_scope(lab_root):
        branch = evolution_lab.read_branch(branch_id)
        if branch is None or branch.get("candidate_id"):
            return
        reply = ((info.last_reply if info else "")
                 or "Evolution worker ended without drafting a candidate.")
        evolution_lab.add_branch_note(branch_id, reply, kind="worker-result")
        evolution_lab.update_branch(
            branch_id,
            status="NEEDS_USER" if info and info.status == "done" else "FAILED",
        )


def _evolution_lab_start_worker(branch_id: str, session: dict,
                                feedback: str = "") -> Optional[str]:
    parent = get_current_agent()
    if parent is None:
        return None
    task = evolution_lab.build_design_task(branch_id, feedback)
    lab_root = str(evolution_lab.project_root())
    child_id = spawn_subagent(
        parent_id=parent.id, task=task, deps=get_loop_deps(),
        name=f"evolve-{branch_id[-8:]}", session=session,
        state_overrides={
            "_evolution_lab_branch": True,
            "_evolution_lab_root": lab_root,
        },
        report_to_parent=False,
    )
    if child_id:
        evolution_lab.update_branch(
            branch_id, status="DESIGNING", worker_agent_id=child_id)
        threading.Thread(
            target=_evolution_lab_watch_worker,
            args=(branch_id, child_id, lab_root), daemon=True,
            name=f"evolution-watch-{branch_id[-8:]}",
        ).start()
    return child_id


def _prompt_lab_create(description: str, session: dict) -> dict:
    chat_history = getattr(handle_meta_command, "_last_chat_history", None) or []
    parent = get_current_agent()
    state = dict(parent.state) if parent else {}
    try:
        base_prompt = paths.project_file(paths.CWD_CLI_PROP).read_text(encoding="utf-8")
    except OSError:
        base_prompt = generate_cli_prop_template()
    lab_section = prompt_lab.get_prompt_lab_section()
    effective_prompt = base_prompt.replace("{{promptOpt}}", lab_section)
    if lab_section and "{{promptOpt}}" not in base_prompt:
        effective_prompt = effective_prompt.rstrip() + "\n\n" + lab_section
    branch = prompt_lab.capture_incident(
        description=description,
        chat_history=chat_history,
        agent_state=state,
        effective_prompt=effective_prompt,
    )
    child_id = _prompt_lab_start_worker(branch["id"], session)
    if child_id:
        branch = prompt_lab.read_branch(branch["id"]) or branch
    return branch


def _prompt_lab_start_test(patch_id: str, session: dict) -> tuple[bool, str]:
    patch = prompt_lab.read_patch(patch_id)
    if patch is None:
        return False, f"Patch {patch_id} not found."
    if not patch.get("tests"):
        return False, "The candidate has no regression cases. Refine it before testing."
    try:
        overlay = prompt_lab.compile_patch(patch)
    except ValueError as exc:
        return False, str(exc)
    branch_id = str(patch.get("branch_id") or "")
    if branch_id:
        prompt_lab.update_branch(branch_id, status="TESTING", test_agent_id=None)

    lab_root = str(prompt_lab.project_root())
    try:
        base_template = paths.project_file(paths.CWD_CLI_PROP).read_text(encoding="utf-8")
    except OSError:
        base_template = generate_cli_prop_template()
    baseline_section = prompt_lab.get_prompt_lab_section({patch_id})
    base_prompt = _render_prop_effective(
        base_template, redact=False, prompt_section=baseline_section)
    candidate_prompt = base_prompt.rstrip() + "\n\n" + overlay + "\n"
    branch = prompt_lab.read_branch(branch_id) if branch_id else None
    test_cwd = str(((branch or {}).get("snapshot") or {}).get("cwd") or os.getcwd())
    deps = get_loop_deps()

    def _backend(message: str, system_prompt: str) -> str:
        kwargs = {
            "session": session,
            "message": message,
            "system_prompt": system_prompt,
            "current_path": test_cwd,
            "history": [],
            "lang": "EN",
            "tools_enabled": False,
        }
        try:
            response = deps.call_backend(**kwargs)
        except TypeError:
            kwargs.pop("tools_enabled", None)
            response = deps.call_backend(**kwargs)
        if not isinstance(response, dict):
            raise RuntimeError("backend returned a non-object response")
        if response.get("error") and not response.get("reply"):
            raise RuntimeError(str(response.get("error")))
        return str(response.get("reply") or response.get("msg") or "")

    def _parse_judgment(text: str) -> dict:
        candidates = [text.strip()]
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "candidate_passed" in value:
                return value
        raise RuntimeError("test judge did not return valid JSON")

    def _runner() -> None:
        try:
            results = []
            for case in patch.get("tests") or []:
                user_input = str(case.get("input") or "")
                baseline_reply = _backend(user_input, base_prompt)
                candidate_reply = _backend(user_input, candidate_prompt)
                judge_input = json.dumps({
                    "test": case,
                    "baseline_reply": baseline_reply,
                    "candidate_reply": candidate_reply,
                }, ensure_ascii=False)
                judgment_text = _backend(
                    judge_input,
                    "You are a strict prompt regression judge. Compare the two replies "
                    "against expected and forbidden behavior. A forbidden safety behavior "
                    "is an automatic failure. Return JSON only with boolean keys "
                    "baseline_passed and candidate_passed, plus a short reason string.",
                )
                judgment = _parse_judgment(judgment_text)
                results.append({
                    "name": case.get("name"),
                    "baseline_passed": bool(judgment.get("baseline_passed")),
                    "candidate_passed": bool(judgment.get("candidate_passed")),
                    "reason": str(judgment.get("reason") or "")[:2000],
                    "baseline_reply": baseline_reply[:4000],
                    "candidate_reply": candidate_reply[:4000],
                })
            passed = bool(results) and all(item["candidate_passed"] for item in results)
            improved = sum(
                1 for item in results
                if item["candidate_passed"] and not item["baseline_passed"])
            report = (
                f"Candidate passed {sum(1 for item in results if item['candidate_passed'])}/"
                f"{len(results)} cases; baseline passed "
                f"{sum(1 for item in results if item['baseline_passed'])}/{len(results)}; "
                f"improved cases: {improved}."
            )
            with prompt_lab.project_scope(lab_root):
                prompt_lab.record_test_result(
                    patch_id, passed, report, cases=results)
            console.print(
                f"\n[{'green' if passed else 'red'}]Prompt Lab test "
                f"{patch_id}: {report}[/{'green' if passed else 'red'}]")
        except Exception as exc:
            with prompt_lab.project_scope(lab_root):
                prompt_lab.add_branch_note(
                    branch_id, f"Regression test failed: {exc}", kind="test-error")
                prompt_lab.update_branch(branch_id, status="FAILED")
            console.print(f"\n[red]Prompt Lab test {patch_id} failed: {exc}[/red]")

    threading.Thread(
        target=_runner, daemon=True,
        name=f"prompt-replay-test-{patch_id[-8:]}",
    ).start()
    return True, "Baseline/candidate replay test started in a no-tools background runner."


def _review_and_approve_current_plan() -> Optional[dict]:
    """Review and approve one immutable WorkGraph revision, failing closed."""
    import plan_mode as _pm
    import workgraph as _wg
    import workflow_engine as _workflow

    snapshot = _pm.get_review_snapshot()
    if snapshot is None or snapshot["work"].get("status") != "REVIEW_PENDING":
        snapshot = _pm.submit_current_plan()
    if snapshot is None:
        console.print("[red]The plan is not ready for review. Ask the AI to complete it first.[/red]")
        return None
    work = snapshot["work"]
    revision = snapshot["revision"]
    steps = snapshot.get("steps") or []
    diff_text = ""
    if int(revision["revision"]) > 1:
        import difflib
        previous = _wg.get_revision(
            work["id"], int(revision["revision"]) - 1,
            cwd=work.get("cwd"))
        if previous:
            diff_text = "\n\nChanges from previous revision:\n" + "\n".join(
                difflib.unified_diff(
                    previous["content"].splitlines(),
                    revision["content"].splitlines(),
                    fromfile=f"revision-{int(revision['revision']) - 1}",
                    tofile=f"revision-{revision['revision']}",
                    lineterm=""))
    body = (
        f"Work: {work['id']}\n"
        f"Revision: {revision['revision']}\n"
        f"SHA-256: {revision['content_sha']}\n"
        f"Steps: {len(steps)}\n\n"
        f"{revision['content']}{diff_text}"
    )
    choice = _blocking_approval_prompt(
        "Plan revision review",
        body,
        "Approve this exact revision and execute it?",
        allow_always=False,
    )
    if choice != "yes":
        console.print(
            "[yellow]Plan not approved. It remains available for AI revision; "
            "use /plan revise <feedback>.[/yellow]")
        return None
    approved = _pm.approve_submitted_plan(
        revision["revision"], revision["content_sha"])
    if approved is None:
        console.print("[red]Plan changed during review; review the latest revision again.[/red]")
        return None
    try:
        _wg.begin_execution(
            work["id"], revision["revision"], revision["content_sha"],
            cwd=approved.get("cwd"))
    except _wg.WorkGraphError as exc:
        console.print(f"[red]Could not start approved execution: {exc}[/red]")
        return None
    wf = _workflow.get_active_workflow()
    if (wf and not wf.completed and wf.current
            and wf.current.exit_condition == "user_confirm"):
        try:
            _workflow.advance_phase(
                f"User approved WorkGraph revision {revision['revision']} "
                f"({revision['content_sha'][:12]}).",
                user_confirmed=True)
        except _workflow.WorkflowTransitionError:
            pass
    approved["work_id"] = work["id"]
    approved["revision"] = revision["revision"]
    approved["content_sha"] = revision["content_sha"]
    return approved


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_cents(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _usage_daily_values(daily: list, days: int) -> list[int]:
    """Calls per day (balance + subscription), zero-filled, oldest → newest."""
    by_date = {d.get("date", ""): (int(d.get("calls", 0) or 0)
                                   + int(d.get("sub_calls", 0) or 0))
               for d in daily if isinstance(d, dict)}
    today = datetime.now()
    return [by_date.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), 0)
            for i in range(days - 1, -1, -1)]


def _usage_sparkline(values: list[int]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    peak = max(values) or 1
    return "".join(
        "[rule]▁[/rule]" if v == 0
        else f"[accent]{blocks[min(7, max(0, round(v / peak * 7)))]}[/accent]"
        for v in values)


_USAGE_BAR_W = 14


def _usage_bar(value: int, peak: int, style: str = "accent") -> str:
    """Proportional horizontal bar, one row per model."""
    if peak <= 0:
        return "[rule]" + "╌" * _USAGE_BAR_W + "[/rule]"
    filled = 0 if value <= 0 else max(1, round(value / peak * _USAGE_BAR_W))
    return (f"[{style}]{'━' * filled}[/{style}]"
            f"[rule]{'╌' * (_USAGE_BAR_W - filled)}[/rule]")


def _usage_section(marker_style: str, title: str, note: str = "") -> Text:
    head = Text()
    head.append("▍", style=marker_style)
    head.append(f" {title}", style="bold")
    if note:
        head.append(f"   {note}", style="muted")
    return head


def _show_usage_command(args: list, session: dict) -> None:
    """/usage — local token accounting + Laintas backend usage (product=cli)."""
    rng, local_only = "30d", False
    for a in args:
        a = a.strip().lower()
        if a == "local":
            local_only = True
        elif a in ("7d", "30d", "90d"):
            rng = a
        elif a:
            raise SlashCommandUsageError("Usage: /usage [7d|30d|90d|local]")
    days = {"7d": 7, "30d": 30, "90d": 90}[rng]

    body: list = []

    # ── LOCAL — token accounting, works for every backend ────────────
    summary = usage_tracker.summarize(days=days)
    range_totals = summary["range"]["totals"]
    body.append(_usage_section("accent", "LOCAL", "this machine · all projects"))
    body.append(Text())

    if range_totals["calls"] == 0 and summary["session"]["totals"]["calls"] == 0:
        body.append(Text("  no calls recorded yet — stats begin with your next AI request",
                         style="muted"))
    else:
        scope = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 1))
        scope.add_column("", style="muted", min_width=10)
        for col in ("calls", "input", "output", "cost"):
            scope.add_column(col, justify="right", header_style="muted", min_width=7)
        for label, key in (("session", "session"), ("today", "today"),
                           (f"{days}d", "range")):
            t = summary[key]["totals"]
            approx = "~" if t["estimated"] else ""
            dimmed = t["calls"] == 0
            row_style = "muted" if dimmed else ""
            scope.add_row(
                label,
                Text(str(t["calls"]), style=row_style or "bold"),
                Text(approx + _fmt_tokens(t["in"]), style=row_style),
                Text(approx + _fmt_tokens(t["out"]), style=row_style),
                Text("—" if dimmed else _fmt_cents(t["costCents"]),
                     style=row_style or ("success" if t["costCents"] == 0 else "")),
            )
        body.append(Padding(scope, (0, 0, 0, 1)))

        models = summary["range"]["models"]
        if models:
            body.append(Text())
            ranked = sorted(models.items(),
                            key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
            peak = ranked[0][1]["in"] + ranked[0][1]["out"]
            mt = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 1))
            mt.add_column("model", style="bold", min_width=18)
            mt.add_column("", min_width=_USAGE_BAR_W)
            mt.add_column("calls", justify="right", header_style="muted")
            mt.add_column("tokens", justify="right", header_style="muted", min_width=9)
            mt.add_column("cost", justify="right", header_style="muted", min_width=7)
            for name, m in ranked[:8]:
                total = m["in"] + m["out"]
                approx = "~" if m["estimated"] else ""
                bar_style = "warning" if m["estimated"] else "accent"
                mt.add_row(
                    name,
                    Text.from_markup(_usage_bar(total, peak, bar_style)),
                    str(m["calls"]),
                    f"{approx}{_fmt_tokens(total)}",
                    _fmt_cents(m["costCents"]),
                )
            body.append(Padding(mt, (0, 0, 0, 1)))
        if range_totals["estimated"]:
            body.append(Text("  ~ estimated — backend sent no token counts (chars/4)",
                             style="muted"))

    # ── LAINTAS — backend usage, same gateway endpoints Helpwo uses ──
    profile = get_backend_profile()
    footnotes: list[str] = []
    if not local_only:
        body.append(Text())
        if not profile.sends_laintas_credentials:
            body.append(_usage_section("warning", "BACKEND", profile.base_url))
            body.append(Text())
            body.append(Text(f"  {profile.billing_label} — not billed by Laintas; "
                             "local stats above are authoritative", style="muted"))
        elif not session.get("userId"):
            body.append(_usage_section("warning", "LAINTAS", "not logged in"))
            body.append(Text())
            body.append(Text.from_markup(
                "  run [bold]/login[/bold] to see backend usage, balance and plan"))
        else:
            usage, bal, fail = None, {}, ""
            headers, cookies = backend_profiles.request_auth(profile, session)
            try:
                resp = requests.get(f"{profile.base_url}/api/usage",
                                    params={"range": rng, "product": "cli"},
                                    headers=headers, cookies=cookies, timeout=10)
                usage = resp.json() if resp.status_code == 200 else None
                if usage is None:
                    fail = f"HTTP {resp.status_code}"
                bresp = requests.get(f"{profile.base_url}/api/balance",
                                     headers=headers, cookies=cookies, timeout=10)
                bal = bresp.json() if bresp.status_code == 200 else {}
            except requests.RequestException as e:
                fail = type(e).__name__

            if not isinstance(usage, dict):
                body.append(_usage_section("warning", "LAINTAS", "unreachable"))
                body.append(Text())
                body.append(Text(f"  backend usage unavailable ({fail})", style="warning"))
            else:
                ov = usage.get("overview") or {}
                spent = int(ov.get("total_spent_cents", 0) or 0)
                bal_calls = int(ov.get("balance_calls", 0) or 0)
                sub_calls = int(ov.get("sub_calls", 0) or 0)
                plan = ((bal.get("subscription") or {}).get("plan") or "").upper()

                body.append(_usage_section("agent", "LAINTAS",
                                           f"{profile.origin} · product=cli · {rng}"))
                body.append(Text())
                grid = Table(box=None, show_edge=False, show_header=False,
                             pad_edge=False, padding=(0, 1))
                grid.add_column(style="muted", min_width=10)
                grid.add_column()
                if bal.get("balanceFormatted"):
                    balance_val = Text(bal["balanceFormatted"], style="bold")
                    if plan:
                        balance_val.append("  ")
                        balance_val.append(f" {plan} ",
                                           style="black on #bb9af7" if plan == "GEN"
                                           else "black on #7aa2f7")
                    grid.add_row("balance", balance_val)
                grid.add_row("spent", Text(_fmt_cents(spent),
                                           style="bold" if spent else "muted"))
                calls_val = Text()
                calls_val.append(str(bal_calls), style="bold")
                calls_val.append(" balance", style="muted")
                calls_val.append("  ·  ", style="rule")
                calls_val.append(str(sub_calls), style="bold")
                calls_val.append(" subscription", style="muted")
                grid.add_row("calls", calls_val)
                values = _usage_daily_values(usage.get("daily") or [], days)
                if any(values):
                    spark = Text.from_markup(_usage_sparkline(values))
                    spark.append(f"  peak {max(values)}/day", style="muted")
                    grid.add_row("activity", spark)
                body.append(Padding(grid, (0, 0, 0, 1)))
                if sub_calls:
                    footnotes.append("subscription calls carry no token counts on the "
                                     "backend — LOCAL tokens are authoritative")

    if not get_runtime_config("show_billing"):
        footnotes.append("/config show_billing true prints cost after every reply")
    if footnotes:
        body.append(Text())
        for note in footnotes:
            body.append(Text(f"· {note}", style="muted"))

    console.print()
    console.print(Panel(
        Group(*body),
        box=box.ROUNDED,
        border_style="rule",
        title="[bold accent]AI usage[/bold accent]",
        title_align="left",
        subtitle=f"[muted]/usage · {rng}[/muted]",
        subtitle_align="right",
        padding=(1, 2),
    ))


def _handle_meta_command_impl(cmd: str, agent_registry: AgentRegistry, session: dict, interactive_session=None) -> bool:
    """Handle meta commands. Returns True if should exit."""
    action, raw_args, parts = _parse_slash_command(cmd)

    if action == "/":
        selected = show_command_palette()
        if selected:
            if not _enqueue_user_input(selected):
                console.print("[red]Could not queue the selected command. Please run it directly.[/red]")
        return False

    if action == "/exit":
        if raw_args:
            console.print("[yellow]Usage: /exit[/yellow]")
            return False
        stop_trigger_scanner()
        close_all_terminals()
        browser_mod.close_all_browser_sessions()
        agent_registry.unregister()
        clear_session()
        console.print("[green]Logged out. Goodbye![/green]")
        return True

    if action in ("/quit", "/q"):
        if raw_args:
            console.print(f"[yellow]Usage: {action}[/yellow]")
            return False
        if _IN_SUB_TERMINAL:
            # Running inside a sub-terminal — detach like /back instead of quitting
            sys.stdout.write("\x1b]777;LAINTAS_DETACH\x07")
            sys.stdout.flush()
            console.print("[green]Detaching...[/green]")
            return False
        stop_trigger_scanner()
        close_all_terminals()
        browser_mod.close_all_browser_sessions()
        agent_registry.unregister()
        console.print("[green]Goodbye![/green]")
        return True

    elif action == "/back":
        if raw_args:
            console.print("[yellow]Usage: /back[/yellow]")
            return False
        if not _IN_SUB_TERMINAL:
            console.print("[dim]/back only detaches from a sub-terminal.[/dim]")
            return False
        # Signal parent enter_session to detach without closing this terminal
        sys.stdout.write("\x1b]777;LAINTAS_DETACH\x07")
        sys.stdout.flush()
        console.print("[green]Detaching...[/green]")
        return False

    elif action == "/help":
        show_help(parts[1] if len(parts) > 1 else "")

    elif action == "/resume":
        console.print("[yellow]/resume is handled by the main REPL. Type it at the prompt to resume a saved session.[/yellow]")

    elif action in ("/new", "/new-session", "/reset-session"):
        console.print("[yellow]/new is handled by the main REPL. Type it at the prompt to start a new session.[/yellow]")

    elif action == "/login":
        console.print()
        console.print(Panel(
            "[bold]Login to Laintas[/bold]\n\n"
            "[1] [bold]Remote login[/bold] — opens accounts.laintas.com\n"
            "    (works from any device, no password typing)\n\n"
            "[2] [bold]Local login[/bold] — username + password in terminal\n"
            "    (no browser needed)",
            title="Choose login method"
        ))
        choice = input("Choose [1] or [2] (default 1): ").strip() or "1"
        if choice == "1":
            new_session = login_via_browser()
        elif choice == "2":
            new_session = login_interactive()
        else:
            new_session = None
            console.print("[red]Invalid choice[/red]")
        if new_session:
            session.clear()
            session.update(new_session)
            # Refresh the Helpwo link only if this terminal was already
            # /connect-ed — logging in never auto-links (two-end handshake).
            if agent_registry.agent_id:
                agent_registry.register(session, quiet=True)
            console.print(f"[green]Logged in as {new_session.get('userEmail') or new_session.get('userName') or new_session['userId']}[/green]")

    elif action == "/model":
        if len(parts) >= 2 and parts[1].lower() in ("reset", "clear", "default"):
            set_selected_model("")
            set_selected_provider("")
            _update_status_cache(model="")
            console.print("[green]Model reset. Backend default will be used.[/green]")
        elif len(parts) >= 2:
            model = _decode_text_arg(raw_args)
            set_selected_model(model)
            _update_status_cache(model=model)
            console.print(f"[green]Model set to: [bold]{model}[/bold][/green]")
        else:
            current = get_selected_model()
            current_provider = get_selected_provider()
            try:
                with console.status("[dim]Fetching available models…[/dim]"):
                    models, endpoint = fetch_available_models(session)
            except Exception as e:
                console.print(f"[red]Failed to fetch models: {e}[/red]")
                console.print(f"Current model: [bold]{current or 'backend default'}[/bold]")
                console.print("Usage: /model <model-id>  or  /model reset")
            else:
                if models and sys.stdin.isatty():
                    selected = show_model_selector(models, current)
                    if selected:
                        model_id = selected.get("id", "") if isinstance(selected, dict) else selected
                        provider_id = selected.get("provider", "") if isinstance(selected, dict) else ""
                        set_selected_model(model_id)
                        set_selected_provider(provider_id)
                        _update_status_cache(model=model_id)
                        info = f"[bold]{model_id}[/bold]"
                        if provider_id:
                            info += f" ([dim]{provider_id}[/dim])"
                        console.print(f"[green]Model set to: {info}[/green]")
                    else:
                        console.print("[dim]Model selection cancelled.[/dim]")
                else:
                    table = Table(title=f"Available Models ({endpoint})")
                    table.add_column("#", style="dim")
                    table.add_column("Current", style="green")
                    table.add_column("Model ID", style="cyan")
                    table.add_column("Name")
                    table.add_column("Provider")
                    for idx, m in enumerate(models, start=1):
                        marker = "*" if current and m["id"] == current else ""
                        table.add_row(
                            str(idx),
                            marker,
                            m["id"],
                            m.get("name", ""),
                            m.get("provider", ""),
                        )
                    if not models:
                        table.add_row("", "", "(none)", "", "")
                    console.print(table)
                    console.print(f"Current model: [bold]{current or 'backend default'}[/bold]" +
                                  (f" ([dim]{current_provider}[/dim])" if current_provider else ""))
                    if models:
                        choice = input("Choose model number or id (Enter to cancel): ").strip()
                        if choice:
                            chosen = None
                            if choice.isdigit():
                                idx = int(choice)
                                if 1 <= idx <= len(models):
                                    chosen = models[idx - 1]
                            else:
                                chosen = next((m for m in models if m["id"] == choice), None)
                                if chosen is None:
                                    chosen = {"id": choice, "provider": ""}
                            if chosen:
                                set_selected_model(chosen["id"])
                                set_selected_provider(chosen.get("provider", ""))
                                _update_status_cache(model=chosen["id"])
                                console.print(f"[green]Model set to: [bold]{chosen['id']}[/bold][/green]")
                            else:
                                console.print(f"[red]Invalid model selection: {choice}[/red]")
                console.print("Set directly with [bold]/model <model-id>[/bold], reset with [bold]/model reset[/bold].")

    elif action == "/name":
        if raw_args:
            name = _decode_text_arg(raw_args)
            config = load_config()
            config["agentName"] = name
            save_config(config)
            console.print(f"[green]Agent name set to: {name}[/green]")
            # Re-register under the new name only if already /connect-ed —
            # renaming never auto-links (two-end handshake).
            if agent_registry.agent_id:
                agent_registry.unregister()
                agent_registry.agent_id = None
                agent_registry.agent_secret = ""
                agent_registry.register(session, name=name, quiet=True)
                agent_registry.start_heartbeat()
                agent_registry.start_message_poll(
                    agent_registry._state_cb or (lambda: {}),
                    agent_registry._chat_cb or (lambda: []),
                )
        else:
            config = load_config()
            current = config.get("agentName", socket.gethostname())
            console.print(f"Current agent name: [bold]{current}[/bold]")
            console.print("Usage: /name <new-name>")
            console.print("       /agents name <new-name>  (rename current agent)")
            console.print("       /term rename <old> <new>  (rename a terminal)")

    elif action == "/memory":
        import memory_system
        sub = parts[1].lower() if len(parts) > 1 else ""
        project_entries, project_errors, _ = _load_project_memory_entries()
        persistent_entries = memory_system.list_memories()

        if sub in ("", "project"):
            if project_errors:
                console.print(Panel(
                    "\n".join(f"[red]- {error}[/red]" for error in project_errors)
                    + f"\n\n[dim]Fix {paths.project_file(paths.CWD_MEMORY)} and retry /memory project.[/dim]",
                    title="Project Memory: invalid", border_style="red",
                ))
            elif project_entries:
                lines = [
                    f"[bold]{entry.get('id')}.[/bold] {entry.get('content', '')}"
                    for entry in project_entries
                ]
                console.print(Panel(
                    "\n".join(lines),
                    title=f"Project memory ({len(project_entries)} entries)",
                    border_style="cyan",
                ))
            else:
                console.print("[dim]Project memory is empty.[/dim]")
            if sub == "":
                console.print(
                    f"[dim]Persistent memory: {len(persistent_entries)} visible entr"
                    f"{'y' if len(persistent_entries) == 1 else 'ies'}. "
                    "Use /memory persistent to list them.[/dim]")

        elif sub in ("persistent", "global"):
            if not persistent_entries:
                console.print("[dim]No persistent memories are visible in this scope.[/dim]")
            else:
                table = Table(title="Persistent Memory", show_lines=False)
                table.add_column("Name", style="cyan")
                table.add_column("Type")
                table.add_column("Scope", style="dim")
                table.add_column("Description")
                for entry in persistent_entries:
                    table.add_row(
                        entry.get("name", "?"), entry.get("type", "unknown"),
                        entry.get("scope", "?"), entry.get("description", ""),
                    )
                console.print(table)
                console.print("[dim]Read one with /memory show <name>.[/dim]")

        elif sub == "show" and len(parts) >= 3:
            selector = parts[2]
            project = next(
                (entry for entry in project_entries
                 if str(entry.get("id")) == selector), None)
            if project is not None:
                _print_long_panel(
                    project.get("content", ""), f"Project memory #{selector}")
            else:
                persistent = memory_system.read_memory(selector)
                if persistent is None:
                    console.print(f"[red]Memory '{selector}' not found.[/red]")
                    console.print("[dim]Use /memory project or /memory persistent to list valid entries.[/dim]")
                else:
                    _print_long_panel(
                        persistent.get("body", ""),
                        f"Persistent memory: {selector}",
                    )
        else:
            console.print("[yellow]Usage: /memory [project|persistent|show <id|name>][/yellow]")

    elif action == "/prop":
        prop = read_file(str(paths.project_file(paths.CWD_CLI_PROP)))
        if prop:
            console.print(Panel(prop[:2000], title=".laintas/cli.prop Prompt Template"))
        else:
            console.print("[dim]No .laintas/cli.prop found.[/dim]")

    elif action == "/scan":
        user_cmds = list_path_commands()
        console.print(f"[bold]{len(user_cmds)} user-facing commands on PATH:[/bold]\n")
        groups: dict = {}
        for c in user_cmds:
            groups.setdefault(c[0], []).append(c)
        for letter in sorted(groups):
            console.print(f"  [cyan]{letter}[/cyan]: {', '.join(groups[letter][:40])}")
            if len(groups[letter]) > 40:
                console.print(f"       [dim](+{len(groups[letter]) - 40} more)[/dim]")

    elif action == "/cwd":
        console.print(f"Working directory: [bold]{os.getcwd()}[/bold]")

    elif action == "/usage":
        _show_usage_command(parts[1:], session)

    elif action == "/bash":
        bash_sub = parts[1].lower() if len(parts) > 1 else ""
        if len(parts) < 2:
            wl = ", ".join(sorted(get_interactive_commands()))
            console.print(Panel(
                "[bold]/bash <command>[/bold]        run <command> directly via term0's real bash "
                "(marker-poll + cwd sync), bypassing the interactive whitelist below\n"
                "[bold]/bash list[/bold]              show the interactive-terminal whitelist\n"
                "[bold]/bash add <command>[/bold]     force <command> through full PTY passthrough "
                "(vim-style — use for full-screen/raw-keystroke programs)\n"
                "[bold]/bash remove <command>[/bold]  let <command> use term0/marker-poll instead\n\n"
                f"[dim]Current whitelist: {wl}[/dim]",
                title="/bash", border_style="cyan",
            ))
        elif bash_sub == "list":
            wl = sorted(get_interactive_commands())
            console.print(Panel("\n".join(wl) or "(empty)",
                                title="Interactive-terminal whitelist", border_style="cyan"))
        elif bash_sub == "add":
            if len(parts) < 3:
                console.print("[yellow]Usage: /bash add <command>[/yellow]")
            elif not re.fullmatch(r"[A-Za-z0-9._+/-]+", parts[2]):
                console.print("[red]Command must be one executable token without shell operators.[/red]")
            else:
                _modify_interactive_commands(parts[2], add=True)
                console.print(f"[green]'{parts[2]}' now uses full PTY passthrough (native terminal).[/green]")
        elif bash_sub == "remove":
            if len(parts) < 3:
                console.print("[yellow]Usage: /bash remove <command>[/yellow]")
            elif not re.fullmatch(r"[A-Za-z0-9._+/-]+", parts[2]):
                console.print("[red]Command must be one executable token without shell operators.[/red]")
            else:
                _modify_interactive_commands(parts[2], add=False)
                console.print(f"[green]'{parts[2]}' now routes through term0/marker-poll.[/green]")
        else:
            raw_cmd = raw_args
            allowed, denial = authorize_direct_command(raw_cmd, os.getcwd())
            if not allowed:
                console.print(f"[red]{denial}[/red]")
                return False
            if not IS_WINDOWS:
                _ensure_term0_alive()
            _t0 = get_terminal("term0")
            if IS_WINDOWS or _t0 is None or _t0.session is None or not _t0.session.is_alive():
                console.print("[red]term0 session unavailable (Windows has no persistent bash).[/red]")
            else:
                result = _marker_poll_exec(_t0.session, raw_cmd, strip_ansi_codes=False)
                _sync_cwd_from_term0(_t0.session)
                stdout = result.get("stdout", "")
                if stdout:
                    console.print(stdout)
                returncode = result.get("returncode")
                rc_text = f" · exit {returncode}" if returncode is not None else ""
                console.print(f"[dim]cwd → {os.getcwd()}{rc_text}[/dim]")

    elif action == "/clear":
        console.clear()

    elif action == "/mode":
        import plan_mode as _pm_mode
        from rich.markup import escape as _escape
        sub = parts[1].lower() if len(parts) > 1 else ""
        _, mode_args_raw = _raw_tail_after_word(raw_args)

        # Gather current state
        _in_plan = _pm_mode.is_plan_mode()
        _cur_plan = _pm_mode.get_current_plan()

        if sub == "plan":
            task = _decode_text_arg(mode_args_raw)
            if not task:
                if _in_plan:
                    console.print("[dim]Already in PLAN mode.[/dim]")
                    return False
                mode_manager.activate("act")
                _pm_mode.arm_plan_mode()
                console.print(Panel(
                    "[bold]PLAN mode[/bold]\n\n"
                    "Describe the task in your next message. The agent will plan "
                    "without modifying files or the system.",
                    title="Mode changed", border_style="green",
                ))
                return False
            if _in_plan:
                console.print(
                    "[yellow]A plan is already active. Use /plan status, "
                    "/plan approve, or /plan exit before starting another.[/yellow]")
                return False
            mode_manager.activate("act")
            plan = _pm_mode.enter_plan_mode(task)
            _enqueue_user_input(task)
            console.print(Panel(
                f"[bold]Plan Mode: [green]ENTERED[/green][/bold]\n\n"
                f"Task: {task}\n"
                f"Plan file: {plan['file']}\n\n"
                f"[dim]The AI will explore and design — no code will be executed.[/dim]\n"
                f"[dim]When ready, the review menu will offer execute, revise, or exit.[/dim]",
                title="Plan Mode", border_style="green",
            ))

        elif sub == "act":
            if _in_plan:
                _pm_mode.exit_plan_mode(approve=False)
            ok, msg = mode_manager.activate("act")
            console.print(
                f"[{'green' if ok else 'red'}]{_escape(msg)}"
                f"[/{'green' if ok else 'red'}]"
                + (" [dim](draft plan saved)[/dim]" if _cur_plan else ""))

        elif sub == "approve":
            # Backward compatibility. Approval is a plan action, not a mode.
            console.print("[yellow]/mode approve is deprecated; use /plan approve.[/yellow]")
            if not _in_plan or not _cur_plan:
                console.print("[yellow]No active plan to approve.[/yellow]")
            else:
                approved = _review_and_approve_current_plan()
                queued = False
                if approved:
                    followup = (
                        f"Execute approved WorkGraph {approved['work_id']} revision "
                        f"{approved['revision']} SHA {approved['content_sha']} for task: "
                        f"{approved['task']}. Follow the injected approved_work_plan exactly."
                    )
                    queued = _enqueue_user_input(followup)
                console.print(Panel(
                    f"[bold]Plan [{'green' if approved else 'yellow'}]"
                    f"{'APPROVED' if approved else 'NOT APPROVED'}"
                    f"[/{'green' if approved else 'yellow'}][/bold]\n\n"
                    f"File: {_cur_plan['file']}\n\n"
                    + ("[dim]Execution request queued.[/dim]" if queued else
                       "[dim]No execution was queued.[/dim]"),
                    title="Plan Approved", border_style="green",
                ))

        elif sub == "list":
            active_name = "plan" if _in_plan else mode_manager.get_active_mode()["name"]
            console.print("[bold]Agent modes[/bold]")
            console.print(
                f"{'[green]*[/green]' if active_name == 'plan' else ' '} "
                "[cyan]plan[/cyan] [dim]Reviewed, read-only planning[/dim]")
            for item in mode_manager.list_modes():
                marker = "[green]*[/green]" if item["name"] == active_name else " "
                source = "built-in" if item["builtin"] else "custom"
                console.print(
                    f"{marker} [cyan]{item['name']}[/cyan] "
                    f"[dim]{item['description']} · {source}[/dim]")

        elif sub == "create":
            if len(parts) < 4:
                console.print(
                    "[yellow]Usage: /mode create <name> [--read-only] "
                    "<instructions>[/yellow]")
            else:
                name = parts[2]
                read_only = "--read-only" in parts[3:]
                instructions = " ".join(
                    item for item in parts[3:] if item != "--read-only")
                ok, msg = mode_manager.create_mode(
                    name, instructions, read_only=read_only)
                console.print(
                    f"[{'green' if ok else 'red'}]{_escape(msg)}"
                    f"[/{'green' if ok else 'red'}]")

        elif sub == "delete":
            if len(parts) != 3:
                console.print("[yellow]Usage: /mode delete <name>[/yellow]")
            else:
                ok, msg = mode_manager.delete_mode(parts[2])
                console.print(
                    f"[{'green' if ok else 'red'}]{_escape(msg)}"
                    f"[/{'green' if ok else 'red'}]")

        elif sub:
            if mode_manager.get_mode(sub) is None:
                ok, msg = False, f"Unknown mode: {sub}"
            else:
                if _in_plan:
                    _pm_mode.exit_plan_mode(approve=False)
                ok, msg = mode_manager.activate(sub)
            console.print(
                f"[{'green' if ok else 'red'}]{_escape(msg)}"
                f"[/{'green' if ok else 'red'}]")

        else:
            active = mode_manager.get_active_mode()
            _mode_name = "PLAN" if _in_plan else active["name"].upper()
            _mode_desc = (
                "Waiting for a task" if _pm_mode.is_pending_task() else
                (_cur_plan.get("task", "") if _cur_plan else active["description"])
            )
            console.print(Panel(
                f"Mode: [accent]{_mode_name}[/accent]\n"
                f"[dim]{_escape(_mode_desc[:120])}[/dim]\n\n"
                "[bold]/mode act[/bold] · [bold]/mode plan[/bold] · "
                "[bold]/mode review[/bold] · [bold]/mode list[/bold]\n"
                "[dim]Security policy is managed separately with /policy.[/dim]",
                title="Current Mode", border_style="cyan",
            ))

    elif action == "/trust":
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub == "status":
            status = trust_store.project_status()
            style = "green" if status.get("trusted") else "yellow"
            hashes = status.get("hashes") or {}
            details = "\n".join(
                f"  {name}: {digest[:16]}…" for name, digest in sorted(hashes.items())
            ) or "  (no executable project customization)"
            console.print(Panel(
                f"Project: {status.get('realpath', os.getcwd())}\n"
                f"Trusted: {status.get('trusted', False)}\n"
                f"Reason: {status.get('reason', '')}\n"
                f"Executable hashes:\n{details}",
                title="Workspace Trust", border_style=style,
            ))
        elif sub == "allow":
            status = trust_store.project_status()
            hashes = status.get("hashes") or {}
            preview = "\n".join(
                f"{name}: {digest}" for name, digest in sorted(hashes.items())
            ) or "No executable files are currently present."
            approved = "--yes" in parts[2:]
            if not approved and sys.stdin.isatty():
                approved = _blocking_approval_prompt(
                    "Trust workspace executable customization",
                    f"Project: {Path.cwd().resolve()}\n\n{preview}\n\n"
                    "Trusted Python runs with your full local account permissions.",
                    "Trust these exact file hashes?",
                    allow_always=False,
                ) == "yes"
            if not approved:
                console.print(
                    "[yellow]Workspace not trusted. In non-interactive mode use "
                    "/trust allow --yes after reviewing the files.[/yellow]")
            else:
                trusted = trust_store.trust_project()
                clear_loop_command_cache()
                global _extra_cmd_handler_cache, _extra_cmd_mtime_cache
                _extra_cmd_handler_cache = None
                _extra_cmd_mtime_cache = 0
                console.print(
                    f"[green]Trusted executable customization for "
                    f"{trusted['realpath']} at the current hashes.[/green]")
        elif sub == "revoke":
            removed = trust_store.revoke_project()
            clear_loop_command_cache()
            _extra_cmd_handler_cache = None
            _extra_cmd_mtime_cache = 0
            console.print(
                "[green]Workspace trust revoked.[/green]" if removed
                else "[dim]Workspace was not explicitly trusted.[/dim]")
        else:
            console.print("[yellow]Usage: /trust [status|allow|revoke][/yellow]")

    elif action == "/backend":
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub == "status":
            profile = get_backend_profile()
            console.print(Panel(
                f"Profile: {profile.name}\nKind: {profile.kind}\n"
                f"URL: {profile.base_url}\nBilling: {profile.billing_label}\n"
                f"Sends Laintas credentials: {profile.sends_laintas_credentials}",
                title="Backend", border_style=(
                    "green" if profile.sends_laintas_credentials else "yellow"),
            ))
        elif sub == "list":
            active = get_backend_profile().name
            for profile in backend_profiles.list_profiles():
                marker = "*" if profile.name == active else " "
                console.print(
                    f"{marker} [bold]{profile.name}[/bold] [{profile.kind}] "
                    f"{profile.base_url} [dim]{profile.billing_label}[/dim]")
        elif sub == "use":
            if len(parts) < 3:
                console.print("[yellow]Usage: /backend use <name>[/yellow]")
            elif os.environ.get("LAINTAS_BACKEND"):
                console.print(
                    "[red]LAINTAS_BACKEND currently overrides profiles; unset it first.[/red]")
            else:
                ok, msg = backend_profiles.set_active(parts[2])
                console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
        elif sub == "config":
            console.print(str(backend_profiles.ensure_template()))
        else:
            console.print("[yellow]Usage: /backend [status|list|use <name>|config][/yellow]")

    elif action == "/hooks":
        sub = parts[1].lower() if len(parts) > 1 else "status"
        hook_path = paths.PYTHON_HOOKS_FILE
        if sub == "status":
            py_status = (
                trust_store.extension_status("hooks", "python", hook_path)
                if hook_path.is_file() else {"trusted": False, "reason": "no hooks.py"}
            )
            config_status = (
                trust_store.extension_status(
                    "hooks", "config", paths.HOOKS_FILE)
                if paths.HOOKS_FILE.is_file()
                else {"trusted": False, "reason": "no hooks.json"}
            )
            info = hooks_mod.reload()
            console.print(Panel(
                f"Python hooks: {hook_path}\n"
                f"Trusted: {py_status.get('trusted', False)}\n"
                f"Reason: {py_status.get('reason', '')}\n"
                f"Config hooks: {paths.HOOKS_FILE}\n"
                f"Trusted: {config_status.get('trusted', False)}\n"
                f"Reason: {config_status.get('reason', '')}\n"
                f"Loaded functions: {', '.join(info.get('python_hooks', [])) or '(none)'}\n"
                f"Configured argv hooks: {info.get('shell_hooks', 0)}",
                title="Hooks", border_style="cyan",
            ))
        elif sub == "trust":
            targets = []
            if hook_path.is_file():
                targets.append(("python", hook_path))
            if paths.HOOKS_FILE.is_file():
                targets.append(("config", paths.HOOKS_FILE))
            if not targets:
                console.print("[red]No hooks.py or hooks.json exists.[/red]")
            else:
                statuses = [
                    (name, path, trust_store.extension_status("hooks", name, path))
                    for name, path in targets
                ]
                details = "\n".join(
                    f"{name}: {path} SHA-256={status.get('sha256', 'unavailable')}"
                    for name, path, status in statuses)
                approved = "--yes" in parts[2:]
                if not approved and sys.stdin.isatty():
                    approved = _blocking_approval_prompt(
                        "Trust executable hooks",
                        f"{details}\n\nThese hooks execute processes or Python "
                        "with your local account permissions.",
                        "Trust these exact hook hashes?", allow_always=False,
                    ) == "yes"
                if approved:
                    trusted = []
                    for name, path, _ in statuses:
                        trusted.append(trust_store.trust_extension(
                            "hooks", name, path))
                    hooks_mod.reload()
                    console.print(
                        f"[green]Trusted {len(trusted)} hook file(s) at their current hashes.[/green]")
                else:
                    console.print("[yellow]Hooks not trusted.[/yellow]")
        elif sub == "revoke":
            removed = trust_store.revoke_extension("hooks", "python")
            removed = trust_store.revoke_extension("hooks", "config") or removed
            hooks_mod.reload()
            console.print("[green]Hook trust revoked.[/green]" if removed
                          else "[dim]Hooks were not trusted.[/dim]")
        elif sub == "reload":
            info = hooks_mod.reload()
            console.print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            console.print("[yellow]Usage: /hooks [status|trust|revoke|reload][/yellow]")

    elif action == "/policy":
        import policy as _pol_cmd
        sub = parts[1].lower() if len(parts) > 1 else ""
        _valid = {"audit", "enforce", "disabled"}
        if sub in _valid:
            if sub == "disabled" and "--yes" not in parts[2:]:
                if not sys.stdin.isatty():
                    console.print(
                        "[red]Disabling policy in a non-interactive shell requires "
                        "/policy disabled --yes.[/red]")
                    return False
                choice = _blocking_approval_prompt(
                    "Disable security policy",
                    "This bypasses policy checks and approval rules for commands.",
                    "Disable policy?",
                )
                if choice != "yes":
                    console.print("[dim]Policy change cancelled.[/dim]")
                    return False
            ok, msg = _pol_cmd.set_mode(sub)
            style = "green" if ok else "red"
            console.print(f"[{style}]{msg}[/{style}]")
            if ok:
                if sub == "enforce":
                    console.print("[dim]Commands matching approval rules will now prompt before execution.[/dim]")
                elif sub == "disabled":
                    console.print("[yellow]⚠ All policy checks bypassed — commands run without approval.[/yellow]")
                elif sub == "audit":
                    console.print("[dim]Policy advisory only — deny rules still block, approvals are advisory.[/dim]")
        elif sub == "reset":
            _reset_session_approvals()
            console.print("[green]Session auto-approvals cleared.[/green]")
        else:
            _cfg = _pol_cmd.get_config()
            _mode = _cfg.get("mode", "audit")
            _mode_style = {"audit": "cyan", "enforce": "yellow",
                           "disabled": "red"}.get(_mode, "cyan")
            _n_allow = len(_cfg.get("allow", []))
            _n_deny = len(_cfg.get("deny", []))
            _n_appr = len(_cfg.get("needs_approval", []))
            console.print(Panel(
                f"Mode:           [{_mode_style}]{_mode}[/{_mode_style}]\n"
                f"Allow rules:    {_n_allow}\n"
                f"Deny rules:     {_n_deny}\n"
                f"Approval rules: {_n_appr}\n"
                f"Config file:    [dim]{_pol_cmd.CONFIG_PATH}[/dim]\n\n"
                f"[dim]Session auto-approve:[/dim]  "
                f"commands={'[green]on[/green]' if _session_approval_state['all_commands'] else 'off'}  "
                f"writes={'[green]on[/green]' if _session_approval_state['all_writes'] else 'off'}\n\n"
                f"[dim]Set with:[/dim]  [bold]/policy audit[/bold]  [bold]/policy enforce[/bold]  [bold]/policy disabled[/bold]\n"
                f"[dim]Reset session approvals:[/dim]  [bold]/policy reset[/bold]",
                title="Security Policy", border_style="cyan",
            ))

    elif action == "/plan":
        import plan_mode as _pm
        sub = parts[1].lower() if len(parts) > 1 else ""
        _, plan_args_raw = _raw_tail_after_word(raw_args)
        if sub == "enter" and plan_args_raw:
            task = _decode_text_arg(plan_args_raw)
            if _pm.is_plan_mode():
                console.print(
                    "[yellow]A plan is already active. Approve or exit it before "
                    "starting another.[/yellow]")
                return False
            mode_manager.activate("act")
            plan = _pm.enter_plan_mode(task)
            _enqueue_user_input(task)
            console.print(Panel(
                f"[bold]Plan Mode: [green]ENTERED[/green][/bold]\n\n"
                f"Task: {task}\n"
                f"Plan file: {plan['file']}\n\n"
                f"[dim]The AI will now explore and design — no code will be executed.[/dim]\n"
                f"[dim]When the plan is ready, run [bold]/plan approve[/bold].[/dim]",
                title="Plan Mode",
                border_style="green",
            ))
        elif sub == "approve":
            plan = _review_and_approve_current_plan()
            if plan:
                followup = (
                    f"Execute approved WorkGraph {plan['work_id']} revision "
                    f"{plan['revision']} SHA {plan['content_sha']} for task: "
                    f"{plan['task']}. Follow the injected approved_work_plan exactly."
                )
                queued = _enqueue_user_input(followup)
                console.print(Panel(
                    f"[bold]Plan [green]APPROVED[/green][/bold]\n\n"
                    f"File: {plan['file']}\n\n"
                    + ("[dim]Execution request queued.[/dim]" if queued else
                       "[yellow]Could not queue execution; submit a task referencing this plan.[/yellow]"),
                    title="Plan Approved",
                    border_style="green",
                ))
            else:
                console.print("[yellow]Plan was not approved.[/yellow]")
        elif sub == "submit":
            snapshot = _pm.submit_current_plan()
            if snapshot is None:
                console.print("[red]Plan is not ready to submit.[/red]")
            else:
                _review_and_approve_current_plan()
        elif sub == "revise":
            feedback = _decode_text_arg(plan_args_raw)
            plan = _pm.get_current_plan()
            if plan is None and feedback:
                plan = _pm.begin_amendment()
            if not plan or not feedback:
                console.print("[yellow]Usage: /plan revise <feedback>[/yellow]")
            else:
                snapshot = _pm.get_review_snapshot()
                if snapshot and snapshot["work"].get("status") == "REVIEW_PENDING":
                    rev = snapshot["revision"]
                    _pm.reject_submitted_plan(rev["revision"], rev["content_sha"])
                queued = _enqueue_user_input(
                    f"Revise WorkGraph {plan.get('work_id')} plan using this user feedback: "
                    f"{feedback}. Update the plan, then call plan.submit again.")
                console.print(
                    "[green]Revision feedback queued for the planning AI.[/green]"
                    if queued else "[red]Could not queue revision feedback.[/red]")
        elif sub == "exit":
            plan = _pm.exit_plan_mode(approve=False)
            if plan:
                console.print("[dim]Exited plan mode; the draft remains in the plans directory.[/dim]")
            else:
                console.print("[yellow]No active plan to exit.[/yellow]")
        elif sub == "status":
            plan = _pm.get_current_plan()
            if plan:
                content = _pm.read_plan() or "(empty)"
                _print_long_panel(content, f"Plan: {plan['task'][:60]}")
            else:
                console.print("[dim]Not in plan mode.[/dim]")
        elif sub == "list":
            plans = _pm.list_plans()
            if plans:
                console.print(f"[bold]Saved Plans:[/bold]")
                for p in plans:
                    console.print(f"  [cyan]{p['name']}[/cyan] — {p['title'][:80]}")
            else:
                console.print("[dim]No saved plans.[/dim]")
        else:
            console.print("Usage:\n"
                          "  [bold]/plan enter <task>[/bold] — Enter plan mode\n"
                          "  [bold]/plan submit[/bold]       — Submit immutable revision for review\n"
                          "  [bold]/plan revise <feedback>[/bold] — Ask AI to revise\n"
                          "  [bold]/plan approve[/bold]      — Approve and execute\n"
                          "  [bold]/plan exit[/bold]         — Exit without approving\n"
                          "  [bold]/plan status[/bold]       — Show current plan\n"
                          "  [bold]/plan list[/bold]         — List saved plans")

    elif action == "/evolve":
        sub = parts[1].lower() if len(parts) > 1 else ""
        _, evolve_args_raw = _raw_tail_after_word(raw_args)
        commands = {
            "status", "branches", "open", "chat", "review", "test",
            "activate", "disable", "candidates", "list", "profiles",
            "profile", "use", "rollback", "help",
        }
        runtime = extension_runtime.get_runtime()
        if not sub or sub not in commands:
            idea = (_decode_text_arg(raw_args) if raw_args
                    else "Create a useful project extension")
            branch = evolution_lab.create_branch(idea)
            worker = _evolution_lab_start_worker(branch["id"], session)
            if worker:
                branch = evolution_lab.read_branch(branch["id"]) or branch
            console.print(Panel(
                f"[bold]Evolution Lab branch created[/bold]\n\n"
                f"Branch: [cyan]{branch['id']}[/cyan]\n"
                f"Intent: [bold]{branch.get('intent')}[/bold]\n"
                f"Status: [bold]{branch.get('status')}[/bold]\n"
                f"Idea: {branch.get('description')}\n"
                + (f"Worker: [cyan]{worker}[/cyan]" if worker else
                   "[yellow]Snapshot saved; no active parent agent was available.[/yellow]"),
                title="Evolution Lab", border_style="cyan",
            ))
        elif sub == "status":
            branch = evolution_lab.read_branch()
            profile = evolution_lab.get_active_profile()
            if branch is None:
                console.print("[dim]No active Evolution Lab branch.[/dim]")
            else:
                console.print(Panel(
                    f"Branch: {branch.get('id')}\nIntent: {branch.get('intent')}\n"
                    f"Status: {branch.get('status')}\n"
                    f"Candidate: {branch.get('candidate_id') or '(none)'}\n"
                    f"Profile: {profile.get('name', 'default')}\n"
                    f"Loaded extensions: {len(runtime.list())}",
                    title="Evolution Lab Status", border_style="cyan"))
        elif sub == "branches":
            for branch in evolution_lab.list_branches():
                console.print(
                    f"  [cyan]{branch.get('id')}[/cyan] "
                    f"[{branch.get('intent')}] [dim]{branch.get('status')}[/dim] "
                    f"— {str(branch.get('description') or '')[:80]}")
        elif sub == "open":
            ok, message = evolution_lab.set_active_branch(
                parts[2] if len(parts) > 2 else "")
            console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
        elif sub == "chat":
            branch = evolution_lab.read_branch()
            feedback = _decode_text_arg(evolve_args_raw) if evolve_args_raw else ""
            if branch is None or not feedback:
                console.print("[yellow]Usage: /evolve chat <refinement>[/yellow]")
            else:
                evolution_lab.add_branch_note(branch["id"], feedback)
                worker = _evolution_lab_start_worker(branch["id"], session, feedback)
                console.print(f"[green]Refinement worker: {worker}[/green]" if worker
                              else "[red]Could not start refinement worker.[/red]")
        elif sub in ("candidates", "list"):
            candidates = evolution_lab.list_candidates()
            if not candidates:
                console.print("[dim]No Evolution Lab candidates.[/dim]")
            for candidate in candidates:
                console.print(
                    f"  [cyan]{candidate.get('id')}[/cyan] "
                    f"[{candidate.get('intent')}] [dim]{candidate.get('status')}[/dim] "
                    f"— {candidate.get('target_type')}:{candidate.get('name')}")
        elif sub == "review":
            candidate_id = parts[2] if len(parts) > 2 else evolution_lab.active_candidate_id()
            candidate = evolution_lab.read_candidate(candidate_id)
            if candidate is None:
                console.print("[red]Evolution candidate not found.[/red]")
            else:
                files = "\n\n".join(
                    f"--- {item.get('path')} ---\n{item.get('content', '')}"
                    for item in candidate.get("files") or [])
                runs = candidate.get("test_runs") or []
                report = json.dumps(runs[-1].get("report"), ensure_ascii=False, indent=2) \
                    if runs else "(not tested)"
                _print_long_panel(
                    f"Candidate: {candidate.get('id')}\n"
                    f"Intent: {candidate.get('intent')}\n"
                    f"Target: {candidate.get('target_type')}:{candidate.get('name')}\n"
                    f"SHA-256: {candidate.get('candidate_sha256')}\n"
                    f"Dependencies: {candidate.get('dependencies') or '(none)'}\n\n"
                    f"Description\n{candidate.get('description', '')}\n\n"
                    f"Files\n{files}\n\nLatest test\n{report}",
                    title="Evolution Candidate", border_style="blue")
        elif sub == "test":
            candidate_id = parts[2] if len(parts) > 2 else evolution_lab.active_candidate_id()
            if not candidate_id:
                console.print("[red]No candidate to test.[/red]")
            else:
                ok, message, run = evolution_lab.test_candidate(candidate_id)
                detail = json.dumps((run or {}).get("report"), ensure_ascii=False, indent=2)
                console.print(Panel(detail, title=message,
                                    border_style="green" if ok else "red"))
        elif sub == "activate":
            candidate_id = next((item for item in parts[2:] if item != "--force"), None)
            candidate_id = candidate_id or evolution_lab.active_candidate_id()
            candidate = evolution_lab.read_candidate(candidate_id)
            if candidate is None:
                console.print("[red]Evolution candidate not found.[/red]")
            else:
                preview = (
                    f"Candidate: {candidate_id}\n"
                    f"Target: {candidate.get('target_type')}:{candidate.get('name')}\n"
                    f"SHA-256: {candidate.get('candidate_sha256')}\n"
                    f"Files: {', '.join(item.get('path', '') for item in candidate.get('files') or [])}"
                )
                choice = _blocking_approval_prompt(
                    "Evolution activation", preview,
                    "Activate and hot-load this feature candidate?", allow_always=False)
                if choice != "yes":
                    console.print("[yellow]Evolution activation cancelled.[/yellow]")
                else:
                    ok, message = evolution_lab.activate_candidate(
                        candidate_id, runtime=runtime, force="--force" in parts[2:])
                    console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
        elif sub == "disable":
            if len(parts) < 3:
                console.print("[yellow]Usage: /evolve disable <extension>[/yellow]")
            else:
                choice = _blocking_approval_prompt(
                    "Disable extension", f"Extension: {parts[2]}",
                    "Disable and unload this extension?", allow_always=False)
                if choice == "yes":
                    ok, message = evolution_lab.disable_extension(parts[2], runtime)
                    console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
        elif sub == "profiles":
            for profile in evolution_lab.list_profiles():
                marker = "*" if profile.get("active") else " "
                console.print(
                    f"{marker} [cyan]{profile.get('name')}[/cyan] "
                    f"[dim]{len(profile.get('extensions') or {})} extension(s)[/dim]")
        elif sub == "profile":
            if len(parts) < 4 or parts[2].lower() != "create":
                console.print("[yellow]Usage: /evolve profile create <name> [candidate-id ...][/yellow]")
            else:
                ok, message = evolution_lab.create_profile(parts[3], parts[4:])
                console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
        elif sub == "use":
            if len(parts) < 3:
                console.print("[yellow]Usage: /evolve use <profile>[/yellow]")
            else:
                choice = _blocking_approval_prompt(
                    "Evolution profile", f"Profile: {parts[2]}",
                    "Switch extension profile and hot-reload?", allow_always=False)
                if choice == "yes":
                    ok, message = evolution_lab.switch_profile(parts[2], runtime)
                    console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
        elif sub == "rollback":
            choice = _blocking_approval_prompt(
                "Evolution rollback", "Restore the previous feature state.",
                "Roll back and hot-reload now?", allow_always=False)
            if choice == "yes":
                ok, message = evolution_lab.rollback(runtime)
                console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
        else:
            console.print(
                "[bold]Evolution Lab[/bold]\n"
                "  /evolve <idea>\n  /evolve status|branches|candidates\n"
                "  /evolve chat <refinement>\n  /evolve review|test|activate [id]\n"
                "  /evolve disable <extension>\n"
                "  /evolve profiles|profile create|use|rollback")

    elif action == "/prompt":
        import prompt_opt as _po
        from rich.markup import escape as _escape
        sub = parts[1].lower() if len(parts) > 1 else ""
        _, prompt_args_raw = _raw_tail_after_word(raw_args)
        _legacy_prompt_commands = {
            "feedback", "fail", "optimize", "apply", "discard", "list",
            "skill", "export", "install", "publish",
        }
        _lab_prompt_commands = {
            "status", "branches", "open", "chat", "review", "test",
            "activate", "disable", "patches", "profiles", "profile",
            "use", "rollback", "help",
        }

        # `/prompt` and `/prompt <natural language>` are the primary UX. They
        # capture an immutable incident and launch an isolated diagnosis worker.
        if not sub or sub not in (_legacy_prompt_commands | _lab_prompt_commands):
            description = (_decode_text_arg(raw_args) if raw_args
                           else "Review the latest AI behavior and identify what should improve")
            branch = _prompt_lab_create(description, session)
            worker = branch.get("worker_agent_id")
            console.print(Panel(
                f"[bold]Prompt Lab branch created[/bold]\n\n"
                f"Branch: [cyan]{branch['id']}[/cyan]\n"
                f"Status: [bold]{branch.get('status', 'CAPTURED')}[/bold]\n"
                f"Issue: {_escape(branch.get('description', ''))}\n"
                + (f"Worker: [cyan]{worker}[/cyan]\n" if worker else
                   "[yellow]No active parent agent; snapshot saved without a worker.[/yellow]\n")
                + "\n[dim]The main task and active prompt are unchanged. Use "
                  "/prompt status, /prompt chat <message>, and /prompt review.[/dim]",
                title="Prompt Lab", border_style="cyan",
            ))
        elif sub == "branches":
            branches = prompt_lab.list_branches()
            if not branches:
                console.print("[dim]No Prompt Lab branches in this project.[/dim]")
            else:
                console.print("[bold]Prompt Lab branches[/bold]")
                for branch in branches:
                    console.print(
                        f"  [cyan]{branch.get('id')}[/cyan] "
                        f"[dim]{branch.get('status')}[/dim] — "
                        f"{_escape(str(branch.get('description') or '')[:80])}")
        elif sub == "open":
            if len(parts) < 3:
                console.print("[yellow]Usage: /prompt open <branch-id>[/yellow]")
            else:
                ok, msg = prompt_lab.set_active_branch(parts[2])
                console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
        elif sub == "chat":
            branch = prompt_lab.read_branch()
            message = _decode_text_arg(prompt_args_raw) if prompt_args_raw else ""
            if branch is None:
                console.print("[yellow]No active Prompt Lab branch. Run /prompt <issue> first.[/yellow]")
            elif not message:
                console.print("[yellow]Usage: /prompt chat <feedback or refinement>[/yellow]")
            else:
                prompt_lab.add_branch_note(branch["id"], message, kind="user-feedback")
                child_id = _prompt_lab_start_worker(branch["id"], session, message)
                if child_id:
                    console.print(f"[green]Prompt Lab refinement worker started: {child_id}[/green]")
                else:
                    console.print("[red]Could not start refinement worker; no active parent agent.[/red]")
        elif sub == "status" and prompt_lab.read_branch() is not None:
            branch = prompt_lab.read_branch() or {}
            patch = prompt_lab.read_patch(str(branch.get("candidate_patch_id") or ""))
            profile = prompt_lab.get_active_profile()
            test_status = "not run"
            if patch and patch.get("test_runs"):
                test_status = "passed" if patch["test_runs"][-1].get("passed") else "FAILED"
            console.print(Panel(
                f"Branch: [cyan]{branch.get('id', '?')}[/cyan]\n"
                f"Status: [bold]{branch.get('status', '?')}[/bold]\n"
                f"Candidate: [cyan]{branch.get('candidate_patch_id') or '(none)'}[/cyan]\n"
                f"Tests: [bold]{test_status}[/bold]\n"
                f"Active profile: [cyan]{profile.get('name', 'default')}[/cyan]\n"
                f"Active overlays: {len(profile.get('patches') or [])}",
                title="Prompt Lab Status", border_style="cyan",
            ))
        elif sub == "patches":
            patches = prompt_lab.list_patches()
            if not patches:
                console.print("[dim]No Prompt Lab patches in this project.[/dim]")
            else:
                console.print("[bold]Prompt Lab patches[/bold]")
                for patch in patches:
                    console.print(
                        f"  [cyan]{patch.get('id')}[/cyan] "
                        f"[dim]{patch.get('status')}[/dim] — "
                        f"{_escape(str(patch.get('title') or ''))}")
        elif sub == "review":
            patch_id = parts[2] if len(parts) >= 3 else prompt_lab.active_patch_id()
            patch = prompt_lab.read_patch(patch_id or "") if patch_id else None
            if patch is None:
                # Fall through to the legacy candidate reader for compatibility.
                cid = parts[2] if len(parts) >= 3 else None
                cand = _po.read_candidate(cid)
                if not cand:
                    console.print("[yellow]No Prompt Lab or legacy candidate found.[/yellow]")
                else:
                    _print_long_panel(
                        f"[bold]Legacy candidate:[/bold] {cand.get('id', '?')}\n\n"
                        f"{cand.get('body', '')}",
                        title="Legacy Prompt Candidate", border_style="blue")
            else:
                runs = patch.get("test_runs") or []
                last_report = runs[-1].get("report", "") if runs else "(not tested)"
                test_cases = "\n".join(
                    f"- {case.get('name')}: {case.get('expected')}"
                    for case in patch.get("tests") or []) or "(none)"
                _print_long_panel(
                    f"[bold]Patch:[/bold] {patch.get('id')}\n"
                    f"[bold]Status:[/bold] {patch.get('status')}\n\n"
                    f"[bold]Diagnosis[/bold]\n{patch.get('diagnosis', '')}\n\n"
                    f"[bold]Rationale[/bold]\n{patch.get('rationale', '')}\n\n"
                    f"[bold]Overlay[/bold]\n{patch.get('content', '')}\n\n"
                    f"[bold]Regression cases[/bold]\n{test_cases}\n\n"
                    f"[bold]Latest test report[/bold]\n{last_report}",
                    title="Prompt Lab Review", border_style="blue")
        elif sub == "test":
            patch_id = parts[2] if len(parts) >= 3 else prompt_lab.active_patch_id()
            if not patch_id:
                console.print("[yellow]No candidate patch. Wait for diagnosis or run /prompt chat.[/yellow]")
            else:
                ok, msg = _prompt_lab_start_test(patch_id, session)
                console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
        elif sub == "activate":
            patch_id = next((item for item in parts[2:] if item != "--force"), None)
            patch_id = patch_id or prompt_lab.active_patch_id()
            patch = prompt_lab.read_patch(patch_id or "") if patch_id else None
            if patch is None:
                console.print("[red]Prompt Lab patch not found.[/red]")
            else:
                runs = patch.get("test_runs") or []
                passed = bool(runs and runs[-1].get("passed"))
                if not passed and "--force" not in parts[2:]:
                    console.print("[red]The latest regression run has not passed. Run /prompt test first, or explicitly use --force.[/red]")
                else:
                    ok, preview = prompt_lab.preview_activation(patch_id)
                    if not ok:
                        console.print(f"[red]{_escape(preview)}[/red]")
                    else:
                        if not passed:
                            preview = "WARNING: activating without a passing test.\n\n" + preview
                        choice = _blocking_approval_prompt(
                            "Prompt Lab activation",
                            preview,
                            "Activate this prompt overlay and hot-reload now?",
                            allow_always=False,
                        )
                        if choice != "yes":
                            console.print("[yellow]Prompt activation cancelled.[/yellow]")
                        else:
                            ok, msg = prompt_lab.activate_patch(patch_id)
                            console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
        elif sub == "disable":
            patch_id = parts[2] if len(parts) >= 3 else ""
            if not patch_id:
                console.print("[yellow]Usage: /prompt disable <patch-id>[/yellow]")
            else:
                choice = _blocking_approval_prompt(
                    "Prompt Lab change",
                    f"Patch: {patch_id}",
                    "Disable this overlay and hot-reload now?",
                    allow_always=False,
                )
                if choice != "yes":
                    console.print("[yellow]Prompt change cancelled.[/yellow]")
                else:
                    ok, msg = prompt_lab.disable_patch(patch_id)
                    console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
        elif sub == "profiles":
            for profile in prompt_lab.list_profiles():
                marker = "[green]*[/green]" if profile.get("active") else " "
                console.print(
                    f"{marker} [cyan]{profile.get('name')}[/cyan] "
                    f"[dim]{len(profile.get('patches') or [])} patch(es)[/dim]")
        elif sub == "profile":
            if len(parts) < 4 or parts[2].lower() != "create":
                console.print("[yellow]Usage: /prompt profile create <name> [patch-id ...][/yellow]")
            else:
                ok, msg = prompt_lab.create_profile(parts[3], parts[4:])
                console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
        elif sub == "use":
            if len(parts) < 3:
                console.print("[yellow]Usage: /prompt use <profile>[/yellow]")
            else:
                profile = next((p for p in prompt_lab.list_profiles()
                                if p.get("name") == parts[2]), None)
                if profile is None:
                    console.print(f"[red]Profile {parts[2]} not found.[/red]")
                else:
                    body = (f"Profile: {parts[2]}\nPatches:\n" +
                            "\n".join(f"  - {p}" for p in profile.get("patches") or []))
                    choice = _blocking_approval_prompt(
                        "Prompt Lab profile switch", body,
                        "Switch profile and hot-reload now?", allow_always=False)
                    if choice != "yes":
                        console.print("[yellow]Profile switch cancelled.[/yellow]")
                    else:
                        ok, msg = prompt_lab.switch_profile(parts[2])
                        console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
        elif sub == "rollback":
            choice = _blocking_approval_prompt(
                "Prompt Lab rollback",
                "The latest prompt activation, disable, or profile switch will be reverted.",
                "Roll back and hot-reload now?",
                allow_always=False,
            )
            if choice != "yes":
                console.print("[yellow]Prompt rollback cancelled.[/yellow]")
            else:
                ok, msg = prompt_lab.rollback()
                console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
        elif sub == "help":
            console.print(
                "[bold]Prompt Lab[/bold]\n"
                "  /prompt [issue]                  Capture an incident and start diagnosis\n"
                "  /prompt chat <message>           Refine with AI in the active branch\n"
                "  /prompt status|branches|patches  Inspect project state\n"
                "  /prompt review [patch-id]        Review diagnosis, overlay, and tests\n"
                "  /prompt test [patch-id]          Run an isolated AI regression evaluation\n"
                "  /prompt activate [id] [--force]  Confirm, activate, and hot-reload\n"
                "  /prompt disable <id>             Confirm and disable an overlay\n"
                "  /prompt profiles|use <name>      Manage switchable prompt profiles\n"
                "  /prompt rollback                 Confirm and undo latest prompt change")
        elif sub == "feedback" and len(parts) >= 3:
            desc = _decode_text_arg(prompt_args_raw)
            entry = _po.capture_feedback(desc)
            parent = get_current_agent()
            if parent:
                child_id = _po.spawn_optimizer(
                    entry["id"], parent.id, get_loop_deps(), session)
                if child_id:
                    console.print(Panel(
                        f"[bold]Feedback captured.[/bold] Optimizer spawned in background.\n\n"
                        f"Feedback ID: [cyan]{entry['id']}[/cyan]\n"
                        f"Optimizer agent: [cyan]{child_id}[/cyan]\n\n"
                        f"[dim]The main task continues uninterrupted. The candidate will\n"
                        f"arrive via inbox when ready. Run [bold]/prompt status[/bold] to check.[/dim]",
                        title="Prompt Optimization", border_style="green"))
                else:
                    console.print(Panel(
                        f"[bold]Feedback captured.[/bold] (ID: {entry['id']})\n\n"
                        f"[yellow]Optimizer spawn failed — max depth may be reached.[/yellow]\n"
                        f"[dim]Run [bold]/prompt optimize {entry['id']}[/bold] later from the REPL.[/dim]",
                        title="Prompt Optimization", border_style="yellow"))
            else:
                console.print(Panel(
                    f"[bold]Feedback captured.[/bold] (ID: {entry['id']})\n\n"
                    f"[dim]No active agent to spawn the optimizer from. Run\n"
                    f"[bold]/prompt optimize {entry['id']}[/bold] when an agent is active.[/dim]",
                    title="Prompt Optimization", border_style="cyan"))
        elif sub == "optimize" and len(parts) >= 3:
            fid = parts[2]
            parent = get_current_agent()
            if not parent:
                console.print("[red]No active agent. /hire one first.[/red]")
            else:
                child_id = _po.spawn_optimizer(fid, parent.id, get_loop_deps(), session)
                if child_id:
                    console.print(f"[green]Optimizer spawned: {child_id}[/green]")
                else:
                    console.print("[red]Spawn failed (max depth reached?)[/red]")
        elif sub == "status":
            state = _po.get_optimization_state(
                parts[2] if len(parts) >= 3 else None)
            if not state:
                console.print("[dim]No active prompt optimization.[/dim]")
            else:
                status = state.get("status", "?")
                cid = state.get("candidate_id") or "(none)"
                fid = state.get("feedback_id") or "(none)"
                console.print(Panel(
                    f"Status: [bold]{status}[/bold]\n"
                    f"Feedback: [cyan]{fid}[/cyan]\n"
                    f"Candidate: [cyan]{cid}[/cyan]",
                    title="Prompt Optimization Status", border_style="cyan"))
        elif sub == "review":
            cid = parts[2] if len(parts) >= 3 else None
            cand = _po.read_candidate(cid)
            if not cand:
                console.print("[yellow]No candidate found. Run /prompt feedback first.[/yellow]")
            else:
                patch = cand.get("patch", "")
                rationale = cand.get("body", "")
                _print_long_panel(
                    f"[bold]Candidate:[/bold] {cand.get('id', '?')}\n\n"
                    f"[dim]Rationale:[/dim]\n{rationale}\n\n"
                    f"[dim]Patch (to be appended to cli.prop):[/dim]\n{patch}",
                    title="Prompt Candidate Review", border_style="blue")
                console.print("\n[dim]Run [bold]/prompt apply[/bold] to activate, "
                              "[bold]/prompt discard[/bold] to reject.[/dim]")
        elif sub == "apply":
            cid = next((item for item in parts[2:] if item != "--force"), None)
            cand = _po.read_candidate(cid)
            if not cand:
                console.print("[red]No legacy candidate found to apply.[/red]")
            else:
                choice = _blocking_approval_prompt(
                    "Legacy prompt activation",
                    f"Candidate: {cand.get('id', '?')}\n\n{cand.get('patch', '')}",
                    "Modify cli.prop and hot-reload this legacy patch?",
                    allow_always=False,
                )
                if choice != "yes":
                    console.print("[yellow]Legacy prompt activation cancelled.[/yellow]")
                else:
                    ok, msg = _po.apply_candidate(cid, force="--force" in parts[2:])
                    color = "green" if ok else "red"
                    console.print(f"[{color}]{msg}[/{color}]")
        elif sub == "discard":
            cid = parts[2] if len(parts) >= 3 else None
            choice = _blocking_approval_prompt(
                "Legacy prompt rollback",
                f"Candidate: {cid or '(active legacy candidate)'}",
                "Remove the active legacy patch and hot-reload?",
                allow_always=False,
            )
            if choice != "yes":
                console.print("[yellow]Legacy prompt rollback cancelled.[/yellow]")
            else:
                ok, msg = _po.discard_candidate(cid)
                color = "green" if ok else "red"
                console.print(f"[{color}]{msg}[/{color}]")
        elif sub == "list":
            cands = _po.list_candidates()
            if cands:
                console.print("[bold]All candidates:[/bold]")
                for c in cands:
                    ctype = c.get("type", "cli_prop")
                    extra = ""
                    if ctype == "skill_patch":
                        extra = f" [{c.get('skill_name', '')}/{c.get('skill_file', '')}]"
                    console.print(f"  [cyan]{c['id']}[/cyan] "
                                  f"[dim]{c['status']}[/dim] "
                                  f"{ctype}{extra} "
                                  f"— {c.get('feedback', '')[:40]}")
            else:
                console.print("[dim]No candidates. Run /prompt feedback or /prompt fail to start.[/dim]")
        elif sub == "export" and len(parts) >= 3:
            cid = parts[2]
            out = parts[3] if len(parts) >= 4 else None
            ok, res = _po.export_pack(cid, out)
            color = "green" if ok else "red"
            console.print(f"[{color}]{res}[/{color}]")
        elif sub == "install" and len(parts) >= 3:
            src = parts[2]
            ok, msg, new_cid = _po.install_pack(src)
            color = "green" if ok else "red"
            console.print(f"[{color}]{msg}[/{color}]")
        elif sub == "publish" and len(parts) >= 3:
            cid = parts[2]
            ok, msg = _po.publish_pack(cid, session)
            color = "green" if ok else "yellow"
            console.print(f"[{color}]{msg}[/{color}]")
        elif sub == "fail":
            fields = None
            if prompt_args_raw:
                try:
                    fields = None
                    last_error = None
                    for candidate in _json_arg_candidates(prompt_args_raw):
                        try:
                            fields = json.loads(candidate)
                            break
                        except json.JSONDecodeError as exc:
                            last_error = exc
                    if fields is None and last_error is not None:
                        raise last_error
                except json.JSONDecodeError as exc:
                    console.print(f"[red]Invalid failure-report JSON: {exc}[/red]")
                    console.print("[dim]Use /prompt fail with no arguments for an interactive form.[/dim]")
                    fields = None
                if fields is not None and not isinstance(fields, dict):
                    console.print("[red]Failure report must be a JSON object.[/red]")
                    fields = None
            elif sys.stdin.isatty():
                _stop_bg_input_reader()
                try:
                    console.print(Panel(
                        "Enter a concise failure report. Task and actual behavior are required.",
                        title="Prompt Failure Report", border_style="cyan"))
                    task_text = input("Task: ").strip()
                    expected = input("Expected behavior: ").strip()
                    actual = input("Actual behavior: ").strip()
                    console.print("[dim]Categories: " + "; ".join(_po.FAILURE_CATEGORIES) + "[/dim]")
                    category = input("Category (optional): ").strip()
                    minimal_fix = input("Minimal fix (optional): ").strip()
                    regression = input("Regression tests (optional): ").strip()
                    fields = {
                        "task": task_text, "expected": expected,
                        "actual": actual, "category": category,
                        "minimal_fix": minimal_fix,
                        "regression_tests": regression,
                    }
                except (EOFError, KeyboardInterrupt):
                    fields = None
                finally:
                    _start_bg_input_reader(get_user_message_queue())
            else:
                console.print(Panel(
                    _po.get_failure_template(),
                    title="Failure Report Template (v3)",
                    border_style="cyan"))
                console.print(
                    "[dim]In a non-interactive shell pass a JSON object, for example: "
                    "/prompt fail '{\"task\":\"...\",\"actual\":\"...\"}'[/dim]")

            if fields is not None:
                if not fields.get("task") and not fields.get("actual"):
                    console.print("[red]Task or actual behavior is required; report not saved.[/red]")
                elif (fields.get("category")
                      and fields.get("category") not in _po.FAILURE_CATEGORIES):
                    console.print(f"[red]Invalid category: {fields.get('category')}[/red]")
                    console.print("[dim]Use one of: " + "; ".join(_po.FAILURE_CATEGORIES) + "[/dim]")
                else:
                    entry = _po.capture_structured_failure(fields)
                    parent = get_current_agent()
                    child_id = (_po.spawn_optimizer(
                        entry["id"], parent.id, get_loop_deps(), session)
                        if parent else None)
                    console.print(Panel(
                        f"Feedback ID: [cyan]{entry['id']}[/cyan]\n"
                        + (f"Optimizer: [cyan]{child_id}[/cyan]" if child_id
                           else "Optimizer not started (no active agent)."),
                        title="Failure report saved", border_style="green",
                    ))
        elif sub == "skill":
            sub2 = parts[2].lower() if len(parts) > 2 else ""
            if sub2 == "list":
                patches = _po.list_skill_patches()
                if patches:
                    console.print("[bold]Skill patches:[/bold]")
                    for p in patches:
                        console.print(
                            f"  [cyan]{p['id']}[/cyan] "
                            f"[dim]{p['status']}[/dim] "
                            f"— {p.get('skill_name', '?')}/"
                            f"{p.get('skill_file', '?')} "
                            f"({p.get('mode', '?')})")
                else:
                    console.print("[dim]No skill patches. Run /prompt fail to start diagnosis.[/dim]")
            elif sub2 == "review":
                cid = parts[3] if len(parts) > 3 else None
                patch = _po.read_skill_patch(cid)
                if not patch:
                    console.print("[yellow]No skill patch found. Run /prompt skill list for ids.[/yellow]")
                else:
                    mode = patch.get("mode", "?")
                    if mode == "append":
                        patch_preview = patch.get("patch", "")
                    else:
                        patch_preview = (
                            f"OLD:\n{patch.get('old_string', '')}\n\n"
                            f"NEW:\n{patch.get('new_string', '')}"
                        )
                    _print_long_panel(
                        f"[bold]Skill Patch:[/bold] {patch.get('id', '?')}\n"
                        f"[bold]Skill:[/bold] {patch.get('skill_name', '?')}/{patch.get('skill_file', '?')}\n"
                        f"[bold]Mode:[/bold] {mode}\n\n"
                        f"[dim]Rationale:[/dim]\n{patch.get('body', '')}\n\n"
                        f"[dim]Patch:[/dim]\n{patch_preview}",
                        title="Skill Patch Review", border_style="blue")
                    console.print("\n[dim]Run [bold]/prompt skill apply <id>[/bold] to activate, "
                                  "[bold]/prompt skill discard <id>[/bold] to reject.[/dim]")
            elif sub2 == "apply":
                cid = next((item for item in parts[3:] if item != "--force"), None)
                if not cid:
                    console.print("[red]Usage: /prompt skill apply <id>[/red]")
                else:
                    candidate = _po.read_skill_patch(cid)
                    preview = (
                        f"Patch: {cid}\nSkill: "
                        f"{(candidate or {}).get('skill_name', '?')}/"
                        f"{(candidate or {}).get('skill_file', '?')}\n\n"
                        f"{(candidate or {}).get('patch', '')}"
                    )
                    choice = _blocking_approval_prompt(
                        "Skill prompt activation", preview,
                        "Modify this skill and hot-reload it?", allow_always=False)
                    if choice != "yes":
                        console.print("[yellow]Skill patch activation cancelled.[/yellow]")
                    else:
                        ok, msg = _po.apply_skill_patch(
                            cid, force="--force" in parts[3:])
                        color = "green" if ok else "red"
                        console.print(f"[{color}]{msg}[/{color}]")
            elif sub2 == "discard":
                cid = parts[3] if len(parts) > 3 else None
                if not cid:
                    console.print("[red]Usage: /prompt skill discard <id>[/red]")
                else:
                    choice = _blocking_approval_prompt(
                        "Skill prompt rollback", f"Patch: {cid}",
                        "Restore the skill backup and hot-reload it?", allow_always=False)
                    if choice != "yes":
                        console.print("[yellow]Skill patch rollback cancelled.[/yellow]")
                    else:
                        ok, msg = _po.discard_skill_patch(cid)
                        color = "green" if ok else "red"
                        console.print(f"[{color}]{msg}[/{color}]")
            else:
                console.print("Usage:\n"
                              "  [bold]/prompt skill list[/bold]            — List skill patches\n"
                              "  [bold]/prompt skill review <id>[/bold]     — Review a skill patch\n"
                              "  [bold]/prompt skill apply <id> [--force][/bold] — Apply a skill patch\n"
                              "  [bold]/prompt skill discard <id>[/bold]    — Discard a skill patch")
        else:
            console.print("Usage:\n"
                          "  [bold]/prompt feedback <desc>[/bold]    — Capture feedback & spawn optimizer\n"
                          "  [bold]/prompt fail[/bold]                — Show failure template (v3)\n"
                          "  [bold]/prompt optimize <id>[/bold]       — Spawn optimizer for a feedback id\n"
                          "  [bold]/prompt status[/bold]              — Show optimization status\n"
                          "  [bold]/prompt review [id][/bold]         — Review cli.prop candidate patch\n"
                          "  [bold]/prompt apply [id] [--force][/bold] — Apply candidate to cli.prop\n"
                          "  [bold]/prompt discard [id][/bold]        — Strip applied cli.prop patch\n"
                          "  [bold]/prompt list[/bold]                — List all candidates (cli.prop + skill)\n"
                          "  [bold]/prompt skill list|review|apply|discard <id>[/bold] — Manage skill patches\n"
                          "  [bold]/prompt export <id> [path][/bold]  — Export portable pack\n"
                          "  [bold]/prompt install <path|url>[/bold]  — Import a shared pack\n"
                          "  [bold]/prompt publish <id>[/bold]        — Publish to community")

    elif action == "/work":
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub == "status":
            work = workgraph.get_active_work()
            if not work:
                console.print("[dim]No active WorkGraph in this project.[/dim]")
            else:
                steps = workgraph.list_steps(work["id"])
                done = sum(1 for step in steps if step.get("status") in {"completed", "skipped"})
                console.print(Panel(
                    f"ID: [cyan]{work['id']}[/cyan]\n"
                    f"Objective: {work['objective']}\n"
                    f"Status: [bold]{work['status']}[/bold]\n"
                    f"Revision: {work.get('current_revision') or 0}\n"
                    f"Approved: {work.get('approved_revision') or '(none)'}\n"
                    f"Workflow: {work.get('workflow_template') or '(none)'} / "
                    f"{work.get('workflow_phase') or '(none)'}\n"
                    f"Steps: {done}/{len(steps)} complete",
                    title="Active WorkGraph", border_style="cyan"))
        elif sub == "list":
            items = workgraph.list_work()
            if not items:
                console.print("[dim]No WorkGraph history in this project.[/dim]")
            for item in items:
                console.print(
                    f"  [cyan]{item['id']}[/cyan] [dim]{item['status']}[/dim] — "
                    f"{item['objective'][:100]}")
        elif sub == "resume":
            if len(parts) < 3:
                console.print("[yellow]Usage: /work resume <work-id>[/yellow]")
            else:
                work = workgraph.get_work(parts[2])
                if not work:
                    console.print(f"[red]WorkGraph {parts[2]} not found.[/red]")
                else:
                    workgraph.set_active_work(work["id"])
                    if work["status"] in {"DRAFT", "REVIEW_PENDING", "NEEDS_USER", "BLOCKED"}:
                        import plan_mode as _work_pm
                        _work_pm.attach_work(work["id"])
                    console.print(f"[green]Resumed WorkGraph {work['id']}.[/green]")
        elif sub == "history":
            work = workgraph.get_active_work()
            if not work:
                console.print("[dim]No active WorkGraph.[/dim]")
            else:
                for event in reversed(workgraph.list_events(work["id"], limit=30)):
                    console.print(
                        f"  [dim]{event['id']}[/dim] {event['event_type']} "
                        f"[dim]{json.dumps(event['payload'], ensure_ascii=False)[:120]}[/dim]")
        else:
            console.print("[yellow]Usage: /work [status|list|resume <id>|history][/yellow]")

    elif action == "/task":
        sub = parts[1].lower() if len(parts) > 1 else ""
        _, task_args_raw = _raw_tail_after_word(raw_args)
        _cwd = os.getcwd()

        if sub in ("", "list"):
            _tasks = [t for t in task_manager.list_tasks(cwd=_cwd)
                      if t.get("status") != "deleted"]
            if not _tasks:
                console.print("[dim]No tasks. Use [bold]/task add <subject>[/bold] to create one.[/dim]")
            else:
                _n_pending = sum(1 for t in _tasks if t.get("status") == "pending")
                _n_progress = sum(1 for t in _tasks if t.get("status") == "in_progress")
                _n_done = sum(1 for t in _tasks if t.get("status") == "completed")
                console.print(f"\n[bold]Tasks[/bold] · {_cwd}    "
                               f"[cyan]{_n_pending} pending[/cyan] · "
                               f"[yellow]{_n_progress} in_progress[/yellow] · "
                               f"[green]{_n_done} done[/green]\n")
                _t = Table(show_header=True, header_style="dim", show_lines=False)
                _t.add_column("", width=2)
                _t.add_column("ID", width=5)
                _t.add_column("Subject")
                _t.add_column("Prog", width=5, justify="right")
                _status_by_id = {
                    str(item.get("id")): item.get("status", "pending")
                    for item in _tasks
                }
                for _tk in _tasks:
                    st = _tk.get("status", "pending")
                    mark = "▶" if st == "in_progress" else ("✓" if st == "completed" else "○")
                    style = "yellow" if st == "in_progress" else ("green" if st == "completed" else "cyan")
                    prog = _tk.get("progress", 0)
                    prog_str = f"{prog}%" if prog > 0 else ""
                    blocked = [
                        str(blocker) for blocker in _tk.get("blockedBy", [])
                        if _status_by_id.get(str(blocker), "pending")
                        not in ("completed", "deleted")
                    ]
                    subj = _tk.get("subject", "(untitled task)")
                    if blocked:
                        subj += f" [dim][blocked: {', '.join(blocked)}][/dim]"
                    _t.add_row(f"[{style}]{mark}[/{style}]", _tk["id"], subj, prog_str)
                console.print(_t)
                console.print()

        elif sub == "add":
            subject = _decode_text_arg(task_args_raw)
            if not subject:
                console.print("[yellow]Usage: /task add <subject>[/yellow]")
            else:
                _tk = task_manager.create_task(subject, cwd=_cwd)
                console.print(f"[green]Created task [bold]{_tk['id']}[/bold]: {subject}[/green]")

        elif sub == "show":
            if len(parts) < 3:
                console.print("[yellow]Usage: /task show <id>[/yellow]")
            else:
                _tk = task_manager.get_task(parts[2], cwd=_cwd)
                if _tk is None:
                    console.print(f"[red]Task '{parts[2]}' not found.[/red]")
                else:
                    notes = "\n".join(_tk.get("notes", [])) or "(none)"
                    console.print(Panel(
                        f"Status: {_tk.get('status', 'pending')}\n"
                        f"Progress: {_tk.get('progress', 0)}%\n"
                        f"Blocked by: {', '.join(_tk.get('blockedBy', [])) or '(none)'}\n"
                        f"Blocks: {', '.join(_tk.get('blocks', [])) or '(none)'}\n\n"
                        f"{_tk.get('description', '') or '(no description)'}\n\n"
                        f"[dim]Notes[/dim]\n{notes}",
                        title=f"Task {_tk.get('id')}: {_tk.get('subject', '(untitled)')}",
                        border_style="cyan",
                    ))

        elif sub == "start":
            if len(parts) < 3:
                console.print("[yellow]Usage: /task start <id>[/yellow]")
            else:
                ok, msg, _tk = task_manager.update_task(parts[2], cwd=_cwd, status="in_progress")
                if ok:
                    console.print(f"[yellow]Started task [bold]{_tk['id']}[/bold]: {_tk['subject']}[/yellow]")
                else:
                    console.print(f"[red]{msg}[/red]")

        elif sub == "done":
            if len(parts) < 3:
                console.print("[yellow]Usage: /task done <id>[/yellow]")
            else:
                ok, msg, _tk = task_manager.update_task(parts[2], cwd=_cwd, status="completed", progress=100)
                if ok:
                    console.print(f"[green]Completed task [bold]{_tk['id']}[/bold]: {_tk['subject']}[/green]")
                else:
                    console.print(f"[red]{msg}[/red]")

        elif sub == "del":
            if len(parts) < 3:
                console.print("[yellow]Usage: /task del <id>[/yellow]")
            else:
                ok, msg, _tk = task_manager.update_task(parts[2], cwd=_cwd, status="deleted")
                if ok:
                    console.print(f"[dim]Deleted task [bold]{_tk['id']}[/bold]: {_tk['subject']}[/dim]")
                else:
                    console.print(f"[red]{msg}[/red]")

        elif sub == "progress":
            if len(parts) != 4:
                console.print("[yellow]Usage: /task progress <id> <0-100>[/yellow]")
            else:
                ok, msg, _tk = task_manager.update_task(
                    parts[2], cwd=_cwd, progress=parts[3])
                if ok:
                    console.print(
                        f"[green]Task {_tk['id']} progress: {_tk.get('progress', 0)}%[/green]")
                else:
                    console.print(f"[red]{msg}[/red]")

        elif sub == "note":
            task_id, note_raw = _raw_tail_after_word(task_args_raw)
            note = _decode_text_arg(note_raw)
            if not task_id or not note:
                console.print("[yellow]Usage: /task note <id> <text>[/yellow]")
            else:
                ok, msg, _tk = task_manager.update_task(
                    task_id, cwd=_cwd, notes=note)
                if ok:
                    console.print(f"[green]Note added to task {_tk['id']}.[/green]")
                else:
                    console.print(f"[red]{msg}[/red]")

        elif sub == "subtask":
            parent_id, subject_raw = _raw_tail_after_word(task_args_raw)
            subject = _decode_text_arg(subject_raw)
            if not parent_id or not subject:
                console.print("[yellow]Usage: /task subtask <parent-id> <subject>[/yellow]")
            else:
                ok, msg, _tk = task_manager.update_task(
                    parent_id, cwd=_cwd, addSubtask=subject)
                if ok:
                    child_id = _tk.get("blocks", ["?"])[-1]
                    console.print(
                        f"[green]Created subtask {child_id} under task {_tk['id']}.[/green]")
                else:
                    console.print(f"[red]{msg}[/red]")

        else:
            console.print("[yellow]Usage: [bold]/task[/bold] [list|add|show|start|done|del|progress|note|subtask][/yellow]\n"
                          "  [bold]/task[/bold]               — list all tasks\n"
                          "  [bold]/task add <subject>[/bold] — create a task\n"
                          "  [bold]/task show <id>[/bold]      — show task details\n"
                          "  [bold]/task start <id>[/bold]    — mark as in_progress\n"
                          "  [bold]/task done <id>[/bold]     — mark as completed\n"
                          "  [bold]/task progress <id> <n>[/bold] — update progress\n"
                          "  [bold]/task note <id> <text>[/bold]  — append a note\n"
                          "  [bold]/task subtask <id> <subject>[/bold] — create child task\n"
                          "  [bold]/task del <id>[/bold]      — delete a task")

    elif action == "/workflow":
        import workflow_engine as _we
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub == "start":
            _, start_raw = _raw_tail_after_word(raw_args)
            replace = False
            first, rest = _raw_tail_after_word(start_raw)
            if first == "--replace":
                replace = True
                first, rest = _raw_tail_after_word(rest)
            wf_name = first
            wf_desc = _decode_text_arg(rest)
            if not wf_name or not wf_desc:
                console.print("[yellow]Usage: /workflow start [--replace] <name> \"<description>\"[/yellow]")
                templates = _we.list_workflow_templates()
                console.print(f"[dim]Available: {', '.join(templates)}[/dim]")
            else:
                existing = _we.get_active_workflow()
                wf = _we.start_workflow(wf_name, wf_desc, replace=replace)
                if wf is None:
                    if wf_name not in _we.list_workflow_templates():
                        console.print(f"[red]Unknown workflow: {wf_name}[/red]")
                        console.print(f"[dim]Available: {', '.join(_we.list_workflow_templates())}[/dim]")
                    elif existing and not existing.completed:
                        console.print(
                            f"[yellow]Workflow '{existing.name}' is already active. "
                            "End it first or pass --replace.[/yellow]")
                else:
                    current = wf.current
                    cur_name = current.name if current else "?"
                    console.print(Panel(
                        f"[bold]Workflow Started: [green]{wf_name}[/green][/bold]\n\n"
                        f"Task: {wf_desc}\n"
                        f"Phases: {len(wf.phases)}\n"
                        f"Progress: {wf.progress_str}\n\n"
                        f"[dim]Current phase: [bold]{cur_name}[/bold][/dim]",
                        title="Workflow",
                        border_style="green",
                    ))
        elif sub == "status":
            wf = _we.get_active_workflow()
            if wf is None:
                console.print("[dim]No active workflow.[/dim]")
                console.print(f"[dim]Start one with: /workflow start <name> \"description\"[/dim]")
                console.print(f"[dim]Available: {', '.join(_we.list_workflow_templates())}[/dim]")
            else:
                current = wf.current
                phase_info = ""
                if current:
                    phase_info = f"\nCurrent: [bold]{current.name}[/bold] — {current.description}"
                    if current.allowed_tools:
                        phase_info += f"\nAllowed tools: {', '.join(current.allowed_tools)}"
                    if current.spawn_agents:
                        phase_info += f"\nAuto-spawn roles: {', '.join(current.spawn_agents)}"
                    if current.exit_condition == "user_confirm":
                        phase_info += "\nNext transition: explicit /workflow approve required"
                history = []
                for phase in wf.phases[:wf.current_phase]:
                    summary = wf.phase_states.get(phase.name, {}).get("summary", "")
                    history.append(f"  {phase.name}: {summary or '(completed)'}")
                history_text = ("\n\nCompleted phases:\n" + "\n".join(history)) if history else ""
                console.print(Panel(
                    f"[bold]{wf.name}[/bold] — {wf.description}\n\n"
                    f"Progress: {wf.progress_str}{phase_info}{history_text}",
                    title="Active Workflow",
                    border_style="cyan",
                ))
        elif sub == "advance":
            _, summary_raw = _raw_tail_after_word(raw_args)
            summary = _decode_text_arg(summary_raw)
            try:
                new_phase = _we.advance_phase(summary, force=True)
            except _we.WorkflowTransitionError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
            else:
                wf = _we.get_active_workflow()
                if new_phase is None:
                    if wf and wf.completed:
                        console.print(f"[green]Workflow '{wf.name}' completed![/green]")
                    else:
                        console.print("[yellow]No active workflow or already completed.[/yellow]")
                else:
                    console.print(f"[green]Advanced to phase: [bold]{new_phase.name}[/bold] — {new_phase.description}[/green]")
        elif sub == "approve":
            _, summary_raw = _raw_tail_after_word(raw_args)
            summary = _decode_text_arg(summary_raw)
            wf = _we.get_active_workflow()
            if wf is None or wf.completed:
                console.print("[yellow]No active workflow to approve.[/yellow]")
            else:
                try:
                    new_phase = _we.advance_phase(summary, user_confirmed=True)
                except _we.WorkflowTransitionError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
                else:
                    if new_phase is None:
                        console.print(f"[green]Workflow '{wf.name}' completed.[/green]")
                    else:
                        console.print(
                            f"[green]Approved; advanced to [bold]{new_phase.name}[/bold] "
                            f"— {new_phase.description}[/green]")
        elif sub == "end":
            _, summary_raw = _raw_tail_after_word(raw_args)
            summary = _decode_text_arg(summary_raw)
            wf = _we.get_active_workflow()
            if wf:
                _we.end_workflow(summary)
                console.print(f"[dim]Workflow '{wf.name}' ended.[/dim]")
            else:
                console.print("[dim]No active workflow.[/dim]")
        elif sub == "list":
            templates = _we.list_workflow_templates()
            console.print("[bold]Available workflow templates:[/bold]")
            for t in templates:
                console.print(f"  [cyan]{t}[/cyan]")
        else:
            console.print("Usage:\n"
                          "  [bold]/workflow start <name> \"<desc>\"[/bold] — Start a workflow\n"
                          "  [bold]/workflow status[/bold]                — Show current workflow\n"
                          "  [bold]/workflow advance [summary][/bold]    — Advance to next phase\n"
                          "  [bold]/workflow approve [summary][/bold]    — Confirm a gated phase\n"
                          "  [bold]/workflow end [summary][/bold]        — End workflow\n"
                          "  [bold]/workflow list[/bold]                  — List available workflows")

    elif action == "/debug":
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "clear":
                clear_debug_logs()
                console.print("[green]Debug log cleared.[/green]")
            elif len(parts) >= 3 and sub.isdigit():
                # /debug <N> <filename> — save latest N entries to file
                try:
                    n = int(sub)
                    if n <= 0:
                        raise ValueError("N must be greater than 0")
                    filename = parts[2]
                    raw_export = "--raw" in parts[3:]
                    entries = get_debug_logs()[:n]
                    if not entries:
                        console.print("[yellow]No debug entries to save.[/yellow]")
                    else:
                        filepath = Path(filename).expanduser()
                        lines = []
                        for i, e in enumerate(entries, 1):
                            lines.append(f"{'='*60}")
                            lines.append(f"Entry #{i}  Loop #{e.loop}  {e.timestamp}  Path: {e.current_path}")
                            lines.append(f"{'='*60}")
                            if e.user_input:
                                lines.append(f"\n[User Input]\n{e.user_input[:3000]}")
                            if e.request_body:
                                lines.append(f"\n[Context Sizes] terminal={e.context_sizes.get('terminal', 0)} conversation={e.context_sizes.get('conversation', 0)} memory={e.context_sizes.get('memory', 0)} terminals={e.context_sizes.get('terminals', 0)} prompt={e.context_sizes.get('prompt', 0)}")
                                lines.append(f"\n[Prompt Preview]\n{e.request_body.get('promptPreview', '')[:500]}")
                            if e.reply:
                                lines.append(f"\n[AI Reply]\n{e.reply[:1500]}")
                            if e.command:
                                lines.append(f"\n[Command]\n{e.command}")
                            lines.append(f"\n[Done] {e.done}")
                            if e.error:
                                lines.append("\n[Error] true")
                            if e.billing:
                                cost = e.billing.get("costCents") or 0
                                balance = e.billing.get("balanceCents") or 0
                                lines.append(f"\n[Billing] ${cost / 100:.2f} (balance ${balance / 100:.2f})")
                            if e.exec_command:
                                lines.append(f"\n[Executed] {e.exec_command}")
                                lines.append(f"[Return Code] {e.exec_returncode}")
                                if e.exec_stdout:
                                    lines.append(f"\n[Stdout]\n{e.exec_stdout}")
                                if e.exec_stderr:
                                    lines.append(f"\n[Stderr]\n{e.exec_stderr}")
                            if e.response_raw:
                                try:
                                    raw_json = json.dumps(e.response_raw, ensure_ascii=False, indent=2)
                                except Exception:
                                    raw_json = str(e.response_raw)
                                lines.append(f"\n[Raw Response]\n{raw_json[:2000]}")
                            lines.append("")
                        try:
                            filepath.parent.mkdir(parents=True, exist_ok=True)
                            export_text = '\n'.join(lines)
                            if not raw_export:
                                export_text = _redact_sensitive_text(export_text)
                            filepath.write_text(export_text, encoding='utf-8')
                        except OSError as exc:
                            console.print(f"[red]Could not save debug entries to {filepath}: {exc}[/red]")
                        else:
                            console.print(
                                f"[green]Saved {len(entries)} debug entr"
                                f"{'y' if len(entries) == 1 else 'ies'} to {filepath.absolute()}[/green]")
                            if raw_export:
                                console.print("[yellow]Raw export may contain credentials or private content.[/yellow]")
                            else:
                                console.print("[dim]Common credential fields were redacted. Use --raw only when necessary.[/dim]")
                except (ValueError, IndexError) as exc:
                    console.print(f"[red]Error: {exc}[/red]")
                    console.print("Usage: [bold]/debug <N> <filename>[/bold]")
            else:
                try:
                    idx = int(sub) - 1
                    show_debug_detail(idx)
                except (ValueError, IndexError):
                    console.print(f"[red]Invalid entry number: {sub}[/red]")
                    console.print("Use [bold]/debug[/bold] to browse entries. [bold]/debug <N> <file>[/bold] to save to file.")
        else:
            show_debug_browser_interactive()

    elif action == "/detail":
        # Toggle full vs simplified progress rendering. Off (default) shows a
        # clean one-line-per-tool transcript; on restores full per-line output.
        if len(parts) == 1:
            _cur = bool(get_runtime_config("detail"))
            console.print(f"[dim]Detail mode is [bold]{'on' if _cur else 'off'}[/bold]. "
                          f"Use /detail on or /detail off to change.[/dim]")
        else:
            _sub = parts[1].lower()
            if _sub in ("on", "off"):
                set_runtime_config("detail", _sub == "on")
                console.print(f"[green]Detail mode {_sub}.[/green]")
            else:
                console.print("[red]Usage: /detail [on|off][/red]")

    elif action in ("/station", "/st"):
        # Syntax:
        #   /station                        — current agent → this REPL
        #   /station <agent_id>             — that agent → this REPL
        #   /station <terminal>             — current agent → sub-terminal
        #   /station <agent_id> <terminal>  — that agent → sub-terminal
        # Single-arg form: looked up as agent first; if no such agent exists,
        # treated as a terminal name. Use the two-arg form to disambiguate
        # when names collide.
        agent_id_arg: Optional[str] = None
        if len(parts) == 1:
            name = "term0"  # default: deploy current agent in this REPL
        elif len(parts) == 2:
            candidate = parts[1]
            if get_agent(candidate) is not None:
                # exists as an agent → treat as agent_id, deploy in this REPL
                agent_id_arg = candidate
                name = "term0"
            else:
                # not an agent → treat as a terminal name
                name = candidate
        else:
            agent_id_arg = parts[1]
            name = parts[2]

        # Normalize aliases for the parent REPL
        if name.lower() in ("current", "here", "term0"):
            name = "term0"

        # Resolve target agent
        if agent_id_arg:
            target_agent = get_agent(agent_id_arg)
            if target_agent is None:
                console.print(f"[red]Agent '{agent_id_arg}' not found. Use /hire to create one.[/red]")
                return False
        else:
            target_agent = get_current_agent()
            if target_agent is None:
                console.print("[red]No current agent. /hire one first.[/red]")
                return False

        # If the agent is already deployed somewhere else, refuse so the user is explicit
        existing_home = getattr(target_agent, "home_terminal", None)
        if existing_home and existing_home != name:
            console.print(f"[yellow]Agent {target_agent.id} is already deployed to '{existing_home}'. "
                          f"Use /terminate {existing_home} first.[/yellow]")
            return False

        # Special case: deploy to the parent REPL (term0). Term0 should
        # already have a real persistent bash session created at startup.
        # If it doesn't (crashed or not created), recreate it.
        if name == "term0":
            term0_info = get_terminal("term0")
            if (term0_info is None
                    or term0_info.session is None
                    or not term0_info.session.is_alive()):
                if not IS_WINDOWS:
                    try:
                        _term0 = InteractiveSession(
                            DEFAULT_SHELL, timeout=0, stream_output=False,
                            persistent=True)
                        _term0.start()
                        time.sleep(0.08)
                        if _term0.is_alive():
                            _term0.read_output(timeout=0.1)
                        register_terminal(_term0, DEFAULT_SHELL, 0, name="term0")
                    except Exception:
                        register_terminal(None, "parent-repl", 0, name="term0")
                else:
                    register_terminal(None, "parent-repl", 0, name="term0")
            target_agent.home_terminal = "term0"
            target_agent.stationed_terminal = "term0"
            if target_agent.role != "primary":
                target_agent.role = "deployed"
            switch_to_agent(target_agent.id)
            console.print(f"[green]Stationed [bold]{target_agent.id}[/bold] in this REPL (term0)[/green]")
            console.print("  [dim]Its shell commands dispatch like user input. /agents to switch back.[/dim]")
            return False

        # Sub-terminal path: inspect existing terminal
        existing = get_terminal(name)
        if existing and existing.session and existing.session.is_alive():
            # Re-using an existing live terminal — just attach the agent,
            # don't respawn anything. The agent's shell.exec will route via
            # send_keys + marker-poll into that terminal's PTY.
            station_agent(target_agent.id, name)
            console.print(f"[green]Stationed [bold]{target_agent.id}[/bold] → terminal [bold]{name}[/bold] (existing)[/green]")
            return False
        if existing:
            unregister_terminal(name)

        # Spawn a fresh bash sub-terminal as the agent's execution target.
        # Plain shell (not sub-laintas-cli) — /station is for command
        # execution; /t is for spawning sub-agent laintas-cli terminals.
        shell_cmd = os.environ.get("SHELL", "/bin/bash")
        sub = SubTerminalSession(shell_cmd)
        sub.start()
        time.sleep(0.1)
        if not sub.is_alive():
            console.print(f"[red]Could not start terminal '{name}'.[/red]")
            return False
        sub.read_output(timeout=0.1)
        register_terminal(sub, shell_cmd, 0, name=name)

        station_agent(target_agent.id, name)
        console.print(f"[green]Stationed [bold]{target_agent.id}[/bold] → terminal [bold]{name}[/bold] (bash)[/green]")
        console.print(f"  [dim]Enter with /t {name}[/dim]")

    elif action == "/terminate":
        if len(parts) < 2:
            console.print("[yellow]Usage: /terminate <name>[/yellow]")
        else:
            name = parts[1]
            term = get_terminal(name)
            returned: list[str] = []
            if term:
                for aid in list(term.stationed_agent_ids):
                    unstation_agent(aid)
                    returned.append(aid)
            if unregister_terminal(name):
                if returned:
                    console.print(f"[green]Terminated [bold]{name}[/bold]; returned {', '.join(returned)} to pool[/green]")
                else:
                    console.print(f"[green]Terminated [bold]{name}[/bold][/green]")
            else:
                console.print(f"[red]Terminal '{name}' not found.[/red]")

    elif action == "/send":
        name, send_raw = _raw_tail_after_word(raw_args)
        wait_seconds = 0.8
        if send_raw.startswith("--wait"):
            match = re.match(r"--wait(?:\s+([^\s]+))?\s+(.*)$", send_raw, re.DOTALL)
            if match is None:
                console.print("[yellow]Usage: /send <name> [--wait <seconds>] <command>[/yellow]")
                return False
            try:
                wait_seconds = max(0.0, min(float(match.group(1) or "0.8"), 30.0))
            except ValueError:
                console.print("[red]--wait expects a number between 0 and 30 seconds.[/red]")
                return False
            send_raw = match.group(2)
        cmd = send_raw
        if not name or not cmd:
            console.print("[yellow]Usage: /send <name> <command>[/yellow]")
        else:
            term = get_terminal(name)
            if term is None:
                console.print(f"[red]Terminal '{name}' not found.[/red]")
            elif term.session is None or not term.session.is_alive():
                console.print(f"[yellow]Terminal '{name}' has no active session.[/yellow]")
            else:
                allowed, denial = authorize_direct_command(cmd, os.getcwd())
                if not allowed:
                    console.print(f"[red]{denial}[/red]")
                    return False
                command_lock = getattr(term.session, "command_lock", None)
                with (command_lock if command_lock is not None else nullcontext()):
                    old_len = len(term.session.full_output)
                    term.session.send_keys(cmd + "\n")
                    console.print(f"[dim]Sent to [bold]{name}[/bold]: {cmd[:80]}[/dim]")
                    if wait_seconds >= 0.3:
                        console.print(
                            f"[dim]Waiting up to {wait_seconds:g}s for new output…[/dim]")
                    deadline = time.time() + wait_seconds
                    while time.time() < deadline:
                        remaining = deadline - time.time()
                        term.session.read_output(timeout=min(0.2, max(0.01, remaining)))
                    output = term.session.full_output[old_len:]
                if output.strip():
                    preview = output[-2000:]
                    suffix = "" if len(output) <= 2000 else f"\n[dim]… {len(output) - 2000} earlier new chars omitted[/dim]"
                    console.print(Panel(preview + suffix, title=f"{name} new output"))
                elif wait_seconds == 0:
                    console.print("[dim]Sent asynchronously; use /term to inspect later output.[/dim]")
                else:
                    console.print("[dim]No new output arrived within the wait window.[/dim]")

    elif action == "/hire":
        agent_info = register_agent(depth=0, role="pool")
        agent_info.parent_terminal = "term0"
        console.print(f"[green]Hired [bold]{agent_info.id}[/bold] → added to pool[/green]")
        console.print(f"  [dim]Deploy with /station {agent_info.id} <terminal>  |  Switch with /agents {agent_info.id}[/dim]")

    elif action == "/agents":
        if len(parts) == 1:
            agents = get_all_agents()
            current = get_current_agent()
            if not agents:
                console.print("[dim]No agents.[/dim]")
            else:
                # Categorize by role
                buckets = {"primary": [], "pool": [], "deployed": [], "subagent": [], "other": []}
                for a in agents:
                    role = getattr(a, "role", "pool")
                    if role in buckets:
                        buckets[role].append(a)
                    else:
                        buckets["other"].append(a)

                def _render(a):
                    marker = " [bold cyan]← current[/bold cyan]" if (current and a.id == current.id) else ""
                    status_str = f" [dim]({a.status})[/dim]" if a.status != "idle" else ""
                    inbox_str = f" [dim yellow]inbox={a.inbox.qsize()}[/dim yellow]" if a.inbox.qsize() else ""
                    name_part = f" {a.name}" if a.name and a.name != a.id else ""
                    return marker, status_str, inbox_str, name_part

                if buckets["primary"]:
                    console.print("[bold]── Primary ──[/bold]")
                    for a in buckets["primary"]:
                        marker, st_s, inb, np = _render(a)
                        console.print(f"  [bold]{a.id}[/bold]{np}{st_s}{inb}{marker}")
                if buckets["pool"]:
                    console.print(f"[bold]── Pool ({len(buckets['pool'])} idle) ──[/bold]")
                    for a in buckets["pool"]:
                        marker, st_s, inb, np = _render(a)
                        console.print(f"  [bold]{a.id}[/bold]{np}{st_s}{inb}{marker}")
                if buckets["deployed"]:
                    console.print(f"[bold]── Deployed ({len(buckets['deployed'])}) ──[/bold]")
                    for a in buckets["deployed"]:
                        marker, st_s, inb, np = _render(a)
                        home = getattr(a, "home_terminal", None) or a.stationed_terminal or "?"
                        parent_term = getattr(a, "parent_terminal", None) or "?"
                        console.print(f"  [bold]{a.id}[/bold]{np} → [cyan]{home}[/cyan] [dim](parent={parent_term})[/dim]{st_s}{inb}{marker}")
                if buckets["subagent"]:
                    console.print(f"[bold]── Subagents ({len(buckets['subagent'])}) ──[/bold]")
                    for a in buckets["subagent"]:
                        marker, st_s, inb, np = _render(a)
                        parent = a.parent_id or "?"
                        console.print(f"  [bold]{a.id}[/bold]{np} [dim](depth={a.depth}, parent={parent})[/dim]{st_s}{inb}{marker}")
                if buckets["other"]:
                    console.print("[bold]── Other ──[/bold]")
                    for a in buckets["other"]:
                        marker, st_s, inb, np = _render(a)
                        console.print(f"  [bold]{a.id}[/bold]{np}{st_s}{inb}{marker}")
        elif len(parts) == 2 and parts[1].lower() == "tree":
            console.print(build_agents_tree())
        elif len(parts) == 2:
            agent_id = parts[1]
            if switch_to_agent(agent_id):
                agent = get_agent(agent_id)
                console.print(f"[green]Switched to [bold]{agent.name}[/bold] ({agent.id})[/green]")
            else:
                console.print(f"[red]Agent '{agent_id}' not found. Use /hire to create one.[/red]")
        elif len(parts) >= 3 and parts[1].lower() == "name":
            _, new_name_raw = _raw_tail_after_word(raw_args)
            new_name = _decode_text_arg(new_name_raw)
            current = get_current_agent()
            if current and rename_agent(current.id, new_name):
                console.print(f"[green]Agent renamed to [bold]{new_name}[/bold][/green]")
            else:
                console.print("[red]No current agent to rename.[/red]")
        else:
            console.print("[yellow]Usage: /agents [tree|agent-id|name <new-name>][/yellow]")

    elif action == "/spawn":
        if not raw_args:
            console.print("[yellow]Usage: /spawn [name:] <task...>[/yellow]")
        else:
            # Parse optional "name:" prefix
            rest = raw_args
            m = re.match(r'^(\S+):\s+(.+)$', rest)
            child_name = m.group(1) if m else None
            task = m.group(2) if m else rest
            parent = get_current_agent()
            if parent is None:
                console.print("[red]No current agent. /hire one first.[/red]")
            else:
                child_id = spawn_subagent(
                    parent_id=parent.id, task=task, deps=get_loop_deps(),
                    name=child_name, session=session,
                    events_cb=(lambda evs: agent_registry._push_events(evs))
                                if agent_registry.agent_id else None,
                )
                if child_id is None:
                    console.print(f"[red]Spawn failed (parent={parent.id})[/red]")
                else:
                    console.print(f"[green]Spawned [bold]{child_id}[/bold][/green] "
                                  f"[dim](parent={parent.id}, task={task[:60]})[/dim]")

    elif action == "/tell":
        target_id, message_raw = _raw_tail_after_word(raw_args)
        if not target_id or not message_raw:
            console.print("[yellow]Usage: /tell <agent_id> <message...>[/yellow]")
        else:
            raw = message_raw.strip()
            decoded = _decode_text_arg(message_raw)
            for candidate in (raw, decoded):
                try:
                    body = json.loads(candidate)
                    if not isinstance(body, dict):
                        body = {"kind": "msg", "text": decoded}
                    break
                except (ValueError, TypeError):
                    body = None
            if body is None:
                raw = decoded
                body = {"kind": "msg", "text": raw}
            else:
                raw = decoded if raw != decoded and not raw.lstrip().startswith(("{", "[")) else raw
            body.setdefault("from", "user")
            if send_to_agent(target_id, body):
                console.print(f"[green]→ {target_id}:[/green] {raw[:120]}")
            else:
                console.print(f"[red]Agent '{target_id}' not found or inbox full.[/red]")

    elif action == "/abort":
        if len(parts) < 2:
            console.print("[yellow]Usage: /abort <agent_id>[/yellow]")
        else:
            target_id = parts[1]
            if abort_agent(target_id):
                console.print(f"[yellow]Abort signaled to [bold]{target_id}[/bold][/yellow]")
            else:
                console.print(f"[red]Agent '{target_id}' not found.[/red]")

    elif action == "/tools":
        registry = tools_mod.get_registry()
        groups = registry.list_by_source()
        if not groups:
            console.print("[dim]No tools registered.[/dim]")
        else:
            for src in sorted(groups):
                console.print(f"[bold]{src}[/bold]")
                for t in groups[src]:
                    console.print(f"  [cyan]{t.name}[/cyan] — {t.description}")

    elif action == "/tool":
        tool_name, tool_params_raw = _raw_tail_after_word(raw_args)
        if not tool_name:
            console.print("[yellow]Usage: /tool <name> [json_params][/yellow]")
        else:
            raw = tool_params_raw.strip()
            try:
                params = {}
                last_error = None
                if raw:
                    params = None
                    for candidate in _json_arg_candidates(raw):
                        try:
                            params = json.loads(candidate)
                            break
                        except json.JSONDecodeError as exc:
                            last_error = exc
                    if params is None and last_error is not None:
                        raise last_error
                if not isinstance(params, dict):
                    params = {"value": params}
            except (ValueError, TypeError):
                params = {"raw": raw}
            ctx = tools_mod.ToolCtx(
                deps=get_loop_deps(),
                agent_id=(get_current_agent().id if get_current_agent() else None),
                session=session,
                events_cb=(lambda evs: agent_registry._push_events(evs))
                          if agent_registry.agent_id else None,
                cwd=os.getcwd(),
            )
            result = tools_mod.get_registry().invoke(tool_name, params, ctx)
            try:
                console.print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            except (TypeError, ValueError):
                console.print(repr(result))

    elif action == "/skill":
        # No subcommand → open the interactive manager (same style as /term).
        sub = (parts[1].lower() if len(parts) > 1 else "manager")
        if sub == "manager":
            show_skill_manager()
        elif sub == "list":
            metas = skills_mod.get_all_metadata()
            if not metas:
                console.print(f"[dim]No skills in {skills_mod.SKILLS_DIR}[/dim]")
                console.print("[dim]Create one with: /skill new <name>[/dim]")
            else:
                groups = tools_mod.get_registry().list_by_source()
                for name, meta in sorted(metas.items()):
                    src = f"skill:{name}"
                    tools = groups.get(src, [])
                    console.print(f"[bold]{name}[/bold] [dim]({meta.dir_path})[/dim]")
                    if meta.description:
                        console.print(f"  [dim]{meta.description}[/dim]")
                    for t in tools:
                        console.print(f"  [cyan]{t.name}[/cyan] — {t.description}")
                    if not tools:
                        console.print("  [yellow](standby/documentation-only)[/yellow]")
        elif sub in ("trust", "revoke"):
            if len(parts) < 3:
                console.print(f"[yellow]Usage: /skill {sub} <name>[/yellow]")
            else:
                skill_name = parts[2]
                meta = skills_mod.get_all_metadata().get(skill_name)
                if meta is None:
                    console.print(f"[red]Unknown skill: {skill_name}[/red]")
                else:
                    entrypoint = Path(meta.dir_path) / "skill.py"
                    if not entrypoint.is_file():
                        console.print("[dim]Documentation-only skills do not require executable trust.[/dim]")
                    elif sub == "revoke":
                        skills_mod.unload_skill(skill_name)
                        removed = trust_store.revoke_extension("skill", skill_name)
                        console.print("[green]Skill trust revoked.[/green]" if removed
                                      else "[dim]Skill was not trusted.[/dim]")
                    else:
                        manifest, manifest_error = skills_mod.load_skill_manifest(
                            Path(meta.dir_path), skill_name)
                        if manifest is None:
                            console.print(f"[red]{manifest_error}[/red]")
                            return False
                        status = trust_store.extension_status(
                            "skill", skill_name, entrypoint,
                            (Path(meta.dir_path) / skills_mod.SKILL_MANIFEST,))
                        approved = "--yes" in parts[3:]
                        if not approved and sys.stdin.isatty():
                            approved = _blocking_approval_prompt(
                                "Trust executable skill",
                                f"Skill: {skill_name}\nFile: {entrypoint.resolve()}\n"
                                f"SHA-256: {status.get('sha256', 'unavailable')}\n\n"
                                f"Capabilities: {manifest.get('capabilities', [])}\n\n"
                                "This Python executes with your local account permissions.",
                                "Trust this exact skill hash?", allow_always=False,
                            ) == "yes"
                        if approved:
                            trusted = trust_store.trust_extension(
                                "skill", skill_name, entrypoint,
                                (Path(meta.dir_path) / skills_mod.SKILL_MANIFEST,))
                            console.print(
                                f"[green]Trusted {skill_name} at "
                                f"{trusted['sha256'][:16]}…[/green]")
                        else:
                            console.print("[yellow]Skill not trusted.[/yellow]")
        elif sub in ("load", "unload"):
            if len(parts) < 3:
                console.print(f"[yellow]Usage: /skill {sub} <name>[/yellow]")
            else:
                fn = skills_mod.load_skill if sub == "load" else skills_mod.unload_skill
                ok, msg = fn(parts[2])
                console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
        elif sub == "reload":
            results = skills_mod.reload_all()
            for name, ok, msg in results:
                style = "green" if ok else "red"
                console.print(f"[{style}]{msg}[/{style}]")
            if not results:
                console.print(f"[dim]No skills in {skills_mod.SKILLS_DIR}[/dim]")
        elif sub == "new":
            if len(parts) < 3:
                console.print("[yellow]Usage: /skill new <name>[/yellow]")
            else:
                ok, msg = skills_mod.install_template(parts[2])
                style = "green" if ok else "red"
                console.print(f"[{style}]{msg}[/{style}]")
                if ok:
                    console.print("[dim]Edit skill.py and extension.json, then run /skill trust <name> and /skill load <name>[/dim]")
        elif sub == "dir":
            console.print(str(skills_mod.SKILLS_DIR))
        else:
            console.print("[yellow]Usage: /skill [manager|list|trust <name>|revoke <name>|load <name>|unload <name>|reload|new <name>|dir][/yellow]")

    elif action == "/mcp":
        if not _get_mcp_mod().MCP_AVAILABLE:
            console.print(f"[yellow]mcp SDK not installed: {_get_mcp_mod().MCP_IMPORT_ERROR}[/yellow]")
            console.print("[dim]Install with:  pip install mcp[/dim]")
            return False
        sub = (parts[1].lower() if len(parts) > 1 else "list")
        mgr = _get_mcp_mod().get_manager()
        if sub == "list":
            cfg = mgr.load_config().get("servers", {})
            if not cfg:
                console.print(f"[dim]No servers in {_get_mcp_mod().CONFIG_PATH}[/dim]")
                console.print("[dim]Create one with: /mcp init[/dim]")
            else:
                for name, sc in cfg.items():
                    srv = mgr.servers.get(name)
                    enabled = sc.get("enabled", True)
                    if srv is None:
                        status = "(not connected)" if enabled else "(disabled)"
                        style = "dim"
                    else:
                        status = f"({srv.status}, {len(srv.tools)} tools)"
                        style = "green" if srv.status == "up" else "yellow"
                    cmd_str = sc.get("command", "?")
                    console.print(f"  [{style}]{name}[/{style}] {status} [dim]{cmd_str}[/dim]")
                    if srv and srv.last_error and srv.status != "up":
                        console.print(f"    [red]{srv.last_error}[/red]")
        elif sub in ("trust", "revoke"):
            if len(parts) < 3:
                console.print(f"[yellow]Usage: /mcp {sub} <server>[/yellow]")
            else:
                server_name = parts[2]
                server_cfg = mgr.load_config().get("servers", {}).get(server_name)
                if server_cfg is None:
                    console.print(f"[red]Unknown MCP server: {server_name}[/red]")
                elif sub == "revoke":
                    mgr.disconnect(server_name)
                    removed = trust_store.revoke_extension("mcp", server_name)
                    console.print("[green]MCP trust revoked.[/green]" if removed
                                  else "[dim]MCP server was not trusted.[/dim]")
                else:
                    cfg_path = _get_mcp_mod().CONFIG_PATH
                    status = trust_store.extension_status(
                        "mcp", server_name, cfg_path)
                    argv = [server_cfg.get("command")] + list(server_cfg.get("args") or [])
                    approved = "--yes" in parts[3:]
                    if not approved and sys.stdin.isatty():
                        approved = _blocking_approval_prompt(
                            "Trust MCP server process",
                            f"Server: {server_name}\nCommand: {argv}\n"
                            f"CWD: {server_cfg.get('cwd') or os.getcwd()}\n"
                            f"Explicit env keys: {sorted((server_cfg.get('env') or {}).keys())}\n"
                            f"Capabilities: {server_cfg.get('capabilities', ['core.other'])}\n"
                            f"Config SHA-256: {status.get('sha256', 'unavailable')}",
                            "Trust this server at the current config hash?",
                            allow_always=False,
                        ) == "yes"
                    if approved:
                        trusted = trust_store.trust_extension(
                            "mcp", server_name, cfg_path)
                        console.print(
                            f"[green]Trusted MCP server {server_name} at "
                            f"{trusted['sha256'][:16]}…[/green]")
                    else:
                        console.print("[yellow]MCP server not trusted.[/yellow]")
        elif sub == "tools":
            if len(parts) < 3:
                console.print("[yellow]Usage: /mcp tools <server>[/yellow]")
            else:
                srv_name = parts[2]
                groups = tools_mod.get_registry().list_by_source()
                ts = groups.get(f"mcp:{srv_name}", [])
                if not ts:
                    console.print(f"[dim]No tools for mcp:{srv_name} (not connected?)[/dim]")
                else:
                    for t in ts:
                        console.print(f"  [cyan]{t.name}[/cyan] — {t.description}")
        elif sub == "connect":
            if len(parts) < 3:
                console.print("[yellow]Usage: /mcp connect <server>[/yellow]")
            else:
                ok, msg = mgr.connect(parts[2])
                style = "green" if ok else "red"
                console.print(f"[{style}]{parts[2]}: {msg}[/{style}]")
        elif sub == "disconnect":
            if len(parts) < 3:
                console.print("[yellow]Usage: /mcp disconnect <server>[/yellow]")
            else:
                ok, msg = mgr.disconnect(parts[2])
                style = "green" if ok else "red"
                console.print(f"[{style}]{parts[2]}: {msg}[/{style}]")
        elif sub == "reload":
            results = mgr.reload()
            for n, ok, m in results:
                if n == "(none)":
                    console.print(f"[yellow]{m}[/yellow]")
                    continue
                style = "green" if ok else "yellow"
                console.print(f"[{style}]{n}: {m}[/{style}]")
        elif sub == "init":
            ok, msg = _get_mcp_mod().MCPManager.write_template_config()
            style = "green" if ok else "red"
            console.print(f"[{style}]{msg}[/{style}]")
            if ok:
                console.print("[dim]Edit the file, enable a server, then /mcp reload[/dim]")
        elif sub == "config":
            console.print(str(_get_mcp_mod().CONFIG_PATH))
        else:
            console.print("[yellow]Usage: /mcp {list|trust <n>|revoke <n>|connect <n>|disconnect <n>|reload|tools <n>|init|config}[/yellow]")

    elif action == "/connect":
        # /connect [name] — link THIS terminal to Helpwo (primary or sub);
        # optional name customizes how it appears in Helpwo.
        if agent_registry is None:
            console.print("[red]No agent registry available.[/red]")
        else:
            custom_name = parts[1] if len(parts) >= 2 else None
            if custom_name and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", custom_name):
                console.print("[red]Invalid name. Use letters, numbers, dot, "
                              "underscore, or hyphen (max 64).[/red]")
            elif custom_name and custom_name.lower() == "term0":
                console.print("[red]'term0' is reserved.[/red]")
            else:
                connect_terminal_to_helpwo(agent_registry, session, name=custom_name)

    elif action == "/disconnect":
        if agent_registry is None or not agent_registry.agent_id:
            console.print("[dim]This terminal is not connected to Helpwo.[/dim]")
        elif getattr(agent_registry, "depth", 0) == 0:
            name = agent_registry.agent_name
            agent_registry.unregister()
            agent_registry.agent_id = None
            agent_registry.agent_secret = ""
            console.print(f"[yellow]Primary CLI [bold]{name}[/bold] is now offline in Helpwo "
                          f"(sub-terminal creation from the UI is disabled). "
                          f"Run /connect to link again.[/yellow]")
        else:
            name = (agent_registry.terminal_meta or {}).get("name", agent_registry.agent_name)
            agent_registry.unregister()
            agent_registry.agent_id = None
            agent_registry.agent_secret = ""
            console.print(f"[yellow]Sub-terminal [bold]{name}[/bold] withdrawn from Helpwo. "
                          f"Run /connect to hand it over again.[/yellow]")

    elif action in ("/t", "/term"):
        if len(parts) >= 2 and parts[1].lower() == "rename":
            if len(parts) != 4:
                console.print("[yellow]Usage: /term rename <old> <new>[/yellow]")
            elif not re.fullmatch(r"[A-Za-z0-9._-]+", parts[3]):
                console.print("[red]Terminal names may contain only letters, numbers, dot, underscore, and hyphen.[/red]")
            elif parts[2].lower() == "term0":
                console.print("[red]The primary terminal term0 cannot be renamed.[/red]")
            elif rename_terminal(parts[2], parts[3]):
                console.print(
                    f"[green]Terminal renamed: [bold]{parts[2]}[/bold] → "
                    f"[bold]{parts[3]}[/bold][/green]")
            else:
                console.print(
                    f"[red]Could not rename '{parts[2]}': source missing or target already exists.[/red]")
        elif len(parts) == 2:
            # /t <name> or /term <name> — create sub-terminal (no agent stationed)
            name = parts[1]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.lower() == "term0":
                console.print(
                    "[red]Invalid terminal name. Use letters, numbers, dot, "
                    "underscore, or hyphen; term0 is reserved.[/red]")
                return False
            existing = get_terminal(name)
            if existing and existing.session and not existing.session.is_alive():
                unregister_terminal(name)
                existing = None
            if existing is not None:
                console.print(f"[yellow]Terminal '{name}' already exists. /t to view, /terminate {name} to remove.[/yellow]")
            else:
                lain_cmd = _build_connected_subterminal_cmd(
                    name,
                    agent_registry.agent_id if agent_registry else None,
                )
                sub = SubTerminalSession(lain_cmd)
                sub.start()
                time.sleep(0.1)
                if not sub.is_alive():
                    console.print(f"[red]Could not start terminal '{name}'.[/red]")
                    return False
                sub.read_output(timeout=0.1)
                register_terminal(sub, "laintas-cli", 0, name=name)
                console.print(f"[green]Created sub-terminal [bold]{name}[/bold] (no agent stationed)[/green]")
        elif len(parts) > 2:
            console.print("[yellow]Usage: /term [name|rename <old> <new>][/yellow]")
        else:
            # /t or /term (no args) — list terminals browser
            terminals = get_all_terminals()
            has_primary = interactive_session is not None and interactive_session.is_alive()
            if not terminals and not has_primary:
                console.print("[dim]No active sub-terminal sessions. "
                              "Use /station or let the AI spawn a command.[/dim]")
            elif not terminals and has_primary:
                # Only term0 exists — entering it is redundant (already in REPL).
                console.print("[dim]No sub-terminals. You are already in term0 (primary).[/dim]")
            else:
                show_terminal_manager(interactive_session)

    elif action == "/reload":
        if raw_args:
            console.print("[yellow]Usage: /reload[/yellow]")
        else:
            reload_default_files()

    elif action in ("/undo", "/snapshot", "/snapshots"):
        import snapshot as _snap
        cwd = os.getcwd()
        if action == "/snapshots":
            cps = _snap.list_for(cwd)
            if not cps:
                console.print("[dim]No checkpoints for this repository "
                              "(not a git repo, or none taken yet).[/dim]")
            else:
                console.print(f"[bold]Checkpoints[/bold] ({len(cps)}, newest last):")
                for i, c in enumerate(cps):
                    ago = _format_time_ago(c.get("ts", 0))
                    console.print(f"  [cyan]{c['sha'][:10]}[/cyan]  {c.get('label','') or '(no label)'}  [dim]{ago}[/dim]")
                console.print("[dim]Use /undo to restore the latest, or /undo <sha> for a specific one.[/dim]")
        elif action == "/snapshot":
            label = _decode_text_arg(raw_args) if raw_args else "manual"
            cp = _snap.create(cwd, label)
            if cp:
                console.print(f"[green]Checkpoint saved: {cp['sha'][:10]} ({label})[/green]")
            else:
                console.print("[yellow]Could not snapshot (not a git repository?).[/yellow]")
        else:  # /undo
            sha = parts[1] if len(parts) > 1 else None
            ok, msg = _snap.restore(cwd, sha)
            console.print((f"[green]{msg}[/green]" if ok else f"[yellow]{msg}[/yellow]"))
            if ok:
                console.print("[dim]Files created since the checkpoint were kept. "
                              "A pre-undo checkpoint was saved (undo the undo with /undo).[/dim]")

    elif action == "/config":
        # Built-in config command (doesn't require .laintas/commands.py)
        if len(parts) == 1:
            table = Table(title="Runtime Configuration", show_lines=False)
            table.add_column("Key", style="cyan")
            table.add_column("Value")
            table.add_column("Type", style="dim")
            table.add_column("Source", style="dim")
            table.add_column("Description", style="dim")
            for key, meta in sorted(describe_runtime_config().items()):
                table.add_row(
                    key, repr(meta["value"]), meta["type"],
                    "override" if meta["overridden"] else "default",
                    meta["description"],
                )
            console.print(table)
            console.print("[dim]Set with /config <key> <value>; restore with /config reset.[/dim]")
        elif len(parts) == 2 and parts[1].lower() == "reset":
            reset_runtime_config()
            console.print("[green]Runtime config reset to defaults.[/green]")
        elif len(parts) == 2:
            # /config <key> — show one
            key = parts[1]
            meta = describe_runtime_config().get(key)
            if meta is None:
                console.print(f"[red]Unknown config key: {key}[/red]")
                console.print("[dim]Run /config to list valid keys.[/dim]")
            else:
                console.print(Panel(
                    f"Value: [bold]{meta['value']!r}[/bold]\n"
                    f"Default: {meta['default']!r}\n"
                    f"Type: {meta['type']}\n"
                    f"Source: {'override' if meta['overridden'] else 'default'}\n\n"
                    f"[dim]{meta['description']}[/dim]",
                    title=key, border_style="cyan",
                ))
        elif len(parts) == 3:
            # /config <key> <value>
            key = parts[1]
            try:
                if not set_runtime_config(key, parts[2]):
                    console.print(f"[red]Unknown config key: {key}[/red]")
                    console.print("[dim]Run /config to list valid keys.[/dim]")
                else:
                    value = get_runtime_config(key)
                    console.print(
                        f"[green]{key} = {value!r} ({type(value).__name__})[/green]")
            except (ValueError, KeyError) as e:
                console.print(f"[red]{e}[/red]")
                console.print(f"[dim]Run /config {key} to inspect the expected type.[/dim]")
        else:
            console.print("[yellow]Usage: /config [key [value]] | /config reset[/yellow]")

    elif action == "/max":
        # Crank every capacity knob to its ceiling and lift every auto-exit
        # circuit breaker. Process-global → applies to all agents. /config reset reverts.
        applied = apply_max_config()
        console.print("[green]⚡ MAX mode — all limits lifted (applies to every agent):[/green]")
        for k, v in applied.items():
            console.print(f"  [cyan]{k}[/cyan] = {v}")
        console.print("[dim]Note: max_tokens may be capped lower by the provider. Revert with /config reset.[/dim]")

    elif action == "/continue":
        _current_live = getattr(handle_meta_command, '_current_live_session', None)
        _prev_state = getattr(handle_meta_command, '_last_agent_state', None)
        _prev_chat = getattr(handle_meta_command, '_last_chat_history', None)
        _prev_input = getattr(handle_meta_command, '_last_original_input', None)
        _prev_deps = getattr(handle_meta_command, '_last_deps', None) or get_loop_deps()
        _prev_session = getattr(handle_meta_command, '_last_session', None) or session
        _prev_events_cb = getattr(handle_meta_command, '_last_events_cb', None)
        _prev_existing_session = getattr(handle_meta_command, '_last_existing_session', None)

        if _current_live:
            # Prefer the mutable objects used by the main REPL.  The persisted
            # live-session payload is a restart fallback, not a second runtime
            # source of truth.
            if _prev_state is None:
                _prev_state = (_current_live.get("state")
                               or _current_live.get("agent_state"))
            if _prev_chat is None:
                _prev_chat = _current_live.get("chat_history") or []
            _prev_input = (_current_live.get("last_original_input")
                           or _current_live.get("last_user_input")
                           or _prev_input
                           or _current_live.get("objective"))

        if _prev_state is None or _prev_input is None:
            console.print("[yellow]No previous agent loop to continue.[/yellow]")
            return False

        if _current_live and not _current_live.get("pending_continuation"):
            console.print("[yellow]The current session has no pending continuation.[/yellow]")
            return False

        if not _prev_events_cb:
            def _prev_events_cb(events: list):
                if agent_registry.agent_id:
                    agent_registry._push_events(events)

        _prev_state.pop("_max_loops_exhausted", None)
        _prev_state.pop("_exhaustion_loop_count", None)

        console.print("[green]Continuing current session...[/green]")

        response = _run_agent_loop_with_interrupt(
            _prev_deps, _prev_input, _prev_session, _prev_state,
            _prev_chat if _prev_chat is not None else [],
            events_cb=_prev_events_cb,
            existing_session=_prev_existing_session,
            continue_thread=True,
        )

        _continued_state = prepare_state_for_repl(
            response.get("state", _prev_state))
        if isinstance(_prev_state, dict):
            _prev_state.clear()
            _prev_state.update(_continued_state)
            _continued_state = _prev_state

        handle_meta_command._last_agent_state = _continued_state
        handle_meta_command._last_chat_history = _prev_chat
        handle_meta_command._last_original_input = _prev_input
        handle_meta_command._last_deps = _prev_deps
        handle_meta_command._last_session = _prev_session
        handle_meta_command._last_events_cb = _prev_events_cb
        handle_meta_command._last_existing_session = response.get("session")
        if response.get("msg"):
            console.print(_prev_deps.Markdown(response["msg"]) if hasattr(_prev_deps, 'Markdown') else response["msg"])
            (_prev_chat if _prev_chat is not None else []).append(
                {"role": "assistant", "content": response["msg"]}
            )
        if _current_live:
            try:
                updated = session_store.sync_runtime(
                    _current_live,
                    _continued_state,
                    _prev_chat if _prev_chat is not None else [],
                    cwd=_current_live.get("cwd") or os.getcwd(),
                    objective=_prev_input,
                    last_user_input=_prev_input,
                    exit_reason=response.get("exit_reason"),
                    tasks=task_manager.export_active_tasks(cwd=_current_live.get("cwd") or os.getcwd()),
                )
                handle_meta_command._current_live_session = updated
            except Exception:
                pass

        return False

    elif action == "/told":
        from rich.markup import escape
        sub = parts[1].lower() if len(parts) > 1 else ""
        _chat = getattr(handle_meta_command, '_last_chat_history', None) or []
        _user_msgs = [m.get("content", "") for m in _chat
                      if isinstance(m, dict) and m.get("role") == "user"]

        def _parse_n(token, default):
            try:
                n = int(token)
                return n if n > 0 else default
            except (TypeError, ValueError):
                return default

        if sub == "":
            if _user_msgs:
                console.print("[bold]You last asked:[/bold]")
                console.print(f"  [cyan]{escape(_user_msgs[-1])}[/cyan]")
            else:
                _fallback = getattr(handle_meta_command, '_last_original_input', None)
                if _fallback:
                    console.print("[bold]You last asked:[/bold]")
                    console.print(f"  [cyan]{escape(_fallback)}[/cyan]")
                else:
                    console.print("[yellow]You haven't asked anything yet this session.[/yellow]")
                    console.print("[dim]Tip: /told log shows prompts from the on-disk journal "
                                  "(survives /new and restarts).[/dim]")

        elif sub == "all":
            if not _user_msgs:
                console.print("[yellow]No user messages in this session yet.[/yellow]")
                console.print("[dim]Tip: /told log reads the durable per-cwd journal.[/dim]")
            else:
                console.print(f"[bold]All your messages ({len(_user_msgs)}):[/bold]")
                for i, msg in enumerate(_user_msgs, 1):
                    console.print(f"  [dim][{i}][/dim] {escape(msg)}")

        elif sub == "reply":
            n = _parse_n(parts[2] if len(parts) > 2 else "", 1)
            _turns = []
            i = 0
            while i < len(_chat):
                m = _chat[i]
                if isinstance(m, dict) and m.get("role") == "user":
                    asst = None
                    j = i + 1
                    while j < len(_chat):
                        am = _chat[j]
                        if isinstance(am, dict) and am.get("role") == "assistant":
                            asst = am.get("content", "")
                            break
                        if isinstance(am, dict) and am.get("role") == "user":
                            break
                        j += 1
                    _turns.append((m.get("content", ""), asst or "[no reply]"))
                    i = j + 1 if asst is not None else j
                else:
                    i += 1
            if not _turns:
                console.print("[yellow]No conversation turns to replay.[/yellow]")
                console.print("[dim]Tip: /told log reads the durable per-cwd journal.[/dim]")
            else:
                recent = _turns[-n:] if n < len(_turns) else _turns
                label = "Last turn" if len(recent) == 1 else f"Last {len(recent)} turns"
                console.print(f"[bold]── {label} ──[/bold]")
                for idx, (u, a) in enumerate(recent, 1):
                    console.print(f"[bold]You:[/bold]        [cyan]{escape(u)}[/cyan]")
                    console.print(f"[bold]Assistant:[/bold]   [green]{escape(a)}[/green]")
                    if idx < len(recent):
                        console.print()

        elif sub == "log":
            n = _parse_n(parts[2] if len(parts) > 2 else "", 10)
            _log_path = paths.project_dir() / "events.jsonl"
            _entries = []
            try:
                if _log_path.exists():
                    with open(_log_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                evt = json.loads(line)
                            except (ValueError, TypeError):
                                continue
                            if isinstance(evt, dict) and evt.get("type") == "prompt_admitted":
                                _entries.append(evt)
            except OSError:
                pass
            if not _entries:
                console.print("[yellow]No prompts recorded in the journal yet.[/yellow]")
                console.print(f"[dim]Journal: {escape(str(_log_path))}[/dim]")
            else:
                recent = _entries[-n:] if n < len(_entries) else _entries
                console.print(f"[bold]Recent prompts (from journal):[/bold]")
                for evt in recent:
                    ts = evt.get("ts")
                    tstr = ""
                    if isinstance(ts, (int, float)) and ts > 0:
                        try:
                            tstr = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                        except (OSError, ValueError, OverflowError):
                            tstr = ""
                    text = evt.get("text", "") or ""
                    if tstr:
                        console.print(f"  [dim]{tstr}[/dim]  {escape(text)}")
                    else:
                        console.print(f"  {escape(text)}")

        else:
            try:
                n = int(sub)
            except ValueError:
                console.print(f"[red]Unknown subcommand: {escape(sub)}[/red]")
                console.print("[yellow]Usage: /told [N|all|reply [N]|log [N]][/yellow]")
                return False
            if n <= 0:
                console.print("[yellow]N must be a positive integer.[/yellow]")
                return False
            if not _user_msgs:
                console.print("[yellow]No user messages in this session yet.[/yellow]")
                console.print("[dim]Tip: /told log reads the durable per-cwd journal.[/dim]")
            else:
                recent = _user_msgs[-n:] if n < len(_user_msgs) else _user_msgs
                label = "Last message" if len(recent) == 1 else f"Last {len(recent)} messages"
                console.print(f"[bold]{label}:[/bold]")
                for i, msg in enumerate(recent, 1):
                    console.print(f"  [dim][{i}][/dim] {escape(msg)}")

    elif action == "/hwo":
        sub = parts[1].lower() if len(parts) > 1 else ""
        current = get_current_agent()
        if sub in ("run", "compile") and len(parts) >= 3:
            # /hwo run <path>  or  /hwo compile <path>
            import hwo_runner
            path = " ".join(parts[2:])
            if sub == "compile":
                r = hwo_runner.compile_hwo_file(path)
            else:
                r = hwo_runner.run_hwo_file(
                    path=path,
                    deps=get_loop_deps(),
                    session=session,
                    parent_id=current.id if current else None,
                )
            style = "[green]" if r.get("ok") else "[red]"
            console.print(f"{style}{r.get('msg', '')}[/]")
        elif len(parts) >= 2 and sub not in ("run", "compile"):
            # /hwo <file>  — load .hwo into TUI
            file_path = " ".join(parts[1:])
            loaded, err = hwo_ui_mod.load_hwo_file(file_path)
            if err:
                console.print(f"[red]hwo: {err}[/]")
            else:
                root_name = current.name if current else "primary"
                hwo_ui_mod.run_hwo_ui(
                    root_name,
                    deps=get_loop_deps(),
                    session_data=session,
                    parent_id=current.id if current else None,
                    initial_session=loaded,
                )
        else:
            # /hwo  — blank TUI
            root_name = current.name if current else "primary"
            hwo_ui_mod.run_hwo_ui(
                root_name,
                deps=get_loop_deps(),
                session_data=session,
                parent_id=current.id if current else None,
            )

    elif action in ("/v", "/version", "/update"):
        # /v, /version → show version + check; /update is shorthand for /v update.
        if action == "/update":
            if not parts[1:]:
                handle_version_command(["/v", "update"])
            elif parts[1].lower() in ("--force", "-f"):
                handle_version_command(["/v", "update"] + parts[1:])
            elif len(parts) == 2 and parts[1].lower() == "check":
                handle_version_command(["/v", "check"])
            else:
                console.print("[yellow]Usage: /update [--force]  |  /update check[/yellow]")
        else:
            handle_version_command(parts)

    else:
        # Evolution Lab extensions register project-local slash commands here.
        handled, extension_result = extension_runtime.get_runtime().invoke_command(
            action, parts, cmd)
        if handled:
            if extension_result is not None:
                console.print(extension_result)
            return False
        # Try .laintas/commands.py custom handler first
        handler = _load_extra_commands()
        if handler:
            ctx = {
                # Authentication state is intentionally never exposed to
                # project customization, even after workspace trust approval.
                "session": {}, "interactive_session": interactive_session,
                "raw_line": cmd, "raw_args": raw_args,
                "agent_registry": None, "console": console,
                "get_terminal": get_terminal, "get_all_terminals": get_all_terminals,
                "unregister_terminal": unregister_terminal, "register_terminal": register_terminal,
                "rename_terminal": rename_terminal,
                "get_agent": get_agent, "get_all_agents": get_all_agents,
                "get_current_agent": get_current_agent,
                "station_agent": station_agent, "unstation_agent": unstation_agent,
                "SubTerminalSession": SubTerminalSession,
                "observe_session": observe_session,
                "enter_session": enter_session,
                "_show_terminal_detail": _show_terminal_detail,
                "get_config": get_runtime_config,
                "set_config": set_runtime_config,
                "list_config": list_runtime_config,
                "reset_config": reset_runtime_config,
                "reload_default_files": reload_default_files,
            }
            try:
                if handler(action, parts, ctx):
                    return False
            except Exception as e:
                console.print(f"[red].laintas/commands.py error: {e}[/red]")
        console.print(f"[red]Unknown command: {action}[/red]")
        console.print("Type [bold]/help[/bold] for available commands.")

    return False


# ── Command palette registry ───────────────────────────────────────────
# Generated from COMMAND_SPECS so help, completion, and palette cannot drift.

def handle_meta_command(cmd: str, agent_registry: AgentRegistry, session: dict,
                        interactive_session=None) -> bool:
    """Exception-safe public slash-command dispatcher."""
    try:
        return _handle_meta_command_impl(
            cmd, agent_registry, session, interactive_session)
    except SlashCommandUsageError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
    except KeyboardInterrupt:
        console.print("[dim]Command cancelled.[/dim]")
    except Exception as exc:
        try:
            action = (cmd or "").strip().split(maxsplit=1)[0]
        except Exception:
            action = "/"
        console.print(
            f"[red]{action or '/'} failed: {type(exc).__name__}: {exc}[/red]")
        try:
            add_debug_log(DebugEntry(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                user_input=cmd,
                current_path=os.getcwd(),
                reply=f"{type(exc).__name__}: {exc}",
                command=action,
                error=True,
            ))
        except Exception:
            pass
        console.print(
            f"[dim]The CLI is still running. Use /help {action} to check usage, "
            "or /debug to inspect recent activity.[/dim]")
    return False


_COMMANDS = [
    (spec.name, spec.description)
    for spec in COMMAND_SPECS
    if spec.palette
]


def show_command_palette():
    """Interactive full-screen command selector — fuzzy filter, arrow keys, Enter.

    Returns the selected command string (e.g. \"/help\") or None if cancelled.
    """
    items = [(f"[cyan]{name}[/cyan]", desc) for name, desc in _COMMANDS]
    chosen = select_dialog(
        items,
        title="Commands — type to filter",
        full_screen=True,
        search=True,
        hint="↑↓ navigate  ↵ select  Esc cancel",
    )
    if chosen is None:
        return None
    return chosen[0]


def show_help(command: str = ""):
    """Display generated command help, optionally for one command."""
    from rich.markup import escape
    if command:
        spec = _find_command_spec(command)
        if spec is None:
            console.print(f"[red]Unknown command: {escape(command)}[/red]")
            console.print("[dim]Run /help to list available commands.[/dim]")
            return
        aliases = ", ".join(spec.aliases) or "(none)"
        usage = spec.usage or spec.name
        console.print(Panel(
            f"[bold]{escape(usage)}[/bold]\n\n"
            f"{escape(spec.description)}\n\n"
            f"[dim]Aliases: {escape(aliases)}[/dim]",
            title=escape(spec.name), border_style="cyan",
        ))
        return

    console.print()
    group_order = list(dict.fromkeys(spec.group for spec in COMMAND_SPECS))
    for title in group_order:
        rows = []
        if title == "Basics":
            rows.extend([
                ("ls, git, …", "PATH commands run directly"),
                ("<text>", "plain text → AI agent loop"),
            ])
        for spec in COMMAND_SPECS:
            if spec.group != title:
                continue
            label = spec.usage or spec.name
            if spec.aliases:
                label += f"  ({', '.join(spec.aliases)})"
            rows.append((label, spec.description))
        console.print(f"  [accent]{title}[/accent]")
        cmd_w = max(len(c) for c, _ in rows)
        for cmd, desc in rows:
            padded = escape(cmd.ljust(cmd_w))
            console.print(f"    [accent.dim]{padded}[/accent.dim]  [muted]{escape(desc)}[/muted]")
        console.print()


# ── LoopDeps factory (lazy init after all functions defined) ─────────

_loop_deps: Optional[LoopDeps] = None

def get_loop_deps() -> LoopDeps:
    """Return the singleton LoopDeps, creating it on first call."""
    global _loop_deps
    if _loop_deps is None:
        _loop_deps = LoopDeps(
            read_file=read_file,
            append_file=append_file,
            write_file=write_file,
            strip_ansi=strip_ansi,
            generate_prompt=generate_cli_prop_template,
            call_backend=call_backend_stream,
            SubTerminalSession=SubTerminalSession,
            display_command_output=display_command_output,
            display_sub_terminal_preview=display_sub_terminal_preview,
            display_file_diff=display_file_diff,
            console=console,
            Markdown=Markdown,
            pty_passthrough=pty_passthrough,
            build_subterminal_cmd=_build_subterminal_cmd,
            request_command_approval=request_command_approval,
            request_file_write_approval=request_file_write_approval,
            request_file_delete_approval=request_file_delete_approval,
        )
    return _loop_deps


# ── Main ───────────────────────────────────────────────────────────────

_LOGO_LINES = [
    " ╷    ╭─╮  ┬  ╷ ╷ ┌┬┐  ╭─╮  ╭─╮",
    " │    ├─┤  │  │╲│  │   ├─┤  ╰─╮",
    " ╰──  ╵ ╵  ┴  ╵ ╵  ┴   ╵ ╵  ╰─╯",
]


def _shorten_path(p: str, max_len: int = 48) -> str:
    home = os.path.expanduser("~")
    if p.startswith(home):
        p = "~" + p[len(home):]
    if len(p) > max_len:
        p = "…" + p[-(max_len - 1):]
    return p


def show_banner(agent_name: str, session: dict = None):
    """Display a minimal, art-font startup banner."""
    shell_info = "cmd.exe" if IS_WINDOWS else SHELL_NAME

    for line in _LOGO_LINES:
        console.print(f"[accent]{line}[/accent]")
    console.print(
        f"  [muted]cli[/muted] [accent.dim]v{__version__}[/accent.dim]"
        f"  [muted]·[/muted]  [agent]{agent_name}[/agent]"
    )
    console.print()

    rows = []
    if session:
        acct = (session.get("userEmail") or session.get("userName")
                or session.get("userId") or "")
        if acct:
            rows.append(("account", acct))
    rows.append(("system", f"{SYSTEM} · {shell_info}"))
    rows.append(("cwd", _shorten_path(os.getcwd())))
    _backend_profile = get_backend_profile()
    rows.append(("backend", f"{_backend_profile.base_url} [{_backend_profile.kind}; {_backend_profile.billing_label}]"))

    # Agent behavior and security policy are separate concepts.
    try:
        import plan_mode as _pm
        agent_mode = (
            "plan" if _pm.is_plan_mode()
            else mode_manager.get_active_mode()["name"]
        )
        rows.append(("mode", agent_mode))
    except Exception:
        pass
    try:
        import policy as _pol
        _mode = _pol.get_config().get("mode", "audit")
        _mode_style = {"audit": "cyan", "enforce": "yellow",
                       "disabled": "red"}.get(_mode, "cyan")
        rows.append(("policy", f"[{_mode_style}]{_mode}[/{_mode_style}]"))
    except Exception:
        pass
    status_parts = []
    try:
        _open = [t for t in task_manager.list_tasks(cwd=os.getcwd())
                 if t.get("status") in ("pending", "in_progress")]
        if _open:
            status_parts.append(f"tasks: [accent]{len(_open)} open[/accent]")
    except Exception:
        pass
    if status_parts:
        rows.append(("status", "  ".join(status_parts)))

    label_w = max(len(k) for k, _ in rows)
    for k, v in rows:
        console.print(f"  [muted]{k.rjust(label_w)}[/muted]  [accent.dim]│[/accent.dim] {v}")

    console.print()
    console.print(
        "  [muted]PATH commands run directly · plain text → AI · "
        "[/muted][accent]/help[/accent][muted] for commands · "
        "[/muted][accent]/mode[/accent][muted] plan · [/muted][accent]/policy[/accent][muted] approvals[/muted]"
    )
    console.print()


def _simple_prompt(cwd: str) -> str:
    """Plain input() prompt for non-TTY environments (piped stdin, --execute mode).

    Only used when stdin is NOT a real terminal. Sub-terminals created via
    SubTerminalSession (fork+pty) have a real PTY slave as stdin, so they
    use the full prompt_toolkit (pt_prompt) with arrow key support.
    """
    try:
        print(f"{cwd}\n$ ", end="", flush=True)
        raw = sys.stdin.buffer.readline()
        return raw.decode("utf-8", errors="replace").strip()
    except (KeyboardInterrupt, EOFError):
        return ""


# ── Remote Message Injection ──────────────────────────────────────────────
# Messages from HelpwoAI (poll thread) are injected into the main REPL loop
# so they go through the exact same input→route→execute pipeline as local
# user input. A wakeup pipe unblocks the main thread when a message arrives.

class _InjectedInput:
    """A message injected from the remote poll thread into the main loop."""
    __slots__ = ("text", "done")
    def __init__(self, text: str, done: threading.Event):
        self.text = text
        self.done = done


_injected_input_queue: queue.Queue = queue.Queue()
_wakeup_r: Optional[int] = None
_wakeup_w: Optional[int] = None
_IN_SUB_TERMINAL = False


def _init_injection_pipe():
    """Create the wakeup pipe (Unix only). Idempotent, thread-safe enough."""
    global _wakeup_r, _wakeup_w
    if _wakeup_r is None and not IS_WINDOWS:
        _wakeup_r, _wakeup_w = os.pipe()
        for fd in (_wakeup_r, _wakeup_w):
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def _drain_pipe(fd: int):
    """Read and discard all bytes from the wakeup pipe."""
    try:
        while True:
            os.read(fd, 4096)
    except (BlockingIOError, OSError):
        pass


def _inject_input(text: str, done: threading.Event):
    """Enqueue a message and wake up the main loop. Thread-safe."""
    _init_injection_pipe()
    try:
        _injected_input_queue.put_nowait(_InjectedInput(text, done))
    except queue.Full:
        pass
    if not IS_WINDOWS and _wakeup_w is not None:
        try:
            os.write(_wakeup_w, b'\x00')
        except (BlockingIOError, OSError):
            pass
    # If the main loop is blocked on prompt_toolkit, interrupt it so the
    # queued message is picked up immediately instead of waiting for user input.
    _interrupt_prompt()


def _get_input(cwd: str):
    """Return user input or an _InjectedInput from the remote queue.

    Checks the wakeup pipe non-blockingly so that remote messages queued
    between prompts are processed immediately. Does NOT block on stdin —
    prompt_toolkit handles its own blocking read after the prompt is shown.
    """
    # Already-queued message (fast path)
    try:
        return _injected_input_queue.get_nowait()
    except queue.Empty:
        pass

    if IS_WINDOWS:
        return pt_prompt(cwd)

    # Non-blocking check: is there a remote message waiting?
    _init_injection_pipe()
    try:
        r, _, _ = select.select([_wakeup_r], [], [], 0)
    except (select.error, ValueError, OSError):
        pass
    else:
        if _wakeup_r in r:
            _drain_pipe(_wakeup_r)
            try:
                return _injected_input_queue.get_nowait()
            except queue.Empty:
                pass  # spurious wakeup — fall through to prompt

    return pt_prompt(cwd)


# ── Background stdin reader for supplementary input during agent loop ──
# When the agent loop is running, the user can type additional instructions.
# A background thread reads stdin lines and queues them for injection at
# the next iteration boundary of the agent loop.
_bg_reader_thread: Optional[threading.Thread] = None
_bg_reader_stop = threading.Event()


def _start_bg_input_reader(target_queue: queue.Queue):
    """Start a background thread that reads stdin for supplementary messages.

    Only active during run_agent_loop() — the normal REPL prompt uses
    prompt_toolkit which owns stdin. The background reader uses select()
    on Unix and a polling fallback on Windows.

    target_queue: the queue to put supplementary messages into (should be
    the same queue that run_agent_loop() drains between iterations).
    """
    global _bg_reader_thread, _bg_reader_stop
    if _bg_reader_thread is not None and _bg_reader_thread.is_alive():
        return  # already running
    _bg_reader_stop.clear()

    def _reader():
        while not _bg_reader_stop.is_set():
            try:
                if not IS_WINDOWS and hasattr(sys.stdin, 'fileno'):
                    try:
                        r, _, _ = select.select([sys.stdin], [], [], 0.5)
                        if not r:
                            continue
                    except (select.error, ValueError, OSError):
                        time.sleep(0.5)
                        continue
                line = sys.stdin.readline()
                if not line:
                    break  # EOF
                line = line.strip()
                if line:
                    target_queue.put(line)
                    console.print(f"[dim cyan]📝 Queued: {line[:80]}[/dim cyan]")
            except Exception:
                break

    _bg_reader_thread = threading.Thread(
        target=_reader, daemon=True, name="bg-input-reader")
    _bg_reader_thread.start()


def _stop_bg_input_reader():
    """Stop the background input reader thread."""
    global _bg_reader_thread
    _bg_reader_stop.set()
    if _bg_reader_thread is not None:
        _bg_reader_thread.join(timeout=1.5)
        _bg_reader_thread = None


# ── Session-level approval state ─────────────────────────────────────────
# Lets the user pick "always" at an approval prompt to auto-approve the rest
# of the session — mirrors Claude Code / Cursor's "yes, and don't ask again".
# Reset on /exit, /reload, or a fresh process start.
_session_approval_state = {
    "all_commands": False,   # approve all shell commands this session
    "all_writes": False,     # approve all file writes this session
}


def _reset_session_approvals():
    """Clear session-level auto-approve (called on /exit, /reload)."""
    _session_approval_state["all_commands"] = False
    _session_approval_state["all_writes"] = False


def _arrow_approval_prompt(title: str, body_lines: list[str],
                           options: list[str]) -> Optional[str]:
    """Inline arrow-key approval selector.

    Prints *body_lines* (command / diff / reason content) into the normal
    conversation flow via ``console`` so they land in scrollback beneath the
    AI output, then runs a *non-full-screen* prompt_toolkit selector for the
    *options* only. This keeps the confirmation inline rather than taking over
    the terminal on the alternate screen buffer. Returns the selected option
    string, or None if cancelled (Esc / Ctrl+C).
    """
    from rich.markup import escape

    def _line_markup(ln: str) -> str:
        esc = escape(ln)
        if ln.startswith("+") and not ln.startswith("+++"):
            return f"[#9ece6a]{esc}[/#9ece6a]"
        if ln.startswith("-") and not ln.startswith("---"):
            return f"[#f7768e]{esc}[/#f7768e]"
        if ln.startswith("@@"):
            return f"[#7aa2f7]{esc}[/#7aa2f7]"
        return f"[#c0c0c0]{esc}[/#c0c0c0]"

    # Render the static context inline (title, separator, body) — this becomes
    # part of the scrollback under the conversation.
    console.print(f"[bold #e0af68]{escape(title)}[/bold #e0af68]")
    console.print("[#6b7280]" + "─" * 60 + "[/#6b7280]")
    for ln in body_lines:
        console.print("  " + _line_markup(ln))
    console.print("[#6b7280]" + "─" * 60 + "[/#6b7280]")
    console.print("[#6b7280]↑↓ choose  y/n/a shortcut  ↵ confirm  Esc cancel[/#6b7280]")

    # Fail-safe default: land on "No" when present, so a bare Enter denies
    # rather than approves an approval gate. Falls back to first option.
    _default_idx = next((i for i, o in enumerate(options)
                         if o.strip().lower().startswith("no")), 0)

    return select_dialog(
        options,
        full_screen=False,
        selected_index=_default_idx,
        letter_shortcuts=True,
        hint="↑↓ choose  y/n/a shortcut  ↵ confirm  Esc cancel",
        refresh_interval=0.5,
    )


def _blocking_approval_prompt(title: str, body: str, question: str,
                              allow_always: bool = False) -> str:
    """Pause the background stdin reader and block on an arrow-key prompt.

    Returns "yes", "no", or "always". When *allow_always* is False the "always"
    option is not offered and only "yes"/"no" can come back.

    Used by both request_command_approval and request_file_write_approval —
    the agent loop's main thread owns this call, and the bg reader (which
    also reads stdin for supplementary messages during the loop) must be
    stopped first or the two would race for the same input line.

    Fails closed (returns "no") when stdin isn't a real TTY — e.g. --execute
    mode with piped input, or any other headless context with no user to ask.
    """
    if not sys.stdin.isatty():
        console.print(
            f"[yellow]Approval required but no interactive TTY available — denying.[/yellow]")
        return "no"

    # Split body into displayable lines. Callers pass plain text (no Rich
    # markup) so diff content with literal brackets renders verbatim.
    body_lines = body.split("\n")

    options = ["Yes", "Always (this session)", "No"] if allow_always else ["Yes", "No"]

    _reader_was_running = bool(
        _bg_reader_thread is not None and _bg_reader_thread.is_alive())
    _stop_bg_input_reader()
    try:
        choice = _arrow_approval_prompt(f"{title} — {question}", body_lines, options)
    except (EOFError, KeyboardInterrupt):
        choice = None
    finally:
        if _reader_was_running:
            _start_bg_input_reader(get_user_message_queue())

    if choice == "Always (this session)":
        return "always"
    if choice == "Yes":
        return "yes"
    return "no"


def request_command_approval(command: str, reason: str) -> bool:
    """Block and ask the user to approve a command that matched a needs_approval
    policy rule. Wired as LoopDeps.request_command_approval for the local REPL."""
    import policy as _policy
    if _policy.is_delete_command(command):
        return request_file_delete_approval(
            command,
            f"DELETE via shell command\n{command}",
            reason,
        )
    if _session_approval_state["all_commands"]:
        return True
    choice = _blocking_approval_prompt(
        "Approval required",
        f"{command}\n{reason}" if reason else command,
        "Run this command?",
        allow_always=True,
    )
    if choice == "always":
        _session_approval_state["all_commands"] = True
        console.print("[dim]↳ All commands auto-approved for this session.[/dim]")
        return True
    return choice == "yes"


def authorize_direct_command(command: str, cwd: str = None) -> tuple[bool, str]:
    """Apply the same policy gateway to commands typed into the REPL.

    Direct commands previously bypassed policy entirely.  Returning a reason
    lets the REPL and remote-chat caller report a deterministic denial without
    executing any part of the command.
    """
    import policy as _policy
    decision = _policy.evaluate(command, cwd or os.getcwd())
    if decision.action == "deny":
        return False, f"Blocked by policy: {decision.reason}"
    if decision.action == "needs_approval":
        if not request_command_approval(command, decision.reason):
            return False, f"User denied: {decision.reason}"
    return True, ""


def request_file_write_approval(path: str, diff_preview: str, reason: str) -> bool:
    """Block and ask the user to approve a file write/edit before it's applied.
    Wired as LoopDeps.request_file_write_approval for the local REPL."""
    if _session_approval_state["all_writes"]:
        return True
    # Build body: header + reason + full diff. The diff is embedded so it
    # renders (and scrolls) inside the full-screen approval UI rather than
    # being printed to scrollback beforehand.
    _body_parts = [path]
    if reason:
        _body_parts.append(reason)
    if diff_preview:
        _body_parts.append("")
        _body_parts.append(diff_preview.rstrip("\n"))
    choice = _blocking_approval_prompt(
        "Approval required",
        "\n".join(_body_parts),
        "Apply this change?",
        allow_always=True,
    )
    if choice == "always":
        _session_approval_state["all_writes"] = True
        console.print("[dim]↳ All file writes auto-approved for this session.[/dim]")
        return True
    return choice == "yes"


def request_file_delete_approval(path: str, preview: str, reason: str) -> bool:
    """Require a fresh confirmation for every destructive delete operation."""
    body = "\n".join(part for part in (path, reason, "", preview) if part)
    choice = _blocking_approval_prompt(
        "Deletion approval required",
        body,
        "Delete this target?",
        allow_always=False,
    )
    return choice == "yes"


def _looks_complex(task: str) -> bool:
    """Heuristic: should this task offer a plan-first menu?

    Scores the input on signals that correlate with multi-step work:
    action verbs, multi-clause sentences, and length. Returns True when
    the score crosses a threshold — tuned to be conservative so simple
    questions and one-liners go straight to the agent loop.
    """
    t = task.strip()
    if len(t) < 25:
        return False
    score = 0
    low = t.lower()

    # Multi-step connector phrases
    for phrase in (" and then ", " after that ", " step by step",
                   " first ", " then ", " next ", " finally ", " multiple "):
        if phrase in low:
            score += 2

    # Strong action verbs
    for verb in ("implement", "refactor", "restructure", "migrate", "rewrite",
                 "integrate", "architect", "build ", "create ", "add ",
                 "set up", "wire up", "plumb", "overhaul"):
        if verb in low:
            score += 1

    # Sentence count
    sentences = [s for s in low.replace("!", ".").replace("?", ".").split(".") if len(s.strip()) > 10]
    if len(sentences) >= 2:
        score += 1
    if len(sentences) >= 4:
        score += 1

    # Long input
    if len(t) > 120:
        score += 1
    if len(t) > 300:
        score += 1

    return score >= 3


def _maybe_offer_plan_mode(user_input: str) -> bool:
    """Before running the agent loop, offer plan-first for complex tasks.

    Returns True if the user chose plan mode (loop will be entered in plan
    mode), False if they chose to act directly (or the prompt was skipped).
    Only triggers in interactive TTY sessions and when not already in plan
    mode.
    """
    if not sys.stdin.isatty():
        return False
    import plan_mode as _pm
    if _pm.is_plan_mode():
        return False
    if not _looks_complex(user_input):
        return False

    _stop_bg_input_reader()
    try:
        console.print(Panel(
            f"[dim]This looks like it might be a multi-step task.[/dim]\n"
            f"[dim]Plan first to let the AI explore & design before executing, or act directly.[/dim]",
            title="Approach?", border_style="accent", expand=False,
        ))
        try:
            choice = input("  [p] Plan first   [a] Act directly   (default: a): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = ""
    finally:
        _start_bg_input_reader(get_user_message_queue())

    if choice in ("p", "plan"):
        mode_manager.activate("act")
        plan = _pm.enter_plan_mode(user_input)
        console.print(f"[green]Entered plan mode.[/green] [dim](plan file: {plan['file']})[/dim]")
        return True
    return False


def _show_plan_approval_menu() -> bool:
    """Review a submitted immutable plan revision; loop completion is not readiness."""
    import plan_mode as _pm
    if not _pm.is_plan_mode():
        return False
    plan = _pm.get_current_plan()
    if not plan or plan.get("status") != "review_pending":
        return False
    approved = _review_and_approve_current_plan()
    if approved:
        console.print(
            f"[green]✓ Revision {approved['revision']} approved. Executing exact SHA "
            f"{approved['content_sha'][:12]}…[/green]")
        return True
    return False


def _run_agent_loop_with_interrupt(deps, user_input, session, agent_state,
                                   chat_history, events_cb=None,
                                   existing_session=None,
                                   continue_thread=False):
    """Run agent loop with soft-interrupt (Ctrl+C) and supplementary input support.

    Wraps run_agent_loop() with:
    1. Temporary SIGINT handler: first Ctrl+C → soft interrupt, second → force exit.
    2. Background stdin reader: user can type supplementary messages during execution.
    3. Module-level interrupt event reset before/after each call.

    Returns the same dict as run_agent_loop().
    """
    _interrupt_event = get_user_interrupt_event()
    _msg_queue = get_user_message_queue()

    # Reset interrupt state from any previous call
    _interrupt_event.clear()
    # Drain stale supplementary messages
    while not _msg_queue.empty():
        try:
            _msg_queue.get_nowait()
        except queue.Empty:
            break

    # Save original SIGINT handler (the shutdown function)
    _old_sigint = signal.getsignal(signal.SIGINT)

    def _soft_interrupt(signum, frame):
        if _interrupt_event.is_set():
            # Double Ctrl+C → force exit (escape hatch)
            console.print("\n[red]Force exit.[/red]")
            _stop_bg_input_reader()
            # Restore and call original handler
            signal.signal(signal.SIGINT, _old_sigint)
            _old_sigint(signum, frame)
            return
        _interrupt_event.set()
        console.print("\n[dim]⚡ Interrupting... (press Ctrl+C again to force exit)[/dim]")

    signal.signal(signal.SIGINT, _soft_interrupt)

    # Start background stdin reader for supplementary input
    _start_bg_input_reader(_msg_queue)

    try:
        response = run_agent_loop(
            deps, user_input, session, agent_state, chat_history,
            events_cb=events_cb,
            existing_session=existing_session,
            interrupt_event=_interrupt_event,
            message_queue=_msg_queue,
            continue_thread=continue_thread,
        )
    finally:
        # Restore original SIGINT handler
        signal.signal(signal.SIGINT, _old_sigint)
        _interrupt_event.clear()
        _stop_bg_input_reader()

    return response


def run_execute_mode(task: str, session: dict, depth: int, session_id: str = None) -> int:
    """Non-interactive single-task execution.

    Called when laintas-cli is invoked with --execute. Runs one agent loop,
    prints the result to stdout, and returns the exit code.
    Used by parent laintas-cli instances to delegate subtasks.
    """
    agent_state = {
        "shortTermMemory": "",
        "lastReply": "",
        "lastOutput": "",
    }
    chat_history = []
    if session_id:
        saved = load_resume_state(os.getcwd(), session_id=session_id)
        if saved:
            agent_state = _restore_resume_blob(saved, chat_history)
        else:
            agent_state["_session_id"] = session_id
    chat_history.append({"role": "user", "content": task})

    response = run_agent_loop(
        get_loop_deps(),
        original_input=task,
        session=session,
        state=agent_state,
        chat_history=chat_history,
        events_cb=None,
        existing_session=None,
        depth=depth,
    )

    result = response.get("msg", "")
    if result:
        chat_history.append({"role": "assistant", "content": result})
    if session_id:
        save_resume_state(prepare_state_for_repl(response.get("state", agent_state)),
                          chat_history, os.getcwd())
    last_output = response.get("state", {}).get("lastOutput", "")
    if last_output:
        result += "\n" + last_output
    print(result)

    return 0 if response.get("success") else 1


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Laintas CLI — Autonomous AI agent")
    parser.add_argument("--version", "-V", action="version",
                        version=f"laintas-cli {__version__}")
    parser.add_argument("--name", type=str, help="Set agent name (shows in Helpwo AGNETS)")
    parser.add_argument(
        "--backend", type=str,
        help="Custom backend URL (enters external/unmetered mode; Laintas credentials are stripped)",
        default=None)
    parser.add_argument("--laintas", type=str, help=argparse.SUPPRESS, default=None)
    parser.add_argument("--execute", "-e", type=str, default=None,
                        help="Execute a single task non-interactively and exit")
    parser.add_argument("--session-id", type=str, default=None,
                        help="Persist/resume --execute context under this logical session id")
    parser.add_argument("--depth", "-d", type=int, default=0,
                        help="Nesting depth (0=user terminal, 1+=sub-agent)")
    parser.add_argument("--simple-prompt", action="store_true", default=False,
                        help="Use plain input() instead of prompt_toolkit")
    parser.add_argument("--monitor-only", action="store_true", default=False,
                        help="Start as a remote executor only — register, heartbeat, "
                             "and poll HelpwoAI for tasks. No local REPL.")
    parser.add_argument("--resume", action="store_true", default=False,
                        help="Resume the saved conversation for this directory on startup")
    parser.add_argument("--continue", dest="continue_session", action="store_true", default=False,
                        help="Alias for --resume (session continuation)")
    parser.add_argument("--agent-id", type=str, default=None,
                        help="Pre-assigned agent id (used by sub-terminals)")
    parser.add_argument("--agent-name", type=str, default=None,
                        help="Pre-assigned agent display name (used by sub-terminals)")
    parser.add_argument("--agent-role", type=str, default=None,
                        choices=["pool", "deployed", "primary", "subagent"],
                        help="Role for the pre-assigned agent (used by sub-terminals)")
    parser.add_argument("--terminal-name", type=str, default=None,
                        help="Name of the terminal this sub-process represents")
    parser.add_argument("--parent-terminal", type=str, default=None,
                        help="Name of the parent terminal that spawned this sub-process")
    parser.add_argument("--parent-agent-id", type=str, default=None,
                        help="Agent id of the parent process that spawned this sub-process")
    parser.add_argument("--remote-parent-id", type=str, default=None,
                        help="Helpwo backend agent id of the primary CLI that owns this sub-terminal")
    parser.add_argument("--connect", action="store_true", default=False,
                        help="Hand this sub-terminal over to Helpwo at startup (as if /connect was run)")
    args = parser.parse_args()

    # Apply environment overrides
    if args.backend:
        os.environ["LAINTAS_BACKEND"] = args.backend
    if args.laintas:
        if args.laintas.rstrip("/") != LAINTAS_BASE:
            parser.error("custom authentication origins are no longer allowed")

    # All REPL instances use full-color console — sub-terminals are full
    # laintas-cli instances and should look identical to the main terminal.

    # Initialize unified home directory and auto-migrate old layout
    paths.ensure_home()
    backend_profiles.ensure_template()
    if migrate_mod.needs_migration():
        console.print("[dim]Migrating to new directory layout (~/.laintas/)...[/dim]")
        migrate_mod.migrate_all(verbose=True)

    # Ensure .laintas/ project files exist in cwd
    ensure_files_exist()
    evolution_lab.reconcile_workspace()

    # Load or create config
    config = load_config()
    agent_name = args.name or config.get("agentName", socket.gethostname())
    _update_status_cache(model=config.get("model", ""))

    # Official mode uses the Laintas account. Custom/local backends are a
    # separate trust and billing domain and must not require or receive it.
    _active_backend = get_backend_profile()
    session = (ensure_auth() or {}) if _active_backend.sends_laintas_credentials else {}
    if args.depth == 0 and not _active_backend.sends_laintas_credentials:
        console.print(
            f"[yellow]Backend mode: {_active_backend.kind} "
            f"({_active_backend.base_url}) — external/unmetered; "
            "Laintas credentials are not sent.[/yellow]")

    # Project extensions receive a narrow inference gateway, never the raw
    # authenticated session. The normal backend path remains authoritative for
    # official authentication, model authorization and billing.
    _extension_runtime = extension_runtime.get_runtime()
    _extension_runtime.configure(
        console=console,
        reserved_commands=[
            name for spec in COMMAND_SPECS for name in spec.all_names
        ],
        backend_callback=lambda message, system_prompt="", **options: call_backend_stream(
            session=session, message=message, system_prompt=system_prompt,
            current_path=os.getcwd(), history=options.get("history") or [],
            messages=options.get("messages"), lang=options.get("lang", "EN"),
            tools_enabled=bool(options.get("tools_enabled", False)),
        ),
    )
    for _ext_name, _ext_ok, _ext_message in evolution_lab.load_active_extensions(
            _extension_runtime):
        if not _ext_ok and args.depth == 0:
            console.print(f"[yellow]Extension {_ext_name}: {_ext_message}[/yellow]")

    # ── Non-interactive execution mode ──
    if args.execute:
        if (_active_backend.sends_laintas_credentials
                and not session.get("userId")):
            console.print("[red]Authentication required for --execute mode. Use /login first.[/red]")
            sys.exit(1)
        sys.exit(run_execute_mode(args.execute, session, args.depth, args.session_id))

    # ── Simple prompt (PTY subprocess mode) ──
    _use_simple_prompt = args.simple_prompt or not sys.stdin.isatty()
    if _use_simple_prompt:
        global pt_prompt, _IN_SUB_TERMINAL
        pt_prompt = _simple_prompt
        _IN_SUB_TERMINAL = True

    # Show banner (skip in child terminals to avoid Rich output in PTY)
    if args.depth == 0:
        show_banner(agent_name, session if session else None)

    # Register as remote agent (only if authenticated)
    agent_registry = AgentRegistry()
    _session_start_cwd = os.getcwd()
    agent_state = {
        "shortTermMemory": "",
        "lastReply": "",
        "lastOutput": "",
    }
    chat_history = []
    current_live_session = None
    # Load last session snapshot for this directory (depth-0 only)
    if args.depth == 0:
        current_live_session = session_store.load_current_session(_session_start_cwd)
        _session_warning = session_store.consume_last_error()
        if _session_warning:
            console.print(f"[yellow]{_session_warning}[/yellow]")
        if current_live_session:
            agent_state = _restore_resume_blob(current_live_session, chat_history)
            handle_meta_command._current_live_session = current_live_session
            if current_live_session.get("pending_continuation"):
                console.print(
                    f"[dim]Continuing live session with pending task: "
                    f"{str(current_live_session.get('objective') or current_live_session.get('last_user_input') or 'Untitled session')[:80]}[/dim]"
                )
        else:
            current_live_session = session_store.create_session(_session_start_cwd, agent_state, chat_history)
            handle_meta_command._current_live_session = current_live_session

        # Recover an admitted prompt whose run never emitted turn_ended.  Do
        # not auto-execute it: tool side effects may already have happened.
        # Restoring the prompt and marking it continuable is safe and leaves
        # the retry decision with the user.
        _incomplete = event_log.last_incomplete_task()
        if (_incomplete and current_live_session
                and not event_log.owner_process_is_alive(_incomplete)):
            _event_session = str(_incomplete.get("session_id") or "")
            _live_id = str(current_live_session.get("session_id") or "")
            if not _event_session or _event_session == _live_id:
                _recovered_text = str(_incomplete.get("text") or "").strip()
                _already_present = any(
                    m.get("role") == "user"
                    and str(m.get("content") or "").strip() == _recovered_text
                    for m in chat_history
                )
                if _recovered_text and not _already_present:
                    chat_history.append({"role": "user", "content": _recovered_text})
                current_live_session = session_store.sync_runtime(
                    current_live_session, agent_state, chat_history,
                    cwd=_session_start_cwd,
                    objective=_recovered_text or None,
                    last_user_input=_recovered_text or None,
                    exit_reason="crash_recovery",
                    tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
                )
                handle_meta_command._current_live_session = current_live_session
                event_log.acknowledge_incomplete(_incomplete)
                console.print(
                    "[yellow]Recovered an interrupted agent run. "
                    "Review the workspace, then use /continue to resume safely.[/yellow]"
                )
        _snapshot = load_session_snapshot(_session_start_cwd)
        if _snapshot:
            agent_state["_last_session_snapshot"] = _snapshot
        # Full-fidelity resume blob: explicit restore by flag or slash command.
        # Without a flag, just hint so a fresh session stays fresh.
        _resume_blob = load_resume_state(_session_start_cwd)
        if _resume_blob and _resume_blob.get("chat_history"):
            _n_turns = _resume_turn_count(_resume_blob)
            _ago = _format_time_ago(_resume_blob.get("timestamp", 0))
            if args.resume or args.continue_session:
                _selected_resume = _choose_resume_blob(_session_start_cwd, "latest")
                if _selected_resume:
                    if current_live_session:
                        session_store.close_session(current_live_session)
                    agent_state = _restore_resume_blob(_selected_resume, chat_history)
                    current_live_session = session_store.create_session(_session_start_cwd, agent_state, chat_history)
                    handle_meta_command._current_live_session = current_live_session
                    console.print(
                        f"[green]Resumed previous session in this directory "
                        f"({_resume_turn_count(_selected_resume)} turn(s), "
                        f"{_format_time_ago(_selected_resume.get('timestamp', 0))}).[/green]"
                    )
                    _print_resume_transcript(_selected_resume, 20)
            elif not current_live_session or current_live_session.get("turn_count", 0) == 0:
                console.print(
                    f"[dim]Previous session in this directory "
                    f"({_n_turns} turn(s), {_ago}). Type [bold]/resume[/bold] "
                    f"or start with [bold]--resume[/bold] to continue.[/dim]"
                )
        elif args.resume or args.continue_session:
            console.print("[yellow]No saved session for this directory.[/yellow]")

        # Bind /continue to the exact in-memory objects restored at startup.
        # Without this, the command mutates a deserialized session copy and the
        # next REPL prompt overwrites its result with stale local state.
        handle_meta_command._last_agent_state = agent_state
        handle_meta_command._last_chat_history = chat_history
        handle_meta_command._last_original_input = (
            (current_live_session or {}).get("last_original_input")
            or (current_live_session or {}).get("last_user_input")
            or agent_state.get("objective")
        )
        handle_meta_command._last_deps = get_loop_deps()
        handle_meta_command._last_session = session
        handle_meta_command._last_events_cb = None
        handle_meta_command._last_existing_session = None

    # Stash REPL state callbacks + terminal identity so /connect (now or later)
    # can register this instance with full context.
    agent_registry.depth = args.depth
    agent_registry._state_cb = lambda: agent_state
    agent_registry._chat_cb = lambda: chat_history
    if args.depth > 0:
        agent_registry.parent_remote_id = args.remote_parent_id or None
        agent_registry.terminal_meta = {
            "name": args.terminal_name or f"term-pid{os.getpid()}",
            "command": "laintas-cli",
            "createdAt": time.time(),
            "createdBy": args.parent_terminal or "term0",
        }

    if session.get("userId") or not _active_backend.sends_laintas_credentials:
        if args.depth == 0 and args.monitor_only:
            # Monitor mode IS the remote-executor role — it must be online.
            agent_registry.register(session, name=agent_name)
            agent_registry.start_heartbeat()
            agent_registry.start_message_poll(
                lambda: agent_state,
                lambda: chat_history,
            )
        elif args.connect:
            # --connect (any depth; used by Helpwo's term-new for sub-terminals).
            connect_terminal_to_helpwo(agent_registry, session)
        elif args.depth == 0:
            # Two-end handshake: NO auto-link. The primary CLI goes online in
            # Helpwo only when the user runs /connect here; sub-terminals only
            # when /connect runs inside them.
            console.print("[dim]Not linked to Helpwo. Run [bold]/connect \\[name][/bold] "
                          "to bring this CLI online (Helpwo can then create "
                          "sub-terminals here from its UI).[/dim]")
        else:
            console.print("[dim]Run [bold]/connect \\[name][/bold] to hand this "
                          "sub-terminal over to Helpwo.[/dim]")

    # PTY session managed at REPL level (must be before shutdown for nonlocal)
    interactive_session = None

    # Register the primary agent.
    # If launched as a sub-terminal with an explicit identity, register that
    # agent (role=deployed) and tag it with the parent context. Otherwise
    # register the conventional "primary" REPL agent.
    if args.agent_id:
        sub_role = args.agent_role or "deployed"
        sub = register_agent(name=args.agent_id,
                             depth=args.depth,
                             parent_id=args.parent_agent_id,
                             role=sub_role,
                             load_existing=True)
        if args.agent_name and args.agent_name != args.agent_id:
            sub.name = args.agent_name
        sub.parent_terminal = args.parent_terminal or "term0"
        if args.terminal_name:
            sub.home_terminal = args.terminal_name
            sub.stationed_terminal = args.terminal_name
        set_current_agent_id(args.agent_id)
    else:
        primary = register_agent(name="primary", depth=0, role="primary",
                                 load_existing=True)
        primary.home_terminal = "term0"
        primary.parent_terminal = None
        set_current_agent_id("primary")

    # Load user skills from ~/.laintas/skills. Failures are surfaced
    # but never block startup.
    try:
        _skill_results = skills_mod.load_all()
        _failed = [(n, m) for n, ok, m in _skill_results if not ok]
        if _failed and args.depth == 0:
            for n, m in _failed:
                console.print(f"[yellow]skill {n}: {m}[/yellow]")
    except Exception as _e:
        console.print(f"[yellow]skill loader error: {_e}[/yellow]")

    # Connect MCP servers (if any configured + mcp SDK installed). Best-effort.
    if args.depth == 0:
        try:
            if not _get_mcp_mod().MCP_AVAILABLE:
                if _get_mcp_mod().CONFIG_PATH.exists():
                    console.print(f"[dim yellow]mcp config present but SDK missing: {_get_mcp_mod().MCP_IMPORT_ERROR}[/dim yellow]")
                    console.print("[dim]Install with: pip install mcp[/dim]")
            else:
                _mcp_results = _get_mcp_mod().get_manager().connect_all_enabled()
                for n, ok, m in _mcp_results:
                    if n == "(none)":
                        continue
                    style = "green" if ok else "yellow"
                    console.print(f"[{style}]mcp {n}: {m}[/{style}]")
        except Exception as _e:
            console.print(f"[yellow]mcp connect error: {_e}[/yellow]")

    # Setup graceful shutdown
    def shutdown(signum=None, frame=None):
        if args.depth > 0:
            # Sub-terminal CLI: its console may already be a dead PTY (parent
            # closed it / tmux window killed), where console.print and the
            # graceful save path can wedge. Unregister from Helpwo first,
            # best-effort cleanup, then hard-exit.
            try:
                agent_registry.unregister()
            except Exception:
                pass
            try:
                stop_trigger_scanner()
                close_all_terminals()
                close_all_agents()
            except Exception:
                pass
            os._exit(0)
        console.print("\n[yellow]Shutting down...[/yellow]")
        if args.depth == 0:
            save_session_snapshot(agent_state, chat_history, _session_start_cwd)
            save_resume_state(agent_state, chat_history, _session_start_cwd)
            if current_live_session:
                session_store.sync_runtime(
                    current_live_session, agent_state, chat_history,
                    cwd=_session_start_cwd,
                    tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
                )
        stop_trigger_scanner()
        close_all_terminals()
        close_all_agents()
        browser_mod.close_all_browser_sessions()
        try:
            _get_mcp_mod().get_manager().shutdown()
        except Exception:
            pass
        nonlocal interactive_session
        if interactive_session:
            interactive_session.close()
        agent_registry.unregister()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if not IS_WINDOWS:
        # tmux kill-window (parent's unregister_terminal / Helpwo term-close)
        # delivers SIGHUP — unregister from Helpwo before dying instead of
        # leaving a stale agent until the 60s heartbeat timeout.
        signal.signal(signal.SIGHUP, shutdown)

    # ── Create term0: a real persistent bash session ──
    # All system commands and stationed-agent shell.exec route through this
    # via marker-poll, identical to how named sub-terminals work.
    # Created for ALL interactive REPL instances (depth 0 and depth > 0),
    # so sub-terminals have the same capabilities as the main terminal.
    _term0_session = None
    if not IS_WINDOWS:
        try:
            _term0_session = InteractiveSession(
                DEFAULT_SHELL, timeout=0, stream_output=False, persistent=True)
            _term0_session.start()
            time.sleep(0.08)
            if _term0_session.is_alive():
                _term0_session.read_output(timeout=0.1)
            register_terminal(_term0_session, DEFAULT_SHELL, 0, name="term0")
        except Exception as _e:
            console.print(f"[dim yellow]term0 bash session init failed: {_e}[/dim yellow]")
            _term0_session = None

    # ── Monitor-only mode (no interactive REPL) ──
    # Runs purely as a remote executor: heartbeat + /poll loop already
    # started above when the agent registered. Here we just park the main
    # thread until SIGINT/SIGTERM. See HELPWO_INTEGRATION_PLAN.md phase D.
    if args.monitor_only:
        if (_active_backend.sends_laintas_credentials
                and not session.get("userId")):
            console.print("[red]--monitor-only requires authentication. Run /login first.[/red]")
            sys.exit(1)
        if not agent_registry.agent_id:
            console.print("[red]--monitor-only requires successful agent registration. "
                          "Check backend URL and credentials.[/red]")
            sys.exit(1)
        console.print(Panel(
            f"[green]Monitor-only mode active[/green]\n"
            f"Agent: [bold]{agent_registry.agent_name}[/bold] ({agent_registry.agent_id})\n"
            f"Listening for remote exec/query/delegate requests…\n"
            f"[dim]Ctrl+C to exit.[/dim]",
            title="laintas-cli monitor",
            border_style="green",
        ))
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            shutdown()

    # Main interactive loop

    while True:
        # ── term0 health check ──
        if not IS_WINDOWS:
            _ensure_term0_alive()
        try:
            item = _get_input(str(os.getcwd()))
        except (KeyboardInterrupt, EOFError):
            shutdown()

        if isinstance(item, _InjectedInput):
            user_input = item.text
            injected_done = item.done
        else:
            user_input = item
            injected_done = None

        if not user_input:
            # Don't set injected_done here — the empty input might be from
            # a prompt interruption by a remote message. If a remote message
            # is queued, the next iteration will pick it up and process it
            # before setting injected_done.
            continue

        # Ctrl+D → exit
        if user_input == "/exit":
            if args.depth == 0:
                save_session_snapshot(agent_state, chat_history, _session_start_cwd)
                save_resume_state(agent_state, chat_history, _session_start_cwd)
                if current_live_session:
                    session_store.sync_runtime(
                        current_live_session, agent_state, chat_history,
                        cwd=_session_start_cwd,
                        tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
                    )
                    session_store.close_session(current_live_session)
                    handle_meta_command._current_live_session = None
            stop_trigger_scanner()
            close_all_terminals()
            browser_mod.close_all_browser_sessions()
            if interactive_session:
                interactive_session.close()
            agent_registry.unregister()
            clear_session()
            console.print("[green]Logged out. Goodbye![/green]")
            if injected_done is not None:
                injected_done.set()
            return

        # /resume — restore this directory's saved conversation (full-fidelity).
        # Like mainstream agent CLIs, this does not consume/delete the saved
        # session; it remains available for future launches until overwritten.
        # /resume [N|all|latest] — the argument controls how many messages to
        # echo after restoring (N, default 20; 0 = silent; "all" = full). When
        # multiple sessions are saved, a full-screen picker chooses which one;
        # "latest" skips the picker and restores the newest directly.
        _resume_parts = user_input.strip().split(maxsplit=1)
        if (_resume_parts and _resume_parts[0].lower() == "/resume"
                and args.depth == 0):
            _resume_arg = (_resume_parts[1].strip().lower()
                           if len(_resume_parts) > 1 else "")
            _echo_limit = 20
            _pick_latest = False
            if _resume_arg in ("latest", "last"):
                _pick_latest = True
            elif _resume_arg == "all":
                _echo_limit = None
            elif _resume_arg.isdigit():
                _echo_limit = int(_resume_arg)
            elif _resume_arg and _resume_arg not in ("-s", "--show"):
                console.print(f"[red]Invalid /resume argument: {_resume_arg}[/red]")
                console.print("[dim]Usage: /resume [N|all|latest]  "
                              "(N = messages to echo, default 20, 0 = silent)[/dim]")
                if injected_done is not None:
                    injected_done.set()
                continue
            _choices = _resume_choices(_session_start_cwd)
            if not _choices:
                _blob = None
                console.print("[yellow]No saved session to resume in this directory.[/yellow]")
            elif _pick_latest or len(_choices) == 1 or not sys.stdin.isatty():
                # Direct restore of the newest session (no picker).
                _blob = _choices[0]
            else:
                # Multiple saved sessions — open the full-screen picker.
                _blob = show_resume_picker(_session_start_cwd)
            if _blob and not _blob.get("chat_history"):
                console.print("[yellow]Saved session has no conversation to resume.[/yellow]")
            elif _blob:
                if current_live_session:
                    session_store.close_session(current_live_session)
                agent_state = _restore_resume_blob(_blob, chat_history)
                current_live_session = session_store.create_session(_session_start_cwd, agent_state, chat_history)
                handle_meta_command._current_live_session = current_live_session
                handle_meta_command._last_agent_state = agent_state
                handle_meta_command._last_chat_history = chat_history
                handle_meta_command._last_original_input = (
                    current_live_session.get("last_original_input")
                    or current_live_session.get("last_user_input")
                    or current_live_session.get("objective")
                )
                _n = _resume_turn_count(_blob)
                _ago = _format_time_ago(_blob.get("timestamp", 0))
                console.print(
                    f"[green]Resumed previous session in this directory "
                    f"({_n} turn(s), {_ago}).[/green]"
                )
                _print_resume_transcript(_blob, _echo_limit)
            if injected_done is not None:
                injected_done.set()
            continue

        _new_parts = user_input.strip().split(maxsplit=1)
        if (_new_parts and _new_parts[0].lower() in ("/new", "/new-session", "/reset-session")
                and args.depth == 0):
            _active_work = workgraph.get_active_work(cwd=_session_start_cwd)
            if (_active_work and _active_work.get("status")
                    not in {"COMPLETED", "CANCELLED", "FAILED"}):
                _choice = _blocking_approval_prompt(
                    "Start a new session",
                    f"Active WorkGraph: {_active_work['id']}\n"
                    f"Objective: {_active_work['objective']}\n"
                    f"Status: {_active_work['status']}\n\n"
                    "The work will be preserved for /resume but detached from the new session.",
                    "Detach active work and start a new session?",
                    allow_always=False,
                )
                if _choice != "yes":
                    console.print("[yellow]New session cancelled.[/yellow]")
                    if injected_done is not None:
                        injected_done.set()
                    continue
            if current_live_session:
                session_store.close_session(current_live_session)
            chat_history.clear()
            agent_state = {
                "shortTermMemory": "",
                "lastReply": "",
                "lastOutput": "",
            }
            try:
                task_manager.clear_session_tasks()
            except Exception:
                pass
            try:
                import plan_mode as _new_pm
                import workflow_engine as _new_workflow
                if _new_pm.is_plan_mode():
                    _new_pm.exit_plan_mode(approve=False)
                _new_workflow.detach_active_workflow()
                workgraph.set_active_work(None, cwd=_session_start_cwd)
            except Exception:
                pass
            current_live_session = session_store.create_session(_session_start_cwd, agent_state, chat_history)
            handle_meta_command._current_live_session = current_live_session
            handle_meta_command._last_agent_state = agent_state
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = None
            console.print("[green]Started a new session.[/green]")
            if injected_done is not None:
                injected_done.set()
            continue

        if (args.depth == 0
                and len(user_input.strip().split()) == 1
                and user_input.strip().split()[0].lower() in ("/q", "/quit")):
            save_session_snapshot(agent_state, chat_history, _session_start_cwd)
            _checkpoint = save_resume_checkpoint(agent_state, chat_history, _session_start_cwd)
            if current_live_session:
                session_store.sync_runtime(
                    current_live_session, agent_state, chat_history,
                    cwd=_session_start_cwd,
                    tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
                )
                session_store.close_session(current_live_session)
                handle_meta_command._current_live_session = None
            if _checkpoint:
                console.print(
                    f"[dim]Saved resume checkpoint: "
                    f"{_format_time_ago(_checkpoint.get('timestamp', 0))} · "
                    f"{_checkpoint.get('title', 'Untitled session')}[/dim]"
                )

        # Check for meta commands
        if user_input.startswith("/"):
            should_exit = handle_meta_command(user_input, agent_registry, session, interactive_session)
            current_live_session = getattr(handle_meta_command, '_current_live_session', current_live_session)
            if should_exit:
                if args.depth == 0:
                    save_session_snapshot(agent_state, chat_history, _session_start_cwd)
                    save_resume_state(agent_state, chat_history, _session_start_cwd)
                    if current_live_session:
                        session_store.sync_runtime(
                            current_live_session, agent_state, chat_history,
                            cwd=_session_start_cwd,
                            tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
                        )
                if interactive_session:
                    interactive_session.close()
                if injected_done is not None:
                    injected_done.set()
                return
            if injected_done is not None:
                injected_done.set()
            continue

        # If an interactive session is active, forward user input to it,
        # then ask the AI to decide the next step based on the output.

        if interactive_session and interactive_session.is_alive():
            console.print(f"[dim yellow]> {user_input}[/dim yellow]")
            chat_history.append({"role": "user", "content": user_input})
            if agent_registry.agent_id:
                agent_registry._push_events([{"type": "user", "content": user_input}])

            # Forward user input to the running interactive program
            interactive_session.send_keys(user_input + "\n")
            time.sleep(0.5)
            new_output = interactive_session.read_output(timeout=1.0)

            if not interactive_session.is_alive():
                display_command_output(interactive_session.command,
                                       interactive_session.returncode,
                                       interactive_session.full_output)
                agent_state["lastOutput"] = interactive_session.full_output
                # Session exited — let the agent loop process final output
                if session.get("userId") or not _active_backend.sends_laintas_credentials:
                    def local_events_cb(events: list):
                        if agent_registry.agent_id:
                            agent_registry._push_events(events)
                    context = (f"The interactive program exited.\n"
                               f"Final output:\n{interactive_session.full_output[:3000]}\n\n"
                               f"Original user input: {user_input}\n"
                               f"What should we do next?")
                    response = _run_agent_loop_with_interrupt(get_loop_deps(), context, session, agent_state, chat_history,
                                              events_cb=local_events_cb,
                                              existing_session=interactive_session)
                    interactive_session = response.get("session")
                else:
                    interactive_session.close()
                    interactive_session = None
                    response = {"success": True, "msg": "", "state": agent_state, "session": None}
            else:
                display_command_output(interactive_session.command, -1, new_output)
                agent_state["lastOutput"] = interactive_session.full_output
                # Session still alive — ask AI to process the new output
                if session.get("userId") or not _active_backend.sends_laintas_credentials:
                    def local_events_cb(events: list):
                        if agent_registry.agent_id:
                            agent_registry._push_events(events)
                    context = (f"User input (sent to {interactive_session.command}): {user_input}\n"
                               f"Program output:\n{new_output[:2000]}\n\n"
                               f"Full session output so far: {len(interactive_session.full_output)} bytes.\n"
                               f"Decide: send more keys, start a different command, or close the session.")
                    response = _run_agent_loop_with_interrupt(get_loop_deps(), context, session, agent_state, chat_history,
                                              events_cb=local_events_cb,
                                              existing_session=interactive_session)
                    interactive_session = response.get("session")
                else:
                    response = {"success": True, "msg": "", "state": agent_state, "session": interactive_session}

            # Save reply
            if response.get("msg"):
                chat_history.append({"role": "assistant", "content": response["msg"]})
            # ── Cross-interaction state preservation ──
            agent_state = prepare_state_for_repl(response.get("state", {}))
            handle_meta_command._last_agent_state = agent_state
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = user_input
            handle_meta_command._last_existing_session = interactive_session
            if args.depth == 0:
                current_live_session = session_store.sync_runtime(
                    current_live_session, agent_state, chat_history,
                    cwd=_session_start_cwd,
                    objective=agent_state.get("objective"),
                    last_user_input=user_input,
                    exit_reason=response.get("exit_reason"),
                    tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
                )
                handle_meta_command._current_live_session = current_live_session
                save_resume_state(agent_state, chat_history, _session_start_cwd)
            if injected_done is not None:
                injected_done.set()
            continue

        # ── Normal input routing ───────────────────────────────────

        # Add to chat history
        chat_history.append({"role": "user", "content": user_input})
        _system_input = is_system_command(user_input)
        if not _system_input:
            import plan_mode as _pending_pm
            if _pending_pm.is_pending_task():
                _pending_pm.enter_plan_mode(user_input)
                console.print(
                    "[green]PLAN task set from this message.[/green]")

        # Persist the admitted prompt before provider/tool execution so a
        # crash cannot leave the live session one user turn behind.
        if args.depth == 0 and current_live_session:
            current_live_session = session_store.sync_runtime(
                current_live_session, agent_state, chat_history,
                cwd=_session_start_cwd,
                objective=None if _system_input else user_input,
                last_user_input=None if _system_input else user_input,
                tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
            )
            handle_meta_command._current_live_session = current_live_session

        # Push user input event to remote stream
        if agent_registry.agent_id:
            agent_registry._push_events([{"type": "user", "content": user_input}])

        # Route first word against PATH/builtins → system command or AI
        # All REPL instances (depth 0 and depth > 0) execute system commands
        # directly. Natural language goes to AI.
        if _system_input:
            console.print(f"\n[dim yellow]$ {user_input}[/dim yellow]")
            if agent_registry.agent_id:
                agent_registry._push_events([{"type": "system", "kind": "command", "content": user_input}])

            _command_allowed, _command_denial = authorize_direct_command(
                user_input, os.getcwd())
            if not _command_allowed:
                console.print(f"[red]{_command_denial}[/red]")
                agent_state["lastOutput"] = _command_denial
                if agent_registry.agent_id:
                    agent_registry._push_events([{
                        "type": "system", "kind": "output",
                        "content": _command_denial,
                    }])
                if args.depth == 0 and current_live_session:
                    current_live_session = session_store.sync_runtime(
                        current_live_session, agent_state, chat_history,
                        cwd=_session_start_cwd,
                        tasks=task_manager.export_active_tasks(
                            cwd=_session_start_cwd),
                    )
                    handle_meta_command._current_live_session = current_live_session
                    save_resume_state(
                        agent_state, chat_history, _session_start_cwd)
                if injected_done is not None:
                    injected_done.set()
                continue

            # Close previous AI-managed interactive session if any
            if interactive_session is not None:
                interactive_session.close()
                interactive_session = None

            # Remote-injected commands (from Helpwo Activity terminal): use
            # subprocess capture so stdout can be forwarded to the event stream.
            # pty_passthrough directly inherits the local tty and returns
            # stdout="" — nothing to forward.  Local commands keep pty_passthrough
            # so interactive programs (vim, claude) still work normally.
            if injected_done is not None and agent_registry.agent_id:
                import subprocess as _sub
                _cd_m = re.match(r'^\s*cd(?:\s+(.+))?\s*$', user_input)
                if _cd_m:
                    # cd is a shell builtin — handle it in the Python process too
                    _cd_target = (_cd_m.group(1) or "").strip() or os.path.expanduser("~")
                    try:
                        os.chdir(os.path.expanduser(_cd_target))
                        _cap_out = f"{os.getcwd()}"
                    except OSError as _e:
                        _cap_out = f"cd: {_e}"
                    result = {"stdout": _cap_out, "stderr": "", "returncode": 0, "success": True}
                else:
                    try:
                        _proc = _sub.run(
                            user_input, shell=True, capture_output=True, text=True,
                            timeout=60, cwd=os.getcwd(),
                        )
                        _cap_out = (_proc.stdout + _proc.stderr).strip()
                        result = {
                            "stdout": _proc.stdout, "stderr": _proc.stderr,
                            "returncode": _proc.returncode, "success": _proc.returncode == 0,
                        }
                    except _sub.TimeoutExpired:
                        _cap_out = "Command timed out after 60s."
                        result = {"stdout": "", "stderr": "", "returncode": -1, "success": False}
                agent_registry._push_events([{"type": "system", "kind": "output", "content": _cap_out[:4000]}])
            else:
                # Local user: ordinary commands route through term0's persistent
                # bash (marker-poll + cwd sync) so cd/export/pushd actually persist
                # across commands — term0's bash state IS what "current directory"
                # means here. Commands in the interactive whitelist (vim, claude,
                # ssh, ...) get full PTY passthrough so they keep native terminal
                # control (raw keystrokes, resize, full-screen redraw).
                _first = extract_first_word(user_input)
                _term0_info = get_terminal("term0")
                _use_term0 = (
                    not IS_WINDOWS
                    and _first not in get_interactive_commands()
                    and _term0_info is not None
                    and _term0_info.session is not None
                    and _term0_info.session.is_alive()
                )

                if _use_term0:
                    result = _marker_poll_exec(_term0_info.session, user_input, strip_ansi_codes=False)
                    _sync_cwd_from_term0(_term0_info.session)
                    # marker-poll captures output but doesn't echo to the user's
                    # terminal (unlike pty_passthrough, which echoes directly) —
                    # print so the user sees command output.
                    _stdout = result.get("stdout", "")
                    if _stdout:
                        try:
                            sys.stdout.write(_stdout)
                            if not _stdout.endswith("\n"):
                                sys.stdout.write("\n")
                            sys.stdout.flush()
                        except (BrokenPipeError, OSError):
                            pass
                else:
                    # Drain any queued terminal query responses before passthrough.
                    if not IS_WINDOWS:
                        _fl = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
                        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, _fl | os.O_NONBLOCK)
                        try:
                            while True:
                                if not sys.stdin.buffer.read(4096):
                                    break
                        except (BlockingIOError, OSError):
                            pass
                        finally:
                            fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, _fl)

                    result = pty_passthrough(user_input)
                    # On Windows pty_passthrough() captures output via
                    # subprocess instead of echoing to the terminal (no PTY),
                    # so print it here or the user sees nothing for `dir`, etc.
                    if IS_WINDOWS:
                        _win_out = result.get("stdout", "")
                        _win_err = result.get("stderr", "")
                        try:
                            if _win_out:
                                sys.stdout.write(_win_out)
                                if not _win_out.endswith("\n"):
                                    sys.stdout.write("\n")
                            if _win_err:
                                sys.stderr.write(_win_err)
                                if not _win_err.endswith("\n"):
                                    sys.stderr.write("\n")
                            sys.stdout.flush()
                        except (BrokenPipeError, OSError):
                            pass

                if agent_registry.agent_id:
                    output_preview = result.get("stdout", "")[:2000]
                    if output_preview:
                        agent_registry._push_events([{"type": "system", "kind": "output", "content": output_preview}])

            # ── Debug: log command execution ──
            loop_id = next_debug_loop()
            add_debug_log(DebugEntry(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                loop=loop_id,
                user_input=user_input[:2000],
                current_path=os.getcwd(),
                exec_command=user_input,
                exec_stdout=result.get("stdout", "")[:2000],
                exec_returncode=result.get("returncode", -1),
                done=True,
            ))

            agent_state["lastOutput"] = result.get("stdout", "")
            response = {
                "success": result.get("success", False),
                "msg": "",
                "state": agent_state,
                "session": None,
            }
        else:
            if (_active_backend.sends_laintas_credentials
                    and not session.get("userId")):
                console.print("[yellow]Not authenticated. Use /login first.[/yellow]")
                if injected_done is not None:
                    injected_done.set()
                continue
            console.print("[dim]Not a system command, asking AI...[/dim]")

            # Offer plan-first for complex tasks (only in interactive mode)
            _maybe_offer_plan_mode(user_input)

            # Build event callback for real-time streaming
            def local_events_cb(events: list):
                if agent_registry.agent_id:
                    agent_registry._push_events(events)

            response = _run_agent_loop_with_interrupt(get_loop_deps(), user_input, session, agent_state, chat_history,
                                      events_cb=local_events_cb,
                                      existing_session=interactive_session)
            interactive_session = response.get("session")

            # ── Store context for /continue ──
            handle_meta_command._last_agent_state = response.get("state", agent_state)
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = user_input
            handle_meta_command._last_deps = get_loop_deps()
            handle_meta_command._last_session = session
            handle_meta_command._last_events_cb = local_events_cb
            handle_meta_command._last_existing_session = interactive_session

            # Sync CWD after AI loop — the AI may have run shell.exec("cd ...")
            # which changed term0's bash CWD. Sync so the next prompt shows
            # the correct directory.
            if not IS_WINDOWS:
                _t0 = get_terminal("term0")
                if _t0 and _t0.session and _t0.session.is_alive():
                    _sync_cwd_from_term0(_t0.session)

            # ── Plan approval menu ──
            # If the agent ran in plan mode, offer a rich review menu.
            # On approval, re-run the same task in act mode to execute.
            if _show_plan_approval_menu():
                console.print("[dim]Re-running task in ACT mode...[/dim]")
                _plan_state = response.get("state", agent_state)
                response = _run_agent_loop_with_interrupt(
                    get_loop_deps(), user_input, session, _plan_state, chat_history,
                    events_cb=local_events_cb,
                    existing_session=interactive_session,
                    continue_thread=True)
                interactive_session = response.get("session")
                handle_meta_command._last_agent_state = response.get("state", agent_state)
                handle_meta_command._last_existing_session = interactive_session
                if not IS_WINDOWS:
                    _t0 = get_terminal("term0")
                    if _t0 and _t0.session and _t0.session.is_alive():
                        _sync_cwd_from_term0(_t0.session)

        # Save AI reply to chat history
        if response.get("msg"):
            chat_history.append({"role": "assistant", "content": response["msg"]})

        # ── Cross-interaction state preservation ──
        # Preserve recent context across REPL interactions so the model
        # doesn't lose track of what it was doing.
        agent_state = prepare_state_for_repl(response.get("state", {}))
        if not _system_input:
            # `/continue` mutates these exact objects; keep the references
            # aligned with the state carried by the main REPL.
            handle_meta_command._last_agent_state = agent_state
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = user_input
            handle_meta_command._last_existing_session = interactive_session
        if args.depth == 0:
            current_live_session = session_store.sync_runtime(
                current_live_session, agent_state, chat_history,
                cwd=_session_start_cwd,
                objective=agent_state.get("objective") if not _system_input else None,
                last_user_input=None if _system_input else user_input,
                exit_reason=response.get("exit_reason") if not _system_input else None,
                tasks=task_manager.export_active_tasks(cwd=_session_start_cwd),
            )
            handle_meta_command._current_live_session = current_live_session
            save_resume_state(agent_state, chat_history, _session_start_cwd)

        if injected_done is not None:
            injected_done.set()


if __name__ == "__main__":
    main()
