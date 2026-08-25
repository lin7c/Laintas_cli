"""Full-screen infinite-canvas viewer for the terminal.

The interactive half of ``infinite_canvas``: one prompt_toolkit Application
that owns a ``Viewport`` and repaints the scene under it. The wheel zooms at
the pointer, dragging pans, and every zoom notch changes *what* is drawn, not
just how big it is — the level-of-detail rules live in ``infinite_canvas``,
this module only moves the window and reports what is under the cursor.

It follows ``hwg_view``'s lifecycle exactly (``_clear_stale_running_loop``
around ``app.run``) so the REPL never inherits a stale asyncio loop.
"""

from __future__ import annotations

import shutil
from typing import Optional

import infinite_canvas as ic

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.filters import Condition
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style


_STYLE = Style.from_dict({
    "root":            "bg:#0d1117 #e6edf3",
    "header":          "bold #4ade80",
    "header.path":     "bold #a78bfa",
    "dim":             "#6b7d6b",
    "help":            "#6b7d6b italic",
    "border":          "#5b6b7e",
    "statusbar":       "bg:#161b22 #8b949e",
    "search":          "bg:#161b22 bold #fbbf24",
    # scene styles (Shape.style -> class:cv.<style>)
    "cv.node":         "#8b949e",
    "cv.dir":          "bold #a78bfa",
    "cv.package":      "bold #a78bfa",
    "cv.module":       "bold #60a5fa",
    "cv.class":        "bold #4ade80",
    "cv.function":     "#e3b341",
    "cv.text":         "#e6edf3",
    "cv.shape":        "#60a5fa",
    "cv.detail":       "#8b949e",
    "cv.sel":          "bold #f0f6fc",
    "cv.hi":           "bold #fbbf24",
    "cv.edge":         "#3f4a56",
    "cv.edge.hi":      "#d2a8ff",
    "cv.map":          "#3f4a56",
    "cv.map.view":     "bold #fbbf24",
    "inspector.title": "bold #a78bfa",
    "inspector.label": "#8b949e",
    "inspector.value": "#e6edf3",
})

def _cwidth(text: str) -> int:
    try:
        from prompt_toolkit.utils import get_cwidth
        return get_cwidth(str(text))
    except Exception:                                  # pragma: no cover
        return len(str(text))


