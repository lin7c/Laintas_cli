"""Host services shared by terminal and embedded agent runtimes.

The agent engine depends on this interface rather than importing a user
interface entry point. Hosts bind once before running any agent work.
"""

from __future__ import annotations

import threading
from typing import Any

_host: Any = None
_lock = threading.RLock()


def bind(host: Any) -> None:
    if host is None:
        raise ValueError("runtime host must not be None")
    global _host
    with _lock:
        _host = host


def current() -> Any:
    with _lock:
        if _host is None:
            raise RuntimeError("agent runtime host has not been bound")
        return _host


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(current(), name)
