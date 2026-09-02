"""Re-validate stale memories against the source that moved under them.

``mem_evidence`` flags a claim when its cited file changes; it deliberately
cannot tell whether the claim is still TRUE, because that needs someone to read
the new source. This module is that reader, and it runs off the critical path —
in the idle pass after a turn, never inside one.

Three verdicts, and the point of the design is that all three are recorded:

  valid    — the file changed elsewhere; re-pin the fingerprint, stay active
  update   — still about the same thing, but the fact moved: write a SUCCESSOR
             entry and supersede the old one, so Y → Y' → Y'' is walkable
             instead of the old text being overwritten and lost
  invalid  — no longer true and nothing replaces it: retire, keep the file

Nothing here deletes, and nothing rewrites an existing entry's prose in place.
A revision that overwrites its predecessor destroys the only record of what the
agent used to believe, which is exactly the evidence you want when a memory
turns out to have been wrong for a while.

Best-effort throughout: the caller is a daemon thread and a failed review must
leave the entry stale rather than guess.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

import memory_system
import mem_evidence

#: Source shown to the reviewer per evidence item. A stale memory is usually
#: about one function, and the whole file is both expensive and worse signal.
EXCERPT_LINES = 120
EXCERPT_CHARS = 6000

#: Reviews per idle pass. Staleness arrives in bursts (one refactor touches
#: many files), and draining the whole backlog in one pass would turn a
#: background nicety into a visible stall and a real bill.
MAX_PER_PASS = 5

SYSTEM_PROMPT = """You re-validate a stored memory against the source file it cites, after that file changed.

You are given: the memory (its description and body), and the CURRENT content of the region it cited.

Decide ONE verdict:
- "valid": the memory is still accurate. The file changed somewhere that does not affect it.
- "update": the memory is about the right thing but the fact itself changed. Supply the corrected description and body.
- "invalid": the memory describes something that no longer exists and has no successor.

Rules:
- Judge ONLY against the source shown. If the source shown is not enough to tell, answer "valid" — leaving a claim standing is safer than deleting one on a guess.
- For "update", keep the same subject. A memory about the auth flow stays about the auth flow; if the file now describes something unrelated, that is "invalid", not "update".
- Do not invent facts that are not in the source shown.

Reply with ONE JSON object and nothing else:
{"verdict": "valid" | "update" | "invalid", "description": "...", "body": "...", "reason": "one short sentence"}
"description" and "body" are required only for "update"."""


def _excerpt(item: dict) -> str:
    """Current text of one cited region, or a note that it is gone."""
    path = str(item.get("path") or "")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return f"--- {path}\n(file is missing or unreadable)"
    start = int(item.get("start") or 1)
    end = int(item.get("end") or (start + EXCERPT_LINES - 1))
    # A recorded range only bounds where to look; the edit that invalidated the
    # claim may well have moved the code, so widen rather than trust it.
    lo = max(0, start - 1 - EXCERPT_LINES // 4)
    hi = min(len(lines), end + EXCERPT_LINES // 4)
    if hi - lo > EXCERPT_LINES:
        hi = lo + EXCERPT_LINES
    body = "".join(lines[lo:hi])[:EXCERPT_CHARS]
    return f"--- {path} (lines {lo + 1}-{hi} of {len(lines)})\n{body}"


def _parse_verdict(text: str) -> Optional[dict]:
    """Pull the JSON object out of a reply, tolerating fences and prose."""
    raw = str(text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            raw = brace.group(0)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("valid", "update", "invalid"):
        return None
    data["verdict"] = verdict
    return data


def review_one(entry: dict, llm_fn: Callable[[list], str]) -> dict:
    """Re-validate one stale entry. Returns {name, verdict, applied, detail}."""
    name = entry["name"]
    data = memory_system.read_memory(name)
    if data is None:
        return {"name": name, "verdict": "skipped", "applied": False,
                "detail": "memory disappeared"}

    evidence = entry.get("evidence") or []
    if not evidence:
        return {"name": name, "verdict": "skipped", "applied": False,
                "detail": "no evidence to review against"}

    sources = "\n\n".join(_excerpt(item) for item in evidence)
    message = (f"MEMORY (name: {name})\n"
               f"description: {data['meta'].get('description', '')}\n"
               f"body:\n{data['body'][:4000]}\n\n"
               f"CURRENT SOURCE IT CITED:\n{sources}")
    try:
        reply = llm_fn([{"role": "user", "content": message}])
    except Exception as exc:
        return {"name": name, "verdict": "error", "applied": False,
                "detail": f"{type(exc).__name__}: {exc}"}

    verdict = _parse_verdict(reply)
    if verdict is None:
        return {"name": name, "verdict": "error", "applied": False,
                "detail": "reviewer returned no usable verdict"}

    kind = verdict["verdict"]
    reason = str(verdict.get("reason") or "")[:200]

    if kind == "valid":
        # Re-pin to what the file says NOW: the claim survived this edit, and
        # leaving the old hash would re-flag it on the very next write.
        fresh = [mem_evidence.evidence_for(item["path"],
                                           start=item.get("start"),
                                           end=item.get("end"))
                 for item in evidence]
        ok, msg = memory_system.clear_stale(name, evidence=fresh)
        return {"name": name, "verdict": kind, "applied": ok, "detail": reason or msg}

    if kind == "invalid":
        ok, msg = memory_system.retire_memory(
            name, f"reviewed after source changed: {reason}" if reason
            else "no longer true after the cited source changed")
        return {"name": name, "verdict": kind, "applied": ok, "detail": reason or msg}

    # update — successor entry, then supersede. Written in that order so a
    # failure leaves the old entry standing rather than retiring a claim whose
    # replacement never landed.
    description = str(verdict.get("description") or "").strip()
    body = str(verdict.get("body") or "").strip()
    if not description or not body:
        return {"name": name, "verdict": kind, "applied": False,
                "detail": "reviewer chose 'update' without supplying the new text"}
    successor = memory_system.successor_name(name)
    fresh = [mem_evidence.evidence_for(item["path"],
                                       start=item.get("start"),
                                       end=item.get("end"))
             for item in evidence]
    ok, msg = memory_system.write_memory(
        successor, entry.get("type", "project"), description, body,
        scope=entry.get("scope"), scope_id=entry.get("scope_id"),
        importance=float(entry.get("importance", 0.5) or 0.5),
        evidence=fresh)
    if not ok:
        return {"name": name, "verdict": kind, "applied": False, "detail": msg}
    ok2, msg2 = memory_system.supersede_memory(name, successor)
    return {"name": name, "verdict": kind, "applied": ok2,
            "detail": f"{name} → {successor}" if ok2 else msg2}


def review_stale(llm_fn: Callable[[list], str],
                 limit: int = MAX_PER_PASS) -> list[dict]:
    """Re-validate up to ``limit`` stale memories. Cheapest checks first.

    ``mem_evidence.reconcile`` runs before anything is sent to a model: an
    edit that was reverted needs no judgement, and paying for one would be
    the kind of quiet waste that makes background work indefensible.
    """
    try:
        mem_evidence.reconcile()
    except Exception:
        pass
    results = []
    stale = memory_system.list_stale()
    # Oldest first: a claim that has been unverified longest is the one most
    # likely to be quietly wrong in the prompt right now.
    stale.sort(key=lambda entry: entry.get("mtime", 0))
    for entry in stale[:max(1, int(limit or MAX_PER_PASS))]:
        results.append(review_one(entry, llm_fn))
    if results:
        try:
            mem_evidence.invalidate_cache()
        except Exception:
            pass
    return results
