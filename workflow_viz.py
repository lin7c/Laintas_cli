"""Layout + plain-text rendering for HWG graphs (metro-map style).

PURE module: HWG statements (from ``hwg_adapter``) + optional per-node run
status in, painted char canvas + hit-region map out. No I/O, no
prompt_toolkit - the interactive layer lives in ``hwg_view``.

Metro-map layout:
  - Back edges (cycle closers, incl. self-loops) are detected by DFS first,
    so layering runs on the remaining DAG.
  - The main line is the longest chain over ALL forward edges (conditional
    or not), drawn vertically with condition labels on the connectors.
  - Every other forward edge is bundled into a right-hand rail: a stub out
    of the source, one shared vertical rail, one arrow into the destination.
    Fan-ins like `x -> {on: FAIL} #report#` collapse into a single track
    instead of a nest of crossings.
  - Back edges route along a left-hand rail marked with their maxLoops
    budget (``↺×N``).

Complexity: layering and routing are O(V+E); painting is O(canvas) once.
The canvas is bounded (see _BOX_*, _RAIL_*), so a full repaint per
keystroke stays cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import symbols


# ── Run status model ──────────────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_PAUSED = "paused"

_STATUS_ICON = {
    STATUS_PENDING: symbols.DOT_OPEN,
    STATUS_RUNNING: symbols.DOT_HALF,
    STATUS_DONE: symbols.OK,
    STATUS_FAILED: symbols.FAIL,
    STATUS_PAUSED: "Ⅱ",
}

_STATUS_STYLE = {
    STATUS_PENDING: "class:st.pend",
    STATUS_RUNNING: "class:st.run",
    STATUS_DONE: "class:st.done",
    STATUS_FAILED: "class:st.fail",
    STATUS_PAUSED: "class:st.paused",
}


def status_icon(status: Optional[str]) -> str:
    return _STATUS_ICON.get(status or "", symbols.DOT_OPEN)


def status_style(status: Optional[str]) -> str:
    return _STATUS_STYLE.get(status or "", _STATUS_STYLE[STATUS_PENDING])


# ── Canvas ────────────────────────────────────────────────────────────────

_EMPTY = " "


@dataclass
class Canvas:
    """Char grid with per-cell style tags and hit regions."""

    width: int
    height: int
    cells: list[list[str]] = field(default_factory=list)
    styles: list[list[str]] = field(default_factory=list)
    # screen row -> [(col_start, col_end_inclusive, node_id), ...]
    hits: dict[int, list[tuple[int, int, str]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cells = [[_EMPTY] * self.width for _ in range(self.height)]
        self.styles = [[""] * self.width for _ in range(self.height)]

    def put(self, row: int, col: int, text: str, style: str = "") -> None:
        """Write text, but never overwrite an existing non-space cell."""
        if not (0 <= row < self.height):
            return
        line, tag = self.cells[row], self.styles[row]
        for i, ch in enumerate(text):
            x = col + i
            if ch == "\n":
                continue
            if 0 <= x < self.width and (ch == _EMPTY or line[x] == _EMPTY):
                if ch != _EMPTY:
                    line[x] = ch
                    tag[x] = style

    def put_force(self, row: int, col: int, text: str, style: str = "") -> None:
        """Write text unconditionally (boxes win over routed lines)."""
        if not (0 <= row < self.height):
            return
        line, tag = self.cells[row], self.styles[row]
        for i, ch in enumerate(text):
            x = col + i
            if ch == "\n":
                continue
            if 0 <= x < self.width:
                line[x] = ch
                tag[x] = style

    def blank(self, row: int, col: int, width: int) -> None:
        """Erase a span (used to reserve label space on a stub)."""
        if not (0 <= row < self.height):
            return
        line, tag = self.cells[row], self.styles[row]
        for x in range(col, min(col + width, self.width)):
            line[x] = _EMPTY
            tag[x] = ""

    def hit(self, row: int, col_start: int, col_end: int, node_id: str) -> None:
        self.hits.setdefault(row, []).append((col_start, col_end, node_id))

    def hit_at(self, row: int, col: int) -> Optional[str]:
        for start, end, node_id in self.hits.get(row, ()):
            if start <= col <= end:
                return node_id
        return None

    def lines(self) -> list[str]:
        return ["".join(line).rstrip() for line in self.cells]

    def styled_rows(self) -> list[list[tuple[str, str]]]:
        """Rows as (text, style) runs - direct prompt_toolkit payload."""
        rows: list[list[tuple[str, str]]] = []
        for y in range(self.height):
            runs: list[tuple[str, str]] = []
            text, tag = self.cells[y], self.styles[y]
            start = 0
            for x in range(1, self.width + 1):
                if x == self.width or tag[x] != tag[start]:
                    chunk = "".join(text[start:x])
                    if chunk:
                        runs.append((chunk, tag[start]))
                    start = x
            rows.append(runs)
        return rows


# ── Input model ───────────────────────────────────────────────────────────

@dataclass
class GraphInput:
    """Normalised graph: nodes, edges, schedule, optional run status."""

    nodes: list[dict]                     # hwg_adapter node dicts
    edges: list[dict]                     # hwg_adapter edge dicts
    schedule: Optional[dict] = None
    status: dict[str, str] = field(default_factory=dict)  # id -> STATUS_*


def graph_from_statements(statements: list[dict],
                          status: Optional[dict[str, str]] = None) -> GraphInput:
    """Adapt hwg_adapter statements into GraphInput."""
    from hwg_adapter import as_graph
    g = as_graph(statements)
    return GraphInput(nodes=g["nodes"], edges=g["edges"],
                      schedule=g.get("schedule"), status=dict(status or {}))


# ── Graph analysis ────────────────────────────────────────────────────────

def _back_edges(nodes: list[dict], edges: list[dict]) -> set[int]:
    """DFS back-edge indices (cycle-closing edges, incl. self-loops)."""
    adj: dict[str, list[tuple[str, int]]] = {n["id"]: [] for n in nodes}
    for idx, e in enumerate(edges):
        adj.setdefault(e["from"], []).append((e["to"], idx))
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n["id"]: WHITE for n in nodes}
    back: set[int] = set()

    def visit(u: str) -> None:
        color[u] = GRAY
        for v, idx in adj.get(u, ()):
            c = color.get(v, BLACK)
            if c == GRAY:
                back.add(idx)
            elif c == WHITE:
                visit(v)
        color[u] = BLACK

    for n in nodes:
        if color[n["id"]] == WHITE:
            visit(n["id"])
    return back


def _longest_chain(nodes: list[dict], forward: list[dict]) -> list[str]:
    """Longest chain over forward edges (memoised DAG walk).

    Ties prefer node declaration order so the layout is deterministic.
    """
    out_adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    indeg: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in forward:
        out_adj.setdefault(e["from"], []).append(e["to"])
        indeg[e["to"]] = indeg.get(e["to"], 0) + 1
    order = {n["id"]: i for i, n in enumerate(nodes)}
    memo: dict[str, list[str]] = {}

    def longest_from(u: str, seen: frozenset) -> list[str]:
        if u in memo:
            return memo[u]
        best: list[str] = [u]
        for v in sorted(out_adj.get(u, ()), key=lambda w: order.get(w, 0)):
            if v in seen:            # defensive: forward set is a DAG
                continue
            path = longest_from(v, seen | {u})
            if 1 + len(path) > len(best):
                best = [u] + path
        memo[u] = best
        return best

    roots = [n["id"] for n in nodes if indeg.get(n["id"], 0) == 0]
    candidates = roots or [n["id"] for n in nodes]
    best_path: list[str] = []
    for nid in candidates:
        path = longest_from(nid, frozenset())
        if len(path) > len(best_path):
            best_path = path
    if not best_path and nodes:
        best_path = [nodes[0]["id"]]
    return best_path


def _edge_label(edge: dict) -> str:
    parts = []
    if edge.get("on"):
        parts.append(str(edge["on"]))
    if edge.get("maxLoops"):
        parts.append(f"↺×{edge['maxLoops']}")
    return " · ".join(parts)


edge_label = _edge_label  # public alias for the interactive viewer


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


# ── Geometry constants ────────────────────────────────────────────────────

_BOX_W = 26
_BOX_H = 4            # border / id+status / file / border
_VGAP = 1             # rows between consecutive mainline boxes
_MAIN_COL = 14        # main line left column
_LOOP_RAIL = 1        # left return-track column
_MIN_WIDTH = 78


@dataclass
class _Box:
    node: dict
    row: int
    col: int
    w: int = _BOX_W
    h: int = _BOX_H

    @property
    def mid_row(self) -> int:
        return self.row + 1

    @property
    def right(self) -> int:
        return self.col + self.w - 1

    @property
    def bottom(self) -> int:
        return self.row + self.h - 1


@dataclass
class GraphView:
    """Fully laid-out graph ready to paint onto a Canvas."""

    canvas: Canvas
    node_rows: dict[str, int]     # node id -> clickable middle row
    mainline: list[str]
    meta: dict = field(default_factory=dict)
    # ── fields the interactive viewer needs ──
    node_rects: dict[str, tuple[int, int, int, int]] = field(
        default_factory=dict)    # id -> (row, col, w, h)
    node_order: list[str] = field(default_factory=list)  # navigation order
    in_edges: dict[str, list[dict]] = field(default_factory=dict)
    out_edges: dict[str, list[dict]] = field(default_factory=dict)
    nodes_by_id: dict[str, dict] = field(default_factory=dict)

    def node(self, nid: str) -> Optional[dict]:
        return self.nodes_by_id.get(nid)


# ── Layout ────────────────────────────────────────────────────────────────

def build_view(graph: GraphInput) -> GraphView:
    nodes, edges = graph.nodes, graph.edges
    if not nodes:
        canvas = Canvas(width=_MIN_WIDTH, height=1)
        canvas.put(0, 2, "empty graph", "class:dim")
        return GraphView(canvas=canvas, node_rows={}, mainline=[],
                         meta={"counts": "0 nodes"})

    node_by_id = {n["id"]: n for n in nodes}
    back = _back_edges(nodes, edges)
    loop_edges = [e for i, e in enumerate(edges) if i in back]
    forward = [e for i, e in enumerate(edges) if i not in back]
    mainline = _longest_chain(nodes, forward)
    main_set = set(mainline)

    # ── vertical placement: mainline column + right stack ──
    boxes: list[_Box] = []
    box_by_id: dict[str, _Box] = {}
    row = 0
    for nid in mainline:
        box = _Box(node=node_by_id[nid], row=row, col=_MAIN_COL)
        boxes.append(box)
        box_by_id[nid] = box
        row += _BOX_H + _VGAP
    main_bottom = row

    side_nodes = [n for n in nodes if n["id"] not in main_set]
    rail_col = _MAIN_COL + _BOX_W + 6      # right rail stack column
    side_row = 0
    for n in side_nodes:
        box = _Box(node=n, row=side_row, col=rail_col + 6)
        boxes.append(box)
        box_by_id[n["id"]] = box
        side_row += _BOX_H + _VGAP
    rail_x = rail_col + 3                  # shared vertical rail column

    max_col = max((b.right for b in boxes), default=_MIN_WIDTH)
    width = max(max_col + 2, _MIN_WIDTH)
    body_rows = max((b.bottom for b in boxes), default=0)
    height = body_rows + 2
    canvas = Canvas(width=width, height=height)

    # ── mainline connectors (drawn first, boxes overwrite crossings) ──
    for a, b in zip(mainline, mainline[1:]):
        edge = next((e for e in forward
                     if e["from"] == a and e["to"] == b), None)
        src, dst = box_by_id[a], box_by_id[b]
        col = src.col + (src.w // 2)
        # gap rows are src.bottom+1 .. dst.row-1 (VGAP of 1 → single row)
        for y in range(src.bottom, dst.row - 1):
            canvas.put(y, col, "│", "class:flow")
        canvas.put_force(dst.row - 1, col, "▼", "class:flow")
        label = _truncate(_edge_label(edge), width - col - 2) if edge else ""
        if label:
            canvas.put_force(dst.row - 1, col + 2, label, "class:flow.label")

    # ── forward side edges: right-rail bundle ──
    # Order by source row so stub rows are stable and labels stack cleanly.
    side_fwd = [e for e in forward
                if not (e["from"] in main_set and e["to"] in main_set
                        and _is_adjacent(e, mainline))]
    side_fwd.sort(key=lambda e: box_by_id[e["from"]].mid_row)
    used_stub_rows: set[int] = set()
    for e in side_fwd:
        src = box_by_id.get(e["from"])
        dst = box_by_id.get(e["to"])
        if src is None or dst is None:
            continue
        span = max(3, rail_x - 1 - src.right)   # usable stub width
        label = _truncate(_edge_label(e) or "->", span - 1)
        y = src.mid_row
        while y in used_stub_rows and y < height - 1:
            y += 1                         # stack labels from the same source
        used_stub_rows.add(y)
        x0 = src.right + 1
        # key elements first (force); filler dashes skip occupied cells
        canvas.put_force(y, x0, label, "class:side.label")
        for x in range(x0 + len(label), rail_x):
            canvas.put(y, x, "─", "class:side")
        canvas.put_force(y, rail_x,
                         "┐" if y < dst.mid_row else "┘", "class:side")
        # shared vertical rail (corner cell stays: put skips it)
        y_lo, y_hi = min(y, dst.mid_row), max(y, dst.mid_row)
        for yy in range(y_lo, y_hi + 1):
            canvas.put(yy, rail_x, "║", "class:side")
        # arrow into the destination's left or right edge
        if dst.col > rail_x:               # destination on the right stack
            for x in range(rail_x + 1, dst.col - 1):
                canvas.put(dst.mid_row, x, "─", "class:side")
            canvas.put_force(dst.mid_row, dst.col - 1, "▶", "class:side")
        else:                              # destination on the mainline
            for x in range(dst.right + 2, rail_x):
                canvas.put(dst.mid_row, x, "─", "class:side")
            canvas.put_force(dst.mid_row, dst.right + 1, "◀", "class:side")

    # ── back edges: left-rail bundle ──
    for e in loop_edges:
        src = box_by_id.get(e["from"])
        dst = box_by_id.get(e["to"])
        if src is None or dst is None:
            continue
        label = _truncate(_edge_label(e) or "↺", 12)
        if src is dst:                     # self-loop: tag left of the box
            canvas.put(src.mid_row, max(0, src.col - len(label) - 2),
                       f"↺ {label}", "class:loop.label")
            continue
        y = src.mid_row
        # vertical rail first, corners (force) win over it, stubs fill the rest
        y_lo, y_hi = min(y, dst.mid_row), max(y, dst.mid_row)
        for yy in range(y_lo, y_hi + 1):
            canvas.put(yy, _LOOP_RAIL, "║", "class:loop")
        canvas.put_force(y, _LOOP_RAIL,
                         "└" if dst.mid_row < y else "┌", "class:loop")
        canvas.put_force(dst.mid_row, _LOOP_RAIL,
                         "┌" if dst.mid_row < y else "└", "class:loop")
        # label right after the rail corner; dashes reach the source's border
        canvas.put_force(y, _LOOP_RAIL + 1, label, "class:loop.label")
        for x in range(_LOOP_RAIL + 1 + len(label), src.col):
            canvas.put(y, x, "─", "class:loop")
        # arrow into the destination's left edge
        for x in range(_LOOP_RAIL + 1, dst.col - 1):
            canvas.put(dst.mid_row, x, "─", "class:loop")
        canvas.put_force(dst.mid_row, dst.col - 1, "▶", "class:loop")

    # ── node boxes last: borders always win over routed lines ──
    node_rows: dict[str, int] = {}
    node_rects: dict[str, tuple[int, int, int, int]] = {}
    for box in boxes:
        node = box.node
        nid = node["id"]
        status = graph.status.get(nid)
        st_key = status or STATUS_PENDING
        border_style = ("class:border.manual" if node.get("manual")
                        else f"class:border.{st_key}")
        top = "┌" + "─" * (box.w - 2) + "┐"
        bottom = "└" + "─" * (box.w - 2) + "┘"
        canvas.put_force(box.row, box.col, top, border_style)
        canvas.put_force(box.bottom, box.col, bottom, border_style)
        for dy in range(1, box.h - 1):
            canvas.put_force(box.row + dy, box.col, "│", border_style)
            canvas.put_force(box.row + dy, box.right, "│", border_style)

        icon = status_icon(status)
        canvas.put_force(box.mid_row, box.col + 1, f" {icon} ",
                         status_style(status))
        head = f"#{nid}#"
        if node.get("manual"):
            head += " ⏸"
        canvas.put_force(box.mid_row, box.col + 4,
                         _truncate(head, box.w - 5), "class:nid")
        policy = node.get("policy") or {}
        file_bits = [str(node.get("file") or "")]
        if policy.get("retry"):
            file_bits.append(f"retry×{policy['retry']}")
        if policy.get("timeout"):
            file_bits.append(f"⏱{policy['timeout']}")
        canvas.put_force(box.bottom - 1, box.col + 1,
                         _truncate(" ".join(b for b in file_bits if b),
                                   box.w - 3), "class:nfile")

        node_rows[nid] = box.mid_row
        node_rects[nid] = (box.row, box.col, box.w, box.h)
        for r in range(box.row, box.bottom + 1):
            canvas.hit(r, box.col, box.right, nid)

    # ── meta line ──
    counts = {
        "node": len(nodes), "edge": len(edges),
        "loop": len(loop_edges),
        "manual": sum(1 for n in nodes if n.get("manual")),
    }
    meta = {"counts": " · ".join(
        f"{v} {k}{'s' if v != 1 else ''}" for k, v in counts.items() if v)}
    if graph.schedule:
        bits = []
        if graph.schedule.get("days"):
            bits.append("days " + ",".join(graph.schedule["days"]))
        if graph.schedule.get("start"):
            bits.append("start " + str(graph.schedule["start"]))
        if graph.schedule.get("deadline"):
            bits.append("by " + str(graph.schedule["deadline"]))
        if graph.schedule.get("tz"):
            bits.append("tz " + str(graph.schedule["tz"]))
        meta["schedule"] = " ".join(bits)

    # ── adjacency for keyboard navigation ──
    in_edges: dict[str, list[dict]] = {}
    out_edges: dict[str, list[dict]] = {}
    for e in edges:
        out_edges.setdefault(e["from"], []).append(e)
        in_edges.setdefault(e["to"], []).append(e)
    node_order = [n["id"] for n in nodes]

    return GraphView(canvas=canvas, node_rows=node_rows,
                     mainline=mainline, meta=meta,
                     node_rects=node_rects, node_order=node_order,
                     in_edges=in_edges, out_edges=out_edges,
                     nodes_by_id=node_by_id)


def _is_adjacent(edge: dict, mainline: list[str]) -> bool:
    """True when edge connects two consecutive mainline nodes."""
    for a, b in zip(mainline, mainline[1:]):
        if edge["from"] == a and edge["to"] == b:
            return True
    return False


# ── Plain-text rendering (non-interactive callers) ────────────────────────

def _plain_body(view: GraphView, title: str) -> str:
    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("─" * min(max(len(title), 8), view.canvas.width))
    if view.meta.get("counts"):
        lines.append(view.meta["counts"])
    if view.meta.get("schedule"):
        lines.append(view.meta["schedule"])
    if lines:
        lines.append("")
    body = view.canvas.lines()
    while body and not body[-1]:
        body.pop()
    lines.extend(body)
    return "\n".join(lines)


def render_plain(graph: GraphInput, title: str = "") -> str:
    """Render a static, pasteable text diagram of the graph."""
    return _plain_body(build_view(graph), title)


# ── HWO tree → lane (swimlane) layout ────────────────────────────────────
#
# HWO is a nested sequence (workflow header / task / agent / parallel).
# The lane layout maps that tree onto the canvas:
#   - every agent is a lane box whose top border carries its name and
#     whose interior stacks the agent's body steps;
#   - plain tasks are single selectable lines;
#   - parallel members are painted side by side under a shared label;
#   - nesting indents inside the parent lane, so width grows with depth
#     but every lane is only as wide as its own content.
#
# Element ids are step paths ("0", "0.1", ...) - the same stepIds the
# hwo_runner emits - so run status maps onto lanes without translation.

_LANE_TEXT_MAX = 34     # max painted text width for one lane line
_LANE_MIN_INNER = 10    # minimum lane interior width
_LANE_HGAP = 2          # columns between parallel lane boxes
_TOP_GAP = 1            # blank rows between top-level elements


@dataclass
class TreeInput:
    """Normalised HWO tree: the adapter's AST plus run status."""

    steps: list                         # hwo_adapter AST nodes
    status: dict[str, str] = field(default_factory=dict)


