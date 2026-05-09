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
import shlex
import signal
import socket
import tempfile
import platform
import webbrowser
import threading
import subprocess
from pathlib import Path
from typing import Optional
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
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

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
    switch_to_agent, set_current_agent_id,
    rename_agent, station_agent, unstation_agent,
    close_all_agents,
    get_runtime_config, set_runtime_config,
    list_runtime_config, reset_runtime_config,
    clear_loop_command_cache,
)

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
        r = requests.get(f"{local}/api/agents", timeout=2)
        if r.status_code in (200, 401):  # 401 = endpoint exists, just needs auth
            return local
    except requests.RequestException:
        pass
    return "https://helpwo.laintas.com"

BACKEND_URL = os.environ.get("LAINTAS_BACKEND") or _detect_backend()
LAINTAS_BASE = os.environ.get("LAINTAS_BASE", "https://laintas.com")
SESSION_FILE = Path.home() / ".laintas_cli_session.json"
CONFIG_FILE = Path.home() / ".laintas_cli_config.json"
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

    The child process takes over the terminal — stdin is forwarded to the
    child and child output goes directly to stdout, exactly like running
    the command in a normal shell. Returns when the child exits.

    Returns {stdout, stderr, returncode, success}.
    On Windows falls back to subprocess.run.
    """
    if IS_WINDOWS:
        return _execute_windows(command, timeout)

    import tty

    session = InteractiveSession(command, timeout=timeout, stream_output=True)
    session.start()

    fd = sys.stdin.fileno()
    old_tcattr = termios.tcgetattr(fd)
    old_sigint = signal.getsignal(signal.SIGINT)

    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        tty.setraw(fd)

        while session.is_alive():
            try:
                r, _, _ = select.select([fd, session.master_fd], [], [], 0.1)
            except (select.error, ValueError):
                break

            if fd in r:
                try:
                    data = os.read(fd, 4096)
                    if data:
                        os.write(session.master_fd, data)
                    else:
                        break
                except OSError:
                    break

            if session.master_fd in r:
                try:
                    data = os.read(session.master_fd, 4096)
                    if data:
                        os.write(sys.stdout.fileno(), data)
                        sys.stdout.flush()
                except OSError:
                    break
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_tcattr)
        except (termios.error, OSError):
            pass
        session.close()

    return {
        "stdout": session.full_output,
        "stderr": "",
        "returncode": session.returncode,
        "success": session.returncode == 0,
    }


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

    def __init__(self, command: str, timeout: int = 120, stream_output: bool = False):
        self.command = command
        self.timeout = timeout
        self.stream_output = stream_output

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
    """Completer that handles /-commands and falls back to path completion."""
    META_COMMANDS = [
        "/help", "/login", "/name", "/memory", "/prop",
        "/scan", "/debug", "/cwd",
        "/station", "/terminate", "/send", "/hire", "/agents",
        "/t", "/term",
        "/clear", "/exit", "/quit",
    ]

    def __init__(self):
        self._path = PathCompleter(expanduser=True)

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        # /-command completion
        if text.startswith("/"):
            for cmd in self.META_COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))
            return
        # Path completion for arguments
        yield from self._path.get_completions(document, complete_event)


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

    return kb


_prompt_session: Optional[PromptSession] = None


def get_prompt_session() -> PromptSession:
    """Get or create the persistent prompt_toolkit session."""
    global _prompt_session
    if _prompt_session is None:
        hist_file = Path.home() / ".laintas_cli_history"
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
# No hardcoded command lists. On startup we scan $PATH (Linux/Mac) or
# %PATH%+%PATHEXT% (Windows) and write every executable name to .cli .
# The AI prompt (.cli.prop) gets a filtered subset — system daemons and
# single-char names are hidden so the AI focuses on user-facing commands.

import re

CLI_FILE = ".cli"


def scan_path_commands() -> set:
    """Scan all directories in $PATH for available executables."""
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

    if IS_WINDOWS:
        commands.update(_WINDOWS_CMD_BUILTINS)

    return commands


def write_cli_file(commands: set) -> None:
    """Write discovered commands to .cli (one per line, sorted)."""
    Path(CLI_FILE).write_text("\n".join(sorted(commands)) + "\n", encoding="utf-8")


def load_cli_commands() -> set:
    """Load command set from .cli file. Returns empty set if file missing."""
    p = Path(CLI_FILE)
    if not p.exists():
        return set()
    text = p.read_text(encoding="utf-8", errors="replace")
    return {line.strip() for line in text.splitlines() if line.strip()}


# Cached command set for dispatch
_CLI_ALL: set = set()       # every command on PATH


def refresh_commands() -> None:
    """Re-scan PATH, rewrite .cli, refresh cache."""
    global _CLI_ALL
    _CLI_ALL = scan_path_commands()
    write_cli_file(_CLI_ALL)


def get_dispatch_commands() -> set:
    """Return the full command set used for dispatch matching."""
    global _CLI_ALL
    if not _CLI_ALL:
        _CLI_ALL = load_cli_commands()
        if not _CLI_ALL:
            _CLI_ALL = scan_path_commands()
            write_cli_file(_CLI_ALL)
    if IS_WINDOWS:
        _CLI_ALL.update(_WINDOWS_CMD_BUILTINS)
    return _CLI_ALL


def _filter_user_commands(all_cmds: set) -> list:
    """Filter out obscure system commands for the AI prompt.

    Keeps commands that look user-facing: 2+ chars, lowercase start,
    no weird characters, not a known system daemon prefix.
    """
    result = []
    for c in sorted(all_cmds):
        if len(c) < 2:
            continue
        if c[0].isupper():
            continue
        if "." in c or ":" in c or c.startswith("_"):
            continue
        # systemd / dbus / kernel internal
        if c.startswith("systemd-") or c.startswith("dbus-") or c.startswith("ksvgtop"):
            continue
        result.append(c)
    return result


def extract_first_word(user_input: str) -> str:
    """Extract the first shell word from user input. Returns lowercase."""
    m = re.match(r'^\s*(\S+)', user_input)
    if not m:
        return ""
    w = m.group(1)
    # strip common wrappers: quotes, semicolons
    w = w.strip("'\"`;&|")
    return w


def is_system_command(user_input: str) -> bool:
    """Check whether the first word of input matches a command on PATH."""
    first = extract_first_word(user_input)
    if not first:
        return False
    dispatch = get_dispatch_commands()
    return first in dispatch


# ── Shell Detection ──────────────────────────────────────────────────────

if IS_WINDOWS:
    DEFAULT_SHELL = os.environ.get("COMSPEC", "cmd.exe")
    SHELL_NAME = "cmd"
else:
    DEFAULT_SHELL = os.environ.get("SHELL", "/bin/bash")
    SHELL_NAME = "bash" if "bash" in DEFAULT_SHELL else ("zsh" if "zsh" in DEFAULT_SHELL else "sh")

# ── Session Management ─────────────────────────────────────────────────

def load_session() -> Optional[dict]:
    """Load saved session token from ~/.laintas_cli_session.json."""
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_session(session: dict) -> None:
    """Save session token to ~/.laintas_cli_session.json."""
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


# ── Authentication ──────────────────────────────────────────────────────

def verify_session(session: dict) -> Optional[dict]:
    """Verify a saved session token with laintas.com. Returns {id, name, email} or None."""
    cookies = session.get("cookies", {})
    headers = session.get("headers", {})
    token = session.get("token", "")
    user_id = session.get("userId", "")

    # Method 1: X-User-Id header (local dev / trusted proxy bypass)
    if user_id:
        try:
            resp = requests.get(f"{LAINTAS_BASE}/api/balance",
                                headers={"X-User-Id": user_id}, timeout=8)
            if resp.status_code == 200:
                return {"id": user_id, "name": "", "email": ""}
        except requests.RequestException:
            pass

    # Method 2 & 3: call get-session to get full user info
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
                                timeout=8, **req_args)
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


# ── CLI Prompt Template (.cli.prop) ────────────────────────────────────

EXTRA_COMMAND_TEMPLATE = '''# .extra_command.py — define custom slash commands for the REPL
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
    \"\"\"Handle custom loop commands defined in .loop_command.py.\"\"\"

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
    """Generate the .cli.prop system prompt template for the current OS."""
    shell_info = "cmd.exe" if IS_WINDOWS else f"{SHELL_NAME}"

    return f"""You are '{{{{agentName}}}}', an autonomous AI agent running in a laintas-cli REPL on {SYSTEM}.
