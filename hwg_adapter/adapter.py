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
  node     : {"type": "node",     "id": str, "file": str, "manual": bool, "policy"?: dict}
  edge     : {"type": "edge",     "from": str, "to": str, "on": str|None, "maxLoops": int|None}
  schedule : {"type": "schedule", "days": list[str]|None, "start": str|None, "deadline": str|None, "tz": str|None}

Grammar:
  (find.hwo)#find#                  a node bound to a .hwo file (its "body")
  (find.hwo)#find# { retry: 2, timeout: "10m", cache: "1h" }   node policy
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
_RETRY_RE = re.compile(r'\bretry\s*:\s*(\d+)')


def _extract_on(content: str) -> Optional[str]:
    m = re.search(r'on\s*:\s*(?:"([^"]*)"|\'([^\']*)\'|([^,}]+))', content)
    if not m:
        return None
    return (m.group(1) or m.group(2) or m.group(3) or "").strip() or None


def _split_top_level(value: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    cur = ""
    depth = 0
    quote = ""
    for ch in value:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            cur += ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            if cur.strip():
                parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _parse_param(raw: str) -> dict:
    s = raw.strip()
    source = None
    default = None
    if "=" in s:
        left, right = s.split("=", 1)
        s = left.strip()
        default = right.strip() or None
        if default and (default.startswith("#") or default.startswith("$")):
            source = default
    if ":" in s:
        left, typ = s.split(":", 1)
        typ = typ.strip() or None
    else:
        left, typ = s, None
    left = left.strip()
    optional = left.endswith("?")
    name = (left[:-1] if optional else left).strip()
    return {"name": name, "type": typ, "optional": optional, "default": default, "source": source}


def _extract_call(content: str, keyword: str) -> Optional[str]:
    m = re.search(r"\b" + re.escape(keyword) + r"\s*\(", content)
    if not m:
        return None
    i = m.end()
    start = i
    depth = 1
    quote = ""
    while i < len(content):
        ch = content[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return content[start:i]
        i += 1
    return None


def _parse_io_block(content: str) -> dict:
    def read(kind: str) -> list:
        inner = _extract_call(content, kind)
        if inner is None:
            return []
        return [p for p in (_parse_param(x) for x in _split_top_level(inner)) if p["name"]]

    return {"in": read("in"), "out": read("out")}


def _extract_max_loops(content: str) -> Optional[int]:
    m = _MAXLOOPS_RE.search(content)
    if not m:
        return None
    n = int(m.group(1))
    return n if (isinstance(n, int) and n > 0) else None


def _extract_retry(content: str) -> Optional[int]:
    m = _RETRY_RE.search(content)
    if not m:
        return None
    return max(0, int(m.group(1)))


def _extract_policy(content: str) -> dict:
    policy: dict[str, Any] = {}
    retry = _extract_retry(content)
    timeout = _extract_string(content, "timeout")
    cache = _extract_string(content, "cache")
    if retry is not None:
        policy["retry"] = retry
    if timeout:
        policy["timeout"] = timeout
    if cache:
        policy["cache"] = cache
    return policy


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


def _validate_io_params(statements: list) -> list[str]:
    errors: list[str] = []

    def check(io: Optional[dict], path: str) -> None:
        if not io:
            return
        for kind in ("in", "out"):
            seen: set = set()
            for p in io.get(kind, []):
                name = p.get("name", "")
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", name):
                    errors.append(f'{path}: invalid {kind} parameter name "{name}".')
                if name in seen:
                    errors.append(f'{path}: duplicate {kind} parameter "{name}".')
                seen.add(name)

    for s in statements:
        if s["type"] == "graph":
            check(s.get("io"), "@graph")
        if s["type"] == "node":
            check(s.get("io"), f'#{s["id"]}#')
    return errors


def _node_outs(node: Optional[dict]) -> set:
    return {p.get("name") for p in (node or {}).get("io", {}).get("out", [])}


def _refs_from_io(io: Optional[dict]) -> list[tuple[str, str, bool]]:
    refs: list[tuple[str, str, bool]] = []
    for p in (io or {}).get("in", []):
        src = p.get("source") or p.get("default") or ""
        m = re.match(r"^#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)(\[-1\])?$", src)
        if m:
            refs.append((m.group(1), m.group(2), bool(m.group(3))))
    return refs


def _condition_field(on: Optional[str]) -> Optional[str]:
    if not on:
        return None
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*(?:==|!=|in\b|>=|<=|>|<)", on)
    return m.group(1) if m else None


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
            if self._starts("@graph"):
                statements.append(self._parse_graph())
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
        self._skip_ws()
        io = None
        if self._peek() == "[":
            io = _parse_io_block(self._read_bracket_content())
        self._skip_ws()
        policy = None
        if self._peek() == "{":
            policy = _extract_policy(self._read_brace_content())
        node = {"type": "node", "id": name, "file": file, "manual": manual}
        if io is not None:
            node["io"] = io
        if policy:
            node["policy"] = policy
        return node

    def _parse_graph(self) -> dict:
        self._expect("@graph")
        self._skip_ws()
        if self._peek() != "[":
            raise HwgParseError("@graph must be followed by [in(...), out(...)]", self.i)
        return {"type": "graph", "io": _parse_io_block(self._read_bracket_content())}

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

    def _read_bracket_content(self) -> str:
        start = self.i
        self._expect("[")
        inner_start = self.i
        depth = 1
        quote = ""
        while not self._eof():
            ch = self._peek()
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    content = self.source[inner_start:self.i]
                    self._expect("]")
                    return content
            self.i += 1
        raise HwgParseError("Unclosed [, expected ]", start)

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
    errors.extend(_validate_io_params(statements))

    # Unique node ids.
    by_id: dict[str, dict] = {}
    for node in nodes:
        if node["id"] in by_id:
            errors.append(f'Duplicate node id "#{node["id"]}#" — node ids must be unique.')
        else:
            by_id[node["id"]] = node
        policy = node.get("policy") or {}
        if policy.get("retry") is not None and (not isinstance(policy["retry"], int) or policy["retry"] < 0):
            errors.append(f'#{node["id"]}#: retry policy must be a non-negative integer.')

    # At most one schedule block.
    if len(schedules) > 1:
        errors.append("Multiple (schedule) blocks found — only one is allowed.")

    # Edges reference declared nodes.
    for edge in edges:
        if edge["from"] not in by_id:
            errors.append(f'Edge from #{edge["from"]}# references an undeclared node.')
        if edge["to"] not in by_id:
            errors.append(f'Edge to #{edge["to"]}# references an undeclared node.')

    available: dict[str, set] = {}
    for node in nodes:
        for ref_node, ref_field, previous in _refs_from_io(node.get("io")):
            outs = available.get(ref_node)
            if ref_node == node["id"] and previous:
                if ref_field not in _node_outs(node):
                    errors.append(f'#{node["id"]}#: input references undeclared previous output #{ref_node}.{ref_field}[-1].')
            elif outs is None:
                errors.append(f'#{node["id"]}#: input references #{ref_node}.{ref_field} before that node has completed.')
            elif ref_field not in outs:
                errors.append(f'#{node["id"]}#: input references undeclared output #{ref_node}.{ref_field}.')
        available[node["id"]] = _node_outs(node)

    for edge in edges:
        field = _condition_field(edge.get("on"))
        if not field:
            continue
        from_node = by_id.get(edge["from"])
        if from_node and field not in _node_outs(from_node):
            errors.append(f'Edge #{edge["from"]}# -> #{edge["to"]}# condition references "{field}", but #{edge["from"]}# does not declare it in out(...).')

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
