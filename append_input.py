"""One foreground Live region for status and supplementary input."""

from contextlib import contextmanager, nullcontext
import threading
import time

from rich.console import Console, Group
from rich.control import Control
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

import symbols
import transcript_view


class AppendConsole(Console):
    """Hold ordinary output until the connected append region closes."""

    def print(self, *objects, **kwargs):
        ui = current(self)
        # Rich refreshes by printing a Control. These must reach Live even
        # while conversation/tool output is held back.
        repaint = bool(objects) and all(isinstance(obj, Control) for obj in objects)
        if ui is not None and not repaint and ui.defer(objects, kwargs):
            return
        if ui is not None and not repaint:
            return ui.print_output(objects, kwargs)
        transient = getattr(self.file, "transient_output", None)
        with (transient() if ui is not None and repaint and callable(transient)
              else nullcontext()):
            return super().print(*objects, **kwargs)


_current = None


def current(console=None):
    ui = _current
    return ui if ui is not None and (console is None or ui.console is console) else None


class AppendInput:
    def __init__(self, console, labels):
        self.console = console
        self.labels = labels
        self._lock = threading.RLock()
        self._display_lock = threading.RLock()
        self.draft = ""
        self.pending = []
        # Keep this turn's submitted branches even after the AI consumes
        # them. Pending remains separate so close() never resubmits history.
        self.submitted = []
        self._deferred = []
        self._renderer = None
        self._label = "Thinking…"
        self._t0 = time.monotonic()
        self._thinking_t0 = self._t0
        self._paused = False
        self._live = None
        self._spinner = Spinner("dots", style="#3fb950")
        self._spinner.frames = list(symbols.SPINNER_RELAY)
        self._spinner.interval = symbols.SPINNER_INTERVAL_MS

    def start(self):
        global _current
        if _current is not None or not self.console.is_interactive:
            return False
        _current = self
        try:
            self.resume()
        except Exception:
            try:
                self.pause()
            except Exception:
                pass
            _current = None
            raise
        return True

    def __rich__(self):
        with self._lock:
            entries = [*self.submitted]
            if self.draft:
                entries.append(self.draft)
            renderer, label, t0 = self._renderer, self._label, self._t0
            thinking_t0 = self._thinking_t0
        if renderer is not None and not entries:
            return renderer() if callable(renderer) else renderer
        width = max(1, (self.console.width or 80) - 1)
        model, mode = self.labels()
        # While the user is composing, tool execution details yield their
        # space to the connected append branches. All rows are one Rich Group.
        if entries:
            label = "Writing…" if label == "Writing…" else "Thinking…"
            t0 = thinking_t0
        header = ("─╮ " if entries else "") + label
        header += f" {time.monotonic() - t0:.1f}s · {model or '—'} · {mode}"
        self._spinner.text = Text(
            transcript_view.crop_cells(header, max(1, width - 3)), style="#8b949e")
        if not entries:
            return self._spinner
        # Newlines and tabs from a paste stay in the submitted message, but
        # never create extra rows or move the connector in the live display.
        # Keep the newest draft and bottom connector on screen when the tree
        # grows taller than the terminal; never let Live replace it with its
        # generic overflow ellipsis.
        capacity = max(2, self.console.size.height - 2)
        rows = []
        if len(entries) > capacity:
            hidden = len(entries) - capacity + 1
            rows.append(f"    ├─ … {hidden} earlier append")
            entries = entries[hidden:]
        for index, content in enumerate(entries):
            branch = "╰" if index == len(entries) - 1 else "├"
            prefix = f"    {branch}─ append "
            preview = " ".join(content.split())
            row = transcript_view.crop_cells(prefix, width)
            row += transcript_view.crop_cells(preview, max(0, width - len(prefix)), middle=True)
            rows.append(row)
        return Group(self._spinner, *(
            Text(transcript_view.crop_cells(row, width), style="#8b949e", no_wrap=True)
            for row in rows))

    def set_draft(self, text):
        with self._lock:
            self.draft = text
        if not text:
            self.flush()

    def draft_text(self):
        with self._lock:
            return self.draft

    def submit(self, target_queue, text):
        # Queue admission and visible acknowledgement are atomic relative to
        # consume(), including a submission at the end of a provider call.
        with self._lock:
            target_queue.put_nowait(text)
            self.pending.append(text)
            self.submitted.append(text)
            self.draft = ""

    def consume(self, messages):
        with self._lock:
            for message in messages:
                if message in self.pending:
                    self.pending.remove(message)
        self.flush()

    def defer(self, objects, kwargs):
        with self._lock:
            if self._paused or not (self.draft or self.submitted):
                return False
            self._deferred.append((objects, dict(kwargs)))
            return True

    def flush(self, force=False):
        with self._lock:
            if not force and (self.draft or self.submitted):
                return
            output, self._deferred = self._deferred, []
        for objects, kwargs in output:
            self.console.print(*objects, **kwargs)

    def print_output(self, objects, kwargs):
        # A regular Rich print includes Live cursor controls in its chunk;
        # the scrollback mirror must discard such chunks. Release the live
        # frame around accepted output so the result is recorded normally.
        with self._display_lock:
            resume = self._live is not None
            if resume:
                self.pause()
            try:
                return Console.print(self.console, *objects, **kwargs)
            finally:
                if resume:
                    self.resume()

    @contextmanager
    def status(self, renderer=None, label="Thinking…", started=None):
        with self._lock:
            saved = self._renderer, self._label, self._t0
            self._renderer, self._label = renderer, label
            self._t0 = started if started is not None else time.monotonic()
            if label in ("Thinking…", "Writing…"):
                self._thinking_t0 = self._t0
        try:
            yield self
        finally:
            with self._lock:
                self._renderer, self._label, self._t0 = saved

    def pause(self):
        with self._display_lock:
            with self._lock:
                self._paused = True
            live, self._live = self._live, None
            if live is not None:
                worker = live._refresh_thread
                live.stop()
                if worker is not None and worker is not threading.current_thread():
                    worker.join(timeout=1.5)

    def resume(self):
        with self._display_lock:
            if self._live is not None:
                return
            with self._lock:
                self._paused = False
            self._live = Live(
                self, console=self.console, refresh_per_second=10,
                transient=True, redirect_stdout=False, redirect_stderr=False)
            self._live.start(refresh=True)

    def close(self):
        global _current
        try:
            self.pause()
        finally:
            if _current is self:
                _current = None
            self.flush(force=True)
        with self._lock:
            # Put any unconsumed input back in the next normal prompt rather
            # than losing it on interrupt / an iteration limit / shutdown.
            return "\n".join([*self.pending, self.draft] if self.draft else self.pending)
