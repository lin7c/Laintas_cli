"""Preferences for the `auto` model: which models it may pick, and which model
to use for everyday and for complex tasks.

Set with ``/model routing``. Sent with every chat request whose model is auto
as the request field ``routing``; the gateway applies them (it decides whether
a task is everyday or complex, from the same rules that pick the thinking
gear) and skips any model it cannot serve right now, so a stale entry narrows
nothing rather than failing a call.

Stored as one terminal preference beside ``model``, and seeded into new
terminals the same way: a taste, not a credential.
"""
from __future__ import annotations

from typing import Any

import terminal_preferences

PREF_KEY = "auto_routing"
SLOTS = ("everyday", "complex")
#: The gateway reads at most this many candidates.
CANDIDATE_LIMIT = 80

#: What the backend last said about auto, from the model catalogue:
#: ``routes_to`` — the models auto may pick right now (modelCapabilities.auto
#: .routesTo); ``supported`` — whether it honours the `routing` field;
#: ``backend`` — the backend these were fetched from, so a switch is noticed.
_CATALOGUE: dict[str, Any] = {"routes_to": [], "supported": False, "backend": ""}


def normalise(value: Any) -> dict:
    """``{"candidates": [...], "everyday": str, "complex": str}`` from anything."""
    raw = value if isinstance(value, dict) else {}
    candidates: list[str] = []
    for item in raw.get("candidates") or []:
        item = str(item).strip()
        if item and item not in candidates:
            candidates.append(item)
    out = {"candidates": candidates[:CANDIDATE_LIMIT]}
    for slot in SLOTS:
        out[slot] = str(raw.get(slot) or "").strip()
    return out


def load() -> dict:
    return normalise(terminal_preferences.get(PREF_KEY, {}))


def save(prefs: dict) -> dict:
    """Store `prefs`; an all-empty value removes the preference entirely."""
    prefs = normalise(prefs)
    if is_default(prefs):
        terminal_preferences.delete(PREF_KEY)
    else:
        terminal_preferences.set_value(PREF_KEY, prefs)
    return prefs


def is_default(prefs: dict) -> bool:
    return not (prefs.get("candidates") or any(prefs.get(s) for s in SLOTS))


def request_field() -> dict:
    """The request's ``routing`` field, or {} when nothing is set."""
    try:
        prefs = load()
    except Exception:
        return {}
    field: dict = {}
    if prefs["candidates"]:
        field["candidates"] = list(prefs["candidates"])
    for slot in SLOTS:
        if prefs[slot]:
            field[slot] = prefs[slot]
    return field


def set_candidates(prefs: dict, chosen: list[str], pool: list[str]) -> tuple[dict, list[str]]:
    """Apply a new candidate selection. Returns ``(prefs, cleared_slots)``.

    Choosing every model in `pool` stores no list at all, so a model the
    operator adds later is included rather than silently left out. A slot whose
    model is no longer a candidate is cleared: the gateway would skip it anyway,
    and a setting that cannot take effect should not stay on screen.
    """
    if not chosen:
        # An empty list would read back as "no narrowing" — the opposite.
        raise ValueError("keep at least one model")
    chosen = [m for m in pool if m in set(chosen)] or list(chosen)
    candidates = [] if pool and set(pool) <= set(chosen) else chosen
    prefs = dict(normalise(prefs), candidates=candidates)
    cleared = []
    if candidates:
        for slot in SLOTS:
            if prefs[slot] and prefs[slot] not in candidates:
                prefs[slot] = ""
                cleared.append(slot)
    return prefs, cleared


def effective_pool(prefs: dict, routes_to: list[str]) -> list[str]:
    """The models auto may pick under `prefs`, in the backend's order."""
    if not prefs.get("candidates"):
        return list(routes_to)
    return [m for m in routes_to if m in prefs["candidates"]]


def remember_catalogue(payload: Any, backend: str) -> None:
    """Record what a /api/models response says about auto."""
    if not isinstance(payload, dict):
        return
    caps = payload.get("modelCapabilities")
    # A backend whose catalogue has no auto entry is still an answer: it has
    # nothing for auto to pick and will not apply preferences.
    auto = caps.get("auto") if isinstance(caps, dict) else None
    auto = auto if isinstance(auto, dict) else {}
    routes = auto.get("routesTo")
    _CATALOGUE["routes_to"] = [str(m) for m in routes if m] if isinstance(routes, list) else []
    _CATALOGUE["supported"] = bool(auto.get("routingPrefs"))
    _CATALOGUE["backend"] = backend


def catalogue(backend: str) -> tuple[list[str], bool] | None:
    """``(routes_to, supported)`` last seen for `backend`, or None if not seen."""
    if not _CATALOGUE["backend"] or _CATALOGUE["backend"] != backend:
        return None
    return list(_CATALOGUE["routes_to"]), bool(_CATALOGUE["supported"])
