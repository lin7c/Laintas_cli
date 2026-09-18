"""Drawing on a board from the terminal.

``canvas.py`` reads boards; this writes them. It is a separate module because
writing is where the hazards are, and they are worth naming:

**Excalidraw elements are a real schema, not a bag of coordinates.** The
browser builds them with `convertToExcalidrawElements`, which fills in thirty
fields nobody thinks about. There is no such helper here, so the factories
below produce complete elements — a shape missing `seed` or `versionNonce`
renders, then behaves strangely the moment somebody edits or undoes it.

**A deletion is a tombstone.** Dropping the entry lets the element come back
the next time an older copy of the scene is reconciled in. Same rule, same
reason, as Helpwo's `canvasScene.ts`.

**Nobody owns the file.** Helpwo keeps an open board in its editor and writes
the file behind it; this writes the file directly. Neither can lock out the
other, so every write here goes through ``canvas.write_scene(expect_mtime=)``
— the board changing underneath turns into a refusal instead of into somebody
losing what they just drew. That is also why this module keeps the whole
element list in memory rather than patching the file in place: a refusal has
to leave the board exactly as it was.

The geometry is in Excalidraw's own coordinates (its pixels), which is what
``canvas.to_canvas_scene`` already uses as world units — so a rectangle drawn
across ten terminal cells lands where the terminal drew it.
"""

from __future__ import annotations

import random
import secrets
import time
from typing import Any, Optional

# Excalidraw's defaults for a hand-drawn shape, as of 0.18. Anything not
# listed here is per-type and lives in the factories below.
_COMMON = {
    "angle": 0,
    "strokeColor": "#1e1e1e",
    "backgroundColor": "transparent",
    "fillStyle": "solid",
    "strokeWidth": 2,
    "strokeStyle": "solid",
    "roughness": 1,
    "opacity": 100,
    "groupIds": [],
    "frameId": None,
    "boundElements": None,
    "link": None,
    "locked": False,
    "isDeleted": False,
}

SHAPE_TYPES = ("rectangle", "ellipse", "diamond")
STROKE_TYPES = ("line", "freedraw")
# Excalidraw's own palette, so a board drawn here and a board drawn there use
# the same colours rather than two nearly-identical sets.
STROKE_COLORS = ("#1e1e1e", "#e03131", "#2f9e44", "#1971c2", "#f08c00",
                 "#9c36b5")
FILL_COLORS = ("transparent", "#ffc9c9", "#b2f2bb", "#a5d8ff", "#ffec99",
               "#eebefa")
FILL_STYLES = ("solid", "hachure", "cross-hatch")
STROKE_WIDTHS = (1, 2, 4)
MIN_SIZE = 8.0          # world units; below this a stray click is not a shape


def _nonce() -> int:
    return random.randint(0, 2 ** 31 - 1)


def _now() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str = "t") -> str:
    """An id that cannot collide with Excalidraw's own or with another CLI.

    64 bits of randomness, not 16: a millisecond timestamp plus four hex
    digits collides inside a single fast loop (the birthday bound over 65536
    is a few hundred draws), and two elements sharing an id is a corrupt
    board, not a cosmetic problem.
    """
    return f"{prefix}{_now():x}{secrets.token_hex(8)}"


# Who the factories attribute new elements to. The person drawing in the
# terminal is "cli"; an agent drawing on their behalf is "ai" plus a turn id,
# which is what Helpwo's editor groups its Show / Keep / Undo banner by. Get
# this wrong and work the model did lands on someone's board with no way to
# review it — the banner counts `author == "ai"` and nothing else.
_AUTHOR: dict = {"author": "cli"}


def authorship(author: str = "cli", turn: str = "") -> dict:
    return {"author": author, **({"turn": turn} if turn else {})}


