"""Interactive HWG graph viewer - full-screen metro-map TUI.

Companion to ``workflow_viz`` (pure layout). This module owns the
prompt_toolkit Application: keyboard + mouse drive ONE selection model,
the Inspector pane shows the selected node's contract, and the graph
canvas is only rebuilt when the source graph changes (scrolling and
selection are cheap viewport shifts over the cached canvas).

Lifecycle follows resource_ui's pattern exactly (app.run wrapped with
_clear_stale_running_loop so the REPL never sees a stale asyncio loop).
"""

from __future__ import annotations

import shutil
import time
from typing import Optional

import symbols
import workflow_viz

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer, Layout, HSplit, VSplit, Window,
    FormattedTextControl, ScrollOffsets)
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style


_STYLE = Style.from_dict({
    "root":            "bg:#0d1117 #e6edf3",
    "header":          "bold #4ade80",
    "header.path":     "bold #a78bfa",
    "dim":             "#6b7d6b",
    "help":            "#6b7d6b italic",
    "border":          "#5b6b7e",
    "border.manual":   "bold #fbbf24",
    "border.pending":  "#5b6b7e",
    "border.running":  "bold #e3b341",
    "border.done":     "bold #4ade80",
    "border.failed":   "bold #f85149",
    "border.paused":   "bold #e3b341",
    "nid":             "bold #a78bfa",
    "nfile":           "#8b949e",
    "flow":            "#5b6b7e",
    "flow.label":      "#d2a8ff",
    "side":            "#5b6b7e",
    "side.label":      "#d2a8ff",
    "loop":            "#60a5fa",
    "loop.label":      "#60a5fa",
    "st.pend":         "#8b949e",
    "st.run":          "bold #e3b341",
    "st.done":         "bold #4ade80",
    "st.fail":         "bold #f85149",
    "st.paused":       "bold #e3b341",
    "selected":        "bold #f0f6fc",
    "inspector.title": "bold #a78bfa",
    "inspector.label": "#8b949e",
    "inspector.value": "#e6edf3",
    "statusbar":       "bg:#161b22 #8b949e",
    "scrollbar.thumb": "bold #3f4a56",
    "scrollbar.rail":  "#21262d",
})


def _plural(value: int, word: str) -> str:
    return f"{value} {word}{'s' if value != 1 else ''}"


def _crop(text: str, width: int) -> str:
    """Cell-width clamp (CJK-safe) for one inspector line."""
    from prompt_toolkit.utils import get_cwidth
    out, used = [], 0
    for ch in str(text or ""):
        w = max(0, get_cwidth(ch))
        if used + w > width:
            return "".join(out) + "…"
        out.append(ch)
        used += w
    return "".join(out)


