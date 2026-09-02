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
  #plan# => #lint#  /  #plan# => #test#   fan-out: every => branch runs
  (merge.hwo)#merge# { join: "all" }      where the fan-out branches converge
  @include "contracts.hwg"          splice another file's declarations here
  (schedule) { start: "09:00", days: ["Mon","Fri"], tz: "Asia/Shanghai" }   schedule block
  ```...```                         comment block
  ->                                no-op separator (ignored)

Node ids must be unique. Every edge endpoint must reference a declared node.
A node with >1 outgoing edge is a branch — every outgoing edge must then carry
`on:`. `=>` edges are a fan-out instead: all of them run, and they must converge
on one node declaring `{ join: "all" }`.
A self-loop (from === to) must carry `maxLoops`. At least one start node
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

_MAXLOOPS_RE = re.compile(r'maxLoops\s*:\s*(\d+)')
_DAYS_RE = re.compile(r'days\s*:\s*\[([^\]]*)\]')
_RETRY_RE = re.compile(r'\bretry\s*:\s*(\d+)')


def _extract_on(content: str) -> Optional[str]:
    # Split metadata into top-level key/value pairs (respecting brackets and
    # quotes) so a value like `status in [PASS, FAIL]` is not split at its
    # inner comma. The `on:` key is matched anchored at the start of a pair so
    # it cannot be confused with a substring of another key (e.g. `action:`).
    for part in _split_top_level(content):
        m = re.match(r'^on\s*:\s*(.+)$', part, re.I)
        if m:
            val = m.group(1).strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            return val or None
    return None


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


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.*?-]*$")

#: `(tool:generate_image)#hero#` — a node bound to a single tool call instead of
#: a .hwo workflow.
#:
#: A .hwo node is an agent: it costs a model call, takes as long as it takes,
#: and its output is whatever the agent decided to return. That is the right
#: shape for judgement and the wrong one for a step that is a function — resize
#: this image, generate that clip. A tool node is the deterministic half, and it
#: is what makes a graph re-runnable: same inputs, same result, so its output can
#: be cached on its inputs rather than on the whole workspace.
#:
#: The binding is kept in `file` verbatim so every existing consumer (summary,
#: visualiser, cache key) keeps working, with `tool` as the parsed name. The
#: pattern deliberately forbids a dot, so an ordinary path like `tool:x.hwo`
#: stays a file binding and nothing that parses today changes meaning.
_TOOL_BINDING_RE = re.compile(r"^tool:([A-Za-z_][A-Za-z0-9_]*)$")


def tool_binding(file: str) -> str:
    """The tool name a node binds, or "" when it binds a .hwo file."""
    match = _TOOL_BINDING_RE.match(str(file or "").strip())
    return match.group(1) if match else ""


def _extract_tools(content: str) -> Optional[list]:
    """`tools: [fs.read, fs.grep]` — the tools this node's agents may call.

    Names or fnmatch globs. Always a *narrowing*: it can remove access, never
    grant it, so it composes with every other restriction already in force.
    """
    m = re.search(r"\btools\s*:\s*\[([^\]]*)\]", content)
    if m is None:
        return None
    names = []
    for raw in m.group(1).split(","):
        name = raw.strip().strip('"').strip("'")
        if name:
            names.append(name)
    return names


def _extract_policy(content: str) -> dict:
    policy: dict[str, Any] = {}
    retry = _extract_retry(content)
    timeout = _extract_string(content, "timeout")
    cache = _extract_string(content, "cache")
    join = _extract_string(content, "join")
    tools = _extract_tools(content)
    if retry is not None:
        policy["retry"] = retry
    if timeout:
        policy["timeout"] = timeout
    if cache:
        policy["cache"] = cache
    if join:
        policy["join"] = join
    if tools is not None:
        policy["tools"] = tools
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
    days: list[str] = []
    for p in m.group(1).split(","):
        p = p.strip()
        p = re.sub(r'^["\']|["\']$', '', p)
        if p:
            days.append(p)
    return days or None


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
        m = re.match(r"^#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)(\[-1\])?#$", src)
        if m:
            refs.append((m.group(1), m.group(2), bool(m.group(3))))
    return refs


