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

_LOOKS_LIKE_AGENT = re.compile(r"^#[^#{}\n\r]+#\s*(?:\([^)]*\)\s*)?\{")
_LOOKS_LIKE_PREFIX = re.compile(r"^\([^)]+\)\s*#[^#{}\n\r]+#\s*\{")


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
            if stop == "brace" and self._peek() == "}":
                break
            if stop == "parallel" and self._at_parallel_marker():
                break
            if self._starts("```"):
                self._skip_comment_block()
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
        return {"type": "agent", "name": name, "promptFile": prompt_file, "model": model, "body": body}

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
            if stop == "brace" and self._peek() == "}":
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


def validate(steps: list) -> list:
    """Return a list of human-readable validation error strings (empty = OK)."""
    return _validate_parallel_blocks(steps) + _validate_unique_names(steps)
