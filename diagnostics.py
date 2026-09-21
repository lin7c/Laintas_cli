"""Structured compile diagnostics for the HWO/HWG languages (design P0).

Product layer only — this module never changes the vendored adapters'
behaviour: hwo_adapter/ and hwg_adapter/ stay byte-identical, so the
agent_gateway parity goldens and the TS mirrors are untouched. It consumes
the same public parse/validate results the runners already use, maps each
error onto a stable code (docs/hwo-hwg-diagnostics-design.md §5), enriches
it with source locations and "did you mean" notes, and renders it as:

  - plain  : "file:line:col: error: message [CODE]"
             (the GCC -fdiagnostics-plain-output shape)
  - pretty : caret block with the source line + notes (color-free in P0)
  - json   : a JSON-serialisable list of dicts

The legacy `msg` strings produced by hwo_runner/hwg_runner are preserved
byte-for-byte; the `diagnostics` / `pretty` fields on compile results are
additive. Columns are 1-based Unicode code points; display alignment uses
wcwidth so CJK identifiers keep the caret on target.
"""
from __future__ import annotations

import bisect
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from hwo_adapter import HwoParseError
from hwo_adapter.adapter import EFFORT_GEARS
from hwg_adapter import HwgParseError, parse as parse_hwg_ast, validate as validate_hwg_ast
from hwg_adapter.adapter import resolve_includes

__all__ = [
    "Locus", "Note", "FixIt", "Diagnostic",
    "render_plain", "render_pretty", "to_jsonable",
    "attach_hwo_compile", "attach_hwg_compile",
]

try:
    import wcwidth
except ImportError:  # pragma: no cover - rich pulls in wcwidth; guard anyway
    wcwidth = None


# ── Data model ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Locus:
    file: str
    line: Optional[int] = None       # 1-based; None = file-level only
    column: Optional[int] = None     # 1-based Unicode code points
    end_line: Optional[int] = None
    end_column: Optional[int] = None
    byte_offset: int = -1


@dataclass(frozen=True)
class Note:
    message: str
    locus: Optional[Locus] = None


@dataclass(frozen=True)
class FixIt:
    kind: str          # insert | replace | delete
    locus: Locus
    text: str = ""


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str      # error | warning | note
    phase: str         # lex | parse | sema | include
    message: str
    locus: Locus
    notes: Tuple[Note, ...] = ()
    fixits: Tuple[FixIt, ...] = ()


# ── Source map: offset -> line/col ──────────────────────────────────────

class _SourceMap:
    """Character offset -> (line, column). Both 1-based; column counts
    Unicode code points, matching the parser's string indices."""

    def __init__(self, source: str):
        self.source = source
        self.line_starts = [0]
        for i, ch in enumerate(source):
            if ch == "\n":
                self.line_starts.append(i + 1)
        self.lines = source.split("\n")

    def locate(self, offset: int) -> Tuple[int, int]:
        if not self.line_starts:
            return 1, 1
        offset = max(0, min(offset, len(self.source)))
        idx = bisect.bisect_right(self.line_starts, offset) - 1
        return idx + 1, offset - self.line_starts[idx] + 1

    def line_text(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1]
        return ""


def _strip_comments(source: str) -> str:
    """Blank out ``` fenced comment blocks (both languages use them),
    preserving offsets and newlines so SourceMap.locate stays exact."""
    chars = list(source)
    i, n = 0, len(source)
    while i < n:
        if source.startswith("```", i):
            j = source.find("```", i + 3)
            end = n if j < 0 else j + 3
            for k in range(i, end):
                if chars[k] != "\n":
                    chars[k] = " "
            i = end
        else:
            i += 1
    return "".join(chars)


