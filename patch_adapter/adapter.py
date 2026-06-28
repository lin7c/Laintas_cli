"""Fault-tolerant single-edit ("apply_patch") — shared pure logic.

A pure, dependency-free port of opencode's edit replacer strategies
(packages/opencode/src/tool/edit.ts). Given a file's content + an ``old_string``
to replace, it tries progressively fuzzier matchers so an edit still lands when
the model's ``old_string`` differs from the file only in whitespace or
indentation — the #1 cause of exact-edit failures.

This is the ONE edit-related piece that is pure algorithm (no I/O, no runtime),
so it lives in the gateway as the single source of truth and is vendored into
each product (laintas_cli now; a TS mirror for Helpwo later). The file read/write
stays in each client; only the matching is shared.

Strategies tried in order (first UNIQUE match wins), mirroring opencode:
  1. exact            2. line-trimmed         3. whitespace-normalized
  4. indentation-flexible                     5. block-anchor (first/last line
     anchor + Levenshtein similarity for 3+ line blocks)
"""
from __future__ import annotations

from typing import Iterator, Optional, Tuple

_SINGLE_SIM_THRESHOLD = 0.65
_MULTI_SIM_THRESHOLD = 0.65


# ── similarity ─────────────────────────────────────────────────────────────
def _levenshtein(a: str, b: str) -> int:
    if a == "" or b == "":
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]


# ── replacers: each yields candidate substrings of ``content`` ─────────────
def _simple(content: str, find: str) -> Iterator[str]:
    yield find


def _line_trimmed(content: str, find: str) -> Iterator[str]:
    original = content.split("\n")
    search = find.split("\n")
    if search and search[-1] == "":
        search.pop()
    if not search:
        return
    for i in range(0, len(original) - len(search) + 1):
        if all(original[i + j].strip() == search[j].strip() for j in range(len(search))):
            start = sum(len(original[k]) + 1 for k in range(i))
            end = start
            for k in range(len(search)):
                end += len(original[i + k])
                if k < len(search) - 1:
                    end += 1
            yield content[start:end]


def _whitespace_normalized(content: str, find: str) -> Iterator[str]:
    import re
    norm = lambda t: re.sub(r"\s+", " ", t).strip()
    nf = norm(find)
    for line in content.split("\n"):
        if norm(line) == nf:
            yield line
    # multi-line block normalization
    fl = find.split("\n")
    if len(fl) > 1:
        cl = content.split("\n")
        for i in range(0, len(cl) - len(fl) + 1):
            block = "\n".join(cl[i:i + len(fl)])
            if norm(block) == nf:
                yield block


def _indentation_flexible(content: str, find: str) -> Iterator[str]:
    def strip_indent(text: str) -> str:
        lines = text.split("\n")
        non_empty = [ln for ln in lines if ln.strip()]
        if not non_empty:
            return text
        min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
        return "\n".join(ln if not ln.strip() else ln[min_indent:] for ln in lines)

    nf = strip_indent(find)
    cl = content.split("\n")
    fl = find.split("\n")
    for i in range(0, len(cl) - len(fl) + 1):
        block = "\n".join(cl[i:i + len(fl)])
        if strip_indent(block) == nf:
            yield block


def _block_anchor(content: str, find: str) -> Iterator[str]:
    original = content.split("\n")
    search = find.split("\n")
    if len(search) < 3:
        return
    if search[-1] == "":
        search.pop()
    if len(search) < 3:
        return
    first = search[0].strip()
    last = search[-1].strip()
    block_size = len(search)
    max_delta = max(1, block_size // 4)

    candidates = []
    for i in range(len(original)):
        if original[i].strip() != first:
            continue
        for j in range(i + 2, len(original)):
            if original[j].strip() == last:
                if abs((j - i + 1) - block_size) <= max_delta:
                    candidates.append((i, j))
                break
    if not candidates:
        return

    def _emit(start_line: int, end_line: int) -> str:
        start = sum(len(original[k]) + 1 for k in range(start_line))
        end = start
        for k in range(start_line, end_line + 1):
            end += len(original[k])
            if k < end_line:
                end += 1
        return content[start:end]

    def _similarity(start_line: int, end_line: int) -> float:
        actual = end_line - start_line + 1
        n = min(block_size - 2, actual - 2)
        if n <= 0:
            return 1.0
        sim = 0.0
        for j in range(1, min(block_size - 1, actual - 1)):
            o = original[start_line + j].strip()
            s = search[j].strip()
            m = max(len(o), len(s))
            if m == 0:
                continue
            sim += 1 - _levenshtein(o, s) / m
        return sim / n

    if len(candidates) == 1:
        s, e = candidates[0]
        if _similarity(s, e) >= _SINGLE_SIM_THRESHOLD:
            yield _emit(s, e)
        return
    best, best_sim = None, -1.0
    for s, e in candidates:
        sim = _similarity(s, e)
        if sim > best_sim:
            best_sim, best = sim, (s, e)
    if best and best_sim >= _MULTI_SIM_THRESHOLD:
        yield _emit(*best)


_REPLACERS = (
    ("exact", _simple),
    ("line-trimmed", _line_trimmed),
    ("whitespace-normalized", _whitespace_normalized),
    ("indentation-flexible", _indentation_flexible),
    ("block-anchor", _block_anchor),
)


def _disproportionate(match: str, old: str) -> bool:
    """Guard: refuse a fuzzy match far larger than the requested old_string."""
    return len(match) > max(len(old) * 3, len(old) + 200)


def apply_edit(content: str, old_string: str, new_string: str,
               replace_all: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """Replace ``old_string`` with ``new_string`` in ``content``, tolerating
    whitespace/indentation drift via opencode's replacer strategies.

    Returns ``(new_content, strategy)`` on success, or ``(None, None)`` when no
    unique, proportionate match is found (caller falls back / reports). Raises
    ``ValueError`` only for nonsensical input (empty/identical old_string).
    """
    if old_string == new_string:
        raise ValueError("oldString and newString are identical — no change.")
    if old_string == "":
        raise ValueError("oldString cannot be empty; use a write for a full rewrite.")

    for strategy, replacer in _REPLACERS:
        for search in replacer(content, old_string):
            if not search:
                continue
            idx = content.find(search)
            if idx == -1:
                continue
            if _disproportionate(search, old_string):
                continue
            if replace_all:
                return content.replace(search, new_string), strategy
            # require a UNIQUE occurrence (first == last)
            if idx != content.rfind(search):
                continue
            return content[:idx] + new_string + content[idx + len(search):], strategy
    return None, None
