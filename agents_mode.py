"""Full-screen multi-Agent view: roster, styled transcript, and a prompt.

Layout, left to right and top to bottom::

    ┌ roster ────┐│ name · title                     model  ● deployed
    │ ▌● worker  ││ ─────────────────────────────────────────────────
    │    Writing ││ › the task                                      ┃
    │    glm · … ││ ● read  src/app.py  42L  ▸                      ┃
    │            ││ ● The answer …                                  │
    │            ││ L» Thinking… 12.4s · glm-5.3 · 3.1k tokens      │
    │            ││ ╭───────────────────────────────────────────────╮
    │            ││ │ › message worker…                             │
    │            ││ ╰───────────────────────────────────────────────╯
    └────────────┘│ enter send · @name route · ctrl+o expand · esc

The roster is shown from 96 columns up and hidden below (Tab shows it as an
overlay). Under the transcript are only the prompt and its key row.

The transcript is rendered by ``agents_transcript`` into exact screen rows,
so scrolling, the scrollbar, mouse selection and click-to-expand all work on
the same row list the person sees.

While the Agent in focus works, the CLI's own status row —
``L› Thinking… 12.4s · model`` with the green highlight sweeping across the
verb — follows its latest output, exactly where the plain CLI paints it. The
frames and the shimmer come from ``agent_loop._thinking_spinner_frame`` /
``_shimmer_segments``, so the two cannot drift apart.
"""

from __future__ import annotations

import base64
from collections import defaultdict
import copy
import queue
import re
import shutil
import threading
import time
from typing import Callable, Optional

import symbols
from rich.console import Console
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Layout, Window, ConditionalContainer
from prompt_toolkit.layout.controls import (
    BufferControl, UIContent, UIControl)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import (
    AfterInput, BeforeInput, ConditionalProcessor)
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.utils import get_cwidth

import agent_loop
import agent_ui_events
import agents_transcript
import paths
from agents_transcript import Row, cell_width, crop as _crop_cells


#: The roster column's width, shared by its renderer and the layout so the
#: two cannot drift and start cropping at different places.
RAIL_WIDTH = 30

#: Statuses that mean "this Agent is doing something right now". They get the
#: animated relay spinner instead of a static glyph, everywhere they appear.
WORKING = ("running", "thinking", "queued", "waiting")

STATUS = {
    "running": (symbols.DOT, "class:running"),
    "thinking": (symbols.DOT_HALF, "class:thinking"),
    "queued": (symbols.DOT_DASH, "class:queued"),
    "waiting": (symbols.DOT_OPEN, "class:waiting"),
    "done": (symbols.OK, "class:done"),
    "ready": (symbols.OK, "class:done"),
    "error": (symbols.FAIL, "class:error"),
    "aborted": (symbols.FAIL, "class:error"),
    "idle": (symbols.DOT_OPEN, "class:idle"),
}


def _spinner_frame(elapsed: float) -> str:
    """The CLI's branded relay frame. Every frame is two cells, so nothing
    to the right of it ever shifts as the animation advances."""
    try:
        return agent_loop._thinking_spinner_frame(elapsed)
    except Exception:
        return symbols.SPINNER_RELAY[0]


def _shimmer_fragments(label: str, elapsed: float) -> list:
    """``(style, text)`` for a label with the CLI's moving highlight band.

    agent_loop returns Rich style strings; prompt_toolkit understands the
    same color vocabulary, so they are used verbatim rather than mapped —
    a mapping is a second place for the two renderers to drift apart.
    """
    try:
        segments = agent_loop._shimmer_segments(label, elapsed)
    except Exception:
        return [("class:thinking", label)]
    return [(f"fg:{style}" if style.startswith("#")
             else f"bold fg:{style.split()[-1]}" if style else "",
             text) for style, text in segments]

# Default md.* styles for the Agents panel, in prompt_toolkit syntax. These
# preserve the original hard-coded look and act as the fallback for any
# palette key the active markdown_theme leaves empty.
_PANEL_MD_DEFAULTS: dict[str, str] = {
    "md.h1": "bold #f0f6fc",
    "md.h2": "bold #d2a8ff",
    "md.bold": "bold #f0f6fc",
    "md.italic": "italic #c9d1d9",
    "md.code": "bg:#161b22 #ffa657",
    "md.codeblock": "bg:#161b22 #c9d1d9",
    "md.link": "underline #58a6ff",
    "md.quote": "italic #8b949e",
    "md.list": "#d2a8ff",
}


def _panel_md_styles() -> dict[str, str]:
    """Derive the panel's md.* styles from the active markdown_theme.

    Palette values are Rich-style strings; prompt_toolkit understands the same
    color/attribute vocabulary (bold/italic/#rrggbb/bg:#rrggbb), so they can
    be reused directly. Keys the palette leaves empty fall back to the
    panel's own defaults. Any failure (e.g. early import before laintas_cli
    is ready) keeps the historical defaults so the feed can never break.
    """
    styles = dict(_PANEL_MD_DEFAULTS)
    try:
        import laintas_cli
        palette = laintas_cli._load_markdown_palette(
            agent_loop.get_runtime_config("markdown_theme"))
    except Exception:
        return styles
    mapping = {
        "md.h1": palette.get("h1"), "md.h2": palette.get("h2"),
        "md.bold": palette.get("bold"), "md.italic": palette.get("italic"),
        "md.code": palette.get("code"), "md.codeblock": palette.get("code_block"),
        "md.link": palette.get("link"), "md.quote": palette.get("quote"),
    }
    for key, value in mapping.items():
        if value:  # only override when the theme actually sets this key
            styles[key] = value
    # Code blocks are drawn as a filled panel; a theme colour without a
    # background would leave them indistinguishable from prose.
    for key in ("md.codeblock", "md.code"):
        if "bg:" not in styles[key]:
            styles[key] = f"bg:#161b22 {styles[key]}"
    return styles


# ── Theme following ────────────────────────────────────────────────────
# The Agents view used to freeze a dark-only palette at import time, so a
# /theme change in the CLI around it never reached the full-screen app.
# STYLE is now a DynamicStyle resolved per redraw from the SAME sources the
# CLI body uses: runtime config (theme + markdown_theme), the terminal
# preference file (changes made in another terminal/process), and the mtime
# of ~/.laintas/markdown_theme.json (custom palette edits while running).

# Dark values below are the historical look and the base every other theme
# is derived from by hex translation.
_STYLE_BASE = {
    "root": "bg:#0d1117 #e6edf3",
    "header": "bold #4ade80",
    "header.brand": "bold #4ade80",
    "pane.title": "bold #6e7681",
    "muted": "#8b949e",
    "dim": "#484f58",
    # The relay spinner carries the CLI's accent green wherever it appears.
    "spinner": "bold #3fb950",
    # ── roster ──
    "rail.bg": "bg:#0a0e13",
    "rail.head": "bold #e6edf3",
    "rail.count": "#6e7681",
    "rail.card.selected": "bg:#161b22",
    "rail.card.hover": "bg:#10151c",
    "rail.bar": "#3fb950",
    "rail.name": "#c9d1d9",
    "rail.name.selected": "bold #f0f6fc",
    "rail.task": "#6e7681",
    "rail.meta": "#484f58",
    "rail.term": "bold #4ade80",
    "rail.arrow": "#6e7681",
    "badge": "bold #0d1117 bg:#d29922",
    "key": "#8b949e bold",
    "stream": "#6e7681",
    "running": "bold #3fb950",
    "thinking": "bold #d29922",
    "queued": "#8b949e",
    "waiting": "#d29922",
    "done": "#3fb950",
    "error": "bold #f85149",
    "idle": "#6e7681",
    "separator": "#262c36",
    "agent": "bold #a78bfa",
    "user": "bold #f0f6fc",
    "input": "bold #4ade80",
    "input.caret": "bold #3fb950",
    "approval": "bold #e3b341",
    # ── title bar ──
    "title.name": "bold #f0f6fc",
    "title.role": "#8b949e",
    # Title-bar buttons (resume / model) share one shape and one hover.
    "chip.model": "bg:#1c2128 #c9d1d9",
    "chip.resume": "bg:#1c2128 #c9d1d9",
    "chip.hover": "bg:#30363d #f0f6fc",
    "chip.warn": "bg:#2d2213 #e3b341",
    "chip.warn.hover": "bg:#30363d #e3b341",
    "chip.raw": "#d2a8ff bold",
    # ── transcript ──
    "user.bg": "bg:#161b22",
    "user.mark": "bold #3fb950",
    "user.text": "#f0f6fc",
    "assistant.mark": "#e6edf3",
    "tool.ok": "#3fb950",
    "tool.error": "#f85149",
    "tool.running": "#d29922",
    "tool.running.dim": "#5c4813",
    "tool.name": "bold #e6edf3",
    "tool.arg": "#8b949e",
    "tool.gutter": "#484f58",
    "out": "#8b949e",
    "out.lineno": "#484f58",
    "diff.add": "#3fb950",
    "diff.del": "#f85149",
    "diff.hunk": "#a371f7",
    "hint": "italic #6e7681",
    "message": "#a78bfa",
    "message.bar": "#a78bfa",
    "message.name": "bold #d2a8ff",
    "tool.row": "bg:#11161d",
    "tool.meta": "#6e7681",
    "tool.toggle": "#484f58",
    "error.text": "#ffa198",
    "md.h3": "bold #c9d1d9",
    "md.th": "bold #e6edf3",
    "md.strike": "strike #8b949e",
    "md.codelang": "bg:#161b22 italic #6e7681",
    "md.quotebar": "#30363d",
    "row.hover": "bg:#10151c",
    "selection": "bg:#264f78 #ffffff",
    "pill": "bg:#1f6feb #ffffff bold",
    "cursor": "bold #3fb950",
    "empty.title": "bold #c9d1d9",
    "scrollbar.track": "#161b22",
    "scrollbar.thumb": "#484f58",
    "scrollbar.thumb.active": "#8b949e",
    # ── profile ──
    "panel.bg": "bg:#0a0e13",
    "profile.border": "#30363d",
    "profile.name": "bold #f0f6fc",
    "profile.title": "bold #d2a8ff",
    "profile.text": "#b1bac4",
    "profile.label": "#6e7681",
    "profile.value": "#c9d1d9",
    "deploy.on": "#3fb950",
    "deploy.off": "#6e7681",
    # ── prompt ──
    "box": "#3d444d",
    "box.active": "#3fb950",
    "box.pulse": "#2ea043",
    "placeholder": "#484f58",
    "approval.border": "#d29922",
    "approval.title": "bold #e3b341",
    "button.yes": "bg:#238636 #ffffff bold",
    "button.no": "bg:#30363d #f0f6fc bold",
    "button.hover": "bg:#2ea043 #ffffff bold",
    "button.no.hover": "bg:#da3633 #ffffff bold",
    "toast.ok": "#3fb950",
    "toast.warn": "#e3b341",
    "toast.err": "#f85149",
    "toast.info": "#58a6ff",
    **_panel_md_styles(),
}

#: dark hex → light hex. Every hex in _STYLE_BASE (including the md.*
#: defaults injected by _panel_md_styles) must have an entry or keep its dark
#: value; the set below covers them all so light mode has no dark leftovers.
_LIGHT_HEX = {
    # surfaces / lines
    "#0d1117": "#ffffff", "#0a0e13": "#f6f8fa", "#161b22": "#eaeef2",
    "#10151c": "#eef1f5", "#11161d": "#eaeef2", "#21262d": "#d0d7de",
    "#1c2128": "#eaeef2", "#262c36": "#d0d7de", "#30363d": "#d0d7de",
    "#3d444d": "#8c959f",
    # foreground greys
    "#f0f6fc": "#1f2328", "#e6edf3": "#24292f", "#c9d1d9": "#24292f",
    "#b1bac4": "#57606a", "#8b949e": "#57606a", "#6e7681": "#6e7781",
    "#484f58": "#818b98",
    # greens
    "#3fb950": "#116329", "#4ade80": "#116329", "#2ea043": "#176f2c",
    "#238636": "#1a7f37", "#10261a": "#dafbe1",
    # reds
    "#f85149": "#cf222e", "#ffa198": "#a40e26", "#da3633": "#cf222e",
    # yellows
    "#e3b341": "#9a6700", "#d29922": "#9a6700", "#2d2213": "#fff8c5",
    "#5c4813": "#4d2d00",
    # blues
    "#58a6ff": "#0969da", "#1f6feb": "#0969da", "#264f78": "#0969da",
    # purples
    "#a78bfa": "#8250df", "#d2a8ff": "#8250df", "#a371f7": "#8250df",
    "#2a1f3d": "#fbefff",
    # markdown extras
    "#ffa657": "#953800",
}

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _translate_hex(value: str, table: dict[str, str]) -> str:
    return _HEX_RE.sub(
        lambda m: table.get(m.group(0).lower(), m.group(0)), value)


def _strip_hex(value: str) -> str:
    """Mono theme: remove every colour, keep attributes (bold/italic/...).

    A rule that was background-only (buttons, selection) degrades to plain
    ``reverse`` so affordances stay visible without colour.
    """
    stripped = _HEX_RE.sub("", value)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not stripped and "bg:" in value:
        return "reverse"
    return stripped


