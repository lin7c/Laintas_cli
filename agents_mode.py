"""Full-screen Focus + Agent Rail + Event Feed interface."""

from __future__ import annotations

from collections import defaultdict
import copy
import queue
import re
import shutil
import threading
import time
from typing import Callable, Optional

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


STATUS = {
    "running": ("●", "class:running"),
    "thinking": ("◐", "class:thinking"),
    "queued": ("◌", "class:queued"),
    "waiting": ("○", "class:waiting"),
    "done": ("✓", "class:done"),
    "ready": ("✓", "class:done"),
    "error": ("✗", "class:error"),
    "aborted": ("✗", "class:error"),
    "idle": ("○", "class:idle"),
}

STYLE = Style.from_dict({
    "header": "bold #58a6ff",
    "muted": "#8b949e",
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
    "agent": "bold #79c0ff",
    "user": "bold #f0f6fc",
    "tool": "#a5d6ff",
    "message": "#d2a8ff",
    "input": "bold #3fb950",
    "feed.time": "#6e7681",
    "feed.agent": "#79c0ff",
    "feed.text": "#b1bac4",
    "md.h1": "bold #f0f6fc",
    "md.h2": "bold #d2a8ff",
    "md.bold": "bold #f0f6fc",
    "md.italic": "italic #c9d1d9",
    "md.code": "bg:#161b22 #ffa657",
    "md.codeblock": "bg:#161b22 #c9d1d9",
    "md.link": "underline #58a6ff",
    "md.quote": "italic #8b949e",
    "md.list": "#d2a8ff",
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
        self.app: Optional[Application] = None
        self.overlay = False
        self.rail_offset = 0
        self.focus_scroll: dict[str, int] = defaultdict(int)
        self.follow: dict[str, bool] = defaultdict(lambda: True)
        self.read_seq: dict[str, int] = defaultdict(int)
        self.notice = "Enter: send · @Agent one-shot · Tab: Agents · Esc: exit"
        self._approval_lock = threading.Lock()
        self._approvals: list[dict] = []
        self._closed = threading.Event()
        self._last_agents: list = []
        selected = agent_loop.get_dialog_agent_for_terminal(self.terminal_name)
        self.selected_id = selected.id if selected else ""

    def agents(self) -> list:
        rows = [a for a in agent_loop.get_all_agents()
                if agent_loop.agent_scope_terminal(a) == self.terminal_name
                and not a.lifecycle_terminated]
        rows.sort(key=lambda a: (
            0 if a.role == "primary" else 1, a.created_at, a.id))
        self._last_agents = rows
        if self.selected_id not in {a.id for a in rows}:
            candidate = agent_loop.get_dialog_agent_for_terminal(self.terminal_name)
            self.selected_id = candidate.id if candidate else (rows[0].id if rows else "")
            if self.selected_id:
                agent_loop.set_dialog_agent_for_terminal(
                    self.terminal_name, self.selected_id)
        return rows

    def select(self, agent_id: str) -> bool:
        if not agent_loop.set_dialog_agent_for_terminal(
                self.terminal_name, agent_id):
            return False
        self.selected_id = agent_id
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
        # Root layout rows outside `main`: top header + separator, input and
        # notice, optional footer, and (on sufficiently large terminals) the
        # two Feed separators plus its four rows. Focus itself adds a two-row
        # title/divider. Keeping this calculation aligned with run() prevents
        # prompt_toolkit from clipping the newest body row at the bottom.
        reserved = 2 + 2 + 2
        if height >= 16:
            reserved += 1
        if width >= 60 and height >= 18:
            reserved += 6
        return width, max(3, height - reserved)

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
        self.terminal_name = terminals[(index + delta) % len(terminals)]
        candidate = agent_loop.get_dialog_agent_for_terminal(self.terminal_name)
        self.selected_id = candidate.id if candidate else ""
        self.overlay = False
        self.invalidate()

    def unread(self, agent_id: str) -> int:
        return sum(
            event.seq > self.read_seq[agent_id]
            and agent_ui_events.hub.needs_attention(event)
            for event in agent_ui_events.hub.agent_events(agent_id))

    def _current_task(self, agent) -> str:
        active = getattr(agent, "active_assignment", None)
        if active is not None and active.task:
            return active.task
        state = getattr(agent, "state", {}) or {}
        history = getattr(agent, "assignment_history", None) or []
        previous_task = history[-1].get("task", "") if history else ""
        return str(state.get("_assignment_task") or state.get("objective")
                   or previous_task or "idle")

    def _display_status(self, agent) -> str:
        """Combine authoritative runtime state with the last durable UI event."""
        status = str(getattr(agent, "status", "idle") or "idle")
        if status in {"running", "thinking", "queued", "waiting"}:
            return status
        events = agent_ui_events.hub.agent_events(agent.id, limit=20)
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
        page = self._rail_page_size()
        max_offset = max(0, len(agents) - page)
        self.rail_offset = min(max(0, self.rail_offset), max_offset)
        visible = agents[self.rail_offset:self.rail_offset + page]
        if self.rail_offset:
            fragments.append(("class:muted", "  ↑ more Agents\n"))
        for agent in visible:
            selected = agent.id == self.selected_id
            status = self._display_status(agent)
            icon, status_style = STATUS.get(status, ("○", "class:idle"))
            unread = self.unread(agent.id)
            row_style = "class:rail.selected" if selected else "class:rail"

            def handler(mouse_event, agent_id=agent.id):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.select(agent_id)
                elif mouse_event.event_type == MouseEventType.SCROLL_UP:
                    self.scroll_rail(-1)
                elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                    self.scroll_rail(1)

            suffix = f"  {unread}" if unread else ""
            fragments.extend([
                (status_style, f" {icon} ", handler),
                (row_style, str(agent.name or agent.id), handler),
                ("class:muted", suffix + "\n", handler),
                ("class:muted", "   " + self._crop(
                    self._current_task(agent), 24) + "\n\n", handler),
            ])
        if self.rail_offset + page < len(agents):
            fragments.append(("class:muted", "  ↓ more Agents\n"))
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
        events = agent_ui_events.hub.agent_events(agent_id, limit=1500)
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
                lines.append(("class:error", f"✗ {event.summary}"))
            elif kind == "approval_requested":
                lines.append(("class:thinking", "◐ Approval required"))
                lines.append(("", event.summary))
                lines.extend(("class:muted", line)
                             for line in event.detail.splitlines()[-12:])
            elif kind == "approval_resolved":
                approved = event.status == "approved"
                lines.append(("class:done" if approved else "class:error",
                              "✓ Approved" if approved else "✗ Denied"))
            elif kind == "agent_done":
                # The answer immediately above is the completion state. A
                # quiet divider separates turns without adding a fake result.
                lines.append(("class:separator", "─" * 24))
            elif kind == "agent_aborted":
                lines.append(("class:error", "✗ Task aborted"))
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
                lines.append(("class:tool", f"◐ {event.summary}"))
                if event.tool_call_id:
                    active_tools[event.tool_call_id] = len(lines) - 1
            elif kind == "tool_finished" and event.tool_call_id in active_tools:
                lines[active_tools.pop(event.tool_call_id)] = (
                    "class:tool", f"● {event.summary}")
                # The start event already owns spacing and chronology.
                continue
            elif kind in {"system", "tool", "tool_finished"}:
                symbol = "◐" if kind == "tool_started" else "●"
                lines.append(("class:tool", f"{symbol} {event.summary}"))
                if event.detail and event.detail != event.summary:
                    lines.extend(("class:muted", "  " + line)
                                 for line in event.detail.splitlines()[-12:])
            elif kind not in {"stream.reset", "stream.end"}:
                symbol = "✗" if "fail" in kind or "error" in kind else "·"
                style = "class:error" if symbol == "✗" else "class:muted"
                lines.append((style, f"{symbol} {event.summary or kind}"))
            if lines and lines[-1][1] != "":
                lines.append(("", ""))
        flush_stream()
        if not lines:
            agent = agent_loop.get_agent(agent_id)
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
        return lines

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

    def _activity_line(self, agent_id: str) -> tuple[str, str] | None:
        agent = agent_loop.get_agent(agent_id)
        if agent is None or agent.status not in {
                "running", "thinking", "queued", "waiting"}:
            return None
        if agent.status == "queued":
            return "class:queued", "◌ Queued…"
        if agent.status == "waiting":
            return "class:waiting", "○ Waiting…"
        events = agent_ui_events.hub.agent_events(agent_id, limit=30)
        for event in reversed(events):
            if event.event_type == "ai_stream":
                return "class:thinking", "◐ Writing…"
            if event.event_type == "ai_end":
                return None
            if event.event_type == "tool_started":
                return "class:thinking", f"◐ {event.summary or 'Running tool…'}"
            if event.event_type in {"tool_finished", "agent_started"}:
                break
        # Three dots animate with the application's refresh tick. The larger
        # dot advances one position per frame, giving feedback without a
        # literal "Thinking" placeholder.
        phase = int(time.monotonic() * 5) % 3
        dots = ["·", "·", "·"]
        dots[phase] = "●"
        return "class:thinking", "  ".join(dots)

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
        activity = self._activity_line(agent_id)
        width, height = self._focus_body_height()
        stream_tail = ""
        if activity:
            height = max(3, height - 1)
            # While the Agent is writing, surface what it is saying live —
            # the full reply is mirrored in place once streaming completes.
            stream_tail = self._stream_tail(agent_id)
            if stream_tail:
                height = max(3, height - 1)
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
        header_name = self._crop(self._agent_name(agent_id), max(1, width - 2))
        fragments = [("class:header", f"  {header_name}\n"),
                     ("class:separator", "  " + "─" * max(
                         1, min(50, width - 2)) + "\n")]
        for row in visible:
            fragments.append(("", " "))
            fragments.extend((style, value) for style, value, *_rest in row)
            fragments.append(("", "\n"))
        if stream_tail:
            fragments.append((
                "class:muted",
                "  " + self._crop(stream_tail, max(20, width - 6)) + "\n"))
        if activity:
            style, text = activity
            fragments.append((style, "  " + text + "\n"))
        return FormattedText(fragments)

    def focus_fragments(self):
        agent_id = self.selected_id
        if not agent_id:
            return FormattedText([("class:muted", "\n  No Agent selected")])
        mirror_lines = self._mirror_lines(agent_id)
        if mirror_lines is not None:
            return self._focus_mirror_fragments(agent_id, mirror_lines)
        lines = self._event_lines(agent_id)
        activity = self._activity_line(agent_id)
        if activity:
            lines.extend([activity, ("", "")])
        _width, height = self._focus_body_height()
        offset = max(0, self.focus_scroll[agent_id])
        end = max(0, len(lines) - offset)
        start = max(0, end - height)
        visible = lines[start:end]
        if self.follow[agent_id]:
            events = agent_ui_events.hub.agent_events(agent_id)
            if events:
                self.read_seq[agent_id] = events[-1].seq
        fragments = [("class:header", f"  {self._agent_name(agent_id)}\n"),
                     ("class:separator", "  " + "─" * 50 + "\n")]
        for style, line in visible:
            fragments.append((style, "  "))
            fragments.extend(self._inline_markdown(style, line))
            fragments.append((style, "\n"))
        return FormattedText(fragments)

    def feed_fragments(self):
        rows = []
        ignored = {
            "ai_stream", "ai_end", "stream.reset", "stream.end", "user", "ai",
            "user_message", "user_broadcast", "tool_output", "tool_started",
            "agent_done",
        }
        for event in agent_ui_events.hub.events(self.terminal_name, limit=100):
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
        return FormattedText(fragments)

    def header_fragments(self):
        agents = self.agents()
        running = sum(self._display_status(a) in {
            "running", "thinking", "queued"} for a in agents)
        width, _height = self._terminal_size()
        if width < 100:
            pieces = [f"[{self._agent_name(self.selected_id)}] "]
            pieces.extend(
                f"{STATUS.get(self._display_status(a), ('○', ''))[0]}{a.name}"
                + (f"({self.unread(a.id)})" if self.unread(a.id) else "")
                for a in agents[:4])
            return FormattedText([("class:header", " ".join(pieces)),
                                  ("class:muted", "  Tab:switch")])
        return FormattedText([
            ("class:header", f" Laintas · {self.terminal_name}"),
            ("class:muted", f" · {running} running · Focus: "),
            ("class:agent", self._agent_name(self.selected_id)),
            ("class:muted", "   Alt+←/→ terminals"),
        ])

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
        deps.console = self._silent_console
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
        queued = f" · {count} pending" if count > 1 else ""
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
        fragments = [
            ("class:thinking", "\n  ◐ Approval required\n\n"),
            ("class:agent", f"  Agent: {agent_name}\n"),
            ("class:muted", f"  Terminal: {request.get('terminal_name')}\n"),
            ("class:tool", f"  Type: {request.get('kind')}\n\n"),
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
                self.notice = "Ready · Enter a message or Esc to return"
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

        rail = Window(FormattedTextControl(self.rail_fragments), width=28,
                      wrap_lines=False, style="class:rail")
        focus = Window(FormattedTextControl(
            lambda: FormattedText([
                (style, text, self.focus_mouse)
                for style, text in self.focus_fragments()
            ])), wrap_lines=True)
        wide_filter = Condition(lambda: self._terminal_size()[0] >= 100)
        wide_rail = ConditionalContainer(rail, filter=wide_filter)
        wide_separator = ConditionalContainer(
            Window(width=1, char="│", style="class:separator"),
            filter=wide_filter)
        main = VSplit([
            wide_rail,
            wide_separator,
            focus,
        ])
        overlay_filter = Condition(lambda: self.overlay)
        approval_view = ConditionalContainer(
            Window(FormattedTextControl(self.approval_fragments),
                   wrap_lines=True),
            filter=approval_filter)
        overlay_rail = ConditionalContainer(
            Window(FormattedTextControl(self.rail_fragments), wrap_lines=False),
            filter=overlay_filter & ~approval_filter)
        input_control = BufferControl(
            buffer=input_buffer,
            input_processors=[BeforeInput(
                lambda: FormattedText([
                    ("class:input", f" @{self._agent_name(self.selected_id)} > ")]))])
        feed_filter = Condition(lambda: (
            self._terminal_size()[0] >= 60
            and self._terminal_size()[1] >= 18))
        verbose_footer_filter = Condition(lambda: self._terminal_size()[1] >= 16)
        root = HSplit([
            Window(FormattedTextControl(self.header_fragments), height=1),
            Window(height=1, char="─", style="class:separator"),
            approval_view,
            overlay_rail,
            ConditionalContainer(
                main, filter=~overlay_filter & ~approval_filter),
            ConditionalContainer(
                Window(height=1, char="─", style="class:separator"),
                filter=feed_filter),
            ConditionalContainer(
                Window(FormattedTextControl(self.feed_fragments), height=4),
                filter=feed_filter),
            ConditionalContainer(
                Window(height=1, char="─", style="class:separator"),
                filter=feed_filter),
            Window(input_control, height=1),
            Window(FormattedTextControl(
                lambda: FormattedText([("class:muted", " " + self.notice)])),
                height=1),
            ConditionalContainer(
                Window(FormattedTextControl(lambda: FormattedText([
                    ("class:muted",
                     " Esc/Ctrl-C: exit · Enter: send · Tab: Agents · "
                     "Alt+↑/↓: focus · PgUp/PgDn: scroll")
                ])), height=1),
                filter=verbose_footer_filter),
        ])
        self.app = Application(
            layout=Layout(root, focused_element=input_control),
            key_bindings=kb, style=STYLE, full_screen=True,
            mouse_support=True, refresh_interval=0.1,
            input=input, output=output)
        agent_ui_events.hub.subscribe(self.on_event)
        try:
            self.app.run()
        finally:
            # Workers may outlive the full-screen application. Close the
            # approval channel first so any later request is denied instead of
            # waiting forever for a UI that no longer exists.
            self.deny_pending_approvals(
                close=True, reason="agents_mode_closed")
            agent_ui_events.hub.unsubscribe(self.on_event)
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
