"""Reverse tasks — work the AI hands to the person, and checks when they say done.

The usual direction is a person asking and the agent doing. Some work only the
person can do: sign in to a merchant dashboard, pay, shoot a scene, answer an
exercise. Done in plain chat, that work loses its shape after a dozen turns —
nobody can say which step they are on, what "done" meant for it, or whether it
was actually done. A ``.retask`` file is that shape, written down.

The file is the whole state
---------------------------
There is no database and no second copy. Helpwo and this CLI read and write the
same file (Helpwo's parser is ``src/tools/retask-core.ts`` and must stay
behaviourally identical — ``tests/fixtures/retask_vectors.json`` pins both), so
a list made in one is the list shown in the other, and the list survives
context compaction, a refresh, and a new session because it was never in the
conversation to begin with.

Format (line oriented, reads like a markdown checklist)::

    retask 1
    title: Integrate WeChat Pay
    goal: One sandbox Native payment goes through end to end

    ## [x] t1 Register a merchant account
    check: contains .env "WECHAT_MCH_ID="
    note: 2026-09-14 10:02 passed: .env contains WECHAT_MCH_ID

    Sign up at pay.weixin.qq.com with the business licence, then put the
    merchant id into .env as WECHAT_MCH_ID.

    ## [>] t2 Get the sandbox API key
    after: t1
    check: contains .env "WECHAT_API_V3_KEY="

    ...description...

A task header is ``## [<mark>] <id> <title>``. Directly under it, ``after:``,
``check:`` and ``note:`` lines (any order, until the first line that is not
one); everything after that up to the next header is the description, verbatim.

Marks: ``[ ]`` todo, ``[>]`` doing, ``[?]`` submitted (the person says it is
done), ``[x]`` done (checked), ``[!]`` rejected (checked, not done), ``[-]``
skipped.

A claim is not a finding
------------------------
The same split ``agent_contract`` makes for sub-agents: ``submitted`` is the
person's claim, ``done`` is what the checks found. ``set_status(..., "done")``
runs every deterministic check against the workspace first and turns a failure
into ``rejected`` with the specific gaps as a note, so no one — the person or the
model — can mark a task done by saying so. A ``review`` check is the one kind a
file cannot answer (a photo's framing, an exercise's reasoning); completing a
task that has one requires a note naming the evidence that was looked at.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VERSION_LINE = "retask 1"
EXTENSION = ".retask"

TODO = "todo"
DOING = "doing"
SUBMITTED = "submitted"
DONE = "done"
REJECTED = "rejected"
SKIPPED = "skipped"

MARKS = {" ": TODO, ">": DOING, "?": SUBMITTED, "x": DONE, "!": REJECTED, "-": SKIPPED}
STATUS_MARK = {status: mark for mark, status in MARKS.items()}
STATUSES = tuple(STATUS_MARK)

#: A task that no longer needs anything from anyone.
CLOSED = frozenset({DONE, SKIPPED})

CHECK_KINDS = ("file_exists", "contains", "matches", "min_length", "review")
#: How many arguments each kind takes after its name.
_CHECK_ARITY = {"file_exists": 1, "contains": 2, "matches": 2, "min_length": 2, "review": 1}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_HEADER_RE = re.compile(r"^## \[(.)\] (\S+)(?: (.*))?$")
_META_RE = re.compile(r"^(after|check|note):[ \t]?(.*)$")

#: Directories never worth descending into when looking for task lists.
_SKIP_DIRS = frozenset({"node_modules", "venv", ".venv", "__pycache__", "dist",
                        "build", ".git"})
_FIND_DEPTH = 3
#: A description longer than this is cut in the per-turn context block (the
#: file keeps all of it; `retask.read` returns all of it).
_ANCHOR_DESCRIPTION_CHARS = 1500
_CHECK_READ_BYTES = 2 * 1024 * 1024


class RetaskError(ValueError):
    """The file or the requested change is malformed."""


@dataclass
class Task:
    id: str
    title: str
    status: str = TODO
    after: list = field(default_factory=list)
    checks: list = field(default_factory=list)      # raw check lines, e.g. 'contains .env "X="'
    notes: list = field(default_factory=list)
    description: str = ""


@dataclass
class Retask:
    title: str
    goal: str = ""
    tasks: list = field(default_factory=list)

    def task(self, task_id: str) -> Optional[Task]:
        return next((t for t in self.tasks if t.id == task_id), None)


# ── check lines ──────────────────────────────────────────────────────

def tokenize(line: str) -> list:
    """Split a check line on whitespace; "double quotes" group, \\" and \\\\ escape.

    Deliberately not shlex: the TypeScript side has to produce exactly the same
    tokens, and shlex's single quotes and comment handling have no twin there.
    """
    tokens, buf, quoted, i, started = [], [], False, 0, False
    text = str(line or "")
    while i < len(text):
        ch = text[i]
        if quoted:
            if ch == "\\" and i + 1 < len(text) and text[i + 1] in '"\\':
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                quoted = False
            else:
                buf.append(ch)
        elif ch == '"':
            quoted, started = True, True
        elif ch in " \t":
            if started:
                tokens.append("".join(buf))
                buf, started = [], False
        else:
            buf.append(ch)
            started = True
        i += 1
    if quoted:
        raise RetaskError(f"unterminated quote in check: {line}")
    if started:
        tokens.append("".join(buf))
    return tokens


def quote(token: str) -> str:
    """Inverse of `tokenize` for one token."""
    token = str(token)
    if token and not re.search(r'[\s"\\]', token):
        return token
    return '"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_check(line: str) -> dict:
    tokens = tokenize(line)
    if not tokens:
        raise RetaskError("empty check")
    kind = tokens[0]
    if kind not in CHECK_KINDS:
        raise RetaskError(
            f"unknown check {kind!r}; use one of {', '.join(CHECK_KINDS)}")
    args = tokens[1:]
    if len(args) != _CHECK_ARITY[kind]:
        raise RetaskError(
            f"check {kind} takes {_CHECK_ARITY[kind]} argument(s), got {len(args)}: {line}")
    if kind == "min_length":
        try:
            if int(args[1]) < 0:
                raise ValueError
        except ValueError:
            raise RetaskError(f"min_length needs a non-negative number: {line}") from None
    if kind == "matches":
        try:
            re.compile(args[1])
        except re.error as exc:
            raise RetaskError(f"invalid pattern in check: {exc}") from None
    return {"kind": kind, "args": args}


def format_check(check: dict) -> str:
    return " ".join([check["kind"], *(quote(a) for a in check["args"])])


_MEDIA_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_VIDEO_FILE_RE = re.compile(r"\.(mp4|webm|mov|m4v|ogv)(?:[?#]|$)", re.IGNORECASE)


def strip_media(text: str) -> str:
    """Images and videos are a Helpwo affordance; a terminal shows a placeholder.

    A list made in Helpwo may carry `![alt](path-or-url)` references. The file
    keeps them (Helpwo still renders them); what this side shows and hands its
    own model is `[image: alt]` / `[video: alt]`.
    """
    def _placeholder(match):
        kind = "video" if _VIDEO_FILE_RE.search(match.group(2)) else "image"
        return f"[{kind}: {match.group(1).strip() or 'untitled'}]"
    return _MEDIA_RE.sub(_placeholder, str(text or ""))


def describe_check(line: str) -> str:
    """The check as a person reads it."""
    try:
        check = parse_check(line)
    except RetaskError:
        return line
    kind, args = check["kind"], check["args"]
    if kind == "file_exists":
        return f"{args[0]} exists"
    if kind == "contains":
        return f"{args[0]} contains {args[1]!r}"
    if kind == "matches":
        return f"{args[0]} matches /{args[1]}/"
    if kind == "min_length":
        return f"{args[0]} has at least {args[1]} characters"
    return f"reviewed: {args[0]}"


# ── parse / serialize ────────────────────────────────────────────────

def parse(text: str) -> Retask:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != VERSION_LINE:
        raise RetaskError(f"not a retask file: the first line must be '{VERSION_LINE}'")
    idx += 1
    title, goal = "", ""
    while idx < len(lines) and not lines[idx].startswith("## "):
        line = lines[idx]
        if line.startswith("title:"):
            title = line[len("title:"):].strip()
        elif line.startswith("goal:"):
            goal = line[len("goal:"):].strip()
        idx += 1

    doc = Retask(title=title, goal=goal)
    while idx < len(lines):
        match = _HEADER_RE.match(lines[idx])
        if not match:
            raise RetaskError(f"line {idx + 1}: expected a task header '## [ ] id title'")
        mark, task_id, task_title = match.group(1), match.group(2), (match.group(3) or "").strip()
        if mark not in MARKS:
            raise RetaskError(f"line {idx + 1}: unknown mark [{mark}]")
        task = Task(id=task_id, title=task_title, status=MARKS[mark])
        idx += 1
        while idx < len(lines):
            meta = _META_RE.match(lines[idx])
            if not meta:
                break
            key, value = meta.group(1), meta.group(2).strip()
            if key == "after":
                task.after.extend(v for v in re.split(r"[\s,]+", value) if v)
            elif key == "check":
                task.checks.append(value)
            elif value:
                task.notes.append(value)
            idx += 1
        body = []
        while idx < len(lines) and not lines[idx].startswith("## "):
            body.append(lines[idx])
            idx += 1
        task.description = "\n".join(body).strip("\n")
        doc.tasks.append(task)
    validate(doc)
    return doc


def serialize(doc: Retask) -> str:
    out = [VERSION_LINE, f"title: {doc.title}"]
    if doc.goal:
        out.append(f"goal: {doc.goal}")
    for task in doc.tasks:
        out.append("")
        out.append(f"## [{STATUS_MARK[task.status]}] {task.id} {task.title}".rstrip())
        if task.after:
            out.append(f"after: {', '.join(task.after)}")
        out.extend(f"check: {c}" for c in task.checks)
        out.extend(f"note: {n}" for n in task.notes)
        if task.description.strip():
            out.append("")
            out.append(task.description.strip("\n"))
    return "\n".join(out) + "\n"


def validate(doc: Retask) -> None:
    if not doc.title.strip():
        raise RetaskError("a retask needs a title")
    if not doc.tasks:
        raise RetaskError("a retask needs at least one task")
    seen = set()
    for task in doc.tasks:
        if not _ID_RE.match(task.id):
            raise RetaskError(f"task id {task.id!r} must be 1-32 letters, digits, _ or -")
        if task.id in seen:
            raise RetaskError(f"duplicate task id {task.id}")
        seen.add(task.id)
        if not task.title.strip():
            raise RetaskError(f"task {task.id} needs a title")
        if task.status not in STATUS_MARK:
            raise RetaskError(f"task {task.id} has unknown status {task.status!r}")
        for line in task.checks:
            parse_check(line)
        for note in task.notes:
            if "\n" in note:
                raise RetaskError(f"task {task.id}: a note must be one line")
    for task in doc.tasks:
        for dep in task.after:
            if dep not in seen:
                raise RetaskError(f"task {task.id} is after unknown task {dep}")
            if dep == task.id:
                raise RetaskError(f"task {task.id} cannot be after itself")
    _reject_cycles(doc)


def _reject_cycles(doc: Retask) -> None:
    after = {t.id: list(t.after) for t in doc.tasks}
    state: dict = {}

    def visit(node: str, trail: list) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            raise RetaskError("tasks wait on each other: " + " -> ".join(trail + [node]))
        state[node] = 1
        for dep in after.get(node, []):
            visit(dep, trail + [node])
        state[node] = 2

    for task in doc.tasks:
        visit(task.id, [])


def load(path: str) -> Retask:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RetaskError(f"cannot read {path}: {exc}") from None
    return parse(text)


def save(path: str, doc: Retask) -> None:
    validate(doc)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(serialize(doc), encoding="utf-8")
    os.replace(tmp, target)
    _find_cache.clear()


# ── progress ─────────────────────────────────────────────────────────

def is_ready(doc: Retask, task: Task) -> bool:
    """Every task this one waits on is closed."""
    for dep in task.after:
        other = doc.task(dep)
        if other is None or other.status not in CLOSED:
            return False
    return True


def current_task(doc: Retask) -> Optional[Task]:
    """The one task that needs attention now.

    A submission waiting for review comes first (the person is waiting on the
    AI), then a rejection (the person has to redo it), then the task in hand,
    then the first task that can be started.
    """
    for status in (SUBMITTED, REJECTED, DOING):
        found = next((t for t in doc.tasks if t.status == status), None)
        if found:
            return found
    return next((t for t in doc.tasks if t.status == TODO and is_ready(doc, t)), None)


def claim_block(doc: Retask, task: Task) -> str:
    """Why the PERSON may not claim this task yet ('' when they may).

    Tasks are done in order: a later task cannot be claimed while an earlier
    one is still open — including one that is only `submitted`, because until
    the AI has checked it, it may come back. The AI's own transitions are not
    bound by this; only the "I've done this" claim is. Mirrors claimBlock in
    Helpwo's retask-core.ts, messages included.
    """
    waiting = [d for d in task.after
               if (doc.task(d) is None or doc.task(d).status not in CLOSED)]
    if waiting:
        return f"waits on {', '.join(waiting)}"
    for other in doc.tasks:
        if other is task:
            break
        if other.status not in CLOSED:
            return f"finish {other.id} first"
    return ""


def progress(doc: Retask) -> tuple:
    closed = sum(1 for t in doc.tasks if t.status in CLOSED)
    return closed, len(doc.tasks)


def is_finished(doc: Retask) -> bool:
    return all(t.status in CLOSED for t in doc.tasks)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _advance(doc: Retask) -> None:
    """Keep exactly one task in hand while there is work nobody has started."""
    if any(t.status in (DOING, SUBMITTED, REJECTED) for t in doc.tasks):
        return
    nxt = next((t for t in doc.tasks if t.status == TODO and is_ready(doc, t)), None)
    if nxt is not None:
        nxt.status = DOING


# ── checks ───────────────────────────────────────────────────────────

def _resolve_inside(base_dir: str, root: str, rel: str) -> str:
    base = Path(base_dir).resolve()
    target = (base / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
    root_path = Path(root).resolve()
    if target != root_path and root_path not in target.parents:
        raise RetaskError(f"{rel} is outside the workspace")
    return str(target)


def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        return fh.read(_CHECK_READ_BYTES).decode("utf-8", errors="replace")


def run_check(line: str, base_dir: str, root: str) -> Optional[str]:
    """Return None when the check holds, else the gap as one sentence.

    `review` returns None: it is the judgement a file cannot make, and it is
    gated in `set_status` by requiring a note instead.
    """
    try:
        check = parse_check(line)
    except RetaskError as exc:
        return str(exc)
    kind, args = check["kind"], check["args"]
    if kind == "review":
        return None
    try:
        path = _resolve_inside(base_dir, root, args[0])
    except RetaskError as exc:
        return str(exc)
    if not os.path.exists(path):
        return f"{args[0]} does not exist"
    if kind == "file_exists":
        return None
    if os.path.isdir(path):
        return f"{args[0]} is a directory, not a file"
    try:
        text = _read_text(path)
    except OSError as exc:
        return f"cannot read {args[0]}: {exc}"
    if kind == "contains":
        return None if args[1] in text else f"{args[0]} does not contain {args[1]!r}"
    if kind == "matches":
        return None if re.search(args[1], text, re.MULTILINE) else \
            f"{args[0]} does not match /{args[1]}/"
    length = len(text.strip())
    need = int(args[1])
    return None if length >= need else \
        f"{args[0]} has {length} characters, needs at least {need}"


def verify(task: Task, base_dir: str, root: str) -> list:
    """Every gap in the task's deterministic checks."""
    return [gap for gap in (run_check(c, base_dir, root) for c in task.checks) if gap]


