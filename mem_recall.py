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
import threading
from collections import OrderedDict
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

# Ranking-result cache. The task is constant within a run and the memory set
# rarely changes, so this collapses the every-loop prompt rebuild into at most
# one /api/rank call per run (mirrors skill_router's cache) — including its
# reason for holding many entries instead of one: concurrent agents work on
# different queries, and a single slot means they evict each other every loop
# and never hit, putting a network round trip back on every prompt rebuild.
_RANK_CACHE_MAX = 32
_rank_cache: "OrderedDict[tuple, list]" = OrderedDict()
_rank_cache_lock = threading.Lock()


def _rank_cache_get(key: tuple):
    with _rank_cache_lock:
        if key not in _rank_cache:
            return None
        _rank_cache.move_to_end(key)
        return _rank_cache[key]


def _rank_cache_put(key: tuple, result: list) -> None:
    with _rank_cache_lock:
        _rank_cache[key] = result
        _rank_cache.move_to_end(key)
        while len(_rank_cache) > _RANK_CACHE_MAX:
            _rank_cache.popitem(last=False)


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
           session: Optional[dict] = None, local_only: bool = False) -> list:
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
    _cached = _rank_cache_get(cache_key)
    if _cached is not None:
        return _cached

    candidates = [(name, _text_of(entry, body))
                  for name, (entry, body) in by_name.items()]
    # Prompt construction is latency-sensitive.  Dynamic context callers use
    # local_only so a remote reranker can never delay the model's first token.
    ranked = None if local_only else embeddings.rank(
        q, candidates, top_k=k, session=session)

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

    _rank_cache_put(cache_key, result)
    return result


def relevant_block(query: str, *, k: int = 5, session: Optional[dict] = None,
                   local_only: bool = False) -> str:
    """Formatted task-relevant summary section for the prompt, or ``""``.

    Full entries remain available through ``mem.list``/``mem.read``; callers do
    not need to bulk-inject the store.
    """
    try:
        hits = recall(query, k=k, session=session, local_only=local_only)
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
