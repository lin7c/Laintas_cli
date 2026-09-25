"""Run a command the user typed in term0 with the real terminal attached.

A direct command used to be typed into term0's bash wrapped as
``echo BEGIN; cmd; echo END:$?`` and the output polled for the END marker,
then printed in one piece once the command was over. That is a collector, not
a terminal: no live output, no keyboard (only guessed "[Y/n]" prompts could be
answered), Esc instead of Ctrl+C, an idle timeout that killed quiet commands,
and a whitelist that sent vim/ssh/python to a *different* shell that had none
of term0's cd/export/alias/venv. ``exec``/``exit`` removed the shell that was
supposed to print END, and a program that kept redrawing kept resetting the
idle clock — so the CLI waited for ever.

Here the user's terminal is wired to term0's pty for the command's lifetime,
the way tmux attaches a client: keystrokes go in verbatim, output comes out
verbatim, the window size follows. Ctrl+C is a byte the pty turns into SIGINT
for the command's process group — never for the CLI. Completion is reported by
the shell itself (shell integration, as iTerm2 and VS Code do): PS0/preexec
prints a *start* sequence, PROMPT_COMMAND/precmd prints *done* with the exit
status and the directory. Both are private OSC 777 sequences carrying a
per-shell nonce, so output that happens to contain one (a nested laintas-cli,
a log being cat'ed) cannot end the command early. They are stripped from what
is shown.

The command is never typed into the shell. It is written to a private file and
the shell is sent a fixed ``eval "$(<file)"`` line: typed text would be
interpreted by the line editor (a Tab would complete, a newline would submit a
half heredoc), while eval at top level keeps every semantic of a typed command
— cd, export, alias, exec, exit, job control. And because the only echoed line
is that fixed trigger, everything before *start* is known to be echo and is
dropped.

Ctrl+] detaches: the command keeps running in term0, /fg reattaches. The shell
ending (exit, exec'd program ending) is reported to the caller, which starts a
fresh term0 in the last known directory.

Only bash >= 4.4 (PS0) and zsh get the integration; any other shell, a
non-tty, or a failure to integrate returns None and the caller keeps the old
marker-poll path.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

#: Ctrl+] — the telnet/ssh-escape convention. Ctrl+\ is SIGQUIT in a real
#: terminal and must reach the program.
DETACH_KEY = b"\x1d"

_ALT_SCREEN_RE = re.compile(rb"\x1b\[\?(?:1049|1047|47)h")
#: Private modes a program may leave on in the *user's* terminal when it is
#: killed, or while the user detaches from it: the alternate screen and mouse
#: reporting. Whatever is still on is switched off before the CLI draws again.
_MODE_RE = re.compile(rb"\x1b\[\?((?:\d+;)*\d+)([hl])")
_TRACKED_MODES = {1049: b"\x1b[?1049l", 1047: b"\x1b[?1047l", 47: b"\x1b[?47l",
                  1000: b"\x1b[?1000l", 1002: b"\x1b[?1002l",
                  1003: b"\x1b[?1003l", 1006: b"\x1b[?1006l", 1015: b"\x1b[?1015l"}
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"          # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[PX^_][^\x1b]*\x1b\\"        # DCS/SOS/PM/APC
    r"|\x1b[@-Z\\-_]"                   # 2-byte
)
#: What the model is handed of one command's output (the tail survives).
_CAPTURE_LIMIT = 256 * 1024
#: How long the shell gets to acknowledge the integration line.
_READY_TIMEOUT = 3.0


def _seq(nonce: str, body: str) -> bytes:
    return f"\x1b]777;laintas;{nonce};{body}\x07".encode()


def _shell_kind(shell: str) -> str:
    name = os.path.basename(str(shell or "")).lstrip("-")
    if name in ("bash", "zsh"):
        return name
    return ""


def integration_line(kind: str, nonce: str, cmdfile: str) -> str:
    """The one line that prepares term0's shell for attached commands.

    Defines the helpers the trigger line calls, and hooks ``__laintas_done``
    in front of the user's own PROMPT_COMMAND / precmd (starship, direnv, …)
    — chained, never replacing them, and ``$?`` captured before anything else
    runs so their view of it is unchanged. That hook is only the fallback for
    a line Ctrl+C abandoned: the trigger line reports completion itself, so a
    user who later overwrites PROMPT_COMMAND or PS0 cannot make a command
    hang. ``stty echo`` gives programs a terminal that echoes what the user
    types (term0 was spawned with echo off for programmatic input; the line
    editor echoes on its own either way).
    """
    osc = "\\033]777;laintas;" + nonce + ";"
    file_q = shlex.quote(cmdfile)
    common = (
        f" __LAINTAS_CMDFILE={file_q}; "
        f"__LAINTAS_START=$'\\033]777;laintas;{nonce};start\\007'; "
        "__laintas_done() { local __laintas_s=$?; "
        f"printf '{osc}done;%s;%s;%s\\007' \"${{__LAINTAS_TOK:-}}\" \"$__laintas_s\" \"$PWD\"; "
        "__LAINTAS_TOK=; return $__laintas_s; }; "
        "__laintas_ret() { return \"$1\"; }; "
    )
    # The trigger line starts with a space and must not reach the user's
    # history file; the command itself should, as if typed at a prompt.
    ready = f"stty echo 2>/dev/null; printf '{osc}ready;1\\007'"
    if kind == "bash":
        return common + (
            "__laintas_hook() { "
            "if [[ \"$(declare -p PROMPT_COMMAND 2>/dev/null)\" == \"declare -a\"* ]]; then "
            "[[ \" ${PROMPT_COMMAND[*]} \" == *\" __laintas_done \"* ]] "
            "|| PROMPT_COMMAND=(__laintas_done \"${PROMPT_COMMAND[@]}\"); "
            "else [[ \"${PROMPT_COMMAND:-}\" == __laintas_done* ]] "
            "|| PROMPT_COMMAND=\"__laintas_done${PROMPT_COMMAND:+;$PROMPT_COMMAND}\"; fi; }; "
            "HISTCONTROL=\"ignorespace${HISTCONTROL:+:$HISTCONTROL}\"; "
            "history -d \"$HISTCMD\" 2>/dev/null; "
            "__laintas_hist() { history -s -- \"$__LAINTAS_CMD\" 2>/dev/null; }; "
            "__laintas_hook; " + ready)
    if kind == "zsh":
        return common + (
            "__laintas_hook() { "
            "(( ${precmd_functions[(Ie)__laintas_done]} )) "
            "|| precmd_functions=(__laintas_done $precmd_functions); }; "
            "setopt HIST_IGNORE_SPACE 2>/dev/null; "
            "__laintas_hist() { print -rs -- \"$__LAINTAS_CMD\" 2>/dev/null; }; "
            "__laintas_hook; " + ready)
    return ""


def trigger(token: str) -> str:
    """The line typed into the shell for one command.

    Everything the user's command must not notice happens around it: ``$?``
    is saved first and restored right before the eval (``&& :`` so a
    non-zero status cannot trip ``set -e``), the file is read into a variable
    (a command substitution in eval's own argument would reset ``$?``), and
    *start* / *done* are printed by this line itself. The token makes this
    command's *done* distinguishable from any other the shell prints — the
    PROMPT_COMMAND fallback's, or one from an agent command run by
    marker-poll whose trailing *done* had not arrived when it returned.

    ``set -e`` never ends term0 here, deliberately. Run through eval, a line
    like ``false && true`` (which a real interactive bash survives) returns
    non-zero from eval itself and errexit would kill the shell; and a shell
    dying takes the user's cd/venv/exports with it. The eval and the report
    therefore sit in ``&& :`` lists, where errexit is ignored, and a failing
    command leaves the shell up — unlike a real terminal, which closes.
    """
    return (f' __LAINTAS_PREV=$?; __laintas_hook; __LAINTAS_TOK={token}; '
            '__LAINTAS_CMD=$(<"$__LAINTAS_CMDFILE"); __laintas_hist; printf %s "$__LAINTAS_START"; '
            '__laintas_ret "$__LAINTAS_PREV" && :; eval "$__LAINTAS_CMD" && :; '
            '__laintas_done && :')


@dataclass
class _Integration:
    nonce: str
    cmdfile: str
    start: bytes
    #: Every sequence this shell's integration prints starts with this.
    prefix: bytes

    def done_for(self, token: str) -> bytes:
        return self.prefix + f"done;{token};".encode()


@dataclass
class AttachResult:
    #: "done" (the shell reported completion), "detached" (Ctrl+]),
    #: "ended" (the shell itself went away: exit, exec'd program ended).
    status: str
    returncode: int = -1
    cwd: str = ""
    output: str = ""
    fullscreen: bool = False


@dataclass
class DetachedJob:
    command: str
    scanner: "_Scanner"
    offset: int
    detached_at: float = field(default_factory=time.time)


# ── the stream scanner ───────────────────────────────────────────────────

class _Scanner:
    """Split term0's output into what the user sees and the control marks.

    ``pre``: before *start* — the shell echoing the trigger line; dropped.
    ``run``: the command's own output; shown and captured.
    Only the *done* carrying this command's token ends it; any other sequence
    of this shell's integration (a stale *done*, a second *start*) is removed
    from the stream. A sequence split across two reads is held back until it
    is whole, so a half marker never reaches the screen.
    """

    def __init__(self, integ: _Integration, token: str, started: bool = False):
        self._start = integ.start
        self._prefix = integ.prefix
        self._done = integ.done_for(token)
        self.phase = "run" if started else "pre"
        self._pending = b""
        self.finished = False
        self.returncode = -1
        self.cwd = ""
        self.fullscreen = False
        self._captured = bytearray()
        self.last_byte = b"\n"
        #: Tracked private modes the program currently has on (see _MODE_RE).
        self.modes_on: set = set()

    @property
    def done_marker(self) -> bytes:
        return self._done

    def _hold_back(self, buf: bytes) -> int:
        """Index from which *buf* might be an unfinished integration sequence."""
        cut = buf.rfind(b"\x1b", max(0, len(buf) - len(self._prefix) - 1))
        while cut >= 0:
            tail = buf[cut:]
            if self._prefix.startswith(tail):
                return cut
            if tail.startswith(self._prefix) and b"\x07" not in tail:
                return cut
            break
        # An integration sequence longer than the window (a long cwd).
        at = buf.rfind(self._prefix)
        if at >= 0 and b"\x07" not in buf[at:]:
            return at
        return len(buf)

    def _strip_marks(self, data: bytes) -> bytes:
        """Drop every complete sequence of this shell's integration."""
        out = bytearray()
        i = 0
        while True:
            j = data.find(self._prefix, i)
            if j < 0:
                out += data[i:]
                return bytes(out)
            out += data[i:j]
            end = data.find(b"\x07", j)
            if end < 0:           # cannot happen after _hold_back; keep it safe
                return bytes(out)
            i = end + 1

    def feed(self, data: bytes) -> bytes:
        """Consume *data*; return the bytes to put on the screen."""
        if self.finished:
            return b""
        buf = self._pending + data
        self._pending = b""
        shown = b""
        if self.phase == "pre":
            s = buf.find(self._start)
            d = buf.find(self._done)
            if d >= 0 and (s < 0 or d < s):
                buf = buf[d:]           # our done without a start: finish
                self.phase = "run"
            elif s >= 0:
                buf = buf[s + len(self._start):]
                self.phase = "run"
            else:
                keep = self._hold_back(buf)
                self._pending = buf[keep:]
                return b""
        d = buf.find(self._done)
        if d < 0:
            keep = self._hold_back(buf)
            shown = buf[:keep]
            self._pending = buf[keep:]
        else:
            end = buf.find(b"\x07", d + len(self._done))
            if end < 0:
                shown = buf[:d]
                self._pending = buf[d:]
            else:
                shown = buf[:d]
                body = buf[d + len(self._done):end].decode("utf-8", "replace")
                rc, _, cwd = body.partition(";")
                try:
                    self.returncode = int(rc)
                except ValueError:
                    self.returncode = -1
                self.cwd = cwd
                self.finished = True
        out = self._strip_marks(shown)
        if out:
            if _ALT_SCREEN_RE.search(out):
                self.fullscreen = True
            for match in _MODE_RE.finditer(out):
                for part in match.group(1).split(b";"):
                    mode = int(part)
                    if mode in _TRACKED_MODES:
                        if match.group(2) == b"h":
                            self.modes_on.add(mode)
                        else:
                            self.modes_on.discard(mode)
            self._captured += out
            if len(self._captured) > _CAPTURE_LIMIT:
                del self._captured[:len(self._captured) - _CAPTURE_LIMIT]
            self.last_byte = out[-1:]
        return out

    def text(self) -> str:
        """What the command printed, as plain text for the model."""
        return clean_output(bytes(self._captured))

    def modes_off(self) -> bytes:
        """Sequences that switch off whatever tracked mode is still on."""
        return b"".join(_TRACKED_MODES[m] for m in sorted(self.modes_on))

    def modes_back_on(self) -> bytes:
        return b"".join(_TRACKED_MODES[m][:-1] + b"h" for m in sorted(self.modes_on))


def clean_output(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = _ANSI_RE.sub("", text)
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        # A bare CR redraws the line (progress bars): keep what stayed.
        if "\r" in line:
            line = [p for p in line.split("\r") if p][-1:] or [""]
            line = line[0]
        lines.append(line)
    return "\n".join(lines).strip("\n")


# ── integration ──────────────────────────────────────────────────────────

_integrations: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_integrations_lock = threading.Lock()


def _write_all(fd: int, data: bytes, deadline: float = 2.0) -> bool:
    """Write everything to a non-blocking pty master; False if it never drains."""
    view = memoryview(data)
    stop = time.monotonic() + deadline
    while view:
        try:
            n = os.write(fd, view)
            view = view[n:]
        except BlockingIOError:
            if time.monotonic() >= stop:
                return False
            select.select([], [fd], [], 0.05)
        except InterruptedError:
            continue
    return True


def _drain_into_buffer(session) -> None:
    """Pull whatever the pty has into the session's own buffer."""
    reader = getattr(session, "_read_output_unlocked", None)
    if not callable(reader):
        return
    for _ in range(256):
        if not reader(0):
            break


def integration(session) -> Optional[_Integration]:
    """Install (once) and return the shell integration for *session*.

    The caller holds ``session.command_lock`` and ``session.output_lock``.
    Returns None when this shell cannot be integrated; that is remembered,
    so the attempt is made once per shell.
    """
    with _integrations_lock:
        if session in _integrations:
            return _integrations[session]
    kind = _shell_kind(getattr(session, "command", ""))
    fd = int(getattr(session, "master_fd", -1))
    result: Optional[_Integration] = None
    if kind and fd >= 0 and session.is_alive():
        nonce = secrets.token_hex(8)
        directory = tempfile.mkdtemp(prefix="laintas-term-")
        cmdfile = os.path.join(directory, "command")
        weakref.finalize(session, shutil.rmtree, directory, True)
        _drain_into_buffer(session)
        offset = session.output_total
        ready = f"\x1b]777;laintas;{nonce};ready;"
        if _write_all(fd, (integration_line(kind, nonce, cmdfile) + "\r").encode()):
            deadline = time.monotonic() + _READY_TIMEOUT
            while time.monotonic() < deadline and session.is_alive():
                session._read_output_unlocked(0.05)
                seen = session.output_from(offset)
                at = seen.find(ready)
                if at >= 0 and "\x07" in seen[at:]:
                    if seen[at + len(ready):at + len(ready) + 1] == "1":
                        result = _Integration(
                            nonce=nonce, cmdfile=cmdfile,
                            start=_seq(nonce, "start"),
                            prefix=f"\x1b]777;laintas;{nonce};".encode())
                    break
        if result is None:
            shutil.rmtree(directory, True)
    with _integrations_lock:
        _integrations[session] = result
    return result


# ── detached jobs ────────────────────────────────────────────────────────

def detached_job(session) -> Optional[DetachedJob]:
    """The command the user detached from in *session*, if one is running.

    Only a real DetachedJob counts: any other object under that name (a
    mock, a stale attribute) must not make a shell look busy.
    """
    job = getattr(session, "_laintas_fg_job", None)
    return job if isinstance(job, DetachedJob) else None


def is_busy(session) -> bool:
    """A command the user detached from is still running in this shell.

    Settles a job that has finished since anyone last looked. The main loop
    only looks before drawing a prompt, so without this an agent turn, or a
    Helpwo command while the CLI sits at the prompt, was refused for a command
    that had already ended.
    """
    if detached_job(session) is None:
        return False
    try:
        _settle(session)
    except Exception:
        pass
    return detached_job(session) is not None


def _settle(session) -> None:
    """Poll the detached job; keep a finished one's result for take_finished()."""
    job = detached_job(session)
    if job is None:
        return
    res = poll_detached(session)
    if res is not None:
        session._laintas_fg_finished = (job.command, res)


def take_finished(session) -> Optional[Tuple[str, AttachResult]]:
    """(command, result) of a detached job that finished and was not yet reported.

    Settling happens wherever is_busy() is asked; reporting belongs to the
    main loop, which says so to the user before the next prompt.
    """
    is_busy(session)
    finished = getattr(session, "_laintas_fg_finished", None)
    if finished is None:
        return None
    session._laintas_fg_finished = None
    return finished


def busy_message(session) -> str:
    job = detached_job(session)
    what = job.command if job else "a command"
    return (f"term0 is still running `{what}` (detached with Ctrl+]). "
            "Use /fg to return to it, or Ctrl+C it there.")


def poll_detached(session) -> Optional[AttachResult]:
    """Has the detached command finished? Non-blocking; clears the job if so.

    Reads only what is already in the pty, and never waits for a lock: the
    main loop calls this before every prompt.
    """
    job = detached_job(session)
    if job is None:
        return None
    lock = getattr(session, "output_lock", None)
    if lock is not None and not lock.acquire(blocking=False):
        return None
    try:
        _drain_into_buffer(session)
        if not session.is_alive():
            session._laintas_fg_job = None
            return AttachResult("ended", output=job.scanner.text())
        text = session.output_from(job.offset)
        marker = job.scanner.done_marker.decode()
        if marker not in text:
            return None
        # Account for it exactly as an attached run would have.
        job.scanner.feed(text.encode("utf-8", "replace"))
        job.offset = session.output_total
        if not job.scanner.finished:
            return None
        session._laintas_fg_job = None
        _mark_clean(session, job.scanner.cwd)
        return AttachResult("done", job.scanner.returncode, job.scanner.cwd,
                            job.scanner.text(), job.scanner.fullscreen)
    finally:
        if lock is not None:
            lock.release()


def _mark_clean(session, cwd: str) -> None:
    try:
        session._laintas_shell_dirty = False
        if cwd:
            session._laintas_last_cwd = cwd
    except Exception:
        pass


# ── the attachment ───────────────────────────────────────────────────────

def _stdout_writer() -> Callable[[bytes], None]:
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        def write(data: bytes) -> None:
            out.write(data)
            out.flush()
        return write
    fd = sys.stdout.fileno()

    def write_fd(data: bytes) -> None:
        _write_all(fd, data, deadline=5.0)
    return write_fd


def _winsize(fd: int) -> Optional[bytes]:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return None


def _sync_winsize(master_fd: int) -> None:
    try:
        size = _winsize(sys.stdout.fileno())
    except (OSError, ValueError):
        size = None
    if size is not None:
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass


def run(session, command: str, *, hold, write=None, mirror=None,
        lock_timeout: float = 2.0) -> Optional[AttachResult]:
    """Run *command* in *session* with the terminal attached.

    ``hold`` is ``terminal_arbiter.hold``-shaped: ``hold(owner, mode,
    timeout=, raw_bytes=True)`` returning a context whose value has
    ``read_bytes(timeout)``. ``mirror(text)`` receives what is shown, for the
    /agents and Helpwo mirrors. Returns None when the command could not be
    attached (no integration, locks busy) — nothing was sent, so the caller
    can use another path.
    """
    return _attach(session, command=command, hold=hold, write=write,
                   mirror=mirror, lock_timeout=lock_timeout)


def resume(session, *, hold, write=None, mirror=None,
           lock_timeout: float = 2.0) -> Optional[AttachResult]:
    """Reattach to the detached job (/fg), replaying what it printed since."""
    if detached_job(session) is None:
        return None
    return _attach(session, command=None, hold=hold, write=write,
                   mirror=mirror, lock_timeout=lock_timeout)


def _attach(session, *, command, hold, write, mirror, lock_timeout):
    write = write or _stdout_writer()
    cmd_lock = getattr(session, "command_lock", None)
    out_lock = getattr(session, "output_lock", None)
    if cmd_lock is not None and not cmd_lock.acquire(timeout=lock_timeout):
        return None
    try:
        if out_lock is not None and not out_lock.acquire(timeout=lock_timeout):
            return None
        try:
            return _attach_locked(session, command, hold, write, mirror)
        finally:
            if out_lock is not None:
                out_lock.release()
    finally:
        if cmd_lock is not None:
            cmd_lock.release()


def _attach_locked(session, command, hold, write, mirror):
    if not session.is_alive():
        return None
    integ = integration(session)
    if integ is None:
        return None
    fd = int(session.master_fd)
    job = detached_job(session)
    replay = b""
    if command is None:
        if job is None:
            return None
        scanner = job.scanner
        _drain_into_buffer(session)
        replay = session.output_from(job.offset).encode("utf-8", "replace")
        shown_command = job.command
    else:
        if job is not None:
            return None     # the caller checks is_busy() first
        token = secrets.token_hex(6)
        scanner = _Scanner(integ, token)
        shown_command = command
        # Whatever the shell printed while nobody watched (its prompt, a
        # background job) belongs to before this command.
        _drain_into_buffer(session)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            cfd = os.open(integ.cmdfile, flags, 0o600)
            with os.fdopen(cfd, "w", encoding="utf-8") as fh:
                fh.write(command + "\n")
        except OSError:
            return None

    main_thread = threading.current_thread() is threading.main_thread()
    old_winch = None
    if main_thread:
        try:
            old_winch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, lambda *_: _sync_winsize(fd))
        except (ValueError, OSError):
            main_thread = False
    _sync_winsize(fd)

    status = None
    try:
        with hold("term0", _raw_mode(), timeout=2.0, raw_bytes=True) as term:
            if command is not None:
                # Ctrl+U first: anything typed into the shell's line editor
                # after the last command must not become a prefix of this one.
                if not _write_all(fd, b"\x15" + trigger(token).encode() + b"\r"):
                    # The shell is not reading and part of the line may be in
                    # it: let the caller's path recover the shell first.
                    session._laintas_shell_dirty = True
                    return None
            if command is None:
                # Back to a program that may be full-screen: restore its modes
                # and have it redraw (SIGWINCH), rather than typing into it.
                _show_raw(scanner.modes_back_on(), write)
            if replay:
                _show(scanner.feed(replay), write, mirror)
            if command is None and not scanner.finished:
                _redraw(fd)
            status = "done" if scanner.finished else None
            while status is None:
                data = term.read_bytes(timeout=0.01)
                if data is not None:
                    if not data:
                        status = "detached"    # our own stdin closed
                        break
                    cut = data.find(DETACH_KEY)
                    if cut >= 0:
                        if cut:
                            _write_all(fd, data[:cut])
                        status = "detached"
                        break
                    if not _write_all(fd, data):
                        pass    # the program is not reading; like a full tty
                try:
                    ready, _, _ = select.select([fd], [], [], 0.01)
                except (OSError, ValueError):
                    status = "ended"
                    break
                if not ready:
                    if _shell_exited(session):
                        status = "ended"
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        status = "ended"
                        break
                    raise
                if not chunk:
                    status = "ended"
                    break
                session._record_output(chunk)
                _show(scanner.feed(chunk), write, mirror)
                if scanner.finished:
                    status = "done"
    finally:
        if main_thread and old_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, old_winch)
            except (ValueError, OSError):
                pass

    if status == "ended":
        # What the shell's last program printed on its way out.
        _show(scanner.feed(_read_rest(fd, session)), write, mirror)

    # Leave the user's terminal the way the CLI needs it: a program that was
    # killed, or that we detached from, may still have the alternate screen or
    # mouse reporting on. (The scanner keeps the set, for /fg.)
    _show_raw(scanner.modes_off(), write)

    if scanner.last_byte not in (b"\n", b"") and not scanner.fullscreen:
        # The next prompt starts on its own line, as a shell's would.
        try:
            write(b"\r\n")
        except OSError:
            pass
        scanner.last_byte = b"\n"

    if status == "done":
        session._laintas_fg_job = None
        _mark_clean(session, scanner.cwd)
        return AttachResult("done", scanner.returncode, scanner.cwd,
                            scanner.text(), scanner.fullscreen)
    if status == "detached":
        session._laintas_fg_job = DetachedJob(
            command=shown_command, scanner=scanner,
            offset=session.output_total)
        return AttachResult("detached", output=scanner.text(),
                            fullscreen=scanner.fullscreen)
    session._laintas_fg_job = None
    try:
        session._check_child()
    except Exception:
        pass
    return AttachResult("ended", getattr(session, "returncode", -1), "",
                        scanner.text(), scanner.fullscreen)


