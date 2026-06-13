"""HWO — /hwo command: visual agent-orchestration builder TUI.

Input grammar (typed at the bottom prompt):
  #name#                — create top-level agent (no duplicate names at same level)
  -> #name#             — create sub-agent under current agent (indented)
  -> text  /  text      — add task to current agent
  //                    — toggle ─── parallel ─── / ─── end ─── separator
  #AgentName#[N]->text  — insert task at position N (1-based) in named agent
  #AgentName#[N]-x>     — delete task at position N in named agent
  #AgentName#[N]-x>txt  — replace task at position N in named agent
  r                     — execute all pending tasks
  ↑ ↓ PgUp PgDn Home End — scroll the tree view
  q / Esc / Ctrl-C      — exit

Task icons:
  □  pending   ◰◳◲◱ running   ✓ done   ✗ error
"""

from __future__ import annotations

import random
import shutil
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
    tasks: list = field(default_factory=list)      # list[HwoTask]
    children: list = field(default_factory=list)   # list[HwoAgent]
    parent: Optional["HwoAgent"] = field(default=None, repr=False)

    def all_tasks(self) -> list:
        result = list(self.tasks)
        for child in self.children:
            result.extend(child.all_tasks())
        return result


@dataclass
class HwoSession:
    root_name: str
    nodes: list = field(default_factory=list)   # HwoAgent | HwoSeparator (top level)
    _parallel_open: bool = False
    _last_agent: Optional[HwoAgent] = field(default=None, repr=False)

    # ── helpers ───────────────────────────────────────────────────────────

    def _top_agents(self) -> list:
        return [n for n in self.nodes if isinstance(n, HwoAgent)]

    def _siblings_of(self, agent: HwoAgent) -> list:
        if agent.parent is None:
            return self._top_agents()
        return agent.parent.children

    def find_agent(self, name: str) -> Optional[HwoAgent]:
        def _search(nodes) -> Optional[HwoAgent]:
            for node in nodes:
                if isinstance(node, HwoAgent):
                    if node.name == name:
                        return node
                    found = _search(node.children)
                    if found:
                        return found
            return None
        return _search(self.nodes)

    # ── mutations ─────────────────────────────────────────────────────────

    def add_agent(self, name: str) -> Optional[str]:
        """Add a top-level agent. Returns error string or None."""
        for n in self._top_agents():
            if n.name == name:
                return f"Agent '{name}' already exists at top level"
        agent = HwoAgent(name=name)
        self.nodes.append(agent)
        self._last_agent = agent
        return None

    def add_child_agent(self, name: str) -> Optional[str]:
        """Add a sub-agent under _last_agent. Returns error string or None."""
        if self._last_agent is None:
            return "Create a parent #agent# first"
        parent = self._last_agent
        for child in parent.children:
            if child.name == name:
                return f"Agent '{name}' already exists under #{parent.name}#"
        child = HwoAgent(name=name, parent=parent)
        parent.children.append(child)
        self._last_agent = child
        return None

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

    def insert_task(self, agent_name: str, idx1: int, text: str) -> Optional[str]:
        agent = self.find_agent(agent_name)
        if agent is None:
            return f"Agent '{agent_name}' not found"
        idx = idx1 - 1
        task = HwoTask(text=text, status="pending")
        agent.tasks.insert(max(0, idx), task)
        return None

    def delete_task(self, agent_name: str, idx1: int) -> Optional[str]:
        agent = self.find_agent(agent_name)
        if agent is None:
            return f"Agent '{agent_name}' not found"
        idx = idx1 - 1
        if idx < 0 or idx >= len(agent.tasks):
            return f"Task index {idx1} out of range (agent has {len(agent.tasks)} tasks)"
        del agent.tasks[idx]
        return None

    def replace_task(self, agent_name: str, idx1: int, text: str) -> Optional[str]:
        agent = self.find_agent(agent_name)
        if agent is None:
            return f"Agent '{agent_name}' not found"
        idx = idx1 - 1
        if idx < 0 or idx >= len(agent.tasks):
            return f"Task index {idx1} out of range (agent has {len(agent.tasks)} tasks)"
        agent.tasks[idx].text = text
        return None

    def all_tasks(self) -> list:
        result = []
        for node in self.nodes:
            if isinstance(node, HwoAgent):
                result.extend(node.all_tasks())
        return result

    def has_pending(self) -> bool:
        return any(t.status == "pending" for t in self.all_tasks())


# ── Renderer ───────────────────────────────────────────────────────────────

def _spin_char(tick: int, offset: int = 0) -> str:
    return _SPIN[(tick + offset) % len(_SPIN)]


def _task_mark(task: HwoTask, tick: int, offset: int = 0) -> tuple:
    if task.status == "running":
        return (_spin_char(tick, offset), "class:task.run")
    if task.status == "done":
        return (_DONE, "class:task.done")
    if task.status == "error":
        return (_ERROR, "class:task.err")
    return (_IDLE, "class:task.pend")


