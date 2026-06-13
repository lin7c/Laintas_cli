"""HWO — /hwo command: visual agent-orchestration builder.

Input grammar (typed at the bottom prompt):
  -> task text         — add a task to the current agent
  #agent-name#         — create a new agent node
  ////                 — parallel separator between agent groups
  q  /  Ctrl-C / Esc   — exit the UI

Rendering:
  • Every task shows a spinning rectangle (◰ ◳ ◲ ◱) while running
  • Done tasks show ■, errors show ✗
  • //// draws a dashed parallel separator
  • Child agents are indented relative to their parent
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

# ── Spin frames for "rotating rectangle" ──────────────────────────────────
_SPIN = ["◰", "◳", "◲", "◱"]
_DONE  = "■"
_ERROR = "✗"
_PEND  = "□"


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class HwoTask:
    text: str
    status: str = "running"  # running | done | error | pending


@dataclass
class HwoAgent:
    name: str
    depth: int = 0
    tasks: list[HwoTask] = field(default_factory=list)
    parallel_before: bool = False  # //// preceded this agent


@dataclass
class HwoSession:
    root_name: str                             # top-level agent (e.g. "primary")
    agents: list[HwoAgent] = field(default_factory=list)
    _depth_stack: list[str] = field(default_factory=list)  # name stack for nesting

    # ── parsing helpers ───────────────────────────────────────────────────

    def add_agent(self, name: str, parallel: bool = False) -> HwoAgent:
        """Add a top-level agent.  Call push_depth() first to nest."""
        depth = len(self._depth_stack)
        agent = HwoAgent(name=name, depth=depth, parallel_before=parallel)
        self.agents.append(agent)
        return agent

    def add_task(self, text: str) -> Optional[HwoTask]:
        if not self.agents:
            return None
        task = HwoTask(text=text, status="running")
        self.agents[-1].tasks.append(task)
        return task

    def push_depth(self) -> None:
        """Increase nesting level (next #agent# will be indented deeper)."""
        if self.agents:
            self._depth_stack.append(self.agents[-1].name)

    def pop_depth(self) -> None:
        if self._depth_stack:
            self._depth_stack.pop()


# ── Renderer ──────────────────────────────────────────────────────────────

def _spin_char(tick: int) -> str:
    return _SPIN[tick % len(_SPIN)]


def _render(session: HwoSession, tick: int) -> list[tuple[str, str]]:
    """Return a prompt_toolkit formatted-text list."""
    out: list[tuple[str, str]] = []

    def push(style: str, text: str) -> None:
        out.append((style, text))

    # ── header ────────────────────────────────────────────────────────────
    push("class:header", f"  HWO  ·  Agent: ")
    push("class:header.name", session.root_name)
    push("class:header", "\n\n")

    if not session.agents:
        push("class:dim", "  type  #agent-name#  to create an agent\n")
        push("class:dim", "        -> task text  to add a task\n")
        push("class:dim", "        ////          for parallel groups\n")
        return out

    for agent in session.agents:
        indent = "  " * agent.depth

        # parallel separator
        if agent.parallel_before:
            sep_indent = "  " * max(0, agent.depth)
            push("class:parallel", f"{sep_indent}{'─' * 4}  ////  {'─' * 4}\n\n")

        # agent box header
        spin = _spin_char(tick)
        push("class:agent.spin", f"{indent}{spin} ")
        push("class:agent.name", f"#{agent.name}#")
        push("class:agent", "\n")

        # tasks
        for task in agent.tasks:
            task_indent = indent + "    "
            if task.status == "running":
                mark = _spin_char(tick + 2)  # offset so agent/task differ
                push("class:task.running", f"{task_indent}{mark} → ")
            elif task.status == "done":
                push("class:task.done", f"{task_indent}{_DONE} → ")
            elif task.status == "error":
                push("class:task.error", f"{task_indent}{_ERROR} → ")
            else:
                push("class:task.pending", f"{task_indent}{_PEND} → ")
            push("class:task.text", f"{task.text}\n")

        if not agent.tasks:
            push("class:dim", f"{indent}    (no tasks yet)\n")

        push("", "\n")

    return out


def _render_help() -> list[tuple[str, str]]:
    return [
        ("class:help", "  → task  │  #agent#  │  ////  │  q=quit"),
    ]


# ── TUI application ───────────────────────────────────────────────────────

_STYLE = Style.from_dict({
    "header":      "bold cyan",
    "header.name": "bold white",
    "agent.spin":  "yellow",
    "agent.name":  "bold magenta",
    "agent":       "",
    "task.running":"cyan",
    "task.done":   "green",
    "task.error":  "red",
    "task.pending":"dim",
    "task.text":   "white",
    "parallel":    "bold blue",
    "dim":         "dim",
    "separator":   "dim",
    "help":        "dim italic",
    "input.prefix":"bold green",
    "input.bar":   "bg:#1a1a1a",
})


def run_hwo_ui(root_agent_name: str) -> None:
    """Launch the /hwo full-screen TUI."""
    session = HwoSession(root_name=root_agent_name)
    tick = [0]
    _next_parallel = [False]   # mutable flag: next agent is parallel
    error_msg = [""]           # shown below input on bad input

    # ── tree view (animated) ──────────────────────────────────────────────
    def _get_tree_text():
        return _render(session, tick[0])

    tree_ctrl = FormattedTextControl(_get_tree_text, focusable=False)
    tree_win = Window(
        content=tree_ctrl,
        dont_extend_height=False,
    )

    # ── separator line ────────────────────────────────────────────────────
    sep_ctrl = FormattedTextControl(
        lambda: [("class:separator", "─" * 80)]
    )
    sep_win = Window(content=sep_ctrl, height=1)

    # ── input field ───────────────────────────────────────────────────────
    input_buf = Buffer(name="hwo_input", multiline=False)

    def _get_input_text():
        prompt_str = "  > "
        err = error_msg[0]
        parts = [("class:input.prefix", prompt_str)]
        if err:
            parts.append(("class:task.error", f"  ← {err}"))
        return parts

    input_prefix_ctrl = FormattedTextControl(_get_input_text, focusable=False)
    input_prefix_win = Window(content=input_prefix_ctrl, height=1,
                              dont_extend_width=True, width=4)

    input_win = Window(content=BufferControl(buffer=input_buf),
                       height=1, dont_extend_height=True)

    # ── help bar ─────────────────────────────────────────────────────────
    help_win = Window(content=FormattedTextControl(_render_help), height=1)

    from prompt_toolkit.layout import VSplit
    input_row = VSplit([input_prefix_win, input_win])

    layout = Layout(
        HSplit([
            tree_win,
            sep_win,
            input_row,
            help_win,
        ]),
        focused_element=input_win,
    )

    # ── key bindings ──────────────────────────────────────────────────────
    kb = KeyBindings()

    def _process_input(text: str) -> None:
        text = text.strip()
        error_msg[0] = ""
        if not text:
            return

        # task
        m_task = re.match(r'^->\s*(.+)$', text)
        if m_task:
            task_text = m_task.group(1).strip()
            if session.add_task(task_text) is None:
                error_msg[0] = "Create an #agent# first"
            return

        # agent
        m_agent = re.match(r'^#([^#]+)#\s*$', text)
        if m_agent:
            name = m_agent.group(1).strip()
            session.add_agent(name, parallel=_next_parallel[0])
            _next_parallel[0] = False
            return

        # parallel separator
        if re.match(r'^/{2,}\s*$', text) or text == "////":
            _next_parallel[0] = True
            return

        error_msg[0] = f"Unknown: use -> task, #agent#, or ////"

    @kb.add("enter")
    def _(event):
        text = input_buf.text
        input_buf.reset()
        _process_input(text)

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("c-d")
    def _(event):
        event.app.exit()

    # ── tick thread ───────────────────────────────────────────────────────
    stop_evt = threading.Event()

    def _ticker(app):
        while not stop_evt.is_set():
            time.sleep(0.18)
            tick[0] += 1
            try:
                app.invalidate()
            except Exception:
                break

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=_STYLE,
        full_screen=True,
        refresh_interval=0.2,
        mouse_support=False,
    )

    ticker_thread = threading.Thread(target=_ticker, args=(app,), daemon=True)
    ticker_thread.start()

    try:
        app.run()
    finally:
        stop_evt.set()