class _EntityIndex:
    """Conservative source scan for validation-stage loci.

    The adapters' AST carries no spans (design §9 P0 honest boundary), so
    positions are recovered by scanning the comment-stripped source for the
    literal tokens each error names. A locus is produced only on a UNIQUE
    match — or, for duplicates, on the second declaration — because a
    diagnostic pointing at the wrong line is worse than one with no line."""

    def __init__(self, path: str, source: str):
        self.path = path
        self.smap = _SourceMap(source)
        self.stripped = _strip_comments(source)

    def _locus_at(self, offset: int) -> Locus:
        line, col = self.smap.locate(offset)
        return Locus(file=self.path, line=line, column=col, byte_offset=offset)

    def unique_locus(self, needle: str) -> Optional[Locus]:
        if not needle:
            return None
        occ = [m.start() for m in re.finditer(re.escape(needle), self.stripped)]
        return self._locus_at(occ[0]) if len(occ) == 1 else None

    def loci_re(self, pattern: str, group: int = 1) -> List[Locus]:
        return [self._locus_at(m.start(group))
                for m in re.finditer(pattern, self.stripped)]

    def unique_locus_re(self, pattern: str, group: int = 1) -> Optional[Locus]:
        found = self.loci_re(pattern, group)
        return found[0] if len(found) == 1 else None


def _hwo_decl_pattern(name: str) -> str:
    """Statement-start shape of an HWO agent declaration: line start,
    optional // parallel opener, optional (prompt-file) prefix."""
    return (r"(?m)^[ \t]*(?://[ \t]*)?(?:\([^)\n]*\))?"
            + "(" + re.escape(f"#{name}#") + ")")


def _hwg_decl_pattern(node_id: str) -> str:
    """HWG node declaration shape: optional !, a (file.hwo) binding, #id#."""
    return r"!?\([^)\n]*\)(" + re.escape(f"#{node_id}#") + ")"


# ── Code tables (design §5) ─────────────────────────────────────────────
# Ordered rules: (regex on the message, code, caret_at_eof).
# caret_at_eof marks raise sites that pass the current cursor (end of
# input) rather than the position where the construct was opened.

_HWO_PARSE_RULES: List[Tuple[str, str, bool]] = [
    (r"^Empty thinking gear", "HWO2001", False),
    (r"^Unknown thinking gear", "HWO2002", False),
    (r"^Unexpected token", "HWO2003", False),
    (r"^Unclosed parallel block", "HWO2004", True),
    (r"^@line must be followed", "HWO2005", True),
    (r"^Unclosed agent name", "HWO2006", False),
    (r"^Empty agent name", "HWO2007", False),
    (r"^Empty model after", "HWO2008", False),
    (r"must be followed by \{", "HWO2009", True),
    (r"^Unclosed body for agent", "HWO2010", True),
    (r"^Unclosed \[", "HWO2011", False),
    (r"^Unclosed prompt prefix", "HWO2012", False),
    (r"^Unclosed comment block", "HWO1001", True),
    (r'^Expected "', "HWO2013", False),
]

_HWG_PARSE_RULES: List[Tuple[str, str, bool]] = [
    (r"^Expected #name# or \{ after", "HWG2001", False),
    (r"^Unexpected token", "HWG2002", False),
    (r"^A manual node must be !\(", "HWG2003", False),
    (r"^A manual node must bind", "HWG2004", False),
    (r"^Empty file binding", "HWG2005", False),
    (r"^@graph must be followed", "HWG2006", True),
    (r"^@include must be followed", "HWG2007", True),
    (r"^Unterminated @include path", "HWG2008", False),
    (r"^Expected -> or => after", "HWG2009", False),
    (r"^Expected target #name# after", "HWG2010", False),
    (r"^Unknown block", "HWG2011", False),
    (r"^Unclosed \(", "HWG2012", False),
    (r"^Unclosed \{", "HWG2013", False),
    (r"^Unclosed \[", "HWG2014", False),
    (r"^Unclosed name", "HWG2015", False),
    (r"^Empty name", "HWG2016", False),
    (r"^Unclosed comment block", "HWG1001", True),
    (r'^Expected "', "HWG2017", False),
]