def _style(spec: Optional[dict]) -> dict:
    """The drawing attributes any element may carry.

    Free drawing is mostly this: the same handful of shapes in different
    colours, weights and fills. Unknown values are dropped rather than passed
    through — Excalidraw silently renders an element with a nonsense
    `fillStyle` as if nothing were wrong, which is worse than being told.
    """
    spec = spec or {}
    out: dict = {}
    color = str(spec.get("color") or "").strip()
    if color:
        out["strokeColor"] = color
    background = str(spec.get("background") or "").strip()
    if background:
        out["backgroundColor"] = background
    fill = str(spec.get("fill") or "").strip()
    if fill in FILL_STYLES:
        out["fillStyle"] = fill
    width = spec.get("strokeWidth")
    if isinstance(width, (int, float)) and width > 0:
        out["strokeWidth"] = float(width)
    dash = str(spec.get("strokeStyle") or "").strip()
    if dash in ("solid", "dashed", "dotted"):
        out["strokeStyle"] = dash
    opacity = spec.get("opacity")
    if isinstance(opacity, (int, float)) and 0 <= opacity <= 100:
        out["opacity"] = float(opacity)
    if spec.get("sloppy") is not None:
        out["roughness"] = 2 if spec.get("sloppy") else 0
    return out


def _base(kind: str, x: float, y: float, w: float, h: float,
          **extra: Any) -> dict:
    element = {
        "id": new_id(),
        "type": kind,
        "x": round(float(x), 2),
        "y": round(float(y), 2),
        "width": round(float(w), 2),
        "height": round(float(h), 2),
        "seed": _nonce(),
        "version": 1,
        "versionNonce": _nonce(),
        "updated": _now(),
        "customData": dict(_AUTHOR),
    }
    element.update(_COMMON)
    element.update(extra)
    return element


# ── factories ─────────────────────────────────────────────────────────────

def shape(kind: str, x: float, y: float, w: float, h: float,
          style: Optional[dict] = None) -> dict:
    if kind not in SHAPE_TYPES:
        raise ValueError(f"not a shape type: {kind}")
    # Negative extents happen every time somebody drags up and to the left.
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    roundness = {"type": 3} if kind == "rectangle" else {"type": 2}
    return _base(kind, x, y, max(MIN_SIZE, w), max(MIN_SIZE, h),
                 roundness=roundness, **_style(style))


TEXT_PADDING = 5.0      # Excalidraw's own padding inside a labelled shape


def text(content: str, x: float, y: float, *,
         container_id: Optional[str] = None, font_size: int = 20,
         width: Optional[float] = None, height: Optional[float] = None,
         style: Optional[dict] = None) -> dict:
    lines = str(content).split("\n")
    if width is None:
        width = max(10.0, max(len(line) for line in lines) * font_size * 0.55)
    if height is None:
        height = max(font_size * 1.25, len(lines) * font_size * 1.25)
    return _base(
        "text", x, y, width, height,
        text=str(content),
        originalText=str(content),
        fontSize=font_size,
        fontFamily=1,
        textAlign="center" if container_id else "left",
        verticalAlign="middle" if container_id else "top",
        containerId=container_id,
        lineHeight=1.25,
        autoResize=True,
        roundness=None,
        **_style(style),
    )


def arrow(x: float, y: float, dx: float, dy: float, *,
          start_id: Optional[str] = None, end_id: Optional[str] = None,
          style: Optional[dict] = None) -> dict:
    """An arrow between two points, optionally bound to shapes at each end.

    The skeleton carries its own x/y/width/height: a binding attaches an arrow
    to a shape, it does not place it. Bound arrows built without geometry all
    end up drawn in the top-left corner of the board — visible only once it is
    rendered, which is how this was found the first time.
    """
    element = _base(
        "arrow", x, y, abs(dx), abs(dy),
        points=[[0, 0], [round(dx, 2), round(dy, 2)]],
        lastCommittedPoint=None,
        startBinding=({"elementId": start_id, "focus": 0, "gap": 4}
                      if start_id else None),
        endBinding=({"elementId": end_id, "focus": 0, "gap": 4}
                    if end_id else None),
        startArrowhead=None,
        endArrowhead="arrow",
        elbowed=False,
        roundness={"type": 2},
        **_style(style),
    )
    return element


