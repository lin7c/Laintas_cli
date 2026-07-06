"""HWG flowchart language — canonical grammar (parser + validation).

Source of truth for the HWG DSL shared by laintas_cli (Python) and Helpwo (TS).
This module is PURE: it turns a ``.hwg`` source string into a JSON-serialisable
AST and validates it. NO I/O, NO graph runtime — each product keeps its own
executor that walks this AST.

This is the Python mirror of ``Helpwo/src/tools/hwg-core.ts`` and MUST stay
behaviourally identical.

Distribution (vendored):
  laintas_cli -> hwg_core.py                (this file)
  Helpwo      -> src/tools/hwg-core.ts      (the TS canonical source)

Canonical AST (JSON):
  node     : {"type": "node",     "id": str, "file": str, "manual": bool}
  edge     : {"type": "edge",     "from": str, "to": str, "on": str|None, "maxLoops": int|None}
  schedule : {"type": "schedule", "days": list[str]|None, "start": str|None, "deadline": str|None, "tz": str|None}

Grammar:
  (find.hwo)#find#                  a node bound to a .hwo file (its "body")
  !(review.hwo)#review#             a manual (human) node — run pauses here
  #find# -> #analyze#               an edge: find then analyze
  #review# -> { on: PASS } #report#                  conditional edge
  #review# -> { on: FAIL, maxLoops: 2 } #analyze#    loop-back edge (bounded)
  (schedule) { start: "09:00", days: ["Mon","Fri"], tz: "Asia/Shanghai" }   schedule block
  ```...```                         comment block
  ->                                no-op separator (ignored)

Node ids must be unique. Every edge endpoint must reference a declared node.
A node with >1 outgoing edge is a branch — every outgoing edge must then carry
`on:`. A self-loop (from === to) must carry `maxLoops`. At least one start node
(no incoming edges) and one end node (no outgoing edges) must exist.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# AST nodes are plain dicts (see module docstring).
HwgStatement = dict


class HwgParseError(Exception):
    """Raised on invalid HWG syntax. ``index`` is the character offset."""

    def __init__(self, message: str, index: int):
        super().__init__(f"{message} at {index}")
        self.index = index


# ── Metadata extractors ─────────────────────────────────────────────────

_ON_RE = re.compile(r'on\s*:\s*(?:"([^"]*)"|\'([^\']*)\'|([A-Za-z_][A-Za-z0-9_-]*))')
_MAXLOOPS_RE = re.compile(r'maxLoops\s*:\s*(\d+)')
_DAYS_RE = re.compile(r'days\s*:\s*\[([^\]]*)\]')


def _extract_on(content: str) -> Optional[str]:
    m = _ON_RE.search(content)
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3) or None


def _extract_max_loops(content: str) -> Optional[int]:
    m = _MAXLOOPS_RE.search(content)
    if not m:
        return None
    n = int(m.group(1))
    return n if (isinstance(n, int) and n > 0) else None


def _extract_string(content: str, key: str) -> Optional[str]:
    re_str = re.compile(key + r'\s*:\s*(?:"([^"]*)"|\'([^\']*)\')')
    m = re_str.search(content)
    if not m:
        return None
    return m.group(1) or m.group(2) or None


def _extract_days(content: str) -> Optional[list[str]]:
    m = _DAYS_RE.search(content)
    if not m:
        return None
    parts = m.group(1).split(",")
    return [p.strip().strip('"\x27') for p in parts if p.strip()] or None


# ── Parser ──────────────────────────────────────────────────────────────


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.i = 0

    # ── public ──
    def parse(self) -> list:
        statements: list = []
        while not self._eof():
            self._skip_ws()
            if self._eof():
                break

            if self._starts("```"):
                self._skip_comment_block()
                continue
            if self._starts("->"):
                self.i += 2
                continue
            if self._peek() == "!":
                statements.append(self._parse_manual_node())
                continue
            if self._peek() == "(":
                label = self._read_paren()
                self._skip_ws()
                if self._peek() == "#":
                    statements.append(self._parse_node_tail(label, False))
                elif self._peek() == "{":
                    statements.append(self._parse_schedule_block(label))
                else:
                    raise HwgParseError(f'Expected #name# or {{ after ({label})', self.i)
                continue
            if self._peek() == "#":
                statements.append(self._parse_edge())
                continue

            snippet = self.source[self.i:self.i + 16]
            raise HwgParseError(f'Unexpected token "{snippet}"', self.i)
        return statements

    # ── Node declarations ──
    def _parse_manual_node(self) -> dict:
        start = self.i
        self._expect("!")
        self._skip_ws()
        if self._peek() != "(":
            raise HwgParseError("A manual node must be !(file.hwo)#name#", start)
        file = self._read_paren()
        self._skip_ws()
        return self._parse_node_tail(file, True)

    def _parse_node_tail(self, file: str, manual: bool) -> dict:
        start = self.i
        self._expect("#")
        name = self._read_name(start)
        self._expect("#")
        if not file:
            raise HwgParseError("Empty file binding — use (file.hwo)#name#", start)
        return {"type": "node", "id": name, "file": file, "manual": manual}

    # ── Edges ──
    def _parse_edge(self) -> dict:
        start = self.i
        self._expect("#")
        from_id = self._read_name(start)
        self._expect("#")
        self._skip_ws()
        if not self._starts("->"):
            raise HwgParseError(
                f'Expected -> after #{from_id}# (a bare #name# must start an edge: #from# -> #to#)',
                self.i,
            )
        self._expect("->")
        self._skip_ws()

        on: Optional[str] = None
        max_loops: Optional[int] = None
        if self._peek() == "{":
            meta = self._read_brace_content()
            self._skip_ws()
            on = _extract_on(meta)
            max_loops = _extract_max_loops(meta)

        if self._peek() != "#":
            raise HwgParseError(
                f'Expected target #name# after -> in edge from #{from_id}#',
                self.i,
            )
        self._expect("#")
        to_id = self._read_name(start)
        self._expect("#")
        return {"type": "edge", "from": from_id, "to": to_id, "on": on, "maxLoops": max_loops}

    # ── Schedule block ──
    def _parse_schedule_block(self, label: str) -> dict:
        start = self.i
        if label.strip().lower() != "schedule":
            raise HwgParseError(
                f'Unknown block "({label})" — only (schedule) {{ ... }} is recognised',
                start,
            )
        content = self._read_brace_content()
        return {
            "type": "schedule",
            "days": _extract_days(content),
            "start": _extract_string(content, "start"),
            "deadline": _extract_string(content, "deadline"),
            "tz": _extract_string(content, "tz"),
        }

    # ── Shared readers ──
    def _read_paren(self) -> str:
        start = self.i
        self._expect("(")
        inner_start = self.i
        while not self._eof() and self._peek() != ")":
            self.i += 1
        if self._eof():
            raise HwgParseError("Unclosed (, expected )", start)
        content = self.source[inner_start:self.i].strip()
        self._expect(")")
        return content

    def _read_brace_content(self) -> str:
        start = self.i
        self._expect("{")
        inner_start = self.i
        in_str = False
        quote = ""
        while not self._eof():
            c = self._peek()
            if in_str:
                if c == quote:
                    in_str = False
                self.i += 1
            else:
                if c in ('"', "'"):
                    in_str = True
                    quote = c
                    self.i += 1
                elif c == "}":
                    break
                else:
                    self.i += 1
        if self._eof():
            raise HwgParseError("Unclosed {, expected }", start)
        content = self.source[inner_start:self.i]
        self._expect("}")
        return content

    def _read_name(self, start: int) -> str:
        name_start = self.i
        while not self._eof() and self._peek() != "#":
            self.i += 1
        if self._eof():
            raise HwgParseError("Unclosed name, expected #", start)
        name = self.source[name_start:self.i].strip()
        if not name:
            raise HwgParseError("Empty name", start)
        return name

    def _skip_comment_block(self) -> None:
        self._expect("```")
        end = self.source.find("```", self.i)
        if end == -1:
            raise HwgParseError("Unclosed comment block, expected ```", self.i)
        self.i = end + 3

    # ── primitives ──
    def _skip_ws(self) -> None:
        while not self._eof() and self.source[self.i].isspace():
            self.i += 1

    def _starts(self, text: str) -> bool:
        return self.source.startswith(text, self.i)

    def _expect(self, text: str) -> None:
        if not self._starts(text):
            raise HwgParseError(f'Expected "{text}"', self.i)
        self.i += len(text)

    def _peek(self) -> str:
        return self.source[self.i] if self.i < len(self.source) else ""

    def _eof(self) -> bool:
        return self.i >= len(self.source)


def parse(source: str) -> list:
    """Parse HWG ``source`` into the canonical AST (a flat list of statements)."""
    return _Parser(source).parse()


# ── Validation ──────────────────────────────────────────────────────────


def validate(statements: list) -> list[str]:
    """Return a list of human-readable validation error strings (empty = OK)."""
    errors: list[str] = []

    nodes = [s for s in statements if s["type"] == "node"]
    edges = [s for s in statements if s["type"] == "edge"]
    schedules = [s for s in statements if s["type"] == "schedule"]

    # Unique node ids.
    by_id: dict[str, dict] = {}
    for node in nodes:
        if node["id"] in by_id:
            errors.append(f'Duplicate node id "#{node["id"]}#" — node ids must be unique.')
        else:
            by_id[node["id"]] = node

    # At most one schedule block.
    if len(schedules) > 1:
        errors.append("Multiple (schedule) blocks found — only one is allowed.")

    # Edges reference declared nodes.
    for edge in edges:
        if edge["from"] not in by_id:
            errors.append(f'Edge from #{edge["from"]}# references an undeclared node.')
        if edge["to"] not in by_id:
            errors.append(f'Edge to #{edge["to"]}# references an undeclared node.')

    # Self-loops require maxLoops.
    for edge in edges:
        if edge["from"] == edge["to"] and not edge.get("maxLoops"):
            errors.append(
                f'Self-loop #{edge["from"]}# -> #{edge["to"]}# requires maxLoops to bound the loop.'
            )

    # Cycles must be bounded: after removing every maxLoops edge, no multi-node
    # cycle may remain (every cycle must contain at least one bounded back-edge,
    # or it can loop forever at runtime).
    if _has_unbounded_cycle(nodes, edges):
        errors.append(
            "Graph has a cycle with no bounded edge — add maxLoops to a loop-back "
            "edge so the loop terminates."
        )

    # Branching: >1 outgoing edge => every outgoing edge must carry `on`.
    outgoing: dict[str, list] = {}
    for edge in edges:
        outgoing.setdefault(edge["from"], []).append(edge)
    for from_id, outs in outgoing.items():
        if len(outs) > 1:
            unconditional = [e for e in outs if not e.get("on")]
            if unconditional:
                errors.append(
                    f'Node #{from_id}# has {len(outs)} outgoing edges (a branch); every branch '
                    f'edge must carry a condition (on:), but {len(unconditional)} are unconditional.'
                )

    # At least one start and one end node.
    if nodes:
        has_incoming = set(e["to"] for e in edges)
        has_outgoing = set(e["from"] for e in edges)
        starts = [n for n in nodes if n["id"] not in has_incoming]
        ends = [n for n in nodes if n["id"] not in has_outgoing]
        if not starts:
            errors.append("No start node — every node has an incoming edge (cycle with no entry point).")
        elif len(starts) > 1:
            ids = ", ".join(f'#{n["id"]}#' for n in starts)
            errors.append(
                f"Multiple start nodes ({ids}) — HWG runs a single entry point. "
                f"Connect them into one chain, or split into separate flowcharts."
            )
        if not ends:
            errors.append("No end node — every node has an outgoing edge (cycle with no exit point).")

    return errors


def _has_unbounded_cycle(nodes: list, edges: list) -> bool:
    """True if a cycle exists using only unbounded, non-self edges.

    Self-loops are covered by the dedicated maxLoops-on-self-loop rule, so they
    are excluded here. Because every ``maxLoops`` edge is removed before the
    search, a cycle only survives when it contains no bounded edge — i.e. it has
    nothing to terminate it. The result is independent of traversal order.
    """
    adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e.get("maxLoops") or e["from"] == e["to"]:
            continue
        if e["from"] in adj and e["to"] in adj:
            adj[e["from"]].append(e["to"])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n["id"]: WHITE for n in nodes}

    def visit(u: str) -> bool:
        color[u] = GRAY
        for v in adj[u]:
            if color[v] == GRAY or (color[v] == WHITE and visit(v)):
                return True
        color[u] = BLACK
        return False

    return any(color[n["id"]] == WHITE and visit(n["id"]) for n in nodes)


def as_graph(statements: list) -> dict[str, Any]:
    """Collect nodes/edges/schedule from a parsed statement list (convenience)."""
    schedule = next((s for s in statements if s["type"] == "schedule"), None)
    return {
        "nodes": [s for s in statements if s["type"] == "node"],
        "edges": [s for s in statements if s["type"] == "edge"],
        "schedule": schedule,
    }