# Ordered rules for validation strings: (regex, code). Specific before
# general; the parallel "//" variants are decided by the scope prefix.
_HWO_VALIDATE_RULES: List[Tuple[str, str]] = [
    (r"parallel blocks may only contain", "HWO3001"),
    (r"duplicate agent name", "HWO3002"),
    (r"invalid (in|out) parameter name", "HWO3003"),
    (r"duplicate (in|out) parameter", "HWO3004"),
    (r"parallel agents cannot read sibling", "HWO3007"),
    (r"undeclared output", "HWO3006"),
    (r"before that agent has completed", "HWO3005"),
    (r"prompt file path must be relative", "HWO3010"),
    (r"in\(\.\.\.\) is declaration syntax only", "HWO3011"),
]

_HWG_VALIDATE_RULES: List[Tuple[str, str]] = [
    (r"invalid (in|out) parameter name", "HWG3001"),
    (r"duplicate (in|out) parameter", "HWG3002"),
    (r"Duplicate node id", "HWG3003"),
    (r"retry policy must be", "HWG3004"),
    (r"tools: applies to the agents", "HWG3005"),
    (r"would leave the node with nothing", "HWG3006"),
    (r"is not a tool name or glob", "HWG3007"),
    (r"Multiple \(schedule\) blocks", "HWG3008"),
    (r"exists\(\) references undeclared node", "HWG3018"),
    (r"exists\(\) references undeclared output", "HWG3019"),
    (r"^Edge from #.* references an undeclared node", "HWG3009"),
    (r"^Edge to #.* references an undeclared node", "HWG3010"),
    (r"condition .* is not valid", "HWG3016"),
    (r"^Edge #.* -> #.* condition references", "HWG3017"),
    (r"its own current output", "HWG3012"),
    (r"undeclared previous output", "HWG3011"),
    (r"input references undeclared node", "HWG3013"),
    (r"input references undeclared output", "HWG3014"),
    (r"cannot execute before", "HWG3015"),
    (r"Self-loop", "HWG3020"),
    (r"cycle with no bounded edge", "HWG3021"),
    (r"every branch edge must carry a condition", "HWG3022"),
    (r"No start node", "HWG3023"),
    (r"Multiple start nodes", "HWG3024"),
    (r"No end node", "HWG3025"),
    (r"mixes -> and =>", "HWG3026"),
    (r"has a single => edge", "HWG3027"),
    (r"=> edges cannot carry maxLoops", "HWG3028"),
    (r"do not converge on one join", "HWG3029"),
    (r"join policy must be", "HWG3030"),
    (r"but no fanout", "HWG3031"),
]

_HWG_INCLUDE_RULES: List[Tuple[str, str]] = [
    (r"has an empty path", "HWG4001"),
    (r"@include cycle", "HWG4002"),
    (r"could not be read", "HWG4003"),
    (r"not found", "HWG4004"),
    (r"failed to parse", "HWG4005"),
    (r"was not resolved", "HWG4006"),
]


def _classify(rules: Sequence[Tuple[str, str]], text: str, fallback: str) -> str:
    for pattern, code in rules:
        if re.search(pattern, text):
            return code
    return fallback


# ── Suggestions ("did you mean") ────────────────────────────────────────

def _suggest(name: str, candidates: Sequence[str]) -> Optional[str]:
    if not name or not candidates:
        return None
    try:
        matches = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    except Exception:
        return None
    return matches[0] if matches else None


def _hwo_agents(ast: List[dict]) -> "dict[str, set]":
    """agent name -> declared out field names, recursive over bodies."""
    found: dict = {}

    def walk(steps):
        for item in steps or []:
            if item.get("type") == "agent":
                io = item.get("io") or {}
                outs = {p.get("name") for p in io.get("out", []) if p.get("name")}
                found.setdefault(item["name"], outs)
                walk(item.get("body"))
            elif item.get("type") == "parallel":
                walk(item.get("body"))

    walk(ast)
    return found


