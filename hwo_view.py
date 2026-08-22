"""Interactive HWO lane viewer - swimlane TUI for .hwo workflows.

Reuses HwgViewer's whole interaction shell (unified selection model,
mouse hit-testing, scrolling, inspector pane) over the lane layout
from workflow_viz. Only the header/status fragments, the rebuild path,
and the inspector content are HWO-specific.

Element ids are step paths ("0.1.2") - the same ids hwo_runner emits -
so a live run's status maps onto lanes without translation.
"""

from __future__ import annotations

from typing import Optional

import workflow_viz
from hwg_view import HwgViewer, _crop


def _parent_path(nid: str) -> str:
    return nid.rsplit(".", 1)[0] if "." in nid else ""


class HwoLaneViewer(HwgViewer):
    """One full-screen swimlane viewing session for an .hwo workflow."""

    def _header_fragments(self):
        view = self.view
        status_bit = ""
        if self.status:
            done = sum(1 for s in self.status.values()
                       if s == workflow_viz.STATUS_DONE)
            status_bit = f"  {done}/{len(self.status)} done"
        return [
            ("class:header", "  HWO LANES"),
            ("class:header.path", f"  {self.path}"),
            ("class:dim", f"  {view.meta.get('counts', '')}{status_bit}"),
        ]

    def _rebuild(self) -> None:
        """Rebuild the lane layout with current status (AST is the source)."""
        if not self.statements:
            return
        tree = workflow_viz.tree_from_steps(self.statements, self.status)
        self.view = workflow_viz.build_lane_view(tree)
        self._total = self.view.canvas.height
        if self.selected not in self.view.nodes_by_id:
            self.selected = (self.view.node_order[0]
                             if self.view.node_order else "")
        self._scroll_to_node(self.selected)

    def _inspector_fragments(self):
        view = self.view
        el = view.node(self.selected)
        if el is None:
            return [("class:dim", "  Select a node\n")]
        kind, node = el["kind"], el["node"]
        out = [
            ("class:inspector.title", "  INSPECTOR\n"),
            ("class:dim", "  " + "─" * 30 + "\n"),
            ("class:inspector.label", "  STEP\n"),
            ("class:nid", f"  {el['id']}  ({kind})\n"),
        ]
        if kind == "agent":
            out += [("class:inspector.label", "  AGENT\n"),
                    ("class:nid", f"  #{node.get('name', '')}#\n")]
            if node.get("model"):
                out.append(("class:inspector.value",
                            f"  model {node['model']}\n"))
            if node.get("promptFile"):
                out += [("class:inspector.label", "  PROMPT\n"),
                        ("class:inspector.value",
                         f"  {node['promptFile']}\n")]
            io = node.get("io") or {}
            for pkind in ("in", "out"):
                params = io.get(pkind) or []
                if not params:
                    continue
                out.append(("class:inspector.label",
                            f"  {pkind.upper()}\n"))
                for p in params:
                    text = f"  {p.get('name', '')}"
                    if p.get("type"):
                        text += f": {p['type']}"
                    if p.get("source") or p.get("default"):
                        text += f" = {p.get('source') or p.get('default')}"
                    out.append(("class:inspector.value",
                                _crop(text, 34) + "\n"))
            if el.get("children"):
                out.append(("class:dim",
                            f"  {len(el['children'])} body step(s)\n"))
        elif kind == "task":
            text = " ".join(str(node.get("text") or "").split())
            out += [("class:inspector.label", "  TASK\n"),
                    ("class:inspector.value", "  " + _crop(text, 34) + "\n")]
        elif kind == "parallel":
            out += [("class:inspector.label", "  PARALLEL\n"),
                    ("class:inspector.value",
                     f"  {len(el.get('children') or [])} member(s)\n")]
        parent = _parent_path(el["id"])
        if parent and parent in view.nodes_by_id:
            pel = view.nodes_by_id[parent]
            label = (f"#{pel['node'].get('name', parent)}#"
                     if pel["kind"] == "agent" else parent)
            out += [("class:inspector.label", "  PARENT\n"),
                    ("class:dim", "  " + _crop(label, 34) + "\n")]
        incoming = view.in_edges.get(self.selected, [])
        outgoing = view.out_edges.get(self.selected, [])
        if incoming:
            out.append(("class:inspector.label", "  PREV\n"))
            for e in incoming:
                out.append(("class:dim",
                            "  " + _crop(e["from"], 34) + "\n"))
        if outgoing:
            out.append(("class:inspector.label", "  NEXT\n"))
            for e in outgoing:
                out.append(("class:flow.label",
                            "  " + _crop(e["to"], 34) + "\n"))
        return out

    def _status_fragments(self):
        if self.show_help:
            return [("class:help",
                     "  ↑↓/jk select · →/l next/dive · ←/h back · "
                     "PgUp/PgDn scroll · Tab cycle status · Esc close")]
        counts = self.view.meta.get("counts", "")
        return [
            ("class:statusbar",
             f"  ↑↓/jk select · →/l next/dive · ←/h back · "
             f"PgUp/PgDn scroll · q quit · ? help · {counts}"),
        ]


# ── Public entry ──────────────────────────────────────────────────────

def open_lane_viewer(path: str, *, status: Optional[dict[str, str]] = None,
                     input=None, output=None) -> bool:
    """Parse an .hwo file, build the lane view, run one full-screen session.

    Returns True when the file parsed and a session ran; False (with the
    caller printing the message) when parsing failed.
    """
    from pathlib import Path
    from hwo_adapter import parse, validate

    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        print(f"hwo view: cannot read '{path}': {e}")
        return False
    try:
        steps = parse(source)
    except Exception as e:
        print(f"hwo view: parse error - {e}")
        return False
    errors = validate(steps)
    if errors:
        print("hwo view: validation errors:\n" + "\n".join(errors))
        return False

    tree = workflow_viz.tree_from_steps(steps, status)
    view = workflow_viz.build_lane_view(tree)
    if not view.node_order:
        print("hwo view: workflow has no steps")
        return False
    HwoLaneViewer(path, view, status=status, statements=steps,
                  input=input, output=output).run()
    return True