Always respond with valid JSON. Your agent ID is '{{{{agentId}}}}'.

[GLOBAL RULES (.helpwo)]
These are persistent rules set by the user. Follow them above all else:
{{{{globalMemory}}}}

[ENVIRONMENT]
- You are agent '{{{{agentName}}}}' (ID: {{{{agentId}}}}) — use /agents to view or rename yourself.
- OS: {SYSTEM} | Shell: {shell_info} | Path: {{{{currentPath}}}} | Depth: {{{{depth}}}}
- Active File: {{{{activeFile}}}}

[REASONING PROTOCOL]
1. Check [TERMINAL OUTPUT] for what just happened.
2. Check [MEMORY SYSTEM] for recent conversation context (session memory).
3. Explore before acting: if the task involves unfamiliar files or project structure, ls/cat/grep first to understand the landscape, then execute.
4. Do NOT repeat commands that have already failed unless you changed parameters.
5. If the user asks a question you already answered, repeat the reply and set done=true.
6. To ask the user for more information, ask clearly and set done=true.
7. Actively maintain global rules in .helpwo — add new rules, update outdated ones, remove obsolete ones.
8. Set done=false when you run a command that needs its output — the loop will feed it back. Set done=true ONLY when the full task is finished or you need the user to answer.

[AVAILABLE COMMANDS]
You have access to standard {shell_info} commands and all executables on $PATH.
Use pipelines (|), redirects (>), and command substitution ($(...)).
When uncertain about paths, explore with ls/pwd/cat first.
For npm/node/python projects, check package.json/requirements.txt first.

