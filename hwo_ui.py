"""HWO — /hwo command: visual agent-orchestration builder TUI.

Input grammar (typed at the bottom prompt):
  #name#          — create an agent node (auto-opens its body scope)
  -> text         — add a serial task/step to the current agent
  text            — same as -> text (plain text inside agent body)
  //              — open a parallel block  (when not inside one)
  //              — close the parallel block (when inside one)
  {               — enter the last agent's body (push scope deeper)
  }               — exit current agent body (pop scope back up)
  q / Esc / Ctrl-C — exit the UI

HWO rules mirrored from the runtime:
  - Parallel blocks may only contain #agent# nodes, not plain text.
  - //  opens,  //  closes.  Everything in between runs concurrently.
  - Agents outside a // block run serially top-to-bottom.
  - -> separates serial steps inside an agent body.
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Union

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, FormattedTextControl
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

# ── Spin frames (rotating rectangle) ─────────────────────────────────────
_SPIN  = ["◰", "◳", "◲", "◱"]
_DONE  = "■"
_ERROR = "✗"
_IDLE  = "□"


# ── HWO data model ────────────────────────────────────────────────────────

@dataclass
class HwoTask:
    kind: str = "task"
    text: str = ""
    status: str = "pending"   # pending | running | done | error


@dataclass
class HwoParallel:
    kind: str = "parallel"
    agents: list["HwoAgent"] = field(default_factory=list)


@dataclass
class HwoAgent:
    kind: str = "agent"
    name: str = ""
    body: list[Union[HwoTask, "HwoAgent", HwoParallel]] = field(default_factory=list)


# ── Session / scope state ─────────────────────────────────────────────────

@dataclass
class HwoSession:
    root_name: str                                      # top-level agent (e.g. "primary")
    root_body: list[Union[HwoTask, HwoAgent, HwoParallel]] = field(default_factory=list)

    # mutable editor state (not part of HWO data model)
    _scope_stack: list[list] = field(default_factory=list)   # stack of body lists
    _name_stack: list[str] = field(default_factory=list)     # agent names for breadcrumb
    _parallel_stack: list[HwoParallel] = field(default_factory=list)  # open // blocks
    _last_agent: Optional[HwoAgent] = field(default=None)    # most recently added agent

    def __post_init__(self):
        # scope_stack[0] always points to root_body
        self._scope_stack = [self.root_body]

    # ── helpers ───────────────────────────────────────────────────────────

    @property
    def _current_list(self) -> list:
        return self._scope_stack[-1]

    @property
    def in_parallel(self) -> bool:
        return bool(self._parallel_stack)

    @property
    def depth(self) -> int:
        return len(self._scope_stack) - 1

    def add_agent(self, name: str) -> HwoAgent:
        agent = HwoAgent(name=name)
        if self.in_parallel:
            self._parallel_stack[-1].agents.append(agent)
        else:
            self._current_list.append(agent)
        self._last_agent = agent
        return agent

    def add_task(self, text: str) -> Optional[HwoTask]:
        if self.in_parallel:
            return None        # plain text not allowed inside // block
        task = HwoTask(text=text, status="running")
        self._current_list.append(task)
        return task

    def enter_agent(self) -> bool:
        """Push into the last-added agent's body scope."""
        if self._last_agent is None:
            return False
        self._scope_stack.append(self._last_agent.body)
        self._name_stack.append(self._last_agent.name)
        self._last_agent = None
        return True

    def exit_scope(self) -> bool:
        """Pop one scope level (mirrors `}`)."""
        if len(self._scope_stack) <= 1:
            return False
        self._scope_stack.pop()
        if self._name_stack:
            self._name_stack.pop()
        return True

    def open_parallel(self) -> HwoParallel:
        block = HwoParallel()
        self._current_list.append(block)
        self._parallel_stack.append(block)
        return block

    def close_parallel(self) -> bool:
        if not self._parallel_stack:
            return False
        self._parallel_stack.pop()
        return True