def _current_theme_name() -> str:
    """The theme the CLI body is showing right now.

    The terminal preference file wins: /theme writes it immediately, and a
    change made in ANOTHER terminal/process is only visible there. The
    in-memory runtime config is the fallback for the very first read and for
    sessions with no preference file yet.
    """
    try:
        import terminal_preferences
        name = terminal_preferences.get_ui_preferences().get("theme")
        if name:
            return str(name)
    except Exception:
        pass
    try:
        return str(agent_loop.get_runtime_config("theme") or "dark")
    except Exception:
        return "dark"


def _theme_signature():
    """Cheap fingerprint of every input the Agents palette depends on.

    Called on every animation tick (≤30/s): three stat-free config reads plus
    two os.stat calls, so following the outside world costs nothing.
    """
    md_name = ""
    custom_mtime = None
    pref_mtime = None
    try:
        md_name = str(agent_loop.get_runtime_config("markdown_theme")
                      or "default")
        if md_name == "custom":
            path = paths.LAINTAS_HOME / "markdown_theme.json"
            try:
                custom_mtime = path.stat().st_mtime
            except OSError:
                custom_mtime = None
    except Exception:
        pass
    try:
        import terminal_preferences
        try:
            pref_mtime = terminal_preferences.preference_path().stat().st_mtime
        except OSError:
            pref_mtime = None
    except Exception:
        pass
    return (_current_theme_name(), md_name, custom_mtime, pref_mtime)


_STYLE_CACHE: dict = {}
_PREF_MTIME_SEEN: list = [None]


def _build_agents_style(theme: str) -> Style:
    rules = dict(_STYLE_BASE)
    rules.update(_panel_md_styles())  # re-derived for the active markdown_theme
    if theme == "light":
        rules = {k: _translate_hex(v, _LIGHT_HEX) for k, v in rules.items()}
    elif theme == "mono":
        rules = {k: _strip_hex(v) for k, v in rules.items()}
    return Style.from_dict(rules)


def _current_style() -> Style:
    """Resolve the Agents style for the themes in effect right now."""
    # Cross-process pickup: when the preference file changed on disk, drop
    # terminal_preferences' in-memory cache so _current_theme_name() sees it.
    try:
        import terminal_preferences
        try:
            mtime = terminal_preferences.preference_path().stat().st_mtime
        except OSError:
            mtime = None
        if mtime != _PREF_MTIME_SEEN[0]:
            _PREF_MTIME_SEEN[0] = mtime
            terminal_preferences.load(refresh=True)
    except Exception:
        pass
    sig = _theme_signature()
    style = _STYLE_CACHE.get(sig)
    if style is None:
        style = _build_agents_style(sig[0] if sig[0] in ("light", "mono")
                                    else "dark")
        _STYLE_CACHE[sig] = style
    return style


#: Drop-in replacement for the old static Style. prompt_toolkit re-queries
#: the callable on every redraw, so an invalidate() after a theme change
#: repaints the whole view in the new palette.
STYLE = DynamicStyle(_current_style)


class _PaneControl(UIControl):
    """A control that paints exactly ``height`` rows of ``width`` cells.

    Every row is padded to the full width, so each cell maps to a known
    position and mouse events land where they were aimed — a click right of
    the last glyph, or on the scrollbar column, would otherwise be reported
    as a click at the start of the row.
    """

    def __init__(self, render: Callable, mouse: Optional[Callable] = None):
        self._render = render
        self._mouse = mouse

    def create_content(self, width: int, height: Optional[int]) -> UIContent:
        lines = self._render(width, height or 1)
        return UIContent(get_line=lambda index: lines[index]
                         if 0 <= index < len(lines) else [],
                         line_count=len(lines), show_cursor=False)

    def mouse_handler(self, mouse_event):
        if self._mouse is None:
            return NotImplemented
        result = self._mouse(mouse_event)
        return None if result is None else result

    def is_focusable(self) -> bool:
        return False


def _fit(fragments: list, width: int, fill: str = "") -> list:
    """Crop or pad fragments to exactly ``width`` cells."""
    out, used = [], 0
    for style, text, *rest in fragments:
        if used >= width:
            break
        piece = []
        for ch in text:
            w = max(0, get_cwidth(ch))
            if used + w > width:
                break
            piece.append(ch)
            used += w
        if piece:
            out.append((style, "".join(piece), *rest))
    if used < width:
        out.append((fill, " " * (width - used)))
    return out


def _slice_cells(text: str, start: int, end: int) -> str:
    out, col = [], 0
    for ch in text:
        w = max(1, get_cwidth(ch))
        if col >= end:
            break
        if col >= start:
            out.append(ch)
        col += w
    return "".join(out)


def _highlight(fragments: list, start: int, end: int, style: str) -> list:
    """Overlay ``style`` on cells [start, end) of a fragment row."""
    out, col = [], 0
    for fragment in fragments:
        base, text = fragment[0], fragment[1]
        for ch in text:
            w = max(1, get_cwidth(ch))
            chosen = f"{base} {style}" if start <= col < end else base
            if out and out[-1][0] == chosen:
                out[-1] = (chosen, out[-1][1] + ch)
            else:
                out.append((chosen, ch))
            col += w
    return out


def _slice_fragments(fragments: list, start: int, end: int) -> list:
    """Cells [start, end) of a fragment row."""
    out, col = [], 0
    for fragment in fragments:
        style, text = fragment[0], fragment[1]
        for ch in text:
            w = max(1, get_cwidth(ch))
            if start <= col < end:
                if out and out[-1][0] == style:
                    out[-1] = (style, out[-1][1] + ch)
                else:
                    out.append((style, ch))
            col += w
    return out


class _NullWriter:
    """File-like sink used to keep Rich from corrupting the full-screen UI."""

    encoding = "utf-8"

    @staticmethod
    def write(value):
        return len(str(value or ""))

    @staticmethod
    def flush():
        return None

    @staticmethod
    def isatty():
        return False