def _condition_field(on: Optional[str]) -> Optional[str]:
    """Left-hand field of a single-atom condition (legacy shape).

    Still used as the validation fallback for conditions parse_condition()
    does not cover, so a typo like `score >= a b` keeps its field check.
    """
    if not on:
        return None
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*(?:==|!=|in\b|>=|<=|>|<)", on)
    return m.group(1) if m else None


# ── Edge conditions ─────────────────────────────────────────────────────
#
# The original grammar allowed exactly one atom per edge:
#
#   PASS                     bare verdict
#   verdict == "PASS"        comparison against a declared output
#   score >= 3               numeric comparison
#   status in [OK, WARN]     membership
#
# Atoms may now be composed with and/or/not (&&/||/!) and grouped with
# parentheses, and a filesystem predicate is available so a branch can turn on
# a fact rather than on a model's word:
#
#   exists(dist/app.js) and tests == "PASS"
#   not exists("$input.report") or force == true
#
# This layer stays pure: exists() is recognised here and evaluated by each
# product's runtime. parse_condition() returns None for anything the structured
# grammar does not cover, and callers then fall back to the legacy "compare the
# whole string against the verdict" behaviour — which is what keeps every
# pre-existing condition working unchanged.

_COND_TOKEN_RE = re.compile(
    r"""(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<op>==|!=|>=|<=|>|<)
      | (?P<andor>&&|\|\|)
      | (?P<bang>!)
      | (?P<list>\[[^\]]*\])
      | (?P<str>"[^"]*"|'[^']*')
      | (?P<word>[^\s()\[\]!<>=&|]+)
    )""",
    re.X,
)

# A condition using any of these is *meant* to be structured, so failing to
# parse it is a compile error rather than a silent fall back to verdict compare.
_COND_STRUCTURED_RE = re.compile(r"(?i)(?:\bexists\s*\(|&&|\|\||\bnot\b|\band\b|\bor\b|^\s*!|\()")

_COND_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class _CondError(Exception):
    """Internal: this string is not a structured condition."""


def _tokenize_condition(on: str) -> list:
    tokens: list = []
    i = 0
    while i < len(on):
        if on[i].isspace():
            i += 1
            continue
        m = _COND_TOKEN_RE.match(on, i)
        if not m:
            raise _CondError(f"unexpected character {on[i]!r}")
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
        i = m.end()
    return tokens


class _CondParser:
    """Recursive descent over the token list. Precedence: not > and > or."""

    def __init__(self, tokens: list):
        self.tokens = tokens
        self.i = 0

    def _peek(self) -> tuple:
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def _next(self) -> tuple:
        token = self._peek()
        self.i += 1
        return token

    def _is_keyword(self, word: str) -> bool:
        kind, value = self._peek()
        return kind == "word" and value.lower() == word

    def parse(self) -> dict:
        node = self._parse_or()
        if self.i != len(self.tokens):
            raise _CondError("unexpected trailing input")
        return node

    def _parse_or(self) -> dict:
        items = [self._parse_and()]
        while True:
            kind, value = self._peek()
            if (kind == "andor" and value == "||") or self._is_keyword("or"):
                self._next()
                items.append(self._parse_and())
            else:
                break
        return items[0] if len(items) == 1 else {"kind": "or", "items": items}

    def _parse_and(self) -> dict:
        items = [self._parse_unary()]
        while True:
            kind, value = self._peek()
            if (kind == "andor" and value == "&&") or self._is_keyword("and"):
                self._next()
                items.append(self._parse_unary())
            else:
                break
        return items[0] if len(items) == 1 else {"kind": "and", "items": items}

    def _parse_unary(self) -> dict:
        kind, _value = self._peek()
        if kind == "bang" or self._is_keyword("not"):
            self._next()
            return {"kind": "not", "item": self._parse_unary()}
        return self._parse_primary()

    def _parse_primary(self) -> dict:
        kind, value = self._next()
        if kind == "lparen":
            node = self._parse_or()
            if self._peek()[0] != "rparen":
                raise _CondError("missing )")
            self._next()
            return node
        if kind != "word":
            raise _CondError(f"unexpected token {value!r}")
        if value.lower() == "exists" and self._peek()[0] == "lparen":
            self._next()
            path_kind, path_value = self._next()
            if path_kind not in ("word", "str"):
                raise _CondError("exists() needs a path")
            if self._peek()[0] != "rparen":
                raise _CondError("missing ) after exists(")
            self._next()
            return {"kind": "exists", "path": _strip_quotes(path_value)}
        next_kind, next_value = self._peek()
        if next_kind == "op":
            if not _COND_FIELD_RE.match(value):
                raise _CondError(f"{value!r} is not a field name")
            self._next()
            operand_kind, operand_value = self._next()
            if operand_kind not in ("word", "str"):
                raise _CondError("comparison needs a value")
            return {"kind": "cmp", "field": value, "op": next_value, "value": operand_value}
        if next_kind == "word" and next_value == "in":
            if not _COND_FIELD_RE.match(value):
                raise _CondError(f"{value!r} is not a field name")
            self._next()
            list_kind, list_value = self._next()
            if list_kind != "list":
                raise _CondError("in needs a [list]")
            inner = list_value[1:-1].strip()
            return {"kind": "in", "field": value,
                    "values": [v.strip() for v in inner.split(",")] if inner else []}
        if value.lower() in ("and", "or", "not", "in"):
            raise _CondError(f"{value!r} is a keyword, not a verdict")
        return {"kind": "verdict", "value": value}


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_condition(on: Optional[str]) -> Optional[dict]:
    """Structured form of an edge condition, or None if it has none.

    None is not an error: a bare verdict like `NEEDS_WORK` parses, but anything
    outside the grammar (`on: some free text`) returns None so runtimes keep
    their legacy verdict-string comparison.
    """
    if not on or not on.strip():
        return None
    try:
        return _CondParser(_tokenize_condition(on.strip())).parse()
    except _CondError:
        return None


