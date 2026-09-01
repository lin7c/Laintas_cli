"""Single owner for the controlling terminal.

Why this module exists
──────────────────────
The terminal is an exclusive resource, but the CLI used to have eight places
that mutated its ``termios`` state and seven independent readers of fd 0, each
following the same broken recipe:

    old = termios.tcgetattr(fd)     # snapshot whatever is there right now
    tty.setcbreak(fd)               # ... or setraw
    ...                             # read bytes directly, one at a time
    termios.tcsetattr(fd, ..., old) # restore the snapshot

Two failure modes follow from that recipe, and both were observed in the wild:

1. **Stale snapshots.** If component B snapshots while component A holds the
   terminal in cbreak/raw, B's "restore" writes *A's* mode back — possibly long
   after A has finished, possibly from the garbage collector, on a thread that
   never owned the terminal. The terminal ends up in a mode nobody asked for
   and the component that thinks it owns it is never told.

2. **Byte stealing.** Two threads calling ``os.read(0, 1)`` split the input
   stream between them at arbitrary boundaries. A multi-byte escape sequence
   (``ESC [ A``) or a bracketed-paste wrapper (``ESC [ 2 0 0 ~``) gets torn in
   half, so one consumer sees a bare ESC and the other sees ``[A`` as literal
   text. That is what put a literal ``^C`` into the prompt buffer instead of
   delivering an interrupt.

The fix is not more locking around the old recipe — it is to stop having the
recipe in more than one place.

The three invariants
────────────────────
**1. One baseline.** ``PRISTINE`` is captured once, before anything touches the
terminal, and every mode is computed *from it* (see ``_apply``). No code path
ever restores from a snapshot taken at a nesting point, so a stale snapshot
cannot exist.

**2. One reader.** Only ``_reader_loop`` calls ``os.read`` on the terminal, and
only while a holder has asked for input. Everyone else receives parsed ``Key``
objects from a queue. Escape sequences are therefore always parsed by one
state machine that sees every byte, so they cannot be torn.

**3. Explicit, diagnosable ownership.** Taking the terminal means
``with arbiter.hold("who-i-am", Mode.CBREAK) as term:``. A second thread that
tries to take it blocks, then raises ``TerminalBusy`` naming the current owner,
rather than silently interleaving. The same thread may nest (the mode stack
pops back correctly).

Modes
─────
``COOKED``   canonical line mode: the shell's default, ISIG on.
``CBREAK``   per-key input, ISIG **on** — Ctrl+C still raises SIGINT.
``RAW``      per-key input, ISIG off. Only for full passthrough (sub-terminal
             takeover), never for a y/n prompt: a prompt that disables ISIG is
             a prompt the user cannot escape from.
``EXTERNAL`` someone else owns the terminal for the duration — a forked child
             that inherited it, or prompt_toolkit driving its own raw mode. The
             arbiter stops reading and does not fight them, but still holds the
             lock so nothing else moves in, and still restores from PRISTINE
             afterwards (which is what cleans up after a child that died in
             raw mode, or after prompt_toolkit leaves O_NONBLOCK set).
"""

from __future__ import annotations

import atexit
import codecs
import contextlib
import fcntl
import os
import queue
import select
import sys
import termios
import threading
import tty
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# tty.cfmakecbreak / cfmakeraw are 3.12+. The .deb runs on whatever Python the
# user's distro ships, so provide the same transformations for older ones.
# Note what cbreak does NOT clear: ISIG. Ctrl+C must keep raising SIGINT in
# every mode except full RAW passthrough.
if hasattr(tty, "cfmakecbreak"):
    _cfmakecbreak = tty.cfmakecbreak
    _cfmakeraw = tty.cfmakeraw
else:                                                    # pragma: no cover
    def _cfmakecbreak(mode):
        mode[3] &= ~(termios.ECHO | termios.ICANON)
        mode[6] = list(mode[6])
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0

    def _cfmakeraw(mode):
        mode[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.IGNPAR
                     | termios.PARMRK | termios.INPCK | termios.ISTRIP
                     | termios.INLCR | termios.IGNCR | termios.ICRNL
                     | termios.IXON | termios.IXANY | termios.IXOFF)
        mode[1] &= ~termios.OPOST
        mode[2] &= ~(termios.PARENB | termios.CSIZE)
        mode[2] |= termios.CS8
        mode[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK
                     | termios.ECHONL | termios.ICANON | termios.IEXTEN
                     | termios.ISIG | termios.NOFLSH | termios.TOSTOP)
        mode[6] = list(mode[6])
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0


