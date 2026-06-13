"""HWO — /hwo command: visual agent-orchestration builder TUI.

Input grammar (typed at the bottom prompt):
  #name#                     — create top-level agent
  -> #name#                  — create sub-agent under current agent (indented)
  -> text  /  text           — add task to current agent
  //                         — toggle ─── parallel ─── / ─── end ─── separator
  #Name#->#Sub#              — append sub-agent to named agent
  #Name#->task               — append task to named agent
  #a#/#b#->...               — path-based: target b under a, then append
  #Name#[N]->text            — insert task at position N (1-based)
  #Name#[N]-x>               — delete task at position N
  #Name#[N]-x>text           — replace task at position N

Keys:
  r          — serialize to temp file and execute via hwo_runner
  w          — prompt for filename, save as .hwo
  wr         — save as .hwo then execute
  ↑ ↓ PgUp PgDn Home End — scroll the tree view
  Esc        — cancel save prompt  /  exit TUI
  q Ctrl-C   — exit

Task icons:
  □  pending   ◰◳◲◱ running   ✓ done   ✗ error
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    nodes: list = field(default_factory=list)
    _parallel_open: bool = False
    _last_agent: Optional[HwoAgent] = field(default=None, repr=False)

    def _top_agents(self) -> list:
        return [n for n in self.nodes if isinstance(n, HwoAgent)]

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

    def find_agent_by_path(self, path: list) -> Optional[HwoAgent]:
        if not path:
            return None
        current = next((n for n in self.nodes
                        if isinstance(n, HwoAgent) and n.name == path[0]), None)
        if current is None:
            return None
        for name in path[1:]:
            current = next((c for c in current.children if c.name == name), None)
            if current is None:
                return None
        return current

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
        return result

    def has_pending(self) -> bool:
        return any(t.status == "pending" for t in self.all_tasks())


# ── HWO serializer ────────────────────────────────────────────────────────

def _session_to_hwo(session: HwoSession) -> str:
    lines: list = []

    def _emit_agent(agent: HwoAgent, depth: int = 0) -> None:
        pad = "  " * depth
        lines.append(f"{pad}#{agent.name}# {{")
        for task in agent.tasks:
            lines.append(f"{pad}  -> {task.text}")
        for child in agent.children:
            _emit_agent(child, depth + 1)
        lines.append(f"{pad}}}")

    in_parallel = False
    for node in session.nodes:
        if isinstance(node, HwoSeparator):
            if node.kind == "parallel":
                lines.append("// parallel //")
                in_parallel = True
            else:
                lines.append("// end //")
                lines.append("")
                in_parallel = False
        elif isinstance(node, HwoAgent):
            _emit_agent(node, depth=1 if in_parallel else 0)

    return "\n".join(lines) + "\n"


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
            out.append((style,             f"{indent}    {mark} → "))
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
        out.append(("class:dim", "  #agent#  ->task  ->#sub#  //\n"))
        out.append(("class:dim", "  r=run  w=save  wr=save+run  ↑↓  q=quit\n"))
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

def _render_status(error_msg: str, executing: bool, mode: str,
                   session: HwoSession, run_result: Optional[dict],
                   total_lines: int, visible_lines: int, scroll_top: int) -> list:
    scroll_info = ""
    if total_lines > visible_lines:
        pct = int(scroll_top / max(1, total_lines - visible_lines) * 100)
        scroll_info = f"  ↑↓/{total_lines}L {pct}%"

    if error_msg:
        return [("class:task.err", f"  ✗ {error_msg}{scroll_info}")]

    if mode in ("save", "save_and_run"):
        label = "save+run" if mode == "save_and_run" else "save"
        return [("class:help", f"  [{label}] enter filename (Esc to cancel){scroll_info}")]

    if executing:
        done   = sum(1 for t in session.all_tasks() if t.status == "done")
        total  = len(session.all_tasks())
        errors = sum(1 for t in session.all_tasks() if t.status == "error")
        msg    = f"{done}/{total} done"
        if errors:
            msg += f"  {errors} failed"
        return [("class:running", f"  running … {msg}{scroll_info}  │  q=quit")]

    if run_result is not None:
        ok  = run_result.get("ok", False)
        ico = _DONE if ok else _ERROR
        cls = "class:task.done" if ok else "class:task.err"
        lbl = "HWO done" if ok else "HWO failed"
        return [(cls, f"  {ico} {lbl}{scroll_info}  │  r=rerun  w=save  q=quit")]

    return [("class:help",
             f"  → task  │  #agent#  │  //  │  r=run  w=save  wr=save+run  │  q=quit"
             + scroll_info)]


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


# ── TUI entry point ───────────────────────────────────────────────────────

def run_hwo_ui(root_agent_name: str,
               deps=None,
               session_data: Optional[dict] = None,
               parent_id: Optional[str] = None) -> None:
    import re

    session    = HwoSession(root_name=root_agent_name)
    tick       = [0]
    executing  = [False]
    error_msg  = [""]
    scroll_top = [0]
    mode       = ["input"]          # "input" | "save" | "save_and_run"
    run_result: list = [None]       # None | {"ok": bool, "msg": str}
    stop_evt   = threading.Event()
    _totals    = {"total": 0, "visible": 0}

    # ── helpers ───────────────────────────────────────────────────────────

    def _visible_height() -> int:
        try:
            rows = shutil.get_terminal_size((80, 24)).lines
        except Exception:
            rows = 24
        return max(4, rows - 6)

    def _save_file(path: str) -> Optional[str]:
        try:
            Path(path).write_text(_session_to_hwo(session), encoding="utf-8")
            return None
        except OSError as e:
            return str(e)

    def _do_run(app, save_path: Optional[str] = None) -> None:
        if not session.nodes:
            error_msg[0] = "Nothing to run"
            return

        # Write to temp or named file
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
        run_result[0] = None
        error_msg[0]  = ""
        for t in session.all_tasks():
            if t.status == "pending":
                t.status = "running"
        try:
            app.invalidate()
        except Exception:
            pass

        def _exec() -> None:
            try:
                import hwo_runner
                r = hwo_runner.run_hwo_file(
                    path=hwo_path,
                    deps=deps,
                    session=session_data or {},
                    parent_id=parent_id,
                    abort_event=stop_evt,
                )
                run_result[0] = r
                final = "done" if r.get("ok") else "error"
                for t in session.all_tasks():
                    if t.status == "running":
                        t.status = final
            except Exception as e:
                run_result[0] = {"ok": False, "msg": repr(e)}
                for t in session.all_tasks():
                    if t.status == "running":
                        t.status = "error"
            finally:
                executing[0] = False
                if cleanup:
                    try:
                        os.unlink(hwo_path)
                    except OSError:
                        pass
                try:
                    app.invalidate()
                except Exception:
                    pass

        threading.Thread(target=_exec, daemon=True).start()

    # ── content getters ───────────────────────────────────────────────────

    def _get_tree_content():
        chunks    = _render_all(session, tick[0], executing[0])
        all_lines = _to_lines(chunks)
        total     = len(all_lines)
        visible   = _visible_height()
        scroll_top[0]      = max(0, min(scroll_top[0], max(0, total - visible)))
        _totals["total"]   = total
        _totals["visible"] = visible
        result = []
        for line in all_lines[scroll_top[0]: scroll_top[0] + visible]:
            result.extend(line)
            result.append(("", "\n"))
        return result

    def _get_scrollbar():
        return _render_scrollbar(
            _totals["total"], _totals["visible"], scroll_top[0], _totals["visible"]
        )

    def _get_prefix():
        if mode[0] == "save":
            return [("class:input.prefix", "  save as: ")]
        if mode[0] == "save_and_run":
            return [("class:input.prefix", "  save+run: ")]
        return [("class:input.prefix", "  > ")]

    def _get_status():
        return _render_status(
            error_msg[0], executing[0], mode[0], session, run_result[0],
            _totals["total"], _totals["visible"], scroll_top[0],
        )

    # ── layout ────────────────────────────────────────────────────────────
    tree_ctrl   = FormattedTextControl(_get_tree_content, focusable=False)
    scroll_ctrl = FormattedTextControl(_get_scrollbar,    focusable=False)
    status_ctrl = FormattedTextControl(_get_status,       focusable=False)
    prefix_ctrl = FormattedTextControl(_get_prefix,       focusable=False)

    tree_win   = Window(content=tree_ctrl,   dont_extend_height=False)
    scroll_win = Window(content=scroll_ctrl, width=2, dont_extend_width=True)
    sep_win    = Window(
        content=FormattedTextControl(lambda: [("class:scrollbar.rail", "─" * 80)]),
        height=1,
    )
    status_win = Window(content=status_ctrl, height=1)
    input_buf  = Buffer(name="hwo_input", multiline=False)
    prefix_win = Window(content=prefix_ctrl, width=12, dont_extend_width=True)
    input_win  = Window(content=BufferControl(buffer=input_buf),
                        height=1, dont_extend_height=True)
    input_row  = VSplit([prefix_win, input_win])
    tree_row   = VSplit([tree_win, scroll_win])

    layout = Layout(
        HSplit([tree_row, sep_win, status_win, input_row]),
        focused_element=input_win,
    )

    # ── input processor (normal mode) ─────────────────────────────────────

    def _process(text: str) -> None:
        error_msg[0] = ""
        if not text:
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
            err = session.replace_task(name, idx1, new_text) if new_text \
                  else session.delete_task(name, idx1)
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
                        error_msg[0] = f"Agent '{child_name}' already exists under #{target.name}#"
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

    # ── key bindings ──────────────────────────────────────────────────────
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        raw  = input_buf.text
        input_buf.reset()
        if mode[0] in ("save", "save_and_run"):
            filename = raw.strip()
            if not filename:
                mode[0] = "input"
                return
            if not filename.endswith(".hwo"):
                filename += ".hwo"
            should_run = (mode[0] == "save_and_run")
            mode[0] = "input"
            err = _save_file(filename)
            if err:
                error_msg[0] = err
                return
            error_msg[0] = f"saved → {filename}"
            if should_run:
                _do_run(event.app, save_path=filename)
        else:
            _process(raw.strip())

    @kb.add("escape")
    def _(event):
        if mode[0] != "input":
            mode[0] = "input"
            input_buf.reset()
            error_msg[0] = ""
        else:
            stop_evt.set()
            event.app.exit()

    # ── scroll ────────────────────────────────────────────────────────────
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

    # ── execute / save ────────────────────────────────────────────────────
    @kb.add("r")
    def _(event):
        if executing[0] or mode[0] != "input":
            return
        if not session.nodes:
            error_msg[0] = "Nothing to run"
            return
        _do_run(event.app)

    @kb.add("w")
    def _(event):
        if executing[0]:
            error_msg[0] = "Cannot save while running"
            return
        if not session.nodes:
            error_msg[0] = "Nothing to save"
            return
        mode[0] = "save"
        input_buf.reset()
        error_msg[0] = ""

    @kb.add("w", "r")
    def _(event):
        if executing[0]:
            error_msg[0] = "Cannot save while running"
            return
        if not session.nodes:
            error_msg[0] = "Nothing to save"
            return
        mode[0] = "save_and_run"
        input_buf.reset()
        error_msg[0] = ""

    @kb.add("q")
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