def _shell_exited(session) -> bool:
    """Has term0's shell exited? Reaps it without touching its output.

    ``is_alive()`` would also drain the pty into the session buffer, and those
    bytes would then never pass through the scanner onto the screen.
    """
    reap = getattr(session, "_reap_child", None)
    if callable(reap) and hasattr(session, "_returncode"):
        try:
            reap()
        except Exception:
            return False
        return session._returncode != -1
    try:
        return not session.is_alive()
    except Exception:
        return True


def _read_rest(fd: int, session) -> bytes:
    """Everything still buffered in the pty after its shell exited."""
    rest = bytearray()
    for _ in range(64):
        try:
            ready, _, _ = select.select([fd], [], [], 0.05)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        try:
            session._record_output(chunk)
        except Exception:
            pass
        rest += chunk
    return bytes(rest)


def _show_raw(data: bytes, write) -> None:
    if data:
        try:
            write(data)
        except OSError:
            pass


def _redraw(master_fd: int) -> None:
    """Ask the pty's foreground program to repaint (it gets SIGWINCH)."""
    try:
        os.killpg(os.tcgetpgrp(master_fd), signal.SIGWINCH)
    except OSError:
        pass


def _show(data: bytes, write, mirror) -> None:
    if not data:
        return
    try:
        write(data)
    except OSError:
        pass
    if mirror is not None:
        try:
            mirror(data.decode("utf-8", "replace"))
        except Exception:
            pass


def _raw_mode():
    from terminal_arbiter import Mode
    return Mode.RAW


def summary_for_model(result: AttachResult, command: str) -> str:
    """The output as the model and chat history should see it."""
    if result.fullscreen:
        head = f"(ran full-screen program `{command}`"
        if result.status == "done":
            head += f"; exit {result.returncode}"
        return head + ")"
    return result.output