def stroke(kind: str, points: list, style: Optional[dict] = None) -> dict:
    """A line or a freehand stroke through a list of absolute points.

    This is what "draw anything" needs and boxes-and-arrows does not: the
    element is defined by its path, so the same factory serves a straight
    segment, a polyline and a pencil scribble. Excalidraw stores the path
    relative to the element's own origin, with the bounding box as its size —
    absolute points in `points` render as an element far away from the line
    it is supposed to be, which is the same class of mistake as an unplaced
    bound arrow.
    """
    if kind not in STROKE_TYPES:
        raise ValueError(f"not a stroke type: {kind}")
    pairs = [(float(px), float(py)) for px, py in points]
    if len(pairs) < 2:
        raise ValueError("a stroke needs at least two points")
    x0 = min(px for px, _ in pairs)
    y0 = min(py for _, py in pairs)
    ox, oy = pairs[0]
    width = max(px for px, _ in pairs) - x0
    height = max(py for _, py in pairs) - y0
    relative = [[round(px - ox, 2), round(py - oy, 2)] for px, py in pairs]

    extra: dict = {"points": relative, "lastCommittedPoint": None}
    if kind == "line":
        extra.update(roundness={"type": 2}, startArrowhead=None,
                     endArrowhead=None, startBinding=None, endBinding=None)
    else:
        # A pencil stroke is not smoothed into a curve, and it carries a
        # pressure array Excalidraw expects to exist even when empty.
        extra.update(roundness=None, pressures=[], simulatePressure=True)
    return _base(kind, ox, oy, width, height, **extra, **_style(style))


def edge_point(box: dict, toward: dict, gap: float = 4.0) -> tuple[float, float]:
    """Where an arrow leaves `box` on its way to `toward`.

    The editor clips a bound arrow to its shape as you drag it; a file loaded
    from disk keeps the endpoints it was given. Starting one at the centre
    therefore draws it straight across the shape it comes from — which is what
    the first rendered version did.
    """
    cx = box.get("x", 0) + box.get("width", 0) / 2
    cy = box.get("y", 0) + box.get("height", 0) / 2
    tx = toward.get("x", 0) + toward.get("width", 0) / 2
    ty = toward.get("y", 0) + toward.get("height", 0) / 2
    dx, dy = tx - cx, ty - cy
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return (cx, cy)
    half_w = max(box.get("width", 0) / 2, 1e-6)
    half_h = max(box.get("height", 0) / 2, 1e-6)
    scale = min(half_w / abs(dx) if dx else float("inf"),
                half_h / abs(dy) if dy else float("inf"))
    length = (dx * dx + dy * dy) ** 0.5
    return (cx + dx * scale + dx / length * gap,
            cy + dy * scale + dy / length * gap)


# ── operations on an element list ─────────────────────────────────────────

def add(elements: list, *new: dict) -> list:
    return list(elements) + [dict(el) for el in new]


def bind(elements: list, container_id: str, child_id: str,
         child_type: str) -> list:
    """Record a child (a label, an arrow end) on its container.

    Excalidraw reads the relationship from both sides. Writing only the
    child's `containerId` produces a label that renders once and then jumps
    out of its box the first time the box is moved.
    """
    out = []
    for el in elements:
        if el.get("id") != container_id:
            out.append(el)
            continue
        bound = list(el.get("boundElements") or [])
        if not any(b.get("id") == child_id for b in bound):
            bound.append({"id": child_id, "type": child_type})
        out.append(_bump(el, {"boundElements": bound}))
    return out


def delete(elements: list, element_id: str) -> list:
    """Tombstone, never removal (see the module docstring)."""
    out = []
    for el in elements:
        if el.get("id") == element_id:
            out.append(_bump(el, {"isDeleted": True}))
            # A container's label goes with it; leaving it behind is a caption
            # floating over nothing.
            continue
        if el.get("containerId") == element_id:
            out.append(_bump(el, {"isDeleted": True}))
            continue
        out.append(el)
    return out


def move(elements: list, element_id: str, dx: float, dy: float) -> list:
    out = []
    for el in elements:
        if el.get("id") != element_id:
            out.append(el)
            continue
        out.append(_bump(el, {"x": round(el.get("x", 0) + dx, 2),
                              "y": round(el.get("y", 0) + dy, 2)}))
    return out


