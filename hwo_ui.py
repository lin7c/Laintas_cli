"""HWO — /hwo command: visual agent-orchestration builder TUI.

Slash commands (press / to open the command palette, then Enter):
  /r           — serialize session to temp file and execute via hwo_runner
  /w [file]    — save as .hwo  (no arg → prompts for filename)
  /h           — toggle help overlay
  /q           — quit

Input syntax:
  #name#              top-level agent
  -> #name#           sub-agent under current agent (indented)
  -> text / text      add task to current agent
  //                  toggle ─── parallel ─── / ─── end ─── separator
  #A#->task           append task to named agent A
  #A#->#B#            append sub-agent B to named agent A
  #A#/#B#->...        path-based append (navigate hierarchy)
  #A#[N]->text        insert task at position N (1-based)
  #A#[N]-x>           delete task at position N
  #A#[N]-x>text       replace task at position N

Task icons:  {symbols.SQUARE_OPEN} pending   ◰◳◲◱ running   {symbols.OK} done   {symbols.FAIL} error
Scroll:      {symbols.ARROW_U} {symbols.ARROW_D} PgUp PgDn Home End
Exit:        /q  or  Ctrl-C
"""

from __future__ import annotations

import copy
import os
import shutil
import symbols
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer, DynamicContainer, Layout, HSplit, VSplit, Window,
    FormattedTextControl)
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.styles import Style

_SPIN  = list(symbols.SPINNER_GEO)
_IDLE  = symbols.SQUARE_OPEN
_DONE  = symbols.OK
_ERROR = symbols.FAIL


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class HwoTask:
    text: str
    status: str = "pending"
    step_id: str = ""
    agent_id: str = ""
    last_event: str = ""
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass
class HwoSeparator:
    kind: str   # "parallel" | "end"


@dataclass
class HwoAgent:
    name: str
    tasks: list = field(default_factory=list)
    children: list = field(default_factory=list)
    parent: Optional["HwoAgent"] = field(default=None, repr=False)
    prompt_file: Optional[str] = None
    model: Optional[str] = None
    io: Optional[dict] = None

    def all_tasks(self) -> list:
        result = list(self.tasks)
        for child in self.children:
            result.extend(child.all_tasks())
        return result


@dataclass
class HwoSession:
    root_name: str
    nodes: list = field(default_factory=list)
    workflow_io: Optional[dict] = None
    _parallel_open: bool = False
    _last_agent: Optional[HwoAgent] = field(default=None, repr=False)

    def _top_agents(self) -> list:
        return [n for n in self.nodes if isinstance(n, HwoAgent)]

    def find_agent(self, name: str) -> Optional[HwoAgent]:
        def _search(nodes):
            for node in nodes:
                if isinstance(node, HwoAgent):
                    if node.name == name:
                        return node
                    found = _search(node.children)
                    if found:
                        return found
            return None
        return _search(self.nodes)

    def find_agent_by_path(self, path: list) -> Optional[HwoAgent]:
        if not path:
            return None
        cur = next((n for n in self.nodes
                    if isinstance(n, HwoAgent) and n.name == path[0]), None)
        if cur is None:
            return None
        for name in path[1:]:
            cur = next((c for c in cur.children if c.name == name), None)
            if cur is None:
                return None
        return cur

    def add_agent(self, name: str) -> Optional[str]:
        for n in self._top_agents():
            if n.name == name:
                return f"Agent '{name}' already exists at top level"
        agent = HwoAgent(name=name)
        self.nodes.append(agent)
        self._last_agent = agent
        return None

    def add_child_agent(self, name: str) -> Optional[str]:
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
        task = HwoTask(text=text)
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
        agent.tasks.insert(max(0, idx1 - 1), HwoTask(text=text))
        return None

    def delete_task(self, agent_name: str, idx1: int) -> Optional[str]:
        agent = self.find_agent(agent_name)
        if agent is None:
            return f"Agent '{agent_name}' not found"
        idx = idx1 - 1
        if idx < 0 or idx >= len(agent.tasks):
            return f"Index {idx1} out of range (agent has {len(agent.tasks)} tasks)"
        del agent.tasks[idx]
        return None

    def replace_task(self, agent_name: str, idx1: int, text: str) -> Optional[str]:
        agent = self.find_agent(agent_name)
        if agent is None:
            return f"Agent '{agent_name}' not found"
        idx = idx1 - 1
        if idx < 0 or idx >= len(agent.tasks):
            return f"Index {idx1} out of range (agent has {len(agent.tasks)} tasks)"
        agent.tasks[idx].text = text
        return None

    def append_task_to(self, agent_name: str, text: str) -> Optional[str]:
        agent = self.find_agent(agent_name)
        if agent is None:
            return f"Agent '{agent_name}' not found"
        agent.tasks.append(HwoTask(text=text))
        return None

    def append_child_to(self, parent_name: str, child_name: str) -> Optional[str]:
        parent = self.find_agent(parent_name)
        if parent is None:
            return f"Agent '{parent_name}' not found"
        for child in parent.children:
            if child.name == child_name:
                return f"Agent '{child_name}' already exists under #{parent_name}#"
        parent.children.append(HwoAgent(name=child_name, parent=parent))
        return None

    def all_tasks(self) -> list:
        result = []
        for node in self.nodes:
            if isinstance(node, HwoAgent):
                result.extend(node.all_tasks())
            elif isinstance(node, HwoTask):
                result.append(node)
        return result

    def has_pending(self) -> bool:
        return any(t.status == "pending" for t in self.all_tasks())


@dataclass(frozen=True)
class HwoOutlineRow:
    """One selectable Studio row, independent from terminal coordinates."""
    kind: str
    node: object
    parent: Optional[HwoAgent]
    depth: int

    @property
    def key(self) -> int:
        return id(self.node)


class StudioMode(str, Enum):
    """Exactly one interaction surface is active in Workflow Studio."""

    NAVIGATION = "navigation"
    HELP = "help"
    INSPECTOR = "inspector"
    ADD_TASK = "add_task"
    ADD_AGENT = "add_agent"
    EDIT = "edit"
    SAVE = "save"
    COMMAND = "command"
    CONFIRM_DELETE = "confirm_delete"
    CONFIRM_CANCEL = "confirm_cancel"
    RUNNING = "running"
    RESULT = "result"


@dataclass
class HwoStudioState:
    """Small sum-type style state instead of independent focus/mode flags."""

    mode: StudioMode = StudioMode.NAVIGATION
    return_mode: StudioMode = StudioMode.NAVIGATION
    target: Optional[HwoOutlineRow] = None
    destructive_selected: bool = False


def _outline_rows(session: HwoSession,
                  collapsed: Optional[set[int]] = None) -> list[HwoOutlineRow]:
    collapsed = collapsed or set()
    rows: list[HwoOutlineRow] = []

    def add_agent(agent: HwoAgent, depth: int) -> None:
        rows.append(HwoOutlineRow("agent", agent, agent.parent, depth))
        if id(agent) in collapsed:
            return
        rows.extend(HwoOutlineRow("task", task, agent, depth + 1)
                    for task in agent.tasks)
        for child in agent.children:
            add_agent(child, depth + 1)

    for node in session.nodes:
        if isinstance(node, HwoAgent):
            add_agent(node, 0)
        elif isinstance(node, HwoTask):
            rows.append(HwoOutlineRow("task", node, None, 0))
        else:
            rows.append(HwoOutlineRow("separator", node, None, 0))
    return rows


def _validate_session(session: HwoSession) -> list[str]:
    """Run the canonical parser/validator against the exact serialized draft."""
    if not session.nodes:
        return ["Workflow is empty"]
    try:
        from hwo_adapter import parse, validate
        return list(validate(parse(_session_to_hwo(session))) or [])
    except Exception as exc:
        return [f"Cannot validate workflow: {exc}"]