def condition_error(on: Optional[str]) -> Optional[str]:
    """Compile error for a condition that reaches for the structured grammar
    (exists/and/or/not/parens) and gets it wrong. Plain verdicts return None."""
    if not on or parse_condition(on) is not None:
        return None
    if not _COND_STRUCTURED_RE.search(on):
        return None
    return (f'condition "{on.strip()}" is not valid. Use atoms '
            f'(PASS, field == "x", score >= 3, field in [A, B], exists(path)) '
            f'combined with and/or/not and ().')


def condition_fields(node: Optional[dict]) -> list:
    """Every output field a parsed condition reads."""
    if not node:
        return []
    kind = node.get("kind")
    if kind in ("and", "or"):
        fields = []
        for item in node.get("items", []):
            for field in condition_fields(item):
                if field not in fields:
                    fields.append(field)
        return fields
    if kind == "not":
        return condition_fields(node.get("item"))
    if kind in ("cmp", "in"):
        return [node["field"]]
    return []


def condition_exists_refs(node: Optional[dict]) -> list:
    """#node.field# references appearing inside exists() paths."""
    if not node:
        return []
    kind = node.get("kind")
    if kind in ("and", "or"):
        refs = []
        for item in node.get("items", []):
            refs.extend(condition_exists_refs(item))
        return refs
    if kind == "not":
        return condition_exists_refs(node.get("item"))
    if kind == "exists":
        return [(m.group(1), m.group(2)) for m in
                re.finditer(r"#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)#", node.get("path", ""))]
    return []


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
            if self._starts("@include"):
                statements.append(self._parse_include())
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
        if tool_binding(file):
            # A manual node is a pause for a person, and what resumes it is a
            # verdict they supply. A tool call has nothing to pause for.
            raise HwgParseError(
                "A manual node must bind a .hwo file, not a tool", start)
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
        tool = tool_binding(file)
        if tool:
            node["tool"] = tool
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

    # ── Includes ──
    def _parse_include(self) -> dict:
        self._expect("@include")
        self._skip_ws()
        quote = self._peek()
        if quote not in ('"', "'"):
            raise HwgParseError('@include must be followed by a quoted path, e.g. @include "contracts.hwg"', self.i)
        self.i += 1
        start = self.i
        while not self._eof() and self._peek() != quote:
            self.i += 1
        if self._eof():
            raise HwgParseError("Unterminated @include path", start)
        path = self.source[start:self.i]
        self.i += 1
        return {"type": "include", "path": path}

    # ── Edges ──
    def _parse_edge(self) -> dict:
        start = self.i
        self._expect("#")
        from_id = self._read_name(start)
        self._expect("#")
        self._skip_ws()
        # `->` routes to exactly one target; `=>` fans out to all its targets.
        fanout = self._starts("=>")
        if fanout:
            self._expect("=>")
        elif self._starts("->"):
            self._expect("->")
        else:
            raise HwgParseError(
                f'Expected -> or => after #{from_id}# (a bare #name# must start an edge: #from# -> #to#)',
                self.i,
            )
        self._skip_ws()

        on: Optional[str] = None
        max_loops: Optional[int] = None
        if self._peek() == "{":
            meta = self._read_brace_content()
            self._skip_ws()
            on = _extract_on(meta)
            max_loops = _extract_max_loops(meta)

        arrow = "=>" if fanout else "->"
        if self._peek() != "#":
            raise HwgParseError(
                f'Expected target #name# after {arrow} in edge from #{from_id}#',
                self.i,
            )
        self._expect("#")
        to_id = self._read_name(start)
        self._expect("#")
        edge = {"type": "edge", "from": from_id, "to": to_id, "on": on, "maxLoops": max_loops}
        if fanout:
            edge["fanout"] = True
        return edge

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