def label(elements: list, container_id: str, content: str,
          author: Optional[dict] = None) -> list:
    """Put text on a shape: replace its existing label, or bind a new one."""
    container = next((el for el in elements
                      if el.get("id") == container_id), None)
    if container is None:
        return elements
    existing = next((el for el in elements
                     if el.get("containerId") == container_id
                     and el.get("type") == "text"
                     and not el.get("isDeleted")), None)
    if existing is not None:
        return [_bump(el, {"text": content, "originalText": content})
                if el is existing else el for el in elements]
    x, y, width, height = _label_box(container, content)
    child = text(content, x, y, container_id=container_id,
                 width=width, height=height)
    if author:
        child["customData"] = dict(author)
    return bind(add(elements, child), container_id, child["id"], "text")


def _label_box(container: dict, content: str,
               font_size: int = 20) -> tuple[float, float, float, float]:
    """Where a bound label sits on its container.

    Excalidraw lays a label out itself while you type, but nothing re-lays it
    out when a file is opened — so a label written at the container's corner
    is drawn at the corner, hanging over the edge. Both cases here were seen
    by rendering the file, not by reading the schema:

    * inside a shape, the label is centred and kept within the shape's width;
    * on an arrow, it is centred on the arrow's midpoint and keeps its own
      natural width. Fitting it to the arrow's bounding box instead squashes
      "autosaves" into the 80 pixels between two boxes, where it covers the
      arrowhead and the line it is labelling.
    """
    lines = str(content).split("\n")
    height = max(font_size * 1.25, len(lines) * font_size * 1.25)
    natural = max(10.0, max(len(line) for line in lines) * font_size * 0.55)
    mid_x = container.get("x", 0) + container.get("width", 0) / 2
    mid_y = container.get("y", 0) + container.get("height", 0) / 2

    if container.get("type") in ("arrow", "line"):
        return (mid_x - natural / 2, mid_y - height / 2, natural, height)

    width = max(10.0, container.get("width", 0) - TEXT_PADDING * 2)
    return (container.get("x", 0) + TEXT_PADDING, mid_y - height / 2,
            width, height)


def _bump(el: dict, patch: dict) -> dict:
    """Apply a patch and make this version win over any older copy."""
    out = dict(el)
    out.update(patch)
    out["version"] = int(el.get("version") or 1) + 1
    out["versionNonce"] = _nonce()
    out["updated"] = _now()
    return out


# ── laying out a batch ────────────────────────────────────────────────────

BOX_W, BOX_H = 220.0, 110.0
# Wide enough that a labelled arrow has somewhere to put its label.
GAP_X, GAP_Y = 160.0, 90.0
PER_ROW = 3


def content_bottom(elements: list) -> float:
    """The lowest point anything live occupies, or 0 for an empty board."""
    live = [el for el in elements if not el.get("isDeleted")]
    if not live:
        return 0.0
    return max(el.get("y", 0) + el.get("height", 0) for el in live)


def autoplace(shapes: list, below: float = 0.0) -> list:
    """Fill in geometry for shapes that came without any.

    A caller describing a diagram ("three boxes and an arrow") should not have
    to invent pixel coordinates, and one that does give coordinates should
    keep them exactly. New rows start below whatever is already on the board,
    so drawing twice does not stack the second diagram on top of the first.
    """
    top = below + (GAP_Y if below else 0.0)
    out = []
    for i, spec in enumerate(shapes):
        spec = dict(spec)
        if spec.get("kind") in STROKE_TYPES:
            # A stroke is defined by its own points; there is nothing to lay
            # out, and moving it into a grid row would move the drawing.
            out.append(spec)
            continue
        width = float(spec.get("width") or BOX_W)
        height = float(spec.get("height") or BOX_H)
        if spec.get("x") is None or spec.get("y") is None:
            column = i % PER_ROW
            row = i // PER_ROW
            if spec.get("x") is None:
                spec["x"] = column * (BOX_W + GAP_X)
            if spec.get("y") is None:
                spec["y"] = top + row * (BOX_H + GAP_Y)
        spec["width"], spec["height"] = width, height
        out.append(spec)
    return out


