"""Whiteboards, from the terminal.

A board is an ordinary `.excalidraw` file — Excalidraw's own JSON. Helpwo draws
it; this reads it, lists them, and creates empty ones, so a person working in a
terminal can see what is on a board without opening a GUI for it.

The rendering rules here mirror Helpwo's `src/utils/canvasScene.ts` — a label
bound to a shape is folded onto its container's line, an arrow reports what it
connects, tombstones are not part of the board. That is a duplicated
implementation in two languages and therefore a place where the two can drift;
it is kept small and rule-for-rule identical on purpose, and the tests on both
sides assert the same three properties. The alternative — one side shelling out
to the other to read a JSON file — would be worse.

What this deliberately does NOT do is change a board that something else may
have open. Helpwo holds an open board's elements in the editor and writes the
file behind them, so a write from here while it is open would be overwritten by
its next autosave, silently. `write_scene` therefore refuses when the file has
changed since it was read, which turns that race into a refusal instead of lost
work.
"""

from __future__ import annotations

import json
import os
from typing import Optional

CANVAS_EXTENSION = ".excalidraw"


class CanvasError(RuntimeError):
    """A failure with a message meant for the operator's screen."""


def is_canvas_path(path: str) -> bool:
    return str(path or "").lower().endswith(CANVAS_EXTENSION)


def empty_scene() -> dict:
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "laintas_cli",
        "elements": [],
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None},
        "files": {},
    }


def read_scene(path: str) -> dict:
    """Load a board. Raises CanvasError with a message a person can act on."""
    path = os.path.expanduser(str(path or "").strip())
    if not path:
        raise CanvasError("a board path is required")
    if not is_canvas_path(path):
        raise CanvasError(f"a board's path must end in {CANVAS_EXTENSION}")
    if not os.path.isfile(path):
        raise CanvasError(f"no board at {path}")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return empty_scene()
    try:
        parsed = json.loads(text)
    except ValueError as e:
        # Never silently substitute an empty board: that is how a session's
        # work disappears behind a "fixed it" message.
        raise CanvasError(f"{path} is not a readable Excalidraw scene ({e})")
    scene = empty_scene()
    scene.update(parsed if isinstance(parsed, dict) else {})
    scene["elements"] = (parsed.get("elements") or []) if isinstance(parsed, dict) else []
    return scene


