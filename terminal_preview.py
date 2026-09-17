"""Bounded, incremental VT screen snapshots for the terminal browser."""

import pyte


class TerminalPreview:
    def __init__(self):
        self._text = ""
        self._size = None
        self.screen = None
        self.stream = None

    def render(self, text: str, columns: int = 100, rows: int = 24) -> str:
        size = (max(1, columns), max(1, rows))
        # A rolling buffer or resized PTY needs a fresh reconstruction. In
        # the common case feed only the new bytes, retaining partial escapes.
        if self._size != size or not text.startswith(self._text):
            self.screen = pyte.Screen(*size)
            self.stream = pyte.Stream(self.screen)
            self._text = ""
            self._size = size
        self.stream.feed(text[len(self._text):].replace("\r\n", "\n").replace("\n", "\r\n"))
        self._text = text
        return "\n".join(line.rstrip() for line in self.screen.display).rstrip()
