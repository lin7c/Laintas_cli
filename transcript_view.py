"""The single renderer for conversation events — live and replayed.

A turn is displayed exactly once in code: the agent loop builds a transcript
event for every tool call, appends it to ``chat_history`` and hands that same
event to the renderer here. ``/resume`` (and ``/told all``) replay the saved
events through the same renderer, so a restored session is identical to the
one you were just watching instead of a second, drifting implementation of
"what a tool call looks like".

The rule that keeps it that way: **everything the display needs must live on
the event**. The live loop has the whole tool result in hand and the replay
has only what was persisted, so anything read off the result dict (a match
count, a completion flag, an elapsed time) is copied onto the event under
``extra`` when it is built. Nothing here may reach for a result object.

Older sessions were saved before ``extra`` existed; their events render
through this same code with whatever they do carry, just with a thinner
status tail.
"""

import re

import symbols

# Tools whose successful calls print nothing: the live task list already
# reflects them, and a replay must stay just as quiet.
SILENT_TOOLS = {"task.create", "task.update", "task.list", "task.get"}

# Successful calls folded into one grouped row instead of a line each.
QUIET_READ_TOOLS = {
    "fs.read", "fs.grep", "fs.list", "fs.ls",
    "memory.search", "memory.get",
}

# Tools whose diff is worth a glance under the row.
DIFF_TOOLS = ("fs.write", "fs.edit", "fs.multi_edit")