[CRITICAL RULES]
1. ONE command per response.
2. Use "rules" field to manage .helpwo global rules: "text" → append, "~N:text" → modify, "-N" → delete.
3. Track progress: "Step 2/3: created X. Next: create Y inside it."
4. PREFER absolute/relative paths over cd: "cat src/App.tsx" not "cd src" + "cat App.tsx".

[SAFETY]
- Never use destructive commands (rm -rf, del /F, format) unless the user explicitly requests.
- If a command errors, analyze and adapt — don't repeat.

[TERMINAL MANAGEMENT]
CLI-level slash commands (handled by shell): /help /login /name /memory /prop /scan /debug /cwd /clear /exit /back /q /agents
AI-level commands you can issue:
- /station <name> — create a persistent laintas-cli REPL terminal and station yourself there.
- /send <name> <cmd> — send a command to a named terminal (captures output).
- /terminate <name> — close and destroy a terminal.
- /t or /term — list all sub-terminals and enter/observe them.
- /hire — create a new AI agent; /agents — list all agents and their names; /agents <name> — switch to that agent.
- wait(N) — sleep N seconds (e.g. wait(3) for server startup).
- learn(text|path|url) — extract knowledge into conversation history as [KNOWLEDGE] (short-term).
- forget(N) — trim conversation history, keeping [KNOWLEDGE] + last N messages.
Rules:
- Every sub-terminal created by /station is a full laintas-cli instance — a peer AI agent just like you, with its own name, identity, and reasoning loop.
- Each sub-terminal AI has its own agent name (e.g. AI-1, AI-2, or custom names). Use /agents to see all agents and their names, and /agents <name> to switch yourself into an existing agent slot.
- When stationed, commands run inside the persistent laintas-cli REPL (cd, export, etc. persist across commands).
- When sending commands to a sub-terminal AI via /send, the target AI will process the command autonomously. The output you see is its reply.
- Plain commands (not stationed) work as before: one-off in temporary sub-shells.

[RESPONSE FORMAT]
Respond ONLY with a single JSON object:
{{
  "reply": "what to tell the user",
  "command": "shell cmd, /station, /send, /terminate, /hire, wait(N), parent(cmd), learn(text), or forget(N)",
  "rules": "text (append new) or ~N:text (modify entry N) or -N (delete entry N)",
  "done": true/false
}}

Each response must include exactly one "command".

Example — listing a directory:
{{"reply": "Let me check what files are in this project.", "command": "ls -la", "rules": "", "done": false}}

Example — task complete:
{{"reply": "Done. Created the config file with the settings you specified.", "command": "cat config.yaml", "rules": "Created config.yaml for the project", "done": true}}

[RECURSIVE ORCHESTRATION]
You are agent '{{{{agentName}}}}' running at depth {{{{depth}}}}.
- Depth 0 = user's terminal (output streams directly to user).
- Depth 1+ = sub-agent or tool (output shown in indented collapsed panels).
Sub-agent delegation: command "laintas-cli --execute 'task description' --depth {{{{nextDepth}}}}"
When you /station a new terminal, a new peer AI agent starts there with its own name and reasoning loop.
At depth >= 2, prefer completing the task directly.
Use parent(cmd) for parent-terminal side effects (cd, clear) when at depth >= 1.