# ── Includes ────────────────────────────────────────────────────────────
#
#   @include "contracts.hwg"
#
# Splices another file's statements in place. It exists so a set of node
# declarations — the contracts several graphs agree on — can be written once
# and wired differently by each graph.
#
# This module stays pure: parse() emits the include statement, and
# resolve_includes() does the splicing through a reader callback the caller
# supplies. Validation refuses to run on an unresolved include rather than
# quietly validating a graph with a hole in it.

MAX_INCLUDE_DEPTH = 10


def _normalise_path(path: str) -> str:
    """POSIX-style normalisation, identical in both languages.

    Not os.path: the Python and TS mirrors must agree byte for byte, and the
    TS side runs against a virtual filesystem with no OS path module.
    """
    absolute = path.startswith("/")
    parts: list = []
    for part in path.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append("..")
            continue
        parts.append(part)
    joined = "/".join(parts)
    return ("/" + joined) if absolute else joined


def _resolve_relative(base_file: str, path: str) -> str:
    """`path` as written inside `base_file`, resolved against its directory."""
    if path.startswith("/"):
        return _normalise_path(path)
    cut = base_file.rfind("/")
    directory = base_file[:cut] if cut != -1 else ""
    return _normalise_path(f"{directory}/{path}" if directory else path)


def include_targets(statements: list, path: str) -> list:
    """Resolved paths the @includes in ``statements`` point at.

    Exists for callers whose file reads are asynchronous: they walk this to
    preload every referenced file, then hand resolve_includes a synchronous
    lookup over what they loaded. Keeping the traversal here means there is
    only one definition of how an include path resolves.
    """
    targets = []
    for statement in statements:
        if statement.get("type") != "include":
            continue
        target = _resolve_relative(path, statement.get("path") or "")
        if target and target not in targets:
            targets.append(target)
    return targets


