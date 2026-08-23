"""Embedding client — thin wrapper over the gateway's ``/api/embed`` endpoint.

Shared foundation for semantic memory recall (#1) and dynamic skill routing (#3).
The embedding model lives at the gateway (one place to manage, reused by every
client); this module only ships text there and gets vectors back.

Design contract: **never raise, never block hard.** Any failure — no session,
endpoint unavailable (503 when no provider configured), network error, malformed
response — returns ``None`` so callers degrade to lexical search. This keeps
semantic recall a pure enhancement that can't break the CLI.

Only depends on ``backend_profiles`` + ``paths`` (both light) — deliberately does
NOT import the ~5k-line ``laintas_cli`` entry module, to stay import-cycle-free.
"""

from __future__ import annotations

import json
import math
import os
from typing import Optional

import backend_profiles
import paths


_BACKEND_URL = os.environ.get("LAINTAS_BACKEND") or "https://laintas.com"
_TIMEOUT = float(os.environ.get("LAINTAS_EMBED_TIMEOUT", "30"))
# rank() gets a much shorter leash than embed(). An embedding is the answer the
# caller asked for and is worth waiting on; a ranking is an ENHANCEMENT with a
# local lexical fallback, and it sits on the critical path of every prompt
# rebuild — so a slow ranker must degrade, not stall the agent. Waiting the full
# 30s here once cost two lost minutes per turn per agent and the result was
# thrown away anyway when the timeout finally fired.
_RANK_TIMEOUT = float(os.environ.get("LAINTAS_RANK_TIMEOUT", "8"))
_MAX_BATCH = 64          # must stay ≤ the gateway's EMBED_MAX_INPUTS
_MAX_CHARS = 8000        # must stay ≤ the gateway's EMBED_MAX_CHARS

# Process-flag: once the endpoint answers 503/404 we stop hammering it for the
# rest of the session (it won't sprout a provider mid-run). Reset only on restart.
_endpoint_disabled = False

# Bounded in-process cache: sha1(text) → vector. Collapses the repeated embedding
# of the constant task query (the prompt is rebuilt every loop) into one network
# call per unique text. FIFO-ish eviction keeps memory bounded.
_embed_cache: dict = {}
_EMBED_CACHE_MAX = 512


def _cache_key(text: str) -> str:
    import hashlib
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()


