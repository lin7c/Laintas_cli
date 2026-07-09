"""HWO workflow language — canonical grammar (parser + validation).

Source of truth for the HWO DSL shared by laintas_cli (Python) and Helpwo (TS).
This module is PURE: it turns a `.hwo` source string into a JSON-serialisable
AST and validates it. It has NO I/O and NO agent runtime — each product keeps
its own executor that walks this AST.

Distribution (vendored copy, see scripts/sync_hwo.sh):
  - laintas_cli : cp adapter.py -> laintas_cli/hwo_adapter/adapter.py
  - Helpwo      : the TS mirror adapter.ts -> Helpwo/src/tools/hwo-core.ts

The TS mirror `adapter.ts` MUST stay byte-for-byte equivalent in behaviour;
samples/ + test_parity.py guard against drift.

── Canonical AST (JSON) ─────────────────────────────────────────────────
  task     : {"type": "task",     "text": str}
  agent    : {"type": "agent",    "name": str, "promptFile": str|None, "model": str|None, "body": [node, ...]}
  parallel : {"type": "parallel", "body": [node, ...]}

── Grammar ──────────────────────────────────────────────────────────────
  #name# { ... }            an agent with a body
  #name@model# { ... }      pin the agent to a backend model (e.g. #fe@glm-5.2#)
  (prop.md)#name# { ... }   leading prompt-file prefix
  #name#(prop.md) { ... }   trailing prompt-file prefix (alias)
  // ... //                 parallel block (agents only)
  ->                        serial step separator (no-op connector)
  ```...```                 comment block
  <plain text>              a task
"""
from __future__ import annotations

import re
from typing import Optional

# AST nodes are plain dicts (see module docstring). Aliased for readability.
HwoNode = dict

_LOOKS_LIKE_AGENT = re.compile(r"^#[^#{}\n\r]+#\s*(?:\([^)]*\)\s*)?(?:\[[\s\S]*?\]\s*)?\{")
_LOOKS_LIKE_PREFIX = re.compile(r"^\([^)]+\)\s*#[^#{}\n\r]+#\s*(?:\[[\s\S]*?\]\s*)?\{")


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