def _render_agent(out: list, agent: HwoAgent, indent: str, tick: int) -> None:
    spin = _spin_char(tick, offset=1)
    out.append(("class:agent.spin", f"{indent}{spin} "))
    out.append(("class:agent.name", f"#{agent.name}#"))
    out.append(("class:agent", "\n"))

    if agent.tasks:
        for i, task in enumerate(agent.tasks):
            mark, style = _task_mark(task, tick, offset=i + 2)
            out.append((style,            f"{indent}    {mark} → "))
            out.append(("class:task.text", f"{task.text}\n"))
    else:
        out.append(("class:dim", f"{indent}    (no tasks)\n"))

    for child in agent.children:
        _render_agent(out, child, indent + "    ", tick)

    out.append(("", "\n"))


def _render_all(session: HwoSession, tick: int, executing: bool) -> list:
    out: list = []

    out.append(("class:header",      "  HWO  ·  Agent: "))
    out.append(("class:header.name", session.root_name))
    if executing:
        out.append(("class:running", "  [running]"))
    out.append(("class:header", "\n\n"))

    if not session.nodes:
        out.append(("class:dim", "  #agent-name#        — create agent\n"))
        out.append(("class:dim", "  -> #child-name#     — create sub-agent\n"))
        out.append(("class:dim", "  -> task text        — add task\n"))
        out.append(("class:dim", "  #Name#[N]->task     — insert task at N\n"))
        out.append(("class:dim", "  #Name#[N]-x>        — delete task N\n"))
        out.append(("class:dim", "  #Name#[N]-x>text    — replace task N\n"))
        out.append(("class:dim", "  //                  — parallel separator\n"))
        out.append(("class:dim", "  r=run  ↑↓ scroll  q=quit\n"))
        return out

    in_parallel = False
    for node in session.nodes:
        if isinstance(node, HwoSeparator):
            in_parallel = (node.kind == "parallel")
            label = " parallel " if node.kind == "parallel" else " end "
            out.append(("class:separator", f"  {'─' * 6}{label}{'─' * 6}\n\n"))
            continue
        base_indent = "    " if in_parallel else "  "
        _render_agent(out, node, base_indent, tick)

    return out


# ── Line splitter ─────────────────────────────────────────────────────────

def _to_lines(chunks: list) -> list:
    lines: list = []
    current: list = []
    for style, text in chunks:
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if part:
                current.append((style, part))
            if i < len(parts) - 1:
                lines.append(current)
                current = []
    if current:
        lines.append(current)
    return lines


# ── Scrollbar ─────────────────────────────────────────────────────────────

def _render_scrollbar(total: int, visible: int, top: int, height: int) -> list:
    out = []
    if total <= visible:
        for _ in range(height):
            out.append(("class:scrollbar.rail", "  \n"))
        return out
    thumb_size = max(1, int(visible / total * height))
    thumb_top  = int(top / max(1, total - visible) * (height - thumb_size))
    for row in range(height):
        in_thumb = thumb_top <= row < thumb_top + thumb_size
        out.append((
            "class:scrollbar.thumb" if in_thumb else "class:scrollbar.rail",
            " █\n" if in_thumb else " │\n",
        ))
    return out


# ── Status bar ────────────────────────────────────────────────────────────

def _render_status(error_msg: str, executing: bool, session: HwoSession,
                   total_lines: int, visible_lines: int, scroll_top: int) -> list:
    if error_msg:
        return [("class:task.err", f"  ✗ {error_msg}")]

    scroll_info = ""
    if total_lines > visible_lines:
        pct = int(scroll_top / max(1, total_lines - visible_lines) * 100)
        scroll_info = f"  ↑↓/{total_lines}L {pct}%"

    if executing:
        done   = sum(1 for t in session.all_tasks() if t.status == "done")
        total  = len(session.all_tasks())
        errors = sum(1 for t in session.all_tasks() if t.status == "error")
        parts  = f"{done}/{total} done"
        if errors:
            parts += f"  {errors} failed"
        return [("class:running", f"  executing … {parts}{scroll_info}  │  q=quit")]

    base = "  → task  │  #agent#  │  -> #sub#  │  //  │  r=run  │  ↑↓  │  q=quit"
    return [("class:help", base + scroll_info)]


# ── Style ─────────────────────────────────────────────────────────────────

_STYLE = Style.from_dict({
    "header":          "bold cyan",
    "header.name":     "bold white",
    "running":         "bold yellow",
    "agent.spin":      "yellow",
    "agent.name":      "bold magenta",
    "agent":           "",
    "task.run":        "cyan",
    "task.done":       "green",
    "task.err":        "red",
    "task.pend":       "dim",
    "task.text":       "white",
    "separator":       "bold blue",
    "dim":             "dim",
    "help":            "dim italic",
    "input.prefix":    "bold green",
    "scrollbar.thumb": "bold white",
    "scrollbar.rail":  "dim",
})


# ── Execution simulator ───────────────────────────────────────────────────

