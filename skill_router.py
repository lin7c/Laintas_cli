"""Dynamic skill routing (#3) — surface the skills that fit the current task.

The ranking ALGORITHM is unified in the gateway (`/api/rank`), shared by every
client and by memory recall, so it upgrades in one place (e.g. swapping to a
reranker later). This module only:
  1. gathers the local skill catalog,
  2. asks the gateway to rank it against the task (with a per-run result cache so
     a rebuilt-every-loop prompt makes at most one rank call per run), and
  3. prepends a "★ most relevant" line to the catalog — purely additive, the
     full catalog is preserved so a mis-rank can never hide a skill.

Offline / old gateway: falls back to a local token-overlap ranking. Never raises.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from typing import Optional

import skills
import embeddings


# Cache of ranking results, keyed by (task, catalog fingerprint, k). The task is
# constant within a run and skills rarely change, so this collapses the
# every-loop prompt rebuild into a single /api/rank call per run.
#
# It holds MANY entries, not one, because agents run concurrently: with a
# single slot, N sub-agents working on N different tasks evicted each other on
# every loop and the hit rate collapsed to zero — turning "one rank call per
# run" into one per agent per iteration, each of them a network round trip on
# the critical path of building the prompt. Bounded so a long session cannot
# grow it without limit, and locked because the evicting write is not atomic.
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


def _skill_text(meta) -> str:
    name = getattr(meta, "name", "") or ""
    desc = getattr(meta, "description", "") or ""
    triggers = getattr(meta, "trigger_patterns", None) or []
    trig = ", ".join(str(t) for t in triggers)
    return f"{name}: {desc}" + (f". triggers: {trig}" if trig else "")


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _local_lexical(task: str, items: list, k: int) -> list:
    """Offline fallback: token overlap. items: [(name, text)]."""
    q = _tokens(task)
    if not q:
        return []
    scored = []
    for name, text in items:
        overlap = len(q & _tokens(text))
        if overlap > 0:
            scored.append((name, float(overlap), "lexical"))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[: max(1, int(k))]


def _catalog_fingerprint(items: list) -> str:
    h = hashlib.sha1()
    for name, text in sorted(items):
        h.update(name.encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(text.encode("utf-8", "replace"))
        h.update(b"\x01")
    return h.hexdigest()


def rank(task: str, *, k: int = 3, session: Optional[dict] = None) -> list:
    """Return up to ``k`` skills most relevant to ``task`` as
    ``[(name, score, method)]``, high→low. Uses the shared gateway ranker with a
    local token-overlap fallback. Cached per (task, catalog, k) within a run."""
    q = str(task or "").strip()
    if not q:
        return []
    try:
        metas = skills.get_all_metadata()
    except Exception:
        return []
    if not metas:
        return []

    items = [(name, _skill_text(meta)) for name, meta in metas.items()]
    key = (q, _catalog_fingerprint(items), int(k))
    _cached = _rank_cache_get(key)
    if _cached is not None:
        return _cached

    # Shared gateway ranking (semantic, with server-side lexical fallback).
    ranked = embeddings.rank(q, items, top_k=k, session=session)
    if ranked is not None:
        # ranked: [(name, score)] restricted to known skill names.
        known = {n for n, _ in items}
        result = [(n, round(float(s), 4), "gateway")
                  for n, s in ranked if n in known][: max(1, int(k))]
    else:
        # Endpoint unreachable → offline local fallback.
        result = _local_lexical(q, items, k)

    _rank_cache_put(key, result)
    return result


def annotate_catalog(task: str, base_catalog: str, *,
                     k: int = 3, session: Optional[dict] = None) -> str:
    """Prepend a task-relevance highlight to the skill catalog. Additive — the
    full ``base_catalog`` is preserved. Returns the base unchanged when there's
    no task, no skills, or nothing ranks. Never raises."""
    base = base_catalog or ""
    try:
        if not str(task or "").strip():
            return base
        ranked = rank(task, k=k, session=session)
        names = [n for n, s, _ in ranked if s > 0]
        if not names:
            return base
        line = ("★ Most relevant skills for this task (consider `skill.load` "
                "these first): " + ", ".join(names))
        return f"{line}\n{base}" if base else line
    except Exception:
        return base
