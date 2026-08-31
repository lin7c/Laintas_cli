"""Infinite canvas: world coordinates in, painted char grid out.

PURE module. A ``Scene`` is a flat list of ``Shape``s in unbounded world
coordinates plus connectors between them; a ``Viewport`` is a window onto
that plane (a centre point and a scale). ``render`` turns the two into a
``workflow_viz.Canvas`` — the same char grid + style tags + hit regions the
HWO/HWG viewers already paint into, so the interactive shell around it is
the one that already exists.

Two things here are worth stating, because they are the difference between
a canvas that scrolls and a canvas you can read:

**Zoom is semantic, not geometric.** A geometric zoom scales the picture:
at 30% every label is one unreadable pixel and at 300% you see four boxes
and no context. Here the *scale decides what a shape is drawn as* — a glyph,
a filled block, a framed box with a name, or a framed box with a name and
its detail lines — and each shape picks its own level from its own on-screen
size. Zooming out is therefore not "the same picture, smaller"; it is a
coarser picture that still says something.

**A child is drawn only when its parent is readable.** Without that rule a
zoomed-out view paints all 600 nodes of a codebase as confetti. With it, the
depth you see is the depth you zoomed to: packages, then modules, then
classes, then functions — SHriMP's nested-graph rule, which is what makes
"scroll the wheel and go deeper" mean anything.

Terminal cells are about twice as tall as they are wide, so the vertical
scale is ``scale * CELL_ASPECT``. World units are therefore square: a square
in world space looks square on screen, which matters as soon as anything
lays out boxes in a grid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

import workflow_viz


# A terminal cell is roughly twice as tall as it is wide.
CELL_ASPECT = 0.5

# Level of detail, coarse to fine. The thresholds are in screen cells and
# are measured against the shape's own painted size.
LOD_HIDDEN = 0      # too small to say anything: not drawn
LOD_GLYPH = 1       # a single mark: "something is here"
LOD_BLOCK = 2       # a solid block: shape and size, no name
LOD_FRAME = 3       # a frame with a name
LOD_DETAIL = 4      # a frame with a name and its detail lines

_GLYPH_MIN_W, _GLYPH_MIN_H = 1, 1
_BLOCK_MIN_W, _BLOCK_MIN_H = 3, 2
_FRAME_MIN_W, _FRAME_MIN_H = 8, 3
_DETAIL_MIN_W, _DETAIL_MIN_H = 22, 6
_CHIP_MIN_W = 5      # narrowest box that can still carry a name across its top

MIN_SCALE = 0.004
MAX_SCALE = 8.0
ZOOM_STEP = 1.3          # one wheel notch / one +- press

# How many unselected dependencies may be drawn in one frame (see
# _paint_connectors: past this the picture stops being readable).
_EDGE_BUDGET = 60


# ── model ─────────────────────────────────────────────────────────────────

@dataclass
class Shape:
    """One thing on the plane, in world units.

    ``parent`` is what makes zoom semantic: a shape whose parent is not yet
    drawn at LOD_FRAME is not drawn at all.
    """

    id: str
    x: float
    y: float
    w: float = 0.0
    h: float = 0.0
    kind: str = "box"           # box | round | diamond | text | dot | line
    label: str = ""
    style: str = ""             # prompt_toolkit style class suffix
    detail: list[str] = field(default_factory=list)
    parent: Optional[str] = None
    depth: int = 0
    points: Optional[list] = None   # world-coordinate path, for kind="line"
    meta: dict = field(default_factory=dict)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


@dataclass
class Connector:
    """A dependency drawn between two shapes (by id, not by coordinate)."""

    src: str
    dst: str
    kind: str = ""
    weight: int = 1
    style: str = ""


@dataclass
class Scene:
    title: str = ""
    shapes: list[Shape] = field(default_factory=list)
    connectors: list[Connector] = field(default_factory=list)
    subtitle: str = ""

    def index(self) -> dict[str, Shape]:
        return {s.id: s for s in self.shapes}

    def bounds(self) -> tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y) over every shape; padded when empty."""
        if not self.shapes:
            return (0.0, 0.0, 1.0, 1.0)
        xs0 = min(s.x for s in self.shapes)
        ys0 = min(s.y for s in self.shapes)
        xs1 = max(s.x + s.w for s in self.shapes)
        ys1 = max(s.y + s.h for s in self.shapes)
        if xs1 - xs0 < 1e-6:
            xs1 = xs0 + 1.0
        if ys1 - ys0 < 1e-6:
            ys1 = ys0 + 1.0
        return (xs0, ys0, xs1, ys1)