# ── Renderer ──────────────────────────────────────────────────────────────

def _spin(tick: int, offset: int = 0) -> str:
    return _SPIN[(tick + offset) % len(_SPIN)]


def _render_body(
    items: list,
    tick: int,
    base_indent: int,
    out: list,
) -> None:
    pad = "  " * base_indent

    for item in items:
        if item.kind == "task":
            if item.status == "running":
                mark = _spin(tick, offset=base_indent)
                style = "class:task.run"
            elif item.status == "done":
                mark, style = _DONE, "class:task.done"
            elif item.status == "error":
                mark, style = _ERROR, "class:task.err"
            else:
                mark, style = _IDLE, "class:task.pend"
            out.append((style, f"{pad}{mark} → "))
            out.append(("class:task.text", f"{item.text}\n"))

        elif item.kind == "agent":
            spin = _spin(tick, offset=base_indent + 1)
            out.append(("class:agent.spin", f"{pad}{spin} "))
            out.append(("class:agent.name", f"#{item.name}#"))
            out.append(("class:agent.brace", " {\n"))
            _render_body(item.body, tick, base_indent + 1, out)
            if not item.body:
                out.append(("class:dim", f"{'  ' * (base_indent + 1)}(empty)\n"))
            out.append(("class:agent.brace", f"{pad}}}\n"))

        elif item.kind == "parallel":
            out.append(("class:parallel", f"{pad}//\n"))
            for agent in item.agents:
                spin = _spin(tick, offset=base_indent + 1)
                inner = "  " * (base_indent + 1)
                out.append(("class:agent.spin", f"{inner}{spin} "))
                out.append(("class:agent.name", f"#{agent.name}#"))
                out.append(("class:agent.brace", " {\n"))
                _render_body(agent.body, tick, base_indent + 2, out)
                if not agent.body:
                    out.append(("class:dim", f"{'  ' * (base_indent + 2)}(empty)\n"))
                out.append(("class:agent.brace", f"{inner}}}\n"))
            if not item.agents:
                out.append(("class:dim", f"{'  ' * (base_indent + 1)}(add #agent# nodes here)\n"))
            out.append(("class:parallel", f"{pad}//\n"))

        out.append(("", "\n"))


def _render(session: HwoSession, tick: int) -> list:
    out: list = []

    # ── header ────────────────────────────────────────────────────────────
    out.append(("class:header", "  HWO  ·  Agent: "))
    out.append(("class:header.name", session.root_name))

    # scope breadcrumb
    if session._name_stack:
        crumbs = " > ".join(f"#{n}#" for n in session._name_stack)
        out.append(("class:dim", f"  [{crumbs}]"))

    # parallel indicator
    if session.in_parallel:
        out.append(("class:parallel", "  [// parallel //]"))

    out.append(("class:header", "\n\n"))

    # ── tree body ─────────────────────────────────────────────────────────
    if not session.root_body:
        out.append(("class:dim", "  #agent-name#      — create an agent\n"))
        out.append(("class:dim", "  -> task text      — add a serial step\n"))
        out.append(("class:dim", "  //                — open/close parallel block\n"))
        out.append(("class:dim", "  { / }             — enter / exit agent body\n"))
    else:
        _render_body(session.root_body, tick, base_indent=1, out=out)

    return out


def _render_status(session: HwoSession, error_msg: str) -> list:
    items = []
    if error_msg:
        items.append(("class:task.err", f"  ✗ {error_msg}"))
    else:
        depth_info = f"depth={session.depth}" if session.depth > 0 else ""
        par_info = "PARALLEL" if session.in_parallel else ""
        meta = "  ".join(filter(None, [depth_info, par_info]))
        items.append(("class:help", f"  → task │ #agent# │ // │ {{ }} │ q=quit"
                       + (f"  [{meta}]" if meta else "")))
    return items


