"""win.screenshot / win.click / win.window speak the kernel's coordinate views.

The kernel is replaced by a recorder at `_call`: what matters here is what
the CLI asks for, not what Windows does with it.
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vision  # noqa: E402
import windows_tools  # noqa: E402
from tools import ToolCtx  # noqa: E402

_PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


def _record(monkeypatch, reply=None):
    calls = []

    def fake(op, args=None):
        calls.append((op, dict(args or {})))
        return dict(reply or {"done": op})

    monkeypatch.setattr(windows_tools, "_call", fake)
    return calls


def _invoke(fn, args, tmp_path):
    return fn(args, ToolCtx(cwd=str(tmp_path)))


def test_screenshot_is_taken_at_the_size_describe_sends(monkeypatch, tmp_path):
    """Larger, and image.describe would shrink it: coordinates in its answer
    would be in a space the kernel's view does not know."""
    calls = _record(monkeypatch, {"dataUrl": _PNG, "width": 1024,
                                  "height": 576, "view": "v3"})
    out = _invoke(windows_tools._win_screenshot, {}, tmp_path)
    assert calls[0][1]["maxEdge"] == vision.DESCRIBE_MAX_EDGE
    text = str(out)
    assert "v3" in text and "1024x576" in text


def test_full_resolution_is_not_offered_for_clicking(monkeypatch, tmp_path):
    calls = _record(monkeypatch, {"dataUrl": _PNG, "width": 3840,
                                  "height": 2160, "view": "v4"})
    out = _invoke(windows_tools._win_screenshot,
                  {"full_resolution": True}, tmp_path)
    assert "maxEdge" not in calls[0][1]
    assert "do not click" in str(out)


def test_click_forwards_the_view(monkeypatch, tmp_path):
    calls = _record(monkeypatch)
    _invoke(windows_tools._win_click, {"view": "v3", "x": 10, "y": 20}, tmp_path)
    assert calls == [("click", {"button": "left", "double": False,
                                "x": 10, "y": 20, "view": "v3"})]


def test_move_sends_only_what_was_given(monkeypatch, tmp_path):
    """Zero-filled fields moved the window to the corner."""
    calls = _record(monkeypatch)
    _invoke(windows_tools._win_window,
            {"action": "move", "handle": 7, "width": 800}, tmp_path)
    assert calls == [("window.move", {"handle": 7, "width": 800})]


def test_an_old_kernel_is_not_offered_coordinate_clicks(monkeypatch, tmp_path):
    _record(monkeypatch, {"dataUrl": _PNG, "width": 1024, "height": 576})
    out = str(_invoke(windows_tools._win_screenshot, {}, tmp_path))
    assert "too old" in out and "win.click with view" not in out