def resolve_includes(statements: list, path: str, read,
                     _stack: Optional[list] = None, _seen: Optional[set] = None) -> tuple:
    """Splice every @include, depth-first. Returns (statements, errors).

    ``read(resolved_path)`` returns the file's source, or None when it does not
    exist.

    A file is spliced at most once per resolution, so the diamond case (two
    libraries that both include the same contracts file) is not a duplicate-id
    error. A file that includes itself, directly or through a chain, is a cycle
    and is reported rather than quietly dropped.
    """
    stack = list(_stack or [_normalise_path(path)])
    seen = _seen if _seen is not None else {_normalise_path(path)}
    if len(stack) > MAX_INCLUDE_DEPTH:
        return [], [f'@include nesting deeper than {MAX_INCLUDE_DEPTH} levels (from "{path}").']

    resolved: list = []
    errors: list = []
    for statement in statements:
        if statement.get("type") != "include":
            resolved.append(statement)
            continue
        target = _resolve_relative(path, statement.get("path") or "")
        if not target:
            errors.append(f'@include in "{path}" has an empty path.')
            continue
        if target in stack:
            chain = " -> ".join([*stack, target])
            errors.append(f"@include cycle: {chain}.")
            continue
        if target in seen:
            continue  # already spliced on another branch (diamond)
        seen.add(target)
        try:
            source = read(target)
        except Exception as e:  # a reader is product code; never let it escape
            source = None
            errors.append(f'@include "{target}" could not be read: {e}.')
            continue
        if source is None:
            errors.append(f'@include "{target}" not found (included from "{path}").')
            continue
        try:
            inner = parse(source)
        except HwgParseError as e:
            errors.append(f'@include "{target}" failed to parse: {e}')
            continue
        inner_statements, inner_errors = resolve_includes(
            inner, target, read, [*stack, target], seen)
        resolved.extend(inner_statements)
        errors.extend(inner_errors)
    return resolved, errors


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

    for statement in statements:
        if statement.get("type") == "include":
            errors.append(
                f'@include "{statement.get("path")}" was not resolved — '
                f'includes must be spliced in before the graph is validated.')

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
        tools = policy.get("tools")
        if tools is not None and node.get("tool"):
            # tools: narrows the agents inside a node. A tool node has no
            # agents, so accepting the key would announce a containment that
            # does not exist.
            errors.append(
                f'#{node["id"]}#: tools: applies to the agents in a .hwo node; '
                f'a tool node calls {node["tool"]} and nothing else.')
        elif tools is not None:
            if not tools:
                errors.append(
                    f'#{node["id"]}#: tools: [] would leave the node with nothing to '
                    f'call. Omit the key to inherit the full set.')
            for name in tools:
                if not _TOOL_NAME_RE.match(name):
                    errors.append(
                        f'#{node["id"]}#: "{name}" is not a tool name or glob '
                        f'(e.g. fs.read, fs.*).')

    # At most one schedule block.
    if len(schedules) > 1:
        errors.append("Multiple (schedule) blocks found — only one is allowed.")

    # Edges reference declared nodes.
    for edge in edges:
        if edge["from"] not in by_id:
            errors.append(f'Edge from #{edge["from"]}# references an undeclared node.')
        if edge["to"] not in by_id:
            errors.append(f'Edge to #{edge["to"]}# references an undeclared node.')

    # I/O references: a node input may read another node's declared output, but
    # only if that node can execute earlier on some path (i.e. it can reach this
    # node via edges). Source declaration order is NOT required - the runtime
    # walks the graph by edges, not by source order, so a node declared later in
    # the source may legitimately be the start that runs first. A self-reference
    # needs `[-1]` (previous iteration); the current iteration's output does not
    # exist until the node finishes.
    ancestors = _ancestors(nodes, edges)
    for node in nodes:
        for ref_node, ref_field, previous in _refs_from_io(node.get("io")):
            if ref_node == node["id"]:
                if previous:
                    if ref_field not in _node_outs(node):
                        errors.append(f'#{node["id"]}#: input references undeclared previous output #{ref_node}.{ref_field}[-1].')
                else:
                    errors.append(f'#{node["id"]}#: input references its own current output #{ref_node}.{ref_field} - use #{ref_node}.{ref_field}[-1] to read the previous iteration.')
                continue
            ref = by_id.get(ref_node)
            if ref is None:
                errors.append(f'#{node["id"]}#: input references undeclared node #{ref_node}.')
            elif ref_field not in _node_outs(ref):
                errors.append(f'#{node["id"]}#: input references undeclared output #{ref_node}.{ref_field}.')
            elif ref_node not in ancestors.get(node["id"], set()):
                errors.append(f'#{node["id"]}#: input references #{ref_node}.{ref_field}, but #{ref_node}# cannot execute before #{node["id"]}# (no path #{ref_node}# -> ... -> #{node["id"]}#).')

    for edge in edges:
        on = edge.get("on")
        cond_error = condition_error(on)
        if cond_error:
            errors.append(f'Edge #{edge["from"]}# -> #{edge["to"]}# {cond_error}')
            continue
        parsed = parse_condition(on)
        from_node = by_id.get(edge["from"])
        fields = condition_fields(parsed)
        if not fields:
            legacy = _condition_field(on)
            fields = [legacy] if legacy else []
        for field in fields:
            if from_node and field not in _node_outs(from_node):
                errors.append(f'Edge #{edge["from"]}# -> #{edge["to"]}# condition references "{field}", but #{edge["from"]}# does not declare it in out(...).')
        for ref_node, ref_field in condition_exists_refs(parsed):
            target = by_id.get(ref_node)
            if target is None:
                errors.append(f'Edge #{edge["from"]}# -> #{edge["to"]}# exists() references undeclared node #{ref_node}#.')
            elif ref_field not in _node_outs(target):
                errors.append(f'Edge #{edge["from"]}# -> #{edge["to"]}# exists() references undeclared output #{ref_node}.{ref_field}#.')

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
        # A fan-out takes every branch by design, so its edges need no condition.
        if len(outs) > 1 and not any(e.get("fanout") for e in outs):
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

    errors.extend(_validate_fanout(nodes, edges, by_id))

    return errors


