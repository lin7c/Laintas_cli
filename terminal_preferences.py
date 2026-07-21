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

PERSISTED_UI_KEYS = frozenset({
    "detail",
    "enable_mouse",
    "paste_summary",
    "paste_summary_min_lines",
    "paste_summary_min_chars",
    "show_billing",
    "stream_preview",
    "theme",
    "markdown_theme",
})


def preference_path() -> Path:
    terminal_id = getattr(paths, "TERMINAL_ID", "terminal-default")
    return paths.SESSIONS_DIR / f"{terminal_id}_preferences.json"


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
    global _CACHE_PATH, _CACHE
    with _LOCK:
        _CACHE_PATH = None
        _CACHE = None