# ── changes ──────────────────────────────────────────────────────────

def set_status(doc: Retask, task_id: str, status: str, *, note: str = "",
               base_dir: str = ".", root: Optional[str] = None) -> dict:
    """Move one task; returns {ok, status, gaps, error}.

    The document is changed in place on success AND on a failed completion (the
    task becomes `rejected` with the gaps noted — that outcome is the point).
    """
    task = doc.task(task_id)
    if task is None:
        ids = ", ".join(t.id for t in doc.tasks)
        return {"ok": False, "error": f"no task {task_id}; tasks are {ids}"}
    if status not in STATUS_MARK:
        return {"ok": False, "error": f"unknown status {status!r}; use one of {', '.join(STATUSES)}"}
    note = _one_line(note)
    root = root or base_dir

    if status in (DOING, SUBMITTED, DONE) and not is_ready(doc, task):
        waiting = [d for d in task.after if (doc.task(d) or Task(d, d)).status not in CLOSED]
        return {"ok": False, "error": f"{task.id} waits on {', '.join(waiting)}"}

    if status == DONE:
        gaps = verify(task, base_dir, root)
        if gaps:
            task.status = REJECTED
            task.notes.append(f"{_stamp()} not done: {'; '.join(gaps)}")
            return {"ok": False, "status": REJECTED, "gaps": gaps,
                    "error": "checks failed: " + "; ".join(gaps)}
        reviews = [c for c in task.checks if c.split(" ", 1)[0] == "review"]
        if reviews and not note:
            return {"ok": False, "error": (
                f"{task.id} has a review check ({'; '.join(describe_check(c) for c in reviews)}); "
                "pass a note naming the evidence you looked at")}

    if status == DOING:
        for other in doc.tasks:
            if other is not task and other.status == DOING:
                other.status = TODO
    task.status = status
    if note:
        label = {DONE: "passed", REJECTED: "not done", SKIPPED: "skipped",
                 SUBMITTED: "submitted"}.get(status, "")
        task.notes.append(f"{_stamp()} {label + ': ' if label else ''}{note}")
    elif status == DONE:
        task.notes.append(f"{_stamp()} passed")
    if status in CLOSED:
        _advance(doc)
    return {"ok": True, "status": task.status, "gaps": []}