def scene_digest(path: str) -> str:
    """A fingerprint of the board on disk; "" when there is no file."""
    import hashlib
    path = os.path.expanduser(str(path or "").strip())
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def write_scene(path: str, scene: dict, *, expect_mtime: Optional[float] = None,
                expect_digest: Optional[str] = None) -> None:
    """Save a board, refusing if it changed under us.

    `expect_mtime` is the modification time the caller read the file at, and
    `expect_digest` is what `scene_digest` returned for it. A mismatch in
    either means somebody else — Helpwo with the board open, most likely —
    wrote it in between, and continuing would drop their work without saying
    so.

    Pass the digest when it matters. Modification times are not a reliable
    guard on their own: two writes that land inside the same filesystem
    timestamp tick compare equal, and the check then waves through exactly the
    overwrite it exists to stop. That is not theoretical — it showed up as a
    test that passed twice and failed the third time.
    """
    path = os.path.expanduser(str(path or "").strip())
    changed = False
    if os.path.exists(path):
        if expect_digest is not None:
            changed = scene_digest(path) != expect_digest
        elif expect_mtime is not None:
            changed = abs(os.path.getmtime(path) - expect_mtime) > 1e-6
    if changed:
        raise CanvasError(
            f"{os.path.basename(path)} changed while this was working on it "
            f"— it is probably open in Helpwo. Nothing was written; read it "
            f"again and retry.")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(scene, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def live_elements(scene: dict) -> list:
    return [el for el in (scene.get("elements") or [])
            if isinstance(el, dict) and not el.get("isDeleted")]


def describe_scene(scene: dict, limit: int = 200) -> str:
    """A structured text rendering of a board.

    Exact, cheap, and the ids in it are the ones an edit would name — which is
    why this, and not a screenshot, is how a board should normally be read. A
    picture is the right tool only when the question is itself visual, and that
    goes through /img on an exported image.
    """
    elements = live_elements(scene)
    if not elements:
        return "(empty board)"

    labels = {}
    for el in elements:
        if el.get("type") == "text" and el.get("containerId"):
            labels[el["containerId"]] = el.get("text") or ""

    lines = []
    for el in elements[:limit]:
        # A bound label is rendered on its container's line, not as a row of
        # its own: two rows for one visible box reads as two shapes.
        if el.get("type") == "text" and el.get("containerId"):
            continue
        parts = [f"{el.get('id', '?')}  {el.get('type', '?')}"]
        text = el.get("text") or labels.get(el.get("id"))
        if text:
            parts.append(f'"{str(text)[:60]}"')
        if el.get("type") in ("arrow", "line"):
            start = (el.get("startBinding") or {}).get("elementId")
            end = (el.get("endBinding") or {}).get("elementId")
            if start or end:
                parts.append(f"{start or '·'} → {end or '·'}")
        parts.append(
            f"at ({round(el.get('x', 0))},{round(el.get('y', 0))}) "
            f"{round(el.get('width', 0))}x{round(el.get('height', 0))}")
        if (el.get("customData") or {}).get("author") == "ai":
            parts.append("[ai]")
        lines.append("  " + "  ".join(parts))

    head = f"{len(elements)} element(s)"
    tail = f"\n  … {len(elements) - limit} more" if len(elements) > limit else ""
    return f"{head}\n" + "\n".join(lines) + tail


_SHAPE_KINDS = {
    "rectangle": "box", "ellipse": "round", "diamond": "diamond",
    "image": "box", "frame": "box", "text": "text",
    "arrow": "line", "line": "line", "draw": "line", "freedraw": "line",
}


def to_canvas_scene(scene: dict, title: str = "") -> dict:
    """Project a board onto the shared canvas-scene contract.

    The contract (``{"shapes": [...], "connectors": [...]}``, same one
    ``code-atlas`` emits) is what the terminal's infinite canvas renders, so a
    board and a code map are the same kind of thing to look at. Excalidraw's
    own pixel coordinates are kept verbatim as world units — a board's layout
    is the author's, and re-laying it out would be showing them a different
    board.
    """
    elements = live_elements(scene)
    labels = {el["containerId"]: el.get("text") or ""
              for el in elements
              if el.get("type") == "text" and el.get("containerId")}

    shapes, connectors = [], []
    for el in elements:
        kind = _SHAPE_KINDS.get(str(el.get("type") or ""), "box")
        if el.get("type") == "text" and el.get("containerId"):
            continue                       # folded onto its container's line
        el_id = str(el.get("id") or "")
        start = (el.get("startBinding") or {}).get("elementId")
        end = (el.get("endBinding") or {}).get("elementId")
        if kind == "line" and start and end:
            # A bound arrow is a relationship, not a drawing: as a connector
            # it keeps pointing at the right boxes when they move or collapse.
            connectors.append({"src": str(start), "dst": str(end),
                               "kind": str(el.get("type") or "arrow")})
            continue
        text = el.get("text") or labels.get(el_id) or ""
        by_ai = (el.get("customData") or {}).get("author") == "ai"
        # A path is carried through as absolute points, not just as its
        # bounding box: a sine wave and a straight diagonal have the same box,
        # and drawing the box is drawing the wrong thing.
        points = None
        if kind == "line" and isinstance(el.get("points"), list):
            ox, oy = float(el.get("x") or 0), float(el.get("y") or 0)
            points = [[ox + float(px), oy + float(py)]
                      for px, py in el["points"]
                      if isinstance(px, (int, float))
                      and isinstance(py, (int, float))]
        shapes.append({
            "id": el_id,
            "kind": kind,
            "x": float(el.get("x") or 0), "y": float(el.get("y") or 0),
            "w": float(el.get("width") or 0), "h": float(el.get("height") or 0),
            "label": str(text)[:120],
            "style": "hi" if by_ai else "shape",
            "points": points,
            "depth": 0,
            "meta": {"type": el.get("type"), "author": "ai" if by_ai else "human"},
        })
    return {"version": 1, "title": title or "board",
            "subtitle": f"{len(shapes)} shapes · {len(connectors)} links",
            "shapes": shapes, "connectors": connectors}


def count_ai_turns(scene: dict) -> list:
    """Visible elements the AI added, per turn. Bound labels do not count."""
    counts = {}
    for el in live_elements(scene):
        data = el.get("customData") or {}
        if data.get("author") != "ai":
            continue
        if el.get("type") == "text" and el.get("containerId"):
            continue
        turn = str(data.get("turn") or "")
        if turn:
            counts[turn] = counts.get(turn, 0) + 1
    return [{"turn": t, "count": c} for t, c in counts.items()]


_SCRATCH_PREFIX = "canvas-"


def scratch_board(root: str = ".") -> tuple[str, bool]:
    """Where a bare ``/canvas`` should land: (path, needs_creating).

    Typing the command is the whole request — nobody wants to be asked for a
    filename before they can draw. So one gets made for them.

    An empty board that a previous bare ``/canvas`` made is reused rather than
    joined by another one; otherwise opening the canvas five times leaves five
    empty files in the project. Only boards this function named are eligible:
    reusing a `plan.excalidraw` that its author has not drawn on yet would be
    opening someone else's file when they asked for a new one.
    """
    root = os.path.expanduser(root or ".")
    for path in find_boards(root):
        name = os.path.basename(path)
        if not name.startswith(_SCRATCH_PREFIX):
            continue
        try:
            if not live_elements(read_scene(path)):
                return (path, False)
        except CanvasError:
            continue                      # unreadable: leave it alone
    import datetime
    stem = _SCRATCH_PREFIX + datetime.date.today().strftime("%Y%m%d")
    candidate = os.path.join(root, stem + CANVAS_EXTENSION)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(root, f"{stem}-{n}{CANVAS_EXTENSION}")
        n += 1
    return (candidate, True)


def find_boards(root: str = ".", limit: int = 40) -> list:
    """Boards under `root`, newest first.

    Skips the directories that make a recursive walk useless in a real project;
    a board is something a person made, and it is not in node_modules.
    """
    skip = {"node_modules", ".git", "venv", ".venv", "__pycache__",
            "dist", "build", ".laintas"}
    found = []
    root = os.path.expanduser(root or ".")
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for name in files:
            if not is_canvas_path(name):
                continue
            full = os.path.join(base, name)
            try:
                found.append((os.path.getmtime(full), full))
            except OSError:
                pass
        if len(found) >= limit * 4:
            break
    found.sort(reverse=True)
    return [p for _mtime, p in found[:limit]]
