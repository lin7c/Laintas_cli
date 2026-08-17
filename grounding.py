"""Grounding repair — thin wrapper over the gateway's ``/api/verify``.

Used on the compaction summary. Every other thing the model says can be checked
against the transcript later; the summary is what REPLACES the transcript, so a
fabricated line in it is invisible from that point on and steers the rest of the
session. That is the one place a grounding check pays for itself.

Repair, not deletion. A sentence that distorts a fact ("cron 上限收紧到 512M"
where the head says 256M) is swapped for the head sentence it came from, which
RESTORES the fact — deleting it would only lose it. A sentence with no close
counterpart in the head is a fabrication with nothing to restore, and goes.

Design contract, same as ``embeddings``: **never raise, never block hard.** Any
failure — no session, endpoint absent, network error, malformed response —
leaves the summary exactly as the model wrote it. A grounding outage must never
be the reason a compaction fails.

The NLI model, the API key, the sentence splitting, the threshold and the
replacement matching all live at the gateway and the service behind it, so every
client judges and repairs identically — same reasoning as ``rank``.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import backend_profiles
import paths


_BACKEND_URL = os.environ.get("LAINTAS_BACKEND") or "https://laintas.com"
# One NLI forward pass per sentence on CPU, more when the head has to be
# windowed. It runs during compaction, which is already slow, but it must not be
# able to stall the loop indefinitely.
_TIMEOUT = float(os.environ.get("LAINTAS_VERIFY_TIMEOUT", "150"))

# Once the endpoint answers 404/503 it will not sprout a checker mid-run.
_endpoint_disabled = False


def _load_session() -> Optional[dict]:
    try:
        f = paths.SESSION_FILE
        if not f.exists():
            return None
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def repair(source: str, summary: str, *, session: Optional[dict] = None) -> Optional[dict]:
    """Return the gateway's repair result, or ``None`` when it could not run.

    ``None`` is "not checked" — a different thing from "checked and clean", and
    callers must not read it as a clean bill.
    """
    global _endpoint_disabled
    source = str(source or "").strip()
    summary = str(summary or "").strip()
    if not source or not summary or _endpoint_disabled:
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
    except Exception:
        return None

    try:
        resp = requests.post(
            f"{profile.base_url}/api/verify",
            headers=headers, cookies=cookies,
            json={"source": source, "claim": summary},
            timeout=_TIMEOUT, allow_redirects=False,
        )
    except Exception:
        return None
    if resp.status_code in (404, 503):
        _endpoint_disabled = True
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    # Only "nli" means a pass actually ran. "unavailable" is the checker being
    # down; "none" is there having been nothing to check. Neither is a clean
    # bill, and neither produces a repaired text worth trusting over the input.
    if body.get("method") != "nli" or not isinstance(body.get("sanitized"), str):
        return None
    return body


def repair_summary(source: str, summary: str, *,
                   session: Optional[dict] = None, log=None) -> str:
    """Repaired summary, or the original when the pass could not run or misfired.

    Guards, in order of how badly each would hurt:

    * nothing came back → keep the original;
    * the repair came back empty while the input was not → keep the original.
      A checker that has gone wrong (empty source, a threshold that no longer
      fits the model) would flag every sentence and hand back "", taking the
      session's memory with it. Losing the check is recoverable; losing the
      summary is not.
    """
    body = repair(source, summary, session=session)
    if not body:
        return summary
    fixed = str(body.get("sanitized") or "").strip()
    if not fixed:
        if log:
            log("grounding: repair came back empty — keeping the summary as written")
        return summary
    if log:
        for c in body.get("changes") or []:
            act, text = c.get("action"), str(c.get("text") or "")[:70]
            if act == "replaced":
                log(f"grounding: repaired [{c.get('type')} {c.get('score')}] {text}"
                    f"  ->  {str(c.get('replacement') or '')[:70]}")
            else:
                log(f"grounding: dropped [{c.get('type')} {c.get('score')}] {text}")
        if body.get("source_truncated"):
            log("grounding: head was only partly readable — repair was partial")
    return fixed
