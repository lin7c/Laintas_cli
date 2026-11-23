"""Local AI-usage accounting for laintas_cli.

Every billed AI call is recorded here: one JSONL line per call in
~/.laintas/usage/YYYY-MM.jsonl plus an in-process session accumulator. Purely
local — official backends contribute exact token counts from the gateway's
`_billing` metadata; backends that send no billing metadata get
character-estimated counts (marked `estimated` and rendered with `~`).

The monthly files are shared across CLI instances and projects (they live
under LAINTAS_HOME, not the per-cwd .laintas/), so `today`/range aggregates
reflect every concurrent CLI, while `session` covers only this process.
Recording must never break the chat path: every public entry point swallows
its own errors.

Record format (``"v": 2``). Fields added over time are optional on read; a
version-1 line (no ``v``) simply lacks them:

    ts, model, in, cachedIn, out, costCents, official, backend, estimated,
    truncated, pid                                   -- version 1
    v, kind, purpose, reasoning, cacheWrite, credits, funding, txId,
    session_id, run_id, agent_id                     -- version 2

``costCents`` is the money drawn from a balance for the call. It is ``null``
when the gateway did not say (organization-metered calls), and 0 when a
membership paid in ``credits`` instead — the two must not be read as "free".
``funding`` is the gateway's ``fundingSource`` (personal / subscription /
seat / organization) and ``txId`` its ledger id, so a local line can be
matched against the account's own history.

Attribution (``session_id``/``run_id``/``agent_id``) comes from the calling
thread's context, set by the agent loop with :func:`call_context`. A call made
outside any loop (a background job) carries empty attribution.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import paths

SCHEMA_VERSION = 2

_LOCK = threading.Lock()
#: The most recent records of this process, oldest first. Bounded: a
#: long-lived process (a bot, an --execute loop) must not grow without limit.
#: Session totals do not depend on this list — see `_SESSION_AGG`.
_SESSION: list[dict] = []
_SESSION_KEEP = 2000
_SESSION_AGG: Optional[dict] = None

_CTX = threading.local()
_CONTEXT_KEYS = ("session_id", "run_id", "agent_id")


# ── attribution context ────────────────────────────────────────────────────

@contextlib.contextmanager
def call_context(**fields):
    """Attribute calls made on this thread to a session/run/agent.

    Nested contexts stack: an inner loop's ids replace the outer ones until it
    exits. Unknown keys are ignored.
    """
    previous = dict(getattr(_CTX, "fields", {}) or {})
    merged = dict(previous)
    for key in _CONTEXT_KEYS:
        if fields.get(key) is not None:
            merged[key] = str(fields[key])[:128]
    _CTX.fields = merged
    try:
        yield
    finally:
        _CTX.fields = previous


def current_context() -> dict:
    return dict(getattr(_CTX, "fields", {}) or {})


def replace_context(fields: Optional[dict]) -> None:
    """Set this thread's attribution outright (for callers that cannot wrap
    their work in `call_context`, and to restore a saved `current_context`)."""
    _CTX.fields = {key: str(value)[:128] for key, value in (fields or {}).items()
                   if key in _CONTEXT_KEYS and value is not None}


# ── storage ────────────────────────────────────────────────────────────────

def _usage_dir() -> Path:
    d = paths.LAINTAS_HOME / "usage"
    paths.require_account_selected()
    paths.assert_account_write(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 heuristic for backends that report no token counts."""
    return max(0, len(text or "")) // 4


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _credits(value) -> Optional[str]:
    """Credits as a decimal string (never a float on disk: ai-pow's protocol
    refuses floats, and a string keeps what the gateway said exactly)."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0:  # NaN or negative
        return None
    return format(number, "g")


def billing_fields(billing: Optional[dict]) -> dict:
    """The gateway's `_billing` / `billing` object as `record()` keywords."""
    billing = billing if isinstance(billing, dict) else {}
    cost = billing.get("costCents")
    return {
        "cost_cents": None if cost is None else _int(cost),
        "credits": billing.get("credits"),
        "funding": str(billing.get("fundingSource") or ""),
        "tx_id": str(billing.get("txId") or ""),
        "reasoning_tokens": _int(billing.get("reasoningTokens")),
        "cache_write_tokens": _int(billing.get("cacheWriteTokens")),
    }


