"""Real-REPL display mirroring for the full-screen /agents view.

The outer REPL is the single executor.  Everything the shared Rich console
prints is captured here as ANSI text, per Agent, so the /agents view can show
the *actual* REPL output instead of a semantic re-rendering.  Ownership of the
physical terminal switches between the plain CLI ("cli") and the full-screen
Agents view ("agents"); output produced while Agents Mode owns the screen is
buffered and replayed to stdout when ownership returns.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import re
import sys
import threading

# Cursor movement / erase / scroll-region sequences mean "repaint in place"
# (Rich Live, spinners, progress bars).  A line-oriented mirror cannot replay
# them, so chunks containing any are dropped entirely — the Agents view has
# its own activity indicator.
_FRAME_CONTROL_RE = re.compile(
    r"\x1b\[(?:\d*[ABCDEFGJKSTLM]|\?\d+[hl]|\d*(?:;\d*)?[Hfr])")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LIVE_STATUS_RE = re.compile(
    r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+(?:Thinking|Writing)…\s+"
    r"\d+(?:\.\d+)?s(?:\s+·.*)?\s*$",
    re.IGNORECASE,
)

_MAX_LINES_PER_AGENT = 4000
_MAX_MISSED_CHUNKS = 4000


def _filter_for_mirror(text: str) -> str:
    """Keep only content a scrollback-style mirror can faithfully replay."""
    if not text:
        return ""
    if _FRAME_CONTROL_RE.search(text):
        return ""
    # Rich may emit cursor controls and the visible Live frame in separate
    # file.write() calls.  The control-code test above then cannot recognize
    # the second chunk as transient.  Never persist a standalone status frame
    # as conversation history (or replay it after leaving /agents).
    if _LIVE_STATUS_RE.fullmatch(_ANSI_RE.sub("", text)):
        return ""
    if "\r" in text:
        text = text.replace("\r\n", "\n")
        if "\r" in text:
            # A bare CR overwrites the line; keep what ends up visible.
            text = "\n".join(
                segment.split("\r")[-1] for segment in text.split("\n"))
    return text


class _AgentBuffer:
    __slots__ = ("lines", "partial")

    def __init__(self):
        self.lines: deque[str] = deque(maxlen=_MAX_LINES_PER_AGENT)
        self.partial = ""


class MirrorHub:
    """Per-Agent ANSI scrollback plus terminal-ownership arbitration."""

    def __init__(self):
        self._lock = threading.RLock()
        self._buffers: dict[str, _AgentBuffer] = {}
        self._owner = "cli"
        self._missed: list[str] = []
        # Process-wide rather than thread-local: Rich Live paints from its own
        # refresh thread.  While active, output still reaches the ordinary CLI
        # terminal but never becomes Agent history or missed-output replay.
        self._transient_output = 0
        # Recording gate: the mirror keeps only Agent conversation — output
        # produced while an agent loop is running (plus explicit write()
        # calls such as the dialogue echo). Terminal decoration (startup
        # banner, idle REPL chatter) never enters an Agent's screen.
        self._recording = 0

    # ── recording gate ─────────────────────────────────────────────────
    def start_recording(self) -> None:
        with self._lock:
            self._recording += 1

    def stop_recording(self) -> None:
        with self._lock:
            self._recording = max(0, self._recording - 1)

    @contextmanager
    def transient_output(self):
        """Route repaint-in-place UI output to the CLI only, never history."""
        with self._lock:
            self._transient_output += 1
        try:
            yield
        finally:
            with self._lock:
                self._transient_output = max(0, self._transient_output - 1)

    # ── ownership ──────────────────────────────────────────────────────
    def is_agents(self) -> bool:
        return self._owner == "agents"

    def set_owner(self, owner: str) -> None:
        pending: list[str] = []
        with self._lock:
            self._owner = "agents" if owner == "agents" else "cli"
            if self._owner == "cli" and self._missed:
                pending, self._missed = self._missed, []
        if pending:
            try:
                sys.stdout.write("".join(pending))
                sys.stdout.flush()
            except Exception:
                pass

    # ── writing ────────────────────────────────────────────────────────
    def write(self, agent_id: str, text: str) -> None:
        """Append already-filtered text to one Agent's mirror only."""
        filtered = _filter_for_mirror(str(text or ""))
        if not filtered:
            return
        with self._lock:
            self._append_locked(str(agent_id or "primary"), filtered)

    def tee_write(self, text: str, agent_id: str) -> None:
        """Route one console chunk: mirror always, stdout per ownership."""
        text = str(text or "")
        if not text:
            return
        filtered = _filter_for_mirror(text)
        with self._lock:
            if self._transient_output:
                if self._owner == "cli":
                    try:
                        sys.stdout.write(text)
                    except Exception:
                        pass
                return
            if filtered and self._recording > 0:
                self._append_locked(str(agent_id or "primary"), filtered)
            if self._owner == "cli":
                try:
                    sys.stdout.write(text)
                except Exception:
                    pass
            elif filtered and len(self._missed) < _MAX_MISSED_CHUNKS:
                self._missed.append(filtered)

    def _append_locked(self, agent_id: str, text: str) -> None:
        buffer = self._buffers.setdefault(agent_id, _AgentBuffer())
        pieces = (buffer.partial + text).split("\n")
        buffer.partial = pieces.pop()
        buffer.lines.extend(pieces)

    def forget_agent(self, agent_id: str) -> None:
        """Drop a terminated Agent's scrollback buffer so it cannot leak.

        Called from agent_loop.unregister_agent. Without this, _buffers keeps one
        _AgentBuffer per distinct agent_id for the whole process lifetime — each
        bounded (deque maxlen), but unbounded in count as sub-agents spawn and
        are fired.
        """
        with self._lock:
            self._buffers.pop(str(agent_id or ""), None)

    # ── reading ────────────────────────────────────────────────────────
    def read_lines(self, agent_id: str, limit: int | None = None) -> list[str]:
        with self._lock:
            buffer = self._buffers.get(str(agent_id or ""))
            if buffer is None:
                return []
            lines = list(buffer.lines)
            if buffer.partial:
                lines.append(buffer.partial)
        if limit is not None:
            lines = lines[-max(1, int(limit)):]
        return lines


class TeeFile:
    """stdout replacement for the shared Rich console.

    Resolves ``sys.stdout`` dynamically on every call so prompt_toolkit's
    patch_stdout (and test harnesses) keep working.
    """

    encoding = "utf-8"

    def __init__(self, target_cb, hub_ref: MirrorHub | None = None):
        self._target_cb = target_cb
        self._hub = hub_ref if hub_ref is not None else hub

    def write(self, text) -> int:
        text = str(text or "")
        try:
            target = self._target_cb() or "primary"
        except Exception:
            target = "primary"
        self._hub.tee_write(text, target)
        return len(text)

    def flush(self) -> None:
        try:
            sys.stdout.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return bool(sys.stdout.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return sys.stdout.fileno()

    def transient_output(self):
        """Context used by Rich Live/status displays backed by this file."""
        return self._hub.transient_output()


hub = MirrorHub()
