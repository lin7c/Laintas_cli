"""Shared JSON-file persistence helpers.

Every small on-disk store in this project (policy config, task list, trust
store, checkpoints, plan state, mode config, agent persistence, ...) used to
hand-roll its own "load with fallback" and "write atomically" logic
independently. That duplication already produced one real inconsistency:
most stores wrote via temp-file + fsync + atomic rename, but at least one
(snapshot.py's checkpoint store) wrote straight to the target file — a crash
mid-write there would truncate/corrupt it. This module is the one
implementation; existing call-sites are being migrated to it gradually
rather than all at once.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Union


def load_json(path: Union[str, Path], default: Any = None) -> Any:
    """Read *path* as JSON.

    Returns *default* — called first if it's callable (so mutable defaults
    like ``dict``/``list`` don't get shared across callers), otherwise
    returned as-is — when the file is missing, unreadable, or not valid
    JSON. Never raises.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return default() if callable(default) else default
    try:
        return json.loads(text)
    except ValueError:
        return default() if callable(default) else default


def save_json_atomic(path: Union[str, Path], data: Any, *, indent: int = 2,
                     ensure_ascii: bool = False, mode: int = None) -> None:
    """Write *data* as JSON to *path* via temp-file + fsync + atomic rename.

    A crash or kill mid-write can never leave a truncated/corrupted target
    file this way — the rename is the only step that touches *path* itself,
    and it's atomic at the filesystem level. Raises OSError on failure;
    callers that want "best effort, never raises" (as some existing stores
    do) should wrap the call in their own try/except — that policy varied
    per module before this helper existed and isn't this function's call to
    make.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # TypeError/ValueError come from json.dump choking on non-serializable
        # data (e.g. an agent's freeform state dict) — still clean up the tmp
        # file either way before re-raising, same as an OSError mid-write.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