[LANGUAGE]
You MUST respond in English. All replies, commands, and rules must be in English.
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
    """Create .cli, .cli.prop, .helpwo, .extra_command.py and .loop_command.py if they don't exist in cwd."""
    global _CLI_ALL
    cli_path = Path.cwd() / CLI_FILE
    cli_prop_path = Path.cwd() / ".cli.prop"
    helpwo_path = Path.cwd() / ".helpwo"
    extra_cmd_path = Path.cwd() / ".extra_command.py"
    loop_cmd_path = Path.cwd() / ".loop_command.py"

    if not cli_path.exists():
        refresh_commands()
        console.print(f"[dim]Scanned PATH → {cli_path} ({len(_CLI_ALL)} commands)[/dim]")
    else:
        _CLI_ALL = load_cli_commands()

    if not cli_prop_path.exists():
        template = generate_cli_prop_template()
        cli_prop_path.write_text(template, encoding="utf-8")
        console.print(f"[dim]Created {cli_prop_path}[/dim]")

    if not helpwo_path.exists():
        helpwo_path.write_text("", encoding="utf-8")
        console.print(f"[dim]Created {helpwo_path}[/dim]")

    if not extra_cmd_path.exists():
        extra_cmd_path.write_text(EXTRA_COMMAND_TEMPLATE, encoding="utf-8")
        console.print(f"[dim]Created {extra_cmd_path}[/dim]")

    if not loop_cmd_path.exists():
        loop_cmd_path.write_text(LOOP_COMMAND_TEMPLATE, encoding="utf-8")
        console.print(f"[dim]Created {loop_cmd_path}[/dim]")


def reload_default_files() -> None:
    """Delete all default files and restart laintas_cli."""
    cwd = Path.cwd()
    for name in (".cli", ".cli.prop", ".helpwo", ".extra_command.py", ".loop_command.py"):
        f = cwd / name
        if f.exists():
            f.unlink()
            console.print(f"[dim]Deleted {f}[/dim]")
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
    # Include X-User-Id for local auth server bypass
    if session.get("userId"):
        headers["X-User-Id"] = session["userId"]
    return headers


def call_backend_stream(
    session: dict,
    message: str,
    system_prompt: str,
    current_path: str,
    history: list = None,
    lang: str = "EN",
) -> dict:
    """Call Helpwo backend /api/chat/stream, same as Helpwo frontend.
    Returns parsed {reply, command, memory, done, _billing} dict."""
    backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)

    payload = {
        "message": message,
        "history": history or [],
        "currentPath": current_path,
        "systemPrompt": system_prompt,
        "lang": lang,
        "maxTokens": int(get_runtime_config("max_tokens")),
    }

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

        # Parse SSE stream
        response_data = None
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                response_data = json.loads(data_str)
                if response_data.get("error"):
                    return {"reply": f"Server Error: {response_data['error']}", "command": "", "rules": "", "done": True, "error": True}
            except json.JSONDecodeError:
                continue

        if not response_data:
            return {"reply": "No response from AI", "command": "", "rules": "", "done": True, "error": True}

        # Normalize legacy send_keys/close_session into meta-commands
        command = response_data.get("command", "")
        send_keys = response_data.get("send_keys", "")
        close_session = response_data.get("close_session", False)
        if not command and close_session:
            command = "/session close"
        elif not command and send_keys:
            command = f"/keys {send_keys}"

        return {
            "reply": response_data.get("reply", ""),
            "command": command,
            "memory": response_data.get("rules", response_data.get("memory", "")),
            "done": response_data.get("done", False),
            "error": False,
            "_billing": response_data.get("_billing", {}),
        }

    except requests.Timeout:
        return {"reply": "Request timed out. Please try again.", "command": "", "rules": "", "done": True, "error": True}
    except requests.ConnectionError:
        return {"reply": f"Cannot connect to backend ({backend_url}). Check your network.", "command": "", "rules": "", "done": True, "error": True}
    except Exception as e:
        return {"reply": f"Error: {e}", "command": "", "rules": "", "done": True, "error": True}


# ── Agent Registration with Helpwo Backend ─────────────────────────────