def tree_from_steps(steps: list,
                    status: Optional[dict[str, str]] = None) -> TreeInput:
    return TreeInput(steps=list(steps or []), status=dict(status or {}))


def _io_summary(io: Optional[dict]) -> str:
    if not io:
        return ""
    parts = []
    for kind in ("in", "out"):
        names = [str(p.get("name") or "") for p in (io.get(kind) or [])
                 if p.get("name")]
        if names:
            parts.append(f"{kind}({', '.join(names)})")
    return f"[{', '.join(parts)}]" if parts else ""


def _flatten_tree(steps: list) -> tuple[list[dict], list[dict], list[dict]]:
    """AST -> pre-ordered elements (children attached) + navigation edges.

    Edges: consecutive siblings chain; an agent links to its first child
    (dive); a parallel links to each member head. These drive the viewer's
    left/right navigation: 'l' dives or advances, 'h' returns or retreats.
    """
    elements: list[dict] = []
    edges: list[dict] = []

    def walk(items: list, path: str, depth: int) -> list[dict]:
        heads: list[dict] = []
        prev: Optional[str] = None
        idx = 0                      # counts non-workflow siblings only,
        for node in items:          # matching hwo_runner's step paths
            if node.get("type") == "workflow":
                # display-only header: no id, no hit zone, no path index
                heads.append({"id": "", "kind": "workflow", "depth": depth,
                              "node": node, "children": []})
                continue
            nid = f"{path}.{idx}" if path else str(idx)
            idx += 1
            el = {"id": nid, "kind": node.get("type"), "depth": depth,
                  "node": node, "children": []}
            elements.append(el)
            heads.append(el)
            if prev is not None:
                edges.append({"from": prev, "to": nid})
            prev = nid
            if node.get("type") == "agent":
                el["children"] = walk(node.get("body") or [], nid, depth + 1)
                if el["children"]:
                    edges.append({"from": nid,
                                  "to": el["children"][0]["id"]})
            elif node.get("type") == "parallel":
                el["children"] = walk(node.get("body") or [], nid, depth + 1)
                for child in el["children"]:
                    edges.append({"from": nid, "to": child["id"]})
        return heads

    roots = walk(steps, "", 0)
    return elements, edges, roots