# ── the board a viewer edits ──────────────────────────────────────────────

class BoardEditor:
    """One open board: its elements, its file, and an undo stack.

    The viewer talks to this and never to the file. Every mutation writes
    immediately — there is no unsaved state to lose to a crash or a closed
    terminal — and every write carries the mtime the elements were read at.

    ``author``/``turn`` stamp what this editor creates. A person drawing in
    the terminal is the author of their own work; an agent is not, and its
    elements carry the tag Helpwo's review banner looks for.
    """

    def __init__(self, path: str, canvas_mod, max_undo: int = 50,
                 author: str = "cli", turn: str = ""):
        self.path = path
        self.canvas = canvas_mod
        self.max_undo = max_undo
        self.author = authorship(author, turn)
        self.scene = canvas_mod.read_scene(path)
        self.mtime = self._mtime()
        self.digest = self._digest()
        self._undo: list[list] = []

    # ---- state ----

    def _mtime(self) -> float:
        import os
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def _digest(self) -> str:
        """What the file looked like when we last agreed with it."""
        getter = getattr(self.canvas, "scene_digest", None)
        return getter(self.path) if getter else ""

    @property
    def elements(self) -> list:
        return self.scene.get("elements") or []

    def reload(self) -> None:
        self.scene = self.canvas.read_scene(self.path)
        self.mtime = self._mtime()
        self.digest = self._digest()
        self._undo.clear()

    # ---- mutation ----

    def apply(self, elements: list) -> tuple[bool, str]:
        """Write a new element list. (ok, message).

        A refusal means somebody else — Helpwo with this board open — wrote
        the file since it was read. The board on disk is theirs and is left
        alone; this reloads so the next edit starts from what is really there.
        """
        previous = list(self.elements)
        scene = dict(self.scene)
        scene["elements"] = elements
        try:
            self._write(scene)
        except self.canvas.CanvasError as e:
            self.reload()
            return (False, f"{e} (reloaded)")
        except OSError as e:
            return (False, str(e))
        self.scene = scene
        self.mtime = self._mtime()
        self.digest = self._digest()
        self._undo.append(previous)
        del self._undo[:-self.max_undo]
        return (True, "")

    def undo(self) -> tuple[bool, str]:
        if not self._undo:
            return (False, "nothing to undo")
        previous = self._undo.pop()
        scene = dict(self.scene)
        scene["elements"] = previous
        try:
            self._write(scene)
        except (self.canvas.CanvasError, OSError) as e:
            self.reload()
            return (False, f"{e}")
        self.scene = scene
        self.mtime = self._mtime()
        self.digest = self._digest()
        return (True, "undone")

    def _write(self, scene: dict) -> None:
        """One write, guarded by both what the file was and when it was."""
        try:
            self.canvas.write_scene(self.path, scene,
                                    expect_mtime=self.mtime,
                                    expect_digest=self.digest)
        except TypeError:            # a canvas module without digest support
            self.canvas.write_scene(self.path, scene, expect_mtime=self.mtime)

    # ---- the operations a viewer offers ----

    def _sign(self, element: dict) -> dict:
        element = dict(element)
        element["customData"] = dict(self.author)
        return element

    def draw_shape(self, kind: str, x: float, y: float, w: float, h: float,
                   style: Optional[dict] = None) -> tuple[bool, str, Optional[str]]:
        element = self._sign(shape(kind, x, y, w, h, style))
        ok, msg = self.apply(add(self.elements, element))
        return (ok, msg, element["id"] if ok else None)

    def draw_stroke(self, kind: str, points: list,
                    style: Optional[dict] = None) -> tuple[bool, str, Optional[str]]:
        element = self._sign(stroke(kind, points, style))
        ok, msg = self.apply(add(self.elements, element))
        return (ok, msg, element["id"] if ok else None)

    def draw_text(self, content: str, x: float,
                  y: float) -> tuple[bool, str, Optional[str]]:
        element = self._sign(text(content, x, y))
        ok, msg = self.apply(add(self.elements, element))
        return (ok, msg, element["id"] if ok else None)

    def draw_arrow(self, src: dict, dst: dict) -> tuple[bool, str, Optional[str]]:
        """An arrow between two elements, from edge to edge."""
        sx, sy = edge_point(src, dst)
        dx, dy = edge_point(dst, src)
        element = self._sign(arrow(sx, sy, dx - sx, dy - sy,
                                   start_id=src.get("id"),
                                   end_id=dst.get("id")))
        elements = add(self.elements, element)
        elements = bind(elements, src["id"], element["id"], "arrow")
        elements = bind(elements, dst["id"], element["id"], "arrow")
        ok, msg = self.apply(elements)
        return (ok, msg, element["id"] if ok else None)

    def set_label(self, element_id: str, content: str) -> tuple[bool, str]:
        return self.apply(label(self.elements, element_id, content,
                                author=self.author))

    def erase(self, element_id: str) -> tuple[bool, str]:
        return self.apply(delete(self.elements, element_id))

    def nudge(self, element_id: str, dx: float, dy: float) -> tuple[bool, str]:
        return self.apply(move(self.elements, element_id, dx, dy))

    def draw_batch(self, shapes: list,
                   connect: Optional[list] = None) -> tuple[bool, str, dict]:
        """A whole diagram in one write.

        Batched deliberately: a caller drawing five boxes one call at a time
        is five chances for Helpwo to save the board in between and have the
        rest refused, leaving half a diagram behind. One write either lands
        whole or does not land.

        `shapes` may name each shape with a local `id`; connections use those
        names, so a caller can describe a diagram without first reading back
        the ids the file gave it.
        """
        elements = list(self.elements)
        placed = autoplace(shapes, below=content_bottom(elements))
        names: dict[str, str] = {}
        made: dict[str, dict] = {}

        for spec in placed:
            kind = str(spec.get("kind") or "rectangle")
            style = spec.get("style") if isinstance(spec.get("style"), dict) else spec
            if kind in STROKE_TYPES:
                element = self._sign(stroke(kind, spec.get("points") or [],
                                            style))
                elements = add(elements, element)
            elif kind == "text":
                element = self._sign(text(str(spec.get("label") or ""),
                                          spec["x"], spec["y"], style=style))
                elements = add(elements, element)
            else:
                element = self._sign(shape(kind, spec["x"], spec["y"],
                                           spec["width"], spec["height"], style))
                elements = add(elements, element)
                if spec.get("label"):
                    elements = label(elements, element["id"],
                                     str(spec["label"]), author=self.author)
            local = str(spec.get("id") or "")
            if local:
                names[local] = element["id"]
            made[element["id"]] = element

        for link in (connect or []):
            src_id = names.get(str(link.get("from")), str(link.get("from")))
            dst_id = names.get(str(link.get("to")), str(link.get("to")))
            src = made.get(src_id) or self._in(elements, src_id)
            dst = made.get(dst_id) or self._in(elements, dst_id)
            if src is None or dst is None:
                continue                     # named something that is not there
            sx, sy = edge_point(src, dst)
            dx, dy = edge_point(dst, src)
            connector = self._sign(arrow(sx, sy, dx - sx, dy - sy,
                                         start_id=src["id"], end_id=dst["id"]))
            elements = add(elements, connector)
            elements = bind(elements, src["id"], connector["id"], "arrow")
            elements = bind(elements, dst["id"], connector["id"], "arrow")
            if link.get("label"):
                elements = label(elements, connector["id"],
                                 str(link["label"]), author=self.author)

        ok, msg = self.apply(elements)
        return (ok, msg, names if ok else {})

    @staticmethod
    def _in(elements: list, element_id: str) -> Optional[dict]:
        return next((el for el in elements
                     if el.get("id") == element_id), None)

    def find(self, element_id: str) -> Optional[dict]:
        return next((el for el in self.elements
                     if el.get("id") == element_id), None)
