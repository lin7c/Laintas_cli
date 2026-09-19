"""`.config` files: `/config` settings, one per line.

A line is exactly what follows `/config` on the command line —

    budget system share 0.05
    compact_background false
    search_engine "cn-bing duckduckgo"

— so a file can hold any setting, a leading `/config` is tolerated (a line
copied from a terminal works as is), `#` starts a comment, and values with
spaces are quoted. `/config import` applies a file, `/config export` writes
the current overrides, and the budget page (`/prop budget N output`) exports
the same format.
"""
from __future__ import annotations

import shlex
from typing import Callable

HEADER = "# laintas-cli settings: one `/config` line per setting; apply with /config import <file>"


def resolve(words: list, described: dict):
    """``(key, value)`` for one line's words, or raise ValueError.

    Budget levels are words (`budget system share 0.05`); every other key is a
    single word followed by its value, which may itself contain spaces.
    """
    if not words:
        raise ValueError("empty setting")
    if words[0].split(".")[0].lower() == "budget":
        def dotted(items):
            return ".".join(p for w in items for p in str(w).split(".") if p)
        if len(words) >= 2 and dotted(words[:-1]) in described:
            return dotted(words[:-1]), words[-1]
        if dotted(words) in described:
            raise ValueError(f"{' '.join(words)}: missing a value")
        raise ValueError(f"unknown budget level: {' '.join(words[:-1]) or words[0]}")
    key = words[0]
    if key not in described:
        raise ValueError(f"unknown setting: {key}")
    if len(words) < 2:
        raise ValueError(f"{key}: missing a value")
    return key, " ".join(words[1:])


def parse(text: str, described: dict) -> list:
    """``[(key, value, line_number)]`` for every setting. Raises ValueError
    naming the line, so a file with one bad line is rejected as a whole."""
    out = []
    for number, raw in enumerate(str(text).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError as exc:
            raise ValueError(f"line {number}: {exc}") from None
        if words and words[0].lower() == "/config":
            words = words[1:]
        if not words:
            continue
        try:
            key, value = resolve(words, described)
        except ValueError as exc:
            raise ValueError(f"line {number}: {exc}") from None
        out.append((key, value, number))
    if not out:
        raise ValueError("the file holds no settings")
    return out


def display_key(key: str) -> str:
    return key.replace(".", " ") if key.startswith("budget.") else key


def _value_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return shlex.quote(text) if (not text or any(c.isspace() for c in text)
                                 or any(c in text for c in "'\"#")) else text


def render(values: dict, *, note: str = "") -> str:
    """Lines for ``{key: value}``, in key order."""
    lines = [HEADER]
    if note:
        lines.append(f"# {note}")
    lines += [f"{display_key(key)} {_value_text(value)}" for key, value in sorted(values.items())]
    return "\n".join(lines) + "\n"


def apply(entries: list, set_value: Callable, snapshot: Callable, restore: Callable) -> None:
    """Apply every entry or none of them.

    Settings validate against each other (the compaction thresholds must stay
    ordered), so an entry rejected only because its partner has not been set
    yet is retried after the rest; anything still rejected restores the state
    taken before the first entry and raises.
    """
    saved = snapshot()
    pending = list(entries)
    try:
        while pending:
            failed = []
            for key, value, number in pending:
                try:
                    set_value(key, value)
                except (ValueError, KeyError, TypeError) as exc:
                    failed.append((key, value, number, exc))
            if not failed:
                return
            if len(failed) == len(pending):
                key, _value, number, exc = failed[0]
                message = str(exc).replace(key, display_key(key))
                raise ValueError(f"line {number} ({display_key(key)}): {message}")
            pending = [(k, v, n) for k, v, n, _e in failed]
    except Exception:
        restore(saved)
        raise