class HwoParseError(Exception):
    """Raised on invalid HWO syntax. `index` is the character offset."""

    def __init__(self, message: str, index: int):
        super().__init__(f"{message} at {index}")
        self.index = index


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.i = 0

    # ── public ──
    def parse(self) -> list:
        steps = self._parse_sequence()
        self._skip_ws()
        if not self._eof():
            snippet = self.source[self.i:self.i + 16]
            raise HwoParseError(f'Unexpected token "{snippet}"', self.i)
        return steps

    # ── grammar ──
    def _parse_sequence(self, stop: Optional[str] = None) -> list:
        steps: list = []
        while not self._eof():
            self._skip_ws()
            if self._eof():
                break
            if stop == "brace" and self._peek() == "}" and self._is_closing_brace():
                break
            if stop == "parallel" and self._at_parallel_marker():
                break
            if self._starts("```"):
                self._skip_comment_block()
                continue
            if stop is None and self._starts("@line"):
                steps.append(self._parse_workflow())
                continue
            if self._starts("->"):
                self.i += 2
                continue
            if self._at_parallel_marker():
                steps.append(self._parse_parallel())
                continue
            if self._peek() == "(" and self._looks_like_prompt_prefix():
                steps.append(self._parse_agent())
                continue
            if self._peek() == "#":
                steps.append(self._parse_agent())
                continue
            text = self._read_text(stop).strip()
            if text:
                steps.append({"type": "task", "text": text})
        return steps

    def _parse_parallel(self) -> HwoNode:
        self._expect("//")
        body = self._parse_sequence("parallel")
        if not self._at_parallel_marker():
            raise HwoParseError(
                "Unclosed parallel block, expected // (preceded by whitespace or a brace)",
                self.i,
            )
        self._expect("//")
        return {"type": "parallel", "body": body}

    def _parse_workflow(self) -> HwoNode:
        self._expect("@line")
        self._skip_ws()
        if self._peek() != "[":
            raise HwoParseError("@line must be followed by [in(...), out(...)]", self.i)
        return {"type": "workflow", "io": _parse_io_block(self._read_bracket_content())}

    def _parse_agent(self) -> HwoNode:
        start = self.i

        # Optional LEADING prompt prefix: (prop.md)#name#
        prompt_file: Optional[str] = None
        if self._peek() == "(":
            prompt_file = self._read_prompt_prefix(start)
            self._skip_ws()

        self._expect("#")
        name_start = self.i
        while not self._eof() and self._peek() != "#":
            self.i += 1
        if self._eof():
            raise HwoParseError("Unclosed agent name, expected #", start)
        # The token between the #…# hashes is `name` or `name@model`. A trailing
        # `@model` (split on the FIRST '@'; model ids never contain '@') pins this
        # agent to a specific backend model, e.g. #researcher@deepseek-v4-flash#.
        raw_name = self.source[name_start:self.i].strip()
        name, sep, model = raw_name.partition("@")
        name = name.strip()
        model = model.strip() if sep else None
        if not name:
            raise HwoParseError("Empty agent name", start)
        if sep and not model:
            raise HwoParseError("Empty model after '@' in agent name", start)
        self._expect("#")
        self._skip_ws()

        # Optional TRAILING prompt prefix: #name#(prop.md). Models frequently emit
        # the familiar function-call shape; accept it as an alias for the leading form.
        if prompt_file is None and self._peek() == "(":
            prompt_file = self._read_prompt_prefix(start)
            self._skip_ws()

        io = None
        if self._peek() == "[":
            io = _parse_io_block(self._read_bracket_content())
            self._skip_ws()

        if self._peek() != "{":
            raise HwoParseError(
                f'Agent "{name}" must be followed by {{ — the prompt prefix goes either before '
                f"or right after the name: (file)#{name}# {{ ... }} or #{name}#(file) {{ ... }}",
                self.i,
            )
        self._expect("{")
        body = self._parse_sequence("brace")
        if self._peek() != "}":
            raise HwoParseError(f'Unclosed body for agent "{name}", expected }}', self.i)
        self._expect("}")
        node = {"type": "agent", "name": name, "promptFile": prompt_file, "model": model, "body": body}
        if io is not None:
            node["io"] = io
        return node

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
        raise HwoParseError("Unclosed [, expected ]", start)

    def _read_prompt_prefix(self, start: int) -> Optional[str]:
        self._expect("(")
        name_start = self.i
        while not self._eof() and self._peek() != ")":
            self.i += 1
        if self._eof():
            raise HwoParseError("Unclosed prompt prefix, expected )", start)
        file = self.source[name_start:self.i].strip() or None
        self._expect(")")
        return file

    def _read_text(self, stop: Optional[str] = None) -> str:
        start = self.i
        while not self._eof():
            if self._starts("```"):
                break
            if self._starts("->"):
                break
            if self._at_parallel_marker():
                break
            if stop == "brace" and self._peek() == "}" and self._is_closing_brace():
                break
            if self._peek() == "(" and self._looks_like_prompt_prefix():
                break
            if self._peek() == "#" and self._looks_like_agent():
                break
            self.i += 1
        return self.source[start:self.i]

    def _skip_comment_block(self) -> None:
        self._expect("```")
        end = self.source.find("```", self.i)
        if end == -1:
            raise HwoParseError("Unclosed comment block, expected ```", self.i)
        self.i = end + 3

    # ── lookahead helpers ──
    def _looks_like_agent(self) -> bool:
        return bool(_LOOKS_LIKE_AGENT.match(self.source[self.i:]))

    def _looks_like_prompt_prefix(self) -> bool:
        return bool(_LOOKS_LIKE_PREFIX.match(self.source[self.i:]))

    def _at_parallel_marker(self) -> bool:
        if not self._starts("//"):
            return False
        if self.i == 0:
            return True
        prev = self.source[self.i - 1]
        return prev in ("\n", " ", "\t", "\r", "{", "}")

    def _is_closing_brace(self) -> bool:
        if self._peek() != "}":
            return False
        nxt = self.source[self.i + 1] if self.i + 1 < len(self.source) else ""
        return (not nxt) or nxt.isspace() or nxt in ("/", "}")

    # ── primitives ──
    def _skip_ws(self) -> None:
        while not self._eof() and self.source[self.i].isspace():
            self.i += 1

    def _starts(self, text: str) -> bool:
        return self.source.startswith(text, self.i)

    def _expect(self, text: str) -> None:
        if not self._starts(text):
            raise HwoParseError(f'Expected "{text}"', self.i)
        self.i += len(text)

    def _peek(self) -> str:
        return self.source[self.i] if self.i < len(self.source) else ""

    def _eof(self) -> bool:
        return self.i >= len(self.source)


def parse(source: str) -> list:
    """Parse HWO `source` into the canonical AST (list of nodes)."""
    return _Parser(source).parse()


# Back-compat alias for the original laintas_cli entry-point name.
parse_hwo = parse


# ── Validation ───────────────────────────────────────────────────────────

def _validate_parallel_blocks(steps: list) -> list:
    errors: list = []

    def walk(items: list, path: str) -> None:
        for item in items:
            if item["type"] == "parallel":
                for child in item["body"]:
                    if child["type"] != "agent":
                        errors.append(f"{path}: parallel blocks may only contain #agent# {{ ... }} entries.")
                walk(item["body"], f"{path}//")
            elif item["type"] == "agent":
                walk(item["body"], f"{path}#{item['name']}#")

    walk(steps, "root")
    return errors


def _validate_unique_names(steps: list) -> list:
    errors: list = []

    def walk_scope(items: list, scope_path: str) -> None:
        seen: set = set()

        def visit(lst: list) -> None:
            for item in lst:
                if item["type"] == "agent":
                    if item["name"] in seen:
                        errors.append(
                            f'{scope_path}: duplicate agent name "#{item["name"]}#" — sibling agents '
                            f"(including parallel-block members) must have unique names in the same scope."
                        )
                    else:
                        seen.add(item["name"])
                    walk_scope(item["body"], f"{scope_path} > #{item['name']}#")
                elif item["type"] == "parallel":
                    visit(item["body"])

        visit(items)

    walk_scope(steps, "root")
    return errors