def _hwg_nodes(statements: List[dict]) -> "dict[str, set]":
    """node id -> declared out field names (include statements are spliced
    before validation, so the walk sees the same set the validator saw)."""
    found: dict = {}
    for s in statements or []:
        if s.get("type") == "node":
            io = s.get("io") or {}
            outs = {p.get("name") for p in io.get("out", []) if p.get("name")}
            found.setdefault(s["id"], outs)
    return found


def _hwo_io_ref_notes(text: str, agents: "dict[str, set]") -> List[Note]:
    m = re.search(r"input references (?:undeclared output )?#([\w.-]+)\.([\w.-]+)", text)
    if not m:
        return []
    ref_agent, ref_field = m.group(1), m.group(2).rstrip(".")  # trailing '.' is sentence punctuation
    if ref_agent not in agents:
        note = f"'#{ref_agent}#' is not declared in this workflow"
        sug = _suggest(ref_agent, agents.keys())
        if sug:
            note += f"; did you mean '#{sug}#'?"
        return [Note(note)]
    if ref_field not in agents[ref_agent]:
        note = f"'{ref_field}' is not one of '#{ref_agent}#'s declared outputs"
        sug = _suggest(ref_field, agents[ref_agent])
        if sug:
            note += f"; did you mean '{sug}'?"
        return [Note(note)]
    return []


def _gear_notes(text: str) -> List[Note]:
    m = re.search(r"Unknown thinking gear '([^']*)'", text)
    if not m:
        return []
    sug = _suggest(m.group(1), EFFORT_GEARS)
    if sug:
        return [Note(f"did you mean '{sug}'?")]
    return []


def _hwg_node_ref_notes(text: str, nodes: "dict[str, set]") -> List[Note]:
    # Undeclared node: edge endpoints, exists(), plain input references.
    m = (re.search(r"^Edge (?:from|to) #([\w.-]+)# references an undeclared node", text)
         or re.search(r"exists\(\) references undeclared node #([\w.-]+)#", text)
         or re.search(r"input references undeclared node #([\w.-]+)", text))
    if m:
        ref = m.group(1)
        note = f"'#{ref}#' is not a declared node"
        sug = _suggest(ref, nodes.keys())
        if sug:
            note += f"; did you mean '#{sug}#'?"
        return [Note(note)]
    # Undeclared output: agent.field reference against a declared node.
    m = (re.search(r"exists\(\) references undeclared output #([\w.-]+)\.([\w.-]+)#", text)
         or re.search(r"input references undeclared output #([\w.-]+)\.([\w.-]+)", text))
    if m and m.group(1) in nodes:
        ref_agent, ref_field = m.group(1), m.group(2).rstrip(".")
        note = f"'{ref_field}' is not one of '#{ref_agent}#'s declared outputs"
        sug = _suggest(ref_field, nodes[ref_agent])
        if sug:
            note += f"; did you mean '{sug}'?"
        return [Note(note)]
    return []


# ── Scope-prefix handling ───────────────────────────────────────────────
# HWO scope paths look like  root  /  root#writer#  /  root//#a#  / nested
# combinations. The prefix is location, not message: strip it for the
# Diagnostic and keep the entity as a normalized "agent '#x#'" lead-in
# (the GCC "In function 'main'" pattern).

_HWO_SCOPE_RE = re.compile(r"^(root(?:(//)?#([\w.-]+)#)*):\s*")
_HWG_NODE_PREFIX_RE = re.compile(r"^#([\w.-]+)#:\s*")


