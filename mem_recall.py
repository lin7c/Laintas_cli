"""Semantic memory recall (read side of #1) — turn the memory store into a
network that surfaces the *relevant* facts for the current task, instead of
bulk-dumping everything every loop.

Ranking is delegated to the gateway's shared ``/api/rank`` service (the SAME
primitive skill routing uses), so the algorithm lives in one place and upgrades
once. This module only: gathers the visible memories (snapshotted per run),
sends them as candidates to be ranked against the task, and formats the top-k.
If ``/api/rank`` is unreachable it falls back to the lexical ``search_memories``,
so recall is a pure enhancement that never breaks, offline or misconfigured.

Only depends on ``memory_system`` + ``embeddings`` + ``paths`` (all light).
"""

from __future__ import annotations

import os
from typing import Optional

import paths
import memory_system
import embeddings


# Keep under the gateway's RANK_MAX_CANDIDATES (200). When a user has more
# memories than this, rank the most important ones (the rest stay reachable via
# the full bulk memory context, which is injected separately).
_MAX_CANDIDATES = 180


# ── Per-run entries snapshot ────────────────────────────────────────────────
# recall() runs every loop (the prompt is rebuilt each iteration), and reading +
# parsing every memory file each time is wasteful. Cache the parsed (entry, body)
# list, keyed by cwd (scope) + a cheap directory fingerprint (name/mtime/size of
# each *.md). Any add/remove/edit — including a background mem_extract write —
# changes the fingerprint and invalidates the cache, so recall stays correct.
_entries_cache: dict = {"key": None, "entries": None}

# Per-run ranking-result cache. The task is constant within a run and the memory
# set rarely changes, so this collapses the every-loop prompt rebuild into at
# most one /api/rank call per run (mirrors skill_router's cache).
_rank_cache: dict = {"key": None, "result": None}


def _dir_fingerprint() -> object:
    """Cheap signature of the memory dir: one scandir, no file reads. Returns a
    unique sentinel on error so the cache is bypassed (safe) rather than stale."""
    try:
        sig = []
        with os.scandir(paths.MEMORY_DIR) as it:
            for de in it:
                name = de.name
                if not name.endswith(".md") or name == "MEMORY.md":
                    continue
                st = de.stat()
                sig.append((name, st.st_mtime_ns, st.st_size))
        return frozenset(sig)
    except Exception:
        # No dir / race: return a fresh object so equality always fails (no cache).
        return object()


def _current_entries(mem_type, fingerprint) -> list:
    """Return [(entry, body)] for visible memories, cached per (cwd, fingerprint)."""
    key = (os.getcwd(), mem_type, fingerprint)
    if _entries_cache["key"] == key and _entries_cache["entries"] is not None:
        return _entries_cache["entries"]
    entries = []
    for entry in memory_system.list_memories(mem_type):
        data = memory_system.read_memory(entry["name"])
        if data:
            entries.append((entry, data["body"]))
    _entries_cache["key"] = key
    _entries_cache["entries"] = entries
    return entries


def _text_of(entry: dict, body: str) -> str:
    """Canonical text ranked for a memory: name + description + body."""
    name = entry.get("name", "")
    desc = entry.get("description", "")
    return f"{name}\n{desc}\n{body}".strip()


def recall(query: str, *, mem_type: str = None, k: int = 5,
           session: Optional[dict] = None) -> list:
    """Return up to ``k`` memories most relevant to ``query``, each as a dict
    with ``name``/``description``/``type``/``body_preview``/``score``/``method``.

    Ranked via the shared gateway ``/api/rank`` (semantic, with server-side
    lexical fallback); if that endpoint is unreachable, falls back to the local
    lexical ``search_memories``. An empty query yields an empty list (bulk
    injection is handled elsewhere — recall is for the query-aware path)."""
    q = str(query or "").strip()
    if not q:
        return []

    fingerprint = _dir_fingerprint()
    entries = _current_entries(mem_type, fingerprint)
    if not entries:
        return []

    # Cap candidates to the gateway's per-request limit; keep the most important.
    if len(entries) > _MAX_CANDIDATES:
        entries = sorted(
            entries,
            key=lambda eb: float(eb[0].get("importance", 0.5) or 0.5),
            reverse=True,
        )[:_MAX_CANDIDATES]

    by_name = {e.get("name", ""): (e, b) for e, b in entries}

    cache_key = (q, mem_type, int(k), fingerprint, len(by_name))
    if _rank_cache["key"] == cache_key and _rank_cache["result"] is not None:
        return _rank_cache["result"]

    candidates = [(name, _text_of(entry, body))
                  for name, (entry, body) in by_name.items()]
    ranked = embeddings.rank(q, candidates, top_k=k, session=session)

    if ranked is None:
        # Endpoint unreachable → local lexical fallback.
        result = _lexical_fallback(q, mem_type, k)
    else:
        out = []
        for name, score in ranked:
            eb = by_name.get(name)
            if not eb:
                continue
            entry, body = eb
            out.append({
                "name": entry.get("name", ""),
                "description": entry.get("description", ""),
                "type": entry.get("type", entry.get("mem_type", "")),
                "importance": entry.get("importance", 0.5),
                "body_preview": body[:500],
                "score": round(float(score), 4),
                "method": "gateway",
            })
        result = out[: max(1, int(k))]

    _rank_cache["key"] = cache_key
    _rank_cache["result"] = result
    return result


def relevant_block(query: str, *, k: int = 5, session: Optional[dict] = None) -> str:
    """Formatted '★ most relevant to this task' section for the prompt, or "" if
    there is nothing to add. Purely additive — the caller still injects the full
    memory context, so this can never drop an important memory; it only surfaces
    the ones (across the WHOLE store, past any per-type cap) that match the task."""
    try:
        hits = recall(query, k=k, session=session)
    except Exception:
        return ""
    if not hits:
        return ""
    # Summary-only, like the bulk context: name + category + one-line summary, no
    # body. The agent expands a specific entry via mem.read when it needs detail.
    lines = ["★ Most relevant memories for the current task (summaries; use mem.read for full text):"]
    for h in hits:
        name = h.get("name", "")
        typ = h.get("type", "")
        desc = (h.get("description", "") or "").strip()
        head = f"- [{name}]" + (f" ({typ})" if typ else "")
        lines.append(f"{head} {desc}".rstrip())
    return "\n".join(lines)


def _lexical_fallback(query: str, mem_type: str, k: int) -> list:
    try:
        results = memory_system.search_memories(query, mem_type=mem_type, limit=k)
    except Exception:
        return []
    for r in results:
        r["method"] = "lexical"
    return results