def _validate_io_params(steps: list) -> list:
    errors: list = []

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

    def walk(items: list, path: str) -> None:
        for item in items:
            if item["type"] == "workflow":
                check(item.get("io"), "@line")
            elif item["type"] == "agent":
                check(item.get("io"), f'{path}#{item["name"]}#')
                walk(item["body"], f'{path}#{item["name"]}#')
            elif item["type"] == "parallel":
                walk(item["body"], f"{path}//")

    walk(steps, "root")
    return errors


def _collect_out_names(agent: dict) -> set:
    return {p.get("name") for p in (agent.get("io") or {}).get("out", [])}


def _refs_from_io(io: Optional[dict]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for p in (io or {}).get("in", []):
        src = p.get("source") or p.get("default") or ""
        m = re.match(r"^#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)(?:\[-1\])?$", src)
        if m:
            refs.append((m.group(1), m.group(2)))
    return refs


def _validate_io_references(steps: list) -> list:
    errors: list = []

    def validate_scope(items: list, path: str) -> None:
        available: dict[str, set] = {}
        for item in items:
            if item["type"] == "agent":
                for ref_agent, ref_field in _refs_from_io(item.get("io")):
                    outs = available.get(ref_agent)
                    if outs is None:
                        errors.append(f'{path}#{item["name"]}#: input references #{ref_agent}.{ref_field} before that agent has completed in this scope.')
                    elif ref_field not in outs:
                        errors.append(f'{path}#{item["name"]}#: input references undeclared output #{ref_agent}.{ref_field}.')
                validate_scope(item["body"], f'{path}#{item["name"]}#')
                available[item["name"]] = _collect_out_names(item)
            elif item["type"] == "parallel":
                parallel_agents = [n for n in item["body"] if n["type"] == "agent"]
                parallel_names = {a["name"] for a in parallel_agents}
                for agent in parallel_agents:
                    for ref_agent, ref_field in _refs_from_io(agent.get("io")):
                        if ref_agent in parallel_names:
                            errors.append(f'{path}//#{agent["name"]}#: parallel agents cannot read sibling output #{ref_agent}.{ref_field}; use agent_send/agent_receive during the block, or read outputs after the block.')
                        else:
                            outs = available.get(ref_agent)
                            if outs is None:
                                errors.append(f'{path}//#{agent["name"]}#: input references #{ref_agent}.{ref_field} before that agent has completed in this scope.')
                            elif ref_field not in outs:
                                errors.append(f'{path}//#{agent["name"]}#: input references undeclared output #{ref_agent}.{ref_field}.')
                    validate_scope(agent["body"], f'{path}//#{agent["name"]}#')
                for agent in parallel_agents:
                    available[agent["name"]] = _collect_out_names(agent)

    validate_scope(steps, "root")
    return errors


def _is_relative_prompt_path(path: str) -> bool:
    s = path.strip()
    if not s:
        return False
    if s.startswith("/") or s.startswith("\\"):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return False
    if "://" in s:
        return False
    return True


def _validate_prompt_paths(steps: list) -> list:
    errors = []

    def walk(items: list, path: str) -> None:
        for item in items:
            if item["type"] == "agent":
                prompt_file = item.get("promptFile")
                if prompt_file and not _is_relative_prompt_path(prompt_file):
                    errors.append(
                        f'{path}#{item["name"]}#: prompt file path must be relative; '
                        "use ../ to reference a parent directory."
                    )
                walk(item["body"], f'{path}#{item["name"]}#')
            elif item["type"] == "parallel":
                walk(item["body"], f"{path}//")

    walk(steps, "root")
    return errors


def _validate_body_variable_misuse(steps: list) -> list:
    errors: list = []
    bad_re = re.compile(r"\bin\s*\(\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\)")

    def walk(items: list, path: str) -> None:
        for item in items:
            if item["type"] == "task":
                m = bad_re.search(item.get("text", ""))
                if m:
                    name = m.group(1)
                    errors.append(
                        f'{path}: body text uses in({name}), but in(...) is declaration syntax only; '
                        f'use $input.{name} or bind it with [in({name} = $input.{name})] and read $self.{name}.'
                    )
            elif item["type"] == "agent":
                walk(item["body"], f'{path}#{item["name"]}#')
            elif item["type"] == "parallel":
                walk(item["body"], f"{path}//")

    walk(steps, "root")
    return errors


def validate(steps: list) -> list:
    """Return a list of human-readable validation error strings (empty = OK)."""
    return (
        _validate_parallel_blocks(steps)
        + _validate_unique_names(steps)
        + _validate_io_params(steps)
        + _validate_io_references(steps)
        + _validate_prompt_paths(steps)
        + _validate_body_variable_misuse(steps)
    )