def _strip_hwo_scope(text: str) -> Tuple[str, Optional[str], bool]:
    m = _HWO_SCOPE_RE.match(text)
    if not m:
        return text, None, False
    body = text[m.end():]
    # Last scope segment names the agent the message is about.
    segs = re.findall(r"(//)?#([\w.-]+)#", m.group(1))
    if segs:
        parallel = segs[-1][0] == "//"
        return body, segs[-1][1], parallel
    return body, None, False


# ── Diagnostic builders ─────────────────────────────────────────────────

def _clean_parse_message(message: str, index: int) -> str:
    """Drop the trailing ' at <index>' the exception glued on, and keep the
    message single-line (design §5 rule 4: never quote raw multi-line
    snippets)."""
    suffix = f" at {index}"
    if message.endswith(suffix):
        message = message[: -len(suffix)]
    return (message.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))


def _parse_diagnostic(lang: str, rules, fallback: str, path: str,
                      source: str, exc: Exception, index: int) -> Diagnostic:
    message = _clean_parse_message(str(exc), index)
    smap = _SourceMap(source)
    line, col = smap.locate(index)
    code = fallback
    at_eof = False
    for pattern, c, eof in rules:
        if re.search(pattern, message):
            code, at_eof = c, eof
            break
    notes: List[Note] = []
    if lang == "hwo":
        notes.extend(_gear_notes(message))
    if at_eof:
        notes.append(Note(f"reached end of input at line {line}"))
    return Diagnostic(
        code=code, severity="error", phase="parse", message=message,
        locus=Locus(file=path, line=line, column=col, byte_offset=index),
        notes=tuple(notes),
    )


def _hwo_validation_diagnostic(path: str, text: str, agents: "dict[str, set]",
                                idx: Optional[_EntityIndex] = None) -> Diagnostic:
    body, agent, parallel = _strip_hwo_scope(text)
    if "before that agent has completed" in body:
        code = "HWO3008" if parallel else "HWO3005"
    elif "undeclared output" in body:
        code = "HWO3009" if parallel else "HWO3006"
    else:
        code = _classify(_HWO_VALIDATE_RULES, text, "HWO3000")
    message = body
    if agent:
        lead = "parallel agent" if parallel else "agent"
        message = f"{lead} '#{agent}': {body}"
    notes = list(_hwo_io_ref_notes(text, agents))
    notes.extend(_gear_notes(text))
    locus = Locus(file=path)
    if idx is not None:
        if code == "HWO3002":
            # GCC-style: point at the redeclaration, note the first one.
            m = re.search(r'duplicate agent name "#([\w.-]+)#"', text)
            decls = idx.loci_re(_hwo_decl_pattern(m.group(1))) if m else []
            if len(decls) >= 2:
                locus = decls[1]
                notes.append(Note("first declared here", locus=decls[0]))
        else:
            # Prefer the offending reference itself, then the agent decl.
            loc = None
            m = re.search(
                r"input references (?:undeclared output )?#([\w.-]+)\.([\w.-]+)",
                text)
            if m:
                loc = idx.unique_locus(
                    f"#{m.group(1)}.{m.group(2).rstrip('.')}")
            if loc is None and agent:
                loc = idx.unique_locus_re(_hwo_decl_pattern(agent))
            locus = loc or locus
    return Diagnostic(
        code=code, severity="error", phase="sema", message=message,
        locus=locus, notes=tuple(notes),
    )