def _measure_element(el: dict, status: dict[str, str]) -> None:
    """Bottom-up pass: display text + (rows, cols) geometry per element."""
    el["st"] = status.get(el["id"]) or STATUS_PENDING
    kind, node = el["kind"], el["node"]
    if kind == "task":
        el["line"] = _truncate(node.get("text") or "", _LANE_TEXT_MAX - 2)
        el["rows"], el["cols"] = 1, 2 + len(el["line"])
    elif kind == "workflow":
        el["line"] = _truncate(("@line " + _io_summary(node.get("io"))).strip(),
                               _LANE_TEXT_MAX)
        el["rows"], el["cols"] = 1, max(1, len(el["line"]))
    elif kind == "agent":
        head = f"#{node.get('name') or el['id']}#"
        if node.get("model"):
            head += f"@{node['model']}"
        meta = " · ".join(b for b in (node.get("promptFile"),
                                      _io_summary(node.get("io"))) if b)
        el["head"], el["meta"] = head, meta
        for child in el["children"]:
            _measure_element(child, status)
        inner = max(len(head) + 2,
                    (len(meta) + 2) if meta else 0,
                    max((c["cols"] for c in el["children"]), default=0),
                    _LANE_MIN_INNER)
        el["cols"] = inner + 4      # border + padding each side
        body_rows = sum(c["rows"] for c in el["children"])
        el["rows"] = 2 + (1 if meta else 0) + max(1, body_rows)
    elif kind == "parallel":
        for child in el["children"]:
            _measure_element(child, status)
        el["rows"] = 1 + max((c["rows"] for c in el["children"]), default=1)
        el["cols"] = (sum(c["cols"] for c in el["children"])
                      + _LANE_HGAP * max(0, len(el["children"]) - 1))
    else:                           # unknown kinds degrade to a stub line
        el["line"] = f"<{kind}>"
        el["rows"], el["cols"] = 1, len(el["line"])


