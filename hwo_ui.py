"""HWO — /hwo command: visual agent-orchestration builder TUI.

Input grammar (typed at the bottom prompt):
  #name#        — create an agent node
  -> text        — add a task to the current agent
  text           — same as -> text
  //             — insert ─── parallel ─── separator (first call)
  //             — insert ─── end ───      separator (second call)
  r key          — start executing all pending tasks
  q / Esc / Ctrl-C — exit

Execution states per task:
  □  pending  (before r is pressed)
  ◰◳◲◱ running (spinning rectangle, 4-frame loop)
  ✓  done
  ✗  error
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, FormattedTextControl
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.styles import Style

# ── Animation frames ──────────────────────────────────────────────────────
_SPIN  = ["◰", "◳", "◲", "◱"]
_IDLE  = "□"
_DONE  = "✓"
_ERROR = "✗"


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class HwoTask:
    text: str
    status: str = "pending"   # pending | running | done | error


@dataclass
class HwoSeparator:
    kind: str                  # "parallel" | "end"


@dataclass
class HwoAgent:
    name: str
    tasks: list[HwoTask] = field(default_factory=list)


@dataclass
class HwoSession:
    root_name: str
    nodes: list = field(default_factory=list)   # HwoAgent | HwoSeparator
    _parallel_open: bool = False
    _last_agent: Optional[HwoAgent] = None

    def add_agent(self, name: str) -> HwoAgent:
        agent = HwoAgent(name=name)
        self.nodes.append(agent)
        self._last_agent = agent
        return agent

    def add_task(self, text: str) -> Optional[HwoTask]:
        if self._last_agent is None:
            return None
        task = HwoTask(text=text, status="pending")
        self._last_agent.tasks.append(task)
        return task

    def toggle_parallel(self) -> HwoSeparator:
        if not self._parallel_open:
            sep = HwoSeparator(kind="parallel")
            self._parallel_open = True
        else:
            sep = HwoSeparator(kind="end")
            self._parallel_open = False
        self.nodes.append(sep)
        return sep

    def all_tasks(self) -> list[HwoTask]:
        result = []
        for node in self.nodes:
            if isinstance(node, HwoAgent):
                result.extend(node.tasks)
        return result

    def has_pending(self) -> bool:
        return any(t.status == "pending" for t in self.all_tasks())


# ── Renderer ──────────────────────────────────────────────────────────────

def _spin_char(tick: int, offset: int = 0) -> str:
    return _SPIN[(tick + offset) % len(_SPIN)]


def _task_mark(task: HwoTask, tick: int, executing: bool, offset: int = 0) -> tuple:
    if task.status == "running":
        return (_spin_char(tick, offset), "class:task.run")
    if task.status == "done":
        return (_DONE, "class:task.done")
    if task.status == "error":
        return (_ERROR, "class:task.err")
    # pending
    return (_IDLE, "class:task.pend")


def _render(session: HwoSession, tick: int, executing: bool, error_msg: str) -> list:
    out = []

    def push(style, text):
        out.append((style, text))

    # ── header ────────────────────────────────────────────────────────────
    push("class:header", "  HWO  ·  Agent: ")
    push("class:header.name", session.root_name)
    if executing:
        push("class:running", "  [running]")
    push("class:header", "\n\n")

    if not session.nodes:
        push("class:dim", "  #agent-name#   — create an agent\n")
        push("class:dim", "  -> task text   — add a task\n")
        push("class:dim", "  //             — parallel / end separator\n")
        push("class:dim", "  r              — execute\n")
        return out

    in_parallel = False

    for node in session.nodes:
        if isinstance(node, HwoSeparator):
            in_parallel = (node.kind == "parallel")
            label = " parallel " if node.kind == "parallel" else " end "
            push("class:separator", f"  {'─' * 6}{label}{'─' * 6}\n\n")
            continue

        # HwoAgent
        indent = "    " if in_parallel else "  "
        spin = _spin_char(tick, offset=1)
        push("class:agent.spin", f"{indent}{spin} ")
        push("class:agent.name", f"#{node.name}#")
        push("class:agent", "\n")

        if node.tasks:
            for i, task in enumerate(node.tasks):
                mark, style = _task_mark(task, tick, executing, offset=i + 2)
                task_indent = indent + "    "
                push(style, f"{task_indent}{mark} → ")
                push("class:task.text", f"{task.text}\n")
        else:
            push("class:dim", f"{indent}    (no tasks)\n")

        push("", "\n")

    return out


def _render_status(error_msg: str, executing: bool, session: HwoSession) -> list:
    if error_msg:
        return [("class:task.err", f"  ✗ {error_msg}")]
    if executing:
        done   = sum(1 for t in session.all_tasks() if t.status == "done")
        total  = len(session.all_tasks())
        errors = sum(1 for t in session.all_tasks() if t.status == "error")
        parts  = f"{done}/{total} done"
        if errors:
            parts += f"  {errors} failed"
        return [("class:running", f"  executing … {parts}  │  q=quit")]
    return [("class:help", "  → task  │  #agent#  │  //  │  r=run  │  q=quit")]


# ── Style ─────────────────────────────────────────────────────────────────

_STYLE = Style.from_dict({
    "header":       "bold cyan",
    "header.name":  "bold white",
    "running":      "bold yellow",
    "agent.spin":   "yellow",
    "agent.name":   "bold magenta",
    "agent":        "",
    "task.run":     "cyan",
    "task.done":    "green",
    "task.err":     "red",
    "task.pend":    "dim",
    "task.text":    "white",
    "separator":    "bold blue",
    "dim":          "dim",
    "help":         "dim italic",
    "input.prefix": "bold green",
})


# ── Execution simulator ───────────────────────────────────────────────────

def _simulate_execution(session: HwoSession, app, stop_evt: threading.Event) -> None:
    """Mark pending tasks running then done/error with small delays."""
    tasks = [t for t in session.all_tasks() if t.status == "pending"]
    for task in tasks:
        task.status = "running"
    try:
        app.invalidate()
    except Exception:
        return

    for task in tasks:
        if stop_evt.is_set():
            break
        delay = random.uniform(0.8, 2.2)
        deadline = time.time() + delay
        while time.time() < deadline:
            if stop_evt.is_set():
                return
            time.sleep(0.05)
        task.status = "done" if random.random() > 0.15 else "error"
        try:
            app.invalidate()
        except Exception:
            return


# ── TUI entry point ───────────────────────────────────────────────────────

def run_hwo_ui(root_agent_name: str) -> None:
    """Launch the /hwo full-screen orchestration builder."""
    session   = HwoSession(root_name=root_agent_name)
    tick      = [0]
    executing = [False]
    error_msg = [""]
    stop_evt  = threading.Event()

    # ── tree panel ────────────────────────────────────────────────────────
    tree_ctrl = FormattedTextControl(
        lambda: _render(session, tick[0], executing[0], error_msg[0]),
        focusable=False,
    )
    tree_win = Window(content=tree_ctrl)

    sep_win = Window(
        content=FormattedTextControl(lambda: [("class:separator", "─" * 80)]),
        height=1,
    )

    status_win = Window(
        content=FormattedTextControl(
            lambda: _render_status(error_msg[0], executing[0], session)
        ),
        height=1,
    )

    # ── input row ─────────────────────────────────────────────────────────
    input_buf = Buffer(name="hwo_input", multiline=False)
    prefix_win = Window(
        content=FormattedTextControl(
            lambda: [("class:input.prefix", "  > ")], focusable=False
        ),
        width=4, dont_extend_width=True,
    )
    input_win = Window(
        content=BufferControl(buffer=input_buf),
        height=1, dont_extend_height=True,
    )
    input_row = VSplit([prefix_win, input_win])

    layout = Layout(
        HSplit([tree_win, sep_win, status_win, input_row]),
        focused_element=input_win,
    )

    # ── key bindings ──────────────────────────────────────────────────────
    kb = KeyBindings()

    def _process(raw: str) -> None:
        import re
        text = raw.strip()
        error_msg[0] = ""
        if not text:
            return

        # parallel toggle
        if re.fullmatch(r'/+', text):
            session.toggle_parallel()
            return

        # agent
        m = re.fullmatch(r'#([^#]+)#\s*', text)
        if m:
            name = m.group(1).strip()
            if not name:
                error_msg[0] = "Agent name cannot be empty"
                return
            session.add_agent(name)
            return

        # task (-> prefix optional)
        m2 = re.match(r'^->\s*(.+)$', text, re.DOTALL)
        task_text = m2.group(1).strip() if m2 else text
        if session.add_task(task_text) is None:
            error_msg[0] = "Create an #agent# first"

    @kb.add("enter")
    def _(event):
        raw = input_buf.text
        input_buf.reset()
        _process(raw)

    @kb.add("r")
    def _(event):
        if executing[0]:
            return
        if not session.has_pending():
            error_msg[0] = "No pending tasks to run"
            return
        error_msg[0] = ""
        executing[0] = True
        app_ref = event.app

        def _exec():
            _simulate_execution(session, app_ref, stop_evt)
            executing[0] = False
            try:
                app_ref.invalidate()
            except Exception:
                pass

        threading.Thread(target=_exec, daemon=True).start()

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("c-d")
    def _(event):
        stop_evt.set()
        event.app.exit()

    # ── tick thread ───────────────────────────────────────────────────────
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
    stop_evt.clear()

    try:
        app.run()
    finally:
        stop_evt.set()