class AgentRegistry:
    """Manages remote agent registration with Helpwo backend."""

    def __init__(self):
        self.agent_id: Optional[str] = None
        self.agent_name: str = ""
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._message_poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._session: Optional[dict] = None
        self._processing_message = threading.Event()
        self._pending_responses: list = []  # thread-safe queue for responses to send

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
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.agent_id = data.get("agentId", "")
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

    def _heartbeat_loop(self):
        """Send heartbeat every HEARTBEAT_INTERVAL seconds."""
        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(self._session) if self._session else {}
        cookies = get_auth_cookies(self._session) if self._session else {}

        while self._running and self.agent_id:
            try:
                requests.post(
                    f"{backend_url}/api/agents/heartbeat",
                    json={"agentId": self.agent_id, "cwd": os.getcwd()},
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
        """Process an incoming message from Helpwo UI through the agent loop."""
        self._processing_message.set()

        content = msg.get("content", "")

        console.print(Panel(
            f"[bold cyan]Remote message from Helpwo:[/bold cyan]\n{content}",
            title="Incoming",
            border_style="cyan",
        ))

        try:
            state = agent_state_cb() if callable(agent_state_cb) else {
                "shortTermMemory": "", "lastReply": "", "lastOutput": ""
            }
            chat_history = chat_history_cb() if callable(chat_history_cb) else []
            session = self._session or {}

            # Build event callback that pushes to backend
            def events_cb(events: list):
                self._push_events(events)

            # Route system commands directly (same as main REPL)
            if is_system_command(content):
                self._push_events([{"type": "system", "kind": "command", "content": content}])
                result = execute_command_pty(content)
                output = (result.get("stdout") or result.get("stderr") or "(no output)")[:2000]
                self._push_events([{"type": "system", "kind": "output", "content": output}])
            else:
                # Run the agent loop — events flow through events_cb in real-time
                result = run_agent_loop(get_loop_deps(), content, session, state, chat_history, events_cb=events_cb)

            # Update lastOutput in agent state on backend
            new_state = result.get("state", state)
            backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
            headers = get_auth_headers(session) if session else {}
            cookies = get_auth_cookies(session) if session else {}

            try:
                requests.post(
                    f"{backend_url}/api/agents/{self.agent_id}/events",
                    json={
                        "events": [],
                        "state": {
                            "cwd": os.getcwd(),
                            "lastOutput": new_state.get("lastOutput", ""),
                            "status": "running",
                        },
                    },
                    headers=headers,
                    cookies=cookies,
                    timeout=10,
                )
            except requests.RequestException:
                pass

        except Exception as e:
            console.print(f"[red]Error handling remote message: {e}[/red]")
            self._push_events([{"type": "system", "kind": "error", "content": str(e)}])

        finally:
            self._processing_message.clear()

    def _push_events(self, events: list):
        """Push events to the backend event stream."""
        if not self.agent_id or not events:
            return
        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(self._session) if self._session else {}
        cookies = get_auth_cookies(self._session) if self._session else {}
        try:
            requests.post(
                f"{backend_url}/api/agents/{self.agent_id}/events",
                json={
                    "events": events,
                    "state": {"cwd": os.getcwd(), "status": "running"},
                },
                headers=headers,
                cookies=cookies,
                timeout=5,
            )
        except requests.RequestException:
            pass

    def unregister(self):
        """Unregister agent on exit."""
        self._running = False
        if not self.agent_id:
            return

        backend_url = os.environ.get("LAINTAS_BACKEND", BACKEND_URL)
        headers = get_auth_headers(self._session) if self._session else {}
        cookies = get_auth_cookies(self._session) if self._session else {}

        try:
            requests.post(
                f"{backend_url}/api/agents/unregister",
                json={"agentId": self.agent_id},
                headers=headers,
                cookies=cookies,
                timeout=5,
            )
        except requests.RequestException:
            pass
        self.agent_id = None


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
        if e.memory:
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

    if primary_session is not None and primary_session.is_alive():
        items.append(("term0 (primary)", primary_session.command, primary_session,
                      0.0, True))

    for term in get_all_terminals():
        items.append((term.name, term.command, term.session,
                      term.created_at, term.session.is_alive()))

    if not items:
        console.print("[dim]No active sub-terminal sessions. "
                      "Run an AI task that spawns a command to create one.[/dim]")
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
            # If session died during enter (user typed /q), unregister it
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
                          term.created_at, term.session.is_alive()))
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

    All keystrokes are forwarded to the session. Type /back in the
    sub-terminal to detach without closing it. Type /q to close the
    sub-terminal and return to term0. Ctrl+\\ also detaches.

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
    old_sigquit = signal.getsignal(signal.SIGQUIT)

    # Clear screen and show header
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(f"● Entered: {cmd_display}\n")
    sys.stdout.write("  /back to detach  |  /q to close  |  Ctrl+\\ to force detach\n")
    sys.stdout.write("─" * 60 + "\n\n")
    sys.stdout.flush()

    # Show pending output (current terminal state, last 100 lines)
    session.read_output(timeout=0.1)
    try:
        pending = session.raw_output
    except Exception:
        pending = session.full_output
    if pending:
        lines = pending.split('\n')
        if len(lines) > 100:
            pending = '\n'.join(lines[-100:])
        sys.stdout.write(pending)
        sys.stdout.flush()

    detached = False
    session_died = False

    def _on_sigquit(signum, frame):
        nonlocal detached
        detached = True

    signal.signal(signal.SIGQUIT, _on_sigquit)

    # Detach marker: emitted by laintas-cli when user types /back
    DETACH_MARKER = b'\x1b]777;LAINTAS_DETACH\x07'
    partial_buf = b''

    try:
        tty.setraw(fd)

        while session.is_alive() and not detached:
            try:
                r, _, _ = select.select([fd, mfd], [], [], 0.1)
            except (select.error, ValueError):
                break

            if fd in r:
                try:
                    data = os.read(fd, 4096)
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
                    # EOF — sub-terminal exited (e.g., /q)
                    session_died = True
                    break

    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old_tcattr)
        except termios.error:
            pass
        signal.signal(signal.SIGQUIT, old_sigquit)

    if session_died:
        console.print(f"\n[dim]● Sub-terminal exited. Returned to term0[/dim]")
    else:
        console.print(f"\n[green]● Detached. Returned to term0[/green]")


