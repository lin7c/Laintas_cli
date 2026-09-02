"""Assertion staleness propagation: when the source moves, the claim is flagged.

A memory that cites a file is an assertion ABOUT that file. Files change under
it, and until now nothing noticed: the claim stayed in the prompt at full
confidence long after the code it described was rewritten. That is the failure
mode a structured memory store makes worse rather than better, because a
confidently wrong memory outranks a fresh read.

What this module does is deliberately narrow:

  * fingerprint the cited files by CONTENT, not by mtime. ``file_pager`` uses
    (size, mtime_ns) because it only has to be right within one session; a
    memory outlives checkouts, and `git checkout` rewrites mtime on files whose
    bytes never changed. A content hash is the only fingerprint that does not
    manufacture false staleness across sessions.
  * on a file-mutating tool, flag the memories that cite that path — and only
    those. The scan is over evidence-bearing memories, which is a small subset
    of a small store.
  * if the bytes come BACK to what was recorded (edit, then revert), clear the
    flag with no model call at all. Deterministic recovery first; the LLM
    review in ``mem_review`` only ever sees what arithmetic could not settle.

Flagging is not deleting and not editing. `stale` means "unverified", not
"wrong" — see ``memory_system`` for the lifecycle.
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Iterable, Optional

import memory_system

#: Truncated sha256. 12 hex is 48 bits — far past collision risk for the number
#: of files one project cites, and short enough to keep the frontmatter line
#: readable, which matters because users open these files.
SHA_LEN = 12

#: Files above this are fingerprinted from a bounded sample rather than whole.
#: A memory citing a 200MB artefact is not a case worth paying full hashing for
#: on every write, and the sample still changes when the file does.
MAX_FULL_HASH_BYTES = 8 * 1024 * 1024


def content_hash(path: str) -> str:
    """Content fingerprint of a file, or "" when it cannot be read.

    A missing file returns "" rather than raising: deletion is a legitimate
    thing to notice, and the caller reports it as a reason for staleness.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            if size <= MAX_FULL_HASH_BYTES:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            else:
                # Head, tail and the length: enough to move on any real edit
                # without reading gigabytes on every write.
                digest.update(fh.read(1 << 20))
                fh.seek(-(1 << 20), os.SEEK_END)
                digest.update(fh.read(1 << 20))
                digest.update(str(size).encode("ascii"))
    except OSError:
        return ""
    return digest.hexdigest()[:SHA_LEN]


def evidence_for(path: str, sha: str = None,
                 start: int = None, end: int = None) -> dict:
    """Build one evidence item for ``path`` at its current content."""
    item = {"path": os.path.abspath(path), "sha": sha or content_hash(path)}
    if start and end:
        item["start"] = int(start)
        item["end"] = int(end)
    return item


def _cited_paths(entry: dict) -> set:
    return {str(item.get("path") or "") for item in (entry.get("evidence") or [])}


# ── Cited-path cache ─────────────────────────────────────────────────────
# propagate() runs after EVERY successful file write, and the honest answer is
# almost always "no memory cites this file". Reading and parsing the whole
# memory store to discover that would put a directory scan on the write path.
# The cheap discriminator is the set of cited paths, refreshed only when the
# memory directory's own mtime moves.
_cited_cache: Optional[frozenset] = None
_cited_cache_stamp: Optional[tuple] = None
_cache_lock = threading.Lock()


def _store_stamp() -> tuple:
    try:
        st = os.stat(memory_system.MEMORY_DIR)
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (0, 0)


def invalidate_cache() -> None:
    """Drop the cited-path cache (memories were written outside this process)."""
    global _cited_cache, _cited_cache_stamp
    with _cache_lock:
        _cited_cache = None
        _cited_cache_stamp = None


def cited_paths() -> frozenset:
    """Every absolute path cited by any visible memory."""
    global _cited_cache, _cited_cache_stamp
    stamp = _store_stamp()
    with _cache_lock:
        if _cited_cache is not None and _cited_cache_stamp == stamp:
            return _cited_cache
    found = set()
    for entry in memory_system.list_memories():
        found |= {os.path.abspath(p) for p in _cited_paths(entry) if p}
    frozen = frozenset(found)
    with _cache_lock:
        _cited_cache = frozen
        _cited_cache_stamp = stamp
    return frozen


def citing_memories(paths: Iterable[str]) -> list[dict]:
    """Visible memories whose evidence names any of ``paths`` (absolute)."""
    wanted = {os.path.abspath(p) for p in paths if p}
    if not wanted:
        return []
    out = []
    for entry in memory_system.list_memories():
        if not entry.get("evidence"):
            continue
        if _cited_paths(entry) & wanted:
            out.append(entry)
    return out


def _drift(entry: dict, only: Optional[set] = None) -> list[str]:
    """Cited files whose bytes no longer match what the entry recorded."""
    reasons = []
    for item in entry.get("evidence") or []:
        path = str(item.get("path") or "")
        if not path or (only is not None and os.path.abspath(path) not in only):
            continue
        current = content_hash(path)
        if current == item.get("sha"):
            continue
        reasons.append(f"{os.path.basename(path)} "
                       + ("was deleted or is unreadable" if not current
                          else f"changed ({item.get('sha')} → {current})"))
    return reasons


def propagate(paths: Iterable[str]) -> list[str]:
    """Flag every memory whose cited source at ``paths`` actually moved.

    Returns the names flagged. A write that leaves the bytes identical (a
    formatter no-op, a rewrite of the same content) flags nothing — the check
    is on content, so this is free of the false positives an mtime check would
    produce on every save.
    """
    wanted = {os.path.abspath(p) for p in paths if p}
    if not wanted or not (wanted & cited_paths()):
        return []
    flagged = []
    for entry in citing_memories(wanted):
        if entry.get("status") == memory_system.STATUS_STALE:
            continue
        reasons = _drift(entry, only=wanted)
        if not reasons:
            continue
        ok, _ = memory_system.mark_stale(entry["name"], "; ".join(reasons))
        if ok:
            flagged.append(entry["name"])
    return flagged


def reconcile() -> list[str]:
    """Clear staleness on entries whose evidence came back to what it was.

    The edit-then-revert case, and the case where two memories cite one file
    but only one of them was about the part that changed and the other's range
    was restored. Deterministic: no model, no cost, no judgement.
    """
    healed = []
    for entry in memory_system.list_stale():
        if not entry.get("evidence"):
            continue
        if _drift(entry):
            continue
        ok, _ = memory_system.clear_stale(entry["name"])
        if ok:
            healed.append(entry["name"])
    return healed


def attest(path: str, start: int = None, end: int = None) -> str:
    """Encoded evidence string for a single file, for ``mem.save``."""
    return memory_system.format_evidence([evidence_for(path, start=start, end=end)])
