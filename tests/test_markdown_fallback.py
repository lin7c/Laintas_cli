import zlib
from types import SimpleNamespace

from agent_loop import _print_markdown_safely


class RecordingConsole:
    def __init__(self):
        self.calls = []

    def print(self, value, **kwargs):
        self.calls.append((value, kwargs))


def test_corrupt_markdown_renderer_falls_back_to_plain_text_once():
    console = RecordingConsole()

    def corrupt_renderer(_content):
        raise zlib.error("incorrect header check")

    deps = SimpleNamespace(console=console, Markdown=corrupt_renderer)

    _print_markdown_safely(deps, "```python\nprint('ok')\n```")
    _print_markdown_safely(deps, "second reply")

    assert console.calls[0] == (
        "```python\nprint('ok')\n```",
        {"markup": False, "highlight": False},
    )
    assert "installed binary may be damaged" in console.calls[1][0]
    assert console.calls[2] == (
        "second reply",
        {"markup": False, "highlight": False},
    )
    assert len(console.calls) == 3
