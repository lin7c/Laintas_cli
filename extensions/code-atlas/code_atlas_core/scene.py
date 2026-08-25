"""Graph -> scene: one deterministic layout, rendered by every front end.

The indexer answers *what the code is*; this answers *where each thing sits*.
It is a separate, equally deterministic pass on purpose. When layout lives
inside a viewer, every viewer invents its own — the terminal draws one
picture, the browser draws another, and a person who learned the shape of a
codebase in one of them has learned nothing about the other. Worse, a viewer
that re-runs a layout on every drill-down redraws the world each click, so
the reader loses the only thing a map is for: a stable place where things
are.

So: coordinates are computed once, here, from the graph alone. Same graph in,
same coordinates out. Both the terminal's infinite canvas and the web view
read this file and are therefore looking at the same map.

The layout is a nested pack (SHriMP's model): a module is a box, its classes
are boxes inside it, their methods are boxes inside those. Depth is drawn as
containment rather than as a separate view, which is what makes zooming *be*
navigation — you dive by magnifying, not by replacing the picture.

Scene contract (v1), shared with laintas_cli's ``infinite_canvas``:

    {"version": 1, "title": str, "subtitle": str, "header": float,
     "shapes":     [{"id","kind","x","y","w","h","label","style","depth",
                     "parent","detail","meta"}],
     "connectors": [{"src","dst","kind","weight"}]}
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

# World units. One unit is one terminal column at zoom 1.0, which is why a
# leaf is 24 wide: a name plus a little air, readable exactly when a reader
# has zoomed far enough in to be asking about single functions.
LEAF_W, LEAF_H = 24.0, 6.0
PAD_X, PAD_Y = 3.0, 2.0        # inside a container, around its children
# Room reserved for a container's own name, above its children. It is part of
# the contract (emitted as "header") because each front end has to reserve
# exactly this much: the terminal writes the name onto the top border, the
# browser sets its font from this band, and a renderer that guesses instead
# ends up printing the label over the first row of children.
HEADER_H = 5.0
GAP_X, GAP_Y = 3.0, 2.0        # between siblings
TARGET_ASPECT = 2.4            # w:h of a packed block (terminals are wide)

_KIND_ORDER = {"dir": 0, "module": 1, "class": 2, "function": 3}


class _Box:
    __slots__ = ("node", "children", "w", "h", "x", "y")

    def __init__(self, node: dict[str, Any]):
        self.node = node
        self.children: list["_Box"] = []
        self.w = LEAF_W
        self.h = LEAF_H
        self.x = 0.0
        self.y = 0.0


def _sort_key(box: "_Box") -> tuple:
    node = box.node
    return (_KIND_ORDER.get(node.get("kind", ""), 9),
            not node.get("public", False),
            str(node.get("name") or node.get("id") or ""))


def _measure(box: _Box) -> None:
    """Bottom-up: a container is as big as its packed children plus padding."""
    if not box.children:
        box.w, box.h = LEAF_W, LEAF_H
        return
    for child in box.children:
        _measure(child)
    box.children.sort(key=_sort_key)

    rows = _shelf_rows(box.children)
    inner_w = max(sum(c.w for c in row) + GAP_X * (len(row) - 1)
                  for row in rows)
    inner_h = (sum(max(c.h for c in row) for row in rows)
               + GAP_Y * (len(rows) - 1))
    box.w = inner_w + PAD_X * 2
    box.h = inner_h + PAD_Y * 2 + HEADER_H


def _shelf_rows(children: list[_Box]) -> list[list[_Box]]:
    """Greedy shelf packing toward a wide-ish block.

    Deterministic by construction: the children are already in a total order
    and the row width target is a pure function of their areas.
    """
    total_area = sum(c.w * c.h for c in children)
    target_w = max(max(c.w for c in children),
                   math.sqrt(max(total_area, 1.0) * TARGET_ASPECT))
    rows: list[list[_Box]] = [[]]
    width = 0.0
    for child in children:
        add = child.w if not rows[-1] else child.w + GAP_X
        if rows[-1] and width + add > target_w:
            rows.append([child])
            width = child.w
        else:
            rows[-1].append(child)
            width += add
    return rows


def _place(box: _Box, x: float, y: float) -> None:
    """Top-down: hand every box its absolute position."""
    box.x, box.y = x, y
    if not box.children:
        return
    rows = _shelf_rows(box.children)
    cy = y + PAD_Y + HEADER_H
    for row in rows:
        cx = x + PAD_X
        for child in row:
            _place(child, cx, cy)
            cx += child.w + GAP_X
        cy += max(c.h for c in row) + GAP_Y


def build_scene(graph: dict[str, Any],
                annotations: dict[str, Any] | None = None,
                *, title: str = "", max_depth: int = 4) -> dict[str, Any]:
    """graph.json (+ optional annotations.json) -> scene dict."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    kids: dict[str | None, list[str]] = {}
    for node in nodes.values():
        parent = node.get("parentId")
        # The indexer names a package's parent directory even when that
        # directory is outside the indexed tree (`dir:src/click` under
        # `dir:src`, which has no node). A parent nobody indexed is a root.
        if parent not in nodes:
            parent = None
        kids.setdefault(parent, []).append(node["id"])

    ann_by_target: dict[str, list[str]] = {}
    for ann in (annotations or {}).get("annotations", []) or []:
        target = str(ann.get("target_id") or "")
        text = str(ann.get("text") or "").strip()
        if target and text:
            ann_by_target.setdefault(target, []).append(text)

    def make(node_id: str, depth: int) -> _Box:
        box = _Box(nodes[node_id])
        if depth < max_depth:
            box.children = [make(cid, depth + 1)
                            for cid in kids.get(node_id, [])]
        return box

    roots = [make(nid, 0) for nid in kids.get(None, [])]
    if not roots:
        return {"version": 1, "header": HEADER_H, "title": title,
                "subtitle": "empty graph", "shapes": [], "connectors": []}

    virtual = _Box({"id": "__root__", "kind": "dir", "name": title})
    virtual.children = roots
    _measure(virtual)
    _place(virtual, 0.0, 0.0)

    shapes: list[dict[str, Any]] = []

    def emit(box: _Box, parent: str | None, depth: int) -> None:
        node = box.node
        node_id = node["id"]
        detail = []
        doc = (node.get("doc") or "").strip()
        if doc:
            detail.append(doc)
        for text in ann_by_target.get(node_id, [])[:3]:
            detail.append("» " + text)
        meta = {k: v for k, v in (
            ("file", node.get("file")), ("line", node.get("line")),
            ("kind", node.get("kind")),
            ("public", "yes" if node.get("public") else "no"),
            ("children", len(box.children) or None),
            ("annotated", "yes" if node_id in ann_by_target else None),
        ) if v}
        shapes.append({
            "id": node_id,
            "kind": "round" if node.get("kind") == "function" else "box",
            "x": round(box.x, 3), "y": round(box.y, 3),
            "w": round(box.w, 3), "h": round(box.h, 3),
            "label": str(node.get("name") or node_id),
            "style": str(node.get("kind") or "node"),
            "depth": depth,
            "parent": parent,
            "detail": detail,
            "meta": meta,
        })
        for child in box.children:
            emit(child, node_id, depth + 1)

    for root in roots:
        emit(root, None, 0)

    drawn = {s["id"] for s in shapes}
    weights: dict[tuple[str, str, str], int] = {}
    for edge in graph.get("edges", []):
        src, dst, kind = edge.get("src"), edge.get("dst"), edge.get("kind", "")
        if kind == "contains" or src not in drawn or dst not in drawn:
            continue
        if src == dst:
            continue
        key = (src, dst, kind)
        weights[key] = weights.get(key, 0) + 1

    connectors = [{"src": s, "dst": d, "kind": k, "weight": w}
                  for (s, d, k), w in sorted(weights.items())]

    mods = sum(1 for s in shapes if s["style"] == "module")
    return {
        "version": 1,
        "header": HEADER_H,
        "title": title or graph.get("root") or "atlas",
        "subtitle": (f"{mods} modules · {len(shapes)} nodes · "
                     f"{len(connectors)} deps"
                     + (f" · {len(ann_by_target)} annotated"
                        if ann_by_target else "")),
        "shapes": shapes,
        "connectors": connectors,
    }


def build_from_dir(atlas_dir: str | Path, *,
                   title: str = "", max_depth: int = 4) -> dict[str, Any]:
    """Read graph.json (+ annotations.json if present) and lay them out."""
    atlas_dir = Path(atlas_dir)
    graph = json.loads((atlas_dir / "graph.json").read_text(encoding="utf-8"))
    ann_path = atlas_dir / "annotations.json"
    annotations = None
    if ann_path.is_file():
        try:
            annotations = json.loads(ann_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            annotations = None
    return build_scene(graph, annotations,
                       title=title or atlas_dir.name, max_depth=max_depth)


def write_scene(atlas_dir: str | Path, *, max_depth: int = 4) -> Path:
    atlas_dir = Path(atlas_dir)
    scene = build_from_dir(atlas_dir, max_depth=max_depth)
    out = atlas_dir / "scene.json"
    out.write_text(json.dumps(scene, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    return out