def scene_from_json(data: dict) -> Scene:
    """Load the shared scene contract."""
    shapes = [
        Shape(
            id=str(d["id"]),
            x=float(d.get("x", 0.0)), y=float(d.get("y", 0.0)),
            w=float(d.get("w", 0.0)), h=float(d.get("h", 0.0)),
            kind=str(d.get("kind", "box")),
            label=str(d.get("label", "")),
            style=str(d.get("style", "")),
            detail=[str(t) for t in (d.get("detail") or [])],
            parent=(str(d["parent"]) if d.get("parent") else None),
            depth=int(d.get("depth", 0)),
            points=(list(d["points"]) if isinstance(d.get("points"), list)
                    else None),
            meta=dict(d.get("meta") or {}),
        )
        for d in (data.get("shapes") or [])
    ]
    conns = [
        Connector(src=str(c["src"]), dst=str(c["dst"]),
                  kind=str(c.get("kind", "")),
                  weight=int(c.get("weight", 1)),
                  style=str(c.get("style", "")))
        for c in (data.get("connectors") or data.get("edges") or [])
        if c.get("src") and c.get("dst")
    ]
    return Scene(title=str(data.get("title", "")),
                 subtitle=str(data.get("subtitle", "")),
                 shapes=shapes, connectors=conns)


# ── viewport ──────────────────────────────────────────────────────────────

@dataclass
class Viewport:
    """A window onto the plane: what is at the centre, and how magnified."""

    cx: float = 0.0
    cy: float = 0.0
    scale: float = 1.0          # screen columns per world unit
    width: int = 80
    height: int = 24

    # ---- transforms ----

    def to_screen(self, wx: float, wy: float) -> tuple[int, int]:
        col = (wx - self.cx) * self.scale + self.width / 2.0
        row = (wy - self.cy) * self.scale * CELL_ASPECT + self.height / 2.0
        return (int(math.floor(col)), int(math.floor(row)))

    def to_world(self, col: float, row: float) -> tuple[float, float]:
        wx = (col + 0.5 - self.width / 2.0) / self.scale + self.cx
        wy = ((row + 0.5 - self.height / 2.0)
              / (self.scale * CELL_ASPECT) + self.cy)
        return (wx, wy)

    # ---- movement ----

    def pan_cells(self, dcol: float, drow: float) -> None:
        self.cx += dcol / self.scale
        self.cy += drow / (self.scale * CELL_ASPECT)

    def zoom_at(self, factor: float, col: float, row: float) -> None:
        """Zoom keeping the world point under (col, row) pinned there.

        Pinning is what makes a wheel usable: without it every notch drags
        the thing you were pointing at off the screen and zooming becomes a
        two-handed operation (zoom, then hunt).
        """
        wx, wy = self.to_world(col, row)
        new_scale = max(MIN_SCALE, min(MAX_SCALE, self.scale * factor))
        if abs(new_scale - self.scale) < 1e-12:
            return
        self.scale = new_scale
        # solve for the centre that puts (wx, wy) back under the cursor
        self.cx = wx - (col + 0.5 - self.width / 2.0) / self.scale
        self.cy = wy - ((row + 0.5 - self.height / 2.0)
                        / (self.scale * CELL_ASPECT))

    def zoom_center(self, factor: float) -> None:
        self.zoom_at(factor, self.width / 2.0 - 0.5, self.height / 2.0 - 0.5)

    def fit(self, bounds: tuple[float, float, float, float],
            margin: float = 0.06) -> None:
        """Frame the given world rectangle."""
        x0, y0, x1, y1 = bounds
        w = max(1e-6, x1 - x0)
        h = max(1e-6, y1 - y0)
        pad = 1.0 + max(0.0, margin) * 2
        sx = self.width / (w * pad)
        sy = self.height / (h * CELL_ASPECT * pad)
        self.scale = max(MIN_SCALE, min(MAX_SCALE, min(sx, sy)))
        self.cx = (x0 + x1) / 2.0
        self.cy = (y0 + y1) / 2.0

    def focus(self, shape: Shape, scale: Optional[float] = None) -> None:
        self.cx, self.cy = shape.cx, shape.cy
        if scale is not None:
            self.scale = max(MIN_SCALE, min(MAX_SCALE, scale))