def _bind_runtime_step_ids(session: HwoSession, hwo_path: str) -> dict[str, HwoTask]:
    """Bind runner step IDs to UI tasks in the serializer's preorder."""
    import hwo_runner

    parsed = hwo_runner.parse_hwo(Path(hwo_path).read_text(encoding="utf-8"))
    task_steps: list[str] = []

    def walk(items, path: str = "") -> None:
        for index, item in enumerate(items):
            step_id = f"{path}.{index}" if path else str(index)
            kind = getattr(item, "kind", "")
            if kind == "task":
                task_steps.append(step_id)
            elif kind in {"agent", "parallel"}:
                walk(getattr(item, "body", []), step_id)

    walk(parsed)
    tasks = session.all_tasks()
    if len(tasks) != len(task_steps):
        # A partial zip would silently display the wrong task as running.
        raise ValueError(
            f"HWO UI/runner task mismatch ({len(tasks)} != {len(task_steps)})")
    result = {}
    for task, step_id in zip(tasks, task_steps):
        task.step_id = step_id
        result[step_id] = task
    return result


def _apply_runtime_events(tasks_by_step: dict[str, HwoTask], events) -> None:
    """Apply only exact runner step transitions; unrelated tasks stay untouched."""
    rows = events if isinstance(events, list) else [events]
    now = time.time()
    for event in rows:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        task = tasks_by_step.get(str(event.get("stepId") or ""))
        if task is None:
            continue
        task.last_event = kind
        task.agent_id = str(event.get("agentId") or task.agent_id or "")
        if kind == "step_started":
            task.status = "running"
            task.started_at = task.started_at or now
            task.completed_at = None
        elif kind == "step_completed":
            task.status = "done"
            task.completed_at = now
        elif kind == "step_failed":
            task.status = "error"
            task.completed_at = now


# ── HWO serializer ────────────────────────────────────────────────────────

def _session_to_hwo(session: HwoSession) -> str:
    lines: list = []

    def _io_text(io: Optional[dict]) -> str:
        if not io:
            return ""
        calls = []
        for kind in ("in", "out"):
            params = []
            for param in io.get(kind, []):
                name = str(param.get("name") or "")
                if not name:
                    continue
                if param.get("optional"):
                    name += "?"
                typ = param.get("type")
                value = param.get("source") or param.get("default")
                if typ:
                    name += f": {typ}"
                if value is not None:
                    name += f" = {value}"
                params.append(name)
            if params:
                calls.append(f"{kind}({', '.join(params)})")
        return f" [{', '.join(calls)}]" if calls else ""

    def _emit(agent: HwoAgent, depth: int = 0) -> None:
        pad = "  " * depth
        prompt = f"({agent.prompt_file})" if agent.prompt_file else ""
        model = f"@{agent.model}" if agent.model else ""
        lines.append(
            f"{pad}{prompt}#{agent.name}{model}#{_io_text(agent.io)} {{")
        for task in agent.tasks:
            lines.append(f"{pad}  -> {task.text}")
        for child in agent.children:
            _emit(child, depth + 1)
        lines.append(f"{pad}}}")

    if session.workflow_io:
        lines.extend([f"@line{_io_text(session.workflow_io)}", ""])

    in_parallel = False
    for node in session.nodes:
        if isinstance(node, HwoSeparator):
            if node.kind == "parallel":
                lines.append("//")
                in_parallel = True
            else:
                lines.append("//")
                lines.append("")
                in_parallel = False
        elif isinstance(node, HwoAgent):
            _emit(node, depth=1 if in_parallel else 0)
        elif isinstance(node, HwoTask):
            pad = "  " if in_parallel else ""
            lines.append(f"{pad}-> {node.text}")

    return "\n".join(lines) + "\n"


# ── .hwo file loader ─────────────────────────────────────────────────────

def _runner_agent_to_ui(ra, parent: Optional[HwoAgent] = None) -> HwoAgent:
    """Recursively convert a hwo_runner.HwoAgent → hwo_ui.HwoAgent."""
    ua = HwoAgent(
        name=ra.name, parent=parent,
        prompt_file=getattr(ra, "prompt_file", None),
        model=getattr(ra, "model", None), io=getattr(ra, "io", None))
    for item in ra.body:
        k = getattr(item, "kind", "")
        if k == "task":
            ua.tasks.append(HwoTask(text=item.text))
        elif k == "agent":
            ua.children.append(_runner_agent_to_ui(item, parent=ua))
        elif k == "parallel":
            raise ValueError(
                f"Nested parallel block inside #{ra.name}# is not editable "
                "in Workflow Studio yet; the source was not modified")
    return ua