def _paint_element(canvas: Canvas, el: dict, x: int, y: int,
                   node_rows: dict, rects: dict) -> None:
    kind, nid = el["kind"], el["id"]
    if not nid:                     # display-only @line header
        canvas.put_force(y, x, el.get("line", ""), "class:dim")
        return
    node_rows[nid] = y
    if kind in ("task", "workflow"):
        if kind == "workflow":
            text, style = el["line"], "class:dim"
        else:
            text = f"{status_icon(el['st'])} {el['line']}"
            style = status_style(el["st"])
        canvas.put_force(y, x, text, style)
        canvas.hit(y, x, x + el["cols"] - 1, nid)
        rects[nid] = (y, x, el["cols"], 1)
        return
    if kind == "agent":
        bstyle = f"class:border.{el['st']}"
        w, rows = el["cols"], el["rows"]
        head = f" {el['head']} "
        top = "┌" + head + "─" * max(1, w - 2 - len(head)) + "┐"
        canvas.put_force(y, x, top, bstyle)
        canvas.put_force(y + rows - 1, x, "└" + "─" * (w - 2) + "┘", bstyle)
        canvas.hit(y, x, x + w - 1, nid)     # header row = the lane's hit zone
        rects[nid] = (y, x, w, rows)
        iy = y + 1
        if el["meta"]:
            canvas.put_force(iy, x + 2, _truncate(el["meta"], w - 5),
                             "class:nfile")
            iy += 1
        for child in el["children"]:
            _paint_element(canvas, child, x + 2, iy, node_rows, rects)
            iy += child["rows"]
        for ry in range(y + 1, y + rows - 1):
            canvas.put_force(ry, x, "│", bstyle)
            canvas.put_force(ry, x + w - 1, "│", bstyle)
        return
    if kind == "parallel":
        label = "┄ // parallel //"
        canvas.put_force(y, x, label, "class:flow.label")
        canvas.hit(y, x, x + len(label) - 1, nid)
        rects[nid] = (y, x, el["cols"], el["rows"])
        mx = x
        for child in el["children"]:
            _paint_element(canvas, child, mx, y + 1, node_rows, rects)
            mx += child["cols"] + _LANE_HGAP
        return
    # stub for unknown kinds
    canvas.put_force(y, x, el["line"], "class:dim")
    canvas.hit(y, x, x + el["cols"] - 1, nid)
    rects[nid] = (y, x, el["cols"], 1)


