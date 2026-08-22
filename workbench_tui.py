"""Textual full-screen workbench for laintas_cli.

The workbench owns terminal rendering while the existing REPL remains the
single execution authority. UI actions are routed back through the same input
queue as classic mode; runtime threads communicate with the app exclusively
through Textual messages.
"""

from __future__ import annotations

from collections import deque
from functools import partial
import os
import threading
from typing import Any, Callable, Iterable, Optional

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Input, Label, OptionList, RichLog, Static,
)
from textual.widgets.option_list import Option

import agent_loop
import agent_ui_events
import symbols


STATUS_META = {
    "running": (symbols.DOT, "RUN", "#4ade80"),
    "thinking": (symbols.DOT_HALF, "THINK", "#e3b341"),
    "queued": (symbols.DOT_DASH, "QUEUE", "#9aa7b8"),
    "waiting": (symbols.DOT_OPEN, "WAIT", "#e3b341"),
    "done": (symbols.OK, "DONE", "#4ade80"),
    "ready": (symbols.OK, "READY", "#4ade80"),
    "error": (symbols.FAIL, "ERROR", "#f87171"),
    "aborted": (symbols.FAIL, "ABORT", "#f87171"),
    "idle": (symbols.DOT_OPEN, "IDLE", "#9aa7b8"),
}


class RuntimeWake(Message):
    """One coalesced wake-up for any number of runtime events."""


class _EventBridge:
    """Bounded multi-producer bridge with at most one queued UI wake-up."""

    def __init__(self, wake: Callable[[], None], max_items: int = 5000):
        self._wake = wake
        self._items: deque[tuple[str, Any]] = deque(maxlen=max_items)
        self._lock = threading.Lock()
        self._scheduled = False

    def push(self, kind: str, value: Any) -> None:
        notify = False
        with self._lock:
            self._items.append((kind, value))
            if not self._scheduled:
                self._scheduled = True
                notify = True
        if notify:
            self._wake()

    def drain(self) -> list[tuple[str, Any]]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            self._scheduled = False
        return items


