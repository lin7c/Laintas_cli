"""Paged file reading: a cursor, a stable page table, and eviction on turn.

Why this exists
---------------
Measured on a six-agent review batch (2026-08-28): 107 of 206 tool calls were
`fs.read`, one child read a 9624-line file 20 times through 120-line windows and
revisited the same lines three times, and the reading — not the thinking — is
what made the review take seventeen minutes. The context was never the
constraint: the thread held every earlier read in full (usable budget 180k
tokens, threads peaked near 55k), so this was not amnesia. Nothing in the stack
had an opinion about reading, and nothing could even observe it.

The model here is a paged document, not a byte range:

* `read(path)` with no window starts at page 1 and reports `page 1/N`.
* `read(path, page="next")` turns the page. The page being left is dropped from
  the context the model is sent, and replaced by a stub: the line range plus a
  DETERMINISTIC index of what was defined in it, plus the model's own `note` if
  it wrote one. So one file costs one page of context no matter how big it is.
* `read(path, offset=..., limit=...)` is unchanged and does NOT page: checking
  thirty lines around a grep hit is the right call and stays cheap. It does not
  move the cursor and is never evicted by this module.

Two rules keep the abstraction honest:

* The page table is computed ONCE per (path, file version) and cached, so
  "page 3" means the same lines for the whole session and across the agents
  that share the file. Editing the file changes its fingerprint, which drops
  the table and the cursor rather than silently renumbering.
* The index in a stub is generated from the source (ast for Python, patterns
  for TS/JS, headings for Markdown) and never from the model. A summary the
  model declined to write, or wrote badly, must not be able to cost a later
  `edit` its anchor.

State lives in the agent's own `state` dict (persisted with the session, so a
resumed session keeps its place); the projection that applies eviction to the
outgoing request lives in agent_loop.
"""
from __future__ import annotations

import ast
import os
import re
import threading
from typing import Optional

#: Page sizing. The page is sized from the context headroom at the moment the
#: file is FIRST opened, then frozen with the table: a 10k-line file read with
#: 150k tokens free is three pages, and stays three pages for the rest of the
#: session even as the thread fills up. Turning a page frees the previous one,
#: so a big page no longer accumulates — which is exactly what made large reads
#: expensive before eviction existed.
PAGE_MIN_CHARS = 8_000
PAGE_MAX_CHARS = 120_000
PAGE_HEADROOM_RATIO = 0.35
#: Fallback when the loop has not published a headroom estimate (tests, tools
#: called outside a turn). Matches the loop's own per-result fs.read budget.
PAGE_DEFAULT_CHARS = 24_000

#: Boundaries are nudged onto a structural line so a page does not end halfway
#: through a function — a page cut mid-body produces a stub that describes half
#: a thing. Bounded so the nudge can never distort page sizes.
BOUNDARY_SEARCH_RATIO = 0.15

#: Pages the model may hold open beyond the current one (the file it is editing
#: while reading somewhere else). Small on purpose: pinning everything is the
#: behaviour this module exists to stop.
MAX_PINS = 2

#: Index entries kept in one stub. A stub is a pointer, not a second copy.
MAX_INDEX_ENTRIES = 24

#: Deliveries of one page before the result says so out loud. Mirrors
#: context_policy/policy.json -> file_read_retention.repeat_stop.
REPEAT_STOP = 6

#: Files tracked per agent (policy: file_read_retention.max_cached_files).
MAX_TRACKED_FILES = 64

#: Delivered bodies, keyed by (path, fingerprint, page). Process-local and
#: deliberately NOT part of the agent's persisted state: it is a cache, and a
#: cache in the resume file is just a bigger resume file. Serving a re-read
#: from here is Helpwo's `tryServeCachedView` (AutonomousKernel.ts): it saves
#: the disk round-trip and, more importantly, guarantees that one page number
#: never yields two different bodies inside a turn.
_BODY_CACHE: "dict[tuple, str]" = {}
_BODY_CACHE_LOCK = threading.Lock()
MAX_CACHED_BODY_CHARS = 200_000
MAX_CACHED_BODIES = 24


def cache_body(path: str, fp: tuple, page: int, payload: dict) -> None:
    """Remember one delivered page (body plus the counts that describe it)."""
    body = (payload or {}).get("body") or ""
    if not body or len(body) > MAX_CACHED_BODY_CHARS:
        return
    with _BODY_CACHE_LOCK:
        _BODY_CACHE[(path, tuple(fp), int(page))] = dict(payload)
        while len(_BODY_CACHE) > MAX_CACHED_BODIES:
            _BODY_CACHE.pop(next(iter(_BODY_CACHE)), None)