# ── Styles ────────────────────────────────────────────────────────────────

_STYLE = Style.from_dict({
    "header":       "bold cyan",
    "header.name":  "bold white",
    "agent.spin":   "yellow",
    "agent.name":   "bold magenta",
    "agent.brace":  "dim cyan",
    "task.run":     "cyan",
    "task.done":    "green",
    "task.err":     "red",
    "task.pend":    "dim",
    "task.text":    "white",
    "parallel":     "bold blue",
    "dim":          "dim",
    "help":         "dim italic",
    "input.prefix": "bold green",
    "separator":    "dim",
})


# ── Main TUI ──────────────────────────────────────────────────────────────

def run_hwo_ui(root_agent_name: str) -> None:
    """Launch the /hwo full-screen orchestration builder."""
    session = HwoSession(root_name=root_agent_name)
    tick = [0]
    error_msg = [""]

    # ── tree view ─────────────────────────────────────────────────────────
    tree_ctrl = FormattedTextControl(
        lambda: _render(session, tick[0]), focusable=False
    )
    tree_win = Window(content=tree_ctrl)

    # ── separator ─────────────────────────────────────────────────────────
    sep_ctrl = FormattedTextControl(lambda: [("class:separator", "─" * 80)])
    sep_win = Window(content=sep_ctrl, height=1)

    # ── status / help line ────────────────────────────────────────────────
    status_ctrl = FormattedTextControl(
        lambda: _render_status(session, error_msg[0]), focusable=False
    )
    status_win = Window(content=status_ctrl, height=1)

    # ── input field ───────────────────────────────────────────────────────
    input_buf = Buffer(name="hwo_input", multiline=False)

    prefix_ctrl = FormattedTextControl(
        lambda: [("class:input.prefix", "  > ")], focusable=False
    )
    prefix_win = Window(content=prefix_ctrl, width=4, dont_extend_width=True)
    input_win  = Window(content=BufferControl(buffer=input_buf),
                        height=1, dont_extend_height=True)
    input_row  = VSplit([prefix_win, input_win])

    layout = Layout(
        HSplit([tree_win, sep_win, status_win, input_row]),
        focused_element=input_win,
    )

    # ── key bindings ──────────────────────────────────────────────────────
    kb = KeyBindings()

    def _process(raw: str) -> None:
        text = raw.strip()
        error_msg[0] = ""
        if not text:
            return

        # Enter agent body
        if text == "{":
            if not session.enter_agent():
                error_msg[0] = "No agent to enter — create one first with #name#"
            return

        # Exit scope
        if text == "}":
            if not session.exit_scope():
                error_msg[0] = "Already at root scope"
            return

        # Parallel toggle
        if re.fullmatch(r'/+', text):
            if session.in_parallel:
                session.close_parallel()
            else:
                session.open_parallel()
            return

        # Agent node
        m = re.fullmatch(r'#([^#{}]+)#\s*', text)
        if m:
            name = m.group(1).strip()
            if not name:
                error_msg[0] = "Agent name cannot be empty"
                return
            session.add_agent(name)
            return

        # Task / serial step (-> prefix optional)
        m2 = re.match(r'^->\s*(.+)$', text, re.DOTALL)
        task_text = m2.group(1).strip() if m2 else text
        if session.in_parallel:
            error_msg[0] = "Inside // — only #agent# nodes allowed, not tasks"
            return
        # Allow tasks at root scope too (HWO supports top-level serial text)
        session.add_task(task_text)

    @kb.add("enter")
    def _(event):
        raw = input_buf.text
        input_buf.reset()
        _process(raw)

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("c-d")
    def _(event):
        event.app.exit()

    # ── tick thread (drives spinner animation) ────────────────────────────
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

    ticker = threading.Thread(target=_ticker, args=(app,), daemon=True)
    ticker.start()

    try:
        app.run()
    finally:
        stop_evt.set()
