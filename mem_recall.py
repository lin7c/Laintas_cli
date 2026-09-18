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
                "status": entry.get("status", memory_system.STATUS_ACTIVE),
                "body_preview": body[:500],
                "score": round(float(score), 4),
                "method": "gateway",
            })
        result = out[: max(1, int(k))]

    _rank_cache_put(cache_key, result)
    return result


#: Ceiling on how many entries a budgeted block may consider. A budget with no
#: ceiling would rank the whole store on every prompt rebuild for lines that
#: will not fit anyway.
_BUDGET_MAX_HITS = 24

# Names already counted as used, as (query, name). The prompt is rebuilt every
# loop from the same cached ranking, so without this one recall would count the
# same entry dozens of times in a single run and "how often has this been
# useful" would measure loop iterations instead.
_USE_RECORDED: set = set()
_USE_RECORDED_MAX = 4096


def _record_use(query: str, names) -> None:
    fresh = []
    for name in names:
        key = (str(query)[:120], str(name))
        if key in _USE_RECORDED:
            continue
        if len(_USE_RECORDED) >= _USE_RECORDED_MAX:
            _USE_RECORDED.clear()
        _USE_RECORDED.add(key)
        fresh.append(name)
    if not fresh:
        return
    try:
        memory_system.touch(fresh, kind="recall")
    except Exception:
        pass


def relevant_block(query: str, *, k: int = 5, session: Optional[dict] = None,
                   local_only: bool = False, budget_chars: int = 0) -> str:
    """Formatted task-relevant summary section for the prompt, or ``""``.

    With ``budget_chars``, ``k`` becomes a FLOOR and the block fills to the
    character budget instead. A fixed count per prompt was the reason a store
    of 150 entries showed the same handful forever: the window never widened,
    so anything ranked sixth was unreachable no matter how relevant it was —
    while a one-line summary costs about as much as one line of anything else
    already in the prompt.

    Full entries remain available through ``mem.list``/``mem.read``; callers do
    not need to bulk-inject the store.
    """
    want = max(1, int(k or 1))
    if budget_chars and budget_chars > 0:
        want = max(want, _BUDGET_MAX_HITS)
    try:
        hits = recall(query, k=want, session=session, local_only=local_only)
    except Exception:
        return ""
    if not hits:
        return ""
    # Summary-only, like the bulk context: name + category + one-line summary, no
    # body. The agent expands a specific entry via mem.read when it needs detail.
    lines = ["★ Most relevant memories for the current task (summaries; use mem.read for full text):"]
    used = len(lines[0])
    shown: list = []
    for index, h in enumerate(hits):
        name = h.get("name", "")
        typ = h.get("type", "")
        desc = (h.get("description", "") or "").strip()
        head = f"- [{name}]" + (f" ({typ})" if typ else "")
        # Recall is where a stale claim does the most damage: it arrives
        # labelled "most relevant to this task", which is exactly the framing
        # that stops the model from checking it. Carry the flag through.
        flag = (" [STALE — cited source changed; verify before relying on it]"
                if h.get("status") == memory_system.STATUS_STALE else "")
        line = f"{head} {desc}{flag}".rstrip()
        # The floor is honoured before the budget: a prompt that shows nothing
        # because the first summary was long is worse than one line over.
        if (budget_chars and index >= max(1, int(k or 1))
                and used + len(line) + 1 > budget_chars):
            break
        used += len(line) + 1
        lines.append(line)
        shown.append(name)
    # Counted here, not in recall(): an entry that was ranked and then cut by
    # the budget was never put in front of the model, and counting it would
    # make "used" mean "considered".
    _record_use(query, shown)
    return "\n".join(lines)


def _lexical_fallback(query: str, mem_type: str, k: int) -> list:
    """Local ranking: lexical overlap first, then what has earned its keep.

    ``search_memories`` scores ``hits * 10 + importance``, so whole-point gaps
    mean "matched one more term". The usage prior is deliberately smaller than
    that: it breaks ties between entries the query touches equally, and never
    outvotes an actual term match.
    """
    try:
        results = memory_system.search_memories(
            query, mem_type=mem_type, limit=max(k, _BUDGET_MAX_HITS))
    except Exception:
        return []
    for r in results:
        r["method"] = "lexical"
        try:
            r["score"] = round(float(r.get("score", 0.0))
                               + 2.0 * memory_system.eviction_score(r), 3)
        except Exception:
            pass
    results.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
    return results[:max(1, int(k or 1))]