class ApprovalScreen(ModalScreen[bool]):
    """Keyboard and mouse-equivalent approval surface."""

    BINDINGS = [
        ("y", "approve", "Approve"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    CSS = """
    ApprovalScreen { align: center middle; background: rgba(0, 0, 0, 0.65); }
    #approval-card {
        width: 86; max-width: 92%; height: auto; max-height: 86%;
        background: #111827; border: heavy #d29922; padding: 1 2;
    }
    #approval-title { color: #e3b341; text-style: bold; height: 2; }
    #approval-body { height: auto; max-height: 24; overflow-y: auto; }
    #approval-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #approval-actions Button { min-width: 14; margin-left: 1; }
    #deny { background: #7f1d1d; }
    #approve { background: #166534; }
    """

    def __init__(self, request: dict):
        super().__init__()
        self.request = dict(request)

    def compose(self) -> ComposeResult:
        kind = str(self.request.get("kind") or "action").upper()
        agent_id = str(self.request.get("agent_id") or "Agent")
        summary = str(self.request.get("summary") or "Approval required")
        detail = str(self.request.get("detail") or "")
        body = Text(summary, style="bold")
        if detail:
            body.append("\n\n")
            body.append(detail)
        with Vertical(id="approval-card"):
            yield Label(f"APPROVAL · {kind} · {agent_id}", id="approval-title")
            yield Static(body, id="approval-body")
            with Horizontal(id="approval-actions"):
                yield Button("Deny  [N]", id="deny", variant="error")
                yield Button("Approve  [Y]", id="approve", variant="success")

    @on(Button.Pressed, "#approve")
    def approve_button(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#deny")
    def deny_button(self) -> None:
        self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class WorkbenchApp(App[Optional[str]]):
    """Responsive Agent workbench backed by the existing runtime controller."""

    TITLE = "Laintas Workbench"
    ENABLE_COMMAND_PALETTE = True
    COMMAND_PALETTE_BINDING = "ctrl+p"

    CSS = """
    $bg: #0d1117;
    $surface: #111827;
    $surface2: #172033;
    $border: #334155;
    $text: #f0f6fc;
    $muted: #9aa7b8;
    $accent: #4ade80;
    $agent: #c4a7ff;

    Screen { background: $bg; color: $text; }
    #topbar {
        height: 2; padding: 0 1; background: #0b1220;
        border-bottom: solid $border; content-align: left middle;
    }
    #brand { width: auto; color: $accent; text-style: bold; }
    #workspace-status { width: 1fr; color: $muted; text-align: right; }
    #toolbar {
        height: 3; padding: 0 1; background: $surface;
        border-bottom: solid $border;
    }
    #toolbar Button { min-width: 9; height: 3; margin-right: 1; }
    #body { height: 1fr; }
    #agent-rail {
        width: 31; min-width: 24; background: $surface;
        border-right: solid $border;
    }
    #rail-title, #context-title, #activity-title {
        height: 2; padding: 0 1; color: $muted; text-style: bold;
    }
    #agent-list { height: 1fr; padding: 0 1; }
    #agent-list:focus { border: tall $accent; }
    #center { width: 1fr; height: 1fr; }
    #conversation-title {
        height: 2; padding: 0 1; color: $agent; text-style: bold;
        border-bottom: solid $border;
    }
    #conversation { height: 1fr; padding: 1 2; background: $bg; }
    #conversation:focus { border: tall $accent; }
    #live-stream {
        display: none; height: auto; max-height: 9; padding: 1 2;
        color: $text; background: #101827; border-top: solid $border;
    }
    #live-stream.visible { display: block; }
    #context-panel {
        width: 32; min-width: 27; padding: 0 1; background: $surface;
        border-left: solid $border;
    }
    #context-body { color: $muted; }
    #activity-wrap {
        height: 6; background: #0b1220; border-top: solid $border;
    }
    #activity-log { height: 1fr; padding: 0 1; color: $muted; }
    #composer-row {
        height: 3; padding: 0 1; background: $surface;
        border-top: solid $border;
    }
    #composer { width: 1fr; height: 3; }
    #composer:focus { border: tall $accent; }
    #send { width: 13; height: 3; margin-left: 1; background: #166534; }
    #notice {
        height: 2; padding: 0 1; color: $muted; background: #0b1220;
        border-top: solid $border;
    }
    .hidden { display: none; }
    .attention { color: #e3b341; text-style: bold; }
    Footer { background: #0b1220; color: $muted; }
    """

    BINDINGS = [
        ("ctrl+a", "toggle_agents", "Agents"),
        ("alt+up", "previous_agent", "Previous Agent"),
        ("alt+down", "next_agent", "Next Agent"),
        ("alt+left", "previous_terminal", "Previous Terminal"),
        ("alt+right", "next_terminal", "Next Terminal"),
        ("ctrl+g", "focus_composer", "Compose"),
        ("ctrl+e", "follow_latest", "Latest"),
        ("ctrl+l", "clear_view", "Clear View"),
        ("ctrl+q", "quit", "Exit Workbench"),
        ("escape", "escape", "Back"),
    ]

    def __init__(self, controller, *, mirror=None):
        super().__init__()
        self.controller = controller
        self.mirror = mirror
        self._bridge = _EventBridge(self._wake_from_thread)
        self._agent_signature: tuple = ()
        self._selected_rendered = ""
        self._stream_text = ""
        self._stream_agent = ""
        self._approval_id = ""
        self._rail_forced = False
        self._narrow = False
        self._console_partial = ""
        self._deferred_line: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Label("LAINTAS  /  WORKBENCH", id="brand")
            yield Label("", id="workspace-status")
        with Horizontal(id="toolbar"):
            yield Button("Agents  ^A", id="agents-button")
            yield Button("Prev  A↑", id="previous-agent")
            yield Button("Next  A↓", id="next-agent")
            yield Button("Terminal ‹", id="previous-terminal")
            yield Button("Terminal ›", id="next-terminal")
            yield Button("Latest  ^E", id="latest")
        with Horizontal(id="body"):
            with Vertical(id="agent-rail"):
                yield Label("AGENTS · status and attention", id="rail-title")
                yield OptionList(id="agent-list")
            with Vertical(id="center"):
                yield Label("CONVERSATION", id="conversation-title")
                yield RichLog(
                    id="conversation", wrap=True, highlight=True,
                    markup=False, auto_scroll=True, max_lines=4000)
                yield Static("", id="live-stream")
            with Vertical(id="context-panel"):
                yield Label("CONTEXT", id="context-title")
                yield Static("", id="context-body")
        with Vertical(id="activity-wrap"):
            yield Label("ACTIVITY · click an Agent or press Ctrl+A", id="activity-title")
            yield RichLog(id="activity-log", wrap=False, markup=False,
                          auto_scroll=True, max_lines=200)
        with Horizontal(id="composer-row"):
            yield Input(
                placeholder="Message, /command, or $ shell command",
                id="composer")
            yield Button("Send  Enter", id="send", variant="success")
        yield Static("Ready", id="notice")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        agent_ui_events.hub.subscribe(self._runtime_event)
        if self.mirror is not None and hasattr(self.mirror, "subscribe"):
            self.mirror.subscribe(self._console_event)
        self.set_interval(0.10, self._refresh_fast)
        self.set_interval(0.75, self._refresh_metadata)
        self._apply_breakpoint(self.size.width, self.size.height)
        self._refresh_agents(force=True)
        self._rebuild_conversation()
        self._rebuild_activity()
        self._refresh_metadata()
        self.query_one("#composer", Input).focus()

    def on_unmount(self) -> None:
        agent_ui_events.hub.unsubscribe(self._runtime_event)
        if self.mirror is not None and hasattr(self.mirror, "unsubscribe"):
            self.mirror.unsubscribe(self._console_event)
        self.controller.deny_pending_approvals(
            close=True, reason="workbench_closed")

    def _wake_from_thread(self) -> None:
        try:
            self.post_message(RuntimeWake())
        except Exception:
            pass

    def _runtime_event(self, event: agent_ui_events.AgentUIEvent) -> None:
        self._bridge.push("event", event)

    def _console_event(self, agent_id: str, text: str,
                       recording: bool) -> None:
        # Agent execution already emits structured events. Console chunks are
        # needed for slash/shell output that has no semantic event of its own.
        if not recording:
            self._bridge.push("console", (agent_id, text))

    @on(RuntimeWake)
    def _drain_runtime(self) -> None:
        if isinstance(self.screen, ApprovalScreen):
            return
        dirty_agents = False
        stream_chunks: list[str] = []
        for kind, value in self._bridge.drain():
            if kind == "event":
                event = value
                dirty_agents = True
                self._append_activity(event)
                if event.agent_id == self.controller.selected_id:
                    if event.event_type == "ai_stream":
                        stream_chunks.append(event.detail)
                    else:
                        self._append_event(event)
            elif kind == "console":
                _agent_id, chunk = value
                self._append_console(chunk)
        if stream_chunks:
            self._append_stream_batch(
                self.controller.selected_id, "".join(stream_chunks))
        if dirty_agents:
            self._refresh_agents()
        self._show_approval_if_needed()

    def _append_console(self, chunk: str) -> None:
        value = self._console_partial + str(chunk or "")
        parts = value.split("\n")
        self._console_partial = parts.pop()
        log = self.query_one("#conversation", RichLog)
        for line in parts:
            if line:
                log.write(Text.from_ansi(line))

    def _append_activity(self, event: agent_ui_events.AgentUIEvent) -> None:
        if event.event_type in {
                "ai_stream", "ai_end", "stream.reset", "stream.end",
                "tool_output", "user", "user_message", "ai"}:
            return
        when = __import__("time").strftime(
            "%H:%M:%S", __import__("time").localtime(event.timestamp))
        actor = event.agent_id or event.event_type
        target = f" → {event.target_agent_id}" if event.target_agent_id else ""
        summary = event.summary or event.event_type
        text = Text.assemble(
            (f"{when}  ", "dim"),
            (f"{actor}{target}  ", "bold #c4a7ff"),
            summary,
        )
        self.query_one("#activity-log", RichLog).write(text)

    def _rebuild_activity(self) -> None:
        log = self.query_one("#activity-log", RichLog)
        log.clear()
        _revision, events_for_terminal = agent_ui_events.hub.events_snapshot(
            self.controller.terminal_name, limit=100)
        for event in events_for_terminal:
            self._append_activity(event)

    def _append_event(self, event: agent_ui_events.AgentUIEvent) -> None:
        log = self.query_one("#conversation", RichLog)
        kind = event.event_type
        if kind == "ai_stream":
            self._append_stream_batch(event.agent_id, event.detail)
            return
        if kind == "ai_end":
            if self._stream_text:
                log.write(Text(self._agent_name(event.agent_id),
                               style="bold #c4a7ff"))
                log.write(RichMarkdown(self._stream_text))
                self._stream_text = ""
                self._stream_agent = ""
            self.query_one("#live-stream", Static).remove_class("visible")
            return
        if kind in {"user", "user_message"}:
            log.write(Text("YOU", style="bold #f0f6fc"))
            log.write(event.detail or event.summary)
        elif kind == "ai":
            log.write(Text(self._agent_name(event.agent_id),
                           style="bold #c4a7ff"))
            log.write(RichMarkdown(event.detail or event.summary))
        elif kind == "tool_started":
            log.write(Text(
                f"{symbols.DOT_HALF} RUN  {event.summary}",
                style="bold #d2a8ff"))
        elif kind in {"tool_finished", "tool"}:
            log.write(Text(
                f"{symbols.OK} TOOL  {event.summary}", style="#a7f3d0"))
        elif kind == "tool_output":
            detail = event.detail or event.summary
            if detail:
                log.write(Text(detail[-8000:], style="dim"))
        elif kind == "approval_requested":
            log.write(Text(
                f"{symbols.DOT_HALF} APPROVAL  {event.summary}",
                style="bold #e3b341"))
        elif kind == "approval_resolved":
            approved = event.status == "approved"
            log.write(Text(
                f"{symbols.OK if approved else symbols.FAIL} "
                f"{'APPROVED' if approved else 'DENIED'}  {event.summary}",
                style="#4ade80" if approved else "#f87171"))
        elif kind in {"agent_error", "step_failed", "node_failed",
                      "input_rejected", "user_message_failed"}:
            log.write(Text(
                f"{symbols.FAIL} {event.summary or kind}",
                style="bold #f87171"))
        elif kind == "agent_message":
            direction = f"{event.agent_id} → {event.target_agent_id}"
            log.write(Text(direction, style="bold #c4a7ff"))
            log.write(event.detail or event.summary)

    def _append_stream_batch(self, agent_id: str, text: str) -> None:
        if self._stream_agent != agent_id:
            self._stream_text = ""
            self._stream_agent = agent_id
        self._stream_text += str(text or "")
        live = self.query_one("#live-stream", Static)
        live.update(RichMarkdown(self._stream_text or "…"))
        live.add_class("visible")

    def _rebuild_conversation(self) -> None:
        agent_id = self.controller.selected_id
        log = self.query_one("#conversation", RichLog)
        log.clear()
        self._stream_text = ""
        self._stream_agent = ""
        self.query_one("#live-stream", Static).remove_class("visible")
        if not agent_id:
            log.write(Text(
                "No Agent is available in this terminal.\n"
                "Use /hire from the composer or switch terminals.",
                style="dim"))
            return
        for event in agent_ui_events.hub.agent_events(agent_id, limit=600):
            self._append_event(event)
        log.scroll_end(animate=False)
        self._selected_rendered = agent_id

    def _agent_name(self, agent_id: str) -> str:
        agent = agent_loop.get_agent(agent_id)
        return str(agent.name or agent.id) if agent else str(agent_id or "Agent")

    def _refresh_agents(self, force: bool = False) -> None:
        agents = self.controller.agents()
        rows = []
        for agent in agents:
            status = self.controller._display_status(agent)
            unread = self.controller.unread(agent.id)
            rows.append((agent.id, str(agent.name or agent.id), status, unread))
        signature = tuple(rows) + (("selected", self.controller.selected_id),)
        if not force and signature == self._agent_signature:
            return
        self._agent_signature = signature
        options = self.query_one("#agent-list", OptionList)
        options.clear_options()
        selected_index = None
        for index, (agent_id, name, status, unread) in enumerate(rows):
            icon, label, style = STATUS_META.get(
                status, (symbols.DOT_OPEN, status.upper(), "#9aa7b8"))
            attention = f"  [{unread}]" if unread else ""
            prompt = Text.assemble(
                (f"{icon} ", style), (name, "bold"),
                (f"\n   {label}{attention}", "dim"),
            )
            options.add_option(Option(prompt, id=agent_id))
            if agent_id == self.controller.selected_id:
                selected_index = index
        if selected_index is not None:
            options.highlighted = selected_index
        if self.controller.selected_id != self._selected_rendered:
            self._rebuild_conversation()

    def _refresh_fast(self) -> None:
        if not self.is_mounted or isinstance(self.screen, ApprovalScreen):
            return
        try:
            self._refresh_agents()
            self._show_approval_if_needed()
            if isinstance(self.screen, ApprovalScreen):
                return
            self.query_one("#notice", Static).update(
                self.controller.notice or "Ready")
        except NoMatches:
            return

    def _refresh_metadata(self) -> None:
        if not self.is_mounted or isinstance(self.screen, ApprovalScreen):
            return
        try:
            self._refresh_metadata_mounted()
        except NoMatches:
            return

    def _refresh_metadata_mounted(self) -> None:
        selected = agent_loop.get_agent(self.controller.selected_id)
        active = [a for a in self.controller.agents()
                  if a.status in {"running", "thinking", "queued", "waiting"}]
        effort = agent_loop.get_runtime_config("reasoning_effort")
        try:
            cwd_path = os.getcwd()
        except OSError:
            # The test harness (and, in practice, deleted/moved mount points) can
            # leave a process attached to a directory that no longer exists.
            cwd_path = ""
        cwd = os.path.basename(cwd_path) or cwd_path or "(detached cwd)"
        self.query_one("#workspace-status", Label).update(
            f"{cwd}  {symbols.BULLET}  effort {effort}  {symbols.BULLET}  "
            f"{len(active)} active  {symbols.BULLET}  {self.controller.terminal_name}")
        name = self._agent_name(self.controller.selected_id)
        status = str(getattr(selected, "status", "idle") or "idle")
        icon, label, _style = STATUS_META.get(
            status, (symbols.DOT_OPEN, status.upper(), "#9aa7b8"))
        self.query_one("#conversation-title", Label).update(
            f"CONVERSATION  /  {name}  {icon} {label}")
        if selected is None:
            body = "No Agent selected"
        else:
            task = self.controller._current_task(selected)
            events_for_agent = agent_ui_events.hub.agent_events(
                selected.id, limit=500)
            tools = sum(e.event_type in {"tool_started", "tool_finished"}
                        for e in events_for_agent)
            approvals = sum(e.event_type == "approval_requested"
                            for e in events_for_agent)
            body = Text.assemble(
                (f"{icon} {label}\n\n", "bold"),
                ("AGENT\n", "dim"), f"{name}\n\n",
                ("ROLE\n", "dim"), f"{selected.role}\n\n",
                ("TASK\n", "dim"), f"{task}\n\n",
                ("RUNTIME\n", "dim"),
                f"terminal  {self.controller.terminal_name}\n",
                f"tools     {tools}\n",
                f"approvals {approvals}\n",
                f"events    {len(events_for_agent)}",
            )
        self.query_one("#context-body", Static).update(body)

    def _show_approval_if_needed(self) -> None:
        request = self.controller.pending_approval()
        request_id = str((request or {}).get("id") or "")
        if not request_id or request_id == self._approval_id:
            return
        self._approval_id = request_id

        def resolved(approved: Optional[bool]) -> None:
            self.controller.resolve_approval(bool(approved))
            self._approval_id = ""
            self.post_message(RuntimeWake())

        self.push_screen(ApprovalScreen(request), resolved)

    @on(OptionList.OptionSelected, "#agent-list")
    def select_agent(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and self.controller.select(str(event.option.id)):
            self._refresh_agents(force=True)
            self._rebuild_conversation()
            if self._narrow:
                self._rail_forced = False
                self._sync_responsive_visibility()

    @on(Input.Submitted, "#composer")
    def submit_input(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    @on(Button.Pressed, "#send")
    def send_button(self) -> None:
        self._submit(self.query_one("#composer", Input).value)

    @on(Button.Pressed, "#agents-button")
    def agents_button(self) -> None:
        self.action_toggle_agents()

    @on(Button.Pressed, "#previous-agent")
    def previous_agent_button(self) -> None:
        self.action_previous_agent()

    @on(Button.Pressed, "#next-agent")
    def next_agent_button(self) -> None:
        self.action_next_agent()

    @on(Button.Pressed, "#previous-terminal")
    def previous_terminal_button(self) -> None:
        self.action_previous_terminal()

    @on(Button.Pressed, "#next-terminal")
    def next_terminal_button(self) -> None:
        self.action_next_terminal()

    @on(Button.Pressed, "#latest")
    def latest_button(self) -> None:
        self.action_follow_latest()

    def _submit(self, raw: str) -> None:
        value = str(raw or "").strip()
        if not value:
            return
        composer = self.query_one("#composer", Input)
        composer.clear()
        if value.startswith("$"):
            command = value[1:].lstrip()
            if not command:
                self.notify("Enter a shell command after $", severity="warning")
                return
            if self._shell_requires_handoff(command):
                self._handoff(command)
                return
            ok, detail = self.controller.repl_submit_cb(command, kind="line")
            self.controller.notice = detail or "Shell command queued"
        elif value.startswith("/"):
            if self._command_requires_handoff(value):
                self._handoff(value)
                return
            ok, detail = self.controller.repl_submit_cb(
                value, kind="line")
            self.controller.notice = detail or "Command queued"
        else:
            self.controller.dispatch(value)
        composer.focus()

    @staticmethod
    def _command_requires_handoff(value: str) -> bool:
        parts = str(value or "").split()
        if not parts:
            return False
        head = parts[0].casefold()
        if head in {"/resume", "/hwo", "/hwg", "/blindpick"}:
            return True
        return len(parts) == 1 and head in {
            "/model", "/mode", "/theme", "/memory", "/agents",
        }

    @staticmethod
    def _shell_requires_handoff(command: str) -> bool:
        try:
            import laintas_cli
            first = laintas_cli.extract_first_word(command)
            return first in laintas_cli.get_interactive_commands()
        except Exception:
            return False

    def _handoff(self, line: str) -> None:
        self._deferred_line = str(line)
        self.controller.notice = "Handing terminal to the classic CLI…"
        self.exit(self._deferred_line)

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        try:
            import laintas_cli
            for spec in laintas_cli.COMMAND_SPECS:
                usage = str(spec.usage or spec.name)
                yield SystemCommand(
                    usage,
                    str(spec.description or spec.group),
                    partial(self._prefill_command, usage),
                )
        except Exception:
            return

    def _prefill_command(self, usage: str) -> None:
        # Strip display-only optional arguments, leaving the canonical command
        # ready for editing rather than accidentally executing a placeholder.
        command = str(usage).split()[0]
        composer = self.query_one("#composer", Input)
        composer.value = command + " "
        composer.cursor_position = len(composer.value)
        composer.focus()

    def action_toggle_agents(self) -> None:
        self._rail_forced = not self._rail_forced
        self._sync_responsive_visibility()
        if self._rail_forced or not self._narrow:
            self.query_one("#agent-list", OptionList).focus()
        else:
            self.query_one("#composer", Input).focus()

    def action_previous_agent(self) -> None:
        self.controller.cycle_agent(-1)
        self._refresh_agents(force=True)
        self._rebuild_conversation()

    def action_next_agent(self) -> None:
        self.controller.cycle_agent(1)
        self._refresh_agents(force=True)
        self._rebuild_conversation()

    def action_previous_terminal(self) -> None:
        self.controller.cycle_terminal(-1)
        self._refresh_agents(force=True)
        self._rebuild_conversation()
        self._rebuild_activity()

    def action_next_terminal(self) -> None:
        self.controller.cycle_terminal(1)
        self._refresh_agents(force=True)
        self._rebuild_conversation()
        self._rebuild_activity()

    def action_focus_composer(self) -> None:
        self.query_one("#composer", Input).focus()

    def action_follow_latest(self) -> None:
        self.query_one("#conversation", RichLog).scroll_end(animate=False)
        self.query_one("#composer", Input).focus()

    def action_clear_view(self) -> None:
        self.query_one("#conversation", RichLog).clear()
        self.query_one("#activity-log", RichLog).clear()
        self.notify("View cleared; runtime history is unchanged")

    def action_escape(self) -> None:
        if self._rail_forced and self._narrow:
            self._rail_forced = False
            self._sync_responsive_visibility()
            self.query_one("#composer", Input).focus()
            return
        self.query_one("#composer", Input).focus()

    def action_quit(self) -> None:
        self.exit()

    def on_resize(self, event: events.Resize) -> None:
        if isinstance(self.screen, ApprovalScreen):
            return
        self._apply_breakpoint(event.size.width, event.size.height)

    def _apply_breakpoint(self, width: int, height: int) -> None:
        self._narrow = width < 96
        self._sync_responsive_visibility(width=width, height=height)

    def _sync_responsive_visibility(self, *, width: Optional[int] = None,
                                    height: Optional[int] = None) -> None:
        width = self.size.width if width is None else width
        height = self.size.height if height is None else height
        rail = self.query_one("#agent-rail")
        inspector = self.query_one("#context-panel")
        activity = self.query_one("#activity-wrap")
        toolbar = self.query_one("#toolbar")
        rail.set_class(not self._narrow or self._rail_forced, "visible")
        rail.set_class(self._narrow and not self._rail_forced, "hidden")
        # On a narrow terminal the rail becomes a full-width drawer.
        rail.styles.width = "100%" if self._narrow else 31
        self.query_one("#center").set_class(
            self._narrow and self._rail_forced, "hidden")
        inspector.set_class(width < 140, "hidden")
        activity.set_class(width < 70 or height < 22, "hidden")
        toolbar.set_class(height < 16, "hidden")
        self.query_one("#previous-terminal").set_class(width < 90, "hidden")
        self.query_one("#next-terminal").set_class(width < 90, "hidden")


def run_workbench(controller, *, mirror=None) -> Optional[str]:
    """Run the Textual workbench using an existing AgentsModeController."""
    with controller._approval_lock:
        controller._closed.clear()
    app = WorkbenchApp(controller, mirror=mirror)
    return app.run()