def cached_body(path: str, fp: tuple, page: int) -> dict:
    """Exactly what was last delivered for this page of this file version."""
    with _BODY_CACHE_LOCK:
        hit = _BODY_CACHE.get((path, tuple(fp), int(page)))
        return dict(hit) if hit else {}


# ── File identity ──────────────────────────────────────────────────────────

def fingerprint(path: str) -> tuple:
    """(size, mtime_ns) — cheap, and changes on every write we care about."""
    try:
        st = os.stat(path)
        return (int(st.st_size), int(st.st_mtime_ns))
    except OSError:
        return (0, 0)


# ── Page table ─────────────────────────────────────────────────────────────

_PY_BOUNDARY = re.compile(r"^(?:@|def |class |async def )")
_TS_BOUNDARY = re.compile(
    r"^(?:export |function |class |interface |type |const |async function )")
_MD_BOUNDARY = re.compile(r"^#{1,3} ")


def _boundary_re(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return _PY_BOUNDARY
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return _TS_BOUNDARY
    if ext in (".md", ".markdown"):
        return _MD_BOUNDARY
    return None


def page_chars_for(headroom_chars: int) -> int:
    """How many characters one page may hold, given the context headroom."""
    if headroom_chars <= 0:
        target = PAGE_DEFAULT_CHARS
    else:
        target = int(headroom_chars * PAGE_HEADROOM_RATIO)
    return max(PAGE_MIN_CHARS, min(PAGE_MAX_CHARS, target))


def build_page_table(path: str, page_chars: int) -> list:
    """Split a file into [start_line, end_line] pages (1-based, inclusive).

    Deterministic for a given (file content, page_chars): the same inputs give
    byte-identical pages, which is what lets a page number be quoted later.
    """
    try:
        with open(path, "rb") as fh:
            widths = [len(raw) for raw in fh]
    except OSError:
        return []
    total = len(widths)
    if total == 0:
        return []

    boundary = _boundary_re(path)
    starts: Optional[list] = None
    if boundary is not None:
        starts = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for no, line in enumerate(fh, 1):
                    if boundary.match(line):
                        starts.append(no)
        except OSError:
            starts = None
    start_set = set(starts or ())

    pages: list = []
    line = 1
    while line <= total:
        used = 0
        end = line - 1
        while end < total and used + widths[end] <= page_chars:
            used += widths[end]
            end += 1
        if end < line:                      # one line wider than a whole page
            end = line
        if end < total and start_set:
            # Nudge the cut onto the nearest structural line, forward first so
            # a page ends just before a definition rather than inside it.
            span = max(1, int((end - line + 1) * BOUNDARY_SEARCH_RATIO))
            best = None
            for candidate in range(end + 1, min(total, end + span) + 2):
                if candidate in start_set:
                    best = candidate - 1
                    break
            if best is None:
                for candidate in range(end, max(line, end - span) - 1, -1):
                    if candidate in start_set:
                        best = candidate - 1
                        break
            if best is not None and best >= line:
                end = best
        pages.append([line, min(end, total)])
        line = min(end, total) + 1
    return pages


def page_of_line(pages: list, line: int) -> int:
    """1-based page holding `line` (clamped)."""
    for i, (start, end) in enumerate(pages, 1):
        if start <= line <= end:
            return i
    return len(pages) or 1


# ── Deterministic index ────────────────────────────────────────────────────

_TS_INDEX = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:(?:async\s+)?function\s+(?P<fn>\w+)"
    r"|class\s+(?P<cls>\w+)"
    r"|interface\s+(?P<iface>\w+)"
    r"|type\s+(?P<type>\w+)\s*="
    r"|(?:const|let)\s+(?P<const>\w+)\s*[:=][^=])")