def build_lane_view(tree: TreeInput) -> GraphView:
    """Lay the HWO tree out as lanes on a Canvas (same GraphView shape
    the HWG metro map produces, so the interactive viewer is shared)."""
    elements, edges, roots = _flatten_tree(tree.steps)
    if not elements:
        canvas = Canvas(width=_MIN_WIDTH, height=1)
        canvas.put(0, 2, "empty workflow", "class:dim")
        return GraphView(canvas=canvas, node_rows={}, mainline=[],
                         meta={"counts": "0 steps"})

    for el in roots:
        _measure_element(el, tree.status)

    total_rows = sum(el["rows"] for el in roots) + _TOP_GAP * (len(roots) - 1)
    total_cols = max(el["cols"] for el in roots)
    canvas = Canvas(width=max(total_cols + 1, _MIN_WIDTH),
                    height=total_rows + 1)

    node_rows: dict[str, int] = {}
    rects: dict[str, tuple[int, int, int, int]] = {}
    y = 0
    for el in roots:
        _paint_element(canvas, el, 0, y, node_rows, rects)
        y += el["rows"] + _TOP_GAP

    in_edges: dict[str, list[dict]] = {}
    out_edges: dict[str, list[dict]] = {}
    for e in edges:
        out_edges.setdefault(e["from"], []).append(e)
        in_edges.setdefault(e["to"], []).append(e)

    n_tasks = sum(1 for e in elements if e["kind"] == "task")
    n_agents = sum(1 for e in elements if e["kind"] == "agent")
    n_par = sum(1 for e in elements if e["kind"] == "parallel")
    counts = f"{n_tasks} tasks · {n_agents} agents"
    if n_par:
        counts += f" · {n_par} parallel"

    return GraphView(
        canvas=canvas, node_rows=node_rows, mainline=[],
        node_rects=rects, node_order=[e["id"] for e in elements],
        in_edges=in_edges, out_edges=out_edges,
        nodes_by_id={e["id"]: e for e in elements},
        meta={"counts": counts})


