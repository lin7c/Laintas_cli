"""Every step's full text, kept so `think(question=...)` can answer from it.

The thread the model reads loses text on purpose: old tool output is
truncated, the head is folded into a summary, another model's thinking is
quoted rather than replayed, and some upstream chat templates drop earlier
thinking before the model ever sees it. When the model notices that the
context no longer explains itself, guessing the missing part is the failure
to avoid; this record is what it can ask instead.

One JSONL file per session and agent under ``~/.laintas/sessions/records``,
private (0600), appended once per committed step. It never leaves the
machine except as a `think` result returned to the same agent. Search is
lexical (no model call), so an answer is always a quotation of what was
actually written or returned, never a reconstruction.
"""

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import paths

#: One field (a reasoning block, one tool output) is kept up to this size.
FIELD_CAP = 256 * 1024
#: Records untouched for this long are removed on the first write of a process.
MAX_AGE_SECONDS = 30 * 86400
#: At most this many places are quoted per question.
MAX_HITS = 4

_lock = threading.Lock()
_gc_done = False

_STOPWORDS = frozenset("""
a an and are as at be been but by can did do does for from had has have how i
if in into is it its me my no not of on or our so that the their then there
these this to was we were what when where which who why will with you your
step steps
""".split())
#: Question words carry no topic ("why", "how", "this", particles). Removed
#: before a CJK run is cut into bigrams, so a question asks about its topic
#: rather than about its question word. Escaped: product source is English.
_CJK_FILLER = re.compile(
    "\u4e3a\u4ec0\u4e48|\u4e3a\u4f55|\u4ec0\u4e48|\u600e\u4e48|\u600e\u6837|\u5982\u4f55|\u54ea\u91cc|\u54ea\u4e2a|\u662f\u5426|\u662f\u4e0d\u662f|\u6211\u4eec|\u4f60\u4eec|\u4ed6\u4eec|\u4e4b\u524d|\u5f53\u65f6|\u521a\u624d|\u8fd9\u4e2a|\u90a3\u4e2a|\u8fd9\u91cc|\u90a3\u91cc|\u4e00\u4e0b|\u56e0\u4e3a|\u6240\u4ee5|"
    "[\u7684\u4e86\u5417\u5462\u5427\u662f\u5728\u548c\u628a\u88ab\u6211\u4f60\u4ed6\u5b83]")
_ASCII_TERM = re.compile(r"[a-z0-9_]+(?:[./-][a-z0-9_]+)*")
_CJK_RUN = re.compile("[\u3400-\u9fff]+")


def _safe(value) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(value or "").strip())[:64]


def record_dir() -> Path:
    return paths.SESSIONS_DIR / "records"


def record_path(session_id: str, agent_id: str = "") -> Optional[Path]:
    sid = _safe(session_id)
    if not sid:
        return None
    return record_dir() / f"{sid}__{_safe(agent_id or 'primary')}.jsonl"


def _cap(text) -> str:
    text = "" if text is None else str(text)
    if len(text) <= FIELD_CAP:
        return text
    return text[:FIELD_CAP] + f"\n[record truncated: {len(text) - FIELD_CAP} more characters]"


def _gc(directory: Path, now: float) -> None:
    try:
        for path in directory.glob("*.jsonl"):
            try:
                if not path.is_symlink() and now - path.stat().st_mtime > MAX_AGE_SECONDS:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def append_step(session_id: str, agent_id: str, step: int, *,
                reasoning: str = "", reply: str = "", calls=()) -> bool:
    """Append one committed step. Best-effort: never raises, True if written."""
    global _gc_done
    try:
        path = record_path(session_id, agent_id)
        if path is None:
            return False
        entry = {"step": int(step or 0), "ts": round(time.time(), 3)}
        if str(reasoning or "").strip():
            entry["reasoning"] = _cap(reasoning)
        if str(reply or "").strip():
            entry["reply"] = _cap(reply)
        rows = []
        for call in calls or ():
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {},
                                       ensure_ascii=False, default=str)
            rows.append({"name": str(call.get("name") or ""),
                         "arguments": _cap(arguments),
                         "output": _cap(call.get("output"))})
        if rows:
            entry["calls"] = rows
        if len(entry) == 2:
            return False
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with _lock:
            directory = path.parent
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
            if not _gc_done:
                _gc_done = True
                _gc(directory, time.time())
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception:
        return False


def _terms(text: str) -> set:
    lowered = str(text or "").lower()
    terms = set()
    for token in _ASCII_TERM.findall(lowered):
        if len(token) >= 2 and token not in _STOPWORDS:
            terms.add(token)
        # `agent_loop.py` should also find `agent_loop`
        for part in re.split(r"[./-]", token):
            if len(part) >= 3 and part not in _STOPWORDS:
                terms.add(part)
    for run in _CJK_RUN.findall(_CJK_FILLER.sub(" ", lowered)):
        if len(run) == 1:
            continue
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def _documents(entry: dict):
    """(label, text) pairs of one step, each quoted on its own."""
    step = entry.get("step")
    if entry.get("reasoning"):
        yield step, "reasoning", entry["reasoning"]
    if entry.get("reply"):
        yield step, "reply", entry["reply"]
    for call in entry.get("calls") or []:
        name = call.get("name") or "tool"
        if call.get("arguments"):
            yield step, f"{name} call", call["arguments"]
        if call.get("output"):
            yield step, f"{name} output", call["output"]


def _excerpt(text: str, terms: set, size: int) -> str:
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms]
    positions = [p for p in positions if p >= 0]
    start = max(0, (min(positions) if positions else 0) - size // 4)
    end = min(len(text), start + size)
    start = max(0, end - size)
    piece = text[start:end]
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


def search(session_id: str, agent_id: str, question: str, budget_chars: int) -> list:
    """Best places in this session's record for `question`, as dicts
    {step, label, excerpt}. Empty when nothing matches well enough."""
    path = record_path(session_id, agent_id)
    terms = _terms(question)
    if path is None or not path.is_file() or not terms:
        return []
    # A good part of the question must appear, not just one of its words: a
    # single shared word ("file", "error") matches nearly every step.
    need = max(1, math.ceil(len(terms) * 0.4))
    scored = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for order, line in enumerate(fh):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                for step, label, text in _documents(entry):
                    lowered = str(text).lower()
                    hit = {term for term in terms if term in lowered}
                    if len(hit) < need:
                        continue
                    scored.append((len(hit), order, step, label, str(text), hit))
    except OSError:
        return []
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    chosen = scored[:MAX_HITS]
    if not chosen:
        return []
    size = max(400, int(budget_chars) // len(chosen) - 80)
    return [{"step": step, "label": label, "excerpt": _excerpt(text, hit, size)}
            for _score, _order, step, label, text, hit in chosen]