def record(*, model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
           cost_cents: Optional[int] = 0, official: bool = False,
           backend_kind: str = "", estimated: bool = False,
           truncated: bool = False, cached_prompt_tokens: int = 0,
           kind: str = "chat", purpose: str = "",
           reasoning_tokens: int = 0, cache_write_tokens: int = 0,
           credits=None, funding: str = "", tx_id: str = "") -> Optional[dict]:
    """Append one AI-call record and return it (None on failure). Never raises.

    ``cached_prompt_tokens`` is the subset of ``prompt_tokens`` the provider
    served from its context cache (gateway `_billing.cachedPromptTokens`). It
    is the diagnostic for prompt-prefix stability: a healthy agent loop reuses
    the same system prompt + tool schemas every call and should show a high
    hit rate, while a prefix that changes each turn (a clock in the system
    prompt, a per-turn block near the top) drives it toward zero and pays the
    full input rate every time.

    ``kind`` names the lane (chat / image / video / ...), ``purpose`` why the
    call was made (the agent loop, or an auxiliary task such as compaction).
    """
    try:
        prompt = _int(prompt_tokens)
        completion = _int(completion_tokens)
        rec = {
            "v": SCHEMA_VERSION,
            "ts": round(time.time(), 3),
            "kind": (kind or "chat")[:24],
            "purpose": (purpose or "")[:48],
            "model": (model or "(default)")[:80],
            "in": prompt,
            "cachedIn": min(_int(cached_prompt_tokens), prompt),
            "out": completion,
            "reasoning": min(_int(reasoning_tokens), completion) if completion else _int(reasoning_tokens),
            "cacheWrite": _int(cache_write_tokens),
            "costCents": None if cost_cents is None else _int(cost_cents),
            "credits": _credits(credits),
            "funding": (funding or "")[:24],
            "txId": (tx_id or "")[:80],
            "official": bool(official),
            "backend": (backend_kind or "")[:24],
            "estimated": bool(estimated),
            # Individual truncations are recovered silently now, so this tally
            # is the only lasting trace of them. A model truncating on a large
            # share of its calls is a configuration signal (wrong model for the
            # workload, or a ceiling worth raising) that belongs in /usage
            # rather than in a warning on every occurrence.
            "truncated": bool(truncated),
            "pid": os.getpid(),
        }
        context = current_context()
        for key in _CONTEXT_KEYS:
            rec[key] = context.get(key, "")
    except Exception:
        return None
    try:
        import extension_runtime
        extension_runtime.emit("model.usage", **rec)
    except Exception:
        pass
    try:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _LOCK:
            _remember(rec)
            fp = _usage_dir() / (datetime.now().strftime("%Y-%m") + ".jsonl")
            with open(fp, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
    return rec


def record_billing(*, kind: str, model: str, billing: Optional[dict],
                   official: bool, backend_kind: str = "",
                   purpose: str = "") -> Optional[dict]:
    """Record a non-chat gateway call (an image, a clip) from its `billing`
    object. Token counts are zero: these lanes are priced per item."""
    return record(model=model, kind=kind, purpose=purpose, official=official,
                  backend_kind=backend_kind, **billing_fields(billing))


def _remember(rec: dict) -> None:
    """Fold `rec` into this process's session view. Caller holds `_LOCK`."""
    global _SESSION_AGG
    _SESSION.append(rec)
    if len(_SESSION) > _SESSION_KEEP:
        del _SESSION[:len(_SESSION) - _SESSION_KEEP]
    if _SESSION_AGG is None:
        _SESSION_AGG = _aggregate([])
    _fold(_SESSION_AGG, rec)


def session_totals() -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) recorded by this process."""
    try:
        with _LOCK:
            totals = (_SESSION_AGG or _aggregate([]))["totals"]
            return int(totals["in"]), int(totals["out"])
    except Exception:
        return 0, 0


# ── reading ────────────────────────────────────────────────────────────────

def _months_covering(since_ts: float) -> list[str]:
    """Month keys (YYYY-MM) from `since_ts` through now, oldest first."""
    out = []
    cur = datetime.fromtimestamp(since_ts).replace(day=1, hour=0, minute=0,
                                                   second=0, microsecond=0)
    end = datetime.now()
    while cur <= end:
        out.append(cur.strftime("%Y-%m"))
        # Advance one month without dateutil
        cur = (cur + timedelta(days=32)).replace(day=1)
    return out


def _iter_records(since_ts: float) -> Iterator[dict]:
    for month in _months_covering(since_ts):
        fp = paths.LAINTAS_HOME / "usage" / f"{month}.jsonl"
        if not fp.exists():
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # torn/corrupt line — skip, keep reading
                    if isinstance(rec, dict) and rec.get("ts", 0) >= since_ts:
                        yield rec
        except OSError:
            continue


def _bucket() -> dict:
    return {"calls": 0, "in": 0, "cachedIn": 0, "out": 0, "reasoning": 0,
            "costCents": 0, "credits": 0.0, "unknownCost": 0,
            "estimated": False, "truncated": 0}


def _fold(agg: dict, r: dict) -> None:
    m = agg["models"].setdefault(r.get("model", "?"), _bucket())
    for bucket in (m, agg["totals"]):
        bucket["calls"] += 1
        bucket["in"] += _int(r.get("in"))
        # Absent on records written before cache accounting existed; those
        # simply contribute 0 hits, so an old file reads as "unknown/low"
        # rather than breaking the aggregate.
        bucket["cachedIn"] += _int(r.get("cachedIn"))
        bucket["out"] += _int(r.get("out"))
        bucket["reasoning"] += _int(r.get("reasoning"))
        if r.get("costCents") is None and "costCents" in r:
            # Metered elsewhere (an organization): not free, just not ours to price.
            bucket["unknownCost"] += 1
        else:
            bucket["costCents"] += _int(r.get("costCents"))
        try:
            bucket["credits"] += float(r.get("credits") or 0)
        except (TypeError, ValueError):
            pass
        bucket["estimated"] = bucket["estimated"] or bool(r.get("estimated"))
        bucket["truncated"] += 1 if r.get("truncated") else 0


def _aggregate(records) -> dict:
    """Fold records into totals + a per-model breakdown."""
    agg = {"totals": _bucket(), "models": {}}
    for r in records:
        _fold(agg, r)
    return agg


def _copy(agg: dict) -> dict:
    return {"totals": dict(agg["totals"]),
            "models": {k: dict(v) for k, v in agg["models"].items()}}


def summarize(days: int = 30) -> dict:
    """Aggregate local usage: this session / today / the last `days` days.

    `session` comes from process memory; `today` and `range` are read from the
    shared monthly files so concurrent CLI instances are all counted.
    """
    try:
        now = time.time()
        midnight = datetime.now().replace(hour=0, minute=0, second=0,
                                          microsecond=0).timestamp()
        range_start = now - days * 86400
        range_records = list(_iter_records(range_start))
        with _LOCK:
            session = _copy(_SESSION_AGG) if _SESSION_AGG is not None \
                else _aggregate(_SESSION)
        return {
            "session": session,
            "today": _aggregate(r for r in range_records
                                if r.get("ts", 0) >= midnight),
            "range": _aggregate(range_records),
            "days": days,
        }
    except Exception:
        empty = _aggregate([])
        return {"session": empty, "today": empty, "range": empty, "days": days}