def index_entries(path: str, start: int, end: int) -> list:
    """What this line range DEFINES, as "kind name line" strings.

    Generated from the source, never from the model: a stub whose index came
    from a summary would let a skipped or lazy summary cost a later edit its
    anchor.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    entries: list = []
    if ext == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)):
                    continue
                line = int(getattr(node, "lineno", 0) or 0)
                if not (start <= line <= end):
                    continue
                kind = "class" if isinstance(node, ast.ClassDef) else "def"
                entries.append((line, f"{kind} {node.name} {line}"))
            entries.sort()
            return [e for _, e in entries[:MAX_INDEX_ENTRIES]]
    lines = text.splitlines()
    for no in range(start, min(end, len(lines)) + 1):
        line = lines[no - 1]
        if ext in (".md", ".markdown"):
            if _MD_BOUNDARY.match(line):
                entries.append((no, f"{line.strip()[:60]} {no}"))
            continue
        m = _TS_INDEX.match(line)
        if m:
            name = next(v for v in m.groupdict().values() if v)
            kind = next(k for k, v in m.groupdict().items() if v)
            entries.append((no, f"{kind} {name} {no}"))
    return [e for _, e in entries[:MAX_INDEX_ENTRIES]]


def render_stub(path: str, page: int, pages_total: int,
                start: int, end: int, index: list, note: str = "") -> str:
    """The ~150 characters that stand in for an evicted page."""
    name = os.path.basename(path) or path
    head = (f"[{name} page {page}/{pages_total} - lines {start}-{end} - "
            f"dropped from context; re-read with page={page}]")
    parts = [head]
    if index:
        parts.append("index: " + " | ".join(index))
    note = " ".join(str(note or "").split())
    if note:
        parts.append("note: " + note[:600])
    return "\n".join(parts)


# ── Per-agent cursor state (lives in the agent's own `state` dict) ──────────

def _store(state: dict) -> dict:
    store = state.get("_pager")
    if not isinstance(store, dict):
        store = {}
        state["_pager"] = store
    return store


def _trim(store: dict) -> None:
    if len(store) <= MAX_TRACKED_FILES:
        return
    for key in sorted(store, key=lambda k: store[k].get("seq", 0))[
            :len(store) - MAX_TRACKED_FILES]:
        store.pop(key, None)


def get_file_state(state: dict, path: str, fp: tuple,
                   headroom_chars: int, seq: float) -> dict:
    """This agent's record for one file, rebuilt when the file changed.

    The page table is frozen on creation: page numbers must not move under a
    session that is quoting them.
    """
    store = _store(state)
    entry = store.get(path)
    if entry is not None and tuple(entry.get("fp") or ()) == tuple(fp):
        entry["seq"] = seq
        return entry
    changed = entry is not None
    entry = {
        "fp": list(fp),
        "pages": build_page_table(path, page_chars_for(headroom_chars)),
        "page": 0,             # 0 = never opened
        "pins": [],
        "reads": {},
        "stubs": {},
        "seq": seq,
        "repaged": changed,    # reported once, so a shifted page number is not
                               # discovered by the model as a wrong answer
    }
    store[path] = entry
    _trim(store)
    return entry


def resolve_page(entry: dict, requested) -> int:
    """Map `page` ("next" / "prev" / N / None) onto a real page number."""
    total = len(entry.get("pages") or [])
    if total == 0:
        return 1
    current = int(entry.get("page") or 0)
    if requested is None or requested == "":
        return current or 1
    if isinstance(requested, str):
        token = requested.strip().lower()
        if token in ("next", "+1", "forward"):
            return min(total, (current or 0) + 1)
        if token in ("prev", "previous", "-1", "back"):
            return max(1, (current or 2) - 1)
        if token in ("first", "start"):
            return 1
        if token in ("last", "end"):
            return total
        if token.isdigit():
            return max(1, min(total, int(token)))
        return current or 1
    try:
        return max(1, min(total, int(requested)))
    except (TypeError, ValueError):
        return current or 1


def note_page_delivered(entry: dict, path: str, page: int) -> int:
    """Record a delivery; returns how many times this page has been served."""
    reads = entry.setdefault("reads", {})
    key = str(page)
    reads[key] = int(reads.get(key, 0)) + 1
    entry["page"] = page
    if not entry.get("stubs", {}).get(key):
        span = entry["pages"][page - 1]
        entry.setdefault("stubs", {})[key] = render_stub(
            path, page, len(entry["pages"]), span[0], span[1],
            index_entries(path, span[0], span[1]))
    return reads[key]


def attach_note(entry: dict, path: str, page: int, note: str) -> bool:
    """Attach the model's summary to a page's stub. Returns True if stored."""
    if not note or not str(note).strip():
        return False
    pages = entry.get("pages") or []
    if not (1 <= page <= len(pages)):
        return False
    span = pages[page - 1]
    entry.setdefault("stubs", {})[str(page)] = render_stub(
        path, page, len(pages), span[0], span[1],
        index_entries(path, span[0], span[1]), note)
    return True


def set_pin(entry: dict, page: int, pinned: bool) -> Optional[int]:
    """Pin/unpin a page. Returns the page dropped when the pin budget is full."""
    pins = [int(p) for p in entry.setdefault("pins", []) if int(p) != page]
    dropped = None
    if pinned:
        pins.append(int(page))
        while len(pins) > MAX_PINS:
            dropped = pins.pop(0)
    entry["pins"] = pins
    return dropped


def live_pages(state: dict, path: str) -> set:
    """Pages that must survive the projection: the cursor page plus pins."""
    entry = _store(state).get(path)
    if not entry:
        return set()
    keep = {int(entry.get("page") or 0)}
    keep.update(int(p) for p in entry.get("pins") or [])
    return {p for p in keep if p > 0}


def stub_for(state: dict, path: str, page: int) -> str:
    entry = _store(state).get(path)
    if not entry:
        return ""
    return str((entry.get("stubs") or {}).get(str(page)) or "")


# ── Visible-range ledger (what the model can still SEE) ────────────────────
#
# Helpwo (toolRuntimeState.ts) refuses a read whose range it has already
# served. That is the right instinct and it has one blind spot: the gate knows
# what was READ, not what is still in the context. After its compaction trims
# an old `cat` result, the model is told "already fully read" about content it
# can no longer see, and every read of that file stays blocked for the rest of
# the run (AutonomousKernel.ts:2799 runs the gate BEFORE the cache that was
# meant to back it up).
#
# Here the projection that evicts pages already knows exactly which reads
# survive into the outgoing request, so the block can be conditioned on it:
# refuse only what the model can still read off its own transcript, and always
# allow re-fetching what was dropped.

def merge_ranges(ranges: list) -> list:
    out: list = []
    for start, end in sorted(tuple(r) for r in ranges):
        if out and start <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return [list(r) for r in out]


def covered(ranges: list, start: int, end: int) -> bool:
    """Is [start, end] entirely inside `ranges`?"""
    for a, b in ranges:
        if a <= start and end <= b:
            return True
    return False


def visible_ranges(state: dict, path: str) -> list:
    store = state.get("_visible_reads")
    if not isinstance(store, dict):
        return []
    return [list(r) for r in (store.get(path) or [])]


def mark_edited(state: dict, path: str) -> None:
    """Record that our own tools changed this file.

    Coverage is cleared for the same reason Helpwo clears it: after an edit the
    line numbers have moved, so an old range must never block a fresh read.
    """
    entry = _store(state).get(path)
    if entry is not None:
        entry["edited"] = True


def stale_files(state: dict) -> list:
    store = state.get("_pager")
    if not isinstance(store, dict):
        return []
    return sorted(p for p, e in store.items()
                  if isinstance(e, dict) and e.get("edited"))


# ── Hand-rolled paging ─────────────────────────────────────────────────────
#
# Measured in a live session on 2026-08-28, hours after paging shipped: 49 of
# 61 reads were still hand-rolled windows, and on one file 5 of 6 consecutive
# windows started exactly where the previous one ended. A window that resumes
# where the last one stopped is not a targeted look at anything — it is a page
# turn the caller is doing by hand, one model round trip at a time, and it is
# the one read pattern that can be told apart from a legitimate targeted read
# without guessing.
#
# This reports; it does not refuse. A refusal here would land on the one case
# where the caller is right and we are wrong (a genuine sequential scan), and
# the cost of being wrong is that the file cannot be read at all.
WALK_TOLERANCE_LINES = 5
WALK_NOTICE_AFTER = 2


def note_window(state: dict, path: str, start: int, end: int) -> int:
    """Record a windowed read; return how many consecutive walk steps it makes.

    0 = not a walk. 1 = it resumed where the last window ended. N = the Nth
    step of a walk down the same file.
    """
    entry = _store(state).setdefault(path, {})
    last = entry.get("last_window")
    streak = int(entry.get("walk_streak") or 0)
    if last and abs(int(last[1]) + 1 - start) <= WALK_TOLERANCE_LINES:
        streak += 1
    else:
        streak = 0
    entry["last_window"] = [start, end]
    entry["walk_streak"] = streak
    entry["seq"] = entry.get("seq") or 0
    return streak


def walk_notice(path: str, start: int, streak: int, headroom_chars: int) -> str:
    """What to tell a caller that is turning pages by hand."""
    if streak < WALK_NOTICE_AFTER:
        return ""
    pages = build_page_table(path, page_chars_for(headroom_chars))
    if not pages:
        return ""
    page = page_of_line(pages, start)
    return (f"these are hand-rolled pages: {streak + 1} windows in a row have "
            f"resumed where the last one ended. read(path, page={page}) covers "
            f"lines {pages[page - 1][0]}-{pages[page - 1][1]} in one call "
            f"({len(pages)} pages total), and turning a page frees the previous "
            f"one from your context")
