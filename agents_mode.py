"""Full-screen multi-Agent view: roster, transcript, and a live status band.

The status band is the same row the plain CLI paints while a turn runs —
``L› Thinking… 12.4s · model · MODE`` with the green highlight sweeping
across the verb. It is not a lookalike: the frames and the shimmer come from
``agent_loop._thinking_spinner_frame`` / ``_shimmer_segments``, which exist
precisely so Rich and prompt_toolkit can render one visual language without
either touching the other's cursor region. An Agent that is working looks the
same here as it does at the prompt, because it is the same row.
"""

from __future__ import annotations

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
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

import agent_loop
import agent_ui_events


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
    return styles


STYLE = Style.from_dict({
    "root": "bg:#0d1117 #e6edf3",
    "header": "bold #4ade80",
    "header.brand": "bold #4ade80",
    "pane.title": "bold #8b949e",
    "pane.title.focus": "bold #4ade80",
    "muted": "#8b949e",
    # The relay spinner carries the CLI's accent green wherever it appears.
    "spinner": "bold #3fb950",
    "rail.mark": "bold #4ade80",
    "rail.gutter": "#0d1117",
    "rail.name": "#c9d1d9",
    "rail.name.selected": "bold #f0f6fc",
    "rail.task": "#6e7681",
    "badge": "bold #d29922",
    "key": "#8b949e bold",
    "stream": "#6e7681",
    "rail.selected": "bg:#1f2937 bold #f0f6fc",
    "rail": "#c9d1d9",
    "running": "bold #3fb950",
    "thinking": "bold #d29922",
    "queued": "#8b949e",
    "waiting": "#d29922",
    "done": "#3fb950",
    "error": "bold #f85149",
    "idle": "#8b949e",
    "separator": "#30363d",
    "agent": "bold #a78bfa",
    "user": "bold #f0f6fc",
    "tool": "#d2a8ff",
    "message": "#d2a8ff",
    "input": "bold #4ade80",
    "input.caret": "#3fb950",
    "feed.time": "#6e7681",
    "feed.agent": "#a78bfa",
    "feed.text": "#b1bac4",
    "inspector.label": "#8b949e",
    "inspector.value": "#e6edf3",
    "approval": "bold #e3b341",
    **_panel_md_styles(),
})


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
                 mirror=None):
        self.terminal_name = terminal_name or "term0"
        self.deps = deps
        self.session = session or {}
        self.external_events_cb = external_events_cb
        self.primary_submit_cb = primary_submit_cb
        # repl_submit_cb forwards a dialogue message to the outer REPL loop —
        # the single executor. When set, this view never runs the primary
        # itself; input always means "talk to this Agent".
        self.repl_submit_cb = repl_submit_cb
        # mirror: per-Agent ANSI scrollback of the real REPL output
        # (repl_mirror.MirrorHub). Focus renders it verbatim when available.
        self.mirror = mirror
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
        self.focus_scroll: dict[str, int] = defaultdict(int)
        self.follow: dict[str, bool] = defaultdict(lambda: True)
        self.read_seq: dict[str, int] = defaultdict(int)
        # Empty until something has happened. The key hints live on their own
        # row; repeating them here spent a line on what was already on screen.
        self.notice = ""
        self._approval_lock = threading.Lock()
        self._approvals: list[dict] = []
        self._closed = threading.Event()
        self._last_agents: list = []
        self._event_lines_cache: dict[str, tuple[tuple[int, int, int], tuple]] = {}
        self._feed_cache: tuple[tuple[int, int], FormattedText] | None = None
        self._drafts: dict[str, str] = defaultdict(str)
        self._input_buffer: Optional[Buffer] = None
        # When each Agent started its current stretch of work, so the status
        # row can show an elapsed clock. Kept here rather than read off the
        # Agent because a primary has no assignment to carry one, and a clock
        # that only some Agents have is worse than none.
        self._work_since: dict[str, float] = {}
        selected = agent_loop.get_dialog_agent_for_terminal(self.terminal_name)
        if selected is not None and agent_loop.agent_deployment_terminal(selected) == self.terminal_name:
            selected = None
        self.selected_id = selected.id if selected else ""

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

    def _focus_body_height(self) -> tuple[int, int]:
        """Return (width, physical rows available below the Focus header)."""
        width, height = self._terminal_size()
        # Root layout rows outside `main`: header + its separator, the input
        # and notice rows, the optional hint row, and the status band with its
        # separator. Focus itself adds a two-row title/divider. Keeping this
        # aligned with run() prevents prompt_toolkit from clipping the newest
        # body row at the bottom.
        reserved = (
            1                       # header
            + 1                     # header rule
            + 1                     # input
            + 2                     # Focus's own title and rule
        )
        if self.notice:
            reserved += 1
        if height >= 16:            # key hints
            reserved += 1
        if height >= 14:            # status band and its rule
            reserved += 3
        return self._focus_pane_width(), max(3, height - reserved)

    def _focus_pane_width(self) -> int:
        """Columns the transcript actually gets, not the terminal's.

        The panes beside it are fixed-width and conditional; wrapping to the
        terminal instead let long lines run under the rail, which reads as
        corruption rather than as an overflow.
        """
        width, _height = self._terminal_size()
        pane = width
        if width >= 96:                       # rail + its separator
            pane -= 31
        if (width >= 140 and self.pending_approval() is None):
            pane -= 30                        # inspector + its separator
        if self.pending_approval() is not None and width >= 120:
            pane -= 43                        # approval side + its separator
        return max(20, pane)

    def _rail_page_size(self) -> int:
        _width, height = self._terminal_size()
        return max(1, (height - 9) // 3)

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
        self.selected_id = candidate.id if candidate else ""
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

    def _rail_subtitle(self, agent, status: str) -> str:
        """One line under an idle Agent's name: outcome first, then task."""
        if status in {"error", "aborted"}:
            return str(getattr(agent, "error", "") or "failed")
        task = self._current_task(agent)
        if status in {"done", "ready"}:
            return f"done {symbols.BULLET} {task}" if task != "idle" else "done"
        return task

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

    def rail_fragments(self):
        fragments = []
        agents = self.agents()
        event_rows = {
            agent.id: agent_ui_events.hub.agent_events(agent.id)
            for agent in agents
        }
        page = self._rail_page_size()
        max_offset = max(0, len(agents) - page)
        self.rail_offset = min(max(0, self.rail_offset), max_offset)
        visible = agents[self.rail_offset:self.rail_offset + page]
        if self.rail_offset:
            fragments.append((f"class:muted", f"  {symbols.ARROW_U} more Agents\n"))
        for agent in visible:
            selected = agent.id == self.selected_id
            status = self._display_status(agent, event_rows[agent.id])
            working = status in WORKING
            unread = self.unread(agent.id, event_rows[agent.id])
            name_style = ("class:rail.name.selected" if selected
                          else "class:rail.name")

            def handler(mouse_event, agent_id=agent.id):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.select(agent_id)
                elif mouse_event.event_type == MouseEventType.SCROLL_UP:
                    self.scroll_rail(-1)
                elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                    self.scroll_rail(1)

            # A bar in the gutter marks the selection. Reversing the whole row
            # instead makes the rail flash on every redraw of an animated
            # neighbour, and buries the status colour that carries the meaning.
            gutter = ("class:rail.mark", f"{symbols.TREE_VERT} ") if selected \
                else ("class:rail.gutter", "  ")
            fragments.append((*gutter, handler))
            if working:
                fragments.append(
                    ("class:spinner",
                     _spinner_frame(self._working_elapsed(agent)) + " ",
                     handler))
            else:
                icon, status_style = STATUS.get(
                    status, (symbols.DOT_OPEN, "class:idle"))
                fragments.append((status_style, f"{icon}  ", handler))
            fragments.append((name_style, self._crop(
                agent.name or agent.id, RAIL_WIDTH - 9), handler))
            if unread:
                fragments.append(("class:badge", f" {unread}", handler))
            fragments.append(("", "\n", handler))

            # Second line: what it is doing, in the same words the status band
            # uses — an idle Agent says so rather than showing a stale task.
            # Under the name, not under the gutter: gutter (2) + status (3).
            indent = " " * 5
            # The bar continues down the second row: a mark that covers only
            # the name makes the two rows look like different entries.
            fragments.append((*gutter, handler))
            if working:
                elapsed = self._working_elapsed(agent)
                fragments.append(("class:muted", indent[2:], handler))
                fragments.extend(
                    (style, text, handler)
                    for style, text in _shimmer_fragments(
                        self._crop(self._work_verb(agent),
                                   RAIL_WIDTH - len(indent) - 6), elapsed))
                fragments.append(
                    ("class:muted", f" {elapsed:.0f}s\n", handler))
            else:
                fragments.append((
                    "class:rail.task",
                    indent[2:] + self._crop(self._rail_subtitle(agent, status),
                                        RAIL_WIDTH - len(indent) - 1)
                    + "\n", handler))
            fragments.append(("", "\n", handler))
        if self.rail_offset + page < len(agents):
            fragments.append((f"class:muted", f"  {symbols.ARROW_D} more Agents\n"))
        if not fragments:
            fragments.append(("class:muted", " No Agents in this terminal\n"))
        return FormattedText(fragments)

    def scroll_rail(self, delta: int) -> None:
        agents = self.agents()
        maximum = max(0, len(agents) - self._rail_page_size())
        self.rail_offset = min(maximum, max(0, self.rail_offset + delta))
        self.invalidate()

    @staticmethod
    def _crop(text: str, width: int) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= width else text[:max(1, width - 1)] + "…"

    def _event_lines(self, agent_id: str) -> list[tuple[str, str]]:
        _revision, events = agent_ui_events.hub.agent_events_snapshot(
            agent_id, limit=1500)
        agent = agent_loop.get_agent(agent_id)
        history_size = len(getattr(agent, "chat_history", []) or []) if agent else 0
        cache_key = (events[-1].seq if events else 0, len(events), history_size)
        cached = self._event_lines_cache.get(agent_id)
        if cached is not None and cached[0] == cache_key:
            return list(cached[1])
        lines: list[tuple[str, str]] = []
        stream = ""
        active_tools: dict[str, int] = {}

        def append_markdown(text: str) -> None:
            fenced = False
            for raw_line in str(text or "").splitlines():
                stripped = raw_line.strip()
                if stripped.startswith("```"):
                    fenced = not fenced
                    if stripped[3:].strip():
                        lines.append(("class:md.codeblock", stripped[3:].strip()))
                    continue
                if fenced:
                    style = "class:md.codeblock"
                elif re.match(r"^#{1,2}\s+", stripped):
                    level = 1 if stripped.startswith("# ") else 2
                    style = f"class:md.h{level}"
                    raw_line = re.sub(r"^#{1,2}\s+", "", stripped)
                elif re.match(r"^(?:[-*_]\s*){3,}$", stripped):
                    style, raw_line = "class:separator", "─" * 40
                elif stripped.startswith(">"):
                    style, raw_line = "class:md.quote", stripped[1:].lstrip()
                elif re.match(r"^(?:[-+*]|\d+[.)])\s+", stripped):
                    style = "class:md.list"
                else:
                    style = ""
                lines.append((style, raw_line))

        def flush_stream() -> None:
            nonlocal stream
            if not stream:
                return
            lines.append(("class:agent", self._agent_name(agent_id)))
            append_markdown(stream)
            lines.append(("", ""))
            stream = ""

        for event in events:
            kind = event.event_type
            if kind == "ai_stream":
                stream += event.detail
                continue
            if kind == "ai_end":
                flush_stream()
                continue
            flush_stream()
            if kind in {"user", "user_message"}:
                lines.append(("class:user", "You"))
                lines.extend(("", line) for line in (event.detail or event.summary).splitlines())
            elif kind == "agent_message":
                outgoing = event.agent_id == agent_id
                peer = event.target_agent_id if outgoing else event.agent_id
                arrow = "↗" if outgoing else "↙"
                lines.append(("class:message", f"{arrow} {peer}"))
                lines.extend(("", line) for line in (event.detail or event.summary).splitlines())
            elif kind == "agent_spawned":
                parent = self._agent_name(event.parent_agent_id)
                lines.append(("class:message", f"↙ task from {parent}"))
                lines.extend(("", line) for line in
                             (event.detail or event.summary).splitlines())
            elif kind in {"agent_started", "workflow_started"}:
                # The accepted user message already represents this task in
                # Focus. Startup is status metadata, not a second chat line.
                continue
            elif kind == "input_rejected":
                lines.append(("class:error", f"{symbols.FAIL} {event.summary}"))
            elif kind == "approval_requested":
                lines.append((f"class:thinking", f"{symbols.DOT_HALF} Approval required"))
                lines.append(("", event.summary))
                lines.extend(("class:muted", line)
                             for line in event.detail.splitlines()[-12:])
            elif kind == "approval_resolved":
                approved = event.status == "approved"
                lines.append(("class:done" if approved else "class:error",
                              f"{symbols.OK} Approved" if approved else f"{symbols.FAIL} Denied"))
            elif kind == "agent_done":
                # The answer immediately above is the completion state. A
                # quiet divider separates turns without adding a fake result.
                lines.append(("class:separator", "─" * 24))
            elif kind == "agent_aborted":
                lines.append((f"class:error", f"{symbols.FAIL} Task aborted"))
                if event.detail:
                    lines.extend(("class:muted", line)
                                 for line in event.detail.splitlines()[-12:])
            elif kind == "ai":
                text = event.detail or event.summary
                lines.append(("class:agent", self._agent_name(agent_id)))
                append_markdown(text)
            elif kind == "tool_output":
                lines.extend(("class:muted", "  " + line)
                             for line in event.detail.splitlines()[-12:])
            elif kind == "tool_started":
                lines.append(("class:tool", f"{symbols.DOT_HALF} {event.summary}"))
                if event.tool_call_id:
                    active_tools[event.tool_call_id] = len(lines) - 1
            elif kind == "tool_finished" and event.tool_call_id in active_tools:
                lines[active_tools.pop(event.tool_call_id)] = (
                    "class:tool", f"{symbols.DOT} {event.summary}")
                # The start event already owns spacing and chronology.
                continue
            elif kind in {"system", "tool", "tool_finished"}:
                symbol = f"{symbols.DOT_HALF}" if kind == "tool_started" else f"{symbols.DOT}"
                lines.append(("class:tool", f"{symbol} {event.summary}"))
                if event.detail and event.detail != event.summary:
                    lines.extend(("class:muted", "  " + line)
                                 for line in event.detail.splitlines()[-12:])
            elif kind not in {"stream.reset", "stream.end"}:
                symbol = f"{symbols.FAIL}" if "fail" in kind or "error" in kind else f"{symbols.BULLET}"
                style = f"class:error" if symbol == f"{symbols.FAIL}" else "class:muted"
                lines.append((style, f"{symbol} {event.summary or kind}"))
            if lines and lines[-1][1] != "":
                lines.append(("", ""))
        flush_stream()
        if not lines:
            for message in (getattr(agent, "chat_history", []) or [])[-30:]:
                role = str(message.get("role") or "")
                label = "You" if role == "user" else self._agent_name(agent_id)
                lines.append(("class:user" if role == "user" else "class:agent", label))
                if role == "user":
                    lines.extend(("", line) for line in str(
                        message.get("content") or "").splitlines())
                else:
                    append_markdown(str(message.get("content") or ""))
                lines.append(("", ""))
        self._event_lines_cache[agent_id] = (cache_key, tuple(lines))
        return list(lines)

    def _agent_name(self, agent_id: str) -> str:
        agent = agent_loop.get_agent(agent_id)
        return str(agent.name or agent.id) if agent else agent_id

    @staticmethod
    def _inline_markdown(style: str, text: str):
        if style in {"class:md.codeblock", "class:separator"}:
            return [(style, text)]
        pattern = re.compile(
            r"(`[^`]+`|\*\*[^*]+\*\*|(?<!\*)\*[^*]+\*(?!\*)|"
            r"\[[^\]]+\]\([^)]+\))")
        fragments = []
        position = 0
        for match in pattern.finditer(text):
            if match.start() > position:
                fragments.append((style, text[position:match.start()]))
            token = match.group(0)
            if token.startswith("`"):
                fragments.append(("class:md.code", token[1:-1]))
            elif token.startswith("**"):
                fragments.append(("class:md.bold", token[2:-2]))
            elif token.startswith("*"):
                fragments.append(("class:md.italic", token[1:-1]))
            else:
                label, url = re.match(r"\[([^]]+)\]\(([^)]+)\)", token).groups()
                fragments.append(("class:md.link", f"{label} ({url})"))
            position = match.end()
        fragments.append((style, text[position:]))
        return fragments

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
        """``model · MODE`` for the status row's tail, best-effort."""
        parts = []
        try:
            model, _provider = agent_loop.resolve_agent_model(agent)
            model = str(model or "") or agent_loop._live_status_model()
            if model:
                parts.append(model.split("/")[-1])
        except Exception:
            pass
        try:
            mode = str(agent_loop._active_mode_label() or "")
            if mode:
                parts.append(mode)
        except Exception:
            pass
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

    def _tail_mirror_rows(self, lines: list[str], width: int,
                          row_limit: int) -> list[list[tuple]]:
        """Return at most `row_limit` wrapped physical rows from the tail.

        Work backwards so the 0.1s Agents Mode refresh never reparses the
        whole 4000-line mirror merely to display the last screenful.
        """
        row_limit = max(1, int(row_limit))
        reverse_rows: list[list[tuple]] = []
        for line in reversed(lines):
            try:
                formatted = list(to_formatted_text(ANSI(line)))
            except Exception:
                formatted = [("", line)]
            wrapped = self._wrap_formatted_rows(formatted, width)
            for row in reversed(wrapped):
                reverse_rows.append(row)
                if len(reverse_rows) >= row_limit:
                    return list(reversed(reverse_rows))
        return list(reversed(reverse_rows))

    def _focus_mirror_fragments(self, agent_id: str, lines: list[str]):
        # No activity row here: the status band below the transcript owns
        # "what is happening now", and showing it twice on one screen made
        # each copy look like a different fact.
        width, height = self._focus_body_height()
        offset = max(0, self.focus_scroll[agent_id])
        # `focus_scroll` is a physical-row offset. Long/CJK/ANSI lines are
        # wrapped before slicing so follow=0 really means the visible bottom,
        # rather than "the last N logical lines, clipped halfway on screen".
        physical_rows = self._tail_mirror_rows(
            lines, max(1, width - 2), height + offset)
        end = max(0, len(physical_rows) - offset)
        start = max(0, end - height)
        visible = physical_rows[start:end]
        if self.follow[agent_id]:
            events = agent_ui_events.hub.agent_events(agent_id)
            if events:
                self.read_seq[agent_id] = events[-1].seq
        fragments = self._focus_header(agent_id, width)
        fragments.extend(self._bottom_pad(len(visible), height))
        for row in visible:
            fragments.append(("", " "))
            fragments.extend((style, value) for style, value, *_rest in row)
            fragments.append(("", "\n"))
        return FormattedText(fragments)

    @staticmethod
    def _bottom_pad(used: int, height: int) -> list:
        """Blank rows above the content so a short transcript sits on the
        floor of the pane, where the newest line is always in the same place
        — right above the status band the eye is already on."""
        return [("", "\n" * max(0, height - used))] if height > used else []

    def _focus_header(self, agent_id: str, width: int) -> list:
        """Name, role and scroll position — the transcript's own title bar."""
        name = self._crop(self._agent_name(agent_id), max(1, width - 20))
        agent = agent_loop.get_agent(agent_id)
        role = str(getattr(agent, "role", "") or "") if agent else ""
        head = [("class:header", f"  {name}")]
        if role and role != "primary":
            head.append(("class:muted", f"  {role}"))
        offset = self.focus_scroll[agent_id]
        if offset:
            head.append(("class:badge", f"  {symbols.ARROW_U}{offset}"))
        head.append(("", "\n"))
        head.append(("class:separator",
                     " " + "─" * max(1, width - 2) + "\n"))
        return head

    def focus_fragments(self):
        agent_id = self.selected_id
        if not agent_id:
            return FormattedText([
                ("class:muted", "\n  No Agents in this terminal\n\n"),
                ("class:muted", "  Hire one with "),
                ("class:key", "/hire <name>"),
                ("class:muted", ", or press "),
                ("class:key", "Esc"),
                ("class:muted", " to leave.\n"),
            ])
        mirror_lines = self._mirror_lines(agent_id)
        if mirror_lines is not None:
            return self._focus_mirror_fragments(agent_id, mirror_lines)
        lines = self._event_lines(agent_id)
        width, height = self._focus_body_height()
        offset = max(0, self.focus_scroll[agent_id])
        end = max(0, len(lines) - offset)
        start = max(0, end - height)
        visible = lines[start:end]
        if self.follow[agent_id]:
            events = agent_ui_events.hub.agent_events(agent_id)
            if events:
                self.read_seq[agent_id] = events[-1].seq
        fragments = self._focus_header(agent_id, width)
        fragments.extend(self._bottom_pad(len(visible), height))
        for style, line in visible:
            fragments.append((style, "  "))
            fragments.extend(self._inline_markdown(style, line))
            fragments.append((style, "\n"))
        return FormattedText(fragments)

    def band_fragments(self):
        """The live band above the input: what is happening, right now.

        One row for the Agent in focus, in the CLI's own status language, and
        one dim row of the text it is producing. When focus is idle the band
        reports the other Agents still working, so leaving a worker and going
        to talk to someone else never means losing sight of it.
        """
        width, _height = self._terminal_size()
        rows: list = []
        status = self._status_fragments(self.selected_id, width)
        if status:
            rows.append([("class:muted", " ")] + status)
            tail = self._stream_tail(self.selected_id)
            if tail:
                rows.append([("class:stream", " " + symbols.INFO + " "),
                             ("class:stream", self._crop(tail, width - 4))])
        else:
            elsewhere = [
                agent for agent in self.agents()
                if agent.id != self.selected_id
                and self._display_status(agent) in WORKING]
            if elsewhere:
                row = [("class:muted", " ")]
                for index, agent in enumerate(elsewhere[:3]):
                    if index:
                        row.append(("class:muted", f"  {symbols.BULLET}  "))
                    row.append(("class:spinner", _spinner_frame(
                        self._working_elapsed(agent)) + " "))
                    row.append(("class:rail.name", str(agent.name or agent.id)))
                    row.append(("class:muted",
                                f" {self._working_elapsed(agent):.0f}s"))
                if len(elsewhere) > 3:
                    row.append(("class:muted",
                                f"  +{len(elsewhere) - 3} more"))
                rows.append(row)
            else:
                rows.append([("class:muted", f" {self._agent_name(self.selected_id)} is idle"
                              if self.selected_id else " No Agents here")])

        fragments = []
        for index, row in enumerate(rows):
            if index:
                fragments.append(("", "\n"))
            fragments.extend(row)
        while sum(1 for _s, text in fragments if "\n" in text) < 1:
            fragments.append(("", "\n"))
        return FormattedText(fragments)

    def hint_fragments(self):
        """Keys that apply to what is on screen, not a fixed keycap dump."""
        if self.pending_approval() is not None:
            keys = [("y", "approve"), ("n", "deny"), ("Esc", "exit")]
        elif self.overlay:
            keys = [("Alt+" + symbols.ARROW_U + symbols.ARROW_D, "pick"),
                    ("Tab", "close"), ("Esc", "exit")]
        else:
            keys = [("Enter", "send"),
                    ("@name", "one-shot"),
                    ("Alt+" + symbols.ARROW_U + symbols.ARROW_D, "agent"),
                    ("Alt+" + symbols.ARROW_L + symbols.ARROW_R, "terminal"),
                    ("Esc", "exit")]
        # Drop from the right as the terminal narrows: the first hints are the
        # ones a person needs, and a row that wraps costs a whole line.
        width, _height = self._terminal_size()
        fragments: list = []
        used = 0
        for index, (key, what) in enumerate(keys):
            lead = f"  {symbols.BULLET}  " if index else " "
            cost = get_cwidth(lead + key + " " + what)
            if used + cost > width - 1:
                break
            fragments.extend([
                ("class:muted", lead),
                ("class:key", key),
                ("class:muted", f" {what}"),
            ])
            used += cost
        return FormattedText(fragments)

    def feed_fragments(self):
        _revision, events = agent_ui_events.hub.events_snapshot(
            self.terminal_name, limit=100)
        cache_key = (events[-1].seq if events else 0, len(events))
        if self._feed_cache is not None and self._feed_cache[0] == cache_key:
            return self._feed_cache[1]
        rows = []
        ignored = {
            "ai_stream", "ai_end", "stream.reset", "stream.end", "user", "ai",
            "user_message", "user_broadcast", "tool_output", "tool_started",
            "agent_done",
        }
        for event in events:
            if event.event_type in ignored:
                continue
            when = time.strftime("%H:%M", time.localtime(event.timestamp))
            if event.event_type == "agent_message":
                actor = f"{event.agent_id or 'user'} → {event.target_agent_id}"
            else:
                actor = event.agent_id or event.event_type
            rows.append((when, actor, event.summary or event.event_type, event))
        fragments = []
        for when, actor, summary, event in rows[-4:]:
            def handler(mouse_event, target=event.target_agent_id or event.agent_id):
                if target and mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.select(target)
            fragments.extend([
                ("class:feed.time", f" {when} ", handler),
                ("class:feed.agent", self._crop(actor, 22) + "  ", handler),
                ("class:feed.text", self._crop(summary, 80) + "\n", handler),
            ])
        while sum(1 for _s, text, *_rest in fragments if text.endswith("\n")) < 4:
            fragments.append(("", "\n"))
        rendered = FormattedText(fragments)
        self._feed_cache = (cache_key, rendered)
        return rendered

    def inspector_fragments(self):
        """Compact operational context for wide terminals."""
        agent = agent_loop.get_agent(self.selected_id) if self.selected_id else None
        if agent is None:
            return FormattedText([("class:muted", "\n  No Agent selected\n")])
        _revision, events = agent_ui_events.hub.agent_events_snapshot(
            agent.id, limit=300)
        status = self._display_status(agent, events)
        tool_ids = {event.tool_call_id for event in events
                    if event.tool_call_id and event.event_type in {
                        "tool", "tool_started", "tool_finished"}}
        anonymous_tools = sum(
            not event.tool_call_id and event.event_type in {
                "tool", "tool_finished"} for event in events)
        tools = len(tool_ids) + anonymous_tools
        messages = sum(event.event_type in {
            "user", "user_message", "ai", "ai_end"} for event in events)
        approvals = sum(event.event_type == "approval_requested" for event in events)
        latest = next((event for event in reversed(events)
                       if event.summary), None)
        rows = [
            ("class:pane.title", "  CONTEXT\n"),
            ("class:separator", "  ───────────────────────\n"),
            ("class:inspector.label", "  STATUS\n"),
            (STATUS.get(status, (symbols.DOT_OPEN, "class:idle"))[1],
             f"  {STATUS.get(status, (symbols.DOT_OPEN, ''))[0]} {status.upper()}\n\n"),
            ("class:inspector.label", "  TASK\n"),
            ("class:inspector.value", "  " + self._crop(
                self._current_task(agent), 25) + "\n\n"),
            ("class:inspector.label", "  RUNTIME\n"),
            ("class:inspector.value", f"  role       {agent.role}\n"),
            ("class:inspector.value", f"  terminal   {self.terminal_name}\n"),
            ("class:inspector.value", f"  messages   {messages}\n"),
            ("class:inspector.value", f"  tools      {tools}\n"),
            ("class:inspector.value", f"  approvals  {approvals}\n"),
        ]
        if latest is not None:
            rows.extend([
                ("class:inspector.label", "\n  LATEST\n"),
                ("class:muted", "  " + self._crop(
                    latest.summary, 25) + "\n"),
            ])
        return FormattedText(rows)

    def header_fragments(self):
        """Identity on the left, a census of the roster on the right.

        The census is the reason to look up here: how many Agents are working
        and how many are waiting on you. Both are counts a person acts on;
        neither is visible from the transcript alone.
        """
        agents = self.agents()
        statuses = {a.id: self._display_status(a) for a in agents}
        working = sum(statuses[a.id] in WORKING for a in agents)
        attention = sum(self.unread(a.id) > 0 for a in agents)
        width, _height = self._terminal_size()

        left = [("class:header.brand", f" {self.terminal_name}")]
        if self.selected_id:
            left.extend([
                ("class:separator", f"  {symbols.TREE_VERT}  "),
                ("class:agent", self._agent_name(self.selected_id)),
            ])
        right = []
        if working:
            right.append(("class:running", f"{working} working"))
        if attention:
            if right:
                right.append(("class:muted", f"  {symbols.BULLET}  "))
            right.append(("class:badge", f"{attention} for you"))
        if not right:
            right = [("class:muted", "all idle")]

        used = sum(get_cwidth(text) for _style, text in left + right)
        gap = max(2, width - used - 1)
        return FormattedText(left + [("", " " * gap)] + right)

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
            if agent.role == "primary":
                if callable(self.repl_submit_cb):
                    # The outer REPL loop runs this as a dialogue message for
                    # the Agent; this view only displays the mirrored result.
                    ok, detail = self.repl_submit_cb(text)
                    self.notice = detail or "Sent"
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
                self.notice = (f"Started {agent.name or agent.id}" if ok else detail)
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

    def approval_fragments(self):
        request = self.pending_approval()
        if not request:
            return FormattedText([])
        agent_name = self._agent_name(str(request.get("agent_id") or ""))
        with self._approval_lock:
            queue_size = len(self._approvals)
        risk = "DESTRUCTIVE" if request.get("kind") == "delete" else "CHANGES SYSTEM"
        fragments = [
            ("class:approval", f"\n  APPROVAL  1/{queue_size}\n"),
            ("class:separator", "  ──────────────────────────────\n\n"),
            ("class:agent", f"  Agent: {agent_name}\n"),
            ("class:muted", f"  Terminal: {request.get('terminal_name')}\n"),
            ("class:tool", f"  Type: {request.get('kind')}\n\n"),
            ("class:error" if request.get("kind") == "delete" else "class:thinking",
             f"  Risk: {risk}\n\n"),
            ("class:user", f"  {request.get('summary')}\n"),
        ]
        detail = str(request.get("detail") or "").splitlines()[-20:]
        fragments.extend(("", f"  {line}\n") for line in detail)
        fragments.extend([
            ("", "\n"),
            ("class:done", "  [y] Approve    "),
            ("class:error", "[n] Deny\n"),
        ])
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
        matches = [a.id for a in self.agents()
                   if a.id.casefold() == folded
                   or str(a.name or "").casefold() == folded]
        return matches[0] if len(matches) == 1 else ""

    def scroll(self, delta: int) -> None:
        if not self.selected_id:
            return
        self.focus_scroll[self.selected_id] = max(
            0, self.focus_scroll[self.selected_id] + delta)
        self.follow[self.selected_id] = self.focus_scroll[self.selected_id] == 0
        self.invalidate()

    def focus_mouse(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.scroll(2)
        elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.scroll(-2)

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
                self.notice = f"Ready {symbols.BULLET} Enter a message or Esc to return"
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
            if self.overlay:
                self.overlay = False
                self.invalidate()
            else:
                input_buffer.text = ""
                event.app.exit()

        @kb.add("c-q")
        def _quit(event):
            event.app.exit()

        @kb.add("c-c")
        def _cancel_or_quit(event):
            input_buffer.text = ""
            event.app.exit()

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
            self.scroll(10)

        @kb.add("pagedown")
        def _page_down(_event):
            self.scroll(-10)

        @kb.add("end")
        def _end(_event):
            if self.selected_id:
                self.focus_scroll[self.selected_id] = 0
                self.follow[self.selected_id] = True
            self.invalidate()

        @kb.add("escape", "left")
        def _terminal_prev(_event):
            self.cycle_terminal(-1)

        @kb.add("escape", "right")
        def _terminal_next(_event):
            self.cycle_terminal(1)

        rail = Window(FormattedTextControl(self.rail_fragments),
                      width=RAIL_WIDTH,
                      wrap_lines=False, style="class:rail")
        focus = Window(FormattedTextControl(
            lambda: FormattedText([
                (style, text, self.focus_mouse)
                for style, text in self.focus_fragments()
            ])), wrap_lines=True)
        wide_filter = Condition(lambda: self._terminal_size()[0] >= 96)
        inspector_filter = Condition(lambda: self._terminal_size()[0] >= 140)
        approval_wide_filter = Condition(
            lambda: self.pending_approval() is not None
            and self._terminal_size()[0] >= 120)
        wide_rail = ConditionalContainer(rail, filter=wide_filter)
        wide_separator = ConditionalContainer(
            Window(width=1, char="│", style="class:separator"),
            filter=wide_filter)
        inspector = ConditionalContainer(
            Window(FormattedTextControl(self.inspector_fragments),
                   width=29, wrap_lines=False, style="class:root"),
            filter=inspector_filter & ~approval_filter)
        inspector_separator = ConditionalContainer(
            Window(width=1, char="│", style="class:separator"),
            filter=inspector_filter & ~approval_filter)
        approval_separator = ConditionalContainer(
            Window(width=1, char="│", style="class:separator"),
            filter=approval_wide_filter)
        approval_side = ConditionalContainer(
            Window(FormattedTextControl(self.approval_fragments),
                   width=42, wrap_lines=True, style="class:root"),
            filter=approval_wide_filter)
        main = VSplit([
            wide_rail,
            wide_separator,
            focus,
            inspector_separator,
            inspector,
            approval_separator,
            approval_side,
        ])
        overlay_filter = Condition(lambda: self.overlay)
        approval_view = ConditionalContainer(
            Window(FormattedTextControl(self.approval_fragments),
                   wrap_lines=True),
            filter=approval_filter & ~approval_wide_filter)
        overlay_rail = ConditionalContainer(
            Window(FormattedTextControl(self.rail_fragments), wrap_lines=False),
            filter=overlay_filter & ~approval_filter)
        input_control = BufferControl(
            buffer=input_buffer,
            input_processors=[BeforeInput(
                lambda: FormattedText([
                    ("class:input", f" {self._agent_name(self.selected_id)} "),
                    ("class:input.caret", f"{symbols.INFO} ")]))])
        band_filter = Condition(lambda: self._terminal_size()[1] >= 14)
        verbose_footer_filter = Condition(lambda: self._terminal_size()[1] >= 16)
        root = HSplit([
            Window(FormattedTextControl(self.header_fragments), height=1),
            Window(height=1, char="─", style="class:separator"),
            approval_view,
            overlay_rail,
            ConditionalContainer(
                main, filter=~overlay_filter & (
                    ~approval_filter | approval_wide_filter)),
            # The live band sits directly above the input, where the eye
            # already is while typing — the same place the plain CLI paints
            # its status row, for the same reason.
            ConditionalContainer(
                Window(height=1, char="─", style="class:separator"),
                filter=band_filter),
            ConditionalContainer(
                Window(FormattedTextControl(self.band_fragments), height=2),
                filter=band_filter),
            Window(input_control, height=1),
            ConditionalContainer(
                Window(FormattedTextControl(
                    lambda: FormattedText(
                        [("class:muted", " " + self.notice)])), height=1),
                filter=Condition(lambda: bool(self.notice))),
            ConditionalContainer(
                Window(FormattedTextControl(self.hint_fragments), height=1),
                filter=verbose_footer_filter),
        ])
        self.app = Application(
            layout=Layout(root, focused_element=input_control),
            key_bindings=kb, style=STYLE, full_screen=True,
            mouse_support=True, refresh_interval=None,
            min_redraw_interval=0.05,
            input=input, output=output)
        agent_ui_events.hub.subscribe(self.on_event)

        def _pre_run():
            async def _animate_live_work():
                import asyncio
                while self.app is not None and not self.app.is_done:
                    active = self.pending_approval() is not None or any(
                        str(getattr(agent, "status", "")) in {
                            "running", "thinking", "queued", "waiting"}
                        for agent in self.agents())
                    # The relay spinner advances every 140ms and the
                    # highlight band sweeps continuously; redrawing slower
                    # than the frame interval turns both into a stutter.
                    await asyncio.sleep(0.08 if active else 0.8)
                    if active and self.app is not None and not self.app.is_done:
                        self.app.invalidate()
            self.app.create_background_task(_animate_live_work())
        try:
            self.app.run(pre_run=_pre_run)
        except (KeyboardInterrupt, EOFError):
            # Keep startup/render/teardown races cancellable even before the
            # regular Esc/Ctrl+C key bindings become active.
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
