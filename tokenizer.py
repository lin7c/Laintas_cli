"""Fast, model-aware token estimation with tiktoken fallback.

Provides `_count(text, model=None)` for accurate token counts.
When tiktoken is unavailable, falls back to a calibrated chars/4 heuristic
that accounts for CJK density better than naive len(text)//4.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------
# Tiktoken encoder cache
# ---------------------------------------------------------------------------

_TIKTOKEN_AVAILABLE: Optional[bool] = None


def _tiktoken_ok() -> bool:
    global _TIKTOKEN_AVAILABLE
    if _TIKTOKEN_AVAILABLE is None:
        try:
            import tiktoken  # noqa: F401
            _TIKTOKEN_AVAILABLE = True
        except Exception:
            _TIKTOKEN_AVAILABLE = False
    return _TIKTOKEN_AVAILABLE


# Model-name substring → tiktoken encoding name.
# Order matters: more specific first.
_ENCODING_MAP = [
    ("gpt-4o", "o200k_base"),
    ("gpt-4", "cl100k_base"),
    ("gpt-3.5", "cl100k_base"),
    ("text-embedding-3", "cl100k_base"),
    ("text-embedding-ada", "cl100k_base"),
    ("claude", "cl100k_base"),      # Anthropic uses ~cl100k-like tokenisation
    ("gemini", "cl100k_base"),      # Approximation; no public tokenizer
]


def _guess_encoding(model: Optional[str]) -> str:
    """Return best-effort tiktoken encoding name for a model string."""
    m = (model or "").lower()
    for needle, enc in _ENCODING_MAP:
        if needle in m:
            return enc
    return "cl100k_base"  # safe default for modern models


@lru_cache(maxsize=8)
def _get_encoder(encoding_name: str):
    import tiktoken
    return tiktoken.get_encoding(encoding_name)


def _encoder_for_model(model: Optional[str]):
    if not _tiktoken_ok():
        return None
    try:
        return _get_encoder(_guess_encoding(model))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def count_tokens(text: str, model: Optional[str] = None) -> int:
    """Return token count for *text*.

    If tiktoken is installed and the model is recognised, use the real
    tokenizer.  Otherwise fall back to a calibrated heuristic that weights
    CJK characters more heavily (~1 token per char) and ASCII ~0.25.
    """
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    enc = _encoder_for_model(model)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # --- calibrated fallback ---
    # CJK / high-Unicode: ~1 token per char
    # ASCII / Latin-1:    ~0.25 token per char
    total = 0
    for ch in text:
        cp = ord(ch)
        if cp <= 0x7F:
            total += 0.25
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            total += 1.0
        elif 0xAC00 <= cp <= 0xD7AF:          # Hangul
            total += 1.0
        elif 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:
            total += 1.0
        elif cp <= 0x7FF:
            total += 0.5
        else:
            total += 0.75
    return max(1, int(total))


def count_messages(messages: list, model: Optional[str] = None) -> int:
    """Count tokens in a list of OpenAI-style message dicts.

    Serialises to JSON (the wire format the model actually sees) and
    counts that text.  Adds a small per-message overhead constant to
    approximate the role/name tokens that tiktoken’s chat format would
    add.
    """
    if not messages:
        return 0
    try:
        blob = json.dumps(messages, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(messages)
    # per-message overhead (~4 tokens for role + formatting)
    overhead = len(messages) * 4
    return count_tokens(blob, model=model) + overhead