def _hwg_validation_diagnostic(path: str, text: str, nodes: "dict[str, set]",
                               idx: Optional[_EntityIndex] = None) -> Diagnostic:
    code = _classify(_HWG_VALIDATE_RULES, text, "HWG3000")
    m = _HWG_NODE_PREFIX_RE.match(text)
    message = text
    if m:
        message = f"node '#{m.group(1)}': {text[m.end():]}"
    notes = list(_hwg_node_ref_notes(text, nodes))
    locus = Locus(file=path)
    if idx is not None:
        if code == "HWG3003":
            mid = re.search(r'Duplicate node id "#([\w.-]+)#"', text)
            decls = idx.loci_re(_hwg_decl_pattern(mid.group(1))) if mid else []
            if len(decls) >= 2:
                locus = decls[1]
                notes.append(Note("first declared here", locus=decls[0]))
        elif m:
            locus = idx.unique_locus_re(_hwg_decl_pattern(m.group(1))) or locus
        else:
            medge = re.search(r"#([\w.-]+)# -> #([\w.-]+)#", text)
            mone = re.match(
                r"Edge (?:from|to) #([\w.-]+)# references an undeclared node", text)
            if medge:
                for arrow in ("->", "=>"):
                    loc = idx.unique_locus(
                        f"#{medge.group(1)}# {arrow} #{medge.group(2)}#")
                    if loc:
                        locus = loc
                        break
            elif mone:
                tok = re.escape(f"#{mone.group(1)}#")
                loc = (idx.unique_locus_re(rf"({tok})\s*(?:->|=>)")
                       or idx.unique_locus_re(rf"(?:->|=>)\s*({tok})"))
                locus = loc or locus
    return Diagnostic(
        code=code, severity="error", phase="sema", message=message,
        locus=locus, notes=tuple(notes),
    )


def _hwg_include_diagnostic(path: str, text: str) -> Diagnostic:
    return Diagnostic(
        code=_classify(_HWG_INCLUDE_RULES, text, "HWG4000"),
        severity="error", phase="include", message=text,
        locus=Locus(file=path),
    )


# ── Renderers ───────────────────────────────────────────────────────────

def _display_width(text: str) -> int:
    if wcwidth is not None:
        try:
            return wcwidth.wcswidth(text)
        except Exception:
            pass
    return len(text)


def render_plain(diags: Sequence[Diagnostic]) -> str:
    """One line per diagnostic, GCC plain-output shape."""
    out: List[str] = []
    for d in diags:
        loc = d.locus
        if loc.line is not None:
            where = f"{loc.file}:{loc.line}:{loc.column}"
        else:
            where = loc.file
        out.append(f"{where}: {d.severity}: {d.message} [{d.code}]")
    return "\n".join(out)


def _caret_lines(header: str, loc: Locus, sources: dict, width: int) -> List[str]:
    lines = [header]
    if loc.line is not None:
        lines.append(f"  --> {loc.file}:{loc.line}:{loc.column}")
        gutter = " " * width
        lines.append(f"  {gutter} |")
        smap = _SourceMap(sources.get(loc.file, ""))
        raw_line = smap.line_text(loc.line)
        lines.append(f" {loc.line:>{width}} | {raw_line.expandtabs(4)}")
        prefix = raw_line[: max(0, (loc.column or 1) - 1)].expandtabs(4)
        lines.append(f"  {gutter} | {' ' * _display_width(prefix)}^")
    else:
        lines.append(f"  --> {loc.file}")
    return lines


def render_pretty(diags: Sequence[Diagnostic], lang: str = "hwo",
                  sources: Optional["dict[str, str]"] = None) -> str:
    """Caret block per diagnostic. Color-free in P0 (the TUI rich layer can
    wrap this later); alignment uses wcwidth so CJK source stays aligned.
    `sources` maps file path -> already-read text (the compile path has it
    in hand); a missing entry just degrades that block to file-level.
    A note carrying its own locus renders as a GCC-style standalone note
    block; plain notes stay inline as `= note:` lines."""
    sources = sources or {}
    loc_lines = [d.locus.line for d in diags if d.locus.line is not None]
    for d in diags:
        loc_lines += [n.locus.line for n in d.notes
                      if n.locus is not None and n.locus.line is not None]
    width = max((len(str(l)) for l in loc_lines), default=1)
    blocks: List[str] = []
    for d in diags:
        lines = _caret_lines(
            f"{lang}: {d.severity}: {d.message} [{d.code}]",
            d.locus, sources, width)
        for note in d.notes:
            if note.locus is None or note.locus.line is None:
                lines.append(f"   = note: {note.message}")
        blocks.append("\n".join(lines))
        for note in d.notes:
            if note.locus is not None and note.locus.line is not None:
                blocks.append("\n".join(_caret_lines(
                    f"{lang}: note: {note.message}",
                    note.locus, sources, width)))
    return "\n\n".join(blocks)