# ── level of detail ───────────────────────────────────────────────────────

def lod_for(cols: int, rows: int) -> int:
    if cols < _GLYPH_MIN_W or rows < _GLYPH_MIN_H:
        return LOD_HIDDEN
    if cols < _BLOCK_MIN_W or rows < _BLOCK_MIN_H:
        return LOD_GLYPH
    if cols < _FRAME_MIN_W or rows < _FRAME_MIN_H:
        return LOD_BLOCK
    if cols < _DETAIL_MIN_W or rows < _DETAIL_MIN_H:
        return LOD_FRAME
    return LOD_DETAIL


# ── rendering ─────────────────────────────────────────────────────────────

_FRAMES = {
    "box":     ("┌", "┐", "└", "┘", "─", "│"),
    "round":   ("╭", "╮", "╰", "╯", "─", "│"),
    "diamond": ("╱", "╲", "╲", "╱", "─", "│"),
}
_GLYPH = {"box": "▪", "round": "●", "diamond": "◆", "dot": "·", "text": "≡"}
_BLOCK = "░"   # a light fill: "something is here", without shouting


def _cwidth(text: str) -> int:
    try:
        from prompt_toolkit.utils import get_cwidth
        return get_cwidth(text)
    except Exception:                                  # pragma: no cover
        return len(text)