def render_lane_plain(tree: TreeInput, title: str = "") -> str:
    """Render a static, pasteable text diagram of the HWO lanes."""
    return _plain_body(build_lane_view(tree), title)


# ── Mini status strip (one-line run digest) ───────────────────────────────
#
# Derives per-node status + duration from a durable run dict (the
# workflow_state shape: events + history + currentNode + pendingInterrupt)
# and renders a single-line progress strip. Kind-agnostic: node ids are
# whatever the runner emitted (hwg node names / hwo step paths).


def _fmt_dur(seconds) -> str:
    if seconds is None:
        return ""
    sec = max(0.0, float(seconds))
    if sec < 10:
        return f"{sec:.1f}s"
    if sec < 60:
        return f"{sec:.0f}s"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def status_from_run(run: dict,
                    node_order: Optional[list[str]] = None) -> dict:
    """Per-node run status reconstructed from the durable run dict.

    Events are the primary source (they carry createdAt); history and
    currentNode cover runs whose event log was trimmed to the last 500.
    Returns {"order", "status", "duration", "overall"}.
    """
    status: dict[str, str] = {}
    started: dict[str, float] = {}
    ended: dict[str, float] = {}
    last_ts: Optional[float] = None
    for ev in run.get("events") or []:
        ts = ev.get("createdAt")
        if not isinstance(ts, (int, float)):
            continue
        last_ts = float(ts) if last_ts is None else max(last_ts, float(ts))
        payload = ev.get("payload") or {}
        node = payload.get("node") or payload.get("stepId")
        if not node:
            continue
        etype = ev.get("type")
        if etype in ("node_started", "step_started"):
            status[node] = STATUS_RUNNING
            started.setdefault(node, float(ts))
        elif etype in ("node_completed", "step_completed"):
            status[node] = STATUS_DONE
            ended[node] = float(ts)
        elif etype in ("node_failed", "step_failed"):
            status[node] = STATUS_FAILED
            ended[node] = float(ts)
    for nid in run.get("history") or []:
        status.setdefault(str(nid), STATUS_DONE)
    overall = str(run.get("status") or "")
    current = run.get("currentNode")
    if current:
        status.setdefault(str(current),
                          STATUS_FAILED if overall == "failed"
                          else STATUS_RUNNING)
    intr = run.get("pendingInterrupt")
    pnode = intr.get("node") if isinstance(intr, dict) else None
    if pnode:
        status[str(pnode)] = STATUS_PAUSED

    order = [str(n) for n in (node_order or [])]
    order += [n for n in status if n not in set(order)]
    duration: dict[str, float] = {}
    for nid, t0 in started.items():
        t1 = ended.get(nid, last_ts)
        if t1 is not None:
            duration[nid] = max(0.0, float(t1) - t0)
    return {"order": order, "status": status,
            "duration": duration, "overall": overall}