_extra_cmd_handler_cache = None
_extra_cmd_mtime_cache = 0


def _load_extra_commands():
    """Load .extra_command.py and return handle_extra_command() if defined."""
    global _extra_cmd_handler_cache, _extra_cmd_mtime_cache
    path = Path.cwd() / ".extra_command.py"
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

    if action == "/exit":
        close_all_terminals()
        agent_registry.unregister()
        clear_session()
        console.print("[green]Logged out. Goodbye![/green]")
        return True

    if action in ("/quit", "/q"):
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
        raw = read_file(".helpwo")
        if raw and raw.strip():
            try:
                entries = json.loads(raw)
                if isinstance(entries, list) and entries:
                    lines = [f"[bold]{e['id']}.[/bold] {e['content']}" for e in entries]
                    text = "\n".join(lines)
                    console.print(Panel(text, title=f".helpwo Memory ({len(entries)} entries)"))
                else:
                    console.print(Panel(raw.strip(), title=".helpwo Memory"))
            except json.JSONDecodeError:
                console.print(Panel(raw.strip(), title=".helpwo Memory"))
        else:
            console.print("[dim]No memory yet. The AI will record learnings here.[/dim]")

    elif action == "/prop":
        prop = read_file(".cli.prop")
        if prop:
            console.print(Panel(prop[:2000], title=".cli.prop Prompt Template"))
        else:
            console.print("[dim]No .cli.prop found.[/dim]")

    elif action == "/scan":
        refresh_commands()
        all_cmds = get_dispatch_commands()
        user_cmds = _filter_user_commands(all_cmds)
        console.print(f"[bold]{len(all_cmds)} total on PATH, {len(user_cmds)} user-facing:[/bold]\n")
        # Show user-facing commands grouped by first letter
        groups: dict = {}
        for c in user_cmds:
            groups.setdefault(c[0], []).append(c)
        for letter in sorted(groups):
            console.print(f"  [cyan]{letter}[/cyan]: {', '.join(groups[letter][:40])}")
            if len(groups[letter]) > 40:
                console.print(f"       [dim](+{len(groups[letter]) - 40} more)[/dim]")
        # Show count of filtered-out system commands
        hidden = len(all_cmds) - len(user_cmds)
        if hidden:
            console.print(f"\n[dim]{hidden} system/internal commands hidden (see .cli for full list)[/dim]")

    elif action == "/cwd":
        console.print(f"Working directory: [bold]{os.getcwd()}[/bold]")

    elif action == "/clear":
        console.clear()

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
        if len(parts) < 2:
            console.print("[yellow]Usage: /station <name>[/yellow]")
            console.print("  Creates a persistent terminal running laintas-cli and stations your AI agent there.")
        else:
            name = parts[1]
            existing = get_terminal(name)
            if existing and not existing.session.is_alive():
                unregister_terminal(name)
                existing = None
            if existing is None:
                lain_cmd = f"{sys.executable} {os.path.abspath(__file__)} --simple-prompt"
                sub = SubTerminalSession(lain_cmd)
                sub.start()
                time.sleep(0.3)
                if sub.is_alive():
                    sub.read_output(timeout=0.3)
                register_terminal(sub, "laintas-cli", 0, name=name)
            current = get_current_agent()
            if current:
                station_agent(current.id, name)
                console.print(f"[green]Stationed [bold]{current.name}[/bold] in terminal [bold]{name}[/bold][/green]")
            else:
                console.print(f"[green]Created terminal [bold]{name}[/bold][/green]")

    elif action == "/terminate":
        if len(parts) < 2:
            console.print("[yellow]Usage: /terminate <name>[/yellow]")
        else:
            name = parts[1]
            term = get_terminal(name)
            if term:
                for aid in list(term.stationed_agent_ids):
                    unstation_agent(aid)
            if unregister_terminal(name):
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
            elif not term.session.is_alive():
                console.print(f"[yellow]Terminal '{name}' is dead.[/yellow]")
            else:
                term.session.send_keys(cmd + "\n")
                time.sleep(0.3)
                term.session.read_output(timeout=0.5)
                output = term.session.full_output
                console.print(f"[dim]Sent to [bold]{name}[/bold]: {cmd[:80]}[/dim]")
                if output.strip():
                    console.print(Panel(output[-2000:], title=f"{name} output"))

    elif action == "/hire":
        agent_info = register_agent(depth=0)
        console.print(f"[green]Hired [bold]{agent_info.id}[/bold][/green]")
        console.print(f"  Switch with [bold]/agents {agent_info.id}[/bold]")

    elif action == "/agents":
        if len(parts) == 1:
            agents = get_all_agents()
            current = get_current_agent()
            if not agents:
                console.print("[dim]No agents.[/dim]")
            else:
                for a in agents:
                    marker = " [bold cyan]<- current[/bold cyan]" if (current and a.id == current.id) else ""
                    st = f" [dim]stationed: {a.stationed_terminal}[/dim]" if a.stationed_terminal else ""
                    console.print(f"  [bold]{a.id}[/bold]: {a.name}{st}{marker}")
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

    elif action in ("/t", "/term"):
        terminals = get_all_terminals()
        has_primary = interactive_session is not None and interactive_session.is_alive()
        if not terminals and not has_primary:
            console.print("[dim]No active sub-terminal sessions. "
                          "Use /station or let the AI spawn a command.[/dim]")
        elif not terminals and has_primary:
            enter_session(interactive_session)
        else:
            show_terminal_manager(interactive_session)

    else:
        # Try .extra_command.py custom handler first
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
                console.print(f"[red].extra_command.py error: {e}[/red]")
        console.print(f"[red]Unknown command: {action}[/red]")
        console.print("Type [bold]/help[/bold] for available commands.")

    return False