__all__ = [
    "Mode", "Key", "TerminalBusy", "TerminalSession",
    "hold", "current_owner", "is_interactive", "reset_to_pristine",
    "get_arbiter", "TerminalArbiter",
]


class Mode(Enum):
    COOKED = "cooked"
    CBREAK = "cbreak"
    RAW = "raw"
    EXTERNAL = "external"


class TerminalBusy(RuntimeError):
    """Raised when the terminal cannot be acquired before the timeout.

    Carries the current owner's name so a hang is attributable instead of
    anonymous.
    """

    def __init__(self, requester: str, owner: str, timeout: float):
        super().__init__(
            f"{requester!r} could not acquire the terminal within {timeout:g}s; "
            f"it is held by {owner!r}")
        self.requester = requester
        self.owner = owner
        self.timeout = timeout


# ── Keys ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Key:
    """One decoded keypress.

    ``name`` is the canonical key name; ``text`` carries the payload for
    ``text`` (printable characters) and ``paste`` (a bracketed-paste block).
    """
    name: str
    text: str = ""

    @property
    def is_text(self) -> bool:
        return self.name in ("text", "paste")


# Control bytes that get a name of their own. Everything else in C0 is
# reported as ctrl-<letter> so no keypress is silently swallowed — the old
# hand-rolled readers dropped 0x03 into an "echo it as a character" fallback,
# which is precisely how Ctrl+C stopped interrupting anything.
_CONTROL_NAMES = {
    0x00: "ctrl-space", 0x01: "ctrl-a", 0x02: "ctrl-b", 0x03: "ctrl-c",
    0x04: "ctrl-d", 0x05: "ctrl-e", 0x06: "ctrl-f", 0x07: "ctrl-g",
    0x08: "backspace", 0x09: "tab", 0x0a: "enter", 0x0b: "ctrl-k",
    0x0c: "ctrl-l", 0x0d: "enter", 0x0e: "ctrl-n", 0x0f: "ctrl-o",
    0x10: "ctrl-p", 0x11: "ctrl-q", 0x12: "ctrl-r", 0x13: "ctrl-s",
    0x14: "ctrl-t", 0x15: "ctrl-u", 0x16: "ctrl-v", 0x17: "ctrl-w",
    0x18: "ctrl-x", 0x19: "ctrl-y", 0x1a: "ctrl-z", 0x1c: "ctrl-backslash",
    0x1d: "ctrl-bracket-right", 0x1e: "ctrl-caret", 0x1f: "ctrl-underscore",
    0x7f: "backspace",
}

# Final bytes of a CSI sequence mapped to key names. Unrecognised sequences
# are reported as Key("unknown") rather than discarded, so a caller that cares
# can tell "nothing happened" from "something I don't model happened".
_CSI_KEYS = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end",
    "1~": "home", "2~": "insert", "3~": "delete", "4~": "end",
    "5~": "pageup", "6~": "pagedown", "7~": "home", "8~": "end",
    "Z": "backtab",
}

_PASTE_START = "200~"
_PASTE_END = "201~"


