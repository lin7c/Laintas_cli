"""The shared Laintas download meter; also mirrored by the bootstrap scripts.

One filename line, one moving rail, and a persistent result line.
Never install a stdout proxy or compete with the foreground append-input Live.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import sys
import threading
import time


from rich.console import Console
from rich.live import Live
from rich.text import Text

ACCENT = "#3fb950"
MUTED = "#8b949e"
TRACK = "#484f58"
INTERVAL = 0.15


def format_size(value: float) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def format_pair(done: int, total: int) -> str:
    divisor, unit = 1, "B"
    for candidate in ("KB", "MB", "GB"):
        if total < divisor * 1024:
            break
        divisor *= 1024
        unit = candidate
    digits = 0 if unit == "B" else 1
    return f"{done / divisor:.{digits}f}/{total / divisor:.{digits}f} {unit}"


def format_time(seconds: float) -> str:
    seconds = min(359999, max(0, int(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def live_hostile_stdout(console_file=None) -> bool:
    for stream in (console_file, sys.stdout):
        kind = type(stream)
        if (kind.__name__ == "StdoutProxy"
                or kind.__module__.startswith("prompt_toolkit.")
                or kind.__module__ == "repl_mirror"):
            return True
    return False


def open_tty():
    try:
        fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except (OSError, AttributeError):
        return None, 0
    width = 80
    try:
        width = os.get_terminal_size(fd).columns or width
    except OSError:
        pass
    return fd, width


def write_tty(fd: int, text: str):
    try:
        os.write(fd, text.encode("utf-8", errors="replace"))
    except OSError:
        pass


class DownloadMeter:
    def __init__(self, label: str, total: int | None):
        # Labels are data: strip terminal controls and render without markup.
        self.label = " ".join("".join(c for c in label if c.isprintable()).split())
        self.total = total
        self.done = 0
        self.started = time.monotonic()
        self.failed = False
        self.finished = False
        self.cancelled = False

    @property
    def styles(self):
        return {
            "accent": ACCENT, "muted": MUTED, "track": TRACK,
            "error": "#f85149", "foreground": "#c9d1d9", "value": "bold #e6edf3",
        }

    def header(self, width: int) -> Text:
        text = Text("  ↓ ", style=self.styles["accent"])
        text.append(self.label, style=self.styles["foreground"])
        text.truncate(max(1, width - 1), overflow="ellipsis")
        return text

    def row(self, width: int = 80) -> Text:
        elapsed = max(0.001, time.monotonic() - self.started)
        speed = self.done / elapsed
        if self.finished or self.failed or self.cancelled:
            mark, word, role = (("○", "Cancelled", "muted") if self.cancelled else
                               ("✗", "Failed", "error") if self.failed else
                               ("✓", "100%", "accent"))
            text = Text(f"  {mark}", style=self.styles[role])
            text.append(f" {word}", style=self.styles["error"] if self.failed else
                        self.styles["muted"] if self.cancelled else self.styles["foreground"])
            text.append(f"  {format_size(self.done)} · {format_time(elapsed)}", style=self.styles["muted"])
            text.truncate(max(1, width - 1), overflow="ellipsis")
            return text

        percent = f"{min(99, int(self.done * 100 / self.total)):3d}%" if self.total else None
        amount = format_pair(self.done, self.total) if self.total else format_size(self.done)
        eta = format_time((self.total - self.done) / speed) if self.total and speed else "--:--"
        fields = ([percent, amount, format_size(speed) + "/s", eta] if self.total else
                  [amount, format_size(speed) + "/s"])
        available = max(1, width - 1)
        # Keep percentage and bytes when a narrow terminal cannot fit all data.
        while len(fields) > 2 and 2 + 8 + 2 + len("  ".join(fields)) > available:
            fields.pop()
        rail_width = max(1, min(32, available - 4 - len("  ".join(fields))))
        text = Text("  ")
        if self.total:
            filled = min(rail_width, int(self.done * rail_width / self.total))
            text.append("━" * filled, style=self.styles["accent"])
            if filled < rail_width:
                text.append("╸", style=self.styles["accent"])
                text.append("─" * (rail_width - filled - 1), style=self.styles["track"])
        else:
            # A short pulse travels out and back, even while network I/O waits.
            pulse = min(4, rail_width)
            span = rail_width - pulse
            tick = int(elapsed / INTERVAL) % (2 * span) if span else 0
            start = min(tick, 2 * span - tick)
            text.append("─" * start, style=self.styles["track"])
            text.append("━" * pulse, style=self.styles["accent"])
            text.append("─" * (rail_width - pulse - start), style=self.styles["track"])
        text.append("  " + fields[0], style=self.styles["value"] if self.total else self.styles["muted"])
        if len(fields) > 1:
            text.append("  " + "  ".join(fields[1:]), style=self.styles["muted"])
        text.truncate(available, overflow="ellipsis")
        return text


def _ansi(text: Text, color: bool) -> str:
    from io import StringIO
    out = StringIO()
    con = Console(file=out, force_terminal=color, color_system="truecolor" if color else None,
                  width=max(1, text.cell_len), legacy_windows=False)
    con.print(text, end="", soft_wrap=True)
    return out.getvalue()


@contextmanager
def _display(meter, con, tty_open, tty_write):
    import append_input
    ui = append_input.current(con)
    width = getattr(con, "width", 80)
    def emit(text):
        if isinstance(con, Console):
            con.print(text)
        else:
            con.print(text.plain)
    if ui is not None:
        emit(meter.header(width))
        try:
            with ui.status(lambda: meter.row(con.width), label="Downloading…"):
                yield
        finally:
            emit(meter.row(width))
        return
    interactive = con.is_terminal and os.environ.get("TERM") != "dumb"
    fd, tty_width = (tty_open() if interactive and live_hostile_stdout(con.file)
                     else (None, 0))
    if fd is not None:
        width = tty_width or width
        stop = threading.Event()
        color = "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        def repaint():
            tty_write(fd, "\r\x1b[2K" + _ansi(meter.row(width), color))
        def animate():
            while not stop.wait(INTERVAL):
                repaint()
        worker = threading.Thread(target=animate, name="laintas-download-meter", daemon=True)
        try:
            tty_write(fd, _ansi(meter.header(width), color) + "\n")
            repaint()
            worker.start()
            yield
        finally:
            stop.set()
            if worker.ident is not None:
                worker.join()
            try:
                repaint()
                tty_write(fd, "\n")
            finally:
                os.close(fd)
        return
    emit(meter.header(width))
    try:
        if interactive and not live_hostile_stdout(con.file):
            with Live(_MeterRenderable(meter, con), console=con, refresh_per_second=1 / INTERVAL,
                      transient=True, redirect_stdout=False, redirect_stderr=False):
                yield
        else:
            yield
    finally:
        emit(meter.row(width))


class _MeterRenderable:
    def __init__(self, meter, con):
        self.meter, self.console = meter, con

    def __rich__(self):
        return self.meter.row(self.console.width)


def stream_download(response, *, label="downloading", console=None,
                    tty_open=open_tty, tty_write=write_tty) -> bytes:
    """Consume and close a requests response with one shared download display."""
    length = response.headers.get("Content-Length", "")
    # requests decodes HTTP compression; its output may exceed the wire length.
    encoded = response.headers.get("Content-Encoding", "identity").lower() != "identity"
    total = int(length) if str(length).isdigit() and int(length) > 0 and not encoded else None
    meter = DownloadMeter(label, total)
    con = console if console is not None else Console()
    buf = bytearray()
    try:
        with _display(meter, con, tty_open, tty_write):
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        buf.extend(chunk)
                        meter.done = len(buf)
                if total is not None and len(buf) != total:
                    raise RuntimeError(f"{label} download is incomplete ({len(buf)}/{total} bytes).")
                meter.finished = True
            except (KeyboardInterrupt, SystemExit):
                meter.cancelled = True
                raise
            except BaseException:
                meter.failed = True
                raise
        return bytes(buf)
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()