def render_mini(run_status: dict, *, width: int = 80) -> str:
    """One-line progress strip; clamps by dropping durations, then
    collapsing the leading run of done nodes, then hard truncating."""
    order = list(run_status.get("order") or [])
    if not order:
        return ""
    st = run_status.get("status") or {}
    dur = run_status.get("duration") or {}

    def seg(nid: str, with_dur: bool) -> str:
        state = st.get(nid) or STATUS_PENDING
        text = f"{status_icon(state)}{nid}"
        if with_dur and state != STATUS_PENDING and dur.get(nid) is not None:
            text += f" {_fmt_dur(dur[nid])}"
        return text

    joiner = " ─ "
    for with_dur in (True, False):
        line = joiner.join(seg(n, with_dur) for n in order)
        if len(line) <= width:
            return line
    cut = 0
    while cut < len(order) and st.get(order[cut]) == STATUS_DONE:
        cut += 1
    tail = order[cut:]
    if cut and tail:
        for with_dur in (True, False):
            line = f"…+{cut} " + joiner.join(seg(n, with_dur) for n in tail)
            if len(line) <= width:
                return line
    return _truncate(joiner.join(seg(n, False) for n in (tail or order)),
                     width)


def mini_from_run(run: dict, node_order: Optional[list[str]] = None,
                  width: int = 80) -> str:
    """One-line progress strip for a durable workflow run (any kind)."""
    return render_mini(status_from_run(run, node_order), width=width)


def node_order_from_source(source: str) -> list[str]:
    """Best-effort declaration order from an .hwg source string.

    Matches the adapter's ``(file.hwo)#name#`` node header; nodes can also
    be declared implicitly by an edge like ``#a# -> #b#``. This exists so
    the mini strip lines up with the diagram without re-parsing the graph.
    """
    import re
    seen: list[str] = []
    for m in re.finditer(r"\([^)]*\.hwo\)\s*#([^#\s]+)#", source):
        nid = m.group(1)
        if nid not in seen:
            seen.append(nid)
    # capture BOTH sides of an edge: the left node may already be seen,
    # but the right one is often declared implicitly (#a# -> #c#).
    for m in re.finditer(r"#([^#\s]+)#\s*(?:->|=>)\s*#([^#\s]+)#", source):
        for nid in m.groups():
            if nid not in seen:
                seen.append(nid)
    return seen