class _KeyParser:
    """Incremental vt100 parser: bytes in, ``Key`` objects out.

    Split UTF-8 sequences are held until complete (an incremental decoder),
    and a partial escape sequence is held until either it completes or
    ``flush_escape()`` declares the pending ESC to be a bare Esc keypress.
    Nothing is ever dropped on the floor to "resynchronise".
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""       # unconsumed decoded input
        self._paste: Optional[list] = None   # accumulating a paste block

    @property
    def escape_pending(self) -> bool:
        """True when input ends mid-escape-sequence (or on a lone ESC)."""
        return self._pending.startswith("\x1b")

    def feed(self, data: bytes) -> list:
        self._pending += self._decoder.decode(data)
        return self._drain()

    def flush_escape(self) -> list:
        """Resolve a pending lone ESC into a real Esc keypress.

        Called by the reader once the inter-byte gap has elapsed with nothing
        further arriving. Only fires for a *bare* ESC: if more bytes showed up
        they are already in ``_pending`` and ``_drain`` will have consumed
        them as a sequence.
        """
        if self._pending == "\x1b":
            self._pending = ""
            return [Key("escape")]
        return []

    def _drain(self) -> list:
        keys: list = []
        while self._pending:
            ch = self._pending[0]

            if ch == "\x1b":
                consumed, key = self._parse_escape(self._pending)
                if consumed == 0:
                    return keys          # incomplete — wait for more bytes
                self._pending = self._pending[consumed:]
                if key is not None:
                    keys.append(key)
                continue

            # Inside a bracketed paste everything is literal text, including
            # control characters, until the closing marker.
            if self._paste is not None:
                self._paste.append(ch)
                self._pending = self._pending[1:]
                continue

            code = ord(ch)
            if code in _CONTROL_NAMES:
                keys.append(Key(_CONTROL_NAMES[code]))
                self._pending = self._pending[1:]
                continue

            # Printable run — batch it so a fast paste in a terminal without
            # bracketed-paste support does not become one Key per character.
            run = []
            while self._pending:
                c = self._pending[0]
                if c == "\x1b" or ord(c) in _CONTROL_NAMES:
                    break
                run.append(c)
                self._pending = self._pending[1:]
            if run:
                keys.append(Key("text", "".join(run)))
        return keys

    def _parse_escape(self, buf: str):
        """Return (bytes_consumed, Key|None); (0, None) means incomplete."""
        if len(buf) == 1:
            return 0, None               # lone ESC so far — caller may flush

        second = buf[1]
        if second == "[":
            return self._parse_csi(buf)
        if second == "O":                # SS3: application-cursor arrows
            if len(buf) < 3:
                return 0, None
            return 3, Key(_CSI_KEYS.get(buf[2], "unknown"))
        if second == "\x1b":
            return 1, Key("escape")      # ESC ESC — first one is a real Esc
        return 2, Key("alt", second)

    def _parse_csi(self, buf: str):
        idx = 2
        params = []
        while idx < len(buf):
            c = buf[idx]
            if "\x40" <= c <= "\x7e":    # final byte of the sequence
                body = "".join(params)
                final = c

                if body + final == _PASTE_START:
                    self._paste = []
                    return idx + 1, None
                if body + final == _PASTE_END:
                    text = "".join(self._paste or [])
                    self._paste = None
                    return idx + 1, Key("paste", text)

                name = (_CSI_KEYS.get(body + final)
                        or _CSI_KEYS.get(final)
                        or "unknown")
                return idx + 1, Key(name)
            params.append(c)
            idx += 1
        return 0, None                   # incomplete sequence


# ── Arbiter ─────────────────────────────────────────────────────────────

@dataclass
class _Holder:
    owner: str
    mode: Mode
    thread_id: int
    depth: int = 1
    inbox: queue.Queue = field(default_factory=queue.Queue)
    # Byte passthrough: a holder forwarding input verbatim to a child pty
    # must receive the original bytes, not parsed keys — re-serialising an
    # escape sequence from a Key would corrupt whatever the child expects.
    # It still goes through the arbiter so exclusivity and restore hold.
    raw_bytes: bool = False


class TerminalSession:
    """Handle given to whoever currently holds the terminal.

    Reads come from the arbiter's single reader thread, so a session can never
    steal bytes from another consumer — there is no other consumer.
    """

    def __init__(self, arbiter: "TerminalArbiter", holder: Optional[_Holder],
                 interactive: bool):
        self._arbiter = arbiter
        self._holder = holder
        self._interactive = interactive

    @property
    def interactive(self) -> bool:
        return self._interactive

    def read_key(self, timeout: Optional[float] = None) -> Optional[Key]:
        """Return the next keypress, or None on timeout/EOF/non-tty."""
        if self._holder is None or self._holder.raw_bytes:
            return None
        try:
            return self._holder.inbox.get(
                timeout=timeout if timeout is not None else None)
        except queue.Empty:
            return None

    def read_bytes(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Return the next chunk of unparsed input (raw_bytes holders only).

        Returns b"" on EOF, None on timeout.
        """
        if self._holder is None or not self._holder.raw_bytes:
            return None
        try:
            return self._holder.inbox.get(
                timeout=timeout if timeout is not None else None)
        except queue.Empty:
            return None

    def read_line(self, timeout: Optional[float] = None) -> Optional[str]:
        """Collect keys into a line. Returns None on Esc/Ctrl+C/EOF.

        Editing is deliberately minimal (backspace, Ctrl+W, Ctrl+U) — this
        exists for one-off prompts, not as a readline replacement. Callers
        that need real editing should hold the terminal as EXTERNAL and let
        prompt_toolkit drive.
        """
        buf: list = []
        while True:
            key = self.read_key(timeout=timeout)
            if key is None:
                return None
            if key.name in ("escape", "ctrl-c", "ctrl-d", "eof"):
                return None
            if key.name == "enter":
                return "".join(buf)
            if key.name == "backspace":
                if buf:
                    buf.pop()
                continue
            if key.name == "ctrl-u":
                buf.clear()
                continue
            if key.name == "ctrl-w":
                while buf and buf[-1] == " ":
                    buf.pop()
                while buf and buf[-1] != " ":
                    buf.pop()
                continue
            if key.is_text:
                buf.append(key.text)

    def drain(self) -> None:
        """Discard input queued but not yet consumed."""
        if self._holder is None:
            return
        while True:
            try:
                self._holder.inbox.get_nowait()
            except queue.Empty:
                return


