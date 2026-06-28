"""Post-edit diagnostics — shared checker selection (pure, no exec).

Single source of truth = ``registry.json`` (sibling). Dependency-free so it can
be vendored into any Python agent product. It only PICKS the right checker for a
file (the first one whose binary is installed); the product actually runs it and
surfaces findings. This is the agent-appropriate slice of LSP: after an edit,
run a fast native checker and tell the model if it introduced an error — without
a full LSP/JSON-RPC server.
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


def pick_checker(file_path: str, registry: Optional[dict] = None) -> Optional[List[str]]:
    """Return the command (with ``{file}`` substituted) of the first checker
    registered for the file's extension whose binary is on PATH, or None."""
    reg = registry or load()
    ext = os.path.splitext(file_path)[1].lower()
    for entry in reg.get("checkers", {}).get(ext, []):
        binary = entry.get("bin")
        if binary and shutil.which(binary):
            return [tok.replace("{file}", file_path) for tok in entry.get("cmd", [])]
    return None


def timeout_seconds(registry: Optional[dict] = None) -> int:
    return int((registry or load()).get("timeout_seconds", 10))


def max_output_chars(registry: Optional[dict] = None) -> int:
    return int((registry or load()).get("max_output_chars", 2000))