def _crop(text: str, width: int) -> str:
    text = str(text or "")
    if _cwidth(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = max(0, _cwidth(ch))
        if used + w > max(0, width - 1):
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _short(path: str) -> str:
    """A board's name as the person typed it, not its absolute path."""
    import os
    try:
        return os.path.relpath(path)
    except ValueError:                                 # different drive
        return str(path)


_HELP = [
    ("wheel / +  -",      "zoom at the pointer (semantic: detail appears)"),
    ("drag / arrows hjkl", "pan"),
    ("click",             "select · double-click dives into a shape"),
    ("Enter",             "dive into the selection"),
    ("Backspace",         "back out to its parent"),
    ("0",                 "fit the whole scene"),
    ("/",                 "search labels · n / N step through matches"),
    ("Tab / S-Tab",       "cycle what is on screen"),
    ("b",                 "switch to another board"),
    ("w",                 "draw: r/o/d shapes · a arrow · l line · p pencil"),
    ("c / f / 1-3",       "while drawing: colour · fill · stroke width"),
    ("t / x / u",         "text · delete · undo (boards only)"),
    ("i",                 "inspector · m minimap · e connectors"),
    ("q / Esc",           "close"),
]


class CanvasViewer:
    """One full-screen canvas session over an already-built scene.

    ``boards`` and ``load_board`` are optional: give them and the session can
    switch to another file without closing (``b``). They are passed in rather
    than looked up here because this class knows about scenes, not about what
    a board file is — the same viewer shows a whiteboard and a code atlas.
    """

    def __init__(self, scene: ic.Scene, *, title: str = "",
                 boards: Optional[list] = None,
                 load_board=None, empty_hint: Optional[list] = None,
                 editor=None, reload_scene=None,
                 input=None, output=None):
        self.scene = scene
        self.title = title or scene.title or "canvas"
        self.index = scene.index()
        self.boards = list(boards or [])
        self.load_board = load_board
        self.empty_hint = list(empty_hint or [])
        self.picker_open = False
        self.picker_at = 0
        # Drawing is off until asked for: on a read-only scene (a code atlas)
        # there is nothing to draw on, and on a board a stray keystroke should
        # not put a rectangle in somebody's diagram.
        self.editor = editor
        self.reload_scene = reload_scene
        self.draw_mode = False
        self.tool = "rectangle"
        self.style_at = {"color": 0, "fill": 0, "width": 1}
        self._path: Optional[list] = None      # a freehand stroke in progress
        self.arrow_from: Optional[str] = None
        self.typing_label = False
        self.label_text = ""
        self._draw_rect: Optional[tuple[int, int, int, int]] = None
        self.selected: Optional[str] = None
        self.show_inspector = True
        self.show_minimap = True
        self.show_edges = True
        self.show_help = False
        self.search_mode = False
        self.query = ""
        self.matches: list[str] = []
        self.match_at = 0
        self.status_note = ""
        self._drag_from: Optional[tuple[int, int]] = None
        self._dragged = False
        self._last_render: Optional[ic.Rendered] = None
        self._last_click: Optional[tuple[int, int, float]] = None

        cols, rows = shutil.get_terminal_size((100, 30))
        self.vp = ic.Viewport(width=max(20, cols - self._side_width()),
                              height=max(8, rows - 3))
        self.vp.fit(scene.bounds())

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

    def _side_width(self) -> int:
        return 36 if self.show_inspector else 0

    # ── selection ────────────────────────────────────────────────────

    def select(self, shape_id: Optional[str]) -> None:
        self.selected = shape_id
        self.status_note = ""

    def dive(self, shape_id: Optional[str] = None) -> None:
        """Zoom to a shape at a scale where its children become readable."""
        shape = self.index.get(shape_id or self.selected or "")
        if shape is None:
            return
        self.selected = shape.id
        span = max(shape.w, shape.h / ic.CELL_ASPECT, 1e-6)
        target = min(ic.MAX_SCALE, (self.vp.width * 0.8) / span)
        self.vp.focus(shape, target)

    def ascend(self) -> None:
        shape = self.index.get(self.selected or "")
        parent = self.index.get(shape.parent) if shape and shape.parent else None
        if parent is not None:
            self.dive(parent.id)
        else:
            self.vp.fit(self.scene.bounds())
            self.selected = None

    # ── drawing ──────────────────────────────────────────────────────

    SHAPE_TOOLS = ("rectangle", "ellipse", "diamond")
    STROKE_TOOLS = ("line", "freedraw")

    def _drawing_shape(self) -> bool:
        return bool(self.draw_mode and self.editor
                    and self.tool in self.SHAPE_TOOLS)

    def _drawing_stroke(self) -> bool:
        return bool(self.draw_mode and self.editor
                    and self.tool in self.STROKE_TOOLS)

    def pen(self) -> dict:
        """The colour, fill and weight new elements are drawn with."""
        import canvas_edit
        return {
            "color": canvas_edit.STROKE_COLORS[
                self.style_at["color"] % len(canvas_edit.STROKE_COLORS)],
            "background": canvas_edit.FILL_COLORS[
                self.style_at["color"] % len(canvas_edit.FILL_COLORS)]
            if self.style_at["fill"] else "transparent",
            "fill": canvas_edit.FILL_STYLES[
                (self.style_at["fill"] - 1) % len(canvas_edit.FILL_STYLES)]
            if self.style_at["fill"] else "",
            "strokeWidth": canvas_edit.STROKE_WIDTHS[
                self.style_at["width"] % len(canvas_edit.STROKE_WIDTHS)],
        }

    def cycle_style(self, what: str, delta: int = 1) -> None:
        import canvas_edit
        sizes = {"color": len(canvas_edit.STROKE_COLORS),
                 "fill": len(canvas_edit.FILL_STYLES) + 1,   # +1 = no fill
                 "width": len(canvas_edit.STROKE_WIDTHS)}
        if what not in sizes:
            return
        self.style_at[what] = (self.style_at[what] + delta) % sizes[what]
        pen = self.pen()
        self.status_note = (f"{what}: " + str(
            pen["color"] if what == "color" else
            (pen["fill"] or "none") if what == "fill" else pen["strokeWidth"]))

    def toggle_draw(self) -> None:
        if self.editor is None:
            self.status_note = "this view is read-only"
            return
        self.draw_mode = not self.draw_mode
        self.arrow_from = None
        self._draw_rect = None
        self.status_note = "draw mode" if self.draw_mode else "viewing"

    def set_tool(self, tool: str) -> None:
        if self.editor is None:
            self.status_note = "this view is read-only"
            return
        self.draw_mode = True
        self.tool = tool
        self.arrow_from = None
        self.status_note = f"tool: {tool}"

    def _after_edit(self, ok: bool, message: str, select: str = None) -> None:
        """One place where an edit becomes a new scene on screen."""
        if not ok:
            self.status_note = message or "refused"
        if self.reload_scene is None:
            return
        scene = self.reload_scene()
        self.scene = scene
        self.index = scene.index()
        if select and select in self.index:
            self.selected = select
        elif self.selected not in self.index:
            self.selected = None
        self._last_render = None

    def _finish_shape(self, c0: int, r0: int, c1: int, r1: int) -> None:
        """Turn a rubber band in screen cells into a shape in world units."""
        x0, y0 = self.vp.to_world(min(c0, c1), min(r0, r1))
        x1, y1 = self.vp.to_world(max(c0, c1) + 1, max(r0, r1) + 1)
        ok, msg, new_id = self.editor.draw_shape(
            self.tool, x0, y0, x1 - x0, y1 - y0, self.pen())
        self._after_edit(ok, msg, new_id)
        if ok:
            self.status_note = f"{self.tool} added"

    def _finish_stroke(self, path: list) -> None:
        """Cells the pointer visited become world points on the board."""
        points = [self.vp.to_world(col + 0.5, row + 0.5) for col, row in path]
        if len(points) < 2 or all(p == points[0] for p in points):
            self.status_note = "too short to draw"
            return
        ok, msg, new_id = self.editor.draw_stroke(
            self.tool, points, self.pen())
        self._after_edit(ok, msg, new_id)
        if ok:
            self.status_note = ("line added" if self.tool == "line"
                                else f"stroke of {len(points)} points")

    def place_shape_at_centre(self) -> None:
        """Keyboard equivalent of a drag, for terminals without a mouse."""
        if not self._drawing_shape():
            return
        span = max(8.0, self.vp.width / 5.0 / max(self.vp.scale, 1e-6))
        x, y = self.vp.to_world(self.vp.width / 2, self.vp.height / 2)
        ok, msg, new_id = self.editor.draw_shape(
            self.tool, x - span / 2, y - span / 4, span, span / 2, self.pen())
        self._after_edit(ok, msg, new_id)
        if ok:
            self.status_note = f"{self.tool} added at centre"

    def start_arrow(self) -> None:
        """Arrows connect two shapes: pick one, then pick the other."""
        if self.editor is None:
            self.status_note = "this view is read-only"
            return
        self.draw_mode = True
        self.tool = "arrow"
        if not self.selected:
            self.arrow_from = None
            self.status_note = "arrow: select the shape it starts from"
            return
        if self.arrow_from is None:
            self.arrow_from = self.selected
            self.status_note = "arrow: now select the shape it points at"
            return
        if self.arrow_from == self.selected:
            self.status_note = "arrow: pick a different shape"
            return
        src = self.editor.find(self.arrow_from)
        dst = self.editor.find(self.selected)
        self.arrow_from = None
        if src is None or dst is None:
            self.status_note = "arrow: one of those is gone"
            return
        ok, msg, _ = self.editor.draw_arrow(src, dst)
        self._after_edit(ok, msg)
        if ok:
            self.status_note = "arrow added"

    def begin_label(self) -> None:
        if self.editor is None:
            self.status_note = "this view is read-only"
            return
        self.typing_label = True
        self.label_text = ""

    def commit_label(self) -> None:
        content, self.label_text = self.label_text, ""
        self.typing_label = False
        if not content.strip():
            return
        if self.selected and self.editor.find(self.selected) is not None:
            ok, msg = self.editor.set_label(self.selected, content)
            self._after_edit(ok, msg, self.selected)
        else:
            # No shape picked: the text is the thing being drawn.
            x, y = self.vp.to_world(self.vp.width / 2, self.vp.height / 2)
            ok, msg, new_id = self.editor.draw_text(content, x, y)
            self._after_edit(ok, msg, new_id)
        if ok:
            self.status_note = "text added"

    def erase_selected(self) -> None:
        if self.editor is None or not self.selected:
            return
        if self.editor.find(self.selected) is None:
            return
        gone = self.selected
        ok, msg = self.editor.erase(gone)
        self.selected = None
        self._after_edit(ok, msg)
        if ok:
            self.status_note = "deleted (u undoes it)"

    def undo_edit(self) -> None:
        if self.editor is None:
            return
        ok, msg = self.editor.undo()
        self._after_edit(ok, msg)
        self.status_note = msg or ("undone" if ok else "nothing to undo")

    # ── switching boards ─────────────────────────────────────────────

    def open_picker(self) -> None:
        if not self.boards or self.load_board is None:
            self.status_note = "no other boards here"
            return
        self.picker_open = True
        self.picker_at = min(self.picker_at, len(self.boards) - 1)

    def move_picker(self, delta: int) -> None:
        if self.boards:
            self.picker_at = (self.picker_at + delta) % len(self.boards)

    def choose_board(self) -> None:
        """Swap the scene under the viewport, keeping the session alive."""
        self.picker_open = False
        if not self.boards or self.load_board is None:
            return
        path = self.boards[self.picker_at]
        try:
            scene, title = self.load_board(path)
        except Exception as exc:                       # unreadable board
            self.status_note = f"{type(exc).__name__}: {exc}"
            return
        self.scene = scene
        self.index = scene.index()
        self.title = title
        # Everything derived from the old scene has to go with it; a stale
        # selection or match list would point at ids this board never had.
        self.selected = None
        self.matches = []
        self.match_at = 0
        self.query = ""
        self._last_render = None
        self.vp.fit(scene.bounds())
        self.status_note = f"opened {title}"

    # ── search ───────────────────────────────────────────────────────

    def run_search(self) -> None:
        hits = ic.search(self.scene, self.query)
        self.matches = [s.id for s in hits]
        self.match_at = 0
        if self.matches:
            self.dive(self.matches[0])
            self.status_note = f"{len(self.matches)} match(es)"
        else:
            self.status_note = f"no match for '{self.query}'"

    def step_match(self, delta: int) -> None:
        if not self.matches:
            return
        self.match_at = (self.match_at + delta) % len(self.matches)
        self.dive(self.matches[self.match_at])
        self.status_note = f"{self.match_at + 1}/{len(self.matches)}"

    # ── keys ─────────────────────────────────────────────────────────

    def _bind_keys(self) -> None:
        kb = self._kb
        typing = Condition(lambda: self.search_mode)
        labelling = Condition(lambda: self.typing_label and not self.search_mode)
        picking = Condition(lambda: self.picker_open and not self.search_mode
                            and not self.typing_label)
        normal = Condition(lambda: not self.search_mode and not self.picker_open
                           and not self.typing_label)
        # hjkl pan while reading, but in draw mode the letters are tools —
        # `l` is Excalidraw's line, `p` its pencil. Panning is always on the
        # arrow keys, which is what the status bar says in that mode.
        viewing = Condition(lambda: not self.search_mode and not self.picker_open
                            and not self.typing_label and not self.draw_mode)
        # Keys that mean one thing while reading and another while drawing are
        # bound twice under mutually exclusive filters, never once with a
        # branch inside: two live bindings for one key is a coin toss.
        drawing = Condition(lambda: not self.search_mode and not self.picker_open
                            and not self.typing_label and self.draw_mode)

        # ---- typing a label ----

        @kb.add("<any>", filter=labelling)
        def _label_typed(event) -> None:
            data = event.data
            if data and data.isprintable():
                self.label_text += data

        @kb.add("backspace", filter=labelling)
        def _label_erase(event) -> None:
            self.label_text = self.label_text[:-1]

        @kb.add("enter", filter=labelling)
        def _label_done(event) -> None:
            self.commit_label()

        @kb.add("escape", filter=labelling)
        def _label_cancel(event) -> None:
            self.typing_label = False
            self.label_text = ""


        # ---- board picker ----

        @kb.add("up", filter=picking)
        @kb.add("k", filter=picking)
        def _pick_up(event) -> None:
            self.move_picker(-1)

        @kb.add("down", filter=picking)
        @kb.add("j", filter=picking)
        def _pick_down(event) -> None:
            self.move_picker(1)

        @kb.add("enter", filter=picking)
        def _pick(event) -> None:
            self.choose_board()

        @kb.add("escape", filter=picking)
        @kb.add("b", filter=picking)
        @kb.add("q", filter=picking)
        def _pick_cancel(event) -> None:
            self.picker_open = False

        @kb.add("<any>", filter=typing)
        def _typed(event) -> None:
            data = event.data
            if data and data.isprintable():
                self.query += data

        @kb.add("backspace", filter=typing)
        def _erase(event) -> None:
            self.query = self.query[:-1]

        @kb.add("enter", filter=typing)
        def _accept(event) -> None:
            self.search_mode = False
            self.run_search()

        @kb.add("escape", filter=typing)
        def _cancel(event) -> None:
            self.search_mode = False
            self.query = ""

        @kb.add("left", filter=normal)
        @kb.add("h", filter=viewing)
        def _left(event) -> None:
            self.vp.pan_cells(-max(2, self.vp.width // 8), 0)

        @kb.add("right", filter=normal)
        @kb.add("l", filter=viewing)
        def _right(event) -> None:
            self.vp.pan_cells(max(2, self.vp.width // 8), 0)

        @kb.add("up", filter=normal)
        @kb.add("k", filter=viewing)
        def _up(event) -> None:
            self.vp.pan_cells(0, -max(1, self.vp.height // 8))

        @kb.add("down", filter=normal)
        @kb.add("j", filter=viewing)
        def _down(event) -> None:
            self.vp.pan_cells(0, max(1, self.vp.height // 8))

        @kb.add("pageup", filter=normal)
        def _pgup(event) -> None:
            self.vp.pan_cells(0, -self.vp.height)

        @kb.add("pagedown", filter=normal)
        def _pgdn(event) -> None:
            self.vp.pan_cells(0, self.vp.height)

        @kb.add("+", filter=normal)
        @kb.add("=", filter=normal)
        def _zin(event) -> None:
            self.vp.zoom_center(ic.ZOOM_STEP)

        @kb.add("-", filter=normal)
        @kb.add("_", filter=normal)
        def _zout(event) -> None:
            self.vp.zoom_center(1 / ic.ZOOM_STEP)

        @kb.add("0", filter=normal)
        @kb.add("f", filter=viewing)
        def _fit(event) -> None:
            self.vp.fit(self.scene.bounds())
            self.status_note = "fit"

        @kb.add("enter", filter=normal)
        def _dive(event) -> None:
            self.dive()

        @kb.add("backspace", filter=normal)
        def _up_level(event) -> None:
            self.ascend()

        @kb.add("tab", filter=normal)
        def _next(event) -> None:
            self._cycle(1)

        @kb.add("s-tab", filter=normal)
        def _prev(event) -> None:
            self._cycle(-1)

        @kb.add("/", filter=normal)
        def _search(event) -> None:
            self.search_mode = True
            self.query = ""

        @kb.add("n", filter=normal)
        def _next_match(event) -> None:
            self.step_match(1)

        @kb.add("N", filter=normal)
        def _prev_match(event) -> None:
            self.step_match(-1)

        @kb.add("i", filter=normal)
        def _inspector(event) -> None:
            self.show_inspector = not self.show_inspector

        @kb.add("m", filter=normal)
        def _minimap(event) -> None:
            self.show_minimap = not self.show_minimap

        @kb.add("e", filter=normal)
        def _edges(event) -> None:
            self.show_edges = not self.show_edges

        @kb.add("b", filter=normal)
        def _switch(event) -> None:
            self.open_picker()

        # ---- drawing ----

        @kb.add("w", filter=normal)
        def _draw(event) -> None:
            self.toggle_draw()

        @kb.add("r", filter=normal)
        def _rect(event) -> None:
            self.set_tool("rectangle")

        @kb.add("o", filter=normal)
        def _ellipse(event) -> None:
            self.set_tool("ellipse")

        @kb.add("d", filter=normal)
        def _diamond(event) -> None:
            self.set_tool("diamond")

        @kb.add("a", filter=normal)
        def _arrow(event) -> None:
            self.start_arrow()

        @kb.add("t", filter=normal)
        def _label(event) -> None:
            self.begin_label()

        @kb.add("l", filter=drawing)
        def _line(event) -> None:
            self.set_tool("line")

        @kb.add("p", filter=normal)
        def _pencil(event) -> None:
            self.set_tool("freedraw")

        @kb.add("space", filter=drawing)
        def _place(event) -> None:
            self.place_shape_at_centre()

        @kb.add("c", filter=drawing)
        def _colour(event) -> None:
            self.cycle_style("color")

        @kb.add("f", filter=drawing)
        def _fill(event) -> None:
            self.cycle_style("fill")

        @kb.add("1", filter=drawing)
        def _thin(event) -> None:
            self.style_at["width"] = 0
            self.status_note = "stroke: thin"

        @kb.add("2", filter=drawing)
        def _medium(event) -> None:
            self.style_at["width"] = 1
            self.status_note = "stroke: medium"

        @kb.add("3", filter=drawing)
        def _thick(event) -> None:
            self.style_at["width"] = 2
            self.status_note = "stroke: thick"

        @kb.add("x", filter=normal)
        @kb.add("delete", filter=normal)
        def _erase(event) -> None:
            self.erase_selected()

        @kb.add("u", filter=normal)
        def _undo(event) -> None:
            self.undo_edit()

        @kb.add("?", filter=normal)
        def _help(event) -> None:
            self.show_help = not self.show_help

        @kb.add("q", filter=normal)
        @kb.add("escape", filter=normal)
        @kb.add("c-c")
        def _quit(event) -> None:
            event.app.exit()

    def _cycle(self, delta: int) -> None:
        visible = (self._last_render.visible if self._last_render else [])
        if not visible:
            return
        if self.selected in visible:
            i = (visible.index(self.selected) + delta) % len(visible)
        else:
            i = 0
        self.select(visible[i])

    # ── painting ─────────────────────────────────────────────────────

    def _canvas_fragments(self):
        cols, rows = shutil.get_terminal_size((100, 30))
        self.vp.width = max(20, cols - self._side_width())
        self.vp.height = max(8, rows - 3)

        highlight = set(self.matches[self.match_at:self.match_at + 1]) \
            if self.matches else set()
        rendered = ic.render(self.scene, self.vp, selected=self.selected,
                             show_connectors=self.show_edges,
                             highlight=highlight)
        self._last_render = rendered
        if self._draw_rect is not None:
            self._paint_band(rendered.canvas)
        if self._path:
            self._paint_path(rendered.canvas)
        if self.picker_open:
            # The list is modal: nothing else paints under it, or the two
            # overlap into text that reads as neither.
            self._paint_picker(rendered.canvas)
        elif not self.scene.shapes:
            self._paint_empty(rendered.canvas)
        elif self.show_minimap:
            self._paint_minimap(rendered.canvas)

        out = []
        for row in rendered.canvas.styled_rows():
            for text, style in row:
                out.append((style or "class:root", text))
            out.append(("", "\n"))
        return out

    def _paint_band(self, canvas) -> None:
        """The rubber band, while the button is still down."""
        c0, r0, c1, r1 = self._draw_rect
        left, right = min(c0, c1), max(c0, c1)
        top, bottom = min(r0, r1), max(r0, r1)
        glyph = {"ellipse": "·", "diamond": "◆"}.get(self.tool, "·")
        for col in range(left, right + 1):
            canvas.put_force(top, col, "─", "class:cv.hi")
            canvas.put_force(bottom, col, "─", "class:cv.hi")
        for row in range(top, bottom + 1):
            canvas.put_force(row, left, "│", "class:cv.hi")
            canvas.put_force(row, right, "│", "class:cv.hi")
        canvas.put_force(top, left, glyph, "class:cv.hi")

    def _paint_path(self, canvas) -> None:
        """The stroke under the pointer, before it is committed."""
        glyph = "·" if self.tool == "freedraw" else "─"
        for col, row in self._path:
            canvas.put_force(row, col, glyph, "class:cv.hi")

    def _paint_empty(self, canvas) -> None:
        """A board with nothing on it yet.

        The alternative — refusing to open — is what made `/canvas` feel like
        it needed a file before it would do anything. An empty canvas that
        says how to draw on it is a better answer than an error.
        """
        lines = [f"{self.title} — empty board"]
        lines += [line for line in self.empty_hint if line]
        top = max(0, canvas.height // 2 - len(lines) // 2)
        for i, line in enumerate(lines):
            # Cropped, not just clipped: a hint that runs off the right edge
            # mid-word looks like a rendering fault rather than a long path.
            line = _crop(line, max(4, canvas.width - 2))
            row = top + i
            col = max(0, (canvas.width - _cwidth(line)) // 2)
            canvas.put_force(row, col, line,
                             "class:header" if i == 0 else "class:help")

    def _paint_picker(self, canvas) -> None:
        """The list of boards, over the canvas."""
        title = " boards · ↑↓ Enter Esc "
        rows = min(len(self.boards), max(3, canvas.height - 6))
        longest = max((_cwidth(_short(b)) for b in self.boards), default=20)
        # Wide enough for the title as well as the entries: a title that
        # overruns its own box eats the border and the text beyond it.
        width = max(_cwidth(title) + 4, longest + 6, 24)
        width = min(width, max(10, canvas.width - 2))
        top = max(0, canvas.height // 2 - (rows + 2) // 2)
        left = max(0, (canvas.width - width) // 2)
        first = max(0, min(self.picker_at - rows // 2,
                           max(0, len(self.boards) - rows)))

        canvas.put_force(top, left, "┌" + "─" * (width - 2) + "┐", "class:header.path")
        canvas.put_force(top, left + 2, _crop(title, width - 4),
                         "class:header.path")
        for i in range(rows):
            index = first + i
            row = top + 1 + i
            canvas.put_force(row, left, "│" + " " * (width - 2) + "│",
                             "class:header.path")
            if index >= len(self.boards):
                continue
            mark = "▶ " if index == self.picker_at else "  "
            text = _crop(mark + _short(self.boards[index]), width - 3)
            canvas.put_force(row, left + 1, text,
                             "class:cv.sel" if index == self.picker_at
                             else "class:inspector.value")
        canvas.put_force(top + rows + 1, left,
                         "└" + "─" * (width - 2) + "┘", "class:header.path")

    def _paint_minimap(self, canvas) -> None:
        """A corner map of the whole plane with the viewport marked on it.

        Zooming in loses the sense of where you are; the map is how you get
        it back without zooming out and losing your place.
        """
        mw, mh = 26, 9
        if canvas.width < mw + 4 or canvas.height < mh + 2:
            return
        x0, y0, x1, y1 = self.scene.bounds()
        left, top = canvas.width - mw - 1, 0
        sx = mw / max(1e-6, x1 - x0)
        sy = mh / max(1e-6, y1 - y0)

        for r in range(top, top + mh):
            canvas.put_force(r, left, " " * mw, "class:cv.map")
        for shape in self.scene.shapes:
            if shape.depth > 1:
                continue
            c = left + int((shape.cx - x0) * sx)
            r = top + int((shape.cy - y0) * sy)
            if left <= c < left + mw and top <= r < top + mh:
                canvas.put_force(r, c, "·", "class:cv.map")

        # viewport rectangle
        vx0, vy0 = self.vp.to_world(0, 0)
        vx1, vy1 = self.vp.to_world(self.vp.width - 1, self.vp.height - 1)
        c0 = left + max(0, min(mw - 1, int((vx0 - x0) * sx)))
        c1 = left + max(0, min(mw - 1, int((vx1 - x0) * sx)))
        r0 = top + max(0, min(mh - 1, int((vy0 - y0) * sy)))
        r1 = top + max(0, min(mh - 1, int((vy1 - y0) * sy)))
        for c in range(c0, c1 + 1):
            canvas.put_force(r0, c, "─", "class:cv.map.view")
            canvas.put_force(r1, c, "─", "class:cv.map.view")
        for r in range(r0, r1 + 1):
            canvas.put_force(r, c0, "│", "class:cv.map.view")
            canvas.put_force(r, c1, "│", "class:cv.map.view")

    def _header_fragments(self):
        pct = f"{self.vp.scale * 100:.0f}%"
        bits = [("class:header", f" {self.title} "),
                ("class:dim", f"· {len(self.scene.shapes)} shapes "),
                ("class:header.path", f"· zoom {pct} ")]
        if self.scene.subtitle:
            bits.append(("class:dim", f"· {self.scene.subtitle}"))
        return bits

    def _inspector_fragments(self):
        shape = self.index.get(self.selected or "")
        if shape is None:
            out = [("class:inspector.title", "  Canvas\n\n")]
            for key, what in _HELP:
                out.append(("class:inspector.label", f"  {key:<20}"))
                out.append(("class:inspector.value", f"{what}\n"))
            return out
        out = [("class:inspector.title", f"  {shape.label or shape.id}\n")]
        out.append(("class:dim", f"  {shape.id}\n\n"))
        rows = [("kind", shape.kind), ("style", shape.style),
                ("depth", str(shape.depth))]
        for key, value in shape.meta.items():
            rows.append((str(key), str(value)))
        for key, value in rows:
            if not value:
                continue
            out.append(("class:inspector.label", f"  {key:<9}"))
            out.append(("class:inspector.value", f"{value}\n"))
        if shape.detail:
            out.append(("class:inspector.label", "\n  detail\n"))
            for line in shape.detail:
                out.append(("class:inspector.value", f"  {line}\n"))
        kids = [s for s in self.scene.shapes if s.parent == shape.id]
        if kids:
            out.append(("class:inspector.label",
                        f"\n  contains ({len(kids)})\n"))
            for kid in kids[:12]:
                out.append(("class:inspector.value",
                            f"  · {kid.label or kid.id}\n"))
        return out

    def _status_fragments(self):
        if self.search_mode:
            return [("class:search", f"  /{self.query}▌")]
        note = f" · {self.status_note}" if self.status_note else ""
        lod = ""
        if self._last_render and self.selected:
            names = {ic.LOD_HIDDEN: "hidden", ic.LOD_GLYPH: "glyph",
                     ic.LOD_BLOCK: "block", ic.LOD_FRAME: "frame",
                     ic.LOD_DETAIL: "detail"}
            lod = f" · lod {names.get(self._last_render.lods.get(self.selected, 0), '')}"
        if self.typing_label:
            target = "label" if self.selected else "text"
            return [("class:search", f"  {target}: {self.label_text}▌"
                                     "   (Enter saves · Esc cancels)")]
        if self.picker_open:
            return [("class:statusbar",
                     "  ↑↓ choose · Enter open · Esc cancel")]
        if self.draw_mode:
            pending = " · pick the target" if self.arrow_from else ""
            pen = self.pen()
            ink = f"{pen['color']}{'/' + pen['fill'] if pen['fill'] else ''}"
            return [("class:statusbar",
                     f"  DRAW [{self.tool}] {ink} w{int(pen['strokeWidth'])} · "
                     f"drag to draw · r/o/d/a/l/p tools · t text · space box · "
                     f"c colour · f fill · 1-3 width · x delete · u undo · "
                     f"arrows pan · w back{pending}{note}")]
        switch = " · b boards" if self.boards else ""
        # `i` is on the status bar and not only in the help list, because the
        # help list lives inside the very pane `i` hides: the one reader who
        # needs the key is the one who cannot see it.
        pane = " · i pane" if self.show_inspector else " · i panel back"
        return [("class:statusbar",
                 "  wheel zoom · drag pan · Enter dive · Backspace back · "
                 f"/ search · 0 fit{switch}{pane} · ? help · q quit{lod}{note}")]

    # ── mouse ────────────────────────────────────────────────────────

    def _mouse(self, mouse_event):
        et = mouse_event.event_type
        if self.picker_open:
            # While the list is up, a click on the canvas underneath would
            # select a shape the reader cannot see. Keyboard only.
            return None
        col, row = mouse_event.position.x, mouse_event.position.y

        if et == MouseEventType.SCROLL_UP:
            self.vp.zoom_at(ic.ZOOM_STEP, col, row)
            return None
        if et == MouseEventType.SCROLL_DOWN:
            self.vp.zoom_at(1 / ic.ZOOM_STEP, col, row)
            return None
        if et == MouseEventType.MOUSE_DOWN:
            self._drag_from = (col, row)
            self._dragged = False
            if self._drawing_shape():
                self._draw_rect = (col, row, col, row)
            elif self._drawing_stroke():
                self._path = [(col, row)]
            return None
        if et == MouseEventType.MOUSE_MOVE:
            if self._path is not None:
                # A pencil stroke is the cells the pointer actually visited;
                # a line only ever has two, so the tail is replaced instead
                # of appended.
                if self.tool == "line":
                    self._path = [self._path[0], (col, row)]
                elif (col, row) != self._path[-1]:
                    self._path.append((col, row))
                self._dragged = True
                return None
            if self._draw_rect is not None:
                # Rubber band: the shape is not created until the button comes
                # up, so a drag that changes its mind costs nothing.
                self._draw_rect = (self._draw_rect[0], self._draw_rect[1],
                                   col, row)
                self._dragged = True
                return None
            if self._drag_from is not None:
                dc = self._drag_from[0] - col
                dr = self._drag_from[1] - row
                if dc or dr:
                    self.vp.pan_cells(dc, dr)
                    self._drag_from = (col, row)
                    # Remembered as a flag, not inferred from the release
                    # position: each move re-anchors the drag, so by the time
                    # the button comes up the anchor *is* where the pointer
                    # is, and a drag would read as a click.
                    self._dragged = True
                return None
            return NotImplemented
        if et == MouseEventType.MOUSE_UP:
            moved = self._dragged
            band = self._draw_rect
            path = self._path
            self._draw_rect = None
            self._path = None
            self._drag_from = None
            self._dragged = False
            if band is not None and self._drawing_shape():
                self._finish_shape(band[0], band[1], col, row)
                return None
            if path is not None and self._drawing_stroke():
                self._finish_stroke(path if path[-1] == (col, row)
                                    else path + [(col, row)])
                return None
            if moved:
                return None
            hit = self._last_render.at(row, col) if self._last_render else None
            if hit:
                import time
                now = time.monotonic()
                double = (self._last_click is not None
                          and self._last_click[:2] == (col, row)
                          and now - self._last_click[2] < 0.45)
                self._last_click = (col, row, now)
                if double:
                    self.dive(hit)
                else:
                    self.select(hit)
            return None
        return NotImplemented

    # ── layout / lifecycle ───────────────────────────────────────────

    def _build_layout(self):
        canvas_win = Window(
            content=_MouseControl(self._canvas_fragments,
                                  mouse_callback=self._mouse),
            style="class:root", wrap_lines=False,
            get_vertical_scroll=lambda window: 0,
            allow_scroll_beyond_bottom=False)
        header_win = Window(
            content=FormattedTextControl(self._header_fragments), height=1)
        inspector_win = ConditionalContainer(
            VSplit([
                Window(width=1, char="│", style="class:border"),
                Window(content=FormattedTextControl(self._inspector_fragments),
                       width=35, wrap_lines=True, style="class:root"),
            ]),
            filter=Condition(lambda: self.show_inspector))
        status_win = Window(
            content=FormattedTextControl(self._status_fragments), height=1,
            style="class:statusbar")
        return Layout(HSplit([header_win,
                              VSplit([canvas_win, inspector_win]),
                              status_win]))

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
    """FormattedTextControl with a version-compatible mouse hook."""

    def __init__(self, *args, mouse_callback=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._mouse_callback = mouse_callback

    def mouse_handler(self, mouse_event):
        if self._mouse_callback is not None:
            return self._mouse_callback(mouse_event)
        return NotImplemented


# ── entry ─────────────────────────────────────────────────────────────────

def open_scene(scene: ic.Scene, *, title: str = "", allow_empty: bool = False,
               boards: Optional[list] = None, load_board=None,
               empty_hint: Optional[list] = None, editor=None,
               reload_scene=None, input=None, output=None) -> bool:
    """Run one session. False means the caller should show something else."""
    if not scene.shapes and not allow_empty:
        print("canvas: nothing to show (the scene is empty)")
        return False
    CanvasViewer(scene, title=title, boards=boards, load_board=load_board,
                 empty_hint=empty_hint, editor=editor,
                 reload_scene=reload_scene, input=input, output=output).run()
    return True