def _simulate_execution(session: HwoSession, app, stop_evt: threading.Event) -> None:
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
        deadline = time.time() + random.uniform(0.8, 2.2)
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
    import re

    session    = HwoSession(root_name=root_agent_name)
    tick       = [0]
    executing  = [False]
    error_msg  = [""]
    scroll_top = [0]
    stop_evt   = threading.Event()
    _totals    = {"total": 0, "visible": 0}

    def _visible_height() -> int:
        try:
            rows = shutil.get_terminal_size((80, 24)).lines
        except Exception:
            rows = 24
        return max(4, rows - 6)

    def _get_tree_content():
        chunks    = _render_all(session, tick[0], executing[0])
        all_lines = _to_lines(chunks)
        total     = len(all_lines)
        visible   = _visible_height()
        scroll_top[0]      = max(0, min(scroll_top[0], max(0, total - visible)))
        _totals["total"]   = total
        _totals["visible"] = visible
        sliced = all_lines[scroll_top[0]: scroll_top[0] + visible]
        result = []
        for line in sliced:
            result.extend(line)
            result.append(("", "\n"))
        return result

    def _get_scrollbar():
        return _render_scrollbar(
            _totals["total"], _totals["visible"], scroll_top[0], _totals["visible"]
        )

    def _get_status():
        return _render_status(
            error_msg[0], executing[0], session,
            _totals["total"], _totals["visible"], scroll_top[0],
        )

    # ── layout ────────────────────────────────────────────────────────────
    tree_ctrl   = FormattedTextControl(_get_tree_content, focusable=False)
    scroll_ctrl = FormattedTextControl(_get_scrollbar,    focusable=False)
    status_ctrl = FormattedTextControl(_get_status,       focusable=False)

    tree_win   = Window(content=tree_ctrl,   dont_extend_height=False)
    scroll_win = Window(content=scroll_ctrl, width=2, dont_extend_width=True)
    sep_win    = Window(
        content=FormattedTextControl(lambda: [("class:scrollbar.rail", "─" * 80)]),
        height=1,
    )
    status_win = Window(content=status_ctrl, height=1)
    input_buf  = Buffer(name="hwo_input", multiline=False)
    prefix_win = Window(
        content=FormattedTextControl(
            lambda: [("class:input.prefix", "  > ")], focusable=False
        ),
        width=4, dont_extend_width=True,
    )
    input_win  = Window(content=BufferControl(buffer=input_buf),
                        height=1, dont_extend_height=True)
    input_row  = VSplit([prefix_win, input_win])
    tree_row   = VSplit([tree_win, scroll_win])

    layout = Layout(
        HSplit([tree_row, sep_win, status_win, input_row]),
        focused_element=input_win,
    )

    # ── input processor ───────────────────────────────────────────────────

    def _process(raw: str) -> None:
        text = raw.strip()
        error_msg[0] = ""
        if not text:
            return

        # // — parallel separator toggle
        if re.fullmatch(r'/+', text):
            session.toggle_parallel()
            return

        # #Name#[N]->text — insert task at position N
        m = re.fullmatch(r'#([^#]+)#\[(\d+)\]->(.*)', text)
        if m:
            name, idx1, task_text = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
            if not task_text:
                error_msg[0] = "Task text cannot be empty for insert"
                return
            err = session.insert_task(name, idx1, task_text)
            if err:
                error_msg[0] = err
            return

        # #Name#[N]-x>text  — replace task N
        # #Name#[N]-x>      — delete task N
        m = re.fullmatch(r'#([^#]+)#\[(\d+)\]-x>(.*)', text)
        if m:
            name, idx1, new_text = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
            if new_text:
                err = session.replace_task(name, idx1, new_text)
            else:
                err = session.delete_task(name, idx1)
            if err:
                error_msg[0] = err
            return

        # -> #name# — create sub-agent under current agent
        m = re.fullmatch(r'->\s*#([^#]+)#\s*', text)
        if m:
            name = m.group(1).strip()
            if not name:
                error_msg[0] = "Agent name cannot be empty"
                return
            err = session.add_child_agent(name)
            if err:
                error_msg[0] = err
            return

        # #name# — create top-level agent
        m = re.fullmatch(r'#([^#]+)#\s*', text)
        if m:
            name = m.group(1).strip()
            if not name:
                error_msg[0] = "Agent name cannot be empty"
                return
            err = session.add_agent(name)
            if err:
                error_msg[0] = err
            return

        # -> text  or  plain text — add task to current agent
        m2 = re.match(r'^->\s*(.+)$', text, re.DOTALL)
        task_text = m2.group(1).strip() if m2 else text
        if session.add_task(task_text) is None:
            error_msg[0] = "Create an #agent# first"

    # ── key bindings ──────────────────────────────────────────────────────
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        raw = input_buf.text
        input_buf.reset()
        _process(raw)

    @kb.add("up")
    def _(event):
        scroll_top[0] = max(0, scroll_top[0] - 1)

    @kb.add("down")
    def _(event):
        scroll_top[0] += 1

    @kb.add("pageup")
    def _(event):
        scroll_top[0] = max(0, scroll_top[0] - max(1, _visible_height() // 2))

    @kb.add("pagedown")
    def _(event):
        scroll_top[0] += max(1, _visible_height() // 2)

    @kb.add("home")
    def _(event):
        scroll_top[0] = 0

    @kb.add("end")
    def _(event):
        scroll_top[0] = max(0, _totals["total"] - _totals["visible"])

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
