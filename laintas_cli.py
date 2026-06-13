#!/usr/bin/env python3
"""
laintas_cli — Autonomous AI agent for your terminal.
Same agent loop as Helpwo, but executes real system commands.

Usage:
    laintas-cli                    # Start interactive session in cwd
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
import signal
import socket
import tempfile
import platform
import webbrowser
import threading
import subprocess
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass, field
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── OS Detection (must come before Unix-specific imports) ────────────────
SYSTEM = platform.system()  # "Linux", "Windows", "Darwin"
IS_WINDOWS = SYSTEM == "Windows"

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
from rich.console import Console
from rich.panel import Panel
from rich.padding import Padding
from rich.markdown import Markdown
from rich.table import Table
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.controls import BufferControl

console = Console()

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
    list_runtime_config, reset_runtime_config,
    prepare_state_for_repl,
    get_user_interrupt_event, get_user_message_queue,
    clear_loop_command_cache,
    stop_trigger_scanner,
    save_session_snapshot, load_session_snapshot,
)

import tools as tools_mod    # noqa: E402 — load after agent_loop so registry inits once
import skills as skills_mod  # noqa: E402
import paths                 # Centralized path management
import migrate as migrate_mod  # Auto-migration from old layout
import hwo_ui as hwo_ui_mod  # /hwo orchestration UI

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
    return "https://helpwo.laintas.com"

BACKEND_URL = os.environ.get("LAINTAS_BACKEND") or _detect_backend()
LAINTAS_BASE = os.environ.get("LAINTAS_BASE", "https://laintas.com")
SESSION_FILE = paths.SESSION_FILE
CONFIG_FILE = paths.CONFIG_FILE
HEARTBEAT_INTERVAL = 30

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




def _marker_poll_exec(session, command: str, timeout: int = 60, strip_ansi_codes: bool = True) -> dict:
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

    # ── start (non-blocking) ─────────────────────────────────~~~~~~~~~

    def start(self) -> None:
        """Start the command in a sub-terminal. Non-blocking."""
        if self._alive:
            return
        self._start_time = time.time()

        if self._use_tmux:
            self._tmux_window = f"laintas-{os.getpid()}-{uuid.uuid4().hex[:6]}"
            safe_cmd = self.command.replace("'", "'\\''")
            # -d: don't switch to the new window (terminal 0 stays active)
            os.system(
                f"tmux new-window -d -n {shlex.quote(self._tmux_window)} "
                f"'{DEFAULT_SHELL} -c {shlex.quote(safe_cmd)}'"
            )
            self._alive = True
        else:
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
            for line in decoded.split('\n'):
                if line:
                    # Send literal text
                    escaped = line.replace("'", "'\\''")
                    os.system(f"tmux send-keys -t {shlex.quote(self._tmux_window)} -l '{escaped}'")
                # Send Enter
                os.system(f"tmux send-keys -t {shlex.quote(self._tmux_window)} Enter")
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
            os.system(f"tmux kill-window -t {shlex.quote(self._tmux_window)} 2>/dev/null")
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

def display_command_output(command: str, returncode: int, output: str, depth: int = 0, elapsed: float = 0.0) -> None:
    """Display command output in a collapsed Rich Panel.

    Shows a compact summary: command name, exit status, elapsed time,
    preview of output, and size info. Full output is stored in agent state
    for AI context and viewable via /debug.

    depth=0: user's terminal, full width panel
    depth>=1: sub-agent tool, indented panel with dimmed border
    """
    lines = output.split("\n") if output else []
    line_count = len(lines)
    byte_count = len(output.encode("utf-8", errors="replace"))

    if returncode == 0:
        status_text = "OK"
        base_style = "green"
    elif returncode == -1:
        status_text = "RUNNING"
        base_style = "yellow"
    else:
        status_text = f"EXIT {returncode}"
        base_style = "red"

    # At depth>=1, dim the border to distinguish from user's terminal output
    border_style = base_style if depth == 0 else f"dim {base_style}"

    # Build compact title with optional elapsed time
    time_part = ""
    if elapsed > 0:
        if elapsed < 1:
            time_part = f" {elapsed * 1000:.0f}ms"
        elif elapsed < 60:
            time_part = f" {elapsed:.1f}s"
        else:
            m, s = divmod(int(elapsed), 60)
            time_part = f" {m}m{s}s"
    title = f"[bold]{command[:80]}[/bold]  [{border_style}]{status_text}{time_part}[/{border_style}]"

    preview = output[:200]

    if line_count == 0 and byte_count == 0:
        summary = "[dim](no output)[/dim]"
    else:
        summary = f"[dim]{line_count} lines, {byte_count} bytes  |  use /debug to view full output[/dim]"

    body = f"[dim]{preview}[/dim]\n\n{summary}"

    panel = Panel(body, title=title, border_style=border_style)
    if depth > 0:
        console.print(Padding(panel, (0, 0, 0, depth * 4)))
    else:
        console.print(panel)


def display_sub_terminal_preview(command: str, output: str, depth: int = 0, alive: bool = True) -> None:
    """Show a compact preview of sub-terminal output — last 8 lines (tail).

    For interactive programs (claude, vim, etc.), the most recent output
    is at the bottom, so we show the tail. ANSI escape sequences are
    stripped for readability.
    """
    clean = strip_ansi(output) if output else ""
    all_lines = [l for l in clean.split("\n") if l.strip()] if clean else []
    total_lines = len(all_lines)

    if total_lines > 8:
        preview_lines = all_lines[-8:]
        preview = "\n".join(preview_lines)
        preview = f"[dim]... ({total_lines} lines total)[/dim]\n{preview}"
    elif all_lines:
        preview = "\n".join(all_lines)
    else:
        preview = "(no output yet)"

    status = "[dim yellow]RUNNING[/dim yellow]" if alive else "[dim red]EXITED[/dim red]"
    title = f"[bold]{command[:80]}[/bold]  {status}"
    panel = Panel(f"[dim]{preview}[/dim]", title=title, border_style="dim yellow" if alive else "dim red")
    if depth > 0:
        console.print(Padding(panel, (0, 0, 0, depth * 4)))
    else:
        console.print(panel)


# ── prompt_toolkit Input Setup ──────────────────────────────────────────

class MetaCompleter(Completer):
    """Context-aware completer: /-commands, shell commands from PATH, and paths."""

    META_COMMANDS = [
        "/help", "/login", "/name", "/memory", "/prop",
        "/scan", "/debug", "/cwd",
        "/station", "/terminate", "/send", "/hire", "/agents",
        "/t", "/term",
        "/clear", "/exit", "/quit",
    ]

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
        # /-command completion
        if text.startswith("/"):
            for cmd in self.META_COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))
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
    """Build prompt_toolkit Style for the prompt."""
    return Style.from_dict({
        "prompt-path": "bold #4e9aed",
        "separator": "#666666",
    })


def _build_keybindings() -> KeyBindings:
    """Build custom keybindings for the prompt."""
    kb = KeyBindings()

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
            enable_history_search=True,
            vi_mode=False,
        )
    return _prompt_session


def pt_prompt(cwd: str) -> str:
    """Read user input with prompt_toolkit (PTY-based terminal input)."""
    session = get_prompt_session()
    # Build the prompt line with styled path
    prompt_html = f"<prompt-path>{cwd}</prompt-path>\n<separator>$</separator> "
    try:
        user_input = session.prompt(
            [("class:prompt-path", cwd), ("", "\n$ ")],
            style=_build_prompt_style(),
            multiline=False,
        )
        return user_input.strip() if user_input else ""
    except (KeyboardInterrupt, EOFError):
        return ""
    except Exception:
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
        try:
            return json.loads(SESSION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_session(session: dict) -> None:
    """Save session token to ~/.laintas/session.json."""
    SESSION_FILE.write_text(json.dumps(session, indent=2))
    SESSION_FILE.chmod(0o600)


def clear_session() -> None:
    """Remove saved session."""
    SESSION_FILE.unlink(missing_ok=True)


def load_config() -> dict:
    """Load CLI config (agent name, backend url, etc.)."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict) -> None:
    """Save CLI config."""
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    CONFIG_FILE.chmod(0o600)


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
    backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
    headers = get_auth_headers(session)
    cookies = get_auth_cookies(session)
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
    selected = [0]
    if current:
        for i, model in enumerate(models):
            if model.get("id") == current:
                selected[0] = i
                break

    def _build_lines():
        lines = []
        lines.append(("bold cyan", "Models — choose with ↑↓ and Enter\n"))
        lines.append(("dim", "─" * 72 + "\n"))

        import shutil
        term_h = shutil.get_terminal_size().lines
        list_h = max(4, term_h - 5)
        start = 0
        if len(models) > list_h:
            half = list_h // 2
            start = max(0, min(selected[0] - half, len(models) - list_h))
        end = min(start + list_h, len(models))

        if start > 0:
            lines.append(("dim", f"  ... {start} more above ...\n"))

        for idx in range(start, end):
            model = models[idx]
            model_id = model.get("id", "")
            provider = model.get("description") or model.get("provider") or ""
            prefix = "▶" if idx == selected[0] else " "
            current_mark = " *" if current and model_id == current else "  "
            style = "class:selected" if idx == selected[0] else ""
            lines.append((style, f" {prefix}{current_mark} [cyan]{model_id:30}[/cyan] {provider}\n"))

        if end < len(models):
            lines.append(("dim", f"  ... {len(models) - end} more below ...\n"))

        lines.append(("", "\n"))
        lines.append(("dim", " ↑↓ navigate  ↵ select  Esc/q cancel"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected[0] = max(0, selected[0] - 1)

    @kb.add("down")
    def _(event):
        selected[0] = min(len(models) - 1, selected[0] + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=models[selected[0]])

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    layout = Layout(HSplit([Window(content=FormattedTextControl(_build_lines))]))
    style = Style.from_dict({"selected": "reverse"})
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=True,
        refresh_interval=0.05,
    )
    return app.run()


# ── Authentication ──────────────────────────────────────────────────────

def verify_session(session: dict) -> Optional[dict]:
    """Verify a saved session token with laintas.com. Returns {id, name, email} or None."""
    cookies = session.get("cookies", {})
    headers = session.get("headers", {})
    token = session.get("token", "")
    # Call get-session to get full user info.
    req_args = None
    # Try __Secure- prefixed cookie first (laintas.com uses cross-subdomain cookies)
    for cookie_name in ["__Secure-better-auth.session_token", "better-auth.session_token"]:
        tok = cookies.get(cookie_name, "")
        if tok:
            req_args = {"cookies": {cookie_name: tok}}
            break
    if not req_args and headers.get("Authorization"):
        req_args = {"headers": headers}

    if req_args:
        try:
            resp = requests.get(f"{LAINTAS_BASE}/api/auth/get-session",
                                timeout=5, **req_args)
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


def login_interactive() -> Optional[dict]:
    """Interactive login flow — username+password via Better Auth. Returns session dict or None."""
    console.print(Panel(
        "[bold]Login to Laintas[/bold]\n\n"
        "Enter your laintas.com username and password.\n"
        f"Don't have an account? Visit [link={LAINTAS_BASE}/login]{LAINTAS_BASE}/login[/link] to sign up.",
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
        resp = requests.post(
            f"{LAINTAS_BASE}/api/auth/sign-in/username",
            json={"username": username.strip(), "password": password},
            headers={"Content-Type": "application/json", "Origin": f"{LAINTAS_BASE}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token", "")
            user = data.get("user", {})
            user_id = user.get("id", "")
            if token:
                # Get the actual signed cookie from response (token in JSON is unsigned)
                cookie_val = resp.cookies.get("__Secure-better-auth.session_token", "")
                session = {
                    "token": token,
                    "userId": user_id,
                    "userName": user.get("name", ""),
                    "userEmail": user.get("email", ""),
                    "cookies": {"__Secure-better-auth.session_token": cookie_val} if cookie_val else {"better-auth.session_token": token},
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
    except requests.RequestException as e:
        console.print(f"[yellow]Cannot reach {LAINTAS_BASE}: {e}[/yellow]")

    # ── Method 2: Paste session token (fallback) ──
    console.print("\n[dim]Or paste a session token from your browser cookies.[/dim]")
    token = input("Session token (or press Enter to cancel): ").strip()
    if not token.strip():
        return None

    session = {
        "cookies": {"__Secure-better-auth.session_token": token.strip()},
        "headers": {},
    }
    user_info = verify_session(session)
    if not user_info:
        session["cookies"] = {"better-auth.session_token": token.strip()}
        user_info = verify_session(session)
    if user_info:
        session["userId"] = user_info["id"]
        session["userName"] = user_info.get("name", "")
        session["userEmail"] = user_info.get("email", "")
        save_session(session)
        console.print(f"[green]Logged in as {user_info['id']}[/green]")
        return session

    # Try Authorization header
    session = {
        "cookies": {},
        "headers": {"Authorization": f"Bearer {token.strip()}"},
    }
    user_info = verify_session(session)
    if user_info:
        session["userId"] = user_info["id"]
        session["userName"] = user_info.get("name", "")
        session["userEmail"] = user_info.get("email", "")
        save_session(session)
        console.print(f"[green]Logged in as {user_info['id']}[/green]")
        return session

    console.print("[red]Invalid session token.[/red]")
    return None


def login_via_browser() -> Optional[dict]:
    """Open browser to laintas.com for authentication.

    Starts a local HTTP callback server, opens the user's browser to
    laintas.com/login (or /register). After the user authenticates,
    laintas.com redirects back to localhost with the session token.

    Returns a session dict on success, None on failure/timeout.
    """
    # Find a free port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    result = {"code": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if code:
                result["code"] = code
                self._respond(200, "<h1>Login Successful</h1><p>You can close this tab and return to the terminal.</p>")
            else:
                result["error"] = "No authorization code in callback"
                self._respond(400, "<h1>Error</h1><p>No authorization code received from laintas.com.</p>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _respond(self, http_code, body):
            self.send_response(http_code)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif;text-align:center;padding-top:3em'>{body}</body></html>".encode())

        def log_message(self, format, *args):
            pass

    callback_url = f"http://localhost:{port}/callback"
    login_url = f"{LAINTAS_BASE}/login?redirect={callback_url}"

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
        # Exchange the one-time code for the actual session token
        try:
            resp = requests.post(
                f"{LAINTAS_BASE}/api/auth/cli-exchange",
                json={"code": result["code"]},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token", "")
                if token:
                    session = {
                        "token": token,
                        "cookies": {"__Secure-better-auth.session_token": token},
                        "headers": {},
                    }
                    user_info = verify_session(session)
                    if user_info:
                        session["userId"] = user_info["id"]
                        session["userName"] = user_info.get("name", "")
                        session["userEmail"] = user_info.get("email", "")
                        save_session(session)
                        display = user_info.get("email") or user_info.get("name") or user_info["id"]
                        console.print(f"[green]Logged in as {display}[/green]")
                        return session
                    else:
                        console.print("[red]Token verification failed.[/red]")
                        return None
                else:
                    console.print("[red]Server returned empty token.[/red]")
                    return None
            else:
                console.print(f"[red]Code exchange failed: {resp.text[:200]}[/red]")
                return None
        except requests.RequestException as e:
            console.print(f"[red]Cannot reach {LAINTAS_BASE} for token exchange: {e}[/red]")
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
        "[1] [bold]Remote login[/bold] — opens browser to laintas.com\n"
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

    The template uses XML-style sections (Anthropic's recommended pattern —
    Claude attends to them better than ALL-CAPS brackets) and teaches the
    agent the full surface: shell, /tool dispatch, /term, /spawn, memory.

    Variables substituted at run time (see agent_loop.run_agent_loop):
      {{agentName}} {{agentId}} {{currentPath}} {{depth}} {{nextDepth}}
      {{activeFile}} {{globalMemory}} {{persistentMemory}} {{lastSession}}
      {{planMode}} {{tools}} {{inbox}} {{children}} {{parent}}
      {{terminalName}} {{parentTerminal}} {{deploymentStatus}}
      {{workflowPhase}} {{rolePrompt}} {{confidenceGuidance}}
      {{skillContext}} {{parallelResults}} {{behaviorDiagnostics}}
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

If a skill looks relevant, call `skill.load` with its name before doing specialized work.
Do not assume unloaded skill instructions. After loading, continue using the instructions below.

Loaded skill instructions:
{{{{skillContext}}}}
</skills>

<tools>
Return actions only inside the JSON `tool_calls` array.
Use `shell.exec` for shell commands and meta-commands. Use structured tools for file, memory, task, plan, web, agent, terminal, time, and sleep operations.
Never write XML, HTML, pseudo-tags, or text wrappers such as `<tool_calls>...</tool_calls>`.
To list skills, emit:
{{"reply":"Listing available skills.","tool_calls":[{{"name":"skill.list","arguments":{{}}}}]}}
To load a skill, emit:
{{"reply":"Loading the relevant skill.","tool_calls":[{{"name":"skill.load","arguments":{{"name":"react-project"}}}}]}}
Catalog:
{{{{tools}}}}
</tools>

<workflow>
- If the user asks a clear read/edit/build/test/investigate task, act with tools. Do not ask for permission to do exactly what was asked.
- Ask one concise clarifying question only when the target or intent is genuinely ambiguous, destructive, or impossible to infer safely.
- For unfamiliar code: search/list, read the relevant region, then edit. Never patch from memory.
- Keep each action small and verifiable. Prefer exact edits over rewrites.
- After a tool failure, read the error and change approach; do not retry identical arguments.
- Verify before claiming completion when verification is practical.
- Save durable user/project preferences with memory tools when the user corrects you or establishes a non-obvious rule.
{{{{workflowPhase}}}}
{{{{rolePrompt}}}}
{{{{confidenceGuidance}}}}
</workflow>

<output_rules>
Every response must be exactly one JSON object:
{{
  "reply": "Brief user-facing text. Markdown OK. Cite files as path:line.",
  "tool_calls": [
    {{"name": "tool.name", "arguments": {{}}}}
  ]
}}

- No prose outside JSON. No code fences.
- Never output `<tool_calls>`, `<invoke>`, `/tool ...` text, or any XML-like tool syntax.
- `reply` is normally 1-3 sentences.
- `tool_calls: []` means answered, complete, or blocked waiting for the user.
- Multiple tool calls are allowed when they are independent or sequentially safe.
</output_rules>

<tone>
Direct, terse, and useful. Match the user's language. Do not add filler or final checklists.
</tone>

<safety>
Do not bypass policy. Do not invent paths, APIs, files, or results. Do not commit, push, deploy, publish, or run destructive commands without explicit user request.
</safety>
"""

    return f"""<role>
You are {{{{agentName}}}} (id: {{{{agentId}}}}, role: {{{{deploymentStatus}}}}), an autonomous coding agent running inside a laintas-cli REPL.
You earn your keep by solving real engineering tasks: explore the codebase, edit files, run commands, verify the result, and report back tersely. Prefer action over explanation once the task is clear — but **confirm intent before touching any file or running any command when the request is conversational, vague, or missing a concrete target**.
</role>

<specialist_role>
{{{{rolePrompt}}}}
</specialist_role>

<environment>
- OS: {SYSTEM} | Shell: {shell_info} | CWD: {{{{currentPath}}}}
- Terminal: {{{{terminalName}}}} | Status: {{{{deploymentStatus}}}}
- Parent terminal: {{{{parentTerminal}}}} | Parent agent: {{{{parent}}}}
- Recursion depth: {{{{depth}}}} (0 = user's terminal; ≥1 = sub-agent in a collapsed panel)
- Children: {{{{children}}}}
- Inbox (messages from other agents this iteration): {{{{inbox}}}}
- Plan mode: {{{{planMode}}}}
- Stationed execution: when deployed to term0, your shell commands (including cd, export, clear) run in a persistent bash session — side effects persist across commands. Use shell.exec("cd ..") normally; it will change the working directory just like the user typing it.
</environment>

<behavior_diagnostics>
{{{{behaviorDiagnostics}}}}
</behavior_diagnostics>

<parallel_results>
{{{{parallelResults}}}}
</parallel_results>

<last_session>
{{{{lastSession}}}}
</last_session>

<persistent_memory>
These memories survive across sessions. Treat them as authoritative context about the user and project.
{{{{persistentMemory}}}}
</persistent_memory>

<project_rules>
Project-local rules stored in .laintas/memory.json. They override defaults; follow them strictly.
{{{{globalMemory}}}}
</project_rules>

<tools>
You have two ways to act, both expressed as entries in the `tool_calls` array of your JSON response:

(A) Shell command — any executable on PATH, with pipes/redirects/substitution. Examples:
    ls -la src/
    grep -rn "TODO" --include="*.py" .
    python -c "import json; print(json.load(open('package.json'))['version'])"

(B) Structured tool — use this form for filesystem, memory, plan, task, and web operations.
    /tool <name> <json-params>

    Example: /tool fs.read {{"path": "src/app.py", "max_bytes": 4000}}

The structured tools below are preferred over raw shell when both work, because they return
clean JSON the loop can use directly, and they don't spawn a PTY. Use shell for: pipelines,
build tools, package managers, git, version control, anything PTY-driven.

Catalog:
{{{{tools}}}}

Meta-commands (use `shell.exec` with the meta-command string when needed):
- /station [name]               station this agent (depth=0 commands run directly, like user input)
- /send <name> <keys>          send keystrokes / commands to a named terminal
- /terminate <name>            close and destroy a terminal
- /t [name]                    list sub-terminals, or /t <name> to create one (no agent stationed)
- /term <name>                 same as /t <name> — spawn a sub-terminal without stationing
- /spawn [name:] <task>        fork an in-process child agent (parent keeps running)
- /tell <agent_id> <message>   send a JSON or text message to another agent's inbox
- /wait <agent_id> [secs]      block until child agent finishes (cap 300s)
- /abort <agent_id>            signal another agent to stop
- /hire                        create a new sibling agent slot
- /agents [name|tree|name X]   inspect or switch agent identity
- /session close               close the current one-off PTY session
- /keys <text>                 send keys to the current one-off PTY session
- wait(N)                      sleep N seconds (e.g. wait(3) after starting a dev server)
</tools>

<workflow>
For every non-trivial task, follow this loop:

1. UNDERSTAND — and ask first when needed.
   Re-state the user's goal in one sentence. **If any of the following are true, ask one clarifying question and return `tool_calls: []` — do NOT start exploring:**
   - The input is conversational or expressive (feedback, complaint, question about behaviour)
   - No concrete file, command, or task target is named
   - The request is short (≲10 words) and could mean multiple things
   - You would have to guess which part of the codebase to touch
   One focused question is better than wrong action. Do not start exploring "to understand" — exploration is action and changes the user's perception of what you're doing.

2. EXPLORE before changing — but report before acting.
   For unfamiliar code use fs.glob → fs.grep → fs.read (offset/limit if large). Cite findings as `path:line`.
   **After exploration, stop and summarise what you found before making any edit or running any write/destructive command.** Return `tool_calls: []` with a summary and your proposed next step, then wait for the user to confirm. Skip this pause only when the user has already explicitly approved the action in this same conversation turn.

3. PLAN before acting. For any non-trivial task, write a brief plan (3–5 bullets) in your `reply` before touching anything. If there are multiple valid approaches, offer 2–3 labelled options and let the user pick — don't silently choose one. For tasks with >3 steps or touching >2 files, also create tasks via task.create. One in_progress task at a time. If plan mode is active, explore only until the user runs /plan approve.

4. ACT in small, verifiable increments. **Work incrementally — never try to do everything in one response.**
   - **One tool call per concern.** Write one file, make one edit, run one command per tool call. Multiple tool calls per turn are fine (they run sequentially), but keep each one small and focused.
   - **Before destructive or hard-to-reverse commands** (rm, overwrite, deploy, bulk rename, db migrations): state in `reply` what you are about to run and why, then execute. This gives the user a chance to redirect if the summary looks wrong.
   - **fs.write: ≤ 50 lines per call.** If a file needs more content, write the skeleton first, then use fs.edit to add sections one at a time. Example: write index.html with basic structure (head, empty body, empty script tag), then fs.edit to add the body content, then fs.edit to add the JavaScript.
   - **fs.edit: replace small, unique snippets.** Don't try to replace entire blocks — find a small unique anchor string and replace just that section.
   - **Self-check before writing:** If you're about to write more than 50 lines of content in a single fs.write call, STOP. Split it: write the skeleton now, add content in the next turn via fs.edit.
   - Prefer fs.edit (exact string replacement) for code changes; fs.write only for new files or initial scaffolding. After edits, run the project's test/typecheck/lint command if one is obvious (package.json scripts, pytest, cargo test, etc.).

5. VERIFY before claiming done. Read the file back, run the test, exit-code-check the build. Saying "done" without verification is the most common failure mode — avoid it.

6. RECOVER from errors. If a command fails: read the error, decide if it's transient (retry once with a fix) or fundamental (change approach). Never re-run the identical failing command. Look at the inline `[error: ...]` hints the loop adds to terminal output — they classify the failure for you.

**CRITICAL: fs.edit failure recovery.** If fs.edit fails with "old_string not found", DO NOT fall back to fs.write with the entire file content. This will exceed token limits and fail. Instead:
   a. Use fs.read to see the current file content
   b. Identify a smaller, unique substring that actually exists in the file
   c. Use multiple smaller fs.edit calls to make incremental changes

7. REMEMBER what's worth remembering. Use the `rules` field for project-local facts ("uses Vite", "tests live in __tests__/"). Use `/tool mem.save` for cross-session memories — **save immediately** when:
   - The user corrects you ("no", "wrong", "don't do that", "I prefer")
   - A non-obvious project constraint or decision is established
   - The user reveals their role, expertise level, or workflow preference
   Don't save derivable facts (git history, current file layout). The `<last_session>` block above gives you continuity from the previous session in this directory.
</workflow>

<workflow_phase>
{{{{workflowPhase}}}}
</workflow_phase>

<skill_context>
{{{{skillContext}}}}
</skill_context>

<output_rules>
Every response is a single JSON object with two fields:

  {{
    "reply": "Brief user-facing text. Markdown OK. Reference files as path:line.",
    "tool_calls": [
      {{"name": "tool.name", "arguments": {{...}}}},
      ...
    ]
  }}

- `reply`: 1-3 sentences for the user. Longer only when summarizing a finished task or asking a clarifying question. No filler ("Sure!", "I'll now..."). Don't restate what the command obviously does.
- `tool_calls`: array of actions to execute, in order. **Empty array `[]` means the task is fully complete, OR you're blocked waiting on the user.** If your reply already answers the question or completes the task, return `tool_calls: []` — do NOT add a redundant verification command (e.g., running `pwd` after `cd` succeeded). Multiple tool calls ARE allowed (they run sequentially).

{{{{confidenceGuidance}}}}

**CRITICAL: Keep each tool call small to avoid token overflow.**
- fs.write content: ≤ 50 lines. For larger files, write skeleton first, then add via fs.edit in subsequent turns.
- fs.edit old_string/new_string: ≤ 20 lines each. Find small unique anchors.
- If you find yourself about to generate a tool call with >100 lines of content, STOP and split it across multiple turns.
- This keeps responses within token limits and makes debugging easier.

Every entry in `tool_calls` has:
  - `name`: a tool name from the catalog (e.g. "shell.exec", "fs.read", "agent.spawn")
  - `arguments`: a JSON object with the tool's parameters (see <tools> for schemas)

Quick reference of available tool names:
  shell.exec — run any shell command (your primary action tool; wraps cd, git, pytest, npm, etc.)
  fs.read, fs.edit, fs.write, fs.glob, fs.grep, fs.ls, fs.diff, fs.multi_edit
  agent.spawn, agent.tell, agent.station, agent.abort, agent.wait, agent.hire, agent.list, agent.rename, agent.switch
  terminal.send, terminal.terminate, terminal.create, terminal.list
  session.close, session.keys
  mem.save, mem.read, mem.delete, mem.list
  task.create, task.update, task.list, task.get
  plan.read, plan.update, plan.list
  web.search, web.fetch
  time.now, sleep

JSON must parse. No prose outside the object. No code fences around it.
</output_rules>

<tone>
- Terse. Direct. No preamble, no flattery, no "Great question!". No final summary if a diff or test result already shows what changed.
- Cite code as `path:line` (e.g. `agent_loop.py:1042`). Never use placeholder file paths.
- Use markdown lists/code blocks in `reply` when they help, not as decoration.
- Match the user's language for replies (English if uncertain). Code identifiers stay English regardless.
</tone>

<safety>
- Destructive commands (`rm -rf`, `mkfs`, `dd of=/dev/...`, `chmod 777 /`) are blocked by the local policy engine. Do not try to bypass it. If a user explicitly asks for a destructive action, run the policy-approved equivalent (e.g. `rm` on specific files).
- Never invent file paths, package names, or APIs. If unsure, read first.
- Do not commit, push, or publish without an explicit request from the user.
- Do not run `curl ... | sh` or pipe untrusted input into a shell.
</safety>

<recursive_orchestration>
You can delegate sub-tasks two ways:

- `agent.spawn` — in-process child agent. Cheap, shares process state, posts result to your inbox.
- shell: `laintas-cli --execute "task description" --depth {{{{nextDepth}}}}` — full sub-process. Heavier, but isolated.

Rule of thumb: spawn for parallel exploration ("read these 3 files in parallel"); execute for isolated long-running tasks. At depth ≥ 2 prefer doing the work yourself — deeper recursion costs more than it saves.

For parent-terminal side effects (cd, clear) at depth ≥ 1, use `parent(cmd)` form.
</recursive_orchestration>

<examples>
Task: "What does this project do?"
{{"reply": "Reading the README.", "tool_calls": [{{"name": "fs.read", "arguments": {{"path": "README.md", "max_bytes": 4000}}}}]}}

Task (next iteration, after seeing README):
{{"reply": "It's a Python CLI that turns a backend LLM into a terminal coding agent. Entrypoint is `laintas_cli.py:1`; the loop lives in `agent_loop.py:1018`.", "tool_calls": []}}

Task: "Fix the typo on line 42 of utils.py"
{{"reply": "Replacing `recieve` with `receive` in utils.py:42.", "tool_calls": [{{"name": "fs.edit", "arguments": {{"path": "utils.py", "old_string": "recieve", "new_string": "receive"}}}}]}}

Task: "Run the tests"
{{"reply": "Running pytest.", "tool_calls": [{{"name": "shell.exec", "arguments": {{"command": "pytest -x --tb=short"}}}}]}}

Task complete after the test passes:
{{"reply": "All 23 tests pass. The typo fix didn't break anything.", "tool_calls": []}}

Task: needs clarification
{{"reply": "Which `config.json` did you mean — the one at the repo root or in `src/`? Both exist.", "tool_calls": []}}

Task: "Create an HTML page with a timeline"
Step 1 — write skeleton:
{{"reply": "Creating index.html skeleton.", "tool_calls": [{{"name": "fs.write", "arguments": {{"path": "index.html", "content": "<!DOCTYPE html>\n<html>\n<head><title>Timeline</title></head>\n<body>\n<!-- content here -->\n</body>\n</html>"}}}}]}}

Step 2 (next turn) — add body content via edit:
{{"reply": "Adding timeline content.", "tool_calls": [{{"name": "fs.edit", "arguments": {{"path": "index.html", "old_string": "<!-- content here -->", "new_string": "<h1>Timeline</h1>\n<div class=\"item\">...</div>"}}}}]}}

Step 3 (next turn) — add styles via edit:
{{"reply": "Adding styles.", "tool_calls": [{{"name": "fs.edit", "arguments": {{"path": "index.html", "old_string": "</head>", "new_string": "<style>.item {{ margin: 1em; }}</style>\n</head>"}}}}]}}
</examples>
"""
# ── File Helpers ───────────────────────────────────────────────────────

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


def reload_default_files() -> None:
    """Delete all project files in .laintas/ and restart laintas_cli."""
    proj = paths.project_dir()
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
    os.execv(sys.argv[0], sys.argv)


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


def _extract_json_object(text: str) -> Optional[dict]:
    """Extract the first top-level JSON object from text. Handles code fences,
    leading prose, and trailing prose. Returns None if no object parses."""
    if not text:
        return None
    s = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # Find the first '{' and walk to its matching '}', respecting strings.
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                candidate = s[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


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


def _try_parse_partial_json(text: str) -> Optional[dict]:
    """Try to parse an in-progress JSON object by closing unfinished structures.
    Used for incremental rendering during SSE streaming."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
    start = s.find("{")
    if start < 0:
        return None
    s = s[start:]
    # Quickly attempt a clean parse
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Walk and close open strings + braces
    depth = 0
    in_str = False
    esc = False
    for c in s:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
    closed = s
    if in_str:
        closed += '"'
    closed += "}" * max(depth, 0)
    try:
        return json.loads(closed)
    except json.JSONDecodeError:
        return None


def _legacy_command_to_tool_calls(command: str) -> list:
    """Convert old-style {command: "..."} responses to new tool_calls format.

    Only kept for the rare model regression where it emits a top-level
    `command` field instead of `tool_calls`. Slash-prefixed meta-commands are
    REPL-only and never appear in AI output, so we don't translate them here.
    """
    cmd = (command or "").strip()
    if not cmd:
        return []
    import re as _re_lctc

    # /tool <name> <json_params> — ad-hoc tool dispatch syntax
    m = _re_lctc.match(r'^/tool\s+(\S+)(?:\s+(.+))?$', cmd)
    if m:
        t_name = m.group(1)
        raw = (m.group(2) or "").strip()
        try:
            t_args = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            t_args = {"raw": raw}
        return [{"name": t_name, "arguments": t_args if isinstance(t_args, dict) else {"value": t_args}}]

    # wait(N) / sleep(N)
    m = _re_lctc.match(r'^(?:wait|sleep)\((\d+(?:\.\d+)?)\)\s*$', cmd)
    if m:
        return [{"name": "sleep", "arguments": {"seconds": float(m.group(1))}}]

    # Plain shell command (most common fallback)
    return [{"name": "shell.exec", "arguments": {"command": cmd}}]


def call_backend_stream(
    session: dict,
    message: str,
    system_prompt: str,
    current_path: str,
    history: list = None,
    lang: str = "EN",
    on_chunk: Optional[Callable[[str, str], None]] = None,
    interrupt_event: Optional[threading.Event] = None,
) -> dict:
    """Call Helpwo backend /api/chat/stream, same as Helpwo frontend.
    Returns parsed {reply, command, memory, done, _billing} dict.

    If interrupt_event is provided, it is checked between SSE chunks so the
    request can be aborted gracefully on Ctrl+C.
    """
    backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)

    payload = {
        "message": message,
        "history": history or [],
        "currentPath": current_path,
        "systemPrompt": system_prompt,
        "lang": lang,
        "maxTokens": int(get_runtime_config("max_tokens")),
    }
    selected_model = get_selected_model()
    if selected_model:
        payload["model"] = selected_model
    selected_provider = get_selected_provider()
    if selected_provider:
        payload["provider"] = selected_provider

    headers = get_auth_headers(session)
    cookies = get_auth_cookies(session)

    try:
        response = requests.post(
            f"{backend_url}/api/chat/stream",
            json=payload,
            headers=headers,
            cookies=cookies,
            stream=True,
            timeout=120,
        )

        if response.status_code != 200:
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
        got_any_event = False
        _diag_events: list = []  # diagnostic: capture non-content fields
        prev_reply_for_chunks = ""
        prev_command_for_chunks = ""
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
                billing_info = evt["_billing"]
                continue
            # Capture diagnostic info: any top-level keys beyond choices
            for k in evt.keys():
                if k not in ("choices", "id", "object", "created", "model", "system_fingerprint") and k not in _diag_events:
                    _diag_events.append(k)
            _choices = evt.get("choices")
            delta_content = (
                _choices[0].get("delta", {}).get("content")
                if isinstance(_choices, list) and _choices
                else None
            )
            # Also check for OpenAI-style native tool_calls in delta
            _delta = _choices[0].get("delta", {}) if isinstance(_choices, list) and _choices else {}
            if _delta.get("tool_calls"):
                # Native function calling — these are real tool calls being dropped!
                _diag_events.append(f"TOOL_CALLS_IN_DELTA:{json.dumps(_delta['tool_calls'], ensure_ascii=False)[:200]}")
            if delta_content:
                accumulated += delta_content
                if on_chunk is not None:
                    parsed = _try_parse_partial_json(accumulated)
                    if parsed is not None:
                        cur_reply = parsed.get("reply", "") or ""
                        cur_tool_calls = parsed.get("tool_calls") or []
                        if cur_reply != prev_reply_for_chunks:
                            delta_r = cur_reply[len(prev_reply_for_chunks):] if cur_reply.startswith(prev_reply_for_chunks) else cur_reply
                            try: on_chunk("reply", delta_r)
                            except Exception: pass
                            prev_reply_for_chunks = cur_reply
                        if cur_tool_calls:
                            first = cur_tool_calls[0] if isinstance(cur_tool_calls, list) and cur_tool_calls else None
                            if isinstance(first, dict):
                                _fn = first.get("name", "")
                                _fa = first.get("arguments", {}) or {}
                                # Mirror _salient_arg in agent_loop.py — keep in sync.
                                if not isinstance(_fa, dict):
                                    _salient_preview = ""
                                elif _fn == "shell.exec":
                                    _salient_preview = _fa.get("command", "") or ""
                                elif _fn == "terminal.send":
                                    _salient_preview = f'{_fa.get("name","?")}: {_fa.get("command","")}'
                                elif _fn in ("fs.read", "fs.write", "fs.edit", "fs.multi_edit", "fs.diff"):
                                    _salient_preview = _fa.get("path", "") or ""
                                elif _fn == "fs.grep":
                                    _salient_preview = f'{_fa.get("pattern","")} in {_fa.get("path","")}'
                                elif _fn == "fs.glob":
                                    _salient_preview = _fa.get("pattern", "") or ""
                                elif _fn == "web.fetch":
                                    _salient_preview = _fa.get("url", "") or ""
                                elif _fn == "web.search":
                                    _salient_preview = _fa.get("query", "") or ""
                                else:
                                    try:
                                        _salient_preview = json.dumps(_fa, ensure_ascii=False)[:60]
                                    except (TypeError, ValueError):
                                        _salient_preview = ""
                                preview = f"{_fn} {_salient_preview}".strip()
                                if len(cur_tool_calls) > 1:
                                    preview = f"{preview} (+{len(cur_tool_calls)-1} more)"
                            else:
                                preview = str(first)
                        else:
                            preview = ""
                        if preview != prev_command_for_chunks:
                            try: on_chunk("command", preview)
                            except Exception: pass
                            prev_command_for_chunks = preview

        if not got_any_event:
            return {"reply": "No response from AI", "tool_calls": [], "done": True, "error": True}

        # Extract the JSON object from accumulated text. Model may wrap in ```json fences or prose.
        parsed = _extract_json_object(accumulated)
        if parsed is None:
            tagged_tool_calls = _extract_tagged_tool_calls(accumulated)
            if tagged_tool_calls:
                return {
                    "reply": "",
                    "tool_calls": tagged_tool_calls,
                    "done": False,
                    "_billing": billing_info,
                    "_diag_events": _diag_events + ["parsed_tagged_tool_calls"],
                }
            raw_text = accumulated.strip() or "(no response)"
            # Pure prose (no '{'): content is valid but JSON wrapper is missing.
            # Continue the loop so the model gets a nudge and can still call tools,
            # but mark _prose_only so the nudge is silent (no user-visible warning).
            # Only show the warning when the model was clearly attempting JSON (has '{').
            return {
                "reply": raw_text,
                "tool_calls": [],
                "done": False,
                "error": False,
                "_parse_failed": True,
                "_prose_only": "{" not in accumulated,
                "_billing": billing_info,
                "_diag_events": _diag_events,
            }

        # Normalize tool_calls from response
        recognized_fields = ("reply", "tool_calls", "command", "close_session", "send_keys")
        if not any(k in parsed for k in recognized_fields):
            tagged_tool_calls = _extract_tagged_tool_calls(accumulated)
            if tagged_tool_calls:
                return {
                    "reply": "",
                    "tool_calls": tagged_tool_calls,
                    "done": False,
                    "_billing": billing_info,
                    "_diag_events": _diag_events + ["parsed_tagged_tool_calls"],
                }
            raw_text = accumulated.strip() or json.dumps(parsed, ensure_ascii=False)
            return {
                "reply": raw_text,
                "tool_calls": [],
                "done": False,
                "error": False,
                "_parse_failed": True,
                "_billing": billing_info,
                "_diag_events": _diag_events,
            }

        tool_calls = parsed.get("tool_calls") or []

        # Backward compat: old-style "command" field -> wrap as tool_call
        command = parsed.get("command", "") or ""
        if command and not tool_calls:
            tool_calls = _legacy_command_to_tool_calls(command)

        # Also handle old close_session/send_keys fields
        if not tool_calls:
            if parsed.get("close_session"):
                tool_calls = [{"name": "session.close", "arguments": {}}]
            elif parsed.get("send_keys"):
                tool_calls = [{"name": "session.keys", "arguments": {"keys": parsed["send_keys"]}}]

        # Normalize: ensure each entry has {name, arguments}
        normalized = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name", tc.get("function", "") if isinstance(tc.get("function"), dict) else "")
            name = name or ""
            args = tc.get("arguments", tc.get("parameters", {}))
            # Support OpenAI-style: {"function": {"name": "...", "arguments": {...}}}
            if isinstance(tc.get("function"), dict) and not name:
                name = tc["function"].get("name", "")
                args = tc["function"].get("arguments", {})
            if name:
                normalized.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
        tool_calls = normalized

        return {
            "reply": parsed.get("reply", "") or "",
            "tool_calls": tool_calls,
            "done": len(tool_calls) == 0,
            "error": False,
            "_billing": billing_info,
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
                f"?sessionId={self.session_id}&role=host&agentSecret={self.agent_secret}")

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
            self._ws = connect(self._ws_url(), open_timeout=10, max_size=None)
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
                data = os.read(self.fd, 65536)
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
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._message_poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._session: Optional[dict] = None
        self._processing_message = threading.Event()
        self._pending_responses: list = []  # thread-safe queue for responses to send

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

        payload = {
            "name": self.agent_name,
            "hostname": hostname,
            "os": SYSTEM,
            "shell": SHELL_NAME,
            "cwd": cwd,
            "userEmail": user_email,
            "userName": user_name,
            "goal": f"CLI agent '{self.agent_name}' on {hostname} ({SYSTEM})",
        }

        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(session)
        cookies = get_auth_cookies(session)

        try:
            resp = requests.post(
                f"{backend_url}/api/agents/register",
                json=payload,
                headers=headers,
                cookies=cookies,
                timeout=5,
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
        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(self._session) if self._session else {}
        cookies = get_auth_cookies(self._session) if self._session else {}
        try:
            requests.post(
                f"{backend_url}/api/agents/{self.agent_id}/events",
                json={
                    "agentSecret": self.agent_secret,
                    "events": events,
                    "state": {"cwd": os.getcwd(), "status": "running"},
                },
                headers=headers,
                cookies=cookies,
                timeout=5,
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
        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(self._session) if self._session else {}
        cookies = get_auth_cookies(self._session) if self._session else {}

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
                    "agentSecret": self.agent_secret,
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

                requests.post(
                    f"{backend_url}/api/agents/heartbeat",
                    json=payload,
                    headers=headers,
                    cookies=cookies,
                    timeout=5,
                )
            except requests.RequestException:
                pass  # heartbeat failures are silent

            time.sleep(float(get_runtime_config("heartbeat_interval")))

    def start_message_poll(self, agent_state_cb, chat_history_cb):
        """Start background thread to poll for incoming messages from Helpwo UI.

        Args:
            agent_state_cb: callable() → dict — returns current agent state
            chat_history_cb: callable() → list — returns current chat history
        """
        if not self.agent_id or not self._session:
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
        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(self._session) if self._session else {}
        cookies = get_auth_cookies(self._session) if self._session else {}

        while self._running and self.agent_id:
            try:
                resp = requests.get(
                    f"{backend_url}/api/agents/{self.agent_id}/poll",
                    params={"agentSecret": self.agent_secret},
                    headers=headers,
                    cookies=cookies,
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    messages = data.get("inputs", [])
                    for msg in messages:
                        self._handle_remote_message(
                            msg, agent_state_cb, chat_history_cb,
                        )
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
            elif kind == "abort":
                self._handle_abort(req_id, payload)
            elif kind == "approval-response":
                self._handle_approval_response(req_id, payload)
            else:
                self._push_final(req_id, "fail", f"unknown kind '{kind}'")
        except Exception as e:
            console.print(f"[red]Error handling remote message: {e}[/red]")
            if req_id:
                self._push_final(req_id, "fail", f"handler exception: {e}")
        finally:
            self._processing_message.clear()

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
        if decision.action == "needs_approval":
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
                data = dict(os.environ)
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
        commands. Set runtime config `disable_remote_terminal` to turn it off.
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

        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
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

        max_loops_val = int(payload.get("maxLoops", get_runtime_config("max_loops")))
        # Temporarily override max_loops for this delegation
        old_max = get_runtime_config("max_loops")
        set_runtime_config("max_loops", min(max_loops_val, 20))

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
            console=console, Markdown=Markdown,
            pty_passthrough=pty_passthrough,
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
            set_runtime_config("max_loops", old_max)
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

        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(self._session) if self._session else {}
        cookies = get_auth_cookies(self._session) if self._session else {}

        try:
            requests.post(
                f"{backend_url}/api/agents/unregister",
                json={"agentId": self.agent_id, "agentSecret": self.agent_secret},
                headers=headers,
                cookies=cookies,
                timeout=5,
            )
        except requests.RequestException:
            pass
        self.agent_id = None
        self.agent_secret = ""


# ── Debug Display ───────────────────────────────────────────────────────

def show_debug_browser_interactive() -> None:
    """Interactive debug browser — arrow keys to select, Enter to view detail, q/Esc to exit."""
    entries = get_debug_logs()[:20]
    if not entries:
        console.print("[dim]No debug entries yet. Run an AI command to see data.[/dim]")
        return

    selected = [0]  # mutable for closure capture

    def _build_lines():
        lines = []
        lines.append(("bold cyan", "Debug Log — Last 20 Entries\n"))
        lines.append(("dim", "↑↓ navigate  ↵ view detail  q/Esc back\n\n"))

        for i, e in enumerate(entries):
            prefix = "▶" if i == selected[0] else " "
            ts = e.timestamp[-19:] if len(e.timestamp) > 19 else e.timestamp
            if e.request_body:
                tag = "AI "
                summary = e.reply[:60] or e.command[:60] or "(no response)"
            else:
                tag = "CMD"
                summary = e.exec_command[:60] or "(no output)"
            summary = summary.replace("\n", " ")[:60]
            style = "class:selected" if i == selected[0] else ""
            lines.append((style, f" {prefix} #{i+1:2d}  {ts}  [{tag}]  {summary}\n"))

        lines.append(("", "\n"))
        lines.append(("dim", f"{len(entries)} entries shown.  /debug clear | /debug <N> | /debug <N> <file>"))
        return lines

    def _get_text():
        return _build_lines()

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected[0] = max(0, selected[0] - 1)

    @kb.add("down")
    def _(event):
        selected[0] = min(len(entries) - 1, selected[0] + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=selected[0])

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=-1)

    style = Style.from_dict({
        "selected": "reverse",
    })

    text_control = FormattedTextControl(_get_text)
    window = Window(content=text_control, always_hide_cursor=False)
    layout = Layout(HSplit([window]))

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=True,
        refresh_interval=0.1,
    )

    result = app.run()
    if result >= 0:
        show_debug_detail(result)
        # After viewing detail, wait for key then return to browser
        input("\n[dim]Press Enter to return to debug browser...[/dim]")
        show_debug_browser_interactive()  # loop back


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
        if getattr(e, "memory", None):
            lines.append(f"[bold]Memory:[/bold] {e.memory[:500]}")
        lines.append(f"[bold]Done:[/bold] {e.done}")
        if e.billing:
            cost = e.billing.get("costCents", 0)
            balance = e.billing.get("balanceCents", 0)
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
    items: list = []  # [(display_name, command, session, created_at, is_alive)]

    # term0 is the shell laintas_cli runs on — "entering" it just drops to a
    # raw bash that requires `exit` to return, which is confusing and useless.
    # Show named sub-terminals only.
    for term in get_all_terminals():
        if term.name == "term0":
            continue
        items.append((term.name, term.command, term.session,
                      term.created_at, term.session is not None and term.session.is_alive()))

    if not items:
        console.print("[dim]No sub-terminals. Use /t <name> to create one, "
                      "or let the AI spawn a command.[/dim]")
        return

    selected = [0]

    def _build_lines():
        lines = []
        lines.append(("bold cyan", "Terminal Manager\n"))
        lines.append(("dim", "↑↓ navigate  ↵ enter  o observe  c close  d details  q back\n\n"))

        for i, (name, cmd, sess, created, alive) in enumerate(items):
            prefix = "▶" if i == selected[0] else " "
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
            style = "class:selected" if i == selected[0] else ""
            lines.append((style, f" {prefix} [bold]{name}[/bold]  {status}{uptime_str}  "
                                  f"[dim]{cmd_preview}[/dim]\n"))

        lines.append(("", "\n"))
        lines.append(("dim", f"{len(items)} terminal(s).  "
                      "Enter=embody  o=observe  c=close  d=details  q=back"))
        return lines

    def _get_text():
        return _build_lines()

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected[0] = max(0, selected[0] - 1)

    @kb.add("down")
    def _(event):
        selected[0] = min(len(items) - 1, selected[0] + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=("enter", selected[0]))

    @kb.add("o")
    def _(event):
        event.app.exit(result=("observe", selected[0]))

    @kb.add("c")
    def _(event):
        event.app.exit(result=("close", selected[0]))

    @kb.add("d")
    def _(event):
        event.app.exit(result=("details", selected[0]))

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=("quit", -1))

    style = Style.from_dict({"selected": "reverse"})
    text_control = FormattedTextControl(_get_text)
    window = Window(content=text_control, always_hide_cursor=False)
    layout = Layout(HSplit([window]))

    app = Application(
        layout=layout, key_bindings=kb, style=style,
        full_screen=True, refresh_interval=0.5,
    )

    while True:
        result = app.run()
        action, idx = result

        if action == "quit":
            break

        if idx < 0 or idx >= len(items):
            continue

        name, cmd, sess, created, alive = items[idx]

        if action == "enter":
            if not alive:
                console.print("\n[yellow]Session has already ended.[/yellow]")
                input("[dim]Press Enter to continue...[/dim]")
                continue
            enter_session(sess, display_name=name, display_cmd=cmd)
            # After detaching from enter_session, the terminal may need
            # a moment to restore before prompt_toolkit takes over again
            time.sleep(0.1)
            # If session died during enter, unregister it
            if name != "term0 (primary)" and not sess.is_alive():
                unregister_terminal(name)

        elif action == "observe":
            if not alive:
                console.print("\n[yellow]Session has already ended.[/yellow]")
                input("[dim]Press Enter to continue...[/dim]")
                continue
            observe_session(sess, display_name=name, display_cmd=cmd)

        elif action == "close":
            if name == "term0 (primary)":
                console.print("\n[yellow]Cannot close the primary session. "
                              "Use /exit or close the parent terminal.[/yellow]")
                input("[dim]Press Enter to continue...[/dim]")
                continue
            unregister_terminal(name)
            console.print(f"\n[green]Closed terminal [bold]{name}[/bold][/green]")

        elif action == "details":
            _show_terminal_detail(name, cmd, sess, created, alive)
            input("\n[dim]Press Enter to continue...[/dim]")

        # Rebuild items after mutations
        items.clear()
        if primary_session is not None and primary_session.is_alive():
            items.append(("term0 (primary)", primary_session.command, primary_session,
                          0.0, True))
        for term in get_all_terminals():
            items.append((term.name, term.command, term.session,
                          term.created_at, term.session is not None and term.session.is_alive()))
        if not items:
            console.print("\n[dim]No more terminals.[/dim]")
            break
        selected[0] = min(selected[0], len(items) - 1)


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


def handle_meta_command(cmd: str, agent_registry: AgentRegistry, session: dict, interactive_session=None) -> bool:
    """Handle meta commands. Returns True if should exit."""
    parts = cmd.strip().split()
    action = parts[0].lower()

    if action == "/":
        selected = show_command_palette()
        if selected:
            return handle_meta_command(selected, agent_registry, session, interactive_session)
        return False

    if action == "/exit":
        stop_trigger_scanner()
        close_all_terminals()
        agent_registry.unregister()
        clear_session()
        console.print("[green]Logged out. Goodbye![/green]")
        return True

    if action in ("/quit", "/q"):
        if _IN_SUB_TERMINAL:
            # Running inside a sub-terminal — detach like /back instead of quitting
            sys.stdout.write("\x1b]777;LAINTAS_DETACH\x07")
            sys.stdout.flush()
            console.print("[green]Detaching...[/green]")
            return False
        stop_trigger_scanner()
        close_all_terminals()
        agent_registry.unregister()
        console.print("[green]Goodbye![/green]")
        return True

    elif action == "/back":
        # Signal parent enter_session to detach without closing this terminal
        sys.stdout.write("\x1b]777;LAINTAS_DETACH\x07")
        sys.stdout.flush()
        console.print("[green]Detaching...[/green]")
        return False

    elif action == "/help":
        show_help()

    elif action == "/login":
        console.print()
        console.print(Panel(
            "[bold]Login to Laintas[/bold]\n\n"
            "[1] [bold]Remote login[/bold] — opens browser to laintas.com\n"
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
            agent_registry.register(session)
            console.print(f"[green]Logged in as {new_session.get('userEmail') or new_session.get('userName') or new_session['userId']}[/green]")

    elif action == "/model":
        if len(parts) >= 2 and parts[1].lower() in ("reset", "clear", "default"):
            set_selected_model("")
            set_selected_provider("")
            console.print("[green]Model reset. Backend default will be used.[/green]")
        elif len(parts) >= 2:
            model = " ".join(parts[1:]).strip()
            set_selected_model(model)
            console.print(f"[green]Model set to: [bold]{model}[/bold][/green]")
        else:
            current = get_selected_model()
            current_provider = get_selected_provider()
            try:
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
                            m.get("description", ""),
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
                                console.print(f"[green]Model set to: [bold]{chosen['id']}[/bold][/green]")
                            else:
                                console.print(f"[red]Invalid model selection: {choice}[/red]")
                console.print("Set directly with [bold]/model <model-id>[/bold], reset with [bold]/model reset[/bold].")

    elif action == "/name":
        # ── /name term<N> <new-name> — rename a terminal ──
        if len(parts) >= 3 and parts[1].startswith("term"):
            old_name = parts[1]
            new_name = parts[2]
            if rename_terminal(old_name, new_name):
                console.print(f"[green]Terminal renamed: [bold]{old_name}[/bold] → [bold]{new_name}[/bold][/green]")
            else:
                console.print(f"[red]Terminal '{old_name}' not found.[/red]")
        # ── /name <name> — set agent name ──
        elif len(parts) > 1:
            name = " ".join(parts[1:])
            config = load_config()
            config["agentName"] = name
            save_config(config)
            console.print(f"[green]Agent name set to: {name}[/green]")
            # Re-register with new name
            agent_registry.unregister()
            agent_registry.register(session, name=name)
            agent_registry.start_heartbeat()
        else:
            config = load_config()
            current = config.get("agentName", socket.gethostname())
            console.print(f"Current agent name: [bold]{current}[/bold]")
            console.print("Usage: /name <new-name>")
            console.print("       /agents name <new-name>  (rename current agent)")

    elif action == "/memory":
        raw = read_file(str(paths.project_file(paths.CWD_MEMORY)))
        if raw and raw.strip():
            try:
                entries = json.loads(raw)
                if isinstance(entries, list) and entries:
                    lines = [f"[bold]{e['id']}.[/bold] {e['content']}" for e in entries]
                    text = "\n".join(lines)
                    console.print(Panel(text, title=f".laintas/memory.json ({len(entries)} entries)"))
                else:
                    console.print(Panel(raw.strip(), title=".laintas/memory.json"))
            except json.JSONDecodeError:
                console.print(Panel(raw.strip(), title=".laintas/memory.json"))
        else:
            console.print("[dim]No memory yet. The AI will record learnings here.[/dim]")

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

    elif action == "/clear":
        console.clear()

    elif action == "/plan":
        import plan_mode as _pm
        if len(parts) >= 3 and parts[1] == "enter":
            task = " ".join(parts[2:])
            plan = _pm.enter_plan_mode(task)
            console.print(Panel(
                f"[bold]Plan Mode: [green]ENTERED[/green][/bold]\n\n"
                f"Task: {task}\n"
                f"Plan file: {plan['file']}\n\n"
                f"[dim]The AI will now explore and design — no code will be executed.[/dim]\n"
                f"[dim]When the plan is ready, run [bold]/plan approve[/bold].[/dim]",
                title="Plan Mode",
                border_style="green",
            ))
        elif len(parts) >= 2 and parts[1] == "approve":
            plan = _pm.exit_plan_mode(approve=True)
            if plan:
                console.print(Panel(
                    f"[bold]Plan [green]APPROVED[/green][/bold]\n\n"
                    f"File: {plan['file']}\n\n"
                    f"[dim]AI will now execute the plan. Run /plan exit to leave without executing.[/dim]",
                    title="Plan Approved",
                    border_style="green",
                ))
            else:
                console.print("[yellow]No active plan to approve.[/yellow]")
        elif len(parts) >= 2 and parts[1] == "exit":
            plan = _pm.exit_plan_mode(approve=False)
            console.print(f"[dim]Exited plan mode (plan saved).[/dim]")
        elif len(parts) >= 2 and parts[1] == "status":
            plan = _pm.get_current_plan()
            if plan:
                content = _pm.read_plan() or "(empty)"
                console.print(Panel(content[:2000], title=f"Plan: {plan['task'][:60]}"))
            else:
                console.print("[dim]Not in plan mode.[/dim]")
        elif len(parts) >= 2 and parts[1] == "list":
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
                          "  [bold]/plan approve[/bold]      — Approve and execute\n"
                          "  [bold]/plan exit[/bold]         — Exit without approving\n"
                          "  [bold]/plan status[/bold]       — Show current plan\n"
                          "  [bold]/plan list[/bold]         — List saved plans")

    elif action == "/workflow":
        import workflow_engine as _we
        sub = parts[1] if len(parts) > 1 else "status"
        if sub == "start":
            if len(parts) < 4:
                console.print("[yellow]Usage: /workflow start <name> \"<description>\"[/yellow]")
                templates = _we.list_workflow_templates()
                console.print(f"[dim]Available: {', '.join(templates)}[/dim]")
            else:
                wf_name = parts[2]
                wf_desc = " ".join(parts[3:])
                wf = _we.start_workflow(wf_name, wf_desc)
                if wf is None:
                    console.print(f"[red]Unknown workflow: {wf_name}[/red]")
                    console.print(f"[dim]Available: {', '.join(_we.list_workflow_templates())}[/dim]")
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
                console.print(Panel(
                    f"[bold]{wf.name}[/bold] — {wf.description}\n\n"
                    f"Progress: {wf.progress_str}{phase_info}",
                    title="Active Workflow",
                    border_style="cyan",
                ))
        elif sub == "advance":
            summary = " ".join(parts[2:]) if len(parts) > 2 else ""
            new_phase = _we.advance_phase(summary)
            wf = _we.get_active_workflow()
            if new_phase is None:
                if wf and wf.completed:
                    console.print(f"[green]Workflow '{wf.name}' completed![/green]")
                else:
                    console.print("[yellow]No active workflow or already completed.[/yellow]")
            else:
                console.print(f"[green]Advanced to phase: [bold]{new_phase.name}[/bold] — {new_phase.description}[/green]")
        elif sub == "end":
            summary = " ".join(parts[2:]) if len(parts) > 2 else ""
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
                    filename = parts[2]
                    entries = get_debug_logs()[:n]
                    if not entries:
                        console.print("[yellow]No debug entries to save.[/yellow]")
                    else:
                        filepath = Path(filename)
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
                            if e.memory:
                                lines.append(f"\n[Memory]\n{e.memory[:500]}")
                            lines.append(f"\n[Done] {e.done}")
                            if e.error:
                                lines.append("\n[Error] true")
                            if e.billing:
                                cost = e.billing.get("costCents", 0)
                                balance = e.billing.get("balanceCents", 0)
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
                        filepath.write_text('\n'.join(lines), encoding='utf-8')
                        console.print(f"[green]Saved {n} debug entries to {filepath.absolute()}[/green]")
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
        if sub.is_alive():
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
        if len(parts) < 3:
            console.print("[yellow]Usage: /send <name> <command>[/yellow]")
        else:
            name = parts[1]
            cmd = " ".join(parts[2:])
            term = get_terminal(name)
            if term is None:
                console.print(f"[red]Terminal '{name}' not found.[/red]")
            elif term.session is None or not term.session.is_alive():
                console.print(f"[yellow]Terminal '{name}' has no active session.[/yellow]")
            else:
                term.session.send_keys(cmd + "\n")
                time.sleep(0.3)
                term.session.read_output(timeout=0.5)
                output = term.session.full_output
                console.print(f"[dim]Sent to [bold]{name}[/bold]: {cmd[:80]}[/dim]")
                if output.strip():
                    console.print(Panel(output[-2000:], title=f"{name} output"))

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
            new_name = " ".join(parts[2:])
            current = get_current_agent()
            if current and rename_agent(current.id, new_name):
                console.print(f"[green]Agent renamed to [bold]{new_name}[/bold][/green]")
            else:
                console.print("[red]No current agent to rename.[/red]")

    elif action == "/spawn":
        if len(parts) < 2:
            console.print("[yellow]Usage: /spawn [name:] <task...>[/yellow]")
        else:
            # Parse optional "name:" prefix
            rest = " ".join(parts[1:])
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
        if len(parts) < 3:
            console.print("[yellow]Usage: /tell <agent_id> <message...>[/yellow]")
        else:
            target_id = parts[1]
            raw = " ".join(parts[2:])
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    body = {"kind": "msg", "text": raw}
            except (ValueError, TypeError):
                body = {"kind": "msg", "text": raw}
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
        if len(parts) < 2:
            console.print("[yellow]Usage: /tool <name> [json_params][/yellow]")
        else:
            tool_name = parts[1]
            raw = " ".join(parts[2:]) if len(parts) > 2 else ""
            try:
                params = json.loads(raw) if raw else {}
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
        sub = parts[1] if len(parts) > 1 else "list"
        if sub == "list":
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
                    console.print("[dim]Edit the file, then run /skill reload[/dim]")
        elif sub == "dir":
            console.print(str(skills_mod.SKILLS_DIR))
        else:
            console.print("[yellow]Usage: /skill {list|reload|new <name>|dir}[/yellow]")

    elif action == "/mcp":
        if not _get_mcp_mod().MCP_AVAILABLE:
            console.print(f"[yellow]mcp SDK not installed: {_get_mcp_mod().MCP_IMPORT_ERROR}[/yellow]")
            console.print("[dim]Install with:  pip install mcp[/dim]")
            return False
        sub = parts[1] if len(parts) > 1 else "list"
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
            console.print("[yellow]Usage: /mcp {list|connect <n>|disconnect <n>|reload|tools <n>|init|config}[/yellow]")

    elif action in ("/t", "/term"):
        if len(parts) >= 2:
            # /t <name> or /term <name> — create sub-terminal (no agent stationed)
            name = parts[1]
            existing = get_terminal(name)
            if existing and existing.session and not existing.session.is_alive():
                unregister_terminal(name)
                existing = None
            if existing is not None:
                console.print(f"[yellow]Terminal '{name}' already exists. /t to view, /terminate {name} to remove.[/yellow]")
            else:
                lain_cmd = f"{sys.executable} {os.path.abspath(__file__)} --depth 1"
                sub = SubTerminalSession(lain_cmd)
                sub.start()
                time.sleep(0.1)
                if sub.is_alive():
                    sub.read_output(timeout=0.1)
                register_terminal(sub, "laintas-cli", 0, name=name)
                console.print(f"[green]Created sub-terminal [bold]{name}[/bold] (no agent stationed)[/green]")
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
        reload_default_files()

    elif action == "/config":
        # Built-in config command (doesn't require .laintas/commands.py)
        parts_lower = [p.lower() for p in parts]
        if len(parts) == 1:
            # /config — show all
            for key, val in sorted(get_runtime_config().items()):
                console.print(f"  [cyan]{key}[/cyan] = {val}")
        elif len(parts) == 2 and parts[1] == "reset":
            reset_runtime_config()
            console.print("[green]Runtime config reset to defaults.[/green]")
        elif len(parts) == 2:
            # /config <key> — show one
            key = parts[1]
            val = get_runtime_config(key)
            if val is None:
                console.print(f"[red]Unknown config key: {key}[/red]")
            else:
                console.print(f"  [cyan]{key}[/cyan] = {val}")
        elif len(parts) == 3:
            # /config <key> <value>
            key = parts[1]
            try:
                set_runtime_config(key, parts[2])
                console.print(f"[green]{key} = {get_runtime_config(key)}[/green]")
            except (ValueError, KeyError) as e:
                console.print(f"[red]{e}[/red]")
        else:
            console.print("[yellow]Usage: /config [key [value]] | /config reset[/yellow]")

    elif action == "/continue":
        # Resume agent loop after max_loops exhaustion.
        # Mirrors Claude Code's /continue: resets the turn counter and
        # re-invokes the agent loop with preserved state.
        _prev_state = getattr(handle_meta_command, '_last_agent_state', None)
        _prev_chat = getattr(handle_meta_command, '_last_chat_history', None)
        _prev_input = getattr(handle_meta_command, '_last_original_input', None)
        _prev_deps = getattr(handle_meta_command, '_last_deps', None)
        _prev_session = getattr(handle_meta_command, '_last_session', None)
        _prev_events_cb = getattr(handle_meta_command, '_last_events_cb', None)
        _prev_existing_session = getattr(handle_meta_command, '_last_existing_session', None)

        if _prev_state is None or _prev_input is None:
            console.print("[yellow]No previous agent loop to continue. "
                         "Run a task first, then use /continue if it hits the turn limit.[/yellow]")
            return False

        if not _prev_state.get("_max_loops_exhausted"):
            console.print("[yellow]The previous agent loop did not hit the turn limit. "
                         "There is nothing to continue.[/yellow]")
            return False

        # Reset exhaustion flag and counter
        _prev_state.pop("_max_loops_exhausted", None)
        _prev_state.pop("_exhaustion_loop_count", None)

        console.print("[green]Resuming agent loop with fresh turn counter...[/green]")

        response = _run_agent_loop_with_interrupt(
            _prev_deps, _prev_input, _prev_session, _prev_state,
            _prev_chat or [],
            events_cb=_prev_events_cb,
            existing_session=_prev_existing_session,
        )

        # Update stored state for potential further /continue
        handle_meta_command._last_agent_state = response.get("state", _prev_state)
        handle_meta_command._last_chat_history = _prev_chat

        if response.get("msg"):
            console.print(deps.Markdown(response["msg"]) if hasattr(deps, 'Markdown') else response["msg"])
            (_prev_chat if _prev_chat is not None else []).append(
                {"role": "assistant", "content": response["msg"]}
            )

        return False

    elif action == "/hwo":
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub in ("run", "compile") and len(parts) >= 3:
            # /hwo run <path.hwo>  or  /hwo compile <path.hwo>
            import hwo_runner
            path = " ".join(parts[2:])
            if sub == "compile":
                r = hwo_runner.compile_hwo_file(path)
            else:
                current = get_current_agent()
                r = hwo_runner.run_hwo_file(
                    path=path,
                    deps=get_loop_deps(),
                    session=session,
                    parent_id=current.id if current else None,
                )
            style = "[green]" if r.get("ok") else "[red]"
            console.print(f"{style}{r.get('msg', '')}[/]")
        else:
            # Visual agent-orchestration builder TUI
            current = get_current_agent()
            root_name = current.name if current else "primary"
            hwo_ui_mod.run_hwo_ui(root_name)

    else:
        # Try .laintas/commands.py custom handler first
        handler = _load_extra_commands()
        if handler:
            ctx = {
                "session": session, "interactive_session": interactive_session,
                "agent_registry": agent_registry, "console": console,
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
# (name, description) tuples for the interactive / command selector.
# Keep this list in sync when adding new slash commands to handle_meta_command.

_COMMANDS = [
    ("/help",      "Show this help"),
    ("/login",     "Re-authenticate with laintas.com (opens browser)"),
    ("/model",     "List or set the backend AI model"),
    ("/name",      "Set current agent name"),
    ("/memory",    "View .laintas/memory.json"),
    ("/prop",      "View .laintas/cli.prop prompt template"),
    ("/scan",      "Scan and list all available system commands from PATH"),
    ("/debug",     "Browse debug entries (/debug), view detail (/debug <N>)"),
    ("/cwd",       "Show current working directory"),
    ("/clear",     "Clear screen"),
    ("/exit",      "Log out and exit (clears cached session)"),
    ("/quit",      "Exit without logging out (keeps cached session)"),
    ("/q",         "Detach from sub-terminal (alias for /back in sub-term, /quit in REPL)"),
    ("/back",      "Detach from sub-terminal without closing it"),
    ("/station",   "Station agent in a persistent shell (current or named terminal)"),
    ("/terminate", "Close and destroy a terminal"),
    ("/send",      "Send a command to a named terminal"),
    ("/hire",      "Create a new AI agent (AI-1, AI-2...)"),
    ("/agents",    "List/switch agents, /agents name <n> to rename"),
    ("/t",         "List sub-terminals (/t), or create new one (/t <name>)"),
    ("/term",      "Same as /t <name> — create a laintas-cli sub-terminal"),
    ("/spawn",     "Spawn a sub-agent with optional name"),
    ("/tell",      "Send a message to another agent"),
    ("/abort",     "Signal abort to an agent"),
    ("/tools",     "List registered tools by source"),
    ("/tool",      "Invoke a tool directly"),
    ("/skill",     "Manage skills"),
    ("/mcp",       "Manage MCP servers"),
    ("/config",    "View or set runtime configuration"),
    ("/reload",    "Reload default files and restart"),
    ("/continue",  "Resume agent loop after max_loops exhaustion"),
]


def _fuzzy_match(text: str, pattern: str) -> bool:
    """Return True if all chars of pattern appear in text in order (fuzzy match)."""
    it = iter(text)
    return all(c in it for c in pattern)


def show_command_palette():
    """Interactive full-screen command selector — fuzzy filter, arrow keys, Enter.

    Returns the selected command string (e.g. \"/help\") or None if cancelled.
    """
    filter_buffer = Buffer()
    selected = [0]

    def _get_filtered():
        filt = filter_buffer.text.strip().lower()
        if not filt:
            return _COMMANDS[:]
        return [(n, d) for n, d in _COMMANDS if _fuzzy_match(n.lower(), filt)]

    def _build_lines():
        lines = []
        lines.append(("bold cyan", "Commands — type to filter\n"))
        filt = filter_buffer.text.strip()
        if filt:
            lines.append(("", f"  filter: [white bold]{filt}[/white bold]\n"))
        else:
            lines.append(("dim", "  (start typing to filter)\n"))
        lines.append(("dim", "─" * 50 + "\n"))

        filtered = _get_filtered()
        if not filtered:
            lines.append(("", "\n"))
            lines.append(("dim", "  No matching commands.\n"))
            lines.append(("", "\n"))
            return lines

        # Clamp selection
        if selected[0] >= len(filtered):
            selected[0] = max(0, len(filtered) - 1)

        # Compute visible range
        import shutil
        term_h = shutil.get_terminal_size().lines
        list_h = max(4, term_h - 5)
        start = 0
        if len(filtered) > list_h:
            start = min(selected[0], len(filtered) - list_h)
            start = max(0, start)
        end = min(start + list_h, len(filtered))

        if start > 0:
            lines.append(("dim", f"  ... {start} more above ...\n"))

        for i in range(start, end):
            cmd, desc = filtered[i]
            prefix = "▶" if i == selected[0] else " "
            style = "class:selected" if i == selected[0] else ""
            lines.append((style, f" {prefix} [cyan]{cmd:14}[/cyan] {desc}\n"))

        if end < len(filtered):
            lines.append(("dim", f"  ... {len(filtered) - end} more below ...\n"))

        lines.append(("", "\n"))
        lines.append(("dim", f" {len(filtered)} commands  ↑↓ navigate  ↵ select  Esc/q cancel"))
        return lines

    # ── Key bindings ──────────────────────────────────────────────
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        filtered = _get_filtered()
        if filtered:
            selected[0] = max(0, selected[0] - 1)

    @kb.add("down")
    def _(event):
        filtered = _get_filtered()
        if filtered:
            selected[0] = min(len(filtered) - 1, selected[0] + 1)

    @kb.add("enter")
    def _(event):
        filtered = _get_filtered()
        if filtered and 0 <= selected[0] < len(filtered):
            event.app.exit(result=filtered[selected[0]][0])

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    # ── Layout ────────────────────────────────────────────────────
    search_control = BufferControl(buffer=filter_buffer)
    search_window = Window(content=search_control, height=1)

    list_control = FormattedTextControl(_build_lines)
    list_window = Window(content=list_control)

    layout = Layout(HSplit([search_window, list_window]))

    # ── Style ─────────────────────────────────────────────────────
    style = Style.from_dict({
        "selected": "reverse",
    })

    # ── Run ───────────────────────────────────────────────────────
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=True,
        refresh_interval=0.05,
    )
    return app.run()


def show_help():
    """Display help."""
    table = Table(title="laintas_cli Commands")
    table.add_column("Command", style="cyan")
    table.add_column("Description")

    table.add_row("ls, cat, mkdir, git, ...", "Commands found on PATH → executed directly")
    table.add_row("<natural language>", "Not a recognized command → AI agent loop")
    table.add_row("/help", "Show this help")
    table.add_row("/login", "Re-authenticate with laintas.com (opens browser)")
    table.add_row("/model [id|reset]", "List available backend models, set model, or reset to backend default")
    table.add_row("/name [name]", "Set current agent name")
    table.add_row("/memory", "View .laintas/memory.json")
    table.add_row("/prop", "View .laintas/cli.prop prompt template")
    table.add_row("/scan", "Scan and list all available system commands from PATH")
    table.add_row("/debug", "Browse debug entries (/debug), view detail (/debug <N>), save to file (/debug <N> <file>), clear (/debug clear)")
    table.add_row("/cwd", "Show current working directory")
    table.add_row("/station [name]", "Station agent in a persistent shell (current terminal, or named)")
    table.add_row("/terminate <name>", "Close and destroy a terminal")
    table.add_row("/send <name> <cmd>", "Send a command to a named terminal")
    table.add_row("/hire", "Create a new AI agent (AI-1, AI-2...)")
    table.add_row("/agents [name]", "List/switch agents, /agents name <n> to rename")
    table.add_row("/t, /term [name]", "List sub-terminals, or create new one (/t <name>)")
    table.add_row("/back", "Detach from sub-terminal without closing it")
    table.add_row("/hwo", "Visual agent-orchestration builder (HWO TUI)")
    table.add_row("/plan", "Structured planning (/plan enter, approve, exit, status, list)")
    table.add_row("/workflow", "Multi-phase workflows (/workflow start, status, advance, end, list)")
    table.add_row("/skill", "Skill management (/skill list, reload, new <name>, dir)")
    table.add_row("/config", "Runtime config (/config, /config <key> <value>, /config reset)")
    table.add_row("/tools", "List registered AI tools")
    table.add_row("/clear", "Clear screen")
    table.add_row("/exit", "Log out and exit (clears cached session)")
    table.add_row("/quit, /q", "Detach from sub-terminal (/q) or exit without logging out (/quit)")

    console.print(table)


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
            console=console,
            Markdown=Markdown,
            pty_passthrough=pty_passthrough,
            build_subterminal_cmd=_build_subterminal_cmd,
        )
    return _loop_deps


# ── Main ───────────────────────────────────────────────────────────────

def show_banner(agent_name: str, session: dict = None):
    """Display startup banner."""
    shell_info = "cmd.exe" if IS_WINDOWS else SHELL_NAME
    # Build account line
    account_line = ""
    if session:
        name = session.get("userName", "")
        email = session.get("userEmail", "")
        uid = session.get("userId", "")
        if email:
            account_line = f"Account: {email}\n"
        elif name:
            account_line = f"Account: {name} ({uid})\n"
        elif uid:
            account_line = f"Account: {uid}\n"
    console.print(Panel(
        f"[bold]Laintas CLI[/bold] — {agent_name}\n"
        f"{account_line}"
        f"OS: {SYSTEM} | Shell: {shell_info}\n"
        f"Working: {os.getcwd()}\n"
        f"Backend: {os.environ.get('LAINTAS_BACKEND', BACKEND_URL)}\n\n"
        f"Commands from PATH → executed directly.\n"
        "Natural language → AI agent loop.\n"
        "Type [bold]/help[/bold] for commands.",
        title="laintas_cli",
        border_style="blue",
    ))


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


def _run_agent_loop_with_interrupt(deps, user_input, session, agent_state,
                                   chat_history, events_cb=None,
                                   existing_session=None):
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
        )
    finally:
        # Restore original SIGINT handler
        signal.signal(signal.SIGINT, _old_sigint)
        _interrupt_event.clear()
        _stop_bg_input_reader()

    return response


def run_execute_mode(task: str, session: dict, depth: int) -> int:
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

    response = run_agent_loop(
        get_loop_deps(),
        original_input=task,
        session=session,
        state=agent_state,
        chat_history=[],
        events_cb=None,
        existing_session=None,
        depth=depth,
    )

    result = response.get("msg", "")
    last_output = response.get("state", {}).get("lastOutput", "")
    if last_output:
        result += "\n" + last_output
    print(result)

    return 0 if response.get("success") else 1


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Laintas CLI — Autonomous AI agent")
    parser.add_argument("--name", type=str, help="Set agent name (shows in Helpwo AGNETS)")
    parser.add_argument("--backend", type=str, help="Backend URL", default=None)
    parser.add_argument("--laintas", type=str, help="Laintas.com base URL", default=None)
    parser.add_argument("--execute", "-e", type=str, default=None,
                        help="Execute a single task non-interactively and exit")
    parser.add_argument("--depth", "-d", type=int, default=0,
                        help="Nesting depth (0=user terminal, 1+=sub-agent)")
    parser.add_argument("--simple-prompt", action="store_true", default=False,
                        help="Use plain input() instead of prompt_toolkit")
    parser.add_argument("--monitor-only", action="store_true", default=False,
                        help="Start as a remote executor only — register, heartbeat, "
                             "and poll HelpwoAI for tasks. No local REPL.")
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
    args = parser.parse_args()

    # Apply environment overrides
    if args.backend:
        os.environ["LAINTAS_BACKEND"] = args.backend
    if args.laintas:
        os.environ["LAINTAS_BASE"] = args.laintas

    # All REPL instances use full-color console — sub-terminals are full
    # laintas-cli instances and should look identical to the main terminal.

    # Initialize unified home directory and auto-migrate old layout
    paths.ensure_home()
    if migrate_mod.needs_migration():
        console.print("[dim]Migrating to new directory layout (~/.laintas/)...[/dim]")
        migrate_mod.migrate_all(verbose=True)

    # Ensure .laintas/ project files exist in cwd
    ensure_files_exist()

    # Load or create config
    config = load_config()
    agent_name = args.name or config.get("agentName", socket.gethostname())

    # Authenticate
    session = ensure_auth() or {}

    # ── Non-interactive execution mode ──
    if args.execute:
        if not session.get("userId"):
            console.print("[red]Authentication required for --execute mode. Use /login first.[/red]")
            sys.exit(1)
        sys.exit(run_execute_mode(args.execute, session, args.depth))

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
    # Load last session snapshot for this directory (depth-0 only)
    if args.depth == 0:
        _snapshot = load_session_snapshot(_session_start_cwd)
        if _snapshot:
            agent_state["_last_session_snapshot"] = _snapshot
    chat_history = []

    if session.get("userId"):
        agent_registry.register(session, name=agent_name, quiet=(args.depth > 0))
        agent_registry.start_heartbeat()
        # Start listening for remote messages from Helpwo (skip in child terminals)
        if args.depth == 0:
            agent_registry.start_message_poll(
                lambda: agent_state,
                lambda: chat_history,
            )

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
        console.print("\n[yellow]Shutting down...[/yellow]")
        if args.depth == 0:
            save_session_snapshot(agent_state, chat_history, _session_start_cwd)
        stop_trigger_scanner()
        close_all_terminals()
        close_all_agents()
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
        if not session.get("userId"):
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
            stop_trigger_scanner()
            close_all_terminals()
            if interactive_session:
                interactive_session.close()
            agent_registry.unregister()
            clear_session()
            console.print("[green]Logged out. Goodbye![/green]")
            if injected_done is not None:
                injected_done.set()
            return

        # Check for meta commands
        if user_input.startswith("/"):
            should_exit = handle_meta_command(user_input, agent_registry, session, interactive_session)
            if should_exit:
                if interactive_session:
                    interactive_session.close()
                if injected_done is not None:
                    injected_done.set()
                return
            if injected_done is not None:
                injected_done.set()
            continue

        # ── Session-aware routing ──────────────────────────────────
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
                if session.get("userId"):
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
                if session.get("userId"):
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
            if injected_done is not None:
                injected_done.set()
            continue

        # ── Normal input routing ───────────────────────────────────

        # Add to chat history
        chat_history.append({"role": "user", "content": user_input})

        # Push user input event to remote stream
        if agent_registry.agent_id:
            agent_registry._push_events([{"type": "user", "content": user_input}])

        # Route first word against PATH/builtins → system command or AI
        # All REPL instances (depth 0 and depth > 0) execute system commands
        # directly. Natural language goes to AI.
        if is_system_command(user_input):
            console.print(f"\n[dim yellow]$ {user_input}[/dim yellow]")
            if agent_registry.agent_id:
                agent_registry._push_events([{"type": "system", "kind": "command", "content": user_input}])

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
                # Local user: full PTY passthrough (interactive programs get real tty).
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
            if not session.get("userId"):
                console.print("[yellow]Not authenticated. Use /login first.[/yellow]")
                if injected_done is not None:
                    injected_done.set()
                continue
            console.print("[dim]Not a system command, asking AI...[/dim]")

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

        # Save AI reply to chat history
        if response.get("msg"):
            chat_history.append({"role": "assistant", "content": response["msg"]})

        # ── Cross-interaction state preservation ──
        # Mirrors Claude Code's approach: preserve recent context across REPL
        # interactions so the model doesn't lose track of what it was doing.
        agent_state = prepare_state_for_repl(response.get("state", {}))

        if injected_done is not None:
            injected_done.set()


if __name__ == "__main__":
    main()
