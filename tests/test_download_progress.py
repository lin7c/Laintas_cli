"""Download rendering and transport contracts across the shared entry points."""
import io
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

import append_input
import download_progress as dp
import release
import updater


class Response:
    def __init__(self, chunks=(b"abc", b"def"), total="6", error=None, encoding="identity"):
        self.headers = {"Content-Length": total, "Content-Encoding": encoding}
        self.chunks, self.error = chunks, error
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self.chunks
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


def captured_console(terminal=False, width=80):
    output = io.StringIO()
    return Console(file=output, force_terminal=terminal, width=width), output


@pytest.mark.parametrize("download", [updater._download, release.download])
def test_both_entry_points_share_output_and_preserve_bytes(download):
    con, output = captured_console()
    response = Response()
    with mock.patch("requests.get", return_value=response):
        assert download("https://example.invalid/data", label="package.zip", console=con) == b"abcdef"
    assert response.closed
    assert output.getvalue() == "  ↓ package.zip\n  ✓ 100%  6 B · 00:00\n"
    assert "\x1b" not in output.getvalue()


def test_enterprise_download_keeps_authenticated_session():
    session = mock.Mock()
    response = Response()
    session.get.return_value = response
    con, _ = captured_console()
    assert release.download("https://example.invalid/enterprise", session=session,
                            timeout=47, console=con) == b"abcdef"
    session.get.assert_called_once_with("https://example.invalid/enterprise", timeout=47, stream=True)


@pytest.mark.parametrize("width", [12, 24, 40, 60, 80, 120])
@pytest.mark.parametrize("total", [None, 64 * 1024 * 1024])
def test_every_frame_fits_terminal_cells(width, total):
    with mock.patch.object(dp.time, "monotonic", return_value=100):
        meter = dp.DownloadMeter("包裹 [red] " * 20, total)
    meter.done = 16 * 1024 * 1024
    for now in (100.001, 101, 103, 110, 120):
        with mock.patch.object(dp.time, "monotonic", return_value=now):
            assert meter.header(width).cell_len < width
            assert meter.row(width).cell_len < width
    meter.finished = True
    assert meter.row(width).cell_len < width
    meter.failed = True
    assert meter.row(width).cell_len < width


def test_indeterminate_pulse_moves_without_inventing_percentage():
    with mock.patch.object(dp.time, "monotonic", return_value=0):
        meter = dp.DownloadMeter("chunked.zip", None)
    frames = []
    for tick in range(4):
        with mock.patch.object(dp.time, "monotonic", return_value=tick * 0.15):
            frames.append(meter.row().plain)
    assert len(set(frames)) == 4
    assert all("%" not in frame and "--:--" not in frame for frame in frames)
    assert all("B" in frame and "/s" in frame for frame in frames)


def test_response_compression_does_not_use_wire_length_as_decoded_size():
    response = Response(total="2", encoding="gzip")
    con, _ = captured_console()
    assert dp.stream_download(response, console=con) == b"abcdef"
    assert response.closed


@pytest.mark.parametrize("error", [None, RuntimeError("connection lost"), KeyboardInterrupt()])
def test_tty_closes_and_animation_stops_on_success_failure_and_interrupt(error):
    proxy = type("StdoutProxy", (), {"__module__": "prompt_toolkit.patch_stdout"})
    con = SimpleNamespace(is_terminal=True, file=proxy(), print=lambda _: None)
    response = Response(error=error)
    read_fd, write_fd = os.pipe()
    try:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm-256color"}):
            if error is None:
                assert dp.stream_download(response, console=con,
                    tty_open=lambda: (write_fd, 80)) == b"abcdef"
            else:
                with pytest.raises(type(error)):
                    dp.stream_download(response, console=con, tty_open=lambda: (write_fd, 80))
        with pytest.raises(OSError):
            os.fstat(write_fd)
        output = os.read(read_fd, 65536).decode()
        assert "\r\x1b[2K" in output  # Erase stale characters on every frame.
        assert "38;" not in output  # NO_COLOR still allows cursor refresh.
        expected = "✓ 100%" if error is None else "○ Cancelled" if isinstance(error, KeyboardInterrupt) else "✗ Failed"
        assert expected in output
        assert not any(t.name == "laintas-download-meter" for t in threading.enumerate())
        assert response.closed
    finally:
        os.close(read_fd)
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_short_response_never_reports_success():
    response = Response(total="9")
    con, output = captured_console()
    with pytest.raises(RuntimeError, match="incomplete"):
        dp.stream_download(response, console=con)
    assert response.closed
    assert "✗ Failed" in output.getvalue()
    assert "100%" not in output.getvalue()


def test_append_input_owns_the_live_region():
    con, output = captured_console()
    ui = mock.Mock()
    @contextmanager
    def status(renderer, **kwargs):
        assert renderer().cell_len < con.width
        assert kwargs["label"] == "Downloading…"
        yield
    ui.status.side_effect = status
    with mock.patch.object(append_input, "current", return_value=ui), \
            mock.patch.object(dp, "Live", side_effect=AssertionError("second Live")):
        assert dp.stream_download(Response(), console=con) == b"abcdef"
    assert "100%" in output.getvalue()


def test_dumb_terminal_does_not_open_tty_or_start_animation():
    con, output = captured_console(terminal=True)
    with mock.patch.dict(os.environ, {"TERM": "dumb"}), \
            mock.patch.object(dp, "Live", side_effect=AssertionError("animation on dumb terminal")):
        assert dp.stream_download(Response(), console=con,
                                  tty_open=mock.Mock(side_effect=AssertionError("TTY opened"))) == b"abcdef"
    assert "100%" in output.getvalue()
    assert "\r" not in output.getvalue()