class HwgViewer:
    """One full-screen HWG viewing session."""

    def __init__(self, path: str, view: workflow_viz.GraphView,
                 *, status: Optional[dict[str, str]] = None,
                 statements: Optional[list] = None,
                 input=None, output=None):
        self.path = path
        self.view = view                      # already-built, immutable
        self.status = dict(status or {})
        self.statements = statements           # kept for cheap view rebuilds
        self.selected: str = (view.node_order[0]
                              if view.node_order else "")
        self.scroll_top = 0
        self.show_help = False
        self._visible = self._visible_height()
        self._total = view.canvas.height
        self._quit = False
        self._input, self._output = input, output
        self._kb = KeyBindings()
        self._bind_keys()
        self._app = Application(
            layout=self._build_layout(),
            key_bindings=self._kb,
            style=_STYLE,
            full_screen=True,
            mouse_support=True,
            input=input,
            output=output,
        )

    # ── selection model ──────────────────────────────────────────────

    def _order_index(self) -> int:
        try:
            return self.view.node_order.index(self.selected)
        except ValueError:
            return 0

    def select(self, nid: str) -> None:
        if nid and nid in self.view.nodes_by_id:
            self.selected = nid
            self._scroll_to_node(nid)

    def _move(self, delta: int) -> None:
        order = self.view.node_order
        if not order:
            return
        idx = self._order_index()
        self.select(order[max(0, min(len(order) - 1, idx + delta))])

    def _neighbor(self, direction: str) -> None:
        """Follow an outgoing ('down') or incoming ('up') edge."""
        if not self.selected:
            return
        edges = (self.view.out_edges.get(self.selected, [])
                 if direction == "down"
                 else self.view.in_edges.get(self.selected, []))
        if not edges:
            self._move(1 if direction == "down" else -1)
            return
        # Prefer the edge whose other end is nearest in declaration order.
        cur = self._order_index()
        def dist(e: dict) -> int:
            other = e["to"] if direction == "down" else e["from"]
            try:
                return abs(self.view.node_order.index(other) - cur)
            except ValueError:
                return 99
        edge = min(edges, key=dist)
        self.select(edge["to"] if direction == "down" else edge["from"])

    # ── scrolling ────────────────────────────────────────────────────

    def _visible_height(self) -> int:
        try:
            rows = shutil.get_terminal_size((100, 30)).lines
        except Exception:
            rows = 30
        return max(4, rows - 3)     # header + statusbar + margin

    def _scroll_to_node(self, nid: str) -> None:
        row = self.view.node_rows.get(nid, 0)
        if row < self.scroll_top:
            self.scroll_top = row
        elif row >= self.scroll_top + self._visible:
            self.scroll_top = row - self._visible + 1

    def _scroll(self, delta: int) -> None:
        max_top = max(0, self.view.canvas.height - self._visible)
        self.scroll_top = max(0, min(max_top, self.scroll_top + delta))

    # ── key bindings ─────────────────────────────────────────────────

    def _bind_keys(self) -> None:
        kb = self._kb

        def _exit(event):
            self._quit = True
            event.app.exit()

        kb.add("q", filter=~Condition(lambda: self.show_help))(lambda e: _exit(e))
        kb.add("escape")(lambda e: self._toggle_help(False) if self.show_help
                         else _exit(e))
        kb.add("down")(lambda e: self._move(1))
        kb.add("up")(lambda e: self._move(-1))
        kb.add("j", filter=~Condition(lambda: self.show_help))(lambda e: self._move(1))
        kb.add("k", filter=~Condition(lambda: self.show_help))(lambda e: self._move(-1))
        kb.add("right")(lambda e: self._neighbor("down"))
        kb.add("l", filter=~Condition(lambda: self.show_help))(lambda e: self._neighbor("down"))
        kb.add("left")(lambda e: self._neighbor("up"))
        kb.add("h", filter=~Condition(lambda: self.show_help))(lambda e: self._neighbor("up"))
        kb.add("pageup")(lambda e: self._scroll(-self._visible))
        kb.add("pagedown")(lambda e: self._scroll(self._visible))
        kb.add("home")(lambda e: setattr(self, "scroll_top", 0))
        kb.add("end")(lambda e: self._scroll(10 ** 6))
        kb.add("g")(lambda e: self._move(-10 ** 6))
        kb.add("G")(lambda e: self._move(10 ** 6))
        kb.add("?")(lambda e: self._toggle_help(True))
        kb.add("tab")(lambda e: self._cycle_next_status())

    def _toggle_help(self, show: bool) -> None:
        self.show_help = show

    def _cycle_next_status(self) -> None:
        """Cycle the selected node's status; rebuild the view from AST."""
        order = [workflow_viz.STATUS_PENDING, workflow_viz.STATUS_RUNNING,
                 workflow_viz.STATUS_DONE, workflow_viz.STATUS_FAILED,
                 workflow_viz.STATUS_PAUSED]
        cur = self.status.get(self.selected, workflow_viz.STATUS_PENDING)
        try:
            nxt = order[(order.index(cur) + 1) % len(order)]
        except ValueError:
            nxt = order[0]
        self.status[self.selected] = nxt
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild the layout with current status (AST is the source)."""
        if not self.statements:
            return
        graph = workflow_viz.graph_from_statements(self.statements,
                                                    self.status)
        self.view = workflow_viz.build_view(graph)
        self._total = self.view.canvas.height
        # keep the selection valid and on-screen after the rebuild
        if self.selected not in self.view.nodes_by_id:
            self.selected = (self.view.node_order[0]
                             if self.view.node_order else "")
        self._scroll_to_node(self.selected)

    # ── rendering ────────────────────────────────────────────────────

    def _header_fragments(self):
        view = self.view
        status_bit = ""
        if self.status:
            done = sum(1 for s in self.status.values()
                       if s == workflow_viz.STATUS_DONE)
            status_bit = f"  {done}/{len(self.status)} done"
        return [
            ("class:header", "  HWG GRAPH"),
            ("class:header.path", f"  {self.path}"),
            ("class:dim", f"  {view.meta.get('counts', '')}{status_bit}"),
        ]

    def _canvas_fragments(self):
        """Viewport slice of the cached canvas with selection highlight."""
        view = self.view
        self._visible = self._visible_height()
        rows = view.canvas.styled_rows()
        top = min(self.scroll_top, max(0, len(rows) - self._visible))
        self.scroll_top = max(0, top)
        visible = self._visible
        out = []
        sel_rect = view.node_rects.get(self.selected)
        for y in range(top, min(len(rows), top + visible)):
            runs = list(rows[y])
            if sel_rect and sel_rect[0] <= y <= sel_rect[0] + sel_rect[3] - 1:
                runs = [(t, "class:selected") for t, _ in runs]
            for text, style in runs:
                out.append((style, text))
            out.append(("", "\n"))
        return out

    def _scrollbar_fragments(self):
        total = max(1, self.view.canvas.height)
        visible = max(1, self._visible_height())
        if total <= visible:
            return [("class:scrollbar.rail", " " * 2)]
        thumb = max(1, visible * visible // total)
        top = int(self.scroll_top / max(1, total - visible) * (visible - thumb))
        cells = ["│"] * visible
        for i in range(top, min(visible, top + thumb)):
            cells[i] = "█"
        return [("class:scrollbar.thumb", "".join(cells))]

    def _inspector_fragments(self):
        view = self.view
        node = view.node(self.selected)
        if node is None:
            return [("class:dim", "  Select a node\n")]
        label = "  INSPECTOR\n"
        out = [
            ("class:inspector.title", label),
            ("class:dim", "  " + "─" * 30 + "\n"),
            ("class:inspector.label", "  NODE\n"),
            ("class:nid", f"  #{node['id']}#\n"),
        ]
        if node.get("manual"):
            out.append(("class:border.manual", "  manual gate - run pauses here\n"))
        if node.get("file"):
            out += [("class:inspector.label", "  FILE\n"),
                    ("class:inspector.value", f"  {node['file']}\n")]
        io = node.get("io") or {}
        for kind in ("in", "out"):
            params = io.get(kind) or []
            if not params:
                continue
            out.append(("class:inspector.label", f"  {kind.upper()}\n"))
            for p in params:
                text = f"  {p.get('name', '')}"
                if p.get("type"):
                    text += f": {p['type']}"
                if p.get("source") or p.get("default"):
                    text += f" = {p.get('source') or p.get('default')}"
                out.append(("class:inspector.value", _crop(text, 34) + "\n"))
        policy = node.get("policy") or {}
        if policy:
            out.append(("class:inspector.label", "  POLICY\n"))
            bits = " ".join(f"{k}={v}" for k, v in policy.items())
            out.append(("class:inspector.value", f"  {_crop(bits, 32)}\n"))
        # edges
        incoming = view.in_edges.get(self.selected, [])
        outgoing = view.out_edges.get(self.selected, [])
        if incoming:
            out.append(("class:inspector.label", "  IN\n"))
            for e in incoming:
                lbl = workflow_viz.edge_label(e) or "->"
                text = "  " + _crop("#{}# {}".format(e["from"], lbl), 34)
                out.append(("class:dim", text + "\n"))
        if outgoing:
            out.append(("class:inspector.label", "  OUT\n"))
            for e in outgoing:
                lbl = workflow_viz.edge_label(e) or "->"
                text = "  " + _crop("{} #{}#".format(lbl, e["to"]), 34)
                out.append(("class:flow.label", text + "\n"))
        return out

    def _status_fragments(self):
        if self.show_help:
            return [("class:help",
                     "  ↑↓/jk select · ←→/hl follow edge · PgUp/PgDn scroll · Tab cycle status · Esc close")]
        counts = f"{len(self.view.node_order)} nodes"
        return [
            ("class:statusbar",
             f"  ↑↓/jk select · ←→/hl follow edge · PgUp/PgDn scroll · q quit · ? help · {counts}"),
        ]

    # ── mouse ────────────────────────────────────────────────────────

    def _canvas_mouse(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._scroll(-3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._scroll(3)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            row = self.scroll_top + mouse_event.position.y
            nid = self.view.canvas.hit_at(row, mouse_event.position.x)
            if nid:
                self.select(nid)
                return None
        return NotImplemented

    # ── layout ───────────────────────────────────────────────────────

    def _build_layout(self):
        def _canvas_window():
            return Window(
                content=_MouseControl(self._canvas_fragments,
                                      mouse_callback=self._canvas_mouse),
                style="class:root", wrap_lines=False,
                get_vertical_scroll=lambda window: 0,
                allow_scroll_beyond_bottom=False)

        header_win = Window(
            content=FormattedTextControl(self._header_fragments), height=1)
        canvas_win = _canvas_window()
        scroll_win = Window(
            content=FormattedTextControl(self._scrollbar_fragments),
            width=2, dont_extend_width=True, style="class:scrollbar.rail")
        inspector_win = Window(
            content=FormattedTextControl(self._inspector_fragments),
            width=38, wrap_lines=True, style="class:root")
        status_win = Window(
            content=FormattedTextControl(self._status_fragments), height=1,
            style="class:statusbar")

        body = VSplit([canvas_win, scroll_win,
                       Window(width=1, char="│", style="class:border"),
                       inspector_win])
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


class _MouseControl(FormattedTextControl):
    """FormattedTextControl with a small version-compatible mouse hook."""

    def __init__(self, *args, mouse_callback=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._mouse_callback = mouse_callback

    def mouse_handler(self, mouse_event):
        if self._mouse_callback is not None:
            return self._mouse_callback(mouse_event)
        return NotImplemented


# ── Public entry ──────────────────────────────────────────────────────

def open_viewer(path: str, *, status: Optional[dict[str, str]] = None,
                input=None, output=None) -> bool:
    """Parse path, build the view, and run one full-screen session.

    Returns True when the file parsed and a session ran; False (with the
    caller printing the message) when parsing failed.
    """
    from pathlib import Path
    from hwg_adapter import parse, validate

    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        print(f"hwg view: cannot read '{path}': {e}")
        return False
    try:
        statements = parse(source)
    except Exception as e:
        print(f"hwg view: parse error - {e}")
        return False
    errors = validate(statements)
    if errors:
        print("hwg view: validation errors:\n" + "\n".join(errors))
        return False

    graph = workflow_viz.graph_from_statements(statements, status)
    view = workflow_viz.build_view(graph)
    if not view.node_order:
        print("hwg view: graph has no nodes")
        return False
    HwgViewer(path, view, status=status, input=input, output=output).run()
    return True