# ── Gantt replay (timeline from durable run events) ───────────────────────
#
# Rebuilds each node's [start, end) span from the event log (createdAt
# timestamps), then paints a horizontal, time-aligned ASCII chart. Row order
# follows node_order (declaration) so the replay reads top-to-bottom like
# the graph itself; spans that overlap (parallel work) render side by side.

_GANTT_CHAR = {
    STATUS_PENDING: "·",
    STATUS_RUNNING: "▓",
    STATUS_DONE: "=",
    STATUS_FAILED: "X",
    STATUS_PAUSED: "░",
}


def timeline_from_run(run: dict,
                      node_order: Optional[list[str]] = None) -> dict:
    """Per-node [start, end] spans + status, rebuilt from the event log.

    Returns {"spans": [{id, start, end, status}, ...], "t0": ..., "t1": ...}.
    start/end are floats (seconds epoch) or None for never-started nodes.
    """
    started: dict[str, float] = {}
    ended: dict[str, float] = {}
    status: dict[str, str] = {}
    last_ts: Optional[float] = None
    for ev in run.get("events") or []:
        ts = ev.get("createdAt")
        if not isinstance(ts, (int, float)):
            continue
        ts = float(ts)
        last_ts = ts if last_ts is None else max(last_ts, ts)
        payload = ev.get("payload") or {}
        node = payload.get("node") or payload.get("stepId")
        if not node:
            continue
        node = str(node)
        etype = ev.get("type")
        if etype in ("node_started", "step_started"):
            started.setdefault(node, ts)
            status[node] = STATUS_RUNNING
        elif etype in ("node_completed", "step_completed"):
            ended[node] = ts
            status[node] = STATUS_DONE
        elif etype in ("node_failed", "step_failed"):
            ended[node] = ts
            status[node] = STATUS_FAILED
    for nid in run.get("history") or []:
        status.setdefault(str(nid), STATUS_DONE)
    overall = str(run.get("status") or "")
    current = run.get("currentNode")
    if current:
        status.setdefault(str(current),
                          STATUS_FAILED if overall == "failed"
                          else STATUS_RUNNING)
    intr = run.get("pendingInterrupt")
    pnode = intr.get("node") if isinstance(intr, dict) else None
    if pnode:
        status[str(pnode)] = STATUS_PAUSED

    order = [str(n) for n in (node_order or [])]
    for nid in list(started) + list(status):
        if nid not in order:
            order.append(nid)

    spans: list[dict] = []
    for nid in order:
        s = started.get(nid)
        e = ended.get(nid)
        st = status.get(nid, STATUS_PENDING)
        if s is None:
            spans.append({"id": nid, "start": None, "end": None,
                          "status": st})
            continue
        if e is None:
            e = last_ts if (st == STATUS_RUNNING and last_ts is not None
                            and last_ts >= s) else s
        spans.append({"id": nid, "start": s, "end": e, "status": st})
    return {"spans": spans, "t0": None, "t1": None}


def render_gantt(timeline: dict, *, width: int = 100,
                 label_width: int = 0) -> str:
    """Horizontal, time-aligned ASCII Gantt chart for a timeline."""
    spans = [s for s in timeline.get("spans") or []]
    if not spans:
        return ""
    timed = [s for s in spans if s["start"] is not None]
    if not timed:
        # no timing data: fall back to a status-only listing
        return "\n".join(f"{status_icon(s['status'])} {s['id']}"
                         for s in spans)
    t0 = min(s["start"] for s in timed)
    t1 = max(s["end"] for s in timed)
    if t1 <= t0:
        t1 = t0 + 1.0
    lw = label_width or max(len(s["id"]) for s in spans) + 2
    bw = max(12, width - lw - 1)

    lines: list[str] = []
    # header: label column + time axis, total duration on the right
    lines.append(f"{'node':<{lw}} 0" + " " * (bw - 2) + _fmt_dur(t1 - t0))
    for s in spans:
        icon = status_icon(s["status"])
        label = f"{icon}{s['id']}"
        label = label[:lw]
        if s["start"] is None:
            bar = "·" * bw
        else:
            x0 = int((s["start"] - t0) / (t1 - t0) * (bw - 1))
            x1 = int((s["end"] - t0) / (t1 - t0) * (bw - 1))
            x1 = max(x1, x0)
            fill = _GANTT_CHAR.get(s["status"], "·")
            bar = ("·" * x0) + (fill * (x1 - x0 + 1)) + ("·" * (bw - x1 - 1))
            bar = bar[:bw]
        dur = ""
        if s["start"] is not None and s["end"] is not None:
            dur = _fmt_dur(s["end"] - s["start"])
        lines.append(f"{label:<{lw}} {bar} {dur}".rstrip())
    return "\n".join(lines)


def gantt_from_run(run: dict, node_order: Optional[list[str]] = None,
                   width: int = 100) -> str:
    """One-call Gantt replay for a durable workflow run (any kind)."""
    return render_gantt(timeline_from_run(run, node_order), width=width)
