"""Styled transcript for the /agents view: events in, wrapped screen rows out.

The view used to paint events as flat ``(style, line)`` pairs and let
prompt_toolkit wrap them. Two things went wrong with that: wrapped rows lost
their indent and ran under the roster's border, and a tool's output arrived as
a raw dump — a 40-line file read took over the screen. This module owns the
whole path instead:

    events ──build_blocks──▶ blocks ──render_rows(width)──▶ rows

A *block* is one thing that happened (a message, a tool call with its output,
an approval). A *row* is one physical terminal row with its fragments, the
plain text it shows (so a mouse selection can be copied), and what a click on
it does. Wrapping happens here, per block, with a hanging indent, so a row
never has to be re-wrapped by the renderer and the scrollbar can count rows
exactly.

No terminal I/O and no prompt_toolkit Application in here — it is pure, so it
can be tested row by row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable, Optional

from prompt_toolkit.utils import get_cwidth

import symbols


# ── rows ──────────────────────────────────────────────────────────────────

@dataclass
class Row:
    """One physical row of the transcript."""

    fragments: list
    #: What the row shows, for copying a selection. Prefix glyphs included:
    #: they are what the person sees and selected.
    text: str = ""
    #: Style used to pad the row to the full pane width (user/code rows
    #: carry a background, and a background that stops at the last glyph
    #: reads as a rendering bug).
    fill: str = ""
    #: ``("toggle", key)`` — a click on this row expands/collapses a block.
    action: Optional[tuple] = None
    #: ``"tool"`` / ``"cursor"`` — rows whose glyph animates at paint time.
    anim: str = ""
    block: str = ""


def cell_width(text: str) -> int:
    return sum(max(0, get_cwidth(ch)) for ch in text)


def _merge(fragments: Iterable[tuple]) -> list:
    merged: list = []
    for style, text in fragments:
        if not text:
            continue
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    return merged


def crop(text: str, width: int) -> str:
    """Single-line crop by cells, with an ellipsis."""
    text = " ".join(str(text or "").split())
    if cell_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = max(0, get_cwidth(ch))
        if used + w > max(0, width - 1):
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _breakable_after(ch: str) -> bool:
    # Not "_" or ".": identifiers and dotted names break mid-word otherwise.
    return ch in " /-,;)]}|" or get_cwidth(ch) == 2


def wrap(fragments: Iterable[tuple], width: int,
         first: Iterable[tuple] = (), rest: Iterable[tuple] = (),
         hard: bool = False) -> list[list]:
    """Word-wrap styled fragments into rows of at most ``width`` cells.

    ``first`` prefixes the first row and ``rest`` every continuation row —
    that is the hanging indent, and the reason continuation text lines up
    under the text rather than under the bullet. ``hard`` breaks exactly at
    the edge (code, tool output) instead of at the last word boundary.
    """
    width = max(4, int(width))
    first, rest = list(first), list(rest)
    first_w, rest_w = cell_width("".join(t for _s, t in first)), \
        cell_width("".join(t for _s, t in rest))
    chars = [(style, ch) for style, text in fragments for ch in text
             if ch != "\n"]
    rows: list[list] = []
    line: list = []
    col = 0
    prefix_w = first_w
    last_break = -1

    def emit(chunk):
        rows.append(_merge((first if not rows else rest) + chunk))

    for style, ch in chars:
        w = max(0, get_cwidth(ch))
        if col + w > width - prefix_w and line:
            if not hard and last_break >= 0 and last_break < len(line) - 1:
                head, tail = line[:last_break + 1], line[last_break + 1:]
            else:
                head, tail = line, []
            # Trailing spaces at a soft break are invisible; strip them so a
            # selection does not pick up padding that was never typed.
            while head and head[-1][1] == " " and not hard:
                head.pop()
            emit(head)
            prefix_w = rest_w
            while tail and tail[0][1] == " " and not hard:
                tail.pop(0)
            line = tail
            col = sum(max(0, get_cwidth(c)) for _s, c in line)
            last_break = -1
            for index, (_s, c) in enumerate(line):
                if _breakable_after(c):
                    last_break = index
            if ch == " " and not line and not hard:
                continue
        line.append((style, ch))
        col += w
        if _breakable_after(ch):
            last_break = len(line) - 1
    emit(line)
    return rows
def _plain(fragments) -> str:
    return "".join(text for _style, text in fragments)


# ── inline markdown ───────────────────────────────────────────────────────

_INLINE_RE = re.compile(
    r"(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|(?<![*\w])\*[^*\s][^*]*\*(?![*\w])|"
    r"~~[^~]+~~|\[[^\]]+\]\([^)]+\))")


def inline(text: str, base: str = "") -> list:
    """``**bold**``, ``*italic*``, `` `code` ``, links and strikethrough."""
    fragments: list = []
    position = 0

    def styled(extra: str) -> str:
        return f"{base} {extra}".strip() if base else extra

    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            fragments.append((base, text[position:match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            fragments.append(("class:md.code", token[1:-1]))
        elif token.startswith("**") or token.startswith("__"):
            fragments.append((styled("class:md.bold"), token[2:-2]))
        elif token.startswith("~~"):
            fragments.append((styled("class:md.strike"), token[2:-2]))
        elif token.startswith("*"):
            fragments.append((styled("class:md.italic"), token[1:-1]))
        else:
            label, url = re.match(r"\[([^]]+)\]\(([^)]+)\)", token).groups()
            fragments.append(("class:md.link", label))
            if url.strip() != label.strip():
                fragments.append(("class:muted", f" {url}"))
        position = match.end()
    if position < len(text):
        fragments.append((base, text[position:]))
    return fragments


def _strip_inline(text: str) -> str:
    return _plain(inline(text))


# ── block-level markdown ──────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(?:[-*_]\s*){3,}$")
_BULLET_RE = re.compile(r"^(\s*)([-+*])\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _table_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _render_table(lines: list[str], width: int, first: list, rest: list
                  ) -> list[Row]:
    header = _table_cells(lines[0])
    body = [_table_cells(line) for line in lines[2:]]
    columns = max([len(header)] + [len(row) for row in body])
    grid = [header + [""] * (columns - len(header))] + [
        row + [""] * (columns - len(row)) for row in body]
    widths = [max(cell_width(_strip_inline(row[i])) for row in grid)
              for i in range(columns)]
    indent = cell_width(_plain(rest))
    total = indent + sum(widths) + 3 * (columns - 1)
    rows: list[Row] = []
    if total > width:
        # Too wide to align: one "a · b · c" line per row still reads as a
        # table and never runs off the edge.
        for index, cells in enumerate(grid):
            frags: list = []
            for i, cell in enumerate(cells):
                if i:
                    frags.append(("class:separator", f" {symbols.BULLET} "))
                frags.extend(inline(cell, "class:md.th" if index == 0 else ""))
            for chunk in wrap(frags, width, first if not rows else rest, rest):
                rows.append(Row(chunk, _plain(chunk)))
        return rows
    for index, cells in enumerate(grid):
        frags = list(first if not rows else rest)
        for i, cell in enumerate(cells):
            if i:
                frags.append(("class:separator", f" {symbols.TREE_VERT} "))
            content = inline(cell, "class:md.th" if index == 0 else "")
            frags.extend(content)
            frags.append(("", " " * (widths[i] - cell_width(_plain(content)))))
        rows.append(Row(_merge(frags), _plain(frags)))
        if index == 0:
            rule = "─┼─".join("─" * w for w in widths)
            rows.append(Row(_merge(list(rest) + [("class:separator", rule)]),
                            _plain(rest) + rule))
    return rows


def markdown_rows(text: str, width: int, first: list, rest: list,
                  base: str = "") -> list[Row]:
    """Render markdown into rows. ``first``/``rest`` are the block's gutter."""
    rows: list[Row] = []
    lines = str(text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    # Leading/trailing blank lines are the model's formatting, not content.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    def prefix():
        return first if not rows else rest

    def add(chunks, fill=""):
        for chunk in chunks:
            rows.append(Row(chunk, _plain(chunk), fill=fill))

    blank_pending = False
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            blank_pending = bool(rows)
            index += 1
            continue
        if blank_pending:
            rows.append(Row(list(rest), _plain(rest)))
            blank_pending = False

        if stripped.startswith("```"):
            language = stripped[3:].strip()
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1  # closing fence
            gutter = prefix()
            inner = width - cell_width(_plain(rest)) - 2
            if language:
                label = [("class:md.codeblock", " "),
                         ("class:md.codelang", crop(language, inner))]
                rows.append(Row(_merge(list(gutter) + label),
                                _plain(gutter) + " " + language,
                                fill="class:md.codeblock"))
                gutter = rest
            for code_line in code or [""]:
                for chunk in wrap([("class:md.codeblock", code_line)], width,
                                  list(gutter) + [("class:md.codeblock", " ")],
                                  list(rest) + [("class:md.codeblock", " ")],
                                  hard=True):
                    rows.append(Row(chunk, _plain(chunk),
                                    fill="class:md.codeblock"))
                gutter = rest
            continue

        if (stripped.startswith("|") and index + 1 < len(lines)
                and _TABLE_SEP_RE.match(lines[index + 1])):
            table = [raw, lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                table.append(lines[index])
                index += 1
            rows.extend(_render_table(table, width, prefix(), rest))
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            style = ("class:md.h1" if level == 1 else
                     "class:md.h2" if level == 2 else "class:md.h3")
            add(wrap(inline(heading.group(2), style), width, prefix(), rest))
            index += 1
            continue
        if _HR_RE.match(stripped):
            rule_width = max(4, min(40, width - cell_width(_plain(rest))))
            add([_merge(list(prefix()) + [("class:separator", "─" * rule_width)])])
            index += 1
            continue
        if stripped.startswith(">"):
            quote = stripped.lstrip(">").strip()
            add(wrap(inline(quote, "class:md.quote"), width,
                     list(prefix()) + [("class:md.quotebar", "▎ ")],
                     list(rest) + [("class:md.quotebar", "▎ ")]))
            index += 1
            continue
        bullet = _BULLET_RE.match(raw)
        ordered = _ORDERED_RE.match(raw) if not bullet else None
        if bullet or ordered:
            match = bullet or ordered
            depth = min(3, len(match.group(1).replace("\t", "  ")) // 2)
            marker = ("•◦▪▫"[depth] if bullet else match.group(2)) + " "
            pad = "  " * depth
            add(wrap(inline(match.group(3), base), width,
                     list(prefix()) + [("", pad), ("class:md.list", marker)],
                     list(rest) + [("", pad + " " * cell_width(marker))]))
            index += 1
            continue
        add(wrap(inline(raw.rstrip(), base), width, prefix(), rest))
        index += 1
    return rows


# ── blocks ────────────────────────────────────────────────────────────────

@dataclass
class Block:
    kind: str
    key: str
    title: str = ""
    body: str = ""
    status: str = ""
    extra: dict = field(default_factory=dict)
    timestamp: float = 0.0


_NOISE = frozenset({
    "stream.reset", "stream.end", "agent_started", "workflow_started",
})

#: Lines of a failed tool's output shown without asking — the error is what
#: gets read. Successful output stays folded, as in the plain CLI.
TOOL_ERROR_PREVIEW_LINES = 3
#: Upper bound when expanded — the event itself is capped at 2000 chars, but
#: a few hundred short lines still fit in that.
TOOL_EXPANDED_LINES = 400

#: Tools whose result is shown as the Agent's reply right after them; the
#: tool row would only print the same answer twice.
HIDDEN_TOOLS = frozenset({"task_complete"})



def build_blocks(events, agent_name: str, agent_id: str = "") -> list[Block]:
    """Fold an Agent's event log into display blocks, oldest first."""
    blocks: list[Block] = []
    tools: dict[str, Block] = {}
    approvals: dict[str, Block] = {}
    stream: Optional[Block] = None
    last_tool: Optional[Block] = None

    for event in events:
        kind = event.event_type
        if kind == "ai_stream":
            if stream is None:
                stream = Block("assistant", f"s{event.seq}", title=agent_name,
                               status="streaming", timestamp=event.timestamp)
                blocks.append(stream)
            stream.body += event.detail or ""
            continue
        if kind == "ai_end":
            if stream is not None:
                stream.status = ""
            stream = None
            continue
        if stream is not None:
            # Anything else ends the visible stream even without an ai_end
            # (an interrupted turn never sends one).
            stream.status = ""
            stream = None
        if kind in _NOISE:
            continue
        if kind in {"user", "user_message"}:
            text = event.detail or event.summary
            status = str(event.status or "")
            blocks.append(Block("user", f"u{event.seq}", body=text,
                                status=status, timestamp=event.timestamp))
            last_tool = None
        elif kind == "ai":
            blocks.append(Block("assistant", f"a{event.seq}", title=agent_name,
                                body=event.detail or event.summary,
                                timestamp=event.timestamp))
        elif kind == "tool_started":
            data = event.data or {}
            block = Block("tool", event.tool_call_id or f"t{event.seq}",
                          title=str(data.get("name") or data.get("content")
                                    or event.summary.split("  ")[0] or "tool"),
                          body="", status="running",
                          extra={"salient": str(data.get("command") or "")},
                          timestamp=event.timestamp)
            blocks.append(block)
            if event.tool_call_id:
                tools[event.tool_call_id] = block
            last_tool = block
        elif kind in {"tool_finished", "tool"}:
            data = event.data or {}
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            ok = meta.get("ok")
            block = tools.pop(event.tool_call_id, None) if event.tool_call_id else None
            if block is None:
                name, _sep, salient = (event.summary or "tool").partition("  ")
                block = Block("tool", event.tool_call_id or f"t{event.seq}",
                              title=str(data.get("content") or name or "tool"),
                              extra={"salient": salient.strip()},
                              timestamp=event.timestamp)
                blocks.append(block)
            if meta.get("salient") and not block.extra.get("salient"):
                block.extra["salient"] = str(meta.get("salient"))
            block.status = "error" if ok is False or event.status in {
                "error", "failed"} else "done"
            block.extra["elapsed"] = max(0.0, event.timestamp - block.timestamp)
            last_tool = block
        elif kind == "tool_output":
            target = last_tool
            if target is None:
                target = Block("tool", f"t{event.seq}", title="output",
                               status="done", timestamp=event.timestamp)
                blocks.append(target)
            target.body = (target.body + "\n" if target.body else "") + \
                (event.detail or "")
        elif kind == "agent_message":
            outgoing = bool(agent_id) and event.agent_id == agent_id
            blocks.append(Block("message", f"m{event.seq}",
                                title=event.target_agent_id if outgoing
                                else event.agent_id,
                                body=event.detail or event.summary,
                                status="out" if outgoing else "in",
                                timestamp=event.timestamp))
        elif kind == "agent_spawned":
            blocks.append(Block("message", f"m{event.seq}",
                                title=event.parent_agent_id or "parent",
                                body=event.detail or event.summary,
                                status="task", timestamp=event.timestamp))
        elif kind == "approval_requested":
            approval_id = str((event.data or {}).get("approvalId") or event.seq)
            block = Block("approval", f"p{approval_id}",
                          title=str((event.data or {}).get("kind") or "action"),
                          body=event.summary, status="waiting",
                          extra={"detail": event.detail},
                          timestamp=event.timestamp)
            approvals[approval_id] = block
            blocks.append(block)
        elif kind == "approval_resolved":
            approval_id = str((event.data or {}).get("approvalId") or "")
            block = approvals.get(approval_id)
            status = "approved" if event.status == "approved" else "denied"
            if block is not None:
                block.status = status
            else:
                blocks.append(Block("approval", f"p{event.seq}",
                                    title="action", body=event.summary,
                                    status=status, timestamp=event.timestamp))
        elif kind == "system" and str((event.data or {}).get("kind")) == "billing":
            # Charges belong to /usage, not to the conversation.
            continue
        elif kind == "agent_done":
            # The reply above is the end of the turn; no footer line.
            continue
        elif kind in {"agent_aborted"}:
            blocks.append(Block("notice", f"x{event.seq}", status="error",
                                title="Interrupted", body=event.detail or "",
                                timestamp=event.timestamp))
        elif kind in {"agent_error", "input_rejected", "step_failed",
                      "node_failed", "user_message_failed"}:
            title = {"input_rejected": "Not sent",
                     "user_message_failed": "Not delivered"}.get(kind, "Failed")
            blocks.append(Block("notice", f"x{event.seq}", status="error",
                                title=title, body=event.summary or event.detail,
                                timestamp=event.timestamp))
        else:
            summary = event.summary or kind
            if summary and summary != kind:
                blocks.append(Block("notice", f"n{event.seq}", status="info",
                                    title=summary, timestamp=event.timestamp))
    # A tool that never reported back is still "running" only while the
    # Agent is; the caller decides that — leave the status as recorded.
    return blocks


# ── block rendering ───────────────────────────────────────────────────────

_LINE_NO_RE = re.compile(r"^(\s*\d+)(→|\t|:|\|)")


def _output_line_fragments(line: str, diff: bool) -> list:
    if diff and line.startswith("+") and not line.startswith("+++"):
        return [("class:diff.add", line)]
    if diff and line.startswith("-") and not line.startswith("---"):
        return [("class:diff.del", line)]
    if diff and line.startswith("@@"):
        return [("class:diff.hunk", line)]
    match = _LINE_NO_RE.match(line)
    if match:
        return [("class:out.lineno", match.group(1).strip().rjust(4) + " "),
                ("class:out", line[match.end():])]
    return [("class:out", line)]


def _tool_meta(block: Block, lines: list) -> str:
    """The CLI's own tool-row tail: ``42L · 2.3s`` / ``failed · 42L``."""
    parts = []
    if block.status == "error":
        parts.append("failed")
    if lines:
        parts.append(f"{len(lines)}L")
    elapsed = block.extra.get("elapsed") or 0.0
    if elapsed >= 2.0:
        parts.append(f"{elapsed:.1f}s")
    return f" {symbols.BULLET} ".join(parts)


def _tool_rows(block: Block, width: int, expanded: bool) -> list[Row]:
    """``● name  hint  meta`` — the row the plain CLI prints for a tool.

    Output stays folded behind the row (a click or Ctrl+O opens it); only a
    failure shows its first lines unasked, because that is what gets read.
    """
    status = block.status or "done"
    bullet_style = {"running": "class:tool.running", "error": "class:tool.error"
                    }.get(status, "class:tool.ok")
    output = str(block.body or "").replace("\r\n", "\n").rstrip("\n")
    # A bare CR overwrites the line (progress bars); keep what stays visible.
    lines = [line.split("\r")[-1] for line in output.split("\n")] \
        if output.strip() else []
    # A tool's output often opens with a blank or a repeat of the command.
    while lines and not lines[0].strip():
        lines.pop(0)
    preview = TOOL_ERROR_PREVIEW_LINES if status == "error" else 0
    limit = TOOL_EXPANDED_LINES if expanded else preview
    toggle = ("toggle", block.key) if len(lines) > preview else None

    meta = _tool_meta(block, lines)
    marker = ("▾" if expanded else "▸") if toggle else ""
    name_w = cell_width(block.title)
    tail_w = (cell_width(meta) + 2 if meta else 0) + (2 if marker else 0)
    salient = " ".join(str(block.extra.get("salient") or "").split())
    room = width - 2 - name_w - tail_w - 2
    head = [(bullet_style, symbols.DOT + " "), ("class:tool.name", block.title)]
    if salient and room > 6:
        head.append(("class:tool.arg", "  " + crop(salient, room)))
    if meta:
        head.append(("class:tool.meta", "  " + meta))
    if marker:
        head.append(("class:tool.toggle", "  " + marker))
    rows = [Row(_merge(head), _plain(head), fill="class:tool.row",
                action=toggle, anim="tool" if status == "running" else "",
                block=block.key)]
    rows[0].fragments = [(f"{s} class:tool.row", t) for s, t in rows[0].fragments]

    diff = any(line.startswith("@@") for line in lines[:40]) or \
        any(w in block.title.lower() for w in ("edit", "write", "patch", "diff"))
    gutter = [("class:tool.gutter", "  │ ")]
    for line in lines[:limit]:
        for chunk in wrap(_output_line_fragments(line, diff), width,
                          gutter, gutter, hard=True):
            rows.append(Row(chunk, _plain(chunk), action=toggle,
                            block=block.key))
    hidden = len(lines) - min(len(lines), limit)
    if hidden and limit:
        label = f"… +{hidden} lines"
        rows.append(Row(_merge(gutter + [("class:muted", label)]),
                        "  │ " + label, action=toggle, block=block.key))
    return rows


def _box(title: list, body: list[list], width: int, style: str) -> list[Row]:
    """A rounded box of ``width`` cells; ``body`` is a list of fragment rows."""
    inner = width - 4
    title_w = cell_width(_plain(title))
    top = [(style, "╭─ ")] + title + [(style, " " + "─" * max(0, width - 5 - title_w) + "╮")] \
        if title else [(style, "╭" + "─" * (width - 2) + "╮")]
    rows = [Row(_merge(top), _plain(top))]
    for line in body:
        pad = max(0, inner - cell_width(_plain(line)))
        frags = [(style, "│ ")] + line + [("", " " * pad), (style, " │")]
        rows.append(Row(_merge(frags), _plain(frags)))
    bottom = [(style, "╰" + "─" * (width - 2) + "╯")]
    rows.append(Row(bottom, _plain(bottom)))
    return rows


def profile_rows(profile: dict, width: int) -> list[Row]:
    """The Agent's identity card: who it is, what it runs on, where it lives."""
    box_width = max(24, min(width, 76))
    inner = box_width - 4
    body: list[list] = []

    def para(text: str, style: str):
        for chunk in wrap([(style, text)], inner):
            body.append(chunk)

    if profile.get("title"):
        para(profile["title"], "class:profile.title")
    if profile.get("description"):
        para(profile["description"], "class:profile.text")
    body.append([])
    label_w = 8
    for label, value, style in profile_fields(profile):
        first = [("class:profile.label", label.ljust(label_w))]
        for chunk in wrap(value if isinstance(value, list) else [(style, value)],
                          inner, first, [("", " " * label_w)]):
            body.append(chunk)
    name = [("class:profile.name", crop(profile.get("name") or "agent",
                                        max(4, box_width - 24)))]
    if profile.get("role_label"):
        name.append(("class:muted", f" {symbols.BULLET} {profile['role_label']}"))
    return _box(name, body, box_width, "class:profile.border")


def profile_fields(profile: dict) -> list[tuple]:
    """(label, value, style) rows shared by the card and the side panel."""
    fields = []
    model = [("class:profile.value", profile.get("model") or "backend default")]
    availability = profile.get("model_available")
    if availability is True:
        model.append(("class:done", f"  {symbols.OK} available"))
    elif availability is False:
        model.append(("class:error", f"  {symbols.WARN} not offered by backend"))
    elif availability == "checking":
        model.append(("class:muted", "  checking…"))
    if profile.get("model_source") and profile.get("model"):
        model.append(("class:muted", f"  ({profile['model_source']})"))
    fields.append(("model", model, ""))
    deploy_style = "class:deploy.on" if profile.get("deployed") else "class:deploy.off"
    fields.append(("deploy", [(deploy_style, (symbols.DOT if profile.get("deployed")
                                              else symbols.DOT_OPEN) + " "),
                              ("class:profile.value", profile.get("deployment") or "")], ""))
    if profile.get("tools"):
        fields.append(("tools", profile["tools"], "class:profile.value"))
    # The current task is not repeated here: it is the message right below.
    return fields


def render_block(block: Block, width: int, expanded: bool = False,
                 now: Optional[float] = None) -> list[Row]:
    kind = block.kind
    if kind == "user":
        rejected = block.status in {"rejected", "queue_full", "delivery_error"}
        queued = block.status == "queued"
        marker = [("class:user.mark", "› ")]
        rows = [Row(chunk, _plain(chunk), fill="class:user.bg", block=block.key)
                for chunk in wrap(inline(block.body, "class:user.text"), width - 1,
                                  [("class:user.bg", " ")] + marker,
                                  [("class:user.bg", "   ")])]
        for row in rows:
            row.fragments = [(f"{s} class:user.bg" if "user.bg" not in s else s, t)
                             for s, t in row.fragments]
        if queued or rejected:
            note = "queued · delivered at the next step" if queued else "not sent"
            rows.append(Row([("class:muted", "   " + note)], "   " + note))
        return rows
    if kind == "assistant":
        rows = markdown_rows(block.body, width,
                             [("class:assistant.mark", symbols.DOT + " ")],
                             [("", "  ")])
        if not rows:
            rows = [Row([("class:assistant.mark", symbols.DOT + " ")], symbols.DOT)]
        if block.status == "streaming":
            rows[-1].anim = "cursor"
        for row in rows:
            row.block = block.key
        return rows
    if kind == "tool":
        return _tool_rows(block, width, expanded)
    if kind == "message":
        arrow = {"out": "↗ to ", "task": "↙ task from "}.get(block.status, "↙ from ")
        head = [("class:message.bar", "▎ "), ("class:message", arrow),
                ("class:message.name", block.title)]
        rows = [Row(_merge(head), _plain(head), block=block.key)]
        rows.extend(markdown_rows(block.body, width, [("class:message.bar", "▎ ")],
                                  [("class:message.bar", "▎ ")]))
        return rows
    if kind == "approval":
        glyph, style, label = {
            "approved": (symbols.OK, "class:done", "Approved"),
            "denied": (symbols.FAIL, "class:error", "Denied"),
        }.get(block.status, (symbols.DOT_HALF, "class:approval", "Needs your approval"))
        head = [(style, f"{glyph} "), (style, label),
                ("class:muted", f"  {block.title}")]
        rows = [Row(_merge(head), _plain(head), block=block.key)]
        for chunk in wrap([("class:tool.arg", block.body)], width,
                          [("class:tool.gutter", "  │ ")],
                          [("class:tool.gutter", "  │ ")]):
            rows.append(Row(chunk, _plain(chunk), block=block.key))
        return rows
    if kind == "notice":
        if block.status == "error":
            head = [("class:error", f"{symbols.FAIL} {block.title}")]
            if block.body:
                head.append(("class:error.text", "  " + block.body.split("\n")[0]))
            rows = [Row(chunk, _plain(chunk), block=block.key)
                    for chunk in wrap(head, width, (), [("", "  ")])]
            extra = [line for line in str(block.body or "").split("\n")[1:12]
                     if line.strip()]
            for line in extra:
                for chunk in wrap([("class:muted", line)], width,
                                  [("", "  ")], [("", "  ")]):
                    rows.append(Row(chunk, _plain(chunk), block=block.key))
            return rows
        return [Row(chunk, _plain(chunk), block=block.key)
                for chunk in wrap([("class:muted", f"{symbols.BULLET} {block.title}")],
                                  width, (), [("", "  ")])]
    return [Row([("", block.body)], block.body, block=block.key)]


def render_rows(blocks: list[Block], width: int,
                expanded: Iterable[str] = (), expand_all: bool = False
                ) -> list[Row]:
    """Rows for a whole transcript, one blank row between blocks."""
    expanded = set(expanded)
    rows: list[Row] = []
    for block in blocks:
        if block.kind == "tool" and block.title in HIDDEN_TOOLS:
            continue
        rendered = render_block(block, width,
                                expand_all or block.key in expanded)
        if not rendered:
            continue
        if rows:
            rows.append(Row([], ""))
        rows.extend(rendered)
    return rows
