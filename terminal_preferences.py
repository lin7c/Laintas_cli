"""Private preferences scoped to one logical terminal.

The process instance id intentionally changes on every CLI launch.  User
choices such as model and mode instead use ``paths.TERMINAL_ID`` so they
survive `/q` + relaunch without leaking into another open terminal.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any

import json_store
import paths


_LOCK = threading.RLock()
_CACHE_PATH: Path | None = None
_CACHE: dict | None = None

#: What a brand-new terminal inherits from the last choices made anywhere.
#: An allowlist rather than "everything", because not every preference is a
#: taste: ``backend_profile`` decides which server the session talks to and
#: whether Laintas credentials are stripped, so a new terminal must start on
#: the official backend rather than quietly inheriting an external one that
#: was selected somewhere else. Anything with a blast radius beyond the
#: user's own comfort stays strictly terminal-scoped.
SEEDED_KEYS = frozenset({"model", "provider", "mode", "agent", "ui"})

PERSISTED_UI_KEYS = frozenset({
    "detail",
    "enable_mouse",
    "paste_summary",
    "paste_summary_min_lines",
    "paste_summary_min_chars",
    "rprompt_slots_detail_on",
    "rprompt_slots_detail_off",
    "rprompt_slot_order",
    "reasoning_effort",
    "show_billing",
    "stream_preview",
    "theme",
    "markdown_theme",
    "critic_profile",
    "critic_prompt_file",
    "search_engine",
    "search_laintas_api_key",
    "search_laintas_api_url",
    "search_proxy",
    "search_proxy_mode",
    "search_cookie_enabled",
    "search_cookie_domains",
    "search_cookie_names",
    "identity_enabled",
    "fetch_render",
    "fetch_unlock",
    "fetch_wayback",
})


def preference_path() -> Path:
    terminal_id = getattr(paths, "TERMINAL_ID", "terminal-default")
    return paths.SESSIONS_DIR / f"{terminal_id}_preferences.json"


# ── Inheriting from the last terminal you used ──────────────────────────
# Per-terminal files are the right storage and the wrong starting point.
# TERMINAL_ID is derived from the tty and POSIX session id when the terminal
# emulator offers nothing better (paths._terminal_identity_source), and both
# change on every new SSH login — so each connection opened a brand-new, empty
# preference file and the user set their model and mode again. Measured on one
# machine before this: 45 distinct preference files in a month, each holding a
# choice that was never read back.
#
# A terminal with no file of its own therefore starts from the most recently
# written one, and keeps its own from then on. Deliberately derived rather
# than stored in a shared "defaults" file: a shared file has to be WRITTEN,
# and a write from inside the storage primitive is a global side effect —
# one terminal pushing its inherited value back over a newer choice made in
# another, and any test that touched preferences rewriting the developer's
# real ones. Both happened; the fix was to have nothing to write.

#: What a brand-new terminal inherits. An allowlist rather than "everything",
#: because not every preference is a taste: ``backend_profile`` decides which
#: server the session talks to and whether Laintas credentials are stripped,
#: so a new terminal must start on the official backend rather than quietly
#: inheriting an external one selected somewhere else. Anything with a blast
#: radius beyond the user's own comfort stays strictly terminal-scoped.
SEEDED_KEYS = frozenset({"model", "provider", "mode", "agent", "ui"})

_SEED: dict | None = None


def _read_file(path: Path) -> dict:
    if not path.exists() or not paths.ensure_private_file(path):
        return {}
    data = json_store.load_json(path, dict)
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        return {}
    data["version"] = 1
    return data


def _seedable(data: dict) -> dict:
    out = {key: value for key, value in data.items() if key in SEEDED_KEYS}
    out["version"] = 1
    return out


def seed_new_terminal() -> dict:
    """Give a terminal with no preferences of its own the last ones used.

    Called once, from the CLI entry point, and never from ``_read``: seeding
    inside the storage primitive would make every library read of a
    never-configured terminal materialise a file — including in tests, which
    then write the developer's real preferences. Returns what was seeded, or
    an empty dict when there was nothing to inherit or the file already
    exists.
    """
    path = preference_path()
    if path.exists():
        return {}
    seed = inherited_defaults()
    if len(seed) <= 1:
        return {}
    with _LOCK:
        try:
            json_store.save_json_atomic(path, seed, mode=0o600)
        except OSError:
            return {}
        reset_cache()
    return {key: value for key, value in seed.items() if key != "version"}


def inherited_defaults(*, refresh: bool = False) -> dict:
    """The seedable half of the most recently written preference file.

    Cached per process: it only answers "what did this machine last use",
    which cannot change under a session that has not written anything itself.
    """
    global _SEED
    with _LOCK:
        if _SEED is not None and not refresh:
            return dict(_SEED)
        seed = {"version": 1}
        try:
            candidates = sorted(
                paths.SESSIONS_DIR.glob("*_preferences.json"),
                key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            candidates = []
        current = preference_path()
        for candidate in candidates:
            if candidate == current:
                continue
            data = _seedable(_read_file(candidate))
            if len(data) > 1:            # more than the version marker
                seed = data
                break
        _SEED = seed
        return dict(seed)


def _read(path: Path) -> dict:
    if not path.exists() or not paths.ensure_private_file(path):
        return {"version": 1}
    data = json_store.load_json(path, dict)
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        return {"version": 1}
    data["version"] = 1
    return data


def load(*, refresh: bool = False) -> dict:
    global _CACHE_PATH, _CACHE
    path = preference_path()
    with _LOCK:
        if refresh or _CACHE is None or _CACHE_PATH != path:
            _CACHE = _read(path)
            _CACHE_PATH = path
        return copy.deepcopy(_CACHE)


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def update(values: dict[str, Any], *, remove: tuple[str, ...] = ()) -> dict:
    """Merge and atomically save preferences without dropping other fields."""
    global _CACHE_PATH, _CACHE
    path = preference_path()
    with _LOCK:
        # Re-read before every mutation so independent modules cannot overwrite
        # fields written since their own cache was populated.
        data = _read(path)
        for key in remove:
            data.pop(key, None)
        data.update(values)
        data["version"] = 1
        json_store.save_json_atomic(path, data, mode=0o600)
        _CACHE_PATH = path
        _CACHE = data
        return copy.deepcopy(data)


def set_value(key: str, value: Any) -> None:
    update({key: value})


def delete(*keys: str) -> None:
    if keys:
        update({}, remove=tuple(keys))


def get_ui_preferences() -> dict:
    value = get("ui", {})
    if not isinstance(value, dict):
        return {}
    return {
        key: copy.deepcopy(item) for key, item in value.items()
        if key in PERSISTED_UI_KEYS
    }


def set_ui_preference(key: str, value: Any) -> None:
    if key not in PERSISTED_UI_KEYS:
        raise KeyError(f"UI preference is not persistent: {key}")
    ui = get_ui_preferences()
    ui[key] = value
    update({"ui": ui})


def clear_ui_preferences() -> None:
    delete("ui")


def reset_cache() -> None:
    """Clear the in-memory cache (primarily useful for restart-style tests)."""
    global _CACHE_PATH, _CACHE, _SEED
    with _LOCK:
        _CACHE_PATH = None
        _CACHE = None
        _SEED = None