class TerminalArbiter:
    # How long a lone ESC waits for a continuation byte before being reported
    # as a bare Esc keypress. Only ever applied once, in the reader thread —
    # the old code duplicated this heuristic in four places with two different
    # timeouts and a "read 32 bytes and throw them away" recovery path.
    ESCAPE_GAP = 0.05

    def __init__(self, fd: Optional[int] = None):
        self._lock = threading.Condition(threading.Lock())
        self._holder: Optional[_Holder] = None
        self._mode_stack: list = []
        self._parser = _KeyParser()

        self._fd = -1
        self._pristine = None
        if fd is None:
            try:
                fd = sys.stdin.fileno()
            except (AttributeError, OSError, ValueError):
                fd = -1
        if fd is not None and fd >= 0:
            try:
                if os.isatty(fd):
                    self._pristine = termios.tcgetattr(fd)
                    self._fd = fd
            except (termios.error, OSError, ValueError):
                self._pristine = None
                self._fd = -1

        self._reader_thread: Optional[threading.Thread] = None
        self._reader_wanted = threading.Event()
        self._shutdown = threading.Event()

        # Clear modes an earlier run may have left in this terminal. A process
        # killed with os._exit, or by a signal, never writes the sequence that
        # turns mouse reporting off, and the terminal keeps reporting into
        # whatever starts next — including this process, whose startup output
        # then arrives interleaved with echoed ``^[[<35;46;1M``. Doing it here
        # is what lets an already-broken terminal heal by relaunching, rather
        # than needing the window closed.
        self._reset_terminal_modes()

    # ── introspection ───────────────────────────────────────────────

    @property
    def interactive(self) -> bool:
        return self._fd >= 0 and self._pristine is not None

    def current_owner(self) -> str:
        with self._lock:
            return self._holder.owner if self._holder else ""

    def current_mode(self) -> Optional[Mode]:
        with self._lock:
            return self._holder.mode if self._holder else None

    # ── termios ─────────────────────────────────────────────────────

    def _apply(self, mode: Mode) -> None:
        """Set the terminal to ``mode``, always computed from PRISTINE.

        This is the only tcsetattr in the process. Because every mode is
        derived from the single baseline rather than from whatever happened
        to be set a moment ago, modes cannot accumulate: leaving RAW returns
        to exactly the state the CLI started in, not to some intermediate
        another component left behind.
        """
        if not self.interactive:
            return
        attrs = list(self._pristine)
        if mode is Mode.CBREAK:
            _cfmakecbreak(attrs)
        elif mode is Mode.RAW:
            _cfmakeraw(attrs)
        # COOKED and EXTERNAL both restore the pristine attributes; EXTERNAL
        # then lets its owner (a child process, or prompt_toolkit) set
        # whatever it wants on top.
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, attrs)
        except (termios.error, OSError):
            pass
        self._clear_nonblocking()

    def _clear_nonblocking(self) -> None:
        """Drop O_NONBLOCK from the terminal fd.

        prompt_toolkit can return with O_NONBLOCK still set; a later select()
        then reports the fd readable forever while read() raises EAGAIN, which
        spins a reader loop at 100% CPU or makes it exit immediately.
        """
        if self._fd < 0:
            return
        try:
            flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
            if flags & os.O_NONBLOCK:
                fcntl.fcntl(self._fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        except OSError:
            pass

    # Terminal-side state that no tcsetattr can reach. Mouse reporting,
    # bracketed paste and cursor visibility are DEC private modes held by the
    # terminal emulator, and they outlive the process that turned them on.
    #
    # Restoring only termios is what made this invisible for so long: the
    # shell echoes again, so the terminal looks recovered, while it keeps
    # sending mouse reports to whatever runs next. Those arrive while no one
    # holds the terminal — pristine means canonical mode with echo — and the
    # line discipline prints them, which is how a screen fills with
    # ``^[[<35;46;1M``. It stayed hidden because mouse reporting was off by
    # default everywhere until the Windows build turned it on.
    _MODE_RESET = (
        "\x1b[?1000l"      # normal mouse tracking
        "\x1b[?1002l"      # button-event tracking
        "\x1b[?1003l"      # any-motion tracking: the flood
        "\x1b[?1015l"      # urxvt extended coordinates
        "\x1b[?1006l"      # SGR extended coordinates
        "\x1b[?2004l"      # bracketed paste
        "\x1b[?25h"        # cursor visible
    )

    def _reset_terminal_modes(self) -> None:
        """Turn off every mode we can leave behind in the terminal itself.

        Written to the terminal fd rather than sys.stdout: this runs on exit
        and crash paths, where stdout may be redirected, wrapped or closed.
        The alternate screen is deliberately not touched — a full-screen UI
        owns that and is entitled to be mid-render when a crash handler runs.
        """
        if self._fd < 0:
            return
        try:
            os.write(self._fd, self._MODE_RESET.encode("ascii"))
        except OSError:
            pass

    def reset_to_pristine(self) -> None:
        """Force the terminal back to its startup state.

        The last-resort cleanup for process exit and for crash handlers.
        """
        if not self.interactive:
            return
        self._reset_terminal_modes()
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._pristine)
        except (termios.error, OSError):
            pass
        self._clear_nonblocking()

    # ── reader ──────────────────────────────────────────────────────

    def _ensure_reader(self) -> None:
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="terminal-arbiter")
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        """The process's only reader of the terminal fd.

        Parks on ``_reader_wanted`` whenever the current holder does not want
        input (EXTERNAL mode, or no holder at all), which is what keeps it
        from stealing keystrokes from a forked child or from prompt_toolkit.
        """
        while not self._shutdown.is_set():
            if not self._reader_wanted.wait(timeout=0.2):
                continue
            if self._fd < 0:
                self._reader_wanted.clear()
                continue

            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except (OSError, ValueError, select.error):
                self._reader_wanted.clear()
                continue

            if not ready:
                # Quiet gap: a lone ESC that has been waiting is a real Esc.
                if self._parser.escape_pending:
                    self._dispatch(self._parser.flush_escape())
                continue

            try:
                data = os.read(self._fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                self._reader_wanted.clear()
                continue

            raw = self._raw_bytes_holder()

            if not data:                       # EOF
                self._dispatch([b""] if raw else [Key("eof")])
                self._reader_wanted.clear()
                continue

            self._dispatch([data] if raw else self._parser.feed(data))

    def _raw_bytes_holder(self) -> bool:
        with self._lock:
            return self._holder is not None and self._holder.raw_bytes

    def _dispatch(self, items: list) -> None:
        if not items:
            return
        with self._lock:
            holder = self._holder
            if holder is None:
                return          # nobody is listening; drop rather than buffer
            inbox = holder.inbox
        for item in items:
            inbox.put(item)

    # ── ownership ───────────────────────────────────────────────────

    @contextlib.contextmanager
    def hold(self, owner: str, mode: Mode = Mode.CBREAK, *,
             timeout: float = 5.0, raw_bytes: bool = False):
        """Take exclusive ownership of the terminal for the block's duration.

        Re-entrant for the *same* thread (the mode stack pops back correctly).
        A different thread blocks up to ``timeout`` and then raises
        ``TerminalBusy`` naming the holder, so a stuck component is reported
        instead of deadlocking silently.

        ``raw_bytes`` delivers unparsed input via ``read_bytes()`` instead of
        parsed keys — for forwarding input verbatim to a child pty.
        """
        if not self.interactive:
            yield TerminalSession(self, None, interactive=False)
            return

        me = threading.get_ident()
        nested = False

        with self._lock:
            if self._holder is not None and self._holder.thread_id == me:
                nested = True
                holder = self._holder
                self._mode_stack.append((holder.mode, holder.raw_bytes))
                holder.depth += 1
                holder.mode = mode
                holder.raw_bytes = raw_bytes
            else:
                if not self._lock.wait_for(
                        lambda: self._holder is None, timeout=timeout):
                    raise TerminalBusy(owner, self._holder.owner
                                       if self._holder else "?", timeout)
                holder = _Holder(owner=owner, mode=mode, thread_id=me,
                                 raw_bytes=raw_bytes)
                self._holder = holder

        self._apply(mode)
        self._set_reader_wanted(mode)
        self._ensure_reader()

        try:
            yield TerminalSession(self, holder, interactive=True)
        finally:
            with self._lock:
                if nested:
                    holder.depth -= 1
                    holder.mode, holder.raw_bytes = (
                        self._mode_stack.pop() if self._mode_stack
                        else (Mode.COOKED, False))
                    restore_mode = holder.mode
                else:
                    self._holder = None
                    restore_mode = Mode.COOKED
                    # A fresh parser per handover: leftover half-parsed input
                    # from the previous owner must never leak into the next.
                    self._parser = _KeyParser()
                    self._lock.notify_all()
            self._set_reader_wanted(restore_mode if nested else None)
            self._apply(restore_mode)

    def _set_reader_wanted(self, mode: Optional[Mode]) -> None:
        """Run the reader only when the holder actually wants keys.

        EXTERNAL means somebody else owns the input stream — a forked child on
        the inherited terminal, or prompt_toolkit's own event loop. Reading
        there would take bytes out of their mouths.
        """
        if mode in (Mode.CBREAK, Mode.RAW, Mode.COOKED):
            self._reader_wanted.set()
        else:
            self._reader_wanted.clear()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._reader_wanted.clear()
        self.reset_to_pristine()


# ── module-level singleton ──────────────────────────────────────────────

_arbiter: Optional[TerminalArbiter] = None
_arbiter_lock = threading.Lock()


def get_arbiter() -> TerminalArbiter:
    """The process-wide arbiter.

    Constructed on first use, which must happen before anything else touches
    the terminal — that first construction is what captures PRISTINE.
    """
    global _arbiter
    with _arbiter_lock:
        if _arbiter is None:
            _arbiter = TerminalArbiter()
            # Backstop for every exit path that is not an explicit shutdown:
            # an uncaught exception, a bare sys.exit deep in a command, a
            # library calling exit(). Any of those can unwind while a holder
            # has the terminal in raw mode, and the user is left with a shell
            # that does not echo. atexit cannot cover os._exit or a fatal
            # signal — those are handled at their own call sites.
            atexit.register(_arbiter.reset_to_pristine)
        return _arbiter


def hold(owner: str, mode: Mode = Mode.CBREAK, *, timeout: float = 5.0,
         raw_bytes: bool = False):
    return get_arbiter().hold(owner, mode, timeout=timeout,
                              raw_bytes=raw_bytes)


def current_owner() -> str:
    return get_arbiter().current_owner()


def is_interactive() -> bool:
    return get_arbiter().interactive


def reset_to_pristine() -> None:
    get_arbiter().reset_to_pristine()