class AgentsModeController:
    def __init__(self, terminal_name: str, deps, session: dict,
                 external_events_cb: Optional[Callable] = None,
                 primary_submit_cb: Optional[Callable] = None,
                 existing_session=None,
                 execution_block_reason: str = "",
                 repl_submit_cb: Optional[Callable] = None,
                 mirror=None, terminal_label: Optional[Callable] = None):
        self.terminal_name = terminal_name or "term0"
        self.terminal_label = terminal_label or (lambda name: name)
        self.deps = deps
        self.session = session or {}
        self.external_events_cb = external_events_cb
        self.primary_submit_cb = primary_submit_cb
        # repl_submit_cb forwards a dialogue message to the outer REPL loop —
        # the single executor. When set, this view never runs the primary
        # itself; input always means "talk to this Agent".
        self.repl_submit_cb = repl_submit_cb
        # mirror: per-Agent ANSI scrollback of the real REPL output
        # (repl_mirror.MirrorHub). Shown on request (Ctrl+R) as the raw view.
        self.mirror = mirror
        # The backend's model list arrives through set_available_models()
        # from whoever opened the view (this module starts no threads), so
        # the profile can say whether an Agent's model is actually offered.
        self._model_ids: Optional[set] = None
        self._model_probe_state = "idle"
        self.existing_session = existing_session
        self.execution_block_reason = str(execution_block_reason or "")
        width = max(40, shutil.get_terminal_size(fallback=(100, 30)).columns)
        self._silent_console = Console(
            file=_NullWriter(), force_terminal=False, width=width)
        self._agent_consoles: dict[str, Console] = {}
        self._console_width = width
        self.app: Optional[Application] = None
        self.overlay = False
        self.rail_offset = 0
        # Scroll position per Agent, in rows from the bottom. `focus_scroll`
        # is where the view is going; `_scroll_pos` is where it is drawn, and
        # eases toward the target so a wheel notch glides instead of jumping.
        self.focus_scroll: dict[str, int] = defaultdict(int)
        self._scroll_pos: dict[str, float] = defaultdict(float)
        self._last_total: dict[str, int] = {}
        self._unseen_rows: dict[str, int] = defaultdict(int)
        self.follow: dict[str, bool] = defaultdict(lambda: True)
        self.read_seq: dict[str, int] = defaultdict(int)
        self._notice = ""
        self._notice_at = 0.0
        self._approval_lock = threading.Lock()
        self._approvals: list[dict] = []
        self._closed = threading.Event()
        self._last_agents: list = []
        self._rows_cache: dict[str, tuple] = {}
        self._drafts: dict[str, str] = defaultdict(str)
        self._input_buffer: Optional[Buffer] = None
        # Tool blocks the person expanded, per Agent; Ctrl+O expands all.
        self._expanded: dict[str, set] = defaultdict(set)
        self._expand_all = False
        # Agents whose next row-count change is a re-layout, not new output.
        self._relayout: set = set()
        # Whether the live status row is currently the transcript's tail.
        self._status_tail: dict[str, bool] = {}
        # Ctrl+R: show the REPL's raw ANSI mirror instead of the transcript.
        self._raw: dict[str, bool] = defaultdict(bool)
        # None = decide by width; True/False = the person toggled it.
        self._profile_pinned: Optional[bool] = None
        # Mouse state: what a press started, what the pointer is over, and
        # the transcript selection as (agent_id, (row, col), (row, col)).
        self._drag: Optional[tuple] = None
        self._hover: Optional[tuple] = None
        self._selection: Optional[tuple] = None
        self._viewport = ("", 0, 0, 0, 0, 1, 1)
        self._pill: Optional[tuple] = None
        self._rail_hits: dict[int, tuple] = {}
        self._title_hits: list = []
        #: Slash commands a title-bar chip asked to run after the view closes.
        #: A list, because the chips act on the SELECTED agent: switching to
        #: it first (/agent <id>) scopes /model and /resume to that agent —
        #: /resume lists get_current_agent_id()'s sessions and /model targets
        #: the current agent's terminal. Consumed by the launcher's _closed().
        self.pending_commands: list[str] = []
        self._button_hits: list = []
        # When each Agent started its current stretch of work, so the status
        # row can show an elapsed clock. Kept here rather than read off the
        # Agent because a primary has no assignment to carry one, and a clock
        # that only some Agents have is worse than none.
        self._work_since: dict[str, float] = {}
        selected = agent_loop.get_dialog_agent_for_terminal(self.terminal_name)
        if selected is not None and agent_loop.agent_deployment_terminal(selected) == self.terminal_name:
            selected = None
        self.selected_id = selected.id if selected else ""
        # Resolve a selection now: the first frame used to paint the
        # transcript before the roster had picked anyone, so the view opened
        # on "No Agents" while the roster showed one selected.
        self.agents()

    @property
    def notice(self) -> str:
        return self._notice

    @notice.setter
    def notice(self, value) -> None:
        self._notice = str(value or "")
        self._notice_at = time.monotonic()

    def _is_deployed_in_terminal(self, agent) -> bool:
        """Agent owns the persistent shell of this terminal (e.g. primary)."""
        return agent_loop.agent_deployment_terminal(agent) == self.terminal_name

    def agents(self) -> list:
        rows = [a for a in agent_loop.get_all_agents()
                if agent_loop.agent_scope_terminal(a) == self.terminal_name
                and not a.lifecycle_terminated
                and not self._is_deployed_in_terminal(a)]
        rows.sort(key=lambda a: (
            0 if a.role == "primary" else 1, a.created_at, a.id))
        self._last_agents = rows
        # Keep selected_id if it still points to a live agent, even one
        # filtered from the rail (e.g. the deployed primary). Only discover
        # a new selection when selected_id is empty or stale.
        current = (agent_loop.get_agent(self.selected_id)
                   if self.selected_id else None)
        if current is None or current.lifecycle_terminated:
            candidate = agent_loop.get_dialog_agent_for_terminal(self.terminal_name)
            if candidate is not None and self._is_deployed_in_terminal(candidate):
                candidate = None
            self.selected_id = (candidate.id if candidate
                                else (rows[0].id if rows else ""))
            if self.selected_id:
                agent_loop.set_dialog_agent_for_terminal(
                    self.terminal_name, self.selected_id)
        return rows

    def select(self, agent_id: str) -> bool:
        # Selection is a view/routing choice, never a foreground session
        # handoff. `/agent` owns that transition on the REPL thread.
        previous_id = self.selected_id
        if not agent_loop.set_dialog_agent_for_terminal(
                self.terminal_name, agent_id):
            return False
        if self._input_buffer is not None and previous_id:
            self._drafts[previous_id] = self._input_buffer.text
        self.selected_id = agent_id
        if self._input_buffer is not None:
            self._input_buffer.text = self._drafts.get(agent_id, "")
            self._input_buffer.cursor_position = len(self._input_buffer.text)
        self.overlay = False
        events = agent_ui_events.hub.agent_events(agent_id)
        if events and self.follow[agent_id]:
            self.read_seq[agent_id] = events[-1].seq
        self.invalidate()
        return True

    def _terminal_size(self):
        try:
            if self.app is not None:
                size = self.app.output.get_size()
                return size.columns, size.rows
        except Exception:
            pass
        size = shutil.get_terminal_size(fallback=(100, 30))
        return size.columns, size.lines

    # ── layout geometry ──────────────────────────────────────────────────
    #: Rows of the right column that are not transcript: title, its rule,
    #: the three-row prompt box and the footer.
    CHROME_ROWS = 1 + 1 + 3 + 1
    PANEL_WIDTH = 34

    #: The roster is shown from this many columns up — the original /agents
    #: breakpoint; below it the transcript gets the width and Tab brings the
    #: roster up as an overlay.
    RAIL_MIN_COLUMNS = 96

    def _rail_width(self) -> int:
        width, _height = self._terminal_size()
        return RAIL_WIDTH if width >= self.RAIL_MIN_COLUMNS else 0

    def _profile_visible(self) -> bool:
        width, _height = self._terminal_size()
        if self._profile_pinned is not None:
            return self._profile_pinned and width >= 100
        return width >= 150

    def _transcript_size(self) -> tuple[int, int]:
        """(width, height) of the transcript pane, scrollbar included."""
        width, height = self._terminal_size()
        rail = self._rail_width()
        pane = width - (rail + 1 if rail else 0)
        if self._profile_visible():
            pane -= self.PANEL_WIDTH + 1
        rows = height - self.CHROME_ROWS - self._approval_height()
        return max(20, pane), max(3, rows)

    def _rail_page_size(self) -> int:
        _width, height = self._terminal_size()
        # Three header rows; each card is three rows and a gap. When the
        # roster does not fit, the "↑/↓ N more" rows need room as well.
        fits = max(1, (height - 3) // 4)
        if len(self._last_agents) <= fits:
            return fits
        return max(1, (height - 5) // 4)

    def _right_width(self) -> int:
        width, _height = self._terminal_size()
        rail = self._rail_width()
        return max(30, width - (rail + 1 if rail else 0))

    def _keep_selected_visible(self) -> None:
        ids = [row.id for row in self.agents()]
        if self.selected_id not in ids:
            return
        index = ids.index(self.selected_id)
        page = self._rail_page_size()
        if index < self.rail_offset:
            self.rail_offset = index
        elif index >= self.rail_offset + page:
            self.rail_offset = index - page + 1

    def cycle_agent(self, delta: int) -> None:
        rows = self.agents()
        if not rows:
            return
        ids = [row.id for row in rows]
        index = ids.index(self.selected_id) if self.selected_id in ids else 0
        self.select(ids[(index + delta) % len(ids)])
        self._keep_selected_visible()

    def cycle_terminal(self, delta: int) -> None:
        terminals = [row.name for row in agent_loop.get_all_terminals()]
        if not terminals:
            return
        index = terminals.index(self.terminal_name) if self.terminal_name in terminals else 0
        if self._input_buffer is not None and self.selected_id:
            self._drafts[self.selected_id] = self._input_buffer.text
        self.terminal_name = terminals[(index + delta) % len(terminals)]
        candidate = agent_loop.get_dialog_agent_for_terminal(self.terminal_name)
        # The Agent deployed in that terminal is not on its roster (the plain
        # CLI already shows it), so selecting it left no card highlighted and
        # a transcript that belonged to nobody on screen.
        if candidate is not None and self._is_deployed_in_terminal(candidate):
            candidate = None
        self.selected_id = candidate.id if candidate else ""
        self.rail_offset = 0
        self.agents()  # picks the first card when nothing was chosen
        if self._input_buffer is not None:
            self._input_buffer.text = self._drafts.get(self.selected_id, "")
            self._input_buffer.cursor_position = len(self._input_buffer.text)
        self.overlay = False
        self.invalidate()

    def unread(self, agent_id: str, events=None) -> int:
        events = (agent_ui_events.hub.agent_events(agent_id)
                  if events is None else events)
        return sum(
            event.seq > self.read_seq[agent_id]
            and agent_ui_events.hub.needs_attention(event)
            for event in events)

    def _current_task(self, agent) -> str:
        active = getattr(agent, "active_assignment", None)
        if active is not None and active.task:
            return active.task
        state = getattr(agent, "state", {}) or {}
        history = getattr(agent, "assignment_history", None) or []
        previous_task = history[-1].get("task", "") if history else ""
        return str(state.get("_assignment_task") or state.get("objective")
                   or previous_task or "idle")

    def _rail_subtitle(self, agent, status: str,
                       profile: Optional[dict] = None) -> str:
        """Second roster row for an idle Agent: outcome first, then its role."""
        if status in {"error", "aborted"}:
            return str(getattr(agent, "error", "") or "failed")
        task = self._current_task(agent)
        if status in {"done", "ready"} and task != "idle":
            return f"done {symbols.BULLET} {task}"
        title = (profile or {}).get("title") or ""
        return title or task
    def _display_status(self, agent, events=None) -> str:
        """Combine authoritative runtime state with the last durable UI event."""
        status = str(getattr(agent, "status", "idle") or "idle")
        if status in {"running", "thinking", "queued", "waiting"}:
            return status
        events = (agent_ui_events.hub.agent_events(agent.id, limit=20)
                  if events is None else events[-20:])
        for event in reversed(events):
            if event.event_type in {"agent_error", "step_failed", "node_failed"}:
                return "error"
            if event.event_type in {"agent_done", "workflow_completed"}:
                return "done"
            if event.event_type in {"agent_started", "workflow_started"}:
                break
        return status

    # ── agent profile ────────────────────────────────────────────────────
    _ROLE_LABELS = {
        "pool": "employee", "deployed": "employee", "primary": "main agent",
        "subagent": "sub-agent",
    }

    def _agent_profile(self, agent) -> dict:
        """Everything the view says about who an Agent is, in one place.

        The roster, the title bar, the profile card and the side panel all
        read this, so they can never disagree about an Agent's model or
        where it is deployed.
        """
        profile = getattr(agent, "profile", None)
        title = str(getattr(profile, "title", "") or "")
        description = str(getattr(profile, "description", "") or "")
        specialist = str(getattr(profile, "specialist_role", "") or "")
        if agent.role == "primary" and title in {"", "General Agent"}:
            title = "Main agent"
            if description.startswith("General-purpose"):
                description = "Talks to you in the terminal and runs the work there."
        if specialist and specialist.casefold() not in title.casefold():
            title = f"{title} {symbols.BULLET} {specialist}" if title else specialist

        model, provider, source = "", "", ""
        try:
            model, provider = agent_loop.resolve_agent_model(agent)
        except Exception:
            pass
        deployment = agent_loop.agent_deployment_terminal(agent)
        if model:
            if getattr(agent, "base_model", "") and model == agent.base_model:
                source = "pinned at hire"
            elif deployment:
                source = f"{self.terminal_label(deployment)} override"
        else:
            try:
                model = str(agent_loop._live_status_model() or "")
            except Exception:
                model = ""
            source = "backend default"
        model = model.split("/")[-1] if "/" in model and not provider else model
        availability = None
        if model:
            if self._model_ids is not None:
                bare = model.split("/")[-1]
                availability = model in self._model_ids or bare in self._model_ids
            elif self._model_probe_state == "running":
                availability = "checking"

        home = getattr(agent, "home_terminal", None) or self.terminal_name
        if deployment:
            where = f"deployed in {self.terminal_label(deployment)}"
        elif agent.role == "subagent":
            where = f"temporary {symbols.BULLET} runs in its own shell"
        else:
            where = f"not deployed {symbols.BULLET} home {self.terminal_label(home)}"

        policy = getattr(profile, "tool_policy", None)
        allowed = getattr(policy, "allowed_tools", None)
        if allowed:
            shown = list(allowed)[:6]
            tools = ", ".join(shown) + (
                f" +{len(allowed) - len(shown)}" if len(allowed) > len(shown) else "")
        elif allowed is None:
            tools = "all tools"
        else:
            tools = "none"
        denied = list(getattr(policy, "denied_tools", None) or [])
        if denied:
            tools += f" {symbols.BULLET} denies {', '.join(denied[:3])}"
        task = self._current_task(agent)
        return {
            "id": agent.id,
            "name": str(agent.name or agent.id),
            "role": agent.role,
            "role_label": self._ROLE_LABELS.get(agent.role, agent.role),
            "title": title,
            "description": description,
            "model": model,
            "provider": provider,
            "model_source": source,
            "model_available": availability,
            "deployed": bool(deployment),
            "deployment": where,
            "tools": tools,
            "task": "" if task == "idle" else task,
            "tags": list(getattr(profile, "capability_tags", None) or []),
        }

    def begin_model_probe(self) -> None:
        """The caller is fetching the backend's model list for us."""
        if self._model_probe_state == "idle":
            self._model_probe_state = "running"
            self._rows_cache.clear()

    def set_available_models(self, models) -> None:
        """Receive the backend's model list (dicts with ``id``, or ids).

        None or an empty list means the probe failed: say nothing rather
        than claim every model is missing.
        """
        ids = set()
        for item in models or ():
            ids.add(str(item.get("id") or "") if isinstance(item, dict)
                    else str(item))
        ids.discard("")
        self._model_ids = ids or None
        self._model_probe_state = "done" if ids else "failed"
        self._rows_cache.clear()
        self.invalidate()

    # ── roster ───────────────────────────────────────────────────────────
    def rail_lines(self, width: int, height: int) -> list:
        """The roster: a header, then one three-row card per Agent."""
        agents = self.agents()
        hits: dict[int, tuple] = {}
        lines: list = []
        statuses = {agent.id: self._display_status(agent) for agent in agents}
        working = sum(statuses[a.id] in WORKING for a in agents)
        attention = sum(self.unread(a.id) > 0 for a in agents)

        # Header: the terminal (switchable when there is more than one) and
        # a census a person acts on — who is working, who wants them.
        terminals = [row.name for row in agent_loop.get_all_terminals()]
        head = [("class:rail.term", f" {self.terminal_label(self.terminal_name)}")]
        if len(terminals) > 1:
            left, right = "‹", "›"
            head = [("class:rail.arrow", f" {left} "),
                    ("class:rail.term", self.terminal_label(self.terminal_name)),
                    ("class:rail.arrow", f" {right}")]
            hits[0] = ("terminal",)
        count = f"{len(agents)} agent{'s' if len(agents) != 1 else ''} "
        lines.append(self._spread(head, [("class:rail.count", count)], width))
        census: list = []
        if working:
            census.append(("class:running", f" {symbols.DOT} {working} working"))
        if attention:
            census.append(("class:approval", f"  {symbols.DOT_HALF} {attention} for you"))
        if not census:
            census = [("class:rail.count", " all idle")]
        lines.append(_fit(census, width))
        lines.append(_fit([("class:separator", "─" * width)], width))

        page = self._rail_page_size()
        max_offset = max(0, len(agents) - page)
        self.rail_offset = min(max(0, self.rail_offset), max_offset)
        # The "↑ N more" / "↓ N more" rows take room from the cards: drop
        # cards until everything fits, or the last indicator is cut off.
        count = page
        while count > 1:
            above = 1 if self.rail_offset else 0
            below = 1 if self.rail_offset + count < len(agents) else 0
            if 3 + above + below + 4 * count <= height:
                break
            count -= 1
        visible = agents[self.rail_offset:self.rail_offset + count]
        if self.rail_offset:
            hits[len(lines)] = ("scroll", -1)
            lines.append(_fit([("class:muted", f" {symbols.ARROW_U} {self.rail_offset} more")], width))
        if not agents:
            lines.append(_fit([], width))
            lines.append(_fit([("class:muted", " No Agents in this terminal")], width))
            lines.append(_fit([("class:dim", " /hire <name> adds one")], width))
        for agent in visible:
            status = statuses[agent.id]
            selected = agent.id == self.selected_id
            hovered = self._hover == ("rail", agent.id)
            bg = ("class:rail.card.selected" if selected else
                  "class:rail.card.hover" if hovered else "")
            bar = ("class:rail.bar", "▌") if selected else (bg, " ")
            profile = self._agent_profile(agent)
            unread = self.unread(agent.id)
            inner = width - 1

            if status in WORKING:
                glyph = [("class:spinner", _spinner_frame(self._working_elapsed(agent)))]
            else:
                icon, style = STATUS.get(status, (symbols.DOT_OPEN, "class:idle"))
                glyph = [(style, icon + " ")]
            name_style = "class:rail.name.selected" if selected else "class:rail.name"
            badge = [("class:badge", f" {unread} ")] if unread else []
            name_w = inner - 4 - (cell_width(f" {unread} ") + 1 if unread else 0)
            row0 = glyph + [("", " "), (name_style, _crop_cells(profile["name"], name_w))]
            row0 = self._spread(row0, badge + [("", " ")] if badge else [], inner)

            if status in WORKING:
                elapsed = self._working_elapsed(agent)
                verb = _crop_cells(self._work_verb(agent), inner - 9)
                row1 = [("", "   ")] + _shimmer_fragments(verb, elapsed) + [
                    ("class:rail.meta", f" {elapsed:.0f}s")]
            else:
                row1 = [("", "   "), ("class:rail.task", _crop_cells(
                    self._rail_subtitle(agent, status, profile), inner - 4))]

            deploy_glyph = symbols.DOT if profile["deployed"] else symbols.DOT_OPEN
            meta = profile["model"] or "default model"
            row2 = [("", "   "),
                    ("class:deploy.on" if profile["deployed"] else "class:rail.meta",
                     deploy_glyph + " "),
                    ("class:rail.meta", _crop_cells(meta, inner - 6))]
            for row in (row0, row1, row2):
                hits[len(lines)] = ("agent", agent.id)
                lines.append([bar] + [(f"{s} {bg}".strip(), t)
                                      for s, t, *_ in _fit(row, inner)])
            hits[len(lines)] = ("agent", agent.id)
            lines.append(_fit([], width))
        hidden_below = len(agents) - self.rail_offset - len(visible)
        if hidden_below > 0:
            hits[len(lines)] = ("scroll", 1)
            lines.append(_fit([("class:muted", f" {symbols.ARROW_D} {hidden_below} more")], width))
        while len(lines) < height:
            lines.append(_fit([], width))
        self._rail_hits = hits
        return lines[:height]

    @staticmethod
    def _spread(left: list, right: list, width: int) -> list:
        """``left`` at the start of the row and ``right`` against its end."""
        right_w = cell_width("".join(t for _s, t, *_ in right))
        body = _fit(left, max(0, width - right_w))
        return body + list(right) if right_w <= width else _fit(left, width)

    def rail_fragments(self):
        _width, height = self._terminal_size()
        lines = self.rail_lines(self._rail_width() or RAIL_WIDTH, height)
        fragments: list = []
        for line in lines:
            fragments.extend(line)
            fragments.append(("", "\n"))
        return FormattedText(fragments)

    def _rail_mouse(self, mouse_event):
        kind = mouse_event.event_type
        if kind == MouseEventType.SCROLL_UP:
            self.scroll_rail(-1)
            return None
        if kind == MouseEventType.SCROLL_DOWN:
            self.scroll_rail(1)
            return None
        hit = self._rail_hits.get(mouse_event.position.y)
        if kind == MouseEventType.MOUSE_MOVE:
            hover = ("rail", hit[1]) if hit and hit[0] == "agent" else None
            if hover != self._hover:
                self._hover = hover
                self.invalidate()
            return None
        if kind == MouseEventType.MOUSE_UP:
            self._drag = None
        if kind == MouseEventType.MOUSE_UP and hit:
            if hit[0] == "agent":
                self.select(hit[1])
                self._keep_selected_visible()
            elif hit[0] == "scroll":
                self.scroll_rail(hit[1])
            elif hit[0] == "terminal":
                self.cycle_terminal(-1 if mouse_event.position.x <= 3 else 1)
        return None

    def scroll_rail(self, delta: int) -> None:
        agents = self.agents()
        maximum = max(0, len(agents) - self._rail_page_size())
        self.rail_offset = min(maximum, max(0, self.rail_offset + delta))
        self.invalidate()

    @staticmethod
    def _crop(text: str, width: int) -> str:
        return _crop_cells(text, width)

    # ── transcript rows ──────────────────────────────────────────────────
    def _history_blocks(self, agent) -> list:
        """Blocks from persisted chat history, for an Agent with no events
        this session (hired earlier, restored from disk)."""
        blocks = []
        for index, message in enumerate(
                (getattr(agent, "chat_history", []) or [])[-30:]):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role not in {"user", "assistant"} or not content.strip():
                continue
            if message.get("input_kind") in {"shell", "interactive", "slash"}:
                continue
            blocks.append(agents_transcript.Block(
                "user" if role == "user" else "assistant", f"h{index}",
                title=self._agent_name(agent.id), body=content))
        return blocks

    def _transcript_rows(self, agent_id: str, width: int) -> list[Row]:
        """Every row of one Agent's transcript at ``width`` cells, cached."""
        agent = agent_loop.get_agent(agent_id)
        if agent is None:
            return []
        raw_lines = self._mirror_lines(agent_id) if self._raw[agent_id] else None
        _revision, events = agent_ui_events.hub.agent_events_snapshot(
            agent_id, limit=1500)
        working = str(getattr(agent, "status", "")) in WORKING
        show_card = not self._profile_visible()
        profile = self._agent_profile(agent) if show_card else None
        history_size = len(getattr(agent, "chat_history", []) or [])
        key = (events[-1].seq if events else 0, len(events), history_size,
               width, frozenset(self._expanded[agent_id]), self._expand_all,
               working, tuple(sorted((k, str(v)) for k, v in profile.items()))
               if profile else None,
               (len(raw_lines), raw_lines[-1] if raw_lines else "")
               if raw_lines is not None else None)
        cached = self._rows_cache.get(agent_id)
        if cached is not None and cached[0] == key:
            return cached[1]

        rows: list[Row] = []
        if raw_lines is not None:
            for line in raw_lines:
                try:
                    formatted = list(to_formatted_text(ANSI(line)))
                except Exception:
                    formatted = [("", line)]
                for chunk in self._wrap_formatted_rows(formatted, width):
                    fragments = [(style, text) for style, text, *_ in chunk]
                    rows.append(Row(fragments, "".join(t for _s, t in fragments)))
        else:
            name = self._agent_name(agent_id)
            blocks = agents_transcript.build_blocks(events, name, agent_id)
            if not blocks:
                blocks = self._history_blocks(agent)
            if not working:
                # A tool with no finish event after the Agent stopped did not
                # keep running; it was cut off with the turn.
                for block in blocks:
                    if block.kind == "tool" and block.status == "running":
                        block.status = "error"
                        block.extra.setdefault("salient", "")
            if profile is not None:
                rows.extend(agents_transcript.profile_rows(profile, width))
                rows.append(Row([], ""))
            if blocks:
                rows.extend(agents_transcript.render_rows(
                    blocks, width, self._expanded[agent_id],
                    expand_all=self._expand_all))
            else:
                rows.append(Row([("class:empty.title", "  No conversation yet")],
                                "  No conversation yet"))
                hint = (f"  Send {name} a message below to give it a task. "
                        f"Its replies, tool calls and results appear here.")
                for chunk in agents_transcript.wrap(
                        [("class:muted", hint)], width, (), [("", "  ")]):
                    rows.append(Row(chunk, "".join(t for _s, t in chunk)))
        self._rows_cache[agent_id] = (key, rows)
        return rows

    def _transcript_text(self, agent_id: str, width: int = 100) -> list[str]:
        """Plain text of each transcript row — what a copy would produce."""
        return [row.text for row in self._transcript_rows(agent_id, width)]

    def _agent_name(self, agent_id: str) -> str:
        agent = agent_loop.get_agent(agent_id)
        return str(agent.name or agent.id) if agent else agent_id

    def _working_elapsed(self, agent) -> float:
        """Seconds this Agent has been in its current stretch of work.

        Started when it enters a working state and forgotten when it leaves,
        so a finished-then-restarted Agent counts from the restart rather
        than showing the age of some earlier task.
        """
        agent_id = str(getattr(agent, "id", "") or "")
        if not agent_id:
            return 0.0
        now = time.monotonic()
        if str(getattr(agent, "status", "")) not in WORKING:
            self._work_since.pop(agent_id, None)
            return 0.0
        started = self._work_since.get(agent_id)
        if started is None:
            # Prefer the assignment's own clock when there is one: it began
            # before this view opened, and restarting the count at zero when
            # someone presses Alt+A would misreport a long-running task.
            active = getattr(agent, "active_assignment", None)
            wall_start = getattr(active, "started_at", None) if active else None
            started = now
            if wall_start:
                try:
                    started = now - max(0.0, time.time() - float(wall_start))
                except Exception:
                    started = now
            self._work_since[agent_id] = started
        return max(0.0, now - started)

    def _work_verb(self, agent) -> str:
        """What this Agent is doing, in the CLI's own vocabulary."""
        status = str(getattr(agent, "status", "") or "")
        with self._approval_lock:
            waiting_on_you = any(request.get("agent_id") == agent.id
                                 for request in self._approvals)
        if waiting_on_you:
            return "Waiting for your approval…"
        if status == "queued":
            return "Queued…"
        if status == "waiting":
            return "Waiting…"
        for event in reversed(
                agent_ui_events.hub.agent_events(agent.id, limit=30)):
            if event.event_type == "ai_stream":
                return "Writing…"
            if event.event_type == "ai_end":
                return "Working…"
            if event.event_type == "tool_started":
                return self._crop(event.summary or "Running…", 28)
            if event.event_type in {"tool_finished", "agent_started"}:
                break
        return "Thinking…"

    def _status_fragments(self, agent_id: str, width: int = 0,
                          context: bool = True) -> list:
        """The live status row for one Agent, or [] when it is not working.

        Same shape as the row the plain CLI paints during a turn:
        ``L› Thinking… 12.4s · model · MODE``. The spinner frame and the
        highlight band are computed from the elapsed clock rather than a
        frame counter, so the animation stays smooth at any redraw rate and
        identical to the CLI's.
        """
        agent = agent_loop.get_agent(agent_id)
        if agent is None or str(getattr(agent, "status", "")) not in WORKING:
            return []
        elapsed = self._working_elapsed(agent)
        verb = self._work_verb(agent)
        fragments = [("class:spinner", _spinner_frame(elapsed) + " ")]
        fragments.extend(_shimmer_fragments(verb, elapsed))
        fragments.append(("class:muted", f" {elapsed:.1f}s"))
        # The model/mode tail is the first thing to go on a narrow screen:
        # the verb and the clock are the row, the rest is context.
        if context and width >= 64:
            tail = self._runtime_context(agent)
            if tail:
                fragments.append(("class:muted",
                                  f" {symbols.BULLET} {tail}"))
        return fragments

    def _runtime_context(self, agent) -> str:
        """The model, for the status row's tail, best-effort."""
        parts = []
        try:
            model, _provider = agent_loop.resolve_agent_model(agent)
            model = str(model or "") or agent_loop._live_status_model()
            if model:
                parts.append(model.split("/")[-1])
        except Exception:
            pass
        # The mode is not repeated here: the footer carries it permanently.
        return f" {symbols.BULLET} ".join(parts)

    def _activity_line(self, agent_id: str) -> tuple[str, str] | None:
        """Flat (style, text) activity summary — kept for non-animated uses."""
        fragments = self._status_fragments(agent_id, context=False)
        if not fragments:
            return None
        return "class:thinking", "".join(text for _style, text in fragments)

    def _mirror_lines(self, agent_id: str) -> Optional[list[str]]:
        """Real-REPL conversation scrollback for REPL-executed Agents.

        Returns None when the mirror doesn't apply to this Agent (fall back
        to the event view). An empty list is meaningful: a fresh Agent's
        screen starts blank — no banner, no terminal decoration.
        """
        if self.mirror is None:
            return None
        agent = agent_loop.get_agent(agent_id)
        if agent is None or agent.role != "primary":
            return None
        try:
            return self.mirror.read_lines(agent_id)
        except Exception:
            return None

    def _stream_tail(self, agent_id: str) -> str:
        """Last visible line of the reply currently being streamed, if any."""
        parts: list[str] = []
        for event in reversed(
                agent_ui_events.hub.agent_events(agent_id, limit=200)):
            if event.event_type == "ai_stream":
                parts.append(event.detail)
            elif event.event_type in {
                    "ai_end", "ai", "user", "user_message",
                    "agent_started", "tool_started", "agent_done"}:
                break
        if not parts:
            return ""
        lines = [line for line in
                 "".join(reversed(parts)).splitlines() if line.strip()]
        return lines[-1] if lines else ""

    @staticmethod
    def _wrap_formatted_rows(fragments, width: int):
        """Wrap styled fragments into physical terminal rows by cell width."""
        width = max(1, int(width))
        rows: list[list[tuple]] = [[]]
        column = 0
        for fragment in explode_text_fragments(list(fragments)):
            style, char, *rest = fragment
            if char == "\n":
                rows.append([])
                column = 0
                continue
            cell_width = max(0, get_cwidth(char))
            if column and cell_width and column + cell_width > width:
                rows.append([])
                column = 0
            row = rows[-1]
            value = (style, char, *rest)
            # Exploding is convenient for width accounting but expensive for
            # rendering. Recombine adjacent characters with identical style
            # and mouse metadata before handing them back to prompt_toolkit.
            if (row and row[-1][0] == style
                    and tuple(row[-1][2:]) == tuple(rest)):
                row[-1] = (style, row[-1][1] + char, *rest)
            else:
                row.append(value)
            column += cell_width
        return rows

    # ── transcript pane ──────────────────────────────────────────────────
    def transcript_lines(self, width: int, height: int) -> list:
        """The visible window of the transcript plus its scrollbar."""
        agent_id = self.selected_id
        if not agent_id or agent_loop.get_agent(agent_id) is None:
            self._viewport = ("", 0, 0, 0, 0, width, height)
            return self._empty_lines(width, height)
        content_w = max(10, width - 3)   # 1 margin, 1 gap, 1 scrollbar
        rows = self._transcript_rows(agent_id, content_w)
        live = self._live_status_row(agent_id, content_w)
        if (live is not None) != self._status_tail.get(agent_id, False):
            # The status row coming or going is not news, and must not
            # nudge a page someone is reading.
            self._status_tail[agent_id] = live is not None
            self._relayout.add(agent_id)
        if live is not None:
            rows = rows + [Row([], ""), Row(
                live, "".join(text for _style, text in live), anim="status")]
        total = len(rows)
        max_offset = max(0, total - height)

        # Keep the page still while new rows arrive below it: a person who
        # scrolled up to read is not following, and the text under their
        # eyes must not creep upward with every streamed chunk.
        previous = self._last_total.get(agent_id)
        if previous is not None and total != previous and not self.follow[agent_id]:
            grown = total - previous
            self.focus_scroll[agent_id] = max(0, self.focus_scroll[agent_id] + grown)
            self._scroll_pos[agent_id] = max(0.0, self._scroll_pos[agent_id] + grown)
            # Rows from expanding or collapsing a block are not news.
            if grown > 0 and agent_id not in self._relayout:
                self._unseen_rows[agent_id] += grown
        self._relayout.discard(agent_id)
        self._last_total[agent_id] = total
        self.focus_scroll[agent_id] = min(max(0, self.focus_scroll[agent_id]), max_offset)
        self._scroll_pos[agent_id] = min(max(0.0, self._scroll_pos[agent_id]), float(max_offset))
        if self.focus_scroll[agent_id] == 0 and self._scroll_pos[agent_id] < 0.5:
            self.follow[agent_id] = True
            self._unseen_rows[agent_id] = 0
        offset = int(round(self._scroll_pos[agent_id]))
        end = total - offset
        start = max(0, end - height)
        visible = rows[start:end]
        if self.follow[agent_id]:
            events = agent_ui_events.hub.agent_events(agent_id, limit=1)
            if events:
                self.read_seq[agent_id] = max(self.read_seq[agent_id], events[-1].seq)
        self._viewport = (agent_id, start, len(visible), total, content_w, width, height)

        selection = self._normalized_selection(agent_id)
        hover_block = self._hover[1] if self._hover and self._hover[0] == "block" else None
        now = time.monotonic()
        lines = []
        for index, row in enumerate(visible):
            absolute = start + index
            fragments = self._animate_row(row, now)
            if hover_block and row.action and row.block == hover_block:
                fragments = [(f"{s} class:row.hover", t) for s, t, *_ in fragments]
            fragments = _fit(fragments, content_w, row.fill)
            if selection:
                (r0, c0), (r1, c1) = selection
                if r0 <= absolute <= r1:
                    lo = c0 if absolute == r0 else 0
                    hi = c1 + 1 if absolute == r1 else content_w
                    fragments = _highlight(fragments, lo, hi, "class:selection")
            lines.append([("", " ")] + fragments + [("", " ")])
        while len(lines) < height:
            lines.append([("", " " * (width - 1))])

        # Scrollbar: a thin track with a thumb sized to the share of the
        # transcript on screen. Absent when everything fits.
        if total > height:
            thumb = max(1, round(height * height / total))
            travel = height - thumb
            top = round(travel * (start / max(1, total - height)))
            active = (self._drag and self._drag[0] == "bar") or self._hover == ("bar",)
            thumb_style = "class:scrollbar.thumb.active" if active else "class:scrollbar.thumb"
            for index in range(height):
                on_thumb = top <= index < top + thumb
                lines[index] = _fit(lines[index], width - 1) + [
                    (thumb_style if on_thumb else "class:scrollbar.track",
                     "┃" if on_thumb else "│")]
        else:
            lines = [_fit(line, width) for line in lines]

        # "Jump to latest" pill over the bottom-right corner while scrolled up.
        self._pill = None
        if not self.follow[agent_id] and height >= 2:
            unseen = self._unseen_rows[agent_id]
            label = (f" {symbols.ARROW_D} {unseen} new line{'s' if unseen != 1 else ''} "
                     if unseen else f" {symbols.ARROW_D} latest ")
            label_w = cell_width(label)
            if label_w + 4 < width:
                x = width - label_w - 3
                last = lines[height - 1]
                lines[height - 1] = (_fit(last, x) + [("class:pill", label)]
                                     + _slice_fragments(last, x + label_w, width))
                self._pill = (height - 1, x, x + label_w)
        return lines

    def _empty_lines(self, width: int, height: int) -> list:
        lines = [_fit([], width) for _ in range(height)]
        message = [
            [("class:empty.title", "No Agents in this terminal")],
            [],
            [("class:muted", "Hire one with "), ("class:key", "/hire <name>"),
             ("class:muted", ", or press "), ("class:key", "Esc"),
             ("class:muted", " to leave.")],
        ]
        top = max(0, height // 2 - 2)
        for index, row in enumerate(message):
            if top + index < height:
                row_w = cell_width("".join(t for _s, t in row))
                pad = max(0, (width - row_w) // 2)
                lines[top + index] = _fit([("", " " * pad)] + row, width)
        return lines

    def _animate_row(self, row: Row, now: float) -> list:
        fragments = list(row.fragments)
        if row.anim == "tool" and fragments:
            # A running tool's bullet breathes; a finished one is solid.
            phase = int(now / 0.45) % 2
            style = "class:tool.running" if phase == 0 else "class:tool.running.dim"
            fragments[0] = (f"{style} class:tool.row", fragments[0][1])
        elif row.anim == "cursor":
            if int(now / 0.5) % 2 == 0:
                fragments.append(("class:cursor", "▍"))
        return fragments

    def _normalized_selection(self, agent_id: str):
        if not self._selection or self._selection[0] != agent_id:
            return None
        _agent, anchor, head = self._selection
        if anchor == head:
            return None
        return (anchor, head) if anchor <= head else (head, anchor)

    def _selected_text(self) -> str:
        agent_id = self._viewport[0]
        selection = self._normalized_selection(agent_id)
        if not selection:
            return ""
        (r0, c0), (r1, c1) = selection
        rows = self._transcript_rows(agent_id, self._viewport[4])
        parts = []
        for index in range(r0, min(r1, len(rows) - 1) + 1):
            text = rows[index].text
            lo = c0 if index == r0 else 0
            hi = c1 + 1 if index == r1 else cell_width(text) + 1
            parts.append(_slice_cells(text, lo, hi).rstrip())
        return "\n".join(parts).strip("\n")

    def _copy_to_clipboard(self, text: str) -> bool:
        """OSC 52: the terminal puts it on the clipboard — works over SSH
        and in tmux, where there is no local clipboard to reach."""
        if not text or self.app is None:
            return False
        try:
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            self.app.output.write_raw(f"\x1b]52;c;{payload}\x07")
            self.app.output.flush()
            return True
        except Exception:
            return False

    def _scroll_to_bar(self, y: int) -> None:
        agent_id, _start, _n, total, _cw, _w, height = self._viewport
        if not agent_id or total <= height:
            return
        fraction = min(1.0, max(0.0, y / max(1, height - 1)))
        offset = round((total - height) * (1.0 - fraction))
        self.focus_scroll[agent_id] = offset
        self._scroll_pos[agent_id] = float(offset)
        self.follow[agent_id] = offset == 0
        self.invalidate()

    def _transcript_mouse(self, mouse_event):
        kind = mouse_event.event_type
        x, y = mouse_event.position.x, mouse_event.position.y
        agent_id, start, _n, total, content_w, width, height = self._viewport
        if kind == MouseEventType.SCROLL_UP:
            self.scroll(3)
            return None
        if kind == MouseEventType.SCROLL_DOWN:
            self.scroll(-3)
            return None
        if not agent_id:
            return None
        on_bar = x >= width - 1 and total > height
        row_index = start + max(0, min(y, height - 1))
        column = max(0, min(content_w - 1, x - 1))
        if kind == MouseEventType.MOUSE_DOWN:
            if self._pill and y == self._pill[0] and self._pill[1] <= x < self._pill[2]:
                self._drag = ("pill",)
                return None
            if on_bar:
                self._drag = ("bar",)
                self._scroll_to_bar(y)
                return None
            self._drag = ("select", (row_index, column))
            if self._selection:
                self._selection = None
                self.invalidate()
            return None
        if kind == MouseEventType.MOUSE_MOVE:
            if self._drag and mouse_event.button == MouseButton.LEFT:
                if self._drag[0] == "bar":
                    self._scroll_to_bar(y)
                elif self._drag[0] == "select":
                    self._selection = (agent_id, self._drag[1], (row_index, column))
                    # Dragging against an edge scrolls, so a selection can
                    # run past what is on screen.
                    if y <= 0:
                        self.scroll(1, smooth=False)
                    elif y >= height - 1:
                        self.scroll(-1, smooth=False)
                    self.invalidate()
                return None
            hover = None
            if on_bar:
                hover = ("bar",)
            else:
                rows = self._transcript_rows(agent_id, content_w)
                if 0 <= row_index < len(rows) and rows[row_index].action:
                    hover = ("block", rows[row_index].block)
            if hover != self._hover:
                self._hover = hover
                self.invalidate()
            return None
        if kind == MouseEventType.MOUSE_UP:
            drag, self._drag = self._drag, None
            if drag and drag[0] == "pill":
                self.follow_latest()
            elif drag and drag[0] == "select":
                text = self._selected_text()
                if text:
                    if self._copy_to_clipboard(text):
                        lines = text.count("\n") + 1
                        self.notice = (f"Copied {len(text)} characters"
                                       + (f" ({lines} lines)" if lines > 1 else ""))
                elif drag[1] == (row_index, column):
                    self._selection = None
                    rows = self._transcript_rows(agent_id, content_w)
                    if 0 <= row_index < len(rows) and rows[row_index].action:
                        self._toggle_block(agent_id, rows[row_index].action[1])
            self.invalidate()
            return None
        return None

    def _toggle_block(self, agent_id: str, key: str) -> None:
        # Hold the page still while the block grows or shrinks under the
        # pointer: in follow mode the bottom is pinned, which would push the
        # block that was just clicked up and off the screen.
        self.follow[agent_id] = False
        self._relayout.add(agent_id)
        expanded = self._expanded[agent_id]
        if key in expanded:
            expanded.discard(key)
        else:
            expanded.add(key)
        self.invalidate()

    def follow_latest(self) -> None:
        if self.selected_id:
            self.focus_scroll[self.selected_id] = 0
            self.follow[self.selected_id] = True
        self.invalidate()

    def focus_fragments(self):
        """The transcript pane as one FormattedText at the current size."""
        width, height = self._transcript_size()
        fragments: list = []
        for line in self.transcript_lines(width, height):
            fragments.extend((style, text) for style, text, *_ in line)
            fragments.append(("", "\n"))
        return FormattedText(fragments)

    # ── title bar ────────────────────────────────────────────────────────
    def title_lines(self, width: int, height: int) -> list:
        agent = agent_loop.get_agent(self.selected_id) if self.selected_id else None
        if agent is None:
            return [_fit([("class:pane.title", " AGENTS")], width)]
        profile = self._agent_profile(agent)
        # Only the two buttons (resume, model) sit on a filled background;
        # everything else on the right is a plain coloured label. Keeping
        # glyphs like ●/○/↑ out of the fills matters: terminals draw them from
        # a fallback font, and a fill behind a fallback glyph comes out a
        # different height from the pure-text buttons beside it.
        chips: list = []
        if self._raw[agent.id]:
            chips += [("class:chip.raw", "RAW"), ("", "  ")]
        offset = self.focus_scroll[agent.id]
        if offset:
            chips += [("class:muted", f"{symbols.ARROW_U} {offset}"), ("", "  ")]
        buttons = self._title_buttons(agent)
        spans = []                   # (offset within chips, width, name)
        for name, label in buttons:
            text = f" {label} "
            spans.append((cell_width("".join(t for _s, t in chips)) + 2,
                          cell_width(text), name))
            chips += [(self._title_button_style(name, profile), text),
                      ("", " ")]
        if buttons:
            chips.append(("", " "))
        if profile["deployed"]:
            chips += [("class:deploy.on", f"{symbols.DOT} "),
                      ("class:title.role", self.terminal_label(
                          agent_loop.agent_deployment_terminal(agent)))]
        else:
            chips += [("class:deploy.off", f"{symbols.DOT_OPEN} "),
                      ("class:title.role", "not deployed")]
        chips = [("", "  ")] + chips + [("", " ")]
        chips_w = cell_width("".join(t for _s, t in chips))
        left = [("", " "), ("class:title.name", profile["name"])]
        if profile["title"]:
            left += [("class:dim", f"  {symbols.BULLET}  "),
                     ("class:title.role", profile["title"])]
        if chips_w + 12 > width:
            chips, spans = [], []
            chips_w = 0
        line = self._spread(left, chips, width)
        # _spread right-aligns the chips; the leading "  " is in `chips`, and
        # each span offset above already counts it.
        start = width - chips_w
        self._title_hits = [(start + offset, start + offset + w, name)
                            for offset, w, name in spans]
        return [line]

    def _title_buttons(self, agent) -> list:
        """``[(name, label), ...]`` for the clickable title-bar buttons.

        A temporary sub-agent has no saved sessions and no model of its own,
        so it gets neither.
        """
        if agent is None or agent.role == "subagent":
            return []
        profile = self._agent_profile(agent)
        return [("resume", "resume"),
                ("model", _crop_cells(profile["model"] or "default model", 28))]

    def _title_button_style(self, name: str, profile: dict) -> str:
        hovered = self._hover == ("title", name)
        if name == "model" and profile["model_available"] is False:
            return "class:chip.warn.hover" if hovered else "class:chip.warn"
        return "class:chip.hover" if hovered else f"class:chip.{name}"

    def _title_hit(self, x: int) -> Optional[str]:
        return next((name for lo, hi, name in self._title_hits
                     if lo <= x < hi), None)

    def _title_mouse(self, mouse_event):
        kind = mouse_event.event_type
        hit = self._title_hit(mouse_event.position.x)
        if kind == MouseEventType.MOUSE_MOVE:
            hover = ("title", hit) if hit else None
            if hover != self._hover:
                self._hover = hover
                self.invalidate()
            return None
        if kind == MouseEventType.MOUSE_DOWN:
            self._drag = ("title", hit) if hit else None
            return None
        if kind != MouseEventType.MOUSE_UP:
            return None
        # A button fires only when the press started on it too: a text
        # selection dragged up out of the transcript and released here must
        # not close the view.
        drag, self._drag = self._drag, None
        if hit and drag == ("title", hit):
            self._hover = None
            self.open_picker(hit)
        return None

    def open_picker(self, name: str) -> bool:
        """Run the CLI's own /resume or /model picker for the selected Agent.

        The pickers are full-screen dialogs owned by the REPL, so the view
        closes, the REPL runs them, and ``/agents <id>`` brings the view back
        on the same Agent — chosen or cancelled.

        /model is aimed at the Agent directly (``/model @<id>``), without
        moving the REPL's focus. /resume has to switch the REPL to the Agent
        first (``/agent <id>``): a resumed session is restored into the
        REPL's conversation, and each Agent keeps its own resume list.
        """
        agent = agent_loop.get_agent(self.selected_id) if self.selected_id else None
        if agent is None or not self._title_buttons(agent):
            return False
        label = agent.name or agent.id
        if name == "model":
            self.request_command([f"/model @{agent.id}", f"/agents {agent.id}"])
            return True
        current = agent_loop.get_agent(agent_loop.get_current_agent_id())
        if agent.id != getattr(current, "id", None):
            # /agent refuses both of these, and /resume would then restore
            # into whatever the REPL was on — the wrong Agent's session.
            if str(getattr(agent, "status", "")) in WORKING or getattr(
                    agent, "active_assignment", None) is not None:
                self.notice = f"Cannot resume {label} while it is working"
                self.invalidate()
                return False
            if current is not None and str(
                    getattr(current, "status", "")) in WORKING:
                self.notice = (f"Cannot resume {label} while "
                               f"{current.name or current.id} is working")
                self.invalidate()
                return False
        elif str(getattr(agent, "status", "")) in WORKING:
            self.notice = f"Cannot resume {label} while it is working"
            self.invalidate()
            return False
        commands = []
        if agent.id != getattr(current, "id", None):
            commands.append(f"/agent {agent.id}")
        commands += ["/resume", f"/agents {agent.id}"]
        self.request_command(commands)
        return True

    def request_command(self, commands: list) -> None:
        """Close the view and hand slash commands to the main REPL.

        The pickers the chips open (/model, /resume) are full-screen dialogs
        owned by the CLI body; running them inside this view would stack two
        prompt_toolkit applications on one tty. So the chip only records the
        commands, exits the view, and the launcher injects them into the REPL
        loop once the screen is back in its hands.
        """
        self.pending_commands = [str(c) for c in commands]
        if self.app is not None and not self.app.is_done:
            self.app.exit()
        else:
            self._deliver_pending_command()

    def _deliver_pending_command(self) -> None:
        commands, self.pending_commands = self.pending_commands, []
        if not commands:
            return
        try:
            import threading as _threading
            import laintas_cli
            for command in commands:
                laintas_cli._inject_input(command, _threading.Event())
        except Exception:
            pass

    # ── status row (segment 1) ───────────────────────────────────────────
    def _live_status_row(self, agent_id: str, width: int) -> Optional[list]:
        """The CLI's status row for a working Agent, or None when idle.

        Painted as the transcript's last row, right under the newest output
        — where the plain CLI shows it — and never cached, since it moves
        every frame.
        """
        status = self._status_fragments(agent_id, width)
        if not status:
            return None
        agent = agent_loop.get_agent(agent_id)
        tokens = int(getattr(agent, "usage_tokens", 0) or 0)
        if tokens and width >= 48:
            status.append(("class:muted", f" {symbols.BULLET} "
                           f"{self._format_tokens(tokens)} tokens"))
        return status

    @staticmethod
    def _format_tokens(count: int) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1000:
            return f"{count / 1000:.1f}k"
        return str(count)

    # ── footer (segment 3) ───────────────────────────────────────────────
    NOTICE_SECONDS = 6.0

    def _toast(self) -> Optional[tuple[str, str]]:
        if self.pending_approval() is not None:
            return None
        if not self._notice or time.monotonic() - self._notice_at > self.NOTICE_SECONDS:
            return None
        lowered = self._notice.casefold()
        if lowered.startswith(("copied", "sent", "started", "ready", "approved",
                               "queued")):
            return "class:toast.ok", self._notice
        if any(word in lowered for word in ("fail", "could not", "cannot",
                                            "not authenticated", "denied",
                                            "unavailable", "not available",
                                            "aborted", "full")):
            return "class:toast.err", self._notice
        return "class:toast.info", self._notice

    def hint_fragments(self):
        """Keys that apply to what is on screen, not a fixed keycap dump."""
        if self.pending_approval() is not None:
            keys = [("y", "approve"), ("n", "deny"), ("esc", "back")]
        elif self.overlay:
            keys = [("alt+" + symbols.ARROW_U + symbols.ARROW_D, "pick"),
                    ("tab", "close"), ("esc", "back")]
        else:
            agent = agent_loop.get_agent(self.selected_id)
            keys = [("enter", self._input_action(agent)),
                    ("@name", "route"),
                    ("alt+" + symbols.ARROW_U + symbols.ARROW_D, "agent")]
            if len(agent_loop.get_all_terminals()) > 1:
                keys.append(("alt+" + symbols.ARROW_L + symbols.ARROW_R, "terminal"))
            keys.append(("ctrl+o", "collapse" if self._expand_all else "expand"))
            if self._selection:
                keys.insert(0, ("esc", "clear selection"))
            else:
                keys.append(("esc", "back"))
        return FormattedText(self._key_row(keys, self._terminal_size()[0] - 2))

    @staticmethod
    def _key_row(keys: list, width: int) -> list:
        # Drop from the right as the terminal narrows: the first hints are the
        # ones a person needs, and a row that wraps costs a whole line.
        fragments: list = []
        used = 0
        for index, (key, what) in enumerate(keys):
            lead = f"  {symbols.BULLET} " if index else " "
            cost = cell_width(lead + key + " " + what)
            if used + cost > width:
                break
            fragments.extend([("class:dim", lead), ("class:key", key),
                              ("class:muted", f" {what}")])
            used += cost
        return fragments

    def footer_lines(self, width: int, height: int) -> list:
        toast = self._toast()
        right: list = []
        try:
            mode = str(agent_loop._active_mode_label() or "")
        except Exception:
            mode = ""
        if not self._rail_width():
            others = [a for a in self.agents() if a.id != self.selected_id
                      and self._display_status(a) in WORKING]
            if others:
                right.append(("class:running", f"{symbols.DOT} {len(others)} working  "))
        if mode:
            right.append(("class:muted", f"{mode} "))
        right_w = cell_width("".join(t for _s, t in right))
        if toast:
            style, text = toast
            icon = {"class:toast.ok": symbols.OK, "class:toast.err": symbols.FAIL
                    }.get(style, symbols.INFO)
            left = [(style, f" {icon} "), (style, _crop_cells(
                text, max(10, width - right_w - 5)))]
        else:
            left = self._key_row(
                list(self._hint_pairs()), max(10, width - right_w - 2))
        return [self._spread(left, right, width)]

    def _hint_pairs(self):
        fragments = list(self.hint_fragments())
        pairs, key = [], None
        for style, text in fragments:
            if style == "class:key":
                key = text
            elif key is not None and style == "class:muted":
                pairs.append((key, text.strip()))
                key = None
        return pairs

    # ── profile panel ────────────────────────────────────────────────────
    def panel_lines(self, width: int, height: int) -> list:
        rows = [_fit(line, width) for line in self._panel_rows(width)][:height]
        return rows + [_fit([], width)] * max(0, height - len(rows))

    def _panel_rows(self, width: int) -> list:
        agent = agent_loop.get_agent(self.selected_id) if self.selected_id else None
        if agent is None:
            return [[("class:muted", "  No Agent selected")]]
        profile = self._agent_profile(agent)
        inner = width - 3
        rows: list = [[]]

        def section(label: str):
            if len(rows) > 1:
                rows.append([])
            rows.append([("class:pane.title", f"  {label}")])

        def text(value: str, style: str = "class:profile.value"):
            for chunk in agents_transcript.wrap([(style, value)], inner,
                                                [("", "  ")], [("", "  ")]):
                rows.append(chunk)

        section("PROFILE")
        rows.append([("", "  "), ("class:profile.name", _crop_cells(profile["name"], inner - 2)),
                     ("class:muted", f"  {profile['role_label']}")])
        if profile["title"]:
            text(profile["title"], "class:profile.title")
        if profile["description"]:
            text(profile["description"], "class:profile.text")
        if profile["tags"]:
            text(" ".join(f"#{tag}" for tag in profile["tags"][:6]), "class:message")

        section("MODEL")
        text(profile["model"] or "backend default")
        availability = profile["model_available"]
        status_row = []
        if availability is True:
            status_row = [("class:done", f"{symbols.OK} available")]
        elif availability is False:
            status_row = [("class:toast.warn", f"{symbols.WARN} not offered by backend")]
        elif availability == "checking":
            status_row = [("class:muted", "checking availability…")]
        if profile["model_source"] and profile["model"]:
            status_row += ([("class:dim", f" {symbols.BULLET} ")] if status_row else []) + [
                ("class:muted", profile["model_source"])]
        if status_row:
            rows.extend(agents_transcript.wrap(status_row, inner,
                                               [("", "  ")], [("", "  ")]))

        section("DEPLOYMENT")
        rows.extend(agents_transcript.wrap(
            [("class:deploy.on" if profile["deployed"] else "class:deploy.off",
              (symbols.DOT if profile["deployed"] else symbols.DOT_OPEN) + " "),
             ("class:profile.value", profile["deployment"])],
            inner, [("", "  ")], [("", "    ")]))

        section("TOOLS")
        text(profile["tools"])

        _revision, events = agent_ui_events.hub.agent_events_snapshot(
            agent.id, limit=300)
        status = self._display_status(agent, events)
        tool_ids = {event.tool_call_id for event in events
                    if event.tool_call_id and event.event_type in {
                        "tool", "tool_started", "tool_finished"}}
        tools = len(tool_ids) + sum(
            not event.tool_call_id and event.event_type in {"tool", "tool_finished"}
            for event in events)
        turns = sum(isinstance(message, dict)
                    and message.get("role") == "user"
                    and message.get("input_kind") not in {"shell", "interactive", "slash"}
                    for message in (agent.chat_history or []))
        section("ACTIVITY")
        icon, style = STATUS.get(status, (symbols.DOT_OPEN, "class:idle"))
        rows.append([("", "  "), ("class:profile.label", "status  "),
                     (style, f"{icon} {status}")])
        for label, value in (("task", profile["task"] or "—"),
                             ("turns", str(turns)),
                             ("tools", f"{tools} call{'s' if tools != 1 else ''}"),
                             ("tokens", self._format_tokens(
                                 int(getattr(agent, "usage_tokens", 0) or 0)))):
            rows.extend(agents_transcript.wrap(
                [("class:profile.value", value)], inner,
                [("", "  "), ("class:profile.label", label.ljust(8))],
                [("", " " * 10)]))
        if agent.role != "subagent":
            section("CONTINUE IN THE CLI")
            rows.append([("", "  "), ("class:key", f"/agent {agent.id}")])
            rows.append([("", "  "), ("class:muted", "then /resume for history")])
        return rows

    def inspector_fragments(self):
        fragments: list = []
        for row in self._panel_rows(self.PANEL_WIDTH):
            fragments.extend(row)
            fragments.append(("", "\n"))
        return FormattedText(fragments)

    def _input_action(self, agent) -> str:
        if agent is None:
            return "select an agent"
        if agent.status in WORKING:
            return "send update"
        if (callable(self.repl_submit_cb)
                and agent.id == agent_loop.get_current_agent_id()):
            return "continue"
        if agent.role in {"pool", "deployed"}:
            return "new task"
        if agent.role == "primary" and not callable(self.repl_submit_cb):
            return "continue"
        return "unavailable"


    def dispatch(self, raw: str) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        if text.startswith("/"):
            self.notice = (
                "Slash commands are handled by the main CLI; "
                "exit Agents Mode to run them")
            self.invalidate()
            return
        if self.selected_id:
            self.focus_scroll[self.selected_id] = 0
            self.follow[self.selected_id] = True
        # Agents Mode routes dialogue only. View-closing commands are consumed
        # by accept() before dispatch; all other slash commands stay with the
        # main CLI so they cannot be duplicated as Agent instructions.
        target_id = self.selected_id
        match = re.match(r"^@([A-Za-z0-9_.:-]+)\s+(.+)$", text, re.S)
        if match:
            reference, text = match.group(1), match.group(2).strip()
            target_id = self.resolve_agent(reference)
        agent = agent_loop.get_agent(target_id)
        if agent is None or agent_loop.agent_scope_terminal(agent) != self.terminal_name:
            self.notice = "Target Agent is not available in this terminal"
            return
        if agent.status in {"running", "thinking", "waiting", "queued"}:
            try:
                agent.message_queue.put_nowait(text)
                agent_ui_events.hub.emit(
                    "user_message", agent_id=agent.id,
                    terminal_name=self.terminal_name,
                    summary=text, detail=text, status="queued")
                self.notice = f"Queued for {agent.name or agent.id}"
            except queue.Full:
                agent_ui_events.hub.emit(
                    "user_message_failed", agent_id=agent.id,
                    terminal_name=self.terminal_name,
                    summary=text, detail=text, status="queue_full")
                self.notice = f"{agent.name or agent.id} instruction queue is full"
            except Exception as exc:
                agent_ui_events.hub.emit(
                    "user_message_failed", agent_id=agent.id,
                    terminal_name=self.terminal_name,
                    summary=text, detail=str(exc), status="delivery_error")
                self.notice = (
                    f"Could not queue instruction for "
                    f"{agent.name or agent.id}: {exc}")
        elif agent.status in {"idle", "ready", "done", "error", "aborted"}:
            if self.execution_block_reason:
                agent_ui_events.hub.emit(
                    "user_message", agent_id=agent.id,
                    terminal_name=self.terminal_name,
                    summary=text, detail=text, status="rejected")
                agent_ui_events.hub.emit(
                    "input_rejected", agent_id=agent.id,
                    terminal_name=self.terminal_name,
                    summary=self.execution_block_reason,
                    detail=self.execution_block_reason, status="error")
                self.notice = self.execution_block_reason
                self.invalidate()
                return
            if (callable(self.repl_submit_cb)
                    and agent.id == agent_loop.get_current_agent_id()):
                # The foreground can be scout/foreman too. Starting a new
                # assignment here would reset its history behind the REPL.
                ok, detail = self.repl_submit_cb(text)
                self.notice = detail or "Sent"
            elif agent.role == "primary":
                if callable(self.repl_submit_cb):
                    self.notice = (
                        f"Exit Agents Mode and use /agent {agent.id} to continue "
                        "this conversation")
                elif not callable(self.primary_submit_cb):
                    self.notice = "Primary runtime is unavailable"
                else:
                    ok, detail = self.primary_submit_cb(
                        agent, text, self._deps_for(agent.id))
                    self.notice = detail
            elif agent.role not in {"pool", "deployed"}:
                self.notice = (
                    f"{agent.name or agent.id} is a finished temporary Agent "
                    "and cannot accept a new assignment")
            else:
                ok, detail, _assignment = agent_loop.start_agent_assignment(
                    agent.id, text, self._deps_for(agent.id), self.session,
                    events_cb=self.external_events_cb)
                self.notice = (f"Started new task for {agent.name or agent.id}"
                               if ok else detail)
        else:
            self.notice = f"Agent state '{agent.status}' cannot accept input"
        self.invalidate()

    def _deps_for(self, agent_id: str):
        """Return execution wiring whose approvals remain attributed to one Agent."""
        deps = copy.copy(self.deps)
        # prompt_toolkit is the sole terminal renderer while Agents Mode owns
        # the screen. Rich Live/status/print output from worker threads causes
        # duplicated input, lost redraws and apparently unresponsive Enter.
        #
        # One console PER AGENT, not one shared silent console: rich refuses a
        # second live display on the same Console, so two agents streaming at
        # the same time meant the later one died with
        # LiveError("Only one live display may be active at once").
        deps.console = self._console_for(agent_id)
        for renderer in (
                "display_command_output", "display_sub_terminal_preview",
                "display_file_diff", "display_task_list"):
            if hasattr(deps, renderer):
                setattr(deps, renderer, lambda *_args, **_kwargs: None)
        deps.request_command_approval = (
            lambda command, reason: self._request_approval(
                agent_id, "command", command, reason))
        deps.request_file_write_approval = (
            lambda path, preview, reason: self._request_approval(
                agent_id, "write", path, "\n".join(
                    part for part in (reason, preview) if part)))
        deps.request_file_delete_approval = (
            lambda path, preview, reason: self._request_approval(
                agent_id, "delete", path, "\n".join(
                    part for part in (reason, preview) if part)))
        return deps

    def _console_for(self, agent_id: str) -> Console:
        """A private silent console per Agent (rich allows one live each)."""
        console = self._agent_consoles.get(agent_id)
        if console is None:
            console = Console(file=_NullWriter(), force_terminal=False,
                              width=self._console_width)
            console.render_terminal = False
            self._agent_consoles[agent_id] = console
        return console

    def _request_approval(self, agent_id: str, kind: str,
                          summary: str, detail: str) -> bool:
        """Bridge worker approval requests into the owning UI event loop."""
        request_agent = agent_loop.get_agent(agent_id)
        request_terminal = (
            agent_loop.agent_scope_terminal(request_agent)
            if request_agent is not None else None) or self.terminal_name
        request = {
            "id": f"approval-{time.time_ns()}",
            "agent_id": agent_id,
            "kind": kind,
            "summary": str(summary or kind),
            "detail": str(detail or ""),
            "terminal_name": request_terminal,
            "done": threading.Event(),
            "approved": False,
        }
        with self._approval_lock:
            closed = self._closed.is_set()
            if not closed:
                self._approvals.append(request)
                is_head = len(self._approvals) == 1
            else:
                is_head = False
            # Keep requested -> resolved ordering atomic with UI shutdown.
            agent_ui_events.hub.emit(
                "approval_requested", agent_id=agent_id,
                terminal_name=request_terminal, summary=request["summary"],
                detail=request["detail"], status="waiting",
                data={"approvalId": request["id"], "kind": kind})
        if closed:
            agent_ui_events.hub.emit(
                "approval_resolved", agent_id=agent_id,
                terminal_name=request_terminal, summary=request["summary"],
                status="denied",
                data={"approvalId": request["id"], "kind": kind,
                      "reason": "agents_mode_closed"})
            return False
        # A later request must not replace the text for the FIFO request that
        # y/n will actually resolve.
        if is_head:
            self.notice = self._approval_notice(request)
            self.invalidate()
        else:
            self._refresh_approval_notice()
        while not request["done"].wait(timeout=0.1):
            agent = agent_loop.get_agent(agent_id)
            if (agent is None or agent.lifecycle_terminated
                    or agent.abort_event.is_set()):
                cancelled = False
                with self._approval_lock:
                    if request in self._approvals:
                        self._approvals.remove(request)
                        request["approved"] = False
                        request["done"].set()
                        cancelled = True
                if cancelled:
                    agent_ui_events.hub.emit(
                        "approval_resolved", agent_id=agent_id,
                        terminal_name=request_terminal,
                        summary=request["summary"], status="denied",
                        data={"approvalId": request["id"], "kind": kind,
                              "reason": "agent_aborted"})
                    self._refresh_approval_notice(default="Denied: Agent aborted")
                break
        return bool(request["approved"])

    def _approval_notice(self, request: dict) -> str:
        agent_name = self._agent_name(str(request.get("agent_id") or ""))
        with self._approval_lock:
            count = len(self._approvals)
        queued = f" {symbols.BULLET} {count} pending" if count > 1 else ""
        return (
            f"Approval for {agent_name} ({request['kind']}): "
            f"{self._crop(request['summary'], 62)}{queued}  "
            "[y] approve  [n] deny"
        )

    def _approval_height(self) -> int:
        request = self.pending_approval()
        if request is None:
            return 0
        width, _height = self._terminal_size()
        return len(self._approval_rows(request, self._right_width()))

    def _approval_rows(self, request: dict, width: int) -> list:
        """A permission card above the prompt: what, why, and two buttons."""
        agent_name = self._agent_name(str(request.get("agent_id") or ""))
        with self._approval_lock:
            queue_size = len(self._approvals)
        kind = str(request.get("kind") or "action")
        verb = {"command": "wants to run a command", "write": "wants to write a file",
                "delete": "wants to delete a file"}.get(kind, f"wants approval ({kind})")
        border = "class:error" if kind == "delete" else "class:approval.border"
        inner = width - 4
        title = [("class:approval.title", f"{symbols.DOT_HALF} {agent_name} {verb}"),
                 ("class:muted", f" {symbols.BULLET} on {request.get('terminal_name')}")]
        if queue_size > 1:
            title.append(("class:muted", f" {symbols.BULLET} 1 of {queue_size}"))
        body: list = []
        for chunk in agents_transcript.wrap(
                [("class:user.text", str(request.get("summary") or ""))], inner):
            body.append(chunk)
        detail = [line for line in str(request.get("detail") or "").splitlines()
                  if line.strip()][-6:]
        for line in detail:
            for chunk in agents_transcript.wrap([("class:muted", line)], inner,
                                                hard=True)[:2]:
                body.append(chunk)
        body.append([])
        yes_style = "class:button.hover" if self._hover == ("button", "yes") else "class:button.yes"
        no_style = "class:button.no.hover" if self._hover == ("button", "no") else "class:button.no"
        yes, no = " Approve  y ", " Deny  n "
        body.append([(yes_style, yes), ("", "  "), (no_style, no)])
        button_row = len(body)          # row index inside the card (top = 0)
        self._button_hits = [(button_row, 2, 2 + cell_width(yes), "yes"),
                             (button_row, 4 + cell_width(yes),
                              4 + cell_width(yes) + cell_width(no), "no")]
        title_w = cell_width("".join(t for _s, t in title))
        top = [(border, "╭─ ")] + title + [
            (border, " " + "─" * max(0, width - 5 - title_w) + "╮")]
        rows = [_fit(top, width)]
        for line in body:
            rows.append([(border, "│ ")] + _fit(line, inner) + [(border, " │")])
        rows.append([(border, "╰" + "─" * (width - 2) + "╯")])
        return rows

    def approval_lines(self, width: int, height: int) -> list:
        request = self.pending_approval()
        if request is None:
            return [_fit([], width)] * height
        rows = self._approval_rows(request, width)
        return (rows + [_fit([], width)] * height)[:height]

    def _approval_mouse(self, mouse_event):
        x, y = mouse_event.position.x, mouse_event.position.y
        hit = next((name for row, lo, hi, name in self._button_hits
                    if row == y and lo <= x < hi), None)
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            hover = ("button", hit) if hit else None
            if hover != self._hover:
                self._hover = hover
                self.invalidate()
        elif mouse_event.event_type == MouseEventType.MOUSE_UP:
            self._drag = None
            if hit:
                self._hover = None
                self.resolve_approval(hit == "yes")
        return None

    def approval_fragments(self):
        request = self.pending_approval()
        if not request:
            return FormattedText([])
        fragments: list = []
        for row in self._approval_rows(request, 72):
            fragments.extend(row)
            fragments.append(("", "\n"))
        return FormattedText(fragments)

    def _refresh_approval_notice(self, default: str = "") -> None:
        following = self.pending_approval()
        self.notice = (self._approval_notice(following)
                       if following else default)
        self.invalidate()

    def pending_approval(self) -> Optional[dict]:
        with self._approval_lock:
            return self._approvals[0] if self._approvals else None

    def resolve_approval(self, approved: bool) -> None:
        with self._approval_lock:
            if not self._approvals:
                return
            request = self._approvals.pop(0)
            request["approved"] = bool(approved)
            request["done"].set()
        agent_ui_events.hub.emit(
            "approval_resolved", agent_id=request["agent_id"],
            terminal_name=request["terminal_name"], summary=request["summary"],
            status="approved" if approved else "denied",
            data={"approvalId": request["id"], "kind": request["kind"]})
        self._refresh_approval_notice(
            default="Approved" if approved else "Denied")

    def deny_pending_approvals(self, *, close: bool = False,
                               reason: str = "cancelled") -> None:
        with self._approval_lock:
            if close:
                self._closed.set()
            pending, self._approvals = self._approvals, []
            for request in pending:
                request["approved"] = False
                request["done"].set()
        for request in pending:
            agent_ui_events.hub.emit(
                "approval_resolved", agent_id=request["agent_id"],
                terminal_name=request["terminal_name"], summary=request["summary"],
                status="denied",
                data={"approvalId": request["id"],
                      "kind": request["kind"], "reason": reason})
        if pending:
            self.notice = "Denied pending approvals"
            self.invalidate()

    def resolve_agent(self, reference: str) -> str:
        folded = reference.casefold()
        # Search every live Agent scoped to this terminal, not just the rail:
        # the deployed primary is hidden from the rail but still addressable.
        matches = [a.id for a in agent_loop.get_all_agents()
                   if agent_loop.agent_scope_terminal(a) == self.terminal_name
                   and not a.lifecycle_terminated
                   and (a.id.casefold() == folded
                   or str(a.name or "").casefold() == folded)]
        return matches[0] if len(matches) == 1 else ""

    def scroll(self, delta: int, smooth: bool = True) -> None:
        if not self.selected_id:
            return
        agent_id = self.selected_id
        target = max(0, self.focus_scroll[agent_id] + delta)
        total = self._last_total.get(agent_id)
        _w, height = self._viewport[5], self._viewport[6]
        if total is not None:
            target = min(target, max(0, total - height))
        self.focus_scroll[agent_id] = target
        if not smooth:
            self._scroll_pos[agent_id] = float(target)
        self.follow[agent_id] = target == 0
        if target == 0:
            self._unseen_rows[agent_id] = 0
        self.invalidate()

    def page_size(self) -> int:
        return max(1, self._viewport[6] - 2)

    def _step_scroll_animation(self) -> bool:
        """Ease each drawn position toward its target. True while moving."""
        moving = False
        for agent_id, target in list(self.focus_scroll.items()):
            position = self._scroll_pos[agent_id]
            gap = target - position
            if abs(gap) < 0.5:
                if position != target:
                    self._scroll_pos[agent_id] = float(target)
                    moving = True
                continue
            step = gap * 0.35
            if abs(step) < 1:
                step = 1 if gap > 0 else -1
            self._scroll_pos[agent_id] = position + step
            moving = True
        return moving

    def focus_mouse(self, mouse_event):
        return self._transcript_mouse(mouse_event)

    def invalidate(self) -> None:
        try:
            if self.app and self.app.is_running:
                self.app.invalidate()
        except Exception:
            pass

    def on_event(self, event) -> None:
        if (event.terminal_name == self.terminal_name
                and event.event_type in {
                    "agent_done", "agent_error", "agent_aborted"}):
            name = self._agent_name(event.agent_id)
            if event.event_type == "agent_done":
                self.notice = (
                    f"Ready {symbols.BULLET} Enter a message or Esc to return"
                    if event.agent_id == self.selected_id
                    else f"{name} finished")
            elif event.event_type == "agent_aborted":
                self.notice = f"Aborted {name}"
            else:
                self.notice = f"Failed {name}: {self._crop(event.summary, 72)}"
        self.invalidate()

    def run(self, input=None, output=None) -> None:
        with self._approval_lock:
            self._closed.clear()
        kb = KeyBindings()
        input_buffer = Buffer(multiline=False)
        self._input_buffer = input_buffer

        def _remember_draft(buffer):
            if self.selected_id:
                self._drafts[self.selected_id] = buffer.text
        input_buffer.on_text_changed += _remember_draft

        def accept(buffer):
            value = buffer.text
            buffer.text = ""
            if value.strip().casefold() in {"/exit", "/quit", "/q", "/back"}:
                self.notice = "Leaving Agents Mode"
                if self.app is not None:
                    self.app.exit()
                return False
            self.dispatch(value)
            return False
        input_buffer.accept_handler = accept

        @kb.add("tab")
        def _tab(_event):
            self.overlay = not self.overlay
            self.invalidate()

        @kb.add("escape")
        def _escape(event):
            if self._selection:
                self._selection = None
                self.invalidate()
            elif self.overlay:
                self.overlay = False
                self.invalidate()
            else:
                input_buffer.text = ""
                event.app.exit()

        @kb.add("c-q")
        def _quit(event):
            event.app.exit()

        @kb.add("c-c")
        def _copy_or_clear(_event):
            # Never leaves the view: Ctrl+C is copy/cancel here, and a
            # reflexive press must not throw away the screen. Esc goes back.
            text = self._selected_text()
            if text and self._copy_to_clipboard(text):
                self._selection = None
                self.notice = f"Copied {len(text)} characters"
            elif input_buffer.text:
                input_buffer.reset()
            else:
                self.notice = "Esc to go back"
            self.invalidate()

        @kb.add("c-d")
        def _eof_quit(event):
            event.app.exit()

        approval_filter = Condition(lambda: self.pending_approval() is not None)

        @kb.add("y", filter=approval_filter)
        def _approve(_event):
            self.resolve_approval(True)

        @kb.add("n", filter=approval_filter)
        def _deny(_event):
            self.resolve_approval(False)

        @kb.add("q", filter=Condition(lambda: self.overlay))
        def _q(event):
            event.app.exit()

        @kb.add("escape", "up")
        def _prev(_event):
            self.cycle_agent(-1)

        @kb.add("escape", "down")
        def _next(_event):
            self.cycle_agent(1)

        @kb.add("pageup")
        def _page_up(_event):
            self.scroll(self.page_size())

        @kb.add("pagedown")
        def _page_down(_event):
            self.scroll(-self.page_size())

        @kb.add("end")
        def _end(_event):
            self.follow_latest()

        @kb.add("c-o")
        def _expand(_event):
            self._expand_all = not self._expand_all
            self._relayout.update(self._last_total)
            self.notice = ("Showing full tool output" if self._expand_all
                           else "Tool output collapsed")
            self.invalidate()

        @kb.add("c-r")
        def _raw(_event):
            agent_id = self.selected_id
            if not agent_id:
                return
            if not self._raw[agent_id] and self._mirror_lines(agent_id) is None:
                self.notice = ("Raw terminal output is only recorded for the "
                               "main agent")
            else:
                self._raw[agent_id] = not self._raw[agent_id]
                self.notice = ("Raw terminal output" if self._raw[agent_id]
                               else "Transcript view")
            self.invalidate()

        @kb.add("c-p")
        def _profile(_event):
            self._profile_pinned = not self._profile_visible()
            if self._profile_pinned and self._terminal_size()[0] < 100:
                self.notice = "The profile panel needs at least 100 columns"
            self._rows_cache.clear()
            self.invalidate()

        @kb.add("escape", "left")
        def _terminal_prev(_event):
            self.cycle_terminal(-1)

        @kb.add("escape", "right")
        def _terminal_next(_event):
            self.cycle_terminal(1)

        rail_visible = Condition(lambda: self._rail_width() > 0)
        rail = ConditionalContainer(VSplit([
            Window(_PaneControl(self.rail_lines, self._rail_mouse),
                   width=lambda: self._rail_width(), style="class:rail.bg"),
            Window(width=1, char="│", style="class:separator"),
        ]), filter=rail_visible)
        overlay_filter = Condition(lambda: self.overlay)
        transcript = Window(_PaneControl(self.transcript_lines,
                                         self._transcript_mouse))
        panel = ConditionalContainer(VSplit([
            Window(width=1, char="│", style="class:separator"),
            Window(_PaneControl(self.panel_lines), width=self.PANEL_WIDTH,
                   style="class:panel.bg"),
        ]), filter=Condition(self._profile_visible))
        body = HSplit([
            ConditionalContainer(
                Window(_PaneControl(self.rail_lines, self._rail_mouse),
                       style="class:rail.bg"),
                filter=overlay_filter),
            ConditionalContainer(VSplit([transcript, panel]),
                                 filter=~overlay_filter),
        ])

        def working_selected() -> bool:
            agent = agent_loop.get_agent(self.selected_id) if self.selected_id else None
            return bool(agent) and str(getattr(agent, "status", "")) in WORKING

        def border_style() -> str:
            # The prompt's frame breathes while the Agent in focus works:
            # the one place the eye rests is also where "busy" is visible.
            if self.pending_approval() is not None:
                return "class:box"
            if working_selected():
                return ("class:box.active" if int(time.monotonic() / 0.8) % 2 == 0
                        else "class:box.pulse")
            return "class:box"

        def prompt_prefix():
            agent = agent_loop.get_agent(self.selected_id) if self.selected_id else None
            glyph = symbols.ARROW_RR if working_selected() else symbols.INFO
            return FormattedText([("class:input.caret", f"{glyph} ")]) if agent \
                else FormattedText([("class:placeholder", f"{symbols.INFO} ")])

        def placeholder():
            agent = agent_loop.get_agent(self.selected_id) if self.selected_id else None
            if agent is None:
                return FormattedText([("class:placeholder", "No Agent selected")])
            if self.pending_approval() is not None:
                return FormattedText([("class:placeholder",
                                       "Answer the request above first")])
            name = str(agent.name or agent.id)
            if working_selected():
                text = f"Add guidance for {name} while it works…"
            else:
                text = f"Message {name}…   @name routes one message elsewhere"
            # Never wrap: a placeholder that takes two rows grows the prompt.
            room = self._right_width() - 8
            if cell_width(text) > room:
                text = _crop_cells(f"Message {name}…", room)
            return FormattedText([("class:placeholder", text)])

        input_control = BufferControl(
            buffer=input_buffer,
            input_processors=[
                BeforeInput(prompt_prefix),
                ConditionalProcessor(AfterInput(placeholder),
                                     filter=Condition(lambda: not input_buffer.text)),
            ])
        input_window = Window(input_control, wrap_lines=True,
                              height=Dimension(min=1, max=5),
                              dont_extend_height=True)
        prompt_box = HSplit([
            VSplit([Window(width=1, height=1, char="╭", style=border_style),
                    Window(height=1, char="─", style=border_style),
                    Window(width=1, height=1, char="╮", style=border_style)]),
            VSplit([Window(width=1, char="│", style=border_style),
                    Window(width=1),
                    input_window,
                    Window(width=1),
                    Window(width=1, char="│", style=border_style)]),
            VSplit([Window(width=1, height=1, char="╰", style=border_style),
                    Window(height=1, char="─", style=border_style),
                    Window(width=1, height=1, char="╯", style=border_style)]),
        ])
        right = HSplit([
            Window(_PaneControl(self.title_lines, self._title_mouse), height=1),
            Window(height=1, char="─", style="class:separator"),
            body,
            ConditionalContainer(
                Window(_PaneControl(self.approval_lines, self._approval_mouse),
                       height=lambda: Dimension.exact(self._approval_height())),
                filter=approval_filter),
            prompt_box,
            Window(_PaneControl(self.footer_lines), height=1),
        ])
        root = VSplit([rail, right], style="class:root")
        self.app = Application(
            layout=Layout(root, focused_element=input_window),
            key_bindings=kb, style=STYLE, full_screen=True,
            mouse_support=True, refresh_interval=None,
            min_redraw_interval=0.03,
            input=input, output=output)
        agent_ui_events.hub.subscribe(self.on_event)

        def _pre_run():
            async def _animate():
                import asyncio
                while self.app is not None and not self.app.is_done:
                    scrolling = self._step_scroll_animation()
                    toast = bool(self._notice) and (
                        time.monotonic() - self._notice_at
                        <= self.NOTICE_SECONDS + 0.5)
                    active = scrolling or self.pending_approval() is not None or any(
                        str(getattr(agent, "status", "")) in WORKING
                        for agent in self.agents())
                    # Theme following: if the CLI body's palette changed (this
                    # process or another one writing the preference file),
                    # drop the cached style so the DynamicStyle resolver picks
                    # the new palette and repaint the whole view.
                    if _theme_signature() != getattr(self, "_theme_sig", None):
                        self._theme_sig = _theme_signature()
                        if self.app is not None and not self.app.is_done:
                            self.app.invalidate()
                    if scrolling or active or toast:
                        if self.app is not None and not self.app.is_done:
                            self.app.invalidate()
                    # ~30fps while gliding, the spinner's own rate while an
                    # Agent works, and a slow idle tick that lets toasts expire.
                    await asyncio.sleep(0.033 if scrolling else
                                        0.08 if active else 0.5)
            self.app.create_background_task(_animate())
        try:
            self.app.run(pre_run=_pre_run)
        except (KeyboardInterrupt, EOFError):
            # Keep startup/render/teardown races cancellable even before the
            # regular Esc key binding becomes active.
            return
        finally:
            # An interrupt landing inside asyncio's own teardown can leave the
            # running-loop flag set on this thread; every later prompt_toolkit
            # dialog (approval gates especially) would then die in asyncio.run()
            # with "cannot be called from a running event loop".
            try:
                import laintas_cli
                laintas_cli._clear_stale_running_loop()
            except Exception:
                pass
            # Workers may outlive the full-screen application. Close the
            # approval channel first so any later request is denied instead of
            # waiting forever for a UI that no longer exists.
            self.deny_pending_approvals(
                close=True, reason="agents_mode_closed")
            agent_ui_events.hub.unsubscribe(self.on_event)
            self._input_buffer = None
            self.app = None


def run_agents_mode(terminal_name: str, deps, session: dict,
                    external_events_cb: Optional[Callable] = None,
                    primary_submit_cb: Optional[Callable] = None,
                    existing_session=None,
                    execution_block_reason: str = "",
                    repl_submit_cb: Optional[Callable] = None,
                    mirror=None):
    controller = AgentsModeController(
        terminal_name, deps, session,
        external_events_cb=external_events_cb,
        primary_submit_cb=primary_submit_cb,
        existing_session=existing_session,
        execution_block_reason=execution_block_reason,
        repl_submit_cb=repl_submit_cb,
        mirror=mirror)
    controller.run()
    return controller.existing_session