def _locus_json(loc: Optional[Locus]) -> Optional[dict]:
    if loc is None:
        return None
    return {
        "file": loc.file, "line": loc.line, "column": loc.column,
        "endLine": loc.end_line, "endColumn": loc.end_column,
        "byteOffset": loc.byte_offset,
    }


def to_jsonable(diags: Sequence[Diagnostic]) -> List[dict]:
    out: List[dict] = []
    for d in diags:
        out.append({
            "code": d.code,
            "severity": d.severity,
            "phase": d.phase,
            "message": d.message,
            "locus": _locus_json(d.locus),
            "notes": [{"message": n.message, "locus": _locus_json(n.locus)}
                      for n in d.notes],
            "fixits": [{"kind": f.kind, "text": f.text} for f in d.fixits],
        })
    return out


# ── Attach points (called by the runners, only on failure) ──────────────

def attach_hwo_compile(result: dict, path: str, source: str, *,
                        parse_error: Optional[HwoParseError] = None,
                        validation_errors: Optional[List[str]] = None,
                        ast: Optional[List[dict]] = None) -> None:
    """Add `diagnostics` (JSON-able) and `pretty` to a failed compile result
    without touching `msg`. Only called on the failure path."""
    diags: List[Diagnostic] = []
    if parse_error is not None:
        diags.append(_parse_diagnostic(
            "hwo", _HWO_PARSE_RULES, "HWO2000", path, source,
            parse_error, getattr(parse_error, "index", 0)))
    if validation_errors:
        agents = _hwo_agents(ast or [])
        idx = _EntityIndex(path, source)
        diags.extend(_hwo_validation_diagnostic(path, e, agents, idx)
                     for e in validation_errors)
    if diags:
        result["diagnostics"] = to_jsonable(diags)
        result["pretty"] = render_pretty(diags, "hwo", {path: source})


def _read_include(path: str) -> Optional[str]:
    """Same contract as hwg_runner._read_include: None means 'no such file'
    and resolve_includes turns that into a named error."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def attach_hwg_compile(result: dict, path: str) -> None:
    """HWG's _read_and_validate collapses the stage into one string, so on
    failure re-run the same pure pipeline (read -> parse -> includes ->
    validate) to recover which stage failed and with what offsets. The
    adapters are pure and the file was just read, so this is cheap and
    cannot diverge from the original failure. `msg` is untouched."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        return  # the legacy 'cannot read' message already says it all
    diags: List[Diagnostic] = []
    try:
        statements = parse_hwg_ast(source)
    except HwgParseError as e:
        diags.append(_parse_diagnostic(
            "hwg", _HWG_PARSE_RULES, "HWG2000", path, source, e, e.index))
    else:
        # Entities spliced in from @include files cannot be located in this
        # file's text; with any include present, stay file-level rather than
        # risk pointing at a same-named token in the wrong file.
        had_includes = any(s.get("type") == "include" for s in statements)
        statements, include_errors = resolve_includes(
            statements, path, _read_include)
        if include_errors:
            diags.extend(_hwg_include_diagnostic(path, e) for e in include_errors)
        else:
            errors = validate_hwg_ast(statements)
            nodes = _hwg_nodes(statements)
            idx = None if had_includes else _EntityIndex(path, source)
            diags.extend(_hwg_validation_diagnostic(path, e, nodes, idx)
                         for e in errors)
    if diags:
        result["diagnostics"] = to_jsonable(diags)
        result["pretty"] = render_pretty(diags, "hwg", {path: source})
