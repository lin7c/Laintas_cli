"""Shared presentation primitives for terminal commands and interactive views.

Normal terminal tables keep their columns. Small viewports present each record
as a named value followed by its labelled fields, keeping data available without
requiring horizontal scrolling. Keyboard hints are fitted as complete actions.
"""

from __future__ import annotations

import os
import re

from rich.cells import cell_len
from rich.table import Table
from rich.text import Text


def clipped(value: str, width: int, *, ellipsis: bool = True) -> str:
    text = Text(str(value).replace("\n", " ").replace("\r", " "))
    text.truncate(max(0, width), overflow="ellipsis" if ellipsis else "crop")
    return text.plain


def fit_line(fragments, width):
    """Crop a styled line without losing the styles of its retained text."""
    out, used = [], 0
    for style, value in fragments:
        text = clipped(value, max(0, width - used))
        out.append((style, text))
        used += Text(text).cell_len
        if used >= width:
            break
    return out


def wrap_fragments(fragments, width):
    """Wrap styled text into physical terminal rows for accurate scrolling."""
    return list(iter_wrapped_fragments(fragments, width))


def iter_wrapped_fragments(fragments, width):
    """Yield rows on demand, including when a single source line is huge."""
    width = max(1, width)
    row, used, emitted = [], 0, False

    def emit(tokens):
        result, chars, current = [], [], None
        for style, char, _size in tokens:
            if chars and current != style:
                result.append((current, "".join(chars)))
                chars = []
            current = style
            chars.append(char)
        if chars:
            result.append((current, "".join(chars)))
        return result

    for style, value in fragments:
        for char in value:
            if char == "\n":
                yield emit(row)
                emitted = True
                row, used = [], 0
                continue
            size = 1 if " " <= char <= "~" else cell_len(char)
            if size > width:
                char, size = "?", 1
            while used + size > width and row:
                boundary = next((index + 1 for index in reversed(range(len(row)))
                                 if row[index][1].isspace()), len(row))
                yield emit(row[:boundary])
                emitted = True
                row = row[boundary:]
                used = sum(token[2] for token in row)
            row.append((style, char, size))
            used += size
    if row or not emitted:
        yield emit(row)


def themed_style(base):
    """Map a full-screen style to the active terminal theme without probing."""
    from prompt_toolkit.styles import Style
    import terminal_theme
    mode = "mono" if "NO_COLOR" in os.environ else terminal_theme.active()
    if mode == "dark":
        return base
    colors = {
        "#0d1117": "#ffffff", "#161b22": "#f6f8fa", "#21262d": "#d0d7de",
        "#e6edf3": "#24292f", "#f0f6fc": "#24292f", "#c9d1d9": "#24292f",
        "#8b949e": "#57606a", "#6b7d6b": "#57606a", "#30363d": "#8c959f",
        "#6e7681": "#57606a",
        "#31533a": "#8c959f", "#5b6b7e": "#57606a", "#3f4a56": "#57606a",
        "#4ade80": "#176f2c", "#3fb950": "#176f2c", "#2ea043": "#176f2c",
        "#a78bfa": "#6e40a8", "#d2a8ff": "#6e40a8", "#e3b341": "#8a5a00",
        "#fbbf24": "#8a5a00", "#60a5fa": "#0969da",
        "#f85149": "#cf222e", "#ff7b72": "#cf222e", "#102619": "#dafbe1",
        "#2d1518": "#ffebe9", "#3b275f": "#e8d9ff",
    }
    rules = []
    for name, value in base.style_rules:
        if mode == "mono":
            value = re.sub(r"(?:bg:)?#[0-9a-fA-F]{6}", "", value)
            if name in {"selected", "list.selected", "search.match", "search.match.current"}:
                value += " bold reverse"
        else:
            value = re.sub(r"#[0-9a-fA-F]{6}", lambda match: colors.get(match.group(), match.group()), value)
        rules.append((name, value))
    return Style(rules)


def slice_line(fragments, start, width):
    """Read a horizontal viewport of a styled diagram in terminal cells."""
    out, position = [], 0
    for style, value in fragments:
        shown = ""
        for char in value:
            size = cell_len(char)
            if size and position >= start + width:
                break
            if position >= start and position + size <= start + width:
                shown += char
            elif position < start < position + size:
                shown += " " * (position + size - start)
            position += size
        if shown:
            out.append((style, shown))
        if position >= start + width:
            break
    return out


def fit_hints(parts, width: int, *, key_style="class:footer.key",
              text_style="class:footer", more=("?", "More")):
    """Fit whole keyboard actions in priority order, reserving help on overflow."""
    budget = max(0, width - 1)
    parts = list(dict.fromkeys(parts))
    labels = [f"{key} {label}" for key, label in parts]
    required = sum(Text(label).cell_len for label in labels) + max(0, len(parts) - 1) * 3
    overflow = required > budget
    if overflow and more:
        parts = [part for part in parts if part[0] != more[0]]
    fitted = []
    used = 0
    reserve = Text(f" · {more[0]} {more[1]}").cell_len if overflow and more else 0
    for key, label in parts:
        size = Text(f"{key} {label}").cell_len + (3 if fitted else 0)
        # Keep the first (escape/recovery) action even in a tiny viewport.
        if used + size + reserve > budget and fitted:
            break
        if used + size > budget:
            fitted.append((key_style, clipped(key, budget, ellipsis=False)))
            used = budget
            break
        if fitted:
            fitted.append((text_style, " · "))
        fitted.extend([(key_style, key), (text_style, f" {label}")])
        used += size
    if overflow and more and used + reserve <= budget:
        if fitted:
            fitted.append((text_style, " · "))
        fitted.extend([(key_style, more[0]), (text_style, f" {more[1]}")])
    return fitted


class AdaptiveTable(Table):
    """A Rich table with a linear record view when its columns would crowd."""

    def _primary_column(self):
        names = [str(column.header).strip().casefold() for column in self.columns]
        for candidate in ("task", "command", "name", "model id", "model", "path",
                          "key", "tool", "alias", "id", "scope"):
            if candidate in names:
                return names.index(candidate)
        return next((index for index, name in enumerate(names) if name), 0)

    def __rich_console__(self, console, options):
        threshold = 96 if len(self.columns) >= 5 else 72
        if (not self.show_header or len(self.columns) < 3
                or options.max_width >= threshold or not self.rows):
            yield from super().__rich_console__(console, options)
            return

        def cell(value, style=""):
            return console.render_str(value, style=style, highlight=False) if isinstance(value, str) else value

        if self.title:
            yield cell(self.title, self.title_style or "bold")
            yield Text("")
        primary = self._primary_column()
        for row_index in range(len(self.rows)):
            if row_index:
                yield Text("")
            content = []
            for index, column in enumerate(self.columns):
                value = column._cells[row_index]
                if value is None or value == "":
                    continue
                content.append((index, column, value))
            main = next((entry for entry in content if entry[0] == primary), None)
            if main is not None:
                yield cell(main[2], "bold " + str(main[1].style or ""))
            fields = Table.grid(padding=(0, 1), expand=True)
            fields.add_column(style="dim", max_width=max(8, options.max_width // 3))
            fields.add_column(ratio=1, overflow="fold")
            for index, column, value in content:
                if index == primary:
                    continue
                header = column.header
                if not str(header).strip():
                    # Current markers, ordinals and bars still belong to the
                    # record, but do not become an invented heading.
                    fields.add_row(Text(""), cell(value, column.style or ""))
                else:
                    fields.add_row(Text(str(header)), cell(value, column.style or ""))
            yield fields
        if self.caption:
            yield cell(self.caption, self.caption_style or "dim")

