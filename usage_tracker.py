"""Local AI-usage accounting for laintas_cli.

Every AI call that flows through call_ai_backend() is recorded here: one JSONL
line per call in ~/.laintas/usage/YYYY-MM.jsonl plus an in-process session
accumulator. Purely local — official backends contribute exact token counts
from the gateway's `_billing` metadata; backends that send no billing metadata
get character-estimated counts (marked `estimated` and rendered with `~`).

The monthly files are shared across CLI instances and projects (they live
under LAINTAS_HOME, not the per-cwd .laintas/), so `today`/range aggregates
reflect every concurrent CLI, while `session` covers only this process.
Recording must never break the chat path: every public entry point swallows
its own errors.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import paths

_LOCK = threading.Lock()
_SESSION: list[dict] = []  # records made by this process, in arrival order


def _usage_dir() -> Path:
    d = paths.LAINTAS_HOME / "usage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 heuristic for backends that report no token counts."""
    return max(0, len(text or "")) // 4


def record(*, model: str, prompt_tokens: int, completion_tokens: int,
           cost_cents: int = 0, official: bool = False, backend_kind: str = "",
           estimated: bool = False, truncated: bool = False,
           cached_prompt_tokens: int = 0) -> None:
    """Append one AI-call record. Never raises.

    ``cached_prompt_tokens`` is the subset of ``prompt_tokens`` the provider
    served from its context cache (gateway `_billing.cachedPromptTokens`). It
    is the diagnostic for prompt-prefix stability: a healthy agent loop reuses
    the same system prompt + tool schemas every call and should show a high
    hit rate, while a prefix that changes each turn (a clock in the system
    prompt, a per-turn block near the top) drives it toward zero and pays the
    full input rate every time.
    """
    try:
        rec = {
            "ts": round(time.time(), 3),
            "model": (model or "(default)")[:80],
            "in": max(0, int(prompt_tokens or 0)),
            "cachedIn": max(0, min(int(cached_prompt_tokens or 0),
                                   max(0, int(prompt_tokens or 0)))),
            "out": max(0, int(completion_tokens or 0)),
            "costCents": max(0, int(cost_cents or 0)),
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
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _LOCK:
            _SESSION.append(rec)
            fp = _usage_dir() / (datetime.now().strftime("%Y-%m") + ".jsonl")
            with open(fp, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


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


def _aggregate(records) -> dict:
    """Fold records into totals + a per-model breakdown."""
    models: dict[str, dict] = {}
    totals = {"calls": 0, "in": 0, "cachedIn": 0, "out": 0, "costCents": 0,
              "estimated": False, "truncated": 0}
    for r in records:
        m = models.setdefault(r.get("model", "?"), {
            "calls": 0, "in": 0, "cachedIn": 0, "out": 0, "costCents": 0,
            "estimated": False, "truncated": 0,
        })
        for bucket in (m, totals):
            bucket["calls"] += 1
            bucket["in"] += int(r.get("in", 0) or 0)
            # Absent on records written before cache accounting existed; those
            # simply contribute 0 hits, so an old file reads as "unknown/low"
            # rather than breaking the aggregate.
            bucket["cachedIn"] += int(r.get("cachedIn", 0) or 0)
            bucket["out"] += int(r.get("out", 0) or 0)
            bucket["costCents"] += int(r.get("costCents", 0) or 0)
            bucket["estimated"] = bucket["estimated"] or bool(r.get("estimated"))
            bucket["truncated"] += 1 if r.get("truncated") else 0
    return {"totals": totals, "models": models}


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
            session_records = list(_SESSION)
        return {
            "session": _aggregate(session_records),
            "today": _aggregate(r for r in range_records
                                if r.get("ts", 0) >= midnight),
            "range": _aggregate(range_records),
            "days": days,
        }
    except Exception:
        empty = _aggregate([])
        return {"session": empty, "today": empty, "range": empty, "days": days}