def show_help():
    """Display help."""
    table = Table(title="laintas_cli Commands")
    table.add_column("Command", style="cyan")
    table.add_column("Description")

    table.add_row("ls, cat, mkdir, git, ...", "Commands found on PATH → executed directly")
    table.add_row("<natural language>", "Not a recognized command → AI agent loop")
    table.add_row("/help", "Show this help")
    table.add_row("/login", "Re-authenticate with laintas.com (opens browser)")
    table.add_row("/name [name]", "Set current agent name")
    table.add_row("/memory", "View .helpwo memory file")
    table.add_row("/prop", "View .cli.prop prompt template")
    table.add_row("/scan", "Scan and list all available system commands from PATH")
    table.add_row("/debug", "Browse debug entries (/debug), view detail (/debug <N>), save to file (/debug <N> <file>), clear (/debug clear)")
    table.add_row("/cwd", "Show current working directory")
    table.add_row("/station <name>", "Create persistent terminal and station AI there")
    table.add_row("/terminate <name>", "Close and destroy a terminal")
    table.add_row("/send <name> <cmd>", "Send a command to a named terminal")
    table.add_row("/hire", "Create a new AI agent (AI-1, AI-2...)")
    table.add_row("/agents [name]", "List/switch agents, /agents name <n> to rename")
    table.add_row("/t, /term", "List sub-terminals, Enter to embody, o to observe")
    table.add_row("/back", "Detach from sub-terminal without closing it")
    table.add_row("/clear", "Clear screen")
    table.add_row("/exit", "Log out and exit (clears cached session)")
    table.add_row("/quit, /q", "Exit without logging out (keeps cached session)")

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
        f"Commands from PATH ({len(get_dispatch_commands())} total) → executed directly.\n"
        "Natural language → AI agent loop.\n"
        "Type [bold]/help[/bold] for commands.",
        title="laintas_cli",
        border_style="blue",
    ))