# Result keys copied onto a tool event so the status tail can be rebuilt
# without the result dict. Keep this small — it is persisted per call.
_EXTRA_KEYS = ("completed", "status", "count", "error", "via")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from captured output."""
    return _ANSI_RE.sub("", str(text or ""))


# ── Text fitting ──────────────────────────────────────────────────────────

def cell_len(value: str) -> int:
    """Return terminal display-cell width (CJK/emoji aware)."""
    try:
        from rich.cells import cell_len as _cell_len
        return _cell_len(str(value or ""))
    except Exception:
        return len(str(value or ""))


def crop_cells(value: str, width: int, *, middle: bool = False) -> str:
    """Crop plain text to exactly a display-cell budget without splitting glyphs."""
    value = str(value or "")
    width = max(0, int(width))
    if cell_len(value) <= width:
        return value
    if width <= 0:
        return ""
    if width == 1:
        return "…"

    def _take(text: str, budget: int, reverse: bool = False) -> str:
        chars = reversed(text) if reverse else iter(text)
        kept = []
        used = 0
        for char in chars:
            cells = max(0, cell_len(char))
            if used + cells > budget:
                break
            kept.append(char)
            used += cells
        if reverse:
            kept.reverse()
        return "".join(kept)

    if not middle:
        return _take(value, width - 1) + "…"
    left_budget = (width - 1) // 2
    right_budget = width - 1 - left_budget
    return _take(value, left_budget) + "…" + _take(value, right_budget, reverse=True)


def shortest_unique(paths: list) -> list:
    """Return the shortest distinguishing suffix for each path.

    When multiple paths share the same basename (e.g. ``a/router.py`` and
    ``b/router.py``), include enough parent segments to tell them apart.
    """
    if not paths:
        return []
    parts_list = [p.rstrip("/").replace("\\", "/").split("/") for p in paths]
    result = []
    for i, parts_i in enumerate(parts_list):
        chosen = parts_i[-1] if parts_i else ""
        for depth in range(1, len(parts_i) + 1):
            candidate = "/".join(parts_i[-depth:])
            if all(
                candidate != "/".join(parts_j[-depth:])
                for j, parts_j in enumerate(parts_list) if j != i
            ):
                chosen = candidate
                break
        result.append(chosen)
    return result


def compact_tool_line(display_name: str, hint: str, meta: str, width: int,
                      hint_middle: bool = True) -> tuple:
    """Fit a compact tool row, preserving status metadata before command prose."""
    name = str(display_name or "tool")
    hint = re.sub(r"\s+", " ", str(hint or "")).strip()
    meta = re.sub(r"\s+", " ", str(meta or "")).strip()
    fixed = 5 + cell_len(name) + (2 if hint else 0) + (2 if meta else 0)
    available = max(8, int(width or 80) - fixed)
    if meta:
        meta_budget = min(max(12, available // 2), max(12, cell_len(meta)))
        meta = crop_cells(meta, meta_budget, middle=("/why" in meta or "/debug" in meta))
        available -= cell_len(meta)
    hint = crop_cells(hint, max(4, available), middle=hint_middle)
    return name, hint, meta


def fold_lines(text: str, limit: int) -> list:
    """Fold output to ``limit`` lines: first half + "… N more" + last half."""
    lines = [line for line in str(text or "").split("\n") if line.strip()]
    if limit <= 0 or len(lines) <= limit:
        return lines
    half = limit // 2
    hidden = len(lines) - limit
    return lines[:half] + [f"… {hidden} more lines"] + lines[-half:]


# ── Events ────────────────────────────────────────────────────────────────

def build_tool_event(name: str, display_name: str, summary: str, output: str,
                     result: dict, *, elapsed: float = 0.0, call_id: str = "",
                     returncode=None, diff_cap: int = 4000) -> dict:
    """Build the transcript record for one tool call.

    This is the only place a tool result is read for display purposes: what
    the row needs is copied onto the event now, so replaying the event later
    produces the identical row.
    """
    result = result or {}
    extra = {}
    for key in _EXTRA_KEYS:
        value = result.get(key)
        if value not in (None, "", False):
            extra[key] = value
    if name == "fs.grep" and isinstance(result.get("matches"), int):
        extra["matches"] = result["matches"]
    if name == "web.search":
        count = result.get("count")
        if not isinstance(count, int):
            payload = result.get("result")
            if isinstance(payload, dict):
                payload = payload.get("results")
            count = len(payload) if isinstance(payload, list) else 0
        extra["count"] = count
    if name == "task.complete":
        extra["complete_summary"] = str(result.get("summary") or "")
    event = {
        "role": "tool",
        "content": str(output or "")[:2000],
        "tool_name": name,
        "display_name": display_name or name,
        "summary": str(summary or "")[:200],
        "call_id": call_id,
        "ok": bool(result.get("ok", False)),
        "returncode": returncode,
        "elapsed": round(float(elapsed or 0.0), 3),
    }
    if extra:
        event["extra"] = extra
    if name in DIFF_TOOLS and result.get("diff"):
        event["diff"] = str(result["diff"])[:diff_cap]
    return event


_LEGACY_TOOL_RE = re.compile(
    r"^\[(?P<call>call_[^\]]+)\]\s+"
    r"(?P<name>[^\s(]+)\((?P<summary>.*?)\)\s+→\s+(?P<result>.*)$",
    re.DOTALL,
)


def normalize_event(message: dict):
    """Return a canonical event, converting pre-typed-tool records.

    Tool calls used to be stored as ``knowledge`` prose. Those sessions are
    still resumable, so they are translated here rather than given a second
    rendering path.
    """
    if not isinstance(message, dict):
        return None
    if message.get("role") == "tool":
        return message
    if message.get("role") != "knowledge":
        return message
    match = _LEGACY_TOOL_RE.match(str(message.get("content") or ""))
    if not match:
        return message
    data = match.groupdict()
    summary = data["summary"].strip()
    duplicate_prefix = data["name"] + " "
    if summary.startswith(duplicate_prefix):
        summary = summary[len(duplicate_prefix):].strip()
    return {
        "role": "tool",
        "tool_name": data["name"],
        "display_name": data["name"],
        "summary": summary,
        "content": data["result"].strip(),
        # Legacy records never stored a verdict; a red dot would be a lie.
        "ok": True,
        "returncode": None,
    }


def tool_meta(event: dict) -> str:
    """The status tail of a tool row, derived only from the event."""
    name = str(event.get("tool_name") or "")
    ok = bool(event.get("ok"))
    rc = event.get("returncode")
    extra = event.get("extra") or {}
    output = str(event.get("content") or "")
    elapsed = float(event.get("elapsed") or 0.0)

    def _lines() -> int:
        return len(output.split("\n")) if output else 0

    def _cause() -> str:
        text = str(extra.get("error") or output or "").strip()
        return re.sub(r"\s+", " ", text).replace("[", "\\[")

    meta = ""
    if name == "terminal.send" and ok:
        count = _lines()
        meta = f"sent {symbols.BULLET} {count}L" if count else "sent"
    elif name in ("terminal.exec", "terminal.read", "terminal.wait") and ok:
        if extra.get("completed"):
            meta = (f"completed {symbols.BULLET} exit {rc}" if rc is not None
                    else f"completed {symbols.BULLET} exit unknown")
        elif name == "terminal.exec":
            meta = f"started {symbols.BULLET} running"
        else:
            meta = str(extra.get("status") or "running").replace("_", " ")
    elif name == "shell.exec":
        if ok:
            count = _lines()
            meta = f"{count}L {symbols.BULLET} exit {rc}" if count else f"exit {rc}"
        else:
            meta = f"exit {rc}"
            cause = _cause()
            if cause:
                meta += f" {symbols.BULLET} {cause[:120]}"
            meta += f" {symbols.BULLET} /why"
    elif name == "fs.grep" and ok:
        matches = extra.get("matches", 0)
        meta = f"{matches} match{'es' if matches != 1 else ''}"
    elif name == "web.search" and ok:
        count = extra.get("count", 0)
        meta = f"{count} result{'s' if count != 1 else ''}"
    elif not ok:
        cause = _cause()
        meta = f"{cause} {symbols.BULLET} /why" if cause else "/why"
    if elapsed >= 2.0:
        meta = f"{meta} {symbols.BULLET} {elapsed:.1f}s" if meta else f"{elapsed:.1f}s"
    return meta


def read_category(tool_name: str) -> str:
    """The grouped-row label a quiet read belongs to."""
    if tool_name == "fs.grep":
        return "Search"
    if tool_name in {"fs.list", "fs.ls"}:
        return "List"
    if tool_name.startswith("memory."):
        return "Memory"
    return "Read"


# ── Rows ──────────────────────────────────────────────────────────────────

def status_dot(ok: bool) -> str:
    """Green dot = quiet success; red dot = a call that actually failed."""
    return (f"[success]{symbols.DOT}[/success]" if ok
            else f"[error]{symbols.DOT}[/error]")


def tool_row(event: dict, width: int) -> str:
    """One aligned tool row: ``  ● Name  hint  meta``."""
    from rich.markup import escape
    tool_name = str(event.get("tool_name") or "")
    hint_plain = str(event.get("summary") or "") or str(
        event.get("display_name") or tool_name)
    name, hint, meta = compact_tool_line(
        str(event.get("display_name") or tool_name or "tool"),
        hint_plain, tool_meta(event), width,
        hint_middle=(tool_name != "shell.exec"))
    row = (f"  {status_dot(bool(event.get('ok')))} "
           f"[accent.dim]{escape(name)}[/accent.dim]"
           f"  [muted]{escape(hint)}[/muted]")
    if meta:
        row += f"  [muted]{escape(meta)}[/muted]"
    return row


def read_group_row(hints: list) -> str:
    """Render one consecutive read group as a single row.

    ``hints`` is a list of ``(category, target)`` pairs in call order.
    """
    if not hints:
        return ""
    category = hints[0][0]
    unique_reads = list(dict.fromkeys(item for _kind, item in hints))
    # For Search category, extract the query from the first hint
    # (fs.grep salient is "pattern in path"); show it in the label.
    query_text = ""
    display_reads = unique_reads
    if category == "Search":
        first_hint = hints[0][1]
        if " in " in first_hint:
            query_text, _path_part = first_hint.split(" in ", 1)
            query_text = query_text.strip()[:40]
            # Strip the query prefix from each target for the tail
            display_reads = []
            for item in unique_reads:
                if " in " in item:
                    _, path_part = item.split(" in ", 1)
                    display_reads.append(path_part.strip())
                else:
                    display_reads.append(item)
    # Shortest unique suffix to disambiguate same-name files
    shown_reads = shortest_unique(display_reads[:3])
    read_tail = f" {symbols.BULLET} ".join(shown_reads).replace("[", "\\[")
    if len(unique_reads) > 3:
        read_tail += f" {symbols.BULLET} +{len(unique_reads) - 3}"
    label, singular, plural = {
        "Search": ("Search", "result", "results"),
        "List": ("List", "location", "locations"),
        "Memory": ("Memory", "source", "sources"),
    }.get(category, ("Read", "source", "sources"))
    if query_text:
        label = f'{label} "{query_text}"'
    noun = singular if len(unique_reads) == 1 else plural
    return (f"  [success]{symbols.DOT}[/success] [accent.dim]{label}[/accent.dim]  "
            f"[muted]{len(unique_reads)} {noun} {symbols.BULLET} {read_tail}[/muted]")


def bg_print(console, markup_text: str, width: int = 0) -> None:
    """Print Rich markup with the 'surface' background, padded to the width."""
    from rich.text import Text
    if not width:
        width = console.width or 80
    try:
        text = Text.from_markup(markup_text)
    except Exception:
        text = Text(markup_text)
    text.set_length(width)
    text.stylize("surface")
    console.print(text, highlight=False)


def print_markdown(console, content: str, markdown_cls=None) -> bool:
    """Render Markdown, degrading to plain text instead of raising.

    Frozen PyInstaller builds load Pygments lexers lazily. If the executable's
    embedded archive is damaged, Rich can raise zlib/import errors only when a
    fenced code block first appears. Returns False when it had to fall back,
    so the caller can warn once.
    """
    if markdown_cls is None:
        from rich.markdown import Markdown as markdown_cls  # noqa: N813
    try:
        console.print(markdown_cls(content))
        return True
    except Exception:
        console.print(content, markup=False, highlight=False)
        return False


def emit_simple_diff(console, diff_text: str, depth: int = 0, cap: int = 30) -> None:
    """Render a minimal diff: changed (+/-) lines only, folded at ``cap``.

    Skips file headers, hunk markers and unchanged context — the reader just
    wants a glance at what changed. The full diff stays in /debug and /detail.
    """
    if not diff_text:
        return
    from rich.markup import escape
    if cap <= 0:
        cap = 30
    hunk = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    changed = []          # (style, mark, lineno, text)
    adds = dels = 0
    old_no = new_no = 0
    for line in str(diff_text).splitlines():
        match = hunk.match(line)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(2))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
            changed.append(("success", "┃+", new_no, line[1:]))
            new_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
            changed.append(("error", "┃-", old_no, line[1:]))
            old_no += 1
        elif line.startswith(" "):
            old_no += 1
            new_no += 1
    if not changed:
        return
    total = len(changed)
    inner = "  " * depth + "  "

    def _print_entry(style, mark, no, text):
        if len(text) > 96:
            text = text[:95] + "…"
        bg_print(console, f"{inner}[muted]{no:>4}[/muted] "
                 f"[{style}]{mark}{escape(text)}[/{style}]")

    bg_print(console, f"{inner}[accent]▍[/accent] [success]+{adds}[/success] "
                      f"[error]−{dels}[/error]")
    if total <= cap:
        for style, mark, no, text in changed:
            _print_entry(style, mark, no, text)
    else:
        half = cap // 2
        hidden = total - cap
        for style, mark, no, text in changed[:half]:
            _print_entry(style, mark, no, text)
        bg_print(console, f"{inner}     [muted]… {hidden} more change(s) "
                          f"{symbols.BULLET} /detail on for full[/muted]")
        for style, mark, no, text in changed[-half:]:
            _print_entry(style, mark, no, text)


class TranscriptRenderer:
    """Print transcript events. One instance per display stream.

    Injected, because the two callers own different plumbing: the live loop
    prints through the agent's background-safe console and renders shell
    blocks via ``LoopDeps``, while the replay prints straight to the REPL
    console. What is *decided* — silent tools, read grouping, the status
    tail, folded output, diffs — is decided here, once.
    """

    def __init__(self, console, *, fold_limit=None, markdown_cls=None,
                 command_block=None, depth: int = 0, on_markdown_error=None):
        self.console = console
        self._fold_limit = fold_limit or (lambda: 30)
        self._markdown_cls = markdown_cls
        self._command_block = command_block
        self._depth = depth
        self._on_markdown_error = on_markdown_error
        self._reads = []

    # -- internals --------------------------------------------------------
    @property
    def _width(self) -> int:
        return self.console.width or 80

    def _emit(self, markup: str) -> None:
        bg_print(self.console, markup, self._width)

    def _plain(self, markup: str) -> None:
        self.console.print(markup, highlight=False)

    def flush(self) -> None:
        """Close an open read group."""
        if self._reads:
            self._emit(read_group_row(self._reads))
            self._reads.clear()

    # -- public -----------------------------------------------------------
    def event(self, message: dict) -> None:
        """Print one transcript event in its live shape."""
        event = normalize_event(message)
        if event is None:
            return
        role = str(event.get("role") or "?")
        if role == "tool":
            self._tool(event)
            return
        self.flush()
        if role == "user":
            self._user(event)
        elif role == "assistant":
            self.assistant(str(event.get("content") or ""))
        elif role == "knowledge":
            self._knowledge(str(event.get("content") or ""))
        elif role == "shell":
            self.command_block(str(event.get("command") or ""),
                               event.get("returncode"),
                               str(event.get("content") or ""))
        else:
            from rich.markup import escape
            self._plain(f"[muted]{escape(role)} {symbols.BULLET} "
                        f"{escape(str(event.get('content') or ''))}[/muted]")

    def events(self, history: list) -> None:
        """Replay a run of events, pairing `!command` lines with their output."""
        index = 0
        history = list(history or [])
        while index < len(history):
            message = history[index]
            nxt = history[index + 1] if index + 1 < len(history) else None
            # A `!command` and its output were one block live; the saved shell
            # event keeps only the output, so re-pair them here.
            if (isinstance(message, dict) and isinstance(nxt, dict)
                    and message.get("role") == "user"
                    and message.get("input_kind") == "shell"
                    and nxt.get("role") == "shell"):
                paired = dict(nxt)
                paired["command"] = str(message.get("content") or "")
                self.event(paired)
                index += 2
                continue
            self.event(message)
            index += 1
        self.flush()

    def assistant(self, content: str) -> None:
        """Render model prose."""
        self.flush()
        ok = print_markdown(self.console, content or "*(empty)*",
                            self._markdown_cls)
        if not ok and self._on_markdown_error is not None:
            self._on_markdown_error()

    def note(self, content: str) -> None:
        """Short intermediate narration before a tool call: ``● text``."""
        from rich.markup import escape
        self.flush()
        self._plain(f"[accent]{symbols.BULLET}[/accent] "
                    f"[dim]{escape(content.strip())}[/dim]")

    def command_block(self, command: str, returncode, output: str) -> None:
        """A shell command and its output."""
        self.flush()
        rc = returncode if isinstance(returncode, int) else -1
        if self._command_block is not None:
            self._command_block(command, rc, output, depth=self._depth)
            return
        style = "error" if rc not in (0, -1) else "muted"
        from rich.text import Text
        for line in fold_lines(output, self._fold_limit()):
            self.console.print(Text(f"  {line}", style=style))

    def rule(self) -> None:
        self.flush()
        self.console.rule(style="muted")

    def diff(self, diff_text: str) -> None:
        """A glance at what a write changed."""
        self.flush()
        emit_simple_diff(self.console, diff_text, depth=self._depth,
                         cap=self._fold_limit())

    def _user(self, event: dict) -> None:
        """The line the user typed, in the prompt's own gutter + caret.

        A replayed prompt cannot be the real prompt_toolkit input line, so it
        is drawn to match it — that line is the anchor the eye looks for when
        scanning back through a turn.
        """
        from rich.markup import escape
        kind = str(event.get("input_kind") or "prompt")
        caret = "!" if kind == "shell" else "›"
        self.console.print()
        self._plain(f"[accent]│[/accent] [success]{caret}[/success] "
                    f"{escape(str(event.get('content') or ''))}")

    def _knowledge(self, content: str) -> None:
        """Injected context. Never shown live, so it stays to one dim line —
        dropping it silently would make a replayed turn look emptier than the
        turn actually was."""
        from rich.markup import escape
        first = re.sub(r"\s+", " ", content).strip()
        self._plain(
            f"  [muted]{symbols.INFO} context {symbols.BULLET} "
            f"{escape(crop_cells(first, max(20, self._width - 16)))}[/muted]")

    # -- tool rows --------------------------------------------------------
    def _tool(self, event: dict) -> None:
        name = str(event.get("tool_name") or "")
        ok = bool(event.get("ok"))
        if ok and name in SILENT_TOOLS:
            return
        if ok and name == "task.complete":
            # A non-empty completion summary is rendered as the final answer;
            # only the empty case gets a closing rule.
            extra = event.get("extra") or {}
            if not str(extra.get("complete_summary") or "").strip():
                self.rule()
            return
        hint = str(event.get("summary") or "") or str(
            event.get("display_name") or name)
        if ok and name in QUIET_READ_TOOLS:
            category = read_category(name)
            if self._reads and self._reads[-1][0] != category:
                self.flush()
            self._reads.append((category, hint))
            return
        self.flush()
        self._emit(tool_row(event, self._width))
        self._after_row(event, name)

    def _after_row(self, event: dict, name: str) -> None:
        """Folded shell output / diff glance, under the row that produced it."""
        from rich.markup import escape
        fold_limit = self._fold_limit()
        output = str(event.get("content") or "")
        if name == "shell.exec" and output and fold_limit > 0:
            lines = [ln for ln in strip_ansi(output).split("\n") if ln.strip()]
            if len(lines) > fold_limit:
                for line in fold_lines(strip_ansi(output), fold_limit):
                    self._emit(f"    [muted]{escape(line)}[/muted]")
        if event.get("diff"):
            self.diff(str(event["diff"]))