def task_from_input(raw, default_id: str) -> Task:
    """One task from tool input, unvalidated (it may point at its siblings)."""
    if not isinstance(raw, dict):
        raise RetaskError("each task must be an object")
    return Task(
        id=str(raw.get("id") or default_id).strip(),
        title=_one_line(raw.get("title")),
        after=[str(a).strip() for a in (raw.get("after") or []) if str(a).strip()],
        checks=[_one_line(c) for c in (raw.get("checks") or []) if _one_line(c)],
        description=str(raw.get("description") or "").strip("\n"),
    )


def new_retask(title: str, goal: str, tasks: list) -> Retask:
    """Build a list from tool input; ids default to t1, t2, …"""
    doc = Retask(title=_one_line(title), goal=_one_line(goal))
    for index, raw in enumerate(tasks or [], start=1):
        doc.tasks.append(task_from_input(raw, f"t{index}"))
    validate(doc)
    _advance(doc)
    return doc


def file_name_for(title: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|\s]+', "-", _one_line(title)).strip("-.") or "tasks"
    return stem[:60] + EXTENSION


# ── finding lists ────────────────────────────────────────────────────

#: find_files runs on every agent turn (context block + tool routing); a short
#: cache keeps that from being a directory walk per turn. `save` clears it, so
#: a list created or finished here is seen on the very next turn.
_FIND_TTL_SECONDS = 3.0
_find_cache: dict = {}