def load_hwo_file(path: str, root_name: str = "primary") -> tuple:
    """Parse a .hwo file into an HwoSession.

    Returns (HwoSession, None) on success, (None, error_str) on failure.
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return None, str(e)

    try:
        import hwo_runner
        steps = hwo_runner.parse_hwo(source)
    except Exception as e:
        return None, f"Parse error: {e}"

    if "```" in source:
        return None, (
            "Comment blocks are not editable in Workflow Studio yet; "
            "the source was not modified")

    session = HwoSession(root_name=root_name)
    try:
        from hwo_adapter import parse as parse_ast
        workflow = next((node for node in parse_ast(source)
                         if node.get("type") == "workflow"), None)
        if workflow is not None:
            session.workflow_io = workflow.get("io")
    except Exception as exc:
        return None, f"Parse error: {exc}"

    try:
        for item in steps:
            k = getattr(item, "kind", "")
            if k == "parallel":
                session.nodes.append(HwoSeparator(kind="parallel"))
                session._parallel_open = True
                for sub in item.body:
                    if getattr(sub, "kind", "") != "agent":
                        raise ValueError(
                            "Parallel blocks containing non-Agent nodes are not editable")
                    ua = _runner_agent_to_ui(sub)
                    session.nodes.append(ua)
                    session._last_agent = ua
                session.nodes.append(HwoSeparator(kind="end"))
                session._parallel_open = False
            elif k == "agent":
                ua = _runner_agent_to_ui(item)
                session.nodes.append(ua)
                session._last_agent = ua
            elif k == "task":
                session.nodes.append(HwoTask(text=item.text))
    except ValueError as exc:
        return None, str(exc)

    return session, None


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
    out.append(("class:agent.spin", f"{indent}{_spin_char(tick, 1)} "))
    out.append(("class:agent.name", f"#{agent.name}#"))
    out.append(("class:agent", "\n"))
    if agent.tasks:
        for i, task in enumerate(agent.tasks):
            mark, style = _task_mark(task, tick, offset=i + 2)
            out.append((style,             f"{indent}    {mark} → "))
            out.append(("class:task.text", f"{task.text}\n"))
    else:
        out.append(("class:dim", f"{indent}    (no tasks)\n"))
    for child in agent.children:
        _render_agent(out, child, indent + "    ", tick)
    out.append(("", "\n"))


_HELP_LINES = [
    ("class:header",    "  WORKFLOW STUDIO — keyboard\n\n"),
    ("class:dim",       "  ↑↓ or j/k      select workflow node\n"),
    ("class:dim",       "  Enter / Space  expand or collapse Agent\n"),
    ("class:dim",       "  a / A          add task / add Agent\n"),
    ("class:dim",       "  e / d          edit / delete with confirmation\n"),
    ("class:dim",       "  r / s          run / save\n"),
    ("class:dim",       "  i / Tab        inspect selected node\n"),
    ("class:dim",       "  :              open HWO command palette\n"),
    ("class:dim",       "  # / -          quick-open palette with DSL prefix\n"),
    ("class:dim",       "  u              undo last deletion\n\n"),
    ("class:header",    "  Command palette\n\n"),
    ("class:dim",       "  /r            run workflow (temp file)\n"),
    ("class:dim",       "  /w [file]     save as .hwo\n"),
    ("class:dim",       "  /h            toggle this help\n"),
    ("class:dim",       "  /q            quit\n\n"),
    ("class:header",    "  Input syntax\n\n"),
    ("class:dim",       "  #name#             top-level agent\n"),
    ("class:dim",       "  -> #name#          sub-agent\n"),
    ("class:dim",       "  -> text            add task\n"),
    ("class:dim",       "  //                 parallel separator\n"),
    ("class:dim",       "  #A#->task          append task to A\n"),
    ("class:dim",       "  #A#->#B#           append sub-agent B to A\n"),
    ("class:dim",       "  #A#/#B#->...       path-based append\n"),
    ("class:dim",       "  #A#[N]->text       insert at N\n"),
    ("class:dim",       "  #A#[N]-x>          delete N\n"),
    ("class:dim",       "  #A#[N]-x>text      replace N\n\n"),
    (f"class:dim",       f"  {symbols.ARROW_U}{symbols.ARROW_D} PgUp PgDn Home End — navigate\n"),
]


def _render_all(session: HwoSession, tick: int, executing: bool,
                show_help: bool) -> list:
    if show_help:
        return list(_HELP_LINES)

    out: list = []
    out.append((f"class:header",      f"  HWO  {symbols.BULLET}  Agent: "))
    out.append(("class:header.name", session.root_name))
    if executing:
        out.append(("class:running", "  [running]"))
    out.append(("class:header", "\n\n"))

    if not session.nodes:
        out.append(("class:dim", "  #agent#  -> task  -> #sub#  //\n"))
        out.append(("class:dim", "  /r=run  /w=save  /h=help  /q=quit\n"))
        return out

    in_parallel = False
    for node in session.nodes:
        if isinstance(node, HwoSeparator):
            in_parallel = (node.kind == "parallel")
            label = " parallel " if node.kind == "parallel" else " end "
            out.append(("class:separator", f"  {'─' * 6}{label}{'─' * 6}\n\n"))
            continue
        _render_agent(out, node, "    " if in_parallel else "  ", tick)
    return out


# ── Line splitter & scrollbar ─────────────────────────────────────────────

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

def _render_status(error_msg: str, notice_msg: str, executing: bool,
                   mode: StudioMode, session: HwoSession,
                   run_result: Optional[dict],
                   total_lines: int, visible_lines: int, scroll_top: int) -> list:
    scroll_info = ""
    if total_lines > visible_lines:
        pct = int(scroll_top / max(1, total_lines - visible_lines) * 100)
        scroll_info = f"  {symbols.ARROW_U}{symbols.ARROW_D}/{total_lines}L {pct}%"

    if error_msg:
        return [("class:task.err", f"  {symbols.FAIL} {error_msg}{scroll_info}")]

    if notice_msg:
        return [("class:task.done", f"  {notice_msg}{scroll_info}")]

    if executing:
        tasks = session.all_tasks()
        done   = sum(1 for t in tasks if t.status == "done")
        total  = len(tasks)
        errors = sum(1 for t in tasks if t.status == "error")
        msg    = f"{done}/{total} done"
        if errors:
            msg += f"  {errors} failed"
        return [("class:running", f"  running … {msg}{scroll_info}  │  Esc=cancel options")]

    if run_result is not None:
        ok  = run_result.get("ok", False)
        cls = "class:task.done" if ok else "class:task.err"
        lbl = f"{_DONE} HWO done" if ok else f"{_ERROR} HWO failed"
        return [(cls, f"  {lbl}{scroll_info}  │  r=run again  s=save  Esc=back")]

    if mode == StudioMode.HELP:
        base = "  HELP  │  ↑↓ scroll  Esc close"
    elif mode == StudioMode.INSPECTOR:
        base = "  INSPECTOR  │  Esc back"
    else:
        base = "  Ready"
    return [("class:help", base + scroll_info)]


# ── Style ─────────────────────────────────────────────────────────────────

_STYLE = Style.from_dict({
    "root":            "bg:#0d1117 #e6edf3",
    "header":          "bold #4ade80",
    "header.name":     "bold #a78bfa",
    "running":         "bold #e3b341",
    "agent.spin":      "#e3b341",
    "agent.name":      "bold #a78bfa",
    "agent":           "#c9d1d9",
    "task.run":        "#e3b341",
    "task.done":       "#4ade80",
    "task.err":        "bold #f85149",
    "task.pend":       "#8b949e",
    "task.text":       "#e6edf3",
    "separator":       "#6b7d6b",
    "dim":             "#6b7d6b",
    "help":            "#6b7d6b italic",
    "input.prefix":    "bold #4ade80",
    "input.label":     "bold #f0f6fc",
    "input.field":     "bg:#161b22 #f0f6fc",
    "command":         "bold #c084fc",
    "danger":          "bold #ff6b63",
    "danger.selected": "bold bg:#5a1f23 #ffffff",
    "button":          "#8b949e",
    "button.selected": "bold bg:#1f6f3d #ffffff",
    "scrollbar.thumb": "bold #3fb950",
    "scrollbar.rail":  "#233323",
    "selected":        "bg:#21262d #f0f6fc",
    "selected.marker": "bold #4ade80",
    "pane.title":      "bold #8b949e",
    "pane.title.focus":"bold #4ade80",
    "inspector.label": "#8b949e",
    "inspector.value": "#e6edf3",
    "violet":          "bold #a78bfa",
    "warning":         "bold #e3b341",
})


# ── TUI entry point ───────────────────────────────────────────────────────

def run_hwo_ui(root_agent_name: str,
               deps=None,
               session_data: Optional[dict] = None,
               parent_id: Optional[str] = None,
               initial_session: Optional[HwoSession] = None,
               input=None, output=None) -> None:
    import re

    session    = initial_session if initial_session is not None \
                 else HwoSession(root_name=root_agent_name)
    tick       = [0]
    executing  = [False]
    error_msg  = [""]
    notice_msg = [""]
    scroll_top = [0]
    studio     = HwoStudioState()
    run_result: list = [None]
    stop_evt   = threading.Event()
    _totals    = {"total": 0, "visible": 0}
    _app_ref: list = [None]
    runner_thread: list[Optional[threading.Thread]] = [None]
    collapsed: set[int] = set()
    selected_index = [0]
    revision = [0]
    saved_revision = [0]
    undo_snapshot: list[Optional[tuple[HwoSession, int]]] = [None]
    outline_cache = {"revision": -1, "collapsed": (), "rows": []}
    validation_cache = {"revision": -1, "errors": []}

    # ── helpers ───────────────────────────────────────────────────────────

    def _visible_height() -> int:
        try:
            rows = shutil.get_terminal_size((80, 24)).lines
        except Exception:
            rows = 24
        chrome = (8 if studio.mode in {StudioMode.CONFIRM_DELETE,
                                       StudioMode.CONFIRM_CANCEL} else 7)
        return max(4, rows - chrome)

    def _is_wide() -> bool:
        return shutil.get_terminal_size((80, 24)).columns >= 120

    def _invalidate() -> None:
        app = _app_ref[0]
        if app is not None:
            try:
                app.invalidate()
            except Exception:
                pass

    def _touch(*, clear_result: bool = True,
               clear_undo: bool = True) -> None:
        revision[0] += 1
        if clear_result:
            run_result[0] = None
        if clear_undo:
            undo_snapshot[0] = None
        _invalidate()

    def _rows() -> list[HwoOutlineRow]:
        key = (revision[0], tuple(sorted(collapsed)))
        if (outline_cache["revision"], outline_cache["collapsed"]) != key:
            outline_cache["revision"], outline_cache["collapsed"] = key
            outline_cache["rows"] = _outline_rows(session, collapsed)
        rows = outline_cache["rows"]
        selected_index[0] = max(0, min(
            selected_index[0], max(0, len(rows) - 1)))
        return rows

    def _selected_row() -> Optional[HwoOutlineRow]:
        rows = _rows()
        return rows[selected_index[0]] if rows else None

    def _selected_agent() -> Optional[HwoAgent]:
        row = _selected_row()
        if row is None:
            return session._last_agent
        if row.kind == "agent":
            return row.node
        return row.parent

    def _remove_selected() -> Optional[str]:
        row = _selected_row()
        if row is None:
            return "Nothing selected"
        undo_snapshot[0] = (copy.deepcopy(session), revision[0])
        if row.kind == "task":
            if row.parent is not None:
                row.parent.tasks.remove(row.node)
            else:
                session.nodes.remove(row.node)
        elif row.kind == "agent":
            container = row.parent.children if row.parent else session.nodes
            container.remove(row.node)
            if session._last_agent is row.node:
                session._last_agent = row.parent
        elif row.kind == "separator":
            index = session.nodes.index(row.node)
            pair_index = None
            if row.node.kind == "parallel":
                pair_index = next((i for i in range(index + 1, len(session.nodes))
                                   if isinstance(session.nodes[i], HwoSeparator)
                                   and session.nodes[i].kind == "end"), None)
            else:
                pair_index = next((i for i in range(index - 1, -1, -1)
                                   if isinstance(session.nodes[i], HwoSeparator)
                                   and session.nodes[i].kind == "parallel"), None)
            for target in sorted(
                    {index} | ({pair_index} if pair_index is not None else set()),
                    reverse=True):
                del session.nodes[target]
            session._parallel_open = any(
                isinstance(node, HwoSeparator) and node.kind == "parallel"
                for node in session.nodes)
        collapsed.discard(row.key)
        _touch(clear_undo=False)
        return None

    def _undo_delete() -> Optional[str]:
        record = undo_snapshot[0]
        if record is None:
            return "Nothing to undo"
        snapshot, previous_revision = record
        session.nodes = snapshot.nodes
        session.workflow_io = snapshot.workflow_io
        session._parallel_open = snapshot._parallel_open
        session._last_agent = snapshot._last_agent
        undo_snapshot[0] = None
        collapsed.clear()
        revision[0] = previous_revision
        outline_cache["revision"] = -1
        validation_cache["revision"] = -1
        run_result[0] = None
        _invalidate()
        return None

    def _focus_tree() -> None:
        app = _app_ref[0]
        if app is not None:
            app.layout.focus(tree_win)

    def _set_navigation(*, clear_messages: bool = False) -> None:
        studio.mode = (StudioMode.RUNNING if executing[0]
                       else StudioMode.RESULT if run_result[0] is not None
                       else StudioMode.NAVIGATION)
        studio.return_mode = studio.mode
        _focus_tree()
        studio.target = None
        studio.destructive_selected = False
        input_buf.reset()
        if clear_messages:
            error_msg[0] = ""
            notice_msg[0] = ""
        _invalidate()

    def _begin_prompt(prompt_mode: StudioMode,
                      target: Optional[HwoOutlineRow] = None) -> None:
        if executing[0]:
            error_msg[0] = "Running snapshot is locked; wait or cancel before editing"
            return
        studio.return_mode = studio.mode
        studio.mode = prompt_mode
        error_msg[0] = ""
        notice_msg[0] = ""
        studio.target = target
        input_buf.reset()
        _app_ref[0].layout.focus(input_win)
        _invalidate()

    def _save_file(path: str) -> Optional[str]:
        try:
            Path(path).write_text(_session_to_hwo(session), encoding="utf-8")
            saved_revision[0] = revision[0]
            return None
        except OSError as e:
            return str(e)

    def _do_run(save_path: Optional[str] = None) -> None:
        app = _app_ref[0]
        if not session.nodes:
            error_msg[0] = "Nothing to run"
            return
        validation_errors = _validate_session(session)
        if validation_errors:
            error_msg[0] = "Validation failed: " + validation_errors[0]
            return
        for task in session.all_tasks():
            task.status = "pending"
            task.step_id = ""
            task.agent_id = ""
            task.last_event = ""
            task.started_at = None
            task.completed_at = None
        if save_path:
            err = _save_file(save_path)
            if err:
                error_msg[0] = err
                return
            hwo_path = save_path
            cleanup  = False
        else:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".hwo", mode="w", delete=False, encoding="utf-8"
            )
            tmp.write(_session_to_hwo(session))
            tmp.close()
            hwo_path = tmp.name
            cleanup  = True

        executing[0]  = True
        studio.mode = StudioMode.RUNNING
        _focus_tree()
        run_result[0] = None
        error_msg[0]  = ""
        notice_msg[0] = ""
        tasks_by_step = {}
        try:
            tasks_by_step = _bind_runtime_step_ids(session, hwo_path)
        except Exception as exc:
            executing[0] = False
            error_msg[0] = f"Cannot map HWO runtime steps: {exc}"
            if cleanup:
                try:
                    os.unlink(hwo_path)
                except OSError:
                    pass
            return
        try:
            app.invalidate()
        except Exception:
            pass

        def _exec() -> None:
            try:
                import hwo_runner

                def _on_hwo_events(events):
                    rows = events if isinstance(events, list) else [events]
                    _apply_runtime_events(tasks_by_step, rows)
                    _invalidate()
                    for event in rows:
                        if not isinstance(event, dict):
                            continue
                        try:
                            import agent_ui_events
                            agent_ui_events.hub.ingest(
                                parent_id or "", [event])
                        except Exception:
                            pass
                    try:
                        app.invalidate()
                    except Exception:
                        pass

                r = hwo_runner.run_hwo_file(
                    path=hwo_path,
                    deps=deps,
                    session=session_data or {},
                    parent_id=parent_id,
                    abort_event=stop_evt,
                    events_cb=_on_hwo_events,
                )
                run_result[0] = r
            except Exception as e:
                run_result[0] = {"ok": False, "msg": repr(e)}
                for t in session.all_tasks():
                    if t.status == "running":
                        t.status = "error"
            finally:
                executing[0] = False
                if studio.mode == StudioMode.RUNNING:
                    studio.mode = StudioMode.RESULT
                elif studio.mode == StudioMode.CONFIRM_CANCEL:
                    studio.mode = StudioMode.RESULT
                elif (studio.mode in {StudioMode.INSPECTOR, StudioMode.HELP}
                      and studio.return_mode == StudioMode.RUNNING):
                    studio.return_mode = StudioMode.RESULT
                _invalidate()
                if cleanup:
                    try:
                        os.unlink(hwo_path)
                    except OSError:
                        pass
                try:
                    app.invalidate()
                except Exception:
                    pass

        runner_thread[0] = threading.Thread(
            target=_exec, name="hwo-runner", daemon=True)
        runner_thread[0].start()

    # ── slash command handler ─────────────────────────────────────────────

    def _handle_slash(text: str) -> None:
        parts = text.split(None, 1)
        cmd   = parts[0].lower()
        arg   = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/q":
            if executing[0]:
                studio.mode = StudioMode.CONFIRM_CANCEL
                studio.destructive_selected = False
                error_msg[0] = ""
                return
            stop_evt.set()
            try:
                _app_ref[0].exit()
            except Exception:
                pass
            return

        if cmd == "/r":
            if executing[0]:
                error_msg[0] = "Already running"
                return
            _do_run()
            return

        if cmd == "/w":
            if executing[0]:
                error_msg[0] = "Cannot save while running"
                return
            if not session.nodes:
                error_msg[0] = "Nothing to save"
                return
            if arg:
                filename = arg if arg.endswith(".hwo") else arg + ".hwo"
                err = _save_file(filename)
                error_msg[0] = err or ""
                notice_msg[0] = "" if err else f"{_DONE} saved → {filename}"
            else:
                _begin_prompt(StudioMode.SAVE, _selected_row())
            return

        if cmd == "/h":
            studio.mode = (StudioMode.NAVIGATION
                           if studio.mode == StudioMode.HELP
                           else StudioMode.HELP)
            _focus_tree()
            error_msg[0] = ""
            notice_msg[0] = ""
            _invalidate()
            return

        error_msg[0] = f"Unknown command '{cmd}'  (/r /w /h /q)"

    # ── normal input processor ────────────────────────────────────────────

    def _process(text: str) -> None:
        error_msg[0] = ""
        notice_msg[0] = ""
        if not text:
            return
        if executing[0]:
            error_msg[0] = (
                "Running snapshot is locked; wait or use /q to cancel")
            return

        if re.fullmatch(r'/+', text):
            session.toggle_parallel()
            return

        m = re.fullmatch(r'#([^#]+)#\[(\d+)\]->(.*)', text)
        if m:
            name, idx1, task_text = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
            if not task_text:
                error_msg[0] = "Task text cannot be empty"
                return
            err = session.insert_task(name, idx1, task_text)
            if err:
                error_msg[0] = err
            return

        m = re.fullmatch(r'#([^#]+)#\[(\d+)\]-x>(.*)', text)
        if m:
            name, idx1, new_text = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
            err = (session.replace_task(name, idx1, new_text)
                   if new_text else session.delete_task(name, idx1))
            if err:
                error_msg[0] = err
            return

        m = re.fullmatch(r'(#[^#]+#(?:/#[^#]+#)+)->(.*)', text)
        if m:
            path_str, action = m.group(1), m.group(2).strip()
            path   = re.findall(r'#([^#]+)#', path_str)
            target = session.find_agent_by_path(path)
            if target is None:
                error_msg[0] = f"Path '{path_str}' not found"
                return
            child_m = re.fullmatch(r'\s*#([^#]+)#\s*', action)
            if child_m:
                child_name = child_m.group(1).strip()
                for existing in target.children:
                    if existing.name == child_name:
                        error_msg[0] = f"'{child_name}' already exists under #{target.name}#"
                        return
                target.children.append(HwoAgent(name=child_name, parent=target))
            elif action:
                target.tasks.append(HwoTask(text=action))
            else:
                error_msg[0] = "Specify a task or #sub-agent# after ->"
            return

        m = re.fullmatch(r'#([^#]+)#->\s*#([^#]+)#\s*', text)
        if m:
            err = session.append_child_to(m.group(1).strip(), m.group(2).strip())
            if err:
                error_msg[0] = err
            return

        m = re.fullmatch(r'#([^#]+)#->(.+)', text)
        if m:
            err = session.append_task_to(m.group(1).strip(), m.group(2).strip())
            if err:
                error_msg[0] = err
            return

        m = re.fullmatch(r'->\s*#([^#]+)#\s*', text)
        if m:
            err = session.add_child_agent(m.group(1).strip())
            if err:
                error_msg[0] = err
            return

        m = re.fullmatch(r'#([^#]+)#\s*', text)
        if m:
            err = session.add_agent(m.group(1).strip())
            if err:
                error_msg[0] = err
            return

        m2 = re.match(r'^->\s*(.+)$', text, re.DOTALL)
        task_text = m2.group(1).strip() if m2 else text
        if session.add_task(task_text) is None:
            error_msg[0] = "Create an #agent# first"

    # ── content getters ───────────────────────────────────────────────────

    def _validation_errors() -> list[str]:
        if validation_cache["revision"] != revision[0]:
            validation_cache["revision"] = revision[0]
            validation_cache["errors"] = _validate_session(session)
        return validation_cache["errors"]

    def _studio_header():
        tasks = session.all_tasks()
        done = sum(task.status == "done" for task in tasks)
        failed = sum(task.status == "error" for task in tasks)
        stage = "RUN" if executing[0] else ("RESULT" if run_result[0] else "DESIGN")
        dirty = "  ● MODIFIED" if revision[0] != saved_revision[0] else ""
        return [
            ("class:header", "  WORKFLOW STUDIO"),
            ("class:dim", f"  /  {session.root_name}"),
            ("class:violet", f"    {stage}"),
            ("class:dim", f"    {done}/{len(tasks)} done"),
            ("class:task.err" if failed else "class:warning" if dirty else "class:dim",
             f"  {failed} failed" if failed else dirty),
        ]

    def _render_studio_row(row: HwoOutlineRow, selected: bool):
        base = "class:selected" if selected else ""
        marker_style = "class:selected.marker" if selected else "class:dim"
        prefix = "  " + ("› " if selected else "  ") + "  " * row.depth
        fragments = [(marker_style, prefix)]
        if row.kind == "agent":
            agent = row.node
            children = bool(agent.tasks or agent.children)
            disclosure = ("▸" if row.key in collapsed else "▾") if children else "·"
            tasks = agent.all_tasks()
            if any(task.status == "error" for task in tasks):
                status_style, status_mark = "class:task.err", _ERROR
            elif any(task.status == "running" for task in tasks):
                status_style, status_mark = "class:task.run", _spin_char(tick[0], 1)
            elif tasks and all(task.status == "done" for task in tasks):
                status_style, status_mark = "class:task.done", _DONE
            else:
                status_style, status_mark = "class:task.pend", _IDLE
            fragments.extend([
                (marker_style, disclosure + " "),
                (status_style, status_mark + " "),
                ("class:agent.name" if not selected else base,
                 f"#{agent.name}#"),
                ("class:dim" if not selected else base,
                 f"  {len(tasks)} task{'s' if len(tasks) != 1 else ''}"),
            ])
        elif row.kind == "task":
            task = row.node
            mark, status_style = _task_mark(task, tick[0], row.depth)
            fragments.extend([
                (status_style, mark + "  "),
                ("class:task.text" if not selected else base, task.text),
            ])
        else:
            label = "PARALLEL" if row.node.kind == "parallel" else "END PARALLEL"
            fragments.append(("class:separator", f"──── {label} ────"))
        fragments.append((base, "\n"))
        return fragments

    def _get_inspector():
        row = _selected_row()
        if row is None:
            return [("class:muted", "\n  Select a workflow node\n")]
        errors = _validation_errors()
        out = [
            ("class:pane.title", "  INSPECTOR\n"),
            ("class:separator", "  ──────────────────────────\n"),
        ]
        if row.kind == "agent":
            agent = row.node
            out.extend([
                ("class:inspector.label", "  AGENT\n"),
                ("class:violet", f"  #{agent.name}#\n\n"),
                ("class:inspector.label", "  CONTENT\n"),
                ("class:inspector.value", f"  own tasks   {len(agent.tasks)}\n"),
                ("class:inspector.value", f"  children    {len(agent.children)}\n"),
                ("class:inspector.value", f"  all tasks   {len(agent.all_tasks())}\n"),
                ("class:inspector.value", f"  model       {agent.model or 'auto'}\n"),
                ("class:inspector.value", f"  prompt      {agent.prompt_file or 'default'}\n"),
            ])
        elif row.kind == "task":
            task = row.node
            elapsed = "-"
            if task.started_at:
                end = task.completed_at or time.time()
                elapsed = f"{max(0.0, end - task.started_at):.1f}s"
            out.extend([
                ("class:inspector.label", "  TASK\n"),
                ("class:inspector.value", f"  {task.text}\n\n"),
                ("class:inspector.label", "  RUNTIME\n"),
                ("class:inspector.value", f"  status     {task.status}\n"),
                ("class:inspector.value", f"  step       {task.step_id or '-'}\n"),
                ("class:inspector.value", f"  agent      {task.agent_id or '-'}\n"),
                ("class:inspector.value", f"  elapsed    {elapsed}\n"),
            ])
        else:
            out.extend([
                ("class:inspector.label", "  PARALLEL BOUNDARY\n"),
                ("class:inspector.value", f"  {row.node.kind}\n"),
            ])
        out.append(("class:inspector.label", "\n  VALIDATION\n"))
        if errors:
            out.extend(("class:task.err", "  " + str(error) + "\n")
                       for error in errors[:5])
        else:
            out.append(("class:task.done", f"  {_DONE} Ready to run\n"))
        return out

    def _get_tree_content():
        if studio.mode == StudioMode.HELP:
            all_lines = _to_lines(_HELP_LINES)
            total = len(all_lines)
            visible = _visible_height()
            scroll_top[0] = max(0, min(
                scroll_top[0], max(0, total - visible)))
            _totals.update(total=total, visible=visible)
            result = []
            for line in all_lines[scroll_top[0]:scroll_top[0] + visible]:
                result.extend(line)
                result.append(("", "\n"))
            return result
        rows = _rows()
        total = len(rows)
        visible   = _visible_height()
        if selected_index[0] < scroll_top[0]:
            scroll_top[0] = selected_index[0]
        elif selected_index[0] >= scroll_top[0] + visible:
            scroll_top[0] = selected_index[0] - visible + 1
        scroll_top[0] = max(0, min(scroll_top[0], max(0, total - visible)))
        _totals["total"]   = total
        _totals["visible"] = visible
        result = []
        for index in range(scroll_top[0], min(total, scroll_top[0] + visible)):
            result.extend(_render_studio_row(
                rows[index], index == selected_index[0]))
        return result

    def _get_scrollbar():
        return _render_scrollbar(
            _totals["total"], _totals["visible"], scroll_top[0], _totals["visible"]
        )

    def _get_status():
        return _render_status(
            error_msg[0], notice_msg[0], executing[0], studio.mode, session,
            run_result[0], _totals["total"], _totals["visible"], scroll_top[0],
        )

    def _target_label() -> str:
        row = studio.target or _selected_row()
        if row is None:
            return "WORKFLOW"
        if row.kind == "agent":
            return f"#{row.node.name}#"
        if row.kind == "task":
            return f"#{row.parent.name}#" if row.parent else "WORKFLOW"
        return "PARALLEL BLOCK"

    def _action_title():
        labels = {
            StudioMode.ADD_TASK: ("class:input.prefix", "  ADD TASK"),
            StudioMode.ADD_AGENT: ("class:input.prefix", "  ADD AGENT"),
            StudioMode.EDIT: ("class:input.prefix", "  EDIT"),
            StudioMode.SAVE: ("class:input.prefix", "  SAVE WORKFLOW"),
            StudioMode.COMMAND: ("class:command", "  COMMAND PALETTE"),
        }
        style, label = labels.get(
            studio.mode, ("class:input.prefix", "  ACTION"))
        target = "" if studio.mode in {StudioMode.SAVE, StudioMode.COMMAND} \
                 else f"    {_target_label()}"
        return [(style, label), ("class:dim", target)]

    def _field_prefix():
        labels = {
            StudioMode.ADD_TASK: "  Task          ┃ ",
            StudioMode.ADD_AGENT: "  Agent name    ┃ ",
            StudioMode.EDIT: "  Value         ┃ ",
            StudioMode.SAVE: "  File          ┃ ",
            StudioMode.COMMAND: "  : ",
        }
        style = ("class:command" if studio.mode == StudioMode.COMMAND
                 else "class:input.label")
        return [(style, labels.get(studio.mode, "  ┃ "))]

    def _field_prefix_width() -> int:
        labels = {
            StudioMode.ADD_TASK: 18,
            StudioMode.ADD_AGENT: 18,
            StudioMode.EDIT: 18,
            StudioMode.SAVE: 18,
            StudioMode.COMMAND: 4,
        }
        return labels.get(studio.mode, 4)

    def _action_hint():
        if error_msg[0]:
            return [("class:task.err", f"  {_ERROR} {error_msg[0]}  ·  edit and retry  ·  Esc cancel")]
        if studio.mode == StudioMode.COMMAND:
            return [("class:dim", "  HWO DSL or /r /w /h /q  ·  Enter apply  ·  Esc close")]
        return [("class:dim", "  Enter confirm  ·  Esc cancel")]

    def _navigation_actions():
        if studio.mode == StudioMode.HELP:
            return [
                ("class:violet", "  HELP"),
                ("class:dim", "\n  ↑↓ Scroll   PgUp/PgDn   Esc Close"),
            ]
        if studio.mode == StudioMode.INSPECTOR:
            return [
                ("class:violet", "  INSPECTOR"),
                ("class:dim", "\n  Runtime and validation detail   Esc Back"),
            ]
        if studio.mode == StudioMode.RUNNING:
            return [
                ("class:running", "  RUNNING"),
                ("class:dim", "\n  ↑↓ Select   i Inspect   Esc Cancel options"),
            ]
        if studio.mode == StudioMode.RESULT:
            return [
                ("class:task.done" if run_result[0] and run_result[0].get("ok")
                 else "class:task.err", "  RESULT"),
                ("class:dim", "\n  ↑↓ Select   i Inspect   r Run again   s Save   Esc Back"),
            ]
        return [
            ("class:pane.title.focus", "  NAVIGATION"),
            ("class:dim", "\n  ↑↓ Select   Enter Expand   a Task   A Agent   e Edit   d Delete"),
            ("class:dim", "\n  r Run   s Save   u Undo   : Command   ? Help   Esc Exit"),
        ]

    def _confirmation_content():
        deleting = studio.mode == StudioMode.CONFIRM_DELETE
        row = studio.target
        if deleting:
            if row is None:
                subject = "selected item"
            elif row.kind == "agent":
                subject = f"Agent #{row.node.name}# and {len(row.node.all_tasks())} task(s)"
            elif row.kind == "task":
                subject = f"Task: {row.node.text}"
            else:
                subject = "Parallel boundary pair"
            title = "  DELETE"
            message = f"  {subject}"
            destructive = "Delete"
        else:
            title = "  CANCEL RUN"
            message = "  Stop the running workflow and close Studio?"
            destructive = "Stop & close"
        cancel_style = ("class:button" if studio.destructive_selected
                        else "class:button.selected")
        danger_style = ("class:danger.selected" if studio.destructive_selected
                        else "class:danger")
        return [
            ("class:danger", title + "\n"),
            ("class:inspector.value", message + "\n"),
            (cancel_style, "  [ Cancel ]"),
            ("", "        "),
            (danger_style, f"[ {destructive} ]\n"),
            ("class:dim", "  ←→ Select  ·  Enter confirm  ·  Esc cancel"),
        ]

    # ── layout ────────────────────────────────────────────────────────────
    tree_win   = Window(content=FormattedTextControl(_get_tree_content, focusable=True),
                        dont_extend_height=False, style="class:root")
    scroll_win = Window(content=FormattedTextControl(_get_scrollbar, focusable=False),
                        width=2, dont_extend_width=True)
    sep_win    = Window(
        content=FormattedTextControl(lambda: [("class:scrollbar.rail", "─" * 80)]),
        height=1,
    )
    status_win = Window(content=FormattedTextControl(_get_status, focusable=False),
                        height=1)
    input_buf  = Buffer(name="hwo_input", multiline=False)
    field_prefix_win = Window(
        content=FormattedTextControl(_field_prefix, focusable=False),
        width=_field_prefix_width, dont_extend_width=True)
    input_win  = Window(content=BufferControl(buffer=input_buf),
                        height=1, dont_extend_height=True,
                        style="class:input.field")
    form_area = HSplit([
        Window(content=FormattedTextControl(_action_title), height=1),
        VSplit([field_prefix_win, input_win]),
        Window(content=FormattedTextControl(_action_hint), height=1),
    ])
    navigation_area = Window(
        content=FormattedTextControl(_navigation_actions), height=3)
    confirmation_area = Window(
        content=FormattedTextControl(_confirmation_content), height=4)

    input_modes = {
        StudioMode.ADD_TASK, StudioMode.ADD_AGENT, StudioMode.EDIT,
        StudioMode.SAVE, StudioMode.COMMAND,
    }

    def _active_action_area():
        if studio.mode in input_modes:
            return form_area
        if studio.mode in {StudioMode.CONFIRM_DELETE,
                           StudioMode.CONFIRM_CANCEL}:
            return confirmation_area
        return navigation_area
    outline_area = VSplit([tree_win, scroll_win])
    wide_inspector_panel = Window(
        content=FormattedTextControl(_get_inspector, focusable=True),
        width=34, wrap_lines=True, style="class:root")
    narrow_inspector_panel = Window(
        content=FormattedTextControl(_get_inspector, focusable=True),
        wrap_lines=True, style="class:root")
    wide_body = VSplit([
        outline_area,
        Window(width=1, char="│", style="class:separator"),
        wide_inspector_panel,
    ])
    narrow_body = HSplit([
        ConditionalContainer(
            outline_area,
            filter=Condition(lambda: studio.mode != StudioMode.INSPECTOR)),
        ConditionalContainer(
            narrow_inspector_panel,
            filter=Condition(lambda: studio.mode == StudioMode.INSPECTOR)),
    ])
    responsive_body = DynamicContainer(
        lambda: wide_body if _is_wide() else narrow_body)

    def _pane_heading():
        if studio.mode == StudioMode.HELP:
            return [("class:violet", "  HELP"),
                    ("class:dim", "    Workflow Studio reference  ·  Esc back")]
        if studio.mode == StudioMode.INSPECTOR:
            return [("class:violet", "  INSPECTOR"),
                    ("class:dim", "    Selected node detail  ·  Esc back")]
        return [("class:pane.title.focus", "  OUTLINE"),
                ("class:dim", "    Direct actions  ·  i inspect  ·  ? help")]

    pane_title = Window(
        content=FormattedTextControl(_pane_heading), height=1)

    layout = Layout(
        HSplit([
            Window(content=FormattedTextControl(_studio_header), height=1,
                   style="class:root"),
            pane_title,
            responsive_body,
            sep_win,
            status_win,
            DynamicContainer(_active_action_area),
        ]),
        focused_element=tree_win,
    )

    # ── key bindings ──────────────────────────────────────────────────────
    kb = KeyBindings()

    design_filter = Condition(lambda: studio.mode in {
        StudioMode.NAVIGATION, StudioMode.RESULT,
    })
    navigation_filter = Condition(lambda: studio.mode in {
        StudioMode.NAVIGATION, StudioMode.RESULT, StudioMode.RUNNING,
    })
    editable_filter = Condition(lambda: studio.mode in input_modes)
    confirmation_filter = Condition(lambda: studio.mode in {
        StudioMode.CONFIRM_DELETE, StudioMode.CONFIRM_CANCEL,
    })

    def _finish_form() -> None:
        # Focus a container that remains in the next layout before hiding input.
        _set_navigation()

    @kb.add("enter")
    def _(event):
        if studio.mode in {StudioMode.NAVIGATION, StudioMode.RESULT,
                           StudioMode.RUNNING}:
            row = _selected_row()
            if row is not None and row.kind == "agent":
                if row.key in collapsed:
                    collapsed.remove(row.key)
                else:
                    collapsed.add(row.key)
                _invalidate()
            return

        if studio.mode in {StudioMode.CONFIRM_DELETE,
                           StudioMode.CONFIRM_CANCEL}:
            if studio.destructive_selected:
                if studio.mode == StudioMode.CONFIRM_DELETE:
                    error_msg[0] = _remove_selected() or ""
                    notice_msg[0] = ("" if error_msg[0]
                                     else f"{_DONE} Deleted  ·  u to undo")
                    _set_navigation()
                    return
                stop_evt.set()
                event.app.exit()
            else:
                notice_msg[0] = ("Delete cancelled"
                                 if studio.mode == StudioMode.CONFIRM_DELETE
                                 else "Workflow continues")
                _set_navigation()
            return

        if studio.mode not in input_modes:
            return

        raw = input_buf.text.strip()

        if studio.mode == StudioMode.SAVE:
            if not raw:
                error_msg[0] = "File path cannot be empty"
                return
            filename = raw if raw.endswith(".hwo") else raw + ".hwo"
            err = _save_file(filename)
            error_msg[0] = err or ""
            notice_msg[0] = "" if err else f"{_DONE} saved → {filename}"
            if not err:
                _finish_form()
            return

        if studio.mode in {StudioMode.ADD_TASK, StudioMode.ADD_AGENT,
                           StudioMode.EDIT}:
            error_msg[0] = ""
            notice_msg[0] = ""
            target = studio.target
            if not raw:
                error_msg[0] = "Value cannot be empty"
            elif studio.mode == StudioMode.ADD_TASK:
                agent = (target.node if target and target.kind == "agent"
                         else target.parent if target and target.kind == "task"
                         else session._last_agent)
                if agent is None:
                    error_msg[0] = "Select or create an Agent first"
                else:
                    agent.tasks.append(HwoTask(raw))
                    session._last_agent = agent
                    notice_msg[0] = f"{_DONE} Task added"
                    _touch()
            elif studio.mode == StudioMode.ADD_AGENT:
                parent = (target.node if target and target.kind == "agent"
                          else target.parent if target else None)
                siblings = parent.children if parent else session._top_agents()
                if any(agent.name == raw for agent in siblings):
                    error_msg[0] = f"Agent '{raw}' already exists here"
                else:
                    agent = HwoAgent(raw, parent=parent)
                    if parent:
                        parent.children.append(agent)
                        collapsed.discard(id(parent))
                    else:
                        session.nodes.append(agent)
                    session._last_agent = agent
                    notice_msg[0] = f"{_DONE} Agent added"
                    _touch()
            elif target is None:
                error_msg[0] = "Nothing selected"
            elif target.kind == "task":
                target.node.text = raw
                notice_msg[0] = f"{_DONE} Task updated"
                _touch()
            elif target.kind == "agent":
                siblings = (target.parent.children if target.parent
                            else session._top_agents())
                if any(agent is not target.node and agent.name == raw
                       for agent in siblings):
                    error_msg[0] = f"Agent '{raw}' already exists here"
                else:
                    target.node.name = raw
                    notice_msg[0] = f"{_DONE} Agent renamed"
                    _touch()
            else:
                error_msg[0] = "Parallel boundaries cannot be renamed"
            if not error_msg[0]:
                _finish_form()
            return

        if studio.mode != StudioMode.COMMAND:
            return
        if not raw:
            error_msg[0] = "Enter HWO DSL or a Slash command"
            return
        before = _session_to_hwo(session)
        if raw.startswith("/") and not re.fullmatch(r'/+', raw):
            _handle_slash(raw)
        else:
            _process(raw)
        changed = _session_to_hwo(session) != before
        if changed:
            _touch()
        if not error_msg[0] and studio.mode == StudioMode.COMMAND:
            notice_msg[0] = (f"{_DONE} Command applied" if changed
                             else notice_msg[0])
            _finish_form()

    @kb.add("a", filter=design_filter)
    def _(event):
        _begin_prompt(StudioMode.ADD_TASK, _selected_row())

    @kb.add("A", filter=design_filter)
    def _(event):
        _begin_prompt(StudioMode.ADD_AGENT, _selected_row())

    @kb.add("e", filter=design_filter)
    def _(event):
        row = _selected_row()
        if row is None or row.kind == "separator":
            error_msg[0] = "Select an Agent or task to edit"
            return
        _begin_prompt(StudioMode.EDIT, row)
        input_buf.text = row.node.name if row.kind == "agent" else row.node.text
        input_buf.cursor_position = len(input_buf.text)

    @kb.add("d", filter=design_filter)
    def _(event):
        if executing[0]:
            error_msg[0] = "Running snapshot is locked"
            return
        if _selected_row() is None:
            error_msg[0] = "Nothing selected"
            return
        studio.target = _selected_row()
        studio.destructive_selected = False
        studio.mode = StudioMode.CONFIRM_DELETE
        error_msg[0] = ""
        notice_msg[0] = ""
        _invalidate()

    @kb.add("space", filter=navigation_filter)
    def _(event):
        row = _selected_row()
        if row is not None and row.kind == "agent":
            if row.key in collapsed:
                collapsed.remove(row.key)
            else:
                collapsed.add(row.key)
            _invalidate()

    @kb.add("r", filter=navigation_filter)
    def _(event):
        if executing[0]:
            error_msg[0] = "Already running"
        else:
            _do_run()

    @kb.add("s", filter=design_filter)
    def _(event):
        _begin_prompt(StudioMode.SAVE, _selected_row())

    @kb.add("u", filter=design_filter)
    def _(event):
        error_msg[0] = _undo_delete() or ""
        notice_msg[0] = "" if error_msg[0] else f"{_DONE} Delete undone"
        if not error_msg[0]:
            _set_navigation()

    @kb.add("?", filter=navigation_filter)
    def _(event):
        studio.return_mode = studio.mode
        studio.mode = StudioMode.HELP
        error_msg[0] = ""
        notice_msg[0] = ""
        scroll_top[0] = 0
        _invalidate()

    @kb.add("i", filter=navigation_filter)
    @kb.add("tab", filter=navigation_filter)
    def _(event):
        studio.return_mode = studio.mode
        studio.mode = StudioMode.INSPECTOR
        error_msg[0] = ""
        notice_msg[0] = ""
        event.app.layout.focus(
            wide_inspector_panel if _is_wide() else narrow_inspector_panel)
        _invalidate()

    @kb.add("tab", filter=Condition(lambda: studio.mode == StudioMode.INSPECTOR))
    def _(event):
        studio.mode = studio.return_mode
        _focus_tree()
        _invalidate()

    @kb.add("tab", filter=editable_filter)
    def _(event):
        # Every contextual form has one field; keep Tab from inserting an
        # invisible control character or escaping into a hidden container.
        return

    def _begin_command(prefix: str = "") -> None:
        _begin_prompt(StudioMode.COMMAND, _selected_row())
        input_buf.text = prefix
        input_buf.cursor_position = len(prefix)

    @kb.add(":", filter=design_filter)
    def _(event):
        _begin_command()

    @kb.add("/", filter=design_filter)
    def _(event):
        _begin_command("/")

    @kb.add("#", filter=design_filter)
    def _(event):
        _begin_command("#")

    @kb.add("-", filter=design_filter)
    def _(event):
        _begin_command("-")

    @kb.add("left", filter=confirmation_filter)
    @kb.add("right", filter=confirmation_filter)
    @kb.add("tab", filter=confirmation_filter)
    def _(event):
        studio.destructive_selected = not studio.destructive_selected
        _invalidate()

    @kb.add("y", filter=confirmation_filter)
    def _(event):
        studio.destructive_selected = True
        if studio.mode == StudioMode.CONFIRM_DELETE:
            error_msg[0] = _remove_selected() or ""
            notice_msg[0] = ("" if error_msg[0]
                             else f"{_DONE} Deleted  ·  u to undo")
            _set_navigation()
        else:
            stop_evt.set()
            event.app.exit()

    @kb.add("n", filter=confirmation_filter)
    def _(event):
        notice_msg[0] = "Cancelled"
        _set_navigation()

    @kb.add("escape")
    def _(event):
        if studio.mode in input_modes:
            notice_msg[0] = "Action cancelled"
            error_msg[0] = ""
            _set_navigation()
        elif studio.mode in {StudioMode.CONFIRM_DELETE,
                             StudioMode.CONFIRM_CANCEL}:
            notice_msg[0] = "Cancelled"
            _set_navigation()
        elif studio.mode in {StudioMode.HELP, StudioMode.INSPECTOR}:
            studio.mode = studio.return_mode
            _focus_tree()
            scroll_top[0] = 0
            _invalidate()
        elif studio.mode == StudioMode.RUNNING or executing[0]:
            studio.mode = StudioMode.CONFIRM_CANCEL
            studio.destructive_selected = False
            _invalidate()
        elif studio.mode == StudioMode.RESULT:
            studio.mode = StudioMode.NAVIGATION
            run_result[0] = None
            _invalidate()
        else:
            stop_evt.set()
            event.app.exit()

    @kb.add("up")
    def _(event):
        if studio.mode in {StudioMode.NAVIGATION, StudioMode.RESULT,
                           StudioMode.RUNNING}:
            selected_index[0] = max(0, selected_index[0] - 1)
        elif studio.mode == StudioMode.HELP:
            scroll_top[0] = max(0, scroll_top[0] - 1)

    @kb.add("down")
    def _(event):
        if studio.mode in {StudioMode.NAVIGATION, StudioMode.RESULT,
                           StudioMode.RUNNING}:
            selected_index[0] = min(
                max(0, len(_rows()) - 1), selected_index[0] + 1)
        elif studio.mode == StudioMode.HELP:
            scroll_top[0] += 1

    @kb.add("k", filter=navigation_filter)
    def _(event):
        selected_index[0] = max(0, selected_index[0] - 1)

    @kb.add("j", filter=navigation_filter)
    def _(event):
        selected_index[0] = min(
            max(0, len(_rows()) - 1), selected_index[0] + 1)

    @kb.add("pageup")
    def _(event):
        amount = max(1, _visible_height() // 2)
        if studio.mode in {StudioMode.NAVIGATION, StudioMode.RESULT,
                           StudioMode.RUNNING}:
            selected_index[0] = max(0, selected_index[0] - amount)
        else:
            scroll_top[0] = max(0, scroll_top[0] - amount)

    @kb.add("pagedown")
    def _(event):
        amount = max(1, _visible_height() // 2)
        if studio.mode in {StudioMode.NAVIGATION, StudioMode.RESULT,
                           StudioMode.RUNNING}:
            selected_index[0] = min(
                max(0, len(_rows()) - 1), selected_index[0] + amount)
        else:
            scroll_top[0] += amount

    @kb.add("home")
    def _(event):
        if studio.mode in {StudioMode.NAVIGATION, StudioMode.RESULT,
                           StudioMode.RUNNING}:
            selected_index[0] = 0
        scroll_top[0] = 0

    @kb.add("end")
    def _(event):
        if studio.mode in {StudioMode.NAVIGATION, StudioMode.RESULT,
                           StudioMode.RUNNING}:
            selected_index[0] = max(0, len(_rows()) - 1)
        scroll_top[0] = max(0, _totals["total"] - _totals["visible"])

    @kb.add("c-c")
    @kb.add("c-d")
    def _(event):
        stop_evt.set()
        event.app.exit()

    # ── tick thread ───────────────────────────────────────────────────────
    def _ticker(app):
        while not stop_evt.is_set():
            if stop_evt.wait(0.2 if executing[0] else 0.8):
                break
            if executing[0]:
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
        refresh_interval=None,
        min_redraw_interval=0.05,
        mouse_support=False,
        input=input,
        output=output,
    )
    # Escape should close the current layer promptly while still leaving
    # enough time for terminal arrow-key escape sequences to arrive intact.
    app.ttimeoutlen = 0.2
    app.timeoutlen = 0.2
    _app_ref[0] = app

    stop_evt.clear()
    ticker_thread = threading.Thread(
        target=_ticker, args=(app,), name="hwo-ui-ticker", daemon=True)
    ticker_thread.start()

    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        # Esc/Ctrl+C bindings handle the steady state.  Treat interrupts that
        # race application startup or teardown as the same ordinary cancel.
        return
    finally:
        stop_evt.set()
        ticker_thread.join(timeout=1)
        active_runner = runner_thread[0]
        if active_runner is not None and active_runner.is_alive():
            active_runner.join(timeout=2)
        # An interrupt inside asyncio's teardown can leave this thread's
        # running-loop flag set, which breaks every later prompt_toolkit
        # dialog (approval gates included) with a RuntimeError.
        try:
            import laintas_cli
            laintas_cli._clear_stale_running_loop()
        except Exception:
            pass