def _fit(text: str, width: int) -> str:
    """Clamp to `width` screen cells, CJK-safe."""
    text = str(text or "")
    if width <= 0:
        return ""
    if _cwidth(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        cw = max(0, _cwidth(ch))
        if used + cw > max(0, width - 1):
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


@dataclass
class Rendered:
    """One painted frame: the grid, plus what is where."""

    canvas: workflow_viz.Canvas
    hits: dict[int, list[tuple[int, int, str, int]]]   # row -> (c0,c1,id,depth)
    lods: dict[str, int]
    visible: list[str]

    def at(self, row: int, col: int) -> Optional[str]:
        """Deepest shape covering the cell (children win over their parent)."""
        best, best_depth = None, -1
        for c0, c1, sid, depth in self.hits.get(row, ()):
            if c0 <= col <= c1 and depth >= best_depth:
                best, best_depth = sid, depth
        return best


def render(scene: Scene, vp: Viewport, *,
           selected: Optional[str] = None,
           show_connectors: bool = True,
           highlight: Optional[Iterable[str]] = None) -> Rendered:
    canvas = workflow_viz.Canvas(width=max(1, vp.width), height=max(1, vp.height))
    hits: dict[int, list[tuple[int, int, str, int]]] = {}
    lods: dict[str, int] = {}
    boxes: dict[str, tuple[int, int, int, int]] = {}    # id -> c0,r0,c1,r1
    visible: list[str] = []
    hi = set(highlight or ())
    # A container's interior is where its children are drawn, so its own
    # detail lines are written only when it has none.
    containers = {s.parent for s in scene.shapes if s.parent}

    for shape in sorted(scene.shapes, key=lambda s: (s.depth, s.id)):
        # a child is only drawn once its parent is readable
        if shape.parent is not None:
            if lods.get(shape.parent, LOD_HIDDEN) < LOD_FRAME:
                lods[shape.id] = LOD_HIDDEN
                continue

        c0, r0 = vp.to_screen(shape.x, shape.y)
        c1, r1 = vp.to_screen(shape.x + shape.w, shape.y + shape.h)
        cols, rows = c1 - c0, r1 - r0
        if shape.kind == "line":
            # A line's bounding box is flat whenever it is horizontal or
            # vertical, and a flat box measures as "too small to draw". Lines
            # are sized by their span, not by their box.
            lod = LOD_FRAME if max(abs(cols), abs(rows)) >= 1 else LOD_GLYPH
        else:
            lod = lod_for(cols, rows)
        lods[shape.id] = lod
        if lod == LOD_HIDDEN:
            continue
        if c1 < 0 or r1 < 0 or c0 >= vp.width or r0 >= vp.height:
            continue                                    # offscreen

        # Projected here, where the viewport is known, and passed to the
        # painter rather than stashed on the shape: a scene is rendered once
        # per frame and often at more than one scale, and a cached path on a
        # shared object is a frame drawn with the last viewport's numbers.
        screen_path = ([vp.to_screen(px, py) for px, py in shape.points]
                       if shape.points else None)
        boxes[shape.id] = (c0, r0, c1, r1)
        visible.append(shape.id)
        style = _style_for(shape, selected, hi)
        _paint(canvas, shape, lod, c0, r0, c1, r1, style,
               with_detail=shape.id not in containers,
               screen_path=screen_path)
        for row in range(max(0, r0), min(vp.height, max(r0 + 1, r1))):
            hits.setdefault(row, []).append(
                (max(0, c0), min(vp.width - 1, max(c0, c1 - 1)),
                 shape.id, shape.depth))

    if show_connectors:
        _paint_connectors(canvas, scene, boxes, lods, selected, hi)

    return Rendered(canvas=canvas, hits=hits, lods=lods, visible=visible)


def _style_for(shape: Shape, selected: Optional[str], hi: set) -> str:
    if shape.id == selected:
        return "class:cv.sel"
    if shape.id in hi:
        return "class:cv.hi"
    return f"class:cv.{shape.style or 'node'}"


def _paint(canvas, shape: Shape, lod: int,
           c0: int, r0: int, c1: int, r1: int, style: str,
           *, with_detail: bool = True,
           screen_path: Optional[list] = None) -> None:
    if shape.kind == "line":
        # Clamp before walking: a line whose far end is a million cells away
        # (deep zoom) would otherwise cost a million steps to paint nothing.
        lo_c, hi_c = -canvas.width, canvas.width * 2
        lo_r, hi_r = -canvas.height, canvas.height * 2

        def clamp(c: int, r: int) -> tuple[int, int]:
            return (max(lo_c, min(hi_c, c)), max(lo_r, min(hi_r, r)))

        # A path is drawn segment by segment. Drawing only its bounding box's
        # diagonal would turn every curve on a board into the same straight
        # line — the box of a sine wave is the box of a diagonal.
        span = screen_path or [(c0, r0), (c1, r1)]
        for (sa, ra), (sb, rb) in zip(span, span[1:]):
            sa, ra = clamp(sa, ra)
            sb, rb = clamp(sb, rb)
            for col, row, glyph in _segment(sa, ra, sb, rb):
                canvas.put(row, col, glyph, style)
        if shape.label and lod >= LOD_FRAME:
            canvas.put(r0, c0, _fit(shape.label, 16), style)
        return

    if lod == LOD_GLYPH:
        canvas.put_force(r0, c0, _GLYPH.get(shape.kind, "▪"), style)
        return

    if shape.kind == "text":
        canvas.put_force(r0, c0, _fit(shape.label, max(0, c1 - c0)), style)
        return

    if lod == LOD_BLOCK:
        for row in range(max(0, r0), min(canvas.height, r1)):
            canvas.put_force(row, max(0, c0), _BLOCK * max(0, c1 - c0), style)
        # A name still fits across the top even when the box is too small for
        # a frame, and an overview of unnamed rectangles is not an overview.
        # This is the same concession the web view makes with its name chips.
        label = _fit(shape.label, max(0, c1 - c0))
        if label and c1 - c0 >= _CHIP_MIN_W:
            canvas.put_force(r0, max(0, c0), label, style)
        return

    tl, tr, bl, br, hz, vt = _FRAMES.get(shape.kind, _FRAMES["box"])
    width = max(2, c1 - c0)
    inner = width - 2

    top = tl + hz * inner + tr
    bot = bl + hz * inner + br
    canvas.put_force(r0, c0, top, style)
    canvas.put_force(r1 - 1, c0, bot, style)
    for row in range(r0 + 1, r1 - 1):
        canvas.put_force(row, c0, vt, style)
        canvas.put_force(row, c1 - 1, vt, style)
        # clear the interior so a parent's fill never bleeds into a child.
        # Clamped first: a box wider than the screen starts off the left edge,
        # and Canvas.blank indexes its row directly.
        start = max(0, c0 + 1)
        span = min(inner - (start - (c0 + 1)), canvas.width - start)
        if span > 0:
            canvas.blank(row, start, span)

    # the name rides on the top border, the way a labelled frame reads
    label = _fit(shape.label, max(0, inner - 2))
    if label:
        canvas.put_force(r0, c0 + 1, " " + label + " ", style)

    if lod >= LOD_DETAIL and with_detail and shape.detail:
        room = (r1 - 1) - (r0 + 1)
        for i, line in enumerate(shape.detail[:max(0, room)]):
            canvas.put_force(r0 + 1 + i, c0 + 2,
                             _fit(line, max(0, inner - 2)), "class:cv.detail")


# ── connectors ────────────────────────────────────────────────────────────

def _paint_connectors(canvas, scene: Scene, boxes: dict, lods: dict,
                      selected: Optional[str], hi: set) -> None:
    """Draw a dependency once both ends are on screen.

    A connector between two shapes that are collapsed into their parents is
    not dropped — it is re-attached to the visible ancestor, so zooming out
    turns twelve call edges into one thick package-level arrow instead of
    into nothing.
    """
    index = scene.index()
    lifted: dict[tuple[str, str], int] = {}
    for conn in scene.connectors:
        pair = _lift(conn.src, conn.dst, index, boxes)
        if pair is None:
            continue
        lifted[pair] = lifted.get(pair, 0) + max(1, conn.weight)

    # Dependencies are only worth drawing between things you can read, and
    # only while there are few enough of them to follow. Zoomed out, 500
    # arrows over 30 rows is not information — it is a grey wash that hides
    # the boxes underneath. Anything touching the selection is drawn anyway:
    # that is the one set of edges the reader is actually asking about.
    ranked = sorted(lifted.items(), key=lambda kv: (-kv[1], kv[0]))
    budget = _EDGE_BUDGET
    for (src, dst), weight in ranked:
        touched = selected in (src, dst) or src in hi or dst in hi
        readable = (lods.get(src, LOD_HIDDEN) >= LOD_FRAME
                    and lods.get(dst, LOD_HIDDEN) >= LOD_FRAME)
        if not touched:
            if not readable or budget <= 0:
                continue
            budget -= 1
        style = "class:cv.edge.hi" if touched else "class:cv.edge"
        _draw_arrow(canvas, boxes[src], boxes[dst], style, weight)


def _lift(src_id: str, dst_id: str, index: dict,
          boxes: dict) -> Optional[tuple[str, str]]:
    """Raise one dependency to the level where it can be drawn as one line.

    Both ends climb to the children of their nearest common ancestor, so a
    call from a method of one class into a method of another is drawn between
    the two *classes* while you are looking at classes, and only splits into
    the real method-to-method arrow once both methods are on screen. Drawing
    it at its literal endpoints instead is what turns a zoomed-out map into a
    ball of string across the whole canvas.
    """
    src_chain = _chain(src_id, index)
    dst_chain = _chain(dst_id, index)
    common = set(src_chain) & set(dst_chain)
    src = next((n for n in src_chain if n not in common), None)
    dst = next((n for n in dst_chain if n not in common), None)
    if src is None or dst is None:
        return None                                # one contains the other
    src = _visible_ancestor(src, index, boxes)
    dst = _visible_ancestor(dst, index, boxes)
    if not src or not dst or src == dst:
        return None
    return (src, dst)


def _chain(shape_id: str, index: dict) -> list[str]:
    """[shape, parent, grandparent, ...] — the shape's own ancestry."""
    out, seen, cur = [], set(), shape_id
    while cur and cur not in seen:
        out.append(cur)
        seen.add(cur)
        shape = index.get(cur)
        cur = shape.parent if shape else None
    return out


def _visible_ancestor(shape_id: str, index: dict, boxes: dict) -> Optional[str]:
    seen = set()
    cur = shape_id
    while cur and cur not in seen:
        if cur in boxes:
            return cur
        seen.add(cur)
        shape = index.get(cur)
        cur = shape.parent if shape else None
    return None


def _draw_arrow(canvas, a: tuple, b: tuple, style: str, weight: int) -> None:
    ax = (a[0] + a[2]) // 2
    ay = (a[1] + a[3]) // 2
    bx = (b[0] + b[2]) // 2
    by = (b[1] + b[3]) // 2
    for col, row, glyph in _segment(ax, ay, bx, by):
        if _inside(a, col, row) or _inside(b, col, row):
            continue                                   # never overdraw a box
        canvas.put(row, col, glyph, style)
    head = "▶" if bx > ax else ("◀" if bx < ax else ("▼" if by > ay else "▲"))
    hx, hy = _edge_point(b, ax, ay)
    canvas.put(hy, hx, head, style)


def _inside(box: tuple, col: int, row: int) -> bool:
    c0, r0, c1, r1 = box
    return c0 <= col < c1 and r0 <= row < r1


def _edge_point(box: tuple, fx: int, fy: int) -> tuple[int, int]:
    """Where the arrow touches the destination box, coming from (fx, fy)."""
    c0, r0, c1, r1 = box
    cx, cy = (c0 + c1) // 2, (r0 + r1) // 2
    if abs(fx - cx) >= abs(fy - cy):
        return (c0 - 1 if fx < cx else c1, cy)
    return (cx, r0 - 1 if fy < cy else r1)


def _segment(x0: int, y0: int, x1: int, y1: int):
    """Bresenham with a glyph per step, chosen from the local direction."""
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    guard = 0
    while guard < 10000:
        guard += 1
        e2 = 2 * err
        step_x = e2 > -dy
        step_y = e2 < dx
        if step_x and step_y:
            glyph = "╲" if sx == sy else "╱"
        elif step_x:
            glyph = "─"
        else:
            glyph = "│"
        yield (x, y, glyph)
        if x == x1 and y == y1:
            return
        if step_x:
            err -= dy
            x += sx
        if step_y:
            err += dx
            y += sy


# ── search ────────────────────────────────────────────────────────────────

def search(scene: Scene, needle: str, limit: int = 50) -> list[Shape]:
    """Case-insensitive label/id match, shallowest first (most useful first)."""
    needle = (needle or "").strip().lower()
    if not needle:
        return []
    hits = [s for s in scene.shapes
            if needle in s.label.lower() or needle in s.id.lower()]
    hits.sort(key=lambda s: (s.depth, len(s.label), s.id))
    return hits[:limit]