def find_files(root: str) -> list:
    """Every .retask under root (shallow, skipping build and hidden dirs), newest first."""
    root = os.path.abspath(root or ".")
    cached = _find_cache.get(root)
    if cached and time.monotonic() - cached[0] < _FIND_TTL_SECONDS:
        return list(cached[1])
    found = _walk(root)
    _find_cache[root] = (time.monotonic(), found)
    return list(found)


def _walk(root: str) -> list:
    found = []
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in _SKIP_DIRS
                       and depth < _FIND_DEPTH]
        for name in filenames:
            if name.endswith(EXTENSION):
                found.append(os.path.join(dirpath, name))
    found.sort(key=lambda p: (-_mtime(p), p))
    return found


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def find_active(root: str) -> Optional[str]:
    """The newest list that still has open work."""
    for path in find_files(root):
        try:
            if not is_finished(load(path)):
                return path
        except RetaskError:
            continue
    return None


# ── what the model sees every turn ───────────────────────────────────

_MARK_GLYPH = {TODO: "○", DOING: "▶", SUBMITTED: "?", DONE: "✓", REJECTED: "✗", SKIPPED: "–"}


def anchor_text(path: str, doc: Retask, root: str = "") -> str:
    """The per-turn context block: every title, and the current task in full.

    Only the current task carries its description and checks: that is what a
    question in the middle of the work is about, and the rest would cost every
    turn for nothing.
    """
    done, total = progress(doc)
    shown = os.path.relpath(path, root) if root else path
    current = current_task(doc)
    lines = [f'<retask file="{shown}" progress="{done}/{total}">',
             f"{doc.title}" + (f" — {doc.goal}" if doc.goal else "")]
    for task in doc.tasks:
        tag = "  <- current" if current is task else ""
        lines.append(f"{_MARK_GLYPH[task.status]} {task.id} [{task.status}] {task.title}{tag}")
        if current is task:
            if task.after:
                lines.append(f"    after: {', '.join(task.after)}")
            description = strip_media(task.description).strip()
            if len(description) > _ANCHOR_DESCRIPTION_CHARS:
                description = description[:_ANCHOR_DESCRIPTION_CHARS] + " …(retask.read for the rest)"
            for text in description.splitlines():
                lines.append(f"    {text}")
            for check in task.checks:
                lines.append(f"    check: {check}")
            if task.notes:
                lines.append(f"    last note: {task.notes[-1]}")
    lines.append("The person does these tasks; you guide and check. "
                 "When they say one is done, read the evidence and call "
                 "retask.update status=done (the checks run then).")
    lines.append("</retask>")
    return "\n".join(lines)


def context_block(root: str) -> str:
    """Anchor for the active list under root, or '' when there is none."""
    path = find_active(root)
    if not path:
        return ""
    try:
        return anchor_text(path, load(path), root)
    except RetaskError:
        return ""


def changed_tasks(before: Optional[Retask], after: Retask) -> list:
    """Tasks whose status or last note moved, in document order."""
    old = {t.id: (t.status, t.notes[-1] if t.notes else "") for t in (before.tasks if before else [])}
    return [t for t in after.tasks
            if old.get(t.id) != (t.status, t.notes[-1] if t.notes else "")]
