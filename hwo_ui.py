"""HWO — /hwo command: visual agent-orchestration builder TUI.

Input grammar (typed at the bottom prompt):
  #name#          — create an agent node
  -> text          — add a task to the current agent (plain text also works)
  //               — insert ─── parallel ─── separator (first call)
  //               — insert ─── end ───      separator (second call)
  r key            — execute all pending tasks
  ↑ ↓ PgUp PgDn   — scroll the tree view
  q / Esc / Ctrl-C — exit

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


# ── Full-content renderer (returns ALL lines, no clipping) ────────────────

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


def _render_all(session: HwoSession, tick: int, executing: bool) -> list:
    """Return ALL formatted-text chunks — callers clip by scroll offset."""
    out = []

    def push(style, text):
        out.append((style, text))

    push("class:header", "  HWO  ·  Agent: ")
    push("class:header.name", session.root_name)
    if executing:
        push("class:running", "  [running]")
    push("class:header", "\n\n")

    if not session.nodes:
        push("class:dim", "  #agent-name#   — create an agent\n")
        push("class:dim", "  -> task text   — add a task\n")
        push("class:dim", "  //             — parallel / end separator\n")
        push("class:dim", "  r              — execute  │  ↑↓ scroll\n")
        return out

    in_parallel = False
    for node in session.nodes:
        if isinstance(node, HwoSeparator):
            in_parallel = (node.kind == "parallel")
            label = " parallel " if node.kind == "parallel" else " end "
            push("class:separator", f"  {'─' * 6}{label}{'─' * 6}\n\n")
            continue

        indent = "    " if in_parallel else "  "
        spin = _spin_char(tick, offset=1)
        push("class:agent.spin", f"{indent}{spin} ")
        push("class:agent.name", f"#{node.name}#")
        push("class:agent", "\n")

        if node.tasks:
            for i, task in enumerate(node.tasks):
                mark, style = _task_mark(task, tick, offset=i + 2)
                push(style, f"{indent}    {mark} → ")
                push("class:task.text", f"{task.text}\n")
        else:
            push("class:dim", f"{indent}    (no tasks)\n")

        push("", "\n")

    return out


# ── Line splitter ─────────────────────────────────────────────────────────

def _to_lines(chunks: list) -> list:
    """Split [(style, text)] into [[(style, text), ...], ...] by newline."""
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


# ── Scrollbar renderer ────────────────────────────────────────────────────

def _render_scrollbar(total: int, visible: int, top: int, height: int) -> list:
    """Return a narrow right-side scrollbar column."""
    out = []
    if total <= visible:
        # No scrollbar needed — draw plain dim rail
        for _ in range(height):
            out.append(("class:scrollbar.rail", "  \n"))
        return out

    # Thumb position and size
    thumb_size = max(1, int(visible / total * height))
    thumb_top  = int(top / max(1, total - visible) * (height - thumb_size))

    for row in range(height):
        in_thumb = thumb_top <= row < thumb_top + thumb_size
        if in_thumb:
            out.append(("class:scrollbar.thumb", " █\n"))
        else:
            out.append(("class:scrollbar.rail",  " │\n"))
    return out


# ── Status bar ────────────────────────────────────────────────────────────

def _render_status(error_msg: str, executing: bool, session: HwoSession,
                   total_lines: int, visible_lines: int, scroll_top: int) -> list:
    if error_msg:
        return [("class:task.err", f"  ✗ {error_msg}")]

    scroll_info = ""
    if total_lines > visible_lines:
        pct = int(scroll_top / max(1, total_lines - visible_lines) * 100)
        scroll_info = (
            f"  ↑↓/{total_lines}L  {pct}%"
        )

    if executing:
        done   = sum(1 for t in session.all_tasks() if t.status == "done")
        total  = len(session.all_tasks())
        errors = sum(1 for t in session.all_tasks() if t.status == "error")
        parts  = f"{done}/{total} done"
        if errors:
            parts += f"  {errors} failed"
        return [("class:running", f"  executing … {parts}{scroll_info}  │  q=quit")]

    base = "  → task  │  #agent#  │  //  │  r=run  │  ↑↓ scroll  │  q=quit"
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
    session    = HwoSession(root_name=root_agent_name)
    tick       = [0]
    executing  = [False]
    error_msg  = [""]
    scroll_top = [0]
    stop_evt   = threading.Event()

    # Mutable totals shared between content getter and status bar
    _totals = {"total": 0, "visible": 0}

    def _visible_height() -> int:
        try:
            rows = shutil.get_terminal_size((80, 24)).lines
        except Exception:
            rows = 24
        # Reserve: 2 header + 1 sep + 1 status + 1 input + 1 border ≈ 6
        return max(4, rows - 6)

    def _get_tree_content():
        chunks = _render_all(session, tick[0], executing[0])
        all_lines = _to_lines(chunks)
        total   = len(all_lines)
        visible = _visible_height()

        # Clamp scroll
        scroll_top[0] = max(0, min(scroll_top[0], max(0, total - visible)))
        _totals["total"]   = total
        _totals["visible"] = visible

        sliced = all_lines[scroll_top[0]: scroll_top[0] + visible]
        result = []
        for line in sliced:
            result.extend(line)
            result.append(("", "\n"))
        return result

    def _get_scrollbar():
        total   = _totals["total"]
        visible = _totals["visible"]
        height  = visible
        return _render_scrollbar(total, visible, scroll_top[0], height)

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
    input_win  = Window(
        content=BufferControl(buffer=input_buf),
        height=1, dont_extend_height=True,
    )
    input_row  = VSplit([prefix_win, input_win])

    # Tree + scrollbar side-by-side
    tree_row = VSplit([tree_win, scroll_win])

    layout = Layout(
        HSplit([tree_row, sep_win, status_win, input_row]),
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

        if re.fullmatch(r'/+', text):
            session.toggle_parallel()
            return

        m = re.fullmatch(r'#([^#]+)#\s*', text)
        if m:
            name = m.group(1).strip()
            if not name:
                error_msg[0] = "Agent name cannot be empty"
                return
            session.add_agent(name)
            return

        m2 = re.match(r'^->\s*(.+)$', text, re.DOTALL)
        task_text = m2.group(1).strip() if m2 else text
        if session.add_task(task_text) is None:
            error_msg[0] = "Create an #agent# first"

    @kb.add("enter")
    def _(event):
        raw = input_buf.text
        input_buf.reset()
        _process(raw)

    # ── scrolling ─────────────────────────────────────────────────────────
    @kb.add("up")
    def _(event):
        scroll_top[0] = max(0, scroll_top[0] - 1)

    @kb.add("down")
    def _(event):
        scroll_top[0] += 1   # clamped in _get_tree_content

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

    # ── execution ─────────────────────────────────────────────────────────
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
