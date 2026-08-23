"""Python adapter for the shared context-compaction policy.

Single source of truth = ``policy.json`` (sibling file). Dependency-free
(stdlib only) so it can be vendored verbatim into any Python agent product
(the gateway backend, laintas_cli, future agents) — mirrors the pattern of
``tools/adapter.py``.

It provides the BUDGET ARITHMETIC every product needs (usable window, recent
budget, overflow check, token estimate) so the numbers never drift. It does NOT
do compaction — each product implements that against its own message/history
structure using these helpers + ``summary_prompt``.

Derived from opencode (``packages/opencode/src/session/overflow.ts`` +
``compaction.ts``): ``usable = window - max(maxOutput, buffer)``;
``keep_recent = clamp(usable*ratio, min, max)``; overflow when token count >=
usable. Token estimate ~= chars/4 (opencode ``Token.estimate``).
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Optional

_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.json")

_cache: Optional[dict] = None


def load(path: str = _POLICY_PATH) -> dict:
    """Load (and cache) the policy dict."""
    global _cache
    if _cache is None:
        with open(path, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def reload(path: str = _POLICY_PATH) -> dict:
    global _cache
    _cache = None
    return load(path)


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """CJK-aware conservative token estimate.

    Accepts any value; non-str is JSON-serialized first so callers can pass a
    messages array directly.
    """
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(text)
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    latin_count = len(text) - cjk_count
    return math.ceil(latin_count / 4 + cjk_count / 2)


def usable_tokens(window: int, max_output: int, policy: Optional[dict] = None) -> int:
    """Budget the assembled prompt must fit under: ``window - max(max_output, buffer)``."""
    p = policy or load()
    if not window or window <= 0:
        return 0
    reserved = max(int(max_output or 0), int(p.get("buffer_tokens", 20000)))
    return max(0, int(window) - reserved)


def keep_recent_tokens(usable: int, policy: Optional[dict] = None) -> int:
    """Token budget for the verbatim recent tail: clamp(usable*ratio, min, max)."""
    p = policy or load()
    pinned = p.get("keep_recent_tokens")
    lo = int(p.get("keep_recent_min", 2000))
    hi = int(p.get("keep_recent_max", 8000))
    if pinned is not None and usable <= 0:
        return int(pinned)
    ratio = float(p.get("keep_recent_ratio", 0.25))
    val = int(usable * ratio) if usable > 0 else int(pinned or hi)
    return max(lo, min(hi, val))


def is_overflow(tokens: int, window: int, max_output: int, policy: Optional[dict] = None) -> bool:
    """True when ``tokens`` (real provider count or estimate) >= usable window."""
    p = policy or load()
    if not p.get("auto", True):
        return False
    if not window or window <= 0:
        return False
    return int(tokens) >= usable_tokens(window, max_output, p)


def is_protected_tool(name: str, policy: Optional[dict] = None) -> bool:
    """Whether a tool's output is protected from pruning."""
    p = policy or load()
    return name in set(p.get("prune_protected_tools", []))


def truncate_tool_output(text: str, policy: Optional[dict] = None) -> str:
    """Truncate a tool output to ``tool_output_max_chars`` with a marker."""
    p = policy or load()
    cap = int(p.get("tool_output_max_chars", 2000))
    if not isinstance(text, str) or len(text) <= cap:
        return text
    omitted = len(text) - cap
    return f"{text[:cap]}\n[truncated {omitted} chars for compaction]"


# ---- file-read retention (re-read amnesia avoidance) ----
def read_retention(policy: Optional[dict] = None) -> dict:
    """The ``file_read_retention`` config block (with safe defaults)."""
    p = policy or load()
    return p.get("file_read_retention", {}) or {}


def is_read_tool(name: str, policy: Optional[dict] = None) -> bool:
    """Whether ``name`` is a file-content read tool."""
    return name in set(read_retention(policy).get("read_tools", []))


def is_edit_tool(name: str, policy: Optional[dict] = None) -> bool:
    """Whether ``name`` is a tool that mutates a file (invalidates its cache)."""
    return name in set(read_retention(policy).get("edit_tools", []))


def repeat_stop(policy: Optional[dict] = None) -> int:
    """How many identical (tool+args) calls before a re-read loop is hard-stopped."""
    return int(read_retention(policy).get("repeat_stop", 4) or 4)
