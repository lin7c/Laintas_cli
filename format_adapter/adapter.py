"""Code formatting — shared formatter selection (pure, no exec).

Single source of truth = ``registry.json`` (sibling). Dependency-free, vendorable.
It only PICKS the right in-place formatter for a file (the first one whose binary
is installed); the product runs it after a full-file write and re-reads the
result. Mirrors ``diagnostics/adapter.py``.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import List, Optional

_REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry.json")

_cache: Optional[dict] = None


def load(path: str = _REGISTRY_PATH) -> dict:
    global _cache
    if _cache is None:
        with open(path, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def reload(path: str = _REGISTRY_PATH) -> dict:
    global _cache
    _cache = None
    return load(path)


def pick_formatter(file_path: str, registry: Optional[dict] = None) -> Optional[List[str]]:
    """Return the in-place formatter command (``{file}`` substituted) of the
    first formatter registered for the extension whose binary is on PATH, else
    None (the caller then leaves the file as-is)."""
    reg = registry or load()
    ext = os.path.splitext(file_path)[1].lower()
    for entry in reg.get("formatters", {}).get(ext, []):
        binary = entry.get("bin")
        if binary and shutil.which(binary):
            return [tok.replace("{file}", file_path) for tok in entry.get("cmd", [])]
    return None


def timeout_seconds(registry: Optional[dict] = None) -> int:
    return int((registry or load()).get("timeout_seconds", 15))