def _simple_prompt(cwd: str) -> str:
    """Plain input() prompt for PTY subprocess environments.

    prompt_toolkit requires a real terminal and won't work inside a PTY.
    Use this when running as a sub-agent of another laintas-cli instance.
    """
    try:
        print(f"{cwd}\n$ ", end="", flush=True)
        return sys.stdin.readline().strip()
    except (KeyboardInterrupt, EOFError):
        return ""


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
    args = parser.parse_args()

    # Apply environment overrides
    if args.backend:
        os.environ["LAINTAS_BACKEND"] = args.backend
    if args.laintas:
        os.environ["LAINTAS_BASE"] = args.laintas

    # Child terminals (depth > 0): use plain console to avoid Rich markup in PTY
    global console
    if args.depth > 0:
        console = Console(force_terminal=False, no_color=True, width=120, highlighter=None)

    # Ensure .cli.prop and .helpwo exist in cwd
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
        global pt_prompt
        pt_prompt = _simple_prompt

    # Show banner (skip in child terminals to avoid Rich output in PTY)
    if args.depth == 0:
        show_banner(agent_name, session if session else None)

    # Register as remote agent (only if authenticated)
    agent_registry = AgentRegistry()
    agent_state = {
        "shortTermMemory": "",
        "lastReply": "",
        "lastOutput": "",
    }
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

    # Register the primary agent
    primary = register_agent(name="primary", depth=0)
    set_current_agent_id("primary")

    # Setup graceful shutdown
    def shutdown(signum=None, frame=None):
        console.print("\n[yellow]Shutting down...[/yellow]")
        close_all_terminals()
        close_all_agents()
        nonlocal interactive_session
        if interactive_session:
            interactive_session.close()
        agent_registry.unregister()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Main interactive loop

    while True:
        try:
            user_input = pt_prompt(str(os.getcwd()))
        except (KeyboardInterrupt, EOFError):
            shutdown()

        if not user_input:
            continue

        # Ctrl+D → exit
        if user_input == "/exit":
            close_all_terminals()
            if interactive_session:
                interactive_session.close()
            agent_registry.unregister()
            clear_session()
            console.print("[green]Logged out. Goodbye![/green]")
            return

        # Check for meta commands
        if user_input.startswith("/"):
            should_exit = handle_meta_command(user_input, agent_registry, session, interactive_session)
            if should_exit:
                if interactive_session:
                    interactive_session.close()
                return
            continue

        # ── Session-aware routing ──────────────────────────────────
        # If an interactive session is active, forward user input to it,
        # then ask the AI to decide the next step based on the output.
        # Child terminals (depth > 0): always route through AI, never
        # forward to sub-sessions — the child laintas IS the terminal.

        if args.depth == 0 and interactive_session and interactive_session.is_alive():
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
                    response = run_agent_loop(get_loop_deps(), context, session, agent_state, chat_history,
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
                    response = run_agent_loop(get_loop_deps(), context, session, agent_state, chat_history,
                                              events_cb=local_events_cb,
                                              existing_session=interactive_session)
                    interactive_session = response.get("session")
                else:
                    response = {"success": True, "msg": "", "state": agent_state, "session": interactive_session}

            # Save reply
            if response.get("msg"):
                chat_history.append({"role": "assistant", "content": response["msg"]})
            agent_state = {
                "shortTermMemory": "",
                "lastReply": "",
                "lastOutput": response.get("state", {}).get("lastOutput", ""),
            }
            continue

        # ── Normal input routing ───────────────────────────────────

        # Add to chat history
        chat_history.append({"role": "user", "content": user_input})

        # Push user input event to remote stream
        if agent_registry.agent_id:
            agent_registry._push_events([{"type": "user", "content": user_input}])

        # Regex match first word against .cli → system command or AI
        # Child terminals (depth > 0): always route through AI — the parent
        # controls this terminal and expects intelligent processing.
        if args.depth == 0 and is_system_command(user_input):
            console.print(f"\n[dim yellow]$ {user_input}[/dim yellow]")
            if agent_registry.agent_id:
                agent_registry._push_events([{"type": "system", "kind": "command", "content": user_input}])

            # Close previous AI-managed interactive session if any
            if interactive_session is not None:
                interactive_session.close()
                interactive_session = None

            # Drain any queued terminal query responses before passthrough
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

            # Full terminal passthrough — user interacts directly with the command
            result = pty_passthrough(user_input)

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

            if agent_registry.agent_id:
                output_preview = result.get("stdout", "")[:2000]
                agent_registry._push_events([{"type": "system", "kind": "output", "content": output_preview}])

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
                continue
            console.print("[dim]Not a system command, asking AI...[/dim]")

            # Build event callback for real-time streaming
            def local_events_cb(events: list):
                if agent_registry.agent_id:
                    agent_registry._push_events(events)

            # Child terminals (depth > 0): don't carry sessions across iterations.
            # Each input goes through a fresh AI loop — the child laintas IS the terminal.
            _existing = interactive_session if args.depth == 0 else None
            response = run_agent_loop(get_loop_deps(), user_input, session, agent_state, chat_history,
                                      events_cb=local_events_cb,
                                      existing_session=_existing)
            interactive_session = response.get("session") if args.depth == 0 else None

        # Save AI reply to chat history
        if response.get("msg"):
            chat_history.append({"role": "assistant", "content": response["msg"]})

        # Reset agent state for next interaction
        agent_state = {
            "shortTermMemory": "",
            "lastReply": "",
            "lastOutput": response.get("state", {}).get("lastOutput", ""),
        }


if __name__ == "__main__":
    main()