def _node_join_mode(node: Optional[dict]) -> Optional[str]:
    return ((node or {}).get("policy") or {}).get("join")


def _reachable_from(start: str, edges: list) -> set:
    """Every node reachable from ``start``, including ``start`` itself."""
    outgoing: dict[str, list] = {}
    for edge in edges:
        outgoing.setdefault(edge["from"], []).append(edge["to"])
    seen = {start}
    stack = [start]
    while stack:
        node_id = stack.pop()
        for nxt in outgoing.get(node_id, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def fanout_joins(nodes: list, edges: list) -> dict:
    """Map each fan-out node id to the join node its branches converge on.

    A fan-out (``#a# => #b#``) runs every branch instead of picking one, so it
    only makes sense if the branches come back together somewhere. That meeting
    point is a node declaring ``{ join: "all" }``, and it must be reachable from
    every branch — otherwise the join would wait forever for a branch that can
    never arrive. Nodes whose branches do not resolve to exactly one such join
    are left out; ``validate`` turns those into errors.
    """
    by_id = {n["id"]: n for n in nodes}
    outgoing: dict[str, list] = {}
    for edge in edges:
        outgoing.setdefault(edge["from"], []).append(edge)
    resolved: dict[str, str] = {}
    for node_id, outs in outgoing.items():
        branches = [e for e in outs if e.get("fanout")]
        if len(branches) < 2:
            continue
        common: Optional[set] = None
        for edge in branches:
            joins = {n for n in _reachable_from(edge["to"], edges)
                     if _node_join_mode(by_id.get(n))}
            common = joins if common is None else (common & joins)
        if common and len(common) == 1:
            resolved[node_id] = next(iter(common))
    return resolved


def _validate_fanout(nodes: list, edges: list, by_id: dict) -> list:
    errors: list[str] = []
    outgoing: dict[str, list] = {}
    for edge in edges:
        outgoing.setdefault(edge["from"], []).append(edge)
    resolved = fanout_joins(nodes, edges)

    for node_id, outs in outgoing.items():
        branches = [e for e in outs if e.get("fanout")]
        if not branches:
            continue
        if len(branches) != len(outs):
            errors.append(
                f'#{node_id}# mixes -> and => edges. A node either routes to one '
                f'target (->) or fans out to all of them (=>), not both.')
            continue
        if len(branches) < 2:
            errors.append(
                f'#{node_id}# has a single => edge. Fan-out needs at least two '
                f'branches; use -> for a single next node.')
            continue
        looping = [e for e in branches if e.get("maxLoops")]
        if looping:
            errors.append(
                f'#{node_id}#: => edges cannot carry maxLoops. Loop with -> edges '
                f'outside the fan-out.')
        if node_id not in resolved:
            errors.append(
                f'#{node_id}# fans out but its branches do not converge on one '
                f'join node. Every branch must reach the same node declaring '
                f'{{ join: "all" }}.')

    joined = set(resolved.values())
    for node in nodes:
        mode = _node_join_mode(node)
        if not mode:
            continue
        if mode != "all":
            errors.append(f'#{node["id"]}#: join policy must be "all" (got "{mode}").')
        elif node["id"] not in joined:
            errors.append(
                f'#{node["id"]}# declares {{ join: "all" }} but no fan-out (=>) '
                f'converges on it.')
    return errors


def _ancestors(nodes: list, edges: list) -> dict[str, set]:
    """Map each node id to the set of node ids that can reach it via edges.

    Used to validate that a node input referencing ``#X.field`` can actually
    have seen X's output at runtime (X must be able to execute before this node
    on some path). Computed via forward reachability: for each node N, every
    descendant of N has N as an ancestor.
    """
    fwd: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["from"] in fwd and e["to"] in fwd:
            fwd[e["from"]].append(e["to"])
    anc: dict[str, set] = {n["id"]: set() for n in nodes}
    for n in nodes:
        seen: set = set()
        stack = [n["id"]]
        while stack:
            x = stack.pop()
            for y in fwd.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        for d in seen:
            anc[d].add(n["id"])
    return anc


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
