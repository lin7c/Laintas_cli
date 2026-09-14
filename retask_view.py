"""Full-screen checklist viewer for ``.retask`` — the HWG viewer's chrome.

A person moving between ``/hwg flow.hwg`` and ``/retask`` should not feel two
products: same palette, same header / body / Inspector / status-bar frame,
same keys where they mean the same thing (↑↓ jk select, g G ends, ? help,
q Esc close). What differs is only what the body is: the graph there, a
markdown-style checklist here, with each task's description folded under its
title and the current task unfolded.

The one write it makes is the person's claim: ``s`` marks the selected task
``submitted``. Deciding ``done`` stays with the checks (``retask.set_status``).
The file is re-read when it changes on disk, so a check the agent runs while
this is open shows up without closing it.

Unlike the graph viewer, mouse capture is OFF by default. A checklist is
something a person copies from — a URL, a key name, a menu path — and a
terminal cannot select text while an application owns the mouse. ``y`` copies
the selected task outright (OSC 52, so it reaches the local clipboard over
SSH too); ``m`` turns clicking back on for anyone who prefers it.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from typing import Optional

import retask
from hwg_view import _STYLE, _MouseControl, _crop

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType

_INSPECTOR_WIDTH = 38

_STATUS_STYLE = {
    retask.TODO: "class:st.pend",
    retask.DOING: "class:st.run",
    retask.SUBMITTED: "class:st.paused",
    retask.DONE: "class:st.done",
    retask.REJECTED: "class:st.fail",
    retask.SKIPPED: "class:dim",
}


def _cells(text: str) -> int:
    from prompt_toolkit.utils import get_cwidth
    return sum(max(0, get_cwidth(ch)) for ch in text)


def _wrap(text: str, width: int) -> list:
    """Wrap one paragraph line to `width` cells (CJK-safe, hard breaks)."""
    from prompt_toolkit.utils import get_cwidth
    width = max(8, width)
    out, line, used = [], [], 0
    for ch in text:
        w = max(0, get_cwidth(ch))
        if used + w > width:
            out.append("".join(line))
            line, used = [], 0
        line.append(ch)
        used += w
    out.append("".join(line))
    return out


class RetaskViewer:
    """One full-screen checklist session."""

    def __init__(self, path: str, *, root: str = "", input=None, output=None):
        self.path = path
        self.root = root
        self.doc: retask.Retask = retask.load(path)
        self._mtime = self._disk_mtime()
        current = retask.current_task(self.doc)
        self.selected: str = current.id if current else self.doc.tasks[0].id
        self.expanded: set = {self.selected}
        self.scroll_top = 0
        self.show_help = False
        self.flash = ""
        self.error = ""
        self.mouse = False              # off: the terminal's own selection works
        self._quit = False
        self._row_task: list = []
        self._kb = KeyBindings()
        self._bind_keys()
        self._app = Application(
            layout=self._build_layout(),
            key_bindings=self._kb,
            style=_STYLE,
            full_screen=True,
            mouse_support=Condition(lambda: self.mouse),
            refresh_interval=1.0,
            input=input,
            output=output,
        )

    # ── file ─────────────────────────────────────────────────────────

    def _disk_mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def _reload_if_changed(self) -> None:
        mtime = self._disk_mtime()
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self.doc = retask.load(self.path)
            self.error = ""
        except retask.RetaskError as exc:
            self.error = str(exc)       # keep showing the last good copy
            return
        if self.doc.task(self.selected) is None:
            current = retask.current_task(self.doc)
            self.selected = current.id if current else self.doc.tasks[0].id

    # ── selection ────────────────────────────────────────────────────

    def _ids(self) -> list:
        return [t.id for t in self.doc.tasks]

    def _move(self, delta: int) -> None:
        ids = self._ids()
        if not ids:
            return
        try:
            idx = ids.index(self.selected)
        except ValueError:
            idx = 0
        self.selected = ids[max(0, min(len(ids) - 1, idx + delta))]
        self.flash = ""

    def toggle(self, task_id: Optional[str] = None) -> None:
        task_id = task_id or self.selected
        if task_id in self.expanded:
            self.expanded.discard(task_id)
        else:
            self.expanded.add(task_id)

    def submit(self) -> None:
        """The person says the selected task is finished."""
        task = self.doc.task(self.selected)
        if task is None:
            return
        if task.status in retask.CLOSED:
            self.flash = f"{task.id} is already {task.status}"
            return
        if task.status == retask.SUBMITTED:
            self.flash = f"{task.id} is waiting for the AI to check it"
            return
        try:
            doc = retask.load(self.path)
            fresh = doc.task(task.id)
            block = retask.claim_block(doc, fresh) if fresh else "is no longer in the list"
            if block:
                self.flash = f"{task.id} {block}"
                return
            outcome = retask.set_status(doc, task.id, retask.SUBMITTED)
            if not outcome["ok"]:
                self.flash = outcome["error"]
                return
            retask.save(self.path, doc)
        except retask.RetaskError as exc:
            self.flash = str(exc)
            return
        self.doc = doc
        self._mtime = self._disk_mtime()
        self.flash = f"{task.id} submitted — the AI checks it on your next message"

    # ── copy ─────────────────────────────────────────────────────────

    def task_text(self, task_id: Optional[str] = None) -> str:
        """The selected task as plain text: title, description, checks."""
        task = self.doc.task(task_id or self.selected)
        if task is None:
            return ""
        parts = [f"{task.id} {task.title}"]
        if task.description.strip():
            parts.append(retask.strip_media(task.description).strip("\n"))
        if task.checks:
            parts.append("Done when:\n" + "\n".join(
                f"- {retask.describe_check(c)}" for c in task.checks))
        return "\n\n".join(parts)

    def copy_selected(self) -> str:
        text = self.task_text()
        if not text:
            return ""
        via = copy_to_clipboard(text, getattr(self._app, "output", None))
        self.flash = (f"copied {self.selected} ({len(text)} chars) via {via}"
                      if via else "could not reach a clipboard — turn mouse off (m) and select the text")
        return text

    def toggle_mouse(self) -> None:
        self.mouse = not self.mouse
        self.flash = ("mouse on: click selects, click again folds (m to turn off and select text)"
                      if self.mouse else "mouse off: select text with the mouse to copy")

    # ── keys ─────────────────────────────────────────────────────────

    def _bind_keys(self) -> None:
        kb = self._kb
        not_help = ~Condition(lambda: self.show_help)

        def _exit(event):
            self._quit = True
            event.app.exit()

        kb.add("q", filter=not_help)(_exit)
        kb.add("escape")(lambda e: setattr(self, "show_help", False)
                         if self.show_help else _exit(e))
        kb.add("down")(lambda e: self._move(1))
        kb.add("up")(lambda e: self._move(-1))
        kb.add("j", filter=not_help)(lambda e: self._move(1))
        kb.add("k", filter=not_help)(lambda e: self._move(-1))
        kb.add("g", filter=not_help)(lambda e: self._move(-10 ** 6))
        kb.add("G", filter=not_help)(lambda e: self._move(10 ** 6))
        kb.add("enter")(lambda e: self.toggle())
        kb.add("space")(lambda e: self.toggle())
        kb.add("s", filter=not_help)(lambda e: self.submit())
        kb.add("y", filter=not_help)(lambda e: self.copy_selected())
        kb.add("m", filter=not_help)(lambda e: self.toggle_mouse())
        kb.add("pageup")(lambda e: self._scroll(-self._visible_height()))
        kb.add("pagedown")(lambda e: self._scroll(self._visible_height()))
        kb.add("?")(lambda e: setattr(self, "show_help", not self.show_help))

    # ── layout maths ─────────────────────────────────────────────────

    def _size(self) -> tuple:
        try:
            size = shutil.get_terminal_size((100, 30))
            return size.columns, size.lines
        except Exception:
            return 100, 30

    def _visible_height(self) -> int:
        return max(4, self._size()[1] - 3)

    def _body_width(self) -> int:
        return max(24, self._size()[0] - _INSPECTOR_WIDTH - 2)

    def _scroll(self, delta: int) -> None:
        self.scroll_top = max(0, self.scroll_top + delta)

    # ── rendering ────────────────────────────────────────────────────

    def _header_fragments(self):
        self._reload_if_changed()
        done, total = retask.progress(self.doc)
        shown = os.path.relpath(self.path, self.root) if self.root else self.path
        out = [
            ("class:header", "  RETASK"),
            ("class:header.path", f"  {shown}"),
            ("class:dim", f"  {done}/{total} done"),
        ]
        if self.doc.goal:
            out.append(("class:dim", f"  {symbol_dot()} {_crop(self.doc.goal, 60)}"))
        return out

    def _rows(self) -> list:
        """[(task_id|None, fragments)] — one entry per screen line."""
        width = self._body_width()
        current = retask.current_task(self.doc)
        rows = [(None, [("class:dim", f"  {self.doc.title}")])]
        rows.append((None, []))
        for task in self.doc.tasks:
            is_sel = task.id == self.selected
            has_body = bool(task.description.strip() or task.checks or task.notes)
            fold = ("▾" if task.id in self.expanded else "▸") if has_body else " "
            mark = retask.STATUS_MARK[task.status]
            style = _STATUS_STYLE.get(task.status, "class:st.pend")
            title_style = "class:selected" if is_sel else (
                "class:dim" if task.status in retask.CLOSED else "")
            frags = [
                ("class:nid" if is_sel else "class:dim", " › " if is_sel else "   "),
                ("class:dim", f"{fold} "),
                (style, f"[{mark}]"),
                ("class:nfile", f" {task.id} "),
                (title_style, _crop(task.title, max(8, width - 16 - len(task.id)))),
            ]
            if current is task:
                frags.append(("class:flow.label", "  ← now"))
            rows.append((task.id, frags))
            if task.id in self.expanded and has_body:
                for para in retask.strip_media(task.description).strip("\n").splitlines() or [""]:
                    for piece in _wrap(para, width - 8):
                        rows.append((task.id, [("", "        " + piece)]))
                rows.append((task.id, []))
        return rows

    def _body_fragments(self):
        rows = self._rows()
        visible = self._visible_height()
        self._row_task = [task_id for task_id, _ in rows]
        # keep the selected title line on screen
        try:
            sel_row = next(i for i, (tid, fr) in enumerate(rows)
                           if tid == self.selected and fr and "›" in fr[0][1])
        except StopIteration:
            sel_row = 0
        if sel_row < self.scroll_top:
            self.scroll_top = sel_row
        elif sel_row >= self.scroll_top + visible:
            self.scroll_top = sel_row - visible + 1
        self.scroll_top = max(0, min(self.scroll_top, max(0, len(rows) - visible)))
        out = []
        for _, frags in rows[self.scroll_top:self.scroll_top + visible]:
            out.extend(frags)
            out.append(("", "\n"))
        return out

    def _inspector_fragments(self):
        task = self.doc.task(self.selected)
        if task is None:
            return [("class:dim", "  Select a task\n")]
        out = [
            ("class:inspector.title", "  INSPECTOR\n"),
            ("class:dim", "  " + "─" * 30 + "\n"),
            ("class:inspector.label", "  TASK\n"),
            ("class:nid", f"  {task.id}\n"),
            ("class:inspector.label", "  STATUS\n"),
            (_STATUS_STYLE.get(task.status, ""), f"  {task.status}\n"),
        ]
        if task.after:
            out.append(("class:inspector.label", "  AFTER\n"))
            for dep in task.after:
                other = self.doc.task(dep)
                state = other.status if other else "?"
                out.append(("class:dim", "  " + _crop(f"{dep} ({state})", 34) + "\n"))
        if task.checks:
            out.append(("class:inspector.label", "  DONE WHEN\n"))
            for check in task.checks:
                for piece in _wrap(retask.describe_check(check), 34):
                    out.append(("class:inspector.value", f"  {piece}\n"))
        if task.notes:
            out.append(("class:inspector.label", "  HISTORY\n"))
            for note in task.notes[-6:]:
                note_style = "class:st.fail" if " not done:" in note else "class:dim"
                for piece in _wrap(note, 34):
                    out.append((note_style, f"  {piece}\n"))
        return out

    def _status_fragments(self):
        if self.error:
            return [("class:st.fail", f"  {self.error}")]
        if self.flash:
            return [("class:statusbar", f"  {self.flash}")]
        keys = "↑↓/jk select · Enter fold · y copy task · s I finished this · m mouse · q close"
        if self.show_help:
            return [("class:help", f"  {keys} · Esc close help")]
        return [("class:statusbar", f"  {keys} · ? help · {len(self.doc.tasks)} tasks")]

    # ── mouse ────────────────────────────────────────────────────────

    def _body_mouse(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._scroll(-3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._scroll(3)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            row = self.scroll_top + mouse_event.position.y
            if 0 <= row < len(self._row_task) and self._row_task[row]:
                task_id = self._row_task[row]
                if task_id == self.selected:
                    self.toggle(task_id)
                else:
                    self.selected = task_id
                return None
        return NotImplemented

    # ── layout ───────────────────────────────────────────────────────

    def _build_layout(self):
        header_win = Window(content=FormattedTextControl(self._header_fragments), height=1)
        body_win = Window(
            content=_MouseControl(self._body_fragments, mouse_callback=self._body_mouse),
            style="class:root", wrap_lines=False)
        inspector_win = Window(
            content=FormattedTextControl(self._inspector_fragments),
            width=_INSPECTOR_WIDTH, wrap_lines=True, style="class:root")
        status_win = Window(
            content=FormattedTextControl(self._status_fragments), height=1,
            style="class:statusbar")
        body = VSplit([body_win, Window(width=1, char="│", style="class:border"), inspector_win])
        return Layout(HSplit([header_win, body, status_win]))

    # ── lifecycle ────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            import laintas_cli
            laintas_cli._clear_stale_running_loop()
        except Exception:
            pass
        try:
            self._app.run()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            try:
                import laintas_cli
                laintas_cli._clear_stale_running_loop()
            except Exception:
                pass


def _osc52(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    seq = f"\x1b]52;c;{payload}\x07"
    if os.environ.get("TMUX"):
        # tmux swallows OSC 52 unless it is wrapped for passthrough.
        seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
    return seq


def _system_clipboard(text: str) -> bool:
    """A local clipboard command, when this machine has a display to own one."""
    candidates = []
    if shutil.which("pbcopy"):
        candidates.append(["pbcopy"])
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        candidates.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            candidates.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            candidates.append(["xsel", "--clipboard", "--input"])
    for cmd in candidates:
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), timeout=2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def copy_to_clipboard(text: str, output=None) -> str:
    """Copy text; return how ("clipboard", "terminal") or "" when neither was possible.

    OSC 52 is always sent when there is a terminal to send it to: over SSH it is
    the only way to reach the clipboard of the machine the person is sitting at,
    and terminals that ignore it ignore it silently.
    """
    via = ""
    if _system_clipboard(text):
        via = "clipboard"
    if output is not None and hasattr(output, "write_raw"):
        try:
            output.write_raw(_osc52(text))
            output.flush()
            via = via or "terminal"
        except Exception:
            pass
    return via


def symbol_dot() -> str:
    try:
        import symbols
        return symbols.BULLET
    except Exception:
        return "·"


def open_viewer(path: str, *, root: str = "", input=None, output=None) -> bool:
    """Load and show one checklist. False (message printed) when it cannot be read."""
    try:
        viewer = RetaskViewer(path, root=root, input=input, output=output)
    except retask.RetaskError as exc:
        print(f"retask view: {exc}")
        return False
    viewer.run()
    return True