def _cache_put(key: str, vec) -> None:
    if len(_embed_cache) >= _EMBED_CACHE_MAX:
        for old in list(_embed_cache.keys())[: max(1, _EMBED_CACHE_MAX // 8)]:
            _embed_cache.pop(old, None)
    _embed_cache[key] = vec


def _with_client_budget(headers: dict, timeout: float) -> dict:
    """Tell the gateway how long we will actually wait.

    Without it the gateway spends its own (longer) budget upstream, finishes
    after we have already given up, and meters the call anyway — we pay for an
    answer delivered into a closed socket. With it, the gateway degrades inside
    our window instead of billing past it.
    """
    out = dict(headers or {})
    out["X-Client-Timeout"] = str(int(max(1.0, float(timeout))))
    return out


def _load_session() -> Optional[dict]:
    try:
        f = paths.SESSION_FILE
        if not f.exists():
            return None
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def available() -> bool:
    """Cheap check: a session exists and we haven't already seen the endpoint
    refuse. Not a guarantee — the actual call still degrades gracefully."""
    return not _endpoint_disabled and _load_session() is not None


def embed(texts, *, session: Optional[dict] = None) -> Optional[list]:
    """Embed a list of strings. Returns a list of vectors (list[float]) aligned
    with ``texts``, or ``None`` on any failure. Empty input → ``[]``."""
    global _endpoint_disabled
    if isinstance(texts, str):
        texts = [texts]
    if not texts:
        return []
    # Truncate + preserve alignment; empty strings become "" (still embedded so
    # indices line up 1:1 with the caller's list).
    cleaned = [str(t or "")[:_MAX_CHARS] for t in texts]

    # ── Per-text cache ──────────────────────────────────────────────────
    # The prompt (with memory recall + skill routing) is rebuilt every loop but
    # the task query is constant within a run, so the same text is embedded many
    # times. Serve repeats from an in-process, bounded cache — repeated queries
    # cost zero network calls, and an all-cached request needs no session at all.
    keys = [_cache_key(t) for t in cleaned]
    result: list = [None] * len(cleaned)
    missing_idx = [i for i, h in enumerate(keys) if _embed_cache.get(h) is None]
    for i, h in enumerate(keys):
        if i not in missing_idx:
            result[i] = _embed_cache[h]
    if not missing_idx:
        return result
    if _endpoint_disabled:
        return None

    try:
        import requests  # local import: keep module cheap to import
    except Exception:
        return None

    session = session or _load_session()
    if session is None:
        return None
    try:
        profile = backend_profiles.resolve(_BACKEND_URL)
        headers, cookies = backend_profiles.request_auth(profile, session)
        headers = _with_client_budget(headers, _TIMEOUT)
    except Exception:
        return None

    to_fetch = [cleaned[i] for i in missing_idx]
    fetched: list = []
    # Chunk to respect the gateway's per-request cap while keeping alignment.
    for i in range(0, len(to_fetch), _MAX_BATCH):
        chunk = to_fetch[i:i + _MAX_BATCH]
        try:
            resp = requests.post(
                f"{profile.base_url}/api/embed",
                headers=headers, cookies=cookies,
                json={"input": chunk}, timeout=_TIMEOUT,
                allow_redirects=False,
            )
        except Exception:
            return None
        if resp.status_code in (404, 503):
            # No embedding provider configured / route absent — stop trying.
            _endpoint_disabled = True
            return None
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
            vecs = body.get("embeddings")
        except Exception:
            return None
        if not isinstance(vecs, list) or len(vecs) != len(chunk):
            return None
        fetched.extend(vecs)

    if len(fetched) != len(missing_idx):
        return None
    for idx, vec in zip(missing_idx, fetched):
        result[idx] = vec
        _cache_put(keys[idx], vec)
    return result


def embed_one(text: str, *, session: Optional[dict] = None) -> Optional[list]:
    """Embed a single string. Returns one vector or ``None``."""
    res = embed([text], session=session)
    if not res:
        return None
    return res[0]


_rank_disabled = False

# Cooldown after a rank call fails or times out. Ranking runs on every prompt
# rebuild, so a ranker that is slow or down was being paid for once per turn per
# agent — seconds of dead wait each time, for a result that was discarded anyway
# in favour of the local lexical fallback. One failure now silences it briefly
# instead of re-learning the same thing every turn. Short enough that recovery
# is picked up on its own.
_RANK_COOLDOWN_S = float(os.environ.get("LAINTAS_RANK_COOLDOWN", "120"))
# A ranking that ARRIVES but takes this long is treated the same way. Success is
# not the bar: the bar is whether blocking the prompt rebuild was worth it, and
# a multi-second wait for a relevance hint that has a local fallback never is.
_RANK_SLOW_S = float(os.environ.get("LAINTAS_RANK_SLOW", "3"))
_rank_cooldown_until = 0.0


def _rank_cooldown_start() -> None:
    global _rank_cooldown_until
    import time as _time
    _rank_cooldown_until = _time.time() + _RANK_COOLDOWN_S


def _rank_in_cooldown() -> bool:
    import time as _time
    return _time.time() < _rank_cooldown_until


def rank(query, candidates, *, top_k: Optional[int] = None,
         session: Optional[dict] = None) -> Optional[list]:
    """Rank candidates via the gateway's shared ``/api/rank`` service — the one
    place the ranking algorithm lives (semantic, with server-side lexical
    fallback). ``candidates``: list of ``(id, text)``. Returns
    ``[(id, score)]`` high→low, or ``None`` when the endpoint is unreachable
    (the caller then does its own offline fallback).

    Note: a 200 response already includes a ranking (semantic OR gateway-side
    lexical), so ``None`` means only "could not reach /api/rank", not "no match".
    """
    global _rank_disabled
    q = str(query or "").strip()
    if not q or not candidates:
        return []
    if _rank_disabled or _rank_in_cooldown():
        return None

    payload_cands = []
    for cid, text in candidates:
        t = str(text or "")
        if t:
            payload_cands.append({"id": str(cid), "text": t[:_MAX_CHARS]})
    if not payload_cands:
        return []
    body = {"query": q[:_MAX_CHARS], "candidates": payload_cands}
    if top_k is not None:
        try:
            body["top_k"] = max(1, int(top_k))
        except (TypeError, ValueError):
            pass

    try:
        import requests
    except Exception:
        return None
    session = session or _load_session()
    if session is None:
        return None
    import time as _time
    _t0 = _time.time()
    try:
        profile = backend_profiles.resolve(_BACKEND_URL)
        headers, cookies = backend_profiles.request_auth(profile, session)
        headers = _with_client_budget(headers, _RANK_TIMEOUT)
        resp = requests.post(
            f"{profile.base_url}/api/rank",
            headers=headers, cookies=cookies,
            json=body, timeout=_RANK_TIMEOUT, allow_redirects=False,
        )
    except Exception:
        _rank_cooldown_start()          # timeout / network error
        return None
    if resp.status_code == 404:
        _rank_disabled = True  # old gateway without /api/rank — stop trying
        return None
    if resp.status_code != 200:
        _rank_cooldown_start()
        return None
    if _time.time() - _t0 >= _RANK_SLOW_S:
        # Keep this answer — it is already paid for — but stop paying that
        # price on every turn until the ranker is quick again.
        _rank_cooldown_start()
    try:
        data = resp.json()
        ranked = data.get("ranked")
    except Exception:
        return None
    if not isinstance(ranked, list):
        return None
    out = []
    for item in ranked:
        if isinstance(item, dict) and "id" in item:
            try:
                out.append((str(item["id"]), float(item.get("score", 0.0))))
            except (TypeError, ValueError):
                continue
    return out


def cosine(a, b) -> float:
    """Cosine similarity of two equal-length vectors. Returns 0.0 on bad input."""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))
    except Exception:
        return 0.0


def top_k(query_vec, candidates, k: int = 5) -> list:
    """Rank ``candidates`` (list of ``(item, vector)``) by cosine to
    ``query_vec``; return the top ``k`` as ``(item, score)``, high→low."""
    if not query_vec or not candidates:
        return []
    scored = []
    for item, vec in candidates:
        scored.append((item, cosine(query_vec, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[: max(0, int(k))]
