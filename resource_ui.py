"""Terminal-native views for agent evidence, documents, and operations.

The module owns one prompt_toolkit Application for an entire browsing session.
It supports responsive split/single-pane layouts, fuzzy search, lazy details,
in-place resource actions, live refresh, status feedback, and styled content.
Command handlers provide data and behavior; they do not build terminal layouts.
The presentation layer deliberately supports three product-specific idioms:
``timeline`` for AI/tool evidence, ``document`` for long-form knowledge, and
``operations`` for mutable runtime resources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shutil
import threading
import time
from typing import Any, Callable, Iterable, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer, Dimension, DynamicContainer, HSplit, Layout, VSplit,
    Window)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth


_MARKUP_RE = re.compile(r"\[/?[^\]]+\]")


def plain(value: Any) -> str:
    """Remove Rich markup and control characters from one display value."""
    text = _MARKUP_RE.sub("", str(value or ""))
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)


def fuzzy_match(text: str, query: str) -> bool:
    """Fast subsequence match used by every resource screen."""
    query = query.casefold().strip()
    if not query:
        return True
    cursor = iter(text.casefold())
    return all(any(char == candidate for candidate in cursor) for char in query)


def display_width(value: Any) -> int:
    return sum(max(0, get_cwidth(char)) for char in str(value or ""))


def truncate_display(value: Any, width: int, suffix: str = "") -> str:
    """Clamp text by terminal cell width, including CJK/full-width glyphs."""
    text = str(value or "")
    width = max(0, int(width))
    if display_width(text) <= width:
        return text
    suffix_width = display_width(suffix)
    budget = max(0, width - suffix_width)
    out = []
    used = 0
    for char in text:
        char_width = max(0, get_cwidth(char))
        if used + char_width > budget:
            break
        out.append(char)
        used += char_width
    return "".join(out) + (suffix if width >= suffix_width else "")


@dataclass(frozen=True)
class UILine:
    text: str
    style: str = "class:detail"
    level: int = 1


@dataclass
class UIDetail:
    title: str
    subtitle: str = ""
    lines: list[UILine] = field(default_factory=list)
    start_line: int = 0
    kind: str = ""

    @classmethod
    def text(cls, title: str, content: Any, subtitle: str = "",
             style: str = "class:detail") -> "UIDetail":
        lines = str(content if content is not None else "(empty)").splitlines()
        return cls(title=title, subtitle=subtitle,
                   lines=[UILine(line, style) for line in lines] or [UILine("(empty)")])


@dataclass
class UIItem:
    key: str
    title: str
    subtitle: str = ""
    status: str = ""
    status_style: str = "class:muted"
    badge: str = ""
    payload: Any = None
    search_text: str = ""
    ordinal: str = ""

    def haystack(self) -> str:
        return " ".join((self.title, self.subtitle, self.status,
                         self.badge, self.search_text))


@dataclass(frozen=True)
class UIAction:
    key: str
    name: str
    label: str
    handler: Optional[Callable[[Optional[UIItem]], "UIActionResult"]] = None
    style: str = "class:accent"
    allow_empty: bool = False


@dataclass
class UIActionResult:
    message: str = ""
    message_style: str = "class:success"
    refresh: bool = False
    close: bool = False
    value: Any = None
    detail: Optional[UIDetail] = None


@dataclass(frozen=True)
class UIOutcome:
    action: str
    item: Optional[UIItem] = None
    value: Any = None


ItemLoader = Callable[[], Iterable[UIItem]]
DetailLoader = Callable[[UIItem], UIDetail]


_STYLE = Style.from_dict({
    # Reuse the main CLI's green/red/violet system. Green identifies active
    # navigation and successful work, red remains destructive/error-only, and
    # violet separates user/tool/code material without reintroducing blue.
    "root": "bg:#0d1117 #e6edf3",
    "header": "bg:#161b22 #e6edf3",
    "header.brand": "bg:#161b22 #4ade80 bold",
    "header.path": "bg:#161b22 #6e7681",
    "header.meta": "bg:#161b22 #8b949e",
    "pane.header": "bg:#161b22 #8b949e bold",
    "pane.header.focus": "bg:#161b22 #4ade80 bold",
    "border": "#30363d",
    "border.focus": "#2ea043",
    "list": "bg:#0d1117 #e6edf3",
    "list.selected": "bg:#21262d #f0f6fc",
    "list.marker": "#3fb950 bold",
    "list.title": "#e6edf3",
    "list.title.selected": "#f0f6fc bold",
    "list.subtitle": "#8b949e",
    "list.badge": "#a78bfa bold",
    "muted": "#8b949e",
    "accent": "#3fb950 bold",
    "success": "#4ade80",
    "warning": "#e3b341",
    "error": "#f85149 bold",
    "detail": "#c9d1d9",
    "detail.title": "#f0f6fc bold",
    "detail.subtitle": "#8b949e",
    "detail.add": "#4ade80 bg:#102619",
    "detail.delete": "#ff7b72 bg:#2d1518",
    "detail.heading": "#4ade80 bold",
    "detail.code": "#d2a8ff",
    "timeline.rail": "#31533a",
    "timeline.user": "#d2a8ff bold",
    "timeline.ai": "#4ade80 bold",
    "timeline.tool": "#a78bfa bold",
    "search": "bg:#161b22 #f0f6fc",
    "search.prompt": "bg:#161b22 #4ade80 bold",
    "search.match": "bg:#3b275f #f0f6fc bold",
    "search.match.current": "bg:#238636 #ffffff bold",
    "footer": "bg:#161b22 #8b949e",
    "footer.key": "bg:#161b22 #e6edf3 bold",
    "help": "bg:#161b22 #c9d1d9",
    "help.heading": "bg:#161b22 #4ade80 bold",
})

_PRESENTATIONS = frozenset({"timeline", "document", "operations"})
_PANE_LABELS = {
    "timeline": ("CONVERSATIONS", "EVIDENCE STREAM"),
    "document": ("LIBRARY", "DOCUMENT"),
    "operations": ("RESOURCES", "INSPECTOR"),
}


class _MouseControl(FormattedTextControl):
    """Formatted control with a small version-compatible mouse hook."""

    def __init__(self, *args, mouse_callback=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._mouse_callback = mouse_callback

    def mouse_handler(self, mouse_event):
        if self._mouse_callback is not None:
            return self._mouse_callback(mouse_event)
        return NotImplemented


class ResourceBrowser:
    """Responsive list/detail application with optional in-place actions."""

    def __init__(self, *, title: str, load_items: ItemLoader,
                 load_detail: Optional[DetailLoader] = None,
                 actions: Iterable[UIAction] = (),
                 primary_action: str = "view",
                 primary_label: str = "View",
                 searchable: bool = True,
                 refresh_interval: Optional[float] = None,
                 empty_message: str = "Nothing here yet.",
                 initial_key: str = "", presentation: str = "operations",
                 pane_labels: Optional[tuple[str, str]] = None,
                 input=None, output=None):
        self.title = title
        self.load_items = load_items
        self.load_detail = load_detail
        self.actions = list(actions)
        self.primary_action = primary_action
        self.primary_label = primary_label
        self.searchable = searchable
        self.refresh_interval = refresh_interval
        self.empty_message = empty_message
        self.initial_key = initial_key
        self.presentation = (presentation if presentation in _PRESENTATIONS
                             else "operations")
        self.pane_labels = pane_labels or _PANE_LABELS[self.presentation]
        self._input = input
        self._output = output

        self.items: list[UIItem] = []
        self.filtered: list[UIItem] = []
        self.selected = 0
        self.list_scroll = 0
        self.detail_scroll = 0
        self.detail: Optional[UIDetail] = None
        self.detail_key = ""
        self.detail_cache: dict[str, UIDetail] = {}
        self.mode = "list"       # narrow screens: list | detail
        self.focus = "list"      # wide screens: list | detail
        self.status = ""
        self.status_style = "class:muted"
        self.search_active = False
        self.search_scope = "list"
        self._list_query = ""
        self._detail_query = ""
        self._detail_search_key = ""
        self._detail_matches: list[tuple[int, int, int]] = []
        self._detail_match_index = -1
        self._search_return_scroll = 0
        self.help_open = False
        self._help_return_focus = "list"
        self._help_return_mode = "list"
        self._last_loaded = 0.0
        self._last_selected_key = initial_key
        self._running = False
        self._lock = threading.RLock()

        self.search = Buffer(name="resource_search", multiline=False)
        self.search.on_text_changed += self._on_search_changed
        self._build_application()

    @property
    def is_wide(self) -> bool:
        return shutil.get_terminal_size((100, 30)).columns >= 104

    def _selected_item(self) -> Optional[UIItem]:
        if not self.filtered:
            return None
        self.selected = max(0, min(self.selected, len(self.filtered) - 1))
        return self.filtered[self.selected]

    def reload(self, *, preserve: bool = True) -> None:
        with self._lock:
            selected = self._selected_item()
            selected_key = selected.key if preserve and selected else self._last_selected_key
            try:
                self.items = list(self.load_items() or ())
                self.status = ""
            except Exception as exc:
                self.items = []
                self.status = f"Could not load items: {type(exc).__name__}: {exc}"
                self.status_style = "class:error"
            self._last_loaded = time.monotonic()
            self._filter(reset=False, selected_key=selected_key)

    def _filter(self, reset: bool = False, selected_key: str = "") -> None:
        query = self._list_query if self.searchable else ""
        self.filtered = [item for item in self.items
                         if fuzzy_match(item.haystack(), query)]
        if selected_key:
            index = next((i for i, item in enumerate(self.filtered)
                          if item.key == selected_key), None)
            if index is not None:
                self.selected = index
            elif reset:
                self.selected = 0
        elif reset:
            self.selected = 0
        self.selected = max(0, min(self.selected, max(0, len(self.filtered) - 1)))
        self.list_scroll = min(self.list_scroll, self.selected)
        self._sync_detail()
        if hasattr(self, "app") and self.app.is_running:
            self.app.invalidate()

    def _on_search_changed(self, buffer: Buffer) -> None:
        """Apply the search buffer to the pane from which search was opened."""
        if self.search_scope == "detail":
            self._detail_query = buffer.text
            self._detail_search_key = self.detail_key if buffer.text else ""
            self._rebuild_detail_matches(reset=True)
        else:
            self._list_query = buffer.text
            self._filter(reset=True)

    def _rebuild_detail_matches(self, *, reset: bool) -> None:
        """Index literal, case-insensitive matches in the complete detail."""
        self._detail_matches = []
        query = self._detail_query
        if self.detail is not None and query:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            for line_index, line in enumerate(self.detail.lines):
                self._detail_matches.extend(
                    (line_index, match.start(), match.end())
                    for match in pattern.finditer(line.text))
        if not self._detail_matches:
            self._detail_match_index = -1
        elif reset or self._detail_match_index < 0:
            self._detail_match_index = 0
            self._locate_detail_match()
        else:
            self._detail_match_index = min(
                self._detail_match_index, len(self._detail_matches) - 1)
        if hasattr(self, "app") and self.app.is_running:
            self.app.invalidate()

    def _locate_detail_match(self) -> None:
        if not self._detail_matches or self._detail_match_index < 0:
            return
        line_index = self._detail_matches[self._detail_match_index][0]
        # Put the hit near the upper third instead of pinning it to the edge.
        height = max(4, shutil.get_terminal_size((100, 30)).lines - 7)
        self.detail_scroll = max(0, line_index - max(0, height // 3))

    def _jump_detail_match(self, delta: int) -> None:
        if not self._detail_matches:
            return
        self._detail_match_index = (
            self._detail_match_index + delta) % len(self._detail_matches)
        self._locate_detail_match()
        if hasattr(self, "app") and self.app.is_running:
            self.app.invalidate()

    def _detail_search_ready(self) -> bool:
        return bool(self._detail_query and self._detail_matches and
                    (self.focus == "detail" or
                     (not self.is_wide and self.mode == "detail")))

    @staticmethod
    def _highlight_fragments(text: str, base_style: str,
                             spans: list[tuple[int, int, bool]]):
        """Split one display string without changing its visible contents."""
        if not spans:
            return [(base_style, text)]
        fragments = []
        cursor = 0
        for start, end, current in spans:
            if start > cursor:
                fragments.append((base_style, text[cursor:start]))
            fragments.append(("class:search.match.current" if current
                              else "class:search.match", text[start:end]))
            cursor = end
        if cursor < len(text):
            fragments.append((base_style, text[cursor:]))
        return fragments

    def _detail_text_fragments(self, line_index: int, text: str,
                               base_style: str):
        spans = [
            (start, end, match_index == self._detail_match_index)
            for match_index, (match_line, start, end)
            in enumerate(self._detail_matches)
            if match_line == line_index
        ]
        return self._highlight_fragments(text, base_style, spans)

    def _list_text_fragments(self, text: str, base_style: str):
        """Highlight the fuzzy subsequence when it is visible in this field."""
        query = self._list_query.casefold().strip()
        if not query:
            return [(base_style, text)]
        positions = []
        start = 0
        folded = text.casefold()
        for char in query:
            found = folded.find(char, start)
            if found < 0:
                return [(base_style, text)]
            positions.append(found)
            start = found + 1
        spans = []
        for position in positions:
            if spans and position == spans[-1][1]:
                spans[-1] = (spans[-1][0], position + 1, False)
            else:
                spans.append((position, position + 1, False))
        return self._highlight_fragments(text, base_style, spans)

    def _sync_detail(self, force: bool = False) -> None:
        item = self._selected_item()
        if item is None or self.load_detail is None:
            self.detail = None
            self.detail_key = ""
            self._detail_matches = []
            self._detail_match_index = -1
            self._detail_search_key = ""
            return
        # Narrow screens do not render the detail pane while browsing. Avoid
        # expensive file/session reads until the user actually opens an item.
        if not force and not self.is_wide and self.mode != "detail":
            return
        if not force and self.detail_key == item.key and self.detail is not None:
            return
        self._last_selected_key = item.key
        if not force and item.key in self.detail_cache:
            detail = self.detail_cache[item.key]
        else:
            try:
                detail = self.load_detail(item)
            except Exception as exc:
                detail = UIDetail.text(
                    item.title,
                    f"Could not load detail: {type(exc).__name__}: {exc}")
                detail.lines[0] = UILine(detail.lines[0].text, "class:error")
            self.detail_cache[item.key] = detail
        changed_item = bool(
            self._detail_search_key and self._detail_search_key != item.key)
        self.detail = detail
        self.detail_key = item.key
        self.detail_scroll = max(0, int(detail.start_line or 0))
        if changed_item:
            self._detail_query = ""
            self._detail_search_key = ""
            self._detail_matches = []
            self._detail_match_index = -1
        elif self._detail_query:
            # Live managers can replace the detail object for the same stable
            # item. Preserve the query and rebuild offsets against new text.
            self._detail_search_key = item.key
            self._rebuild_detail_matches(reset=False)

    def _move_selection(self, delta: int) -> None:
        if not self.filtered:
            return
        self.selected = max(0, min(len(self.filtered) - 1,
                                   self.selected + delta))
        self._sync_detail()

    def _move_detail(self, delta: int) -> None:
        count = len(self.detail.lines) if self.detail else 0
        self.detail_scroll = max(0, min(max(0, count - 1),
                                        self.detail_scroll + delta))

    def _detail_anchors(self) -> list[int]:
        """Return meaningful navigation stops for the current inspector."""
        if not self.detail:
            return []
        if self.detail.kind == "diff":
            return [index for index, line in enumerate(self.detail.lines)
                    if line.style in {"class:detail.add", "class:detail.delete"}]
        return [index for index, line in enumerate(self.detail.lines)
                if line.style == "class:detail.heading"]

    def _jump_detail_anchor(self, forward: bool = True) -> None:
        anchors = self._detail_anchors()
        if not anchors:
            return
        if forward:
            target = next((line for line in anchors
                           if line > self.detail_scroll), anchors[0])
        else:
            target = next((line for line in reversed(anchors)
                           if line < self.detail_scroll), anchors[-1])
        self.detail_scroll = target

    def _list_row_height(self) -> int:
        # Runtime managers favour scan speed; transcripts and documents keep a
        # second line because their context is part of the identity.
        return 1 if self.presentation == "operations" else 2

    def _focus_pane(self, pane: str, app=None) -> None:
        """Keep logical and prompt_toolkit focus synchronized.

        Narrow layouts change visibility dynamically. Both pane windows stay
        in the layout tree (see ``_build_application``), while this guard also
        makes terminal-resize races harmless.
        """
        pane = "detail" if pane == "detail" else "list"
        self.focus = pane
        target_app = app or getattr(self, "app", None)
        target = (getattr(self, "detail_window", None) if pane == "detail"
                  else getattr(self, "list_window", None))
        if target_app is None or target is None:
            return
        try:
            target_app.layout.focus(target)
        except ValueError:
            # A resize can swap the outer responsive container in the same
            # event-loop tick. The next render repairs focus without crashing.
            target_app.invalidate()

    def _execute_action(self, action: UIAction) -> None:
        item = self._selected_item()
        if item is None and not action.allow_empty:
            return
        if action.handler is None:
            self.app.exit(result=UIOutcome(
                action.name, item, item.payload if item is not None else None))
            return
        try:
            result = action.handler(item) or UIActionResult()
        except Exception as exc:
            result = UIActionResult(
                message=f"{action.label} failed: {type(exc).__name__}: {exc}",
                message_style="class:error")
        self.status = result.message
        self.status_style = result.message_style
        if result.detail is not None:
            self.detail_cache[item.key] = result.detail
            self.detail = result.detail
            self.detail_key = item.key
            self.detail_scroll = max(0, result.detail.start_line)
            self.mode = "detail"
            self._focus_pane("detail")
        if result.refresh:
            self.detail_cache.clear()
            self.detail = None
            self.detail_key = ""
            self.reload(preserve=True)
            self.status = result.message
            self.status_style = result.message_style
        if result.close:
            self.app.exit(result=UIOutcome(action.name, item, result.value))

    def _primary(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        action = next((a for a in self.actions
                       if a.name == self.primary_action), None)
        if action is not None:
            self._execute_action(action)
            return
        if self.primary_action != "view":
            self.app.exit(result=UIOutcome(
                self.primary_action, item, item.payload))
            return
        self.mode = "detail"
        self._focus_pane("detail")
        self._sync_detail(force=True)

    def _list_fragments(self):
        width, height = shutil.get_terminal_size((100, 30))
        pane_width = max(32, int(width * 0.38)) if self.is_wide else width
        row_height = self._list_row_height()
        visible_rows = max(3, (height - 7) // row_height)
        if self.selected < self.list_scroll:
            self.list_scroll = self.selected
        if self.selected >= self.list_scroll + visible_rows:
            self.list_scroll = self.selected - visible_rows + 1
        start = max(0, self.list_scroll)
        end = min(len(self.filtered), start + visible_rows)
        fragments = []
        if not self.filtered:
            message = "No matches." if self.items else self.empty_message
            return [("class:muted", f"\n  {message}\n")]
        for index in range(start, end):
            item = self.filtered[index]
            selected = index == self.selected
            base = "class:list.selected" if selected else "class:list"
            marker = "›" if selected else " "
            badge = f"{plain(item.badge).upper()}  " if item.badge else ""
            status = f"  {item.status}" if item.status else ""
            ordinal = item.ordinal or (
                f"{len(self.filtered) - index:02d}  "
                if self.presentation == "timeline" else "")
            max_title = max(
                12, pane_width - display_width(status)
                - display_width(badge) - display_width(ordinal) - 7)
            title = truncate_display(
                plain(item.title).replace("\n", " "), max_title)
            subtitle = plain(item.subtitle).replace("\n", " ")
            fragments.extend([
                ("class:list.marker" if selected else base, f" {marker} "),
                ("class:muted", ordinal),
                ("class:list.badge" if item.badge else base, badge),
            ])
            fragments.extend(self._list_text_fragments(
                title,
                "class:list.title.selected" if selected
                else "class:list.title"))
            if self.presentation == "operations" and subtitle:
                inline_width = max(
                    0, pane_width - display_width(title) - display_width(status)
                    - display_width(badge) - 9)
                if inline_width >= 8:
                    fragments.extend([
                        ("class:muted", "  —  "),
                    ])
                    fragments.extend(self._list_text_fragments(
                        truncate_display(subtitle, inline_width),
                        "class:list.subtitle"))
            fragments.extend([
                (item.status_style if item.status else base, status),
                (base, "\n"),
            ])
            if self.presentation != "operations":
                fragments.extend([
                    ("class:timeline.rail" if self.presentation == "timeline"
                     else base,
                     "     │ " if self.presentation == "timeline" else "     "),
                ])
                fragments.extend(self._list_text_fragments(
                    truncate_display(subtitle, max(12, pane_width - 8)),
                    "class:list.subtitle"))
                fragments.append(("class:list.subtitle", "\n"))
        if end < len(self.filtered):
            fragments.append(("class:muted", f"     ↓ {len(self.filtered) - end} more\n"))
        return fragments

    def _list_mouse(self, mouse_event):
        """Make rows genuinely clickable instead of merely enabling mouse mode."""
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._move_selection(-2)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._move_selection(2)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            index = (self.list_scroll
                     + max(0, mouse_event.position.y // self._list_row_height()))
            if index < len(self.filtered):
                self.selected = index
                self.focus = "list"
                if not self.is_wide:
                    self.mode = "detail"
                    self._focus_pane("detail")
                    self._sync_detail(force=True)
                else:
                    self._sync_detail()
                self.app.invalidate()
            return None
        return NotImplemented

    def _detail_fragments(self):
        if self.help_open:
            return self._help_fragments()
        detail = self.detail
        if detail is None:
            return [("class:muted", "\n  Select an item to see its details.\n")]
        _, height = shutil.get_terminal_size((100, 30))
        body_height = max(4, height - 7)
        max_scroll = max(0, len(detail.lines) - body_height)
        self.detail_scroll = max(0, min(self.detail_scroll, max_scroll))
        end = min(len(detail.lines), self.detail_scroll + body_height)
        fragments = []
        for line_index in range(self.detail_scroll, end):
            line = detail.lines[line_index]
            if self.presentation == "timeline" and detail.kind != "diff":
                heading = line.style == "class:detail.heading"
                upper = line.text.upper()
                if heading:
                    if upper.startswith(("PROMPT", "USER")):
                        glyph, style = "●", "class:timeline.user"
                    elif upper.startswith(("AI", "ASSISTANT")):
                        glyph, style = "●", "class:timeline.ai"
                    else:
                        glyph, style = "├", "class:timeline.tool"
                    fragments.append((style, f" {glyph} "))
                    fragments.extend(self._detail_text_fragments(
                        line_index, plain(line.text), style))
                    fragments.append((style, "\n"))
                elif not line.text:
                    fragments.append(("class:timeline.rail", " │\n"))
                else:
                    fragments.append(("class:timeline.rail", " │  "))
                    base_style = line.style or "class:detail"
                    fragments.extend(self._detail_text_fragments(
                        line_index, line.text, base_style))
                    fragments.append((base_style, "\n"))
            else:
                prefix = "   " if self.presentation == "document" else " "
                base_style = line.style or "class:detail"
                fragments.append((base_style, prefix))
                fragments.extend(self._detail_text_fragments(
                    line_index, line.text, base_style))
                fragments.append((base_style, "\n"))
        if end < len(detail.lines):
            fragments.append(("class:muted",
                              f"\n  ↓ {len(detail.lines) - end} more lines"))
        return fragments

    def _help_fragments(self):
        rows = [
            ("NAVIGATION", "↑/↓ or j/k  move    g/G  first/last    PgUp/PgDn  page"),
            ("VIEW", "Enter  open/focus    Tab  switch pane    [/ ]  section"),
        ]
        if self.searchable:
            rows.append(("SEARCH", "/  find in focused pane    n/N  next/previous match"))
        action_text = "    ".join(f"{action.key}  {action.label}"
                                  for action in self.actions)
        if action_text:
            rows.append(("ACTIONS", action_text))
        rows.append(("CLOSE", "?  close help    q  close screen"))
        fragments = [("class:help.heading", " KEYBOARD\n"),
                     ("class:border", " ───────────────────────────────────────\n")]
        for heading, body in rows:
            fragments.extend([
                ("class:help.heading", f" {heading}\n"),
                ("class:help", f"   {body}\n\n"),
            ])
        return fragments

    def _detail_mouse(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._move_detail(-3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._move_detail(3)
            return None
        return NotImplemented

    def _header_fragments(self):
        count = len(self.filtered)
        total = len(self.items)
        scope = f"{count}/{total}" if count != total else str(total)
        mode = "DETAIL" if (not self.is_wide and self.mode == "detail") else "BROWSE"
        return [
            ("class:header.brand", "  LAINTAS"),
            ("class:header.path", "  /  "),
            ("class:header", plain(self.title).upper()),
            ("class:header.meta", f"    {scope}  ·  {mode}\n"),
        ]

    def _pane_header_fragments(self, pane: str):
        left, right = self.pane_labels
        label = left if pane == "list" else right
        focused = (self.focus == pane or
                   (not self.is_wide and self.mode == pane))
        style = "class:pane.header.focus" if focused else "class:pane.header"
        if pane == "list":
            suffix = f"  {len(self.filtered)}"
        elif self.help_open:
            suffix = "  SHORTCUTS"
        elif self.detail:
            suffix = "  " + truncate_display(
                plain(self.detail.title), max(12, shutil.get_terminal_size().columns // 3))
            if self._detail_query:
                current = (self._detail_match_index + 1
                           if self._detail_match_index >= 0 else 0)
                suffix += f"  ·  FIND {current}/{len(self._detail_matches)}"
        else:
            suffix = ""
        return [(style, f"  {label}{suffix}\n")]

    def _search_prompt_fragments(self):
        label = "Find in detail" if self.search_scope == "detail" else "Filter list"
        return [("class:search.prompt", f"  {label}  ")]

    def _footer_fragments(self):
        width = max(24, shutil.get_terminal_size((100, 30)).columns)
        if not self.is_wide and self.mode == "detail":
            parts = [("Esc", "Back"), ("↑↓", "Scroll"),
                     ("[/]", "Section"), ("Pg", "Page")]
            if self._detail_query:
                parts[0] = ("Esc", "Clear Find")
                parts.insert(1, ("n/N", "Match"))
            parts.extend((action.key, action.label) for action in self.actions
                         if action.name != self.primary_action)
        else:
            parts = [("↑↓", "Navigate"), ("↵", self.primary_label)]
            if self.searchable:
                parts.append(("/", "Find"))
            parts.extend((action.key, action.label) for action in self.actions
                         if action.name != self.primary_action)
            if self.is_wide:
                parts.append(("Tab", "Focus"))
                if self.focus == "detail":
                    parts.append(("[/]", "Section"))
                    if self._detail_query:
                        parts.append(("n/N", "Match"))
            parts.append(("?", "Keys"))
            parts.append(("Esc", "Close"))
        fragments = []
        if self.status:
            # A one-line footer must never wrap over the body. While feedback
            # is visible it owns the row; Esc remains discoverable.
            suffix = "  Esc Close"
            available = max(4, width - len(suffix) - 2)
            message = plain(self.status).replace("\n", " ")
            message = truncate_display(message, available, "…")
            return [
                (self.status_style, f" {message}"),
                ("class:footer", suffix),
            ]
        for idx, (key, label) in enumerate(parts):
            if idx:
                fragments.append(("class:footer", " · "))
            fragments.extend([
                ("class:footer.key", key),
                ("class:footer", f" {label}"),
            ])
        # Defensive final clamp for very small terminals or extension actions.
        used = 0
        fitted = []
        for style, value in fragments:
            remaining = width - used - 1
            if remaining <= 0:
                break
            value = truncate_display(value, remaining)
            fitted.append((style, value))
            used += display_width(value)
        fragments = fitted
        return fragments

    def _build_application(self) -> None:
        kb = KeyBindings()

        @kb.add("up", filter=Condition(lambda: not self.search_active))
        def _up(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self._move_detail(-1)
            else:
                self._move_selection(-1)

        @kb.add("down", filter=Condition(lambda: not self.search_active))
        def _down(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self._move_detail(1)
            else:
                self._move_selection(1)

        @kb.add("k", filter=Condition(lambda: not self.search_active))
        def _vim_up(event):
            _up(event)

        @kb.add("j", filter=Condition(lambda: not self.search_active))
        def _vim_down(event):
            _down(event)

        @kb.add("pageup", filter=Condition(lambda: not self.search_active))
        def _page_up(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self._move_detail(-10)
            else:
                self._move_selection(-10)

        @kb.add("pagedown", filter=Condition(lambda: not self.search_active))
        def _page_down(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self._move_detail(10)
            else:
                self._move_selection(10)

        @kb.add("home", filter=Condition(lambda: not self.search_active))
        def _home(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self.detail_scroll = 0
            else:
                self.selected = 0
                self._sync_detail()

        @kb.add("end", filter=Condition(lambda: not self.search_active))
        def _end(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self.detail_scroll = max(0, len(self.detail.lines) - 1) if self.detail else 0
            elif self.filtered:
                self.selected = len(self.filtered) - 1
                self._sync_detail()

        @kb.add("g", filter=Condition(lambda: not self.search_active))
        def _first(event):
            _home(event)

        @kb.add("G", filter=Condition(lambda: not self.search_active))
        def _last(event):
            _end(event)

        @kb.add("]", filter=Condition(lambda: not self.search_active))
        def _next_anchor(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self._jump_detail_anchor(True)

        @kb.add("[", filter=Condition(lambda: not self.search_active))
        def _previous_anchor(event):
            if self.focus == "detail" or (not self.is_wide and self.mode == "detail"):
                self._jump_detail_anchor(False)

        @kb.add("enter")
        def _enter(event):
            if self.search_active:
                self.search_active = False
                pane = (self.mode if not self.is_wide else self.focus)
                self._focus_pane(pane, event.app)
            elif not self.is_wide and self.mode == "detail":
                self.mode = "list"
                self._focus_pane("list", event.app)
            else:
                self._primary()

        @kb.add("/", filter=Condition(lambda: not self.search_active))
        def _search(event):
            if not self.searchable:
                return
            detail_scope = bool(
                not self.help_open and self.detail is not None and
                (self.focus == "detail" or
                 (not self.is_wide and self.mode == "detail")))
            self.search_scope = "detail" if detail_scope else "list"
            if detail_scope:
                self._search_return_scroll = self.detail_scroll
                value = self._detail_query
            else:
                value = self._list_query
            self.search_active = True
            self.search.text = value
            self.search.cursor_position = len(value)
            if detail_scope:
                self._rebuild_detail_matches(reset=True)
            event.app.layout.focus(self.search_window)

        @kb.add("tab", filter=Condition(lambda: not self.search_active))
        def _tab(event):
            if self.is_wide:
                self.focus = "detail" if self.focus == "list" else "list"
                event.app.layout.focus(
                    self.detail_window if self.focus == "detail"
                    else self.list_window)

        @kb.add("?")
        def _help(event):
            if self.search_active:
                return
            if not self.help_open:
                self._help_return_focus = self.focus
                self._help_return_mode = self.mode
                self.help_open = True
                if not self.is_wide:
                    self.mode = "detail"
                self._focus_pane("detail", event.app)
            else:
                self.help_open = False
                if not self.is_wide:
                    self.mode = self._help_return_mode
                self._focus_pane(self._help_return_focus, event.app)
            event.app.invalidate()

        @kb.add("escape")
        def _escape(event):
            if self.help_open:
                self.help_open = False
                if not self.is_wide:
                    self.mode = self._help_return_mode
                self._focus_pane(self._help_return_focus, event.app)
            elif self.search_active:
                restore_detail = self.search_scope == "detail"
                if self.search.text:
                    self.search.text = ""
                if restore_detail:
                    self._detail_query = ""
                    self._detail_search_key = ""
                    self._detail_matches = []
                    self._detail_match_index = -1
                    self.detail_scroll = self._search_return_scroll
                self.search_active = False
                pane = (self.mode if not self.is_wide else self.focus)
                self._focus_pane(pane, event.app)
            elif self._detail_query:
                self._detail_query = ""
                self._detail_search_key = ""
                self._detail_matches = []
                self._detail_match_index = -1
                event.app.invalidate()
            elif not self.is_wide and self.mode == "detail":
                self.mode = "list"
                self._focus_pane("list", event.app)
            else:
                event.app.exit(result=UIOutcome("cancel"))

        @kb.add("q", filter=Condition(lambda: not self.search_active))
        @kb.add("c-c")
        def _quit(event):
            event.app.exit(result=UIOutcome("cancel"))

        @kb.add("n", filter=Condition(
            lambda: not self.search_active and self._detail_search_ready()))
        def _next_match(event):
            self._jump_detail_match(1)

        @kb.add("N", filter=Condition(
            lambda: not self.search_active and self._detail_search_ready()))
        def _previous_match(event):
            self._jump_detail_match(-1)

        for action in self.actions:
            if action.key in {"enter", "escape", "q", "/", "tab"}:
                continue

            action_filter = Condition(
                lambda key=action.key: not self.search_active and not (
                    key in {"n", "N"} and self._detail_search_ready()))

            @kb.add(action.key, filter=action_filter)
            def _action(event, selected_action=action):
                self._execute_action(selected_action)

        header = Window(
            FormattedTextControl(self._header_fragments), height=1,
            style="class:header", always_hide_cursor=True)
        self.search_window = Window(
            BufferControl(
                buffer=self.search,
                input_processors=[BeforeInput(self._search_prompt_fragments)]),
            height=1, style="class:search")
        search_container = ConditionalContainer(
            self.search_window,
            filter=Condition(lambda: self.searchable and self.search_active))
        self.list_window = Window(
            _MouseControl(
                self._list_fragments, focusable=True,
                mouse_callback=self._list_mouse),
            style="class:list", always_hide_cursor=True,
            width=Dimension(weight=38))
        self.detail_window = Window(
            _MouseControl(
                self._detail_fragments, focusable=True,
                mouse_callback=self._detail_mouse),
            style="class:root", always_hide_cursor=True, wrap_lines=True,
            width=Dimension(weight=62))
        self.list_header = Window(
            FormattedTextControl(lambda: self._pane_header_fragments("list")),
            height=1, style="class:pane.header", always_hide_cursor=True)
        self.detail_header = Window(
            FormattedTextControl(lambda: self._pane_header_fragments("detail")),
            height=1, style="class:pane.header", always_hide_cursor=True)
        list_pane = HSplit([self.list_header, self.list_window])
        detail_pane = HSplit([self.detail_header, self.detail_window])
        divider = Window(char="│", width=1, style="class:border")
        split = VSplit([
            list_pane,
            divider,
            detail_pane,
        ], padding=0)
        # Keep both narrow panes in the static layout tree and only toggle
        # visibility. This lets prompt_toolkit focus a pane during the same key
        # event that changes ``mode``; a DynamicContainer here made the target
        # window temporarily absent and caused search Enter/Esc crashes.
        narrow = HSplit([
            ConditionalContainer(
                list_pane, filter=Condition(lambda: self.mode == "list")),
            ConditionalContainer(
                detail_pane, filter=Condition(lambda: self.mode == "detail")),
        ])
        body = DynamicContainer(lambda: split if self.is_wide else narrow)
        footer = Window(
            FormattedTextControl(self._footer_fragments), height=1,
            style="class:footer", always_hide_cursor=True)
        root = HSplit([header, search_container, body, footer])
        io_options = {}
        if self._input is not None:
            io_options["input"] = self._input
        if self._output is not None:
            io_options["output"] = self._output
        self.app = Application(
            layout=Layout(root, focused_element=self.list_window),
            key_bindings=kb, style=_STYLE, full_screen=True,
            # Static browsers repaint only on input/invalidation. Live managers
            # use the explicit reload task below, avoiding duplicate timers.
            mouse_support=True, refresh_interval=None,
            **io_options)

    def _pre_run(self) -> None:
        self.reload(preserve=False)
        self._running = True
        if self.refresh_interval:
            async def _refresh_loop():
                import asyncio
                while self._running and not self.app.is_done:
                    await asyncio.sleep(max(0.2, self.refresh_interval))
                    self.detail_cache.clear()
                    self.detail = None
                    self.detail_key = ""
                    self.reload(preserve=True)
                    self.app.invalidate()
            self.app.create_background_task(_refresh_loop())

    def run(self) -> UIOutcome:
        """Run one full browser session and restore terminal state on exit."""
        try:
            import laintas_cli
            laintas_cli._clear_stale_running_loop()
        except Exception:
            pass
        try:
            outcome = self.app.run(pre_run=self._pre_run)
            return outcome if isinstance(outcome, UIOutcome) else UIOutcome("cancel")
        except (KeyboardInterrupt, EOFError):
            return UIOutcome("cancel")
        finally:
            self._running = False
            try:
                import laintas_cli
                laintas_cli._clear_stale_running_loop()
            except Exception:
                pass
