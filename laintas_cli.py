#!/usr/bin/env python3
"""
laintas_cli — Autonomous AI agent for your terminal.
Same agent loop as Helpwo, but executes real system commands.

Usage:
    laintas-cli                    # Start interactive session in cwd
    laintas-cli --resume           # Resume saved conversation for cwd
    laintas-cli --name my-server   # Set agent name
    laintas-cli --backend URL      # Custom backend URL
"""

import asyncio
import copy
import io
import symbols
import os
import re
import sys
import json
import time
import uuid
import errno
import queue
import shlex
import shutil
import base64
import hashlib
import signal
import socket
import tempfile
import platform
import webbrowser
import threading
import warnings
# The PTY model is fork()+exec on a dedicated pair (InteractiveSession); the
# child execs a shell immediately, so CPython 3.12's multi-threaded-fork
# DeprecationWarning is noise here — concurrent Agent workers always make
# this process multi-threaded.
warnings.filterwarnings(
    "ignore",
    message=r".*use of fork\(\) may lead to deadlocks in the child.*",
    category=DeprecationWarning)
import subprocess
from concurrent.futures import ThreadPoolExecutor
import difflib
from contextlib import nullcontext, contextmanager
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

# ── OS Detection (must come before Unix-specific imports) ────────────────
SYSTEM = platform.system()  # "Linux", "Darwin"

# Capture restart identity before os.chdir() or an in-place update can alter
# what argv[0] resolves to.  A PATH launch commonly has argv[0] ==
# "laintas-cli"; joining that to the cwd creates a nonexistent file.
_LAUNCH_CWD = os.getcwd()
_LAUNCH_ARGV0 = sys.argv[0] if sys.argv else ""
_LAUNCH_SCRIPT_PATH = os.path.abspath(__file__)


def _resolve_launch_executable() -> str:
    """Resolve the executable that launched this process to an absolute path."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    if _LAUNCH_ARGV0:
        if os.path.dirname(_LAUNCH_ARGV0):
            candidates.append(os.path.abspath(
                os.path.join(_LAUNCH_CWD, _LAUNCH_ARGV0)))
        else:
            found = shutil.which(_LAUNCH_ARGV0)
            if found:
                candidates.append(found)
    candidates.append(sys.executable)
    for candidate in candidates:
        path = os.path.realpath(os.path.abspath(candidate))
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return os.path.realpath(os.path.abspath(candidates[0]))


_LAUNCH_EXECUTABLE_PATH = _resolve_launch_executable()


def _restart_process(executable: Optional[str] = None) -> None:
    """Replace this process using a validated absolute restart command.

    Frozen installs restart the replaced binary. Source/console-script installs
    restart the module with the same Python interpreter, avoiding dependence on
    a PATH shim or on argv[0] remaining valid after a cwd change.
    """
    args = list(sys.argv[1:])
    if executable or getattr(sys, "frozen", False):
        target = os.path.realpath(os.path.abspath(
            executable or _LAUNCH_EXECUTABLE_PATH))
        argv = [target, *args]
    else:
        target = os.path.realpath(os.path.abspath(sys.executable))
        script = os.path.realpath(_LAUNCH_SCRIPT_PATH)
        if not os.path.isfile(script):
            raise FileNotFoundError(
                f"restart script does not exist: {script}")
        argv = [target, script, *args]
    if not os.path.isfile(target):
        raise FileNotFoundError(f"restart executable does not exist: {target}")
    if not os.access(target, os.X_OK):
        raise PermissionError(f"restart executable is not executable: {target}")
    os.execv(target, argv)

import pty
import select
import fcntl
import termios
import tty

import requests
from rich.console import Console, Group
from rich.panel import Panel
from rich.padding import Padding
from rich.markdown import Markdown as RichMarkdown
from rich.table import Table as RichTable
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.theme import Theme
from rich.style import Style as RichStyle
from rich.markup import escape
from rich.syntax import SyntaxTheme
from rich import box


class InlineSection:
    """Copy-friendly replacement for bordered Rich panels.

    Claude-style terminal output keeps headings and content in normal
    scrollback. Box borders are visually noisy and become unwanted text when
    users copy a login URL, approval details, or diagnostic output.
    """

    def __init__(self, renderable="", *, title=None, **_kwargs):
        self.renderable = renderable
        self.title = str(title) if title else ""

    def __rich_console__(self, _console, _options):
        if self.title:
            try:
                title = Text.from_markup(self.title)
                title.stylize("bold accent")
            except Exception:
                title = Text(self.title, style="bold accent")
            yield title
        if self.title and self.renderable:
            yield Text("")
        if isinstance(self.renderable, str):
            try:
                yield Text.from_markup(self.renderable)
            except Exception:
                yield Text(self.renderable)
        elif self.renderable:
            yield self.renderable


# Keep existing call sites and extension compatibility while making every
# former Panel copy-friendly. Constructor options such as border_style and
# expand remain accepted and intentionally ignored by InlineSection.
Panel = InlineSection


def Table(*args, **kwargs):
    """Create copy-friendly, borderless tabular output."""
    kwargs["box"] = None
    kwargs["show_lines"] = False
    kwargs.setdefault("show_edge", False)
    kwargs.setdefault("pad_edge", False)
    return RichTable(*args, **kwargs)

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.filters import Condition

# ── Central UI theme ──────────────────────────────────────────────────
# Minimal palette: one accent, muted secondaries, semantic status colors.
# Use these names instead of inline literals so the whole UI restyles here.
LAINTAS_THEME = Theme({
    "accent":   "#3fb950",          # primary action / active state
    "accent.dim": "#2ea043",
    "success":  "#4ade80",
    "error":    "bold #f85149",
    "warning":  "#e3b341",
    "foreground": "#e6edf3",
    "muted":    "#8b949e",
    "subtle":   "#6e7681",
    "surface":  "on #161b22",
    "selected": "bold #f0f6fc on #21262d",
    "agent":    "#a78bfa",          # agent / orchestration (soft violet, kept distinct)
    "path":     "#c9d1d9",
    "glyph":    "#3fb950",
    "rule":     "#30363d",
    # Rich's default Markdown inline-code style is cyan on black. Keep all
    # ordinary/model text on the terminal's default background instead.
    "markdown.code": "white",
    "markdown.code_block": "white",
})


class _PlainWhiteSyntaxTheme(SyntaxTheme):
    """Code blocks use plain white text and inherit the terminal background."""

    def get_style_for_token(self, token_type) -> RichStyle:
        return RichStyle(color="white")

    def get_background_style(self) -> RichStyle:
        return RichStyle()


class _PlatedWhiteSyntaxTheme(_PlainWhiteSyntaxTheme):
    """Same white tokens, but the whole block sits on a dark plate."""

    def get_background_style(self) -> RichStyle:
        return RichStyle(bgcolor="#161b22")


_PLAIN_WHITE_SYNTAX = _PlainWhiteSyntaxTheme()
_PLATED_WHITE_SYNTAX = _PlatedWhiteSyntaxTheme()


class LaintasMarkdown(RichMarkdown):
    """Markdown renderer with no baked-in black/blue code treatment.

    Fenced code blocks render on a dark plate (#161b22, same as the
    ``surface`` semantic color); inline code stays on the terminal background.
    """

    def __init__(self, markup: str, **kwargs):
        kwargs.setdefault("code_theme", _PLATED_WHITE_SYNTAX)
        kwargs.setdefault("inline_code_theme", _PLAIN_WHITE_SYNTAX)
        kwargs.setdefault("style", "white")
        super().__init__(markup, **kwargs)


# Keep the existing dependency-injection surface (`deps.Markdown`) stable.
Markdown = LaintasMarkdown


def _no_color_requested() -> bool:
    """Honor the conventional NO_COLOR environment flag, even when empty."""
    return "NO_COLOR" in os.environ


console = Console(
    theme=LAINTAS_THEME,
    style="foreground",
    no_color=_no_color_requested(),
)

# ── /agents mirror: the shared console tees every chunk into the current
# Agent's ANSI scrollback so the full-screen view can show the real REPL
# output. Ownership of stdout switches in _enter/_exit_agents_view.
import repl_mirror


def _mirror_target_agent_id() -> str:
    try:
        from agent_loop import get_current_agent as _gca
        _agent = _gca()
        return _agent.id if _agent is not None else "primary"
    except Exception:
        return "primary"


console.file = repl_mirror.TeeFile(_mirror_target_agent_id)

# Built-in Markdown palettes for model output. Keys map to Rich style names
# (h1 -> markdown.h1, bold -> markdown.strong, italic -> markdown.em,
# quote -> markdown.block_quote; headings h1..h6 are looked up by
# rich.markdown.Heading). An empty string means "no style override" (keep the
# Rich/terminal default). The `default` palette preserves the pre-existing
# plain-white behavior.
_MARKDOWN_THEMES: dict[str, dict[str, str]] = {
    "default": {
        "h1": "", "h2": "", "h3": "", "h4": "", "h5": "", "h6": "",
        "code": "white", "code_block": "white", "link": "",
        "bold": "", "italic": "", "quote": "",
    },
    "green-red": {
        "h1": "bold #4ade80", "h2": "bold #22c55e",      # green titles
        "h3": "bold #f85149", "h4": "bold #f85149",       # red sub-titles
        "h5": "#f85149", "h6": "#f85149",
        "code": "white", "code_block": "white", "link": "",
        "bold": "", "italic": "", "quote": "",
    },
}

# Palette key -> Rich style name. Must match the keys used above.
_MARKDOWN_RICH_KEYS: dict[str, str] = {
    "h1": "markdown.h1", "h2": "markdown.h2", "h3": "markdown.h3",
    "h4": "markdown.h4", "h5": "markdown.h5", "h6": "markdown.h6",
    "code": "markdown.code", "code_block": "markdown.code_block",
    "link": "markdown.link", "bold": "markdown.strong",
    "italic": "markdown.em", "quote": "markdown.block_quote",
}

_MARKDOWN_STYLE_KEYS = tuple(_MARKDOWN_RICH_KEYS)

# Style keys every palette must define (fill with "" when unused). A palette
# that misses one of these keys silently falls back to `default` instead of
# half-rendering headings, so a broken preset can never degrade output.
_MARKDOWN_REQUIRED_KEYS = frozenset(_MARKDOWN_STYLE_KEYS)

for _preset_name, _preset in _MARKDOWN_THEMES.items():
    if not _MARKDOWN_REQUIRED_KEYS.issubset(_preset):
        raise RuntimeError(
            f"markdown theme {_preset_name!r} is missing keys: "
            f"{sorted(_MARKDOWN_REQUIRED_KEYS - set(_preset))}")

# Semantic palette variants applied on top of the shared Console.
_THEME_VARIANTS = {
    "dark": LAINTAS_THEME,
    "light": Theme({
        "accent": "#176f2c", "accent.dim": "#238636", "success": "#116329",
        "error": "bold #cf222e", "warning": "#9a6700", "foreground": "#24292f",
        "muted": "#57606a", "subtle": "#6e7781", "surface": "on #f6f8fa",
        "selected": "bold #24292f on #d0d7de", "agent": "#8250df",
        "path": "#24292f", "glyph": "#176f2c", "rule": "#d0d7de",
        "markdown.code": "#24292f", "markdown.code_block": "#24292f",
    }),
    "mono": Theme({
        "accent": "bold", "accent.dim": "dim", "success": "bold",
        "error": "bold reverse", "warning": "bold", "foreground": "",
        "muted": "dim", "subtle": "dim", "surface": "", "selected": "reverse",
        "agent": "bold", "path": "", "glyph": "bold", "rule": "dim",
        "markdown.code": "", "markdown.code_block": "",
    }),
}
def _load_markdown_palette(name: str) -> dict[str, str]:
    """Resolve a markdown_theme value to a complete {key: style} palette.

    `default`/built-in presets are returned as-is. `custom` merges the global
    ~/.laintas/markdown_theme.json overrides onto the default palette. Any
    failure (missing/invalid file, bad style value) falls back to default so
    model output can never be degraded by a broken theme file.
    """
    base = dict(_MARKDOWN_THEMES["default"])
    normalized = str(name or "default").strip().lower()
    if normalized in _MARKDOWN_THEMES:
        base.update(_MARKDOWN_THEMES[normalized])
        return base
    if normalized != "custom":
        return base

    path = paths.LAINTAS_HOME / "markdown_theme.json"
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")
        from rich.style import Style as _RichStyle
        invalid = []
        for key, value in data.items():
            if key not in _MARKDOWN_REQUIRED_KEYS:
                continue  # ignore unknown keys; forward-compatible
            text = str(value)
            try:
                _RichStyle.parse(text)  # raises on invalid style syntax
            except Exception:
                invalid.append(key)
                continue  # skip just this key, keep the valid ones
            base[key] = text
        if invalid:
            console.print(
                f"[yellow]markdown_theme.json: ignored invalid style(s) for "
                f"{', '.join(sorted(invalid))}.[/yellow]")
    except FileNotFoundError:
        pass  # custom selected but no file yet: default palette is fine
    except Exception as exc:
        console.print(
            f"[yellow]markdown_theme.json ignored ({exc}); using default "
            "Markdown palette.[/yellow]")
    return base


def _markdown_theme_styles(palette: dict[str, str]) -> dict[str, str]:
    """Map palette keys to Rich style names (h1 -> markdown.h1, ...)."""
    styles = {}
    for key in _MARKDOWN_STYLE_KEYS:
        value = palette.get(key, "")
        if value:
            styles[_MARKDOWN_RICH_KEYS[key]] = value
    return styles


_console_theme_pushed = False


def _apply_ui_theme(name: str) -> str:
    """Apply a semantic Rich theme without replacing the shared Console."""
    global _console_theme_pushed
    normalized = str(name or "dark").strip().lower()
    if normalized not in _THEME_VARIANTS:
        raise ValueError("theme expects dark, light, or mono")
    # Merge the semantic palette with the active Markdown palette so one
    # push_theme call covers both; markdown_theme="default" contributes no
    # heading overrides and preserves the historical plain-white rendering.
    palette = _load_markdown_palette(get_runtime_config("markdown_theme"))
    merged = Theme({
        **_THEME_VARIANTS[normalized].styles,
        **_markdown_theme_styles(palette),
    })
    if _console_theme_pushed:
        try:
            console.pop_theme()
        except Exception:
            pass
    console.push_theme(merged, inherit=False)
    _console_theme_pushed = True
    return normalized


def _ptk_fragments(pairs):
    """Expand a list of (base_style, text) pairs into prompt_toolkit fragments.

    ``text`` may embed Rich markup (``[bold]``, ``[green]``, ``[dim]`` …).
    prompt_toolkit's ``FormattedTextControl`` does not understand Rich markup —
    left as-is it prints literally (e.g. a raw ``[bold][/bold]``). This resolves
    the markup via Rich (honoring ``LAINTAS_THEME``) and emits proper
    prompt_toolkit ``(style_str, text)`` fragments, folding each ``base_style``
    (e.g. ``"class:selected"``) onto every produced segment.
    """
    from rich.text import Text as _RichText

    out = []
    for base_style, text in pairs:
        base = base_style or ""
        if "[" not in text:
            out.append((base, text))
            continue
        try:
            rt = _RichText.from_markup(text)
        except Exception:
            out.append((base, text))
            continue
        for seg in rt.render(console):
            parts = []
            if base:
                parts.append(base)
            st = seg.style
            if st is not None:
                if st.bold:
                    parts.append("bold")
                if st.dim:
                    parts.append("dim")
                if st.italic:
                    parts.append("italic")
                if st.underline:
                    parts.append("underline")
                if st.reverse:
                    parts.append("reverse")
                if st.color is not None:
                    try:
                        parts.append("#" + st.color.get_truecolor().hex.lstrip("#"))
                    except Exception:
                        pass
                if st.bgcolor is not None:
                    try:
                        parts.append("bg:#" + st.bgcolor.get_truecolor().hex.lstrip("#"))
                    except Exception:
                        pass
            out.append((" ".join(parts), seg.text))
    return out


# ── Reusable selection dialog ──────────────────────────────────────────
# A single component covering single-select, multi-select, inline (non-full-
# screen), full-screen, fuzzy-search, and action-key variants.  Supersedes
# the ~40-line scaffold duplicated across the 7 hand-rolled pickers.


def _fuzzy_match(text: str, pattern: str) -> bool:
    """Return True if all chars of pattern appear in text in order (fuzzy match)."""
    it = iter(text)
    return all(c in it for c in pattern)


@contextmanager
def _alt_screen():
    """Enter the alternate screen buffer for inline content between two
    full-screen ``select_dialog`` calls.

    Without this, printing between picker invocations flashes the normal
    screen (REPL scrollback) for a frame before the next picker re-enters
    the alternate screen.  Wrapping the content in ``\\x1b[?1049h/l`` keeps
    the terminal in the alternate buffer the whole time.
    """
    _file = getattr(console, "file", None) or sys.stdout
    try:
        _file.write("\x1b[?1049h")
        _file.flush()
        yield
    finally:
        _file.write("\x1b[?1049l")
        _file.flush()


def _clear_stale_running_loop() -> None:
    """Clear a stale asyncio running-loop flag left on this thread.

    prompt_toolkit's ``Application.run()`` calls ``asyncio.run()`` which
    internally calls ``loop.run_forever()``.  ``run_forever()`` sets the
    running-loop flag in a ``try`` and clears it in a ``finally`` block.
    If a SIGINT (Ctrl+C) lands inside that ``finally`` block - between the
    Python-level ``except`` handling and the ``events._set_running_loop(None)``
    call - the flag is never cleared.  The next ``asyncio.run()`` on the same
    thread then fails with::

        RuntimeError: asyncio.run() cannot be called from a running event loop

    This is a no-op when no stale flag is present; it only clears the flag
    when a previous ``app.run()`` was interrupted by SIGINT.
    """
    try:
        if asyncio.events._get_running_loop() is not None:
            asyncio.events._set_running_loop(None)
    except Exception:
        pass


def select_dialog(
    items,
    *,
    title: str = "",
    multi: bool = False,
    full_screen: bool = True,
    selected_index: int = 0,
    checked=None,
    search: bool = False,
    hint: str = "",
    action_keys=None,
    enter_action: str = "",
    letter_shortcuts: bool = False,
    refresh_interval: float = 0.05,
    auto_confirm_seconds: Optional[float] = None,
    auto_confirm_index: Optional[int] = None,
):
    """Interactive arrow-key selector — single or multi-select.

    Parameters
    ----------
    items : list[str] | list[tuple[str, str]]
        Options to choose from.  Each entry is either a bare label or a
        ``(label, description)`` tuple; both are rendered with Rich markup.
    title : str
        Bold header line (rendered with Rich markup).
    multi : bool
        ``False`` (default) → single-select: Enter returns the chosen item.
        ``True`` → multi-select: Space toggles a checkbox, Enter returns the
        list of checked items.
    full_screen : bool
        ``True`` (default) → takes over the alternate screen buffer with
        terminal-height-aware pagination.  ``False`` → inline non-full-screen
        renderer (for approval gates that keep body content in scrollback).
    selected_index : int
        Initial highlighted row.
    checked : set[int] | None
        (multi only) Pre-checked row indices.
    search : bool
        Show a fuzzy-filter input box above the list (typed text filters
        items by subsequence match on the label).
    hint : str
        Footer hint line.  Auto-derived from mode when empty.
    action_keys : dict[str, str] | None
        Map of ``key → action_name``.  When a key is pressed the dialog exits
        returning ``(action_name, absolute_index)`` instead of an item.  The
        caller can loop and re-invoke for multi-step pickers (like the resume
        picker's d/x keys).
    enter_action : str
        When non-empty (and ``action_keys`` is also set), Enter returns
        ``(enter_action, idx)`` instead of the raw item.  Useful for pickers
        where Enter triggers an action (e.g. "resume", "toggle") rather than
        returning a value.
    letter_shortcuts : bool
        When True, pressing a letter jumps to the first option whose label
        starts with that letter and confirms (muscle-memory compat for
        y/n/a approval gates).  Ignored in multi mode.
    refresh_interval : float
        Application refresh interval in seconds.
    auto_confirm_seconds : float | None
        When set, automatically confirm after this delay unless the user makes
        a selection or cancels first.
    auto_confirm_index : int | None
        Row confirmed by the timer. Defaults to the initially selected row.

    Returns
    -------
    Single-select → the chosen item (str or tuple) or ``None`` (cancelled).
    Multi-select  → list of checked items, or ``None`` (cancelled).
    action_keys   → ``(action_name, absolute_index)`` or ``(None, -1)``.
    """
    if not items:
        return None

    # Clear stray typeahead and flush terminal input buffer so that keys
    # left over from a previous prompt_toolkit session (e.g. the REPL
    # PromptSession, or Enter pressed during the agent loop) don't get
    # replayed here and auto-cancel the dialog before the user can react.
    try:
        from prompt_toolkit.input.typeahead import clear_typeahead
        from prompt_toolkit.application.current import get_app_session
        clear_typeahead(get_app_session().input)
    except Exception:
        pass
    try:
        import termios as _termios
        _termios.tcflush(sys.stdin.fileno(), _termios.TCIFLUSH)
    except (OSError, ValueError, io.UnsupportedOperation, _termios.error):
        pass

    # ── Normalise items into (label, desc) pairs ──────────────────
    norm: list[tuple[str, str]] = []
    for it in items:
        if isinstance(it, (tuple, list)):
            norm.append((str(it[0]), str(it[1]) if len(it) > 1 else ""))
        else:
            norm.append((str(it), ""))

    sel = [max(0, min(selected_index, len(norm) - 1))]
    _auto_seconds = (float(auto_confirm_seconds)
                     if auto_confirm_seconds is not None else None)
    _auto_deadline = (time.monotonic() + max(0.0, _auto_seconds)
                      if _auto_seconds is not None else None)
    _auto_index = max(0, min(
        selected_index if auto_confirm_index is None else auto_confirm_index,
        len(norm) - 1,
    ))
    chk: set[int] = set(checked) if (multi and checked) else set()
    filter_buf = Buffer() if search else None
    act_keys: dict[str, str] = action_keys or {}

    def _visible():
        """Return (list_of_(orig_idx, label, desc)) after filtering."""
        unfiltered = [(oi, lab, desc) for oi, (lab, desc) in enumerate(norm)]
        if not filter_buf:
            return unfiltered
        f = filter_buf.text.strip().lower()
        if not f:
            return unfiltered
        out = []
        for oi, (lab, _desc) in enumerate(norm):
            plain = re.sub(r"\[/?[^\]]+\]", "", lab).lower()
            if _fuzzy_match(plain, f):
                out.append((oi, lab, _desc))
        return out

    def _clamp_sel():
        vis = _visible()
        if not vis:
            sel[0] = 0
            return vis
        if sel[0] >= vis[-1][0]:
            sel[0] = vis[-1][0]
        if sel[0] < vis[0][0]:
            sel[0] = vis[0][0]
        return vis

    def _build_lines():
        lines = []
        if title:
            lines.append(("bold #e6edf3", f"{title}\n"))

        vis = _clamp_sel()

        # ── Pagination (full-screen only) ──
        start, end = 0, len(vis)
        if full_screen and vis:
            import shutil
            term_h = shutil.get_terminal_size().lines
            # Reserve lines for title, search, header/footer hints
            reserve = (3 if title else 0) + (3 if search else 0) + 3
            list_h = max(4, term_h - reserve)
            if len(vis) > list_h:
                cur_pos = next((i for i, (oi, _, _) in enumerate(vis)
                                if oi == sel[0]), 0)
                half = list_h // 2
                start = max(0, min(cur_pos - half, len(vis) - list_h))
                end = min(start + list_h, len(vis))

        if start > 0:
            lines.append(("class:muted", f"  {symbols.ARROW_U} {start} more\n"))

        if not vis:
            lines.append(("class:muted", "  No matches\n"))

        for vi in range(start, end):
            oi, lab, desc = vis[vi]
            is_sel = (oi == sel[0])
            prefix = "›" if is_sel else " "
            if multi:
                mark = "☑" if oi in chk else "☐"
                row_text = f" {prefix} {mark}  {lab}"
            else:
                row_text = f" {prefix} {lab}"
            if desc:
                row_text += f"  [dim]{desc}[/dim]"
            style = "class:selected" if is_sel else ""
            lines.append((style, row_text + "\n"))

        if end < len(vis):
            lines.append(("class:muted", f"  {symbols.ARROW_D} {len(vis) - end} more\n"))

        # ── Footer hint ──
        lines.append(("", "\n"))
        if not hint:
            if multi:
                parts = ["Space toggle", "Enter confirm", "Esc cancel"]
            else:
                parts = ["Enter select", "Esc cancel"]
            if search:
                parts.insert(0, "Type to filter")
            if act_keys:
                parts.insert(0, "  ".join(f"{k}={a}" for k, a in act_keys.items()))
            lines.append((f"class:muted", "  " + f"  {symbols.BULLET}  ".join(parts)))
        else:
            lines.append(("class:muted", "  " + hint))
        if _auto_deadline is not None:
            remaining = max(0.0, _auto_deadline - time.monotonic())
            total = _auto_seconds or 1.0
            frac = max(0.0, min(1.0, remaining / total))
            bar_w = 12
            filled = int(frac * bar_w)
            bar = "▓" * filled + "░" * (bar_w - filled)
            lines.append(("class:auto-confirm", f"  {bar} {int(remaining + 0.999)}s"))
        return lines

    # ── Key bindings ──────────────────────────────────────────────
    # Second line of defense after the typeahead/tcflush purge above: bytes
    # that land between the flush and prompt_toolkit's first read (a queued
    # Enter from fast typing) would otherwise confirm the dialog before it is
    # even rendered. Affirmative keys are ignored for a short window after
    # open; cancellation and navigation always stay live.
    _opened_at = time.monotonic()

    def _in_grace() -> bool:
        return (time.monotonic() - _opened_at) < 0.25

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        vis = _visible()
        if not vis:
            return
        cur_vi = next((i for i, (oi, _, _) in enumerate(vis) if oi == sel[0]), 0)
        if cur_vi > 0:
            sel[0] = vis[cur_vi - 1][0]

    @kb.add("down")
    def _(event):
        vis = _visible()
        if not vis:
            return
        cur_vi = next((i for i, (oi, _, _) in enumerate(vis) if oi == sel[0]), 0)
        if cur_vi < len(vis) - 1:
            sel[0] = vis[cur_vi + 1][0]

    @kb.add("home")
    def _(event):
        vis = _visible()
        if vis:
            sel[0] = vis[0][0]

    @kb.add("end")
    def _(event):
        vis = _visible()
        if vis:
            sel[0] = vis[-1][0]

    if multi:
        @kb.add("space")
        def _(event):
            if sel[0] in chk:
                chk.discard(sel[0])
            else:
                chk.add(sel[0])

    @kb.add("enter")
    def _(event):
        if _in_grace():
            return
        vis = _clamp_sel()
        if not vis:
            if act_keys:
                event.app.exit(result=(None, -1))
            else:
                event.app.exit(result=None)
            return
        if multi:
            checked_items = [items[oi] for oi in sorted(chk)
                             if oi < len(items)]
            event.app.exit(result=checked_items)
        elif enter_action and act_keys:
            event.app.exit(result=(enter_action, sel[0]))
        else:
            event.app.exit(result=items[sel[0]])

    # Action keys (e.g. d=details, x=delete in resume picker)
    for _key, _action in list(act_keys.items()):

        @kb.add(_key)
        def _ak(event, _a=_action):
            if _in_grace():
                return
            vis = _clamp_sel()
            if vis and 0 <= sel[0] < len(items):
                event.app.exit(result=(_a, sel[0]))
            else:
                event.app.exit(result=(_a, -1))

    # Letter shortcuts (y/n/a for approval gates)
    if letter_shortcuts and not multi:
        # Collect the first letter of each label (lowercased).
        _first_letters = set()
        for lab, _desc in norm:
            fl = lab.strip()[:1].lower()
            if fl:
                _first_letters.add(fl)

        for _letter in sorted(_first_letters):

            @kb.add(_letter)
            @kb.add(_letter.upper())
            def _lk(event, _l=_letter):
                if _in_grace():
                    return
                for i, (lab, _desc) in enumerate(norm):
                    if lab.strip()[:1].lower() == _l:
                        sel[0] = i
                        event.app.exit(result=items[i])
                        return

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _(event):
        # Cancellation is never subject to the startup grace period.  A user
        # who sees a blank or half-rendered selector must always have an
        # immediate escape hatch.  Only affirmative actions (Enter, shortcut
        # selection) are protected from replayed typeahead above.
        if multi:
            event.app.exit(result=None)
        elif act_keys:
            event.app.exit(result=(None, -1))
        else:
            event.app.exit(result=None)

    # ── Layout ────────────────────────────────────────────────────
    layout_panes = []
    if search:
        layout_panes.append(Window(
            content=FormattedTextControl([("class:search-label", "  Filter")]),
            height=1,
        ))
        layout_panes.append(Window(
            content=BufferControl(buffer=filter_buf),
            height=1,
            style="class:search-input",
        ))
    list_ctrl = FormattedTextControl(lambda: _ptk_fragments(_build_lines()))
    if full_screen:
        layout_panes.append(Window(content=list_ctrl))
    else:
        # Rows + optional title + spacer + footer. The old ``height=len(items)``
        # clipped the hint and sometimes the final choices, making inline
        # selectors look like a plain, incomplete box.
        inline_height = len(norm) + 2 + (1 if title else 0)
        layout_panes.append(Window(
            content=list_ctrl,
            always_hide_cursor=True,
            height=max(3, inline_height),
        ))

    layout = Layout(HSplit(layout_panes))
    style = Style.from_dict({
        "selected": "bold #f0f6fc bg:#21262d",
        "option": "#e6edf3",
        "muted": "#8b949e",
        "search-label": "bold #8b949e",
        "search-input": "#e6edf3 bg:#161b22",
        "auto-confirm": "#e3b341",
    })

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=full_screen,
        refresh_interval=refresh_interval,
    )
    async def _auto_confirm():
        await asyncio.sleep(max(0.0, _auto_seconds or 0.0))
        if app.is_done:
            return
        sel[0] = _auto_index
        if multi:
            result = [items[oi] for oi in sorted(chk) if oi < len(items)]
        elif enter_action and act_keys:
            result = (enter_action, sel[0])
        else:
            result = items[sel[0]]
        app.exit(result=result)

    def _pre_run():
        if _auto_seconds is not None:
            app.create_background_task(_auto_confirm())

    # A selector is a cancellable UI component.  prompt_toolkit can still
    # surface KeyboardInterrupt when the terminal is interrupted while the
    # application is starting/stopping; convert that into the same ordinary
    # cancel result as Esc/q instead of leaking a traceback to the user.
    try:
        result = app.run(pre_run=_pre_run)
        _clear_stale_running_loop()
        return result
    except (KeyboardInterrupt, EOFError):
        _clear_stale_running_loop()
        return (None, -1) if act_keys else None


def choose_record(records, *, title: str, label: Callable,
                  description: Optional[Callable] = None,
                  selected_index: int = 0, search: bool = False,
                  full_screen: bool = True):
    """Select and return one record while keeping rendering/mapping centralized."""
    records = list(records or [])
    if not records or not sys.stdin.isatty():
        return None
    rows = [
        (str(label(record)),
         str(description(record)) if description is not None else "")
        for record in records
    ]
    chosen = select_dialog(
        rows,
        title=title,
        selected_index=max(0, min(selected_index, len(rows) - 1)),
        search=search,
        full_screen=full_screen,
        hint=(f"Type to filter  {symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ select  Esc/q cancel"
              if search else f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ select  Esc/q cancel"),
    )
    if chosen is None:
        return None
    return records[rows.index(chosen)]


try:
    from version import __version__
except Exception:
    __version__ = "0.0.0"

# ── Agent Loop (extracted module) ─────────────────────────────────────
from agent_loop import (
    DebugEntry, TerminalInfo, AgentInfo, EmployeeProfile, AgentToolPolicy,
    add_debug_log, clear_debug_logs, get_recent_tool_failures,
    next_debug_loop, get_debug_logs,
    run_agent_loop, LoopDeps,
    register_terminal, unregister_terminal,
    get_terminal, get_all_terminals, close_all_terminals,
    rename_terminal,
    register_agent, unregister_agent,
    get_agent, get_all_agents, get_current_agent,
    begin_primary_run, finish_primary_run, queue_primary_message,
    agent_deployment_terminal, agent_scope_terminal,
    set_terminal_model_selection,
    get_pool_agents, get_deployed_agents, get_or_hire_pool_agent,
    start_agent_assignment,
    switch_to_agent, set_current_agent_id,
    rename_agent, station_agent, unstation_agent,
    close_all_agents,
    spawn_subagent, send_to_agent, recv_from_inbox, drain_inbox,
    abort_agent, wait_for_agent, build_agents_tree,
    get_runtime_config, set_runtime_config,
    list_runtime_config, describe_runtime_config,
    reset_runtime_config, apply_max_config,
    prepare_state_for_repl, session_context_status, compact_session_context,
    get_user_interrupt_event, get_user_message_queue,
    clear_loop_command_cache,
    stop_trigger_scanner,
    set_trigger_wake_callback,
    save_session_snapshot,
    save_resume_state, load_resume_state, save_resume_checkpoint, list_resume_states,
    delete_resume_state,
)

import tools as tools_mod    # noqa: E402 — load after agent_loop so registry inits once
import skills as skills_mod  # noqa: E402
import task_manager          # noqa: E402 — resume blob rehydrates the task plan
import paths                 # Centralized path management
import terminal_preferences  # durable choices isolated to this logical terminal
import migrate as migrate_mod  # Auto-migration from old layout
import hwo_ui as hwo_ui_mod  # /hwo orchestration UI
import browser_session as browser_mod  # headless-browser live-view stack
import session_store             # durable live current-session state
import event_log                 # prompt admission + interrupted-run recovery
import prompt_lab                # project-scoped prompt diagnosis/testing branches
import evolution_lab             # project-scoped feature/extension evolution
import extension_runtime         # hot-loaded project extension runtime
import workgraph                 # unified objective/plan/steps/workflow state
import hooks as hooks_mod        # trusted Python hooks + argv hooks
import backend_profiles          # backend trust domains + credential isolation
import trust_store               # workspace trust for executable customization
import usage_tracker             # local AI token/cost accounting (/usage)
import agent_ui_events           # observable events for full-screen Agents Mode
import mode_manager              # declarative user-selectable agent modes
import auto_pilot                # heuristic task classification + hint injection

# MCP client: lazy import (saves ~1.8s on startup)
_mcp_mod = None
def _get_mcp_mod():
    global _mcp_mod
    if _mcp_mod is None:
        import mcp_client as _m
        _mcp_mod = _m
    return _mcp_mod

# ── ANSI escape sequence stripping ─────────────────────────────────────
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-~]|\x1b[()][AB12]|\x0d')
def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences and carriage returns from text."""
    return _ANSI_RE.sub('', text).replace('\r', '')

# ── Configuration ──────────────────────────────────────────────────────
def _detect_backend() -> str:
    """Auto-detect the Helpwo backend URL. Prefers local if running."""
    local = "http://127.0.0.1:2913"
    try:
        r = requests.get(f"{local}/api/agents", timeout=1.5)
        if r.status_code in (200, 401):  # 401 = endpoint exists, just needs auth
            return local
    except requests.RequestException:
        pass
    return "https://laintas.com"

BACKEND_URL = os.environ.get("LAINTAS_BACKEND") or "https://laintas.com"
# Authentication origins are fixed audiences. Backend customization must never
# redirect account cookies, passwords, or OAuth codes to another host.
LAINTAS_BASE = "https://laintas.com"
ACCOUNTS_BASE = "https://accounts.laintas.com"
SESSION_FILE = paths.SESSION_FILE
CONFIG_FILE = paths.CONFIG_FILE
HEARTBEAT_INTERVAL = 30


def get_backend_profile() -> backend_profiles.BackendProfile:
    """Resolve the active backend without ever promoting custom URLs to official."""
    try:
        return backend_profiles.resolve(BACKEND_URL)
    except ValueError:
        # Fail closed to the exact official origin; never guess that a malformed
        # custom URL is entitled to receive the official session.
        return backend_profiles.BackendProfile(
            "safe-fallback", "official", "https://laintas.com")


def get_backend_url() -> str:
    return get_backend_profile().base_url

# ── PTY-based Command Execution ──────────────────────────────────────────
# Commands are executed inside a pseudo-terminal so they get a real TTY:
# colors, progress bars, and interactive programs work correctly.
# Output is streamed to the real terminal AND captured for the agent loop.

def execute_command_pty(command: str, timeout: int = 120) -> dict:
    """Execute a shell command in a PTY. Output streams to terminal AND is captured.

    Returns {stdout, stderr, returncode, success}.
    """
    session = InteractiveSession(command, timeout=timeout, stream_output=True)
    try:
        return session.run_to_completion()
    finally:
        session.close()


def pty_passthrough(command: str, timeout: int = 120) -> dict:
    """Run a command with full terminal passthrough.

    Forks and execs the command so the child directly inherits the real
    terminal — no new PTY is created. This is identical to running the
    command from bash: interactive apps (codex, claude, vim, etc.) get the
    same terminal environment the user sees.

    Returns {stdout, stderr, returncode, success}.
    """
    fd = sys.stdin.fileno()
    old_tcattr = termios.tcgetattr(fd)
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigquit = signal.getsignal(signal.SIGQUIT)

    pid = os.fork()
    if pid == 0:
        # Child: restore default signal handlers and exec directly into the
        # current terminal (no new PTY — stdin/stdout/stderr are inherited).
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGQUIT, signal.SIG_DFL)
            os.execve(DEFAULT_SHELL, [DEFAULT_SHELL, "-c", command], _child_env())
        except Exception:
            pass
        os._exit(127)

    # Parent: let SIGINT reach the child's process group, just wait.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGQUIT, signal.SIG_IGN)

    returncode = -1
    try:
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            returncode = -os.WTERMSIG(status)
    except ChildProcessError:
        pass
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGQUIT, old_sigquit)
        # Restore terminal settings the child may have left in a dirty state.
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_tcattr)
        except (termios.error, OSError):
            pass

    return {
        "stdout": "",
        "stderr": "",
        "returncode": returncode,
        "success": returncode == 0,
    }




def _marker_poll_exec(session, command: str, timeout: int = 60,
                      strip_ansi_codes: bool = True) -> dict:
    """Serialize a command on a persistent terminal session."""
    lock = getattr(session, "command_lock", None)
    if lock is None:
        return _marker_poll_exec_unlocked(
            session, command, timeout, strip_ansi_codes)
    with lock:
        return _marker_poll_exec_unlocked(
            session, command, timeout, strip_ansi_codes)


def _marker_poll_exec_unlocked(session, command: str, timeout: int = 60,
                               strip_ansi_codes: bool = True) -> dict:
    """Execute a command through a persistent bash session via marker-poll.

    Returns {stdout, stderr, returncode, success}.
    Same mechanism as tools.py _bi_shell_exec marker-poll path.

    When strip_ansi_codes=False, ANSI escape sequences are preserved in stdout
    (needed for commands like `clear` whose output IS escape sequences).
    """
    if session is None or not session.is_alive():
        return {"stdout": "", "stderr": "session not alive", "returncode": -1, "success": False}

    import uuid as _uuid
    import re as _re

    marker_id = _uuid.uuid4().hex[:8]
    start_marker = f"__CMD_BEGIN_{marker_id}__"
    end_marker = f"__CMD_END_{marker_id}__"
    payload = tools_mod.shell_payload_for_pty(
        command, noninteractive=True, token=marker_id,
        agent_automation=False)
    wrapped = f"echo {start_marker}; {payload} 2>&1; __laintas_rc=$?; echo {end_marker}:$__laintas_rc"

    _output_total = getattr(session, "output_total", None)
    if isinstance(_output_total, int):
        old_len = _output_total
    else:
        try:
            old_len = len(session.raw_output)
        except AttributeError:
            old_len = len(session.full_output)

    if getattr(session, "_laintas_shell_dirty", None) is True:
        if not tools_mod.recover_stuck_shell(session):
            return {"stdout": "", "returncode": -1, "success": False,
                    "stderr": ("terminal is stuck: a previous command is "
                               "still running or an interactive program is "
                               "holding the shell")}
    session.send_keys(wrapped + "\n")
    poll_start = time.time()
    cmd_output = ""
    returncode = -1
    new_content = ""

    while time.time() - poll_start < timeout:
        time.sleep(0.08)
        session.read_output(timeout=0.1)
        output_from_fn = getattr(session, "output_from", None)
        if (isinstance(_output_total, int)
                and callable(output_from_fn)):
            new_content = (
                output_from_fn(old_len) if old_len > 0
                else output_from_fn(0))
        else:
            try:
                raw = session.raw_output
            except AttributeError:
                raw = session.full_output
            new_content = raw[old_len:] if old_len > 0 else raw

        end_match = _re.search(rf'{_re.escape(end_marker)}:(\d+)', new_content)
        if end_match:
            returncode = int(end_match.group(1))
            starts = list(_re.finditer(
                rf'{_re.escape(start_marker)}(?=[\r\n]|$)', new_content))
            if starts:
                # Take the FIRST start marker before end, not the last -
                # command output begins right after the first marker; taking
                # the last one truncates any output between first and last.
                valid = [m for m in starts if m.end() < end_match.start()]
                chosen = valid[0] if valid else starts[0]
                body_start = chosen.end()
                while body_start < len(new_content) and new_content[body_start] in '\r\n':
                    body_start += 1
                cmd_output = new_content[body_start:end_match.start()]
                cmd_output = cmd_output.rstrip('\r\n').strip()
            else:
                parts = new_content.rsplit(start_marker, 1)
                if len(parts) > 1:
                    tail = parts[1].split(end_marker, 1)[0]
                    cmd_output = tools_mod.scrub_marker_noise(
                        tail.strip('\r\n')).strip()
            break
        if not session.is_alive():
            cmd_output = tools_mod.scrub_marker_noise(new_content)
            break

    stderr_note = ""
    if returncode == -1 and session.is_alive():
        # The command never signalled completion (pager, prompt, or genuinely
        # long-running). Reclaim the shell so the next command doesn't type
        # into the stuck program and time out too.
        if tools_mod.recover_stuck_shell(session):
            stderr_note = (f"command timed out ({timeout}s); "
                           "foreground process stopped, terminal recovered")
        else:
            stderr_note = (f"command timed out ({timeout}s); "
                           "terminal still busy")
    elif returncode != -1:
        try:
            session._laintas_shell_dirty = False
        except Exception:
            pass

    if returncode == -1 and not cmd_output:
        cmd_output = tools_mod.scrub_marker_noise(new_content)

    if strip_ansi_codes:
        cmd_output = strip_ansi(cmd_output)
    return {
        "stdout": cmd_output,
        "stderr": stderr_note,
        "returncode": returncode,
        "success": returncode == 0,
    }


def _quick_pwd(session, timeout: float = 2.0) -> str:
    """Run pwd via marker-poll and return the result."""
    if session is None or not session.is_alive():
        return os.getcwd()
    try:
        result = _marker_poll_exec(session, "pwd", timeout=int(timeout))
        output = result.get("stdout", "").strip()
        return output if output else os.getcwd()
    except Exception:
        return os.getcwd()


def _sync_cwd_from_term0(session) -> None:
    """Sync the parent process CWD to match term0's bash session CWD."""
    if session is None or not session.is_alive():
        return
    try:
        term0_cwd = _quick_pwd(session)
        if term0_cwd and os.path.isdir(term0_cwd) and term0_cwd != os.getcwd():
            os.chdir(term0_cwd)
    except Exception:
        pass


def _ensure_term0_alive() -> None:
    """Health-check term0's bash session and replace dead/dirty PTYs in place."""
    term0_info = get_terminal("term0")
    old_session = term0_info.session if term0_info is not None else None
    try:
        alive = bool(old_session and old_session.is_alive())
    except Exception:
        alive = False
    dirty = bool(getattr(old_session, "_laintas_shell_dirty", False))
    if alive and not dirty:
        return
    restart_cwd = str(
        getattr(old_session, "_laintas_last_cwd", "") or os.getcwd())
    if not os.path.isdir(restart_cwd):
        restart_cwd = os.getcwd()
    replacement = None
    try:
        replacement = InteractiveSession(
            DEFAULT_SHELL, timeout=0, stream_output=False,
            persistent=True, cwd=restart_cwd)
        replacement.start()
        time.sleep(0.08)
        if replacement.is_alive():
            replacement.read_output(timeout=0.1)
        if not replacement.is_alive():
            raise RuntimeError("replacement term0 exited during startup")
        if term0_info is None:
            register_terminal(replacement, DEFAULT_SHELL, 0, name="term0")
        else:
            # Preserve terminal ownership/dialog/model metadata. Unregistering
            # term0 would cascade into its deployed primary agent.
            term0_info.session = replacement
            term0_info.command = DEFAULT_SHELL
            term0_info.completed_at = None
            term0_info.returncode = None
        if old_session is not None:
            try:
                old_session.close()
            except Exception:
                pass
    except Exception:
        if replacement is not None:
            try:
                replacement.close()
            except Exception:
                pass


# ── Interactive-terminal whitelist ──────────────────────────────────────
# Commands whose first word is in this set need a real PTY (pty_passthrough)
# instead of term0/marker-poll, because they take over the whole screen and
# need raw keystrokes (vim, claude) or block waiting on a live remote tty
# (ssh). Everything else routes through term0 so cd/export/pushd persist
# across commands (term0's bash state IS the parent shell's state).
_DEFAULT_INTERACTIVE_COMMANDS = {
    "vim", "vi", "nano", "pico", "emacs",
    "less", "more", "man",
    "htop", "top",
    "python", "python3", "ipython", "node", "irb", "ruby",
    "mysql", "psql", "sqlite3",
    "ssh", "telnet",
    "tmux", "screen", "mc",
    "claude", "codex",
}

_interactive_commands_cache: Optional[set] = None
_interactive_commands_mtime: float = 0.0


def get_interactive_commands() -> set:
    """Merge built-in defaults with user add/remove overrides.

    Persisted in ~/.laintas/interactive_commands.json, mtime-cached so /bash
    add/remove takes effect immediately without restarting — same pattern as
    policy.py/hooks.py.
    """
    global _interactive_commands_cache, _interactive_commands_mtime
    path = paths.INTERACTIVE_COMMANDS_FILE
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    if _interactive_commands_cache is not None and mtime == _interactive_commands_mtime:
        return _interactive_commands_cache

    added, removed = [], []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            added = data.get("add", [])
            removed = data.get("remove", [])
        except (OSError, json.JSONDecodeError):
            pass

    result = (set(_DEFAULT_INTERACTIVE_COMMANDS) | set(added)) - set(removed)
    _interactive_commands_cache = result
    _interactive_commands_mtime = mtime
    return result


def _modify_interactive_commands(command: str, add: bool) -> None:
    """Persist a /bash add|remove override to ~/.laintas/interactive_commands.json."""
    path = paths.INTERACTIVE_COMMANDS_FILE
    data = {"add": [], "remove": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    data.setdefault("add", [])
    data.setdefault("remove", [])
    if add:
        data["remove"] = [c for c in data["remove"] if c != command]
        if command not in data["add"] and command not in _DEFAULT_INTERACTIVE_COMMANDS:
            data["add"].append(command)
    else:
        data["add"] = [c for c in data["add"] if c != command]
        if command not in data["remove"] and command in _DEFAULT_INTERACTIVE_COMMANDS:
            data["remove"].append(command)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _pane_snapshot_delta(previous: str, current: str) -> str:
    """Return output added between two bounded tmux pane snapshots.

    tmux ``capture-pane`` returns a rolling window: once output exceeds the
    pane height the oldest lines are trimmed. A naive ``current[old_len:]``
    slice breaks because ``old_len`` (the previous snapshot length) can exceed
    the current snapshot length after scrolling, yielding empty deltas forever.

    We find the longest suffix of ``previous`` that is a prefix of ``current``
    (KMP failure-function overlap). Everything after that overlap in
    ``current`` is the true incremental delta.

    If no overlap exists (clear/redraw), return ``current`` wholesale — this
    is correct for a one-shot refresh but callers that must avoid duplicates
    should deduplicate on their own (see trigger scanner).
    """
    if not current:
        return ""
    if not previous:
        return current
    if current == previous:
        return ""
    if current.startswith(previous):
        return current[len(previous):]

    n, m = len(previous), len(current)
    # KMP prefix function on `current`
    lps = [0] * m
    k = 0
    for i in range(1, m):
        while k > 0 and current[i] != current[k]:
            k = lps[k - 1]
        if current[i] == current[k]:
            k += 1
        lps[i] = k
    # Walk `previous` through `current`'s LPS to find the longest suffix of
    # `previous` matching a prefix of `current`.
    j = 0
    for i in range(n):
        while j > 0 and previous[i] != current[j]:
            j = lps[j - 1]
        if previous[i] == current[j]:
            j += 1
            if j == m:
                # `current` is fully matched inside `previous`; no new content.
                return ""
    return current[j:]


class SubTerminalSession:
    """A sub-terminal session that runs a command in a separate terminal context.

    When inside tmux: creates a new tmux window (terminal N+1) so the command
    has full native terminal passthrough while the parent terminal continues
    running the AI loop.

    When NOT inside tmux: falls back to a background InteractiveSession (PTY).

    Implements the same interface as InteractiveSession so it can be used
    interchangeably in the AI loop (start, send_keys, read_output, is_alive,
    close, full_output, returncode, command).
    """

    def __init__(self, command: str, timeout: int = 120):
        self.command = command
        self.timeout = timeout
        self._use_tmux = "TMUX" in os.environ
        self._tmux_window: str = ""
        self._pty: Optional[InteractiveSession] = None
        self._alive: bool = False
        self._output_buf: list[str] = []
        self._start_time: float = 0.0
        self._returncode: int = -1
        self._tmux_exit_marker = f"__LAINTAS_EXIT_{uuid.uuid4().hex}__"
        # tmux pane snapshots are bounded (rolling). A naive length-diff delta
        # breaks once output scrolls past the pane height. We track the last
        # raw snapshot and compute true increments via suffix/prefix overlap,
        # accumulating into _accumulated_output so full_output/output_total/
        # output_from stay correct regardless of pane scrolling.
        self._last_pane_snapshot: str = ""
        self._accumulated_output: str = ""
        # Commands routed through one shell must be atomic. Without this,
        # marker-poll executions from multiple agents can interleave.
        self.command_lock = threading.RLock()

    # ── start (non-blocking) ─────────────────────────────────~~~~~~~~~

    def start(self) -> None:
        """Start the command in a sub-terminal. Non-blocking."""
        if self._alive:
            return
        self._start_time = time.time()

        if self._use_tmux:
            self._tmux_window = f"laintas-{os.getpid()}-{uuid.uuid4().hex[:6]}"
            # -d: don't switch to the new window (terminal 0 stays active)
            try:
                # Gate the real command until remain-on-exit is configured.
                # Setting it after launching the command races with fast jobs;
                # setting it from inside the pane can target the wrong window
                # on some tmux versions. The parent therefore creates a pane
                # blocked on one input line, configures that exact window, then
                # releases it with Enter.
                wrapped = (
                    "read -r __laintas_start_gate; "
                    f"{shlex.quote(DEFAULT_SHELL)} -c {shlex.quote(self.command)}; "
                    "__laintas_rc=$?; "
                    f"printf '\\n{self._tmux_exit_marker}:%s\\n' \"$__laintas_rc\"; "
                    "exit \"$__laintas_rc\""
                )
                result = subprocess.run(
                    ["tmux", "new-window", "-d", "-n", self._tmux_window,
                     f"{shlex.quote(DEFAULT_SHELL)} -c {shlex.quote(wrapped)}"],
                    capture_output=True, text=True, timeout=5,
                )
                self._alive = result.returncode == 0
                if self._alive:
                    option_result = subprocess.run(
                        ["tmux", "set-window-option", "-t", self._tmux_window,
                         "remain-on-exit", "on"],
                        capture_output=True, text=True, timeout=5,
                    )
                    release_result = subprocess.run(
                        ["tmux", "send-keys", "-t", self._tmux_window, "Enter"],
                        capture_output=True, text=True, timeout=5,
                    )
                    self._alive = (
                        option_result.returncode == 0
                        and release_result.returncode == 0
                    )
            except (OSError, subprocess.SubprocessError):
                self._alive = False
            if self._alive:
                return
            else:
                if self._tmux_window:
                    try:
                        subprocess.run(
                            ["tmux", "kill-window", "-t", self._tmux_window],
                            capture_output=True, timeout=5,
                        )
                    except (OSError, subprocess.SubprocessError):
                        pass
                self._tmux_window = ""
                self._use_tmux = False
        if not self._use_tmux:
            self._pty = InteractiveSession(self.command, timeout=self.timeout, stream_output=False)
            self._pty.start()
            self._alive = self._pty.is_alive()

    # ── output ─────────────────────────────────────────────────~~~~~~

    def _tmux_sync_snapshot(self) -> str:
        """Capture the tmux pane, compute the true delta since the last
        snapshot, append it to the accumulated buffer, and return the delta.

        tmux capture-pane returns a *bounded rolling* snapshot. Once output
        exceeds the pane height the oldest lines are trimmed, so a naive
        length-diff (old_len vs len(snapshot)) permanently returns empty.
        We instead find the longest suffix of the previous snapshot that is
        a prefix of the new snapshot (KMP), and treat everything after that
        overlap as the incremental delta.
        """
        if not self._tmux_window:
            return ""
        import subprocess as _sp
        try:
            result = _sp.run(
                ["tmux", "capture-pane", "-p", "-t", self._tmux_window, "-S", "-500"],
                capture_output=True, text=True, timeout=5,
            )
            new_output = result.stdout or ""
            self._capture_tmux_returncode(new_output)
        except Exception:
            return ""

        previous = self._last_pane_snapshot
        self._last_pane_snapshot = new_output

        if not new_output:
            return ""
        if not previous:
            delta = new_output
        elif new_output == previous:
            return ""
        elif new_output.startswith(previous):
            delta = new_output[len(previous):]
        else:
            # Rolling snapshot: find longest suffix of `previous` that is a
            # prefix of `new_output` (KMP overlap).
            delta = _pane_snapshot_delta(previous, new_output)

        if delta:
            self._output_buf.append(delta)
            self._accumulated_output += delta
        return self._clean_tmux_markers(delta)

    def read_output(self, timeout: float = 0.3) -> str:
        """Read current output. For tmux: captures pane content."""
        if self._use_tmux:
            return self._tmux_sync_snapshot()
        else:
            if self._pty:
                return self._pty.read_output(timeout=timeout)
            return ""

    # ── stdin ─────────────────────────────────────────────────~~~~~~

    def send_keys(self, text: str) -> None:
        """Send keystrokes to the sub-terminal."""
        if not self._alive:
            return
        decoded = _decode_send_keys(text)
        if self._use_tmux:
            try:
                # Normalise CRLF -> LF first, then standalone CR -> LF, so
                # that "\r\n" does not become two newlines.  Both CR and LF
                # represent the Enter key in terminal context.
                normalized = decoded.replace('\r\n', '\n').replace('\r', '\n')
                # Split on newlines.  Enter is sent *before* each line
                # (except the first), so a trailing "\n" produces exactly
                # one Enter and a string with no trailing "\n" sends none.
                # Empty lines still get Enter, preserving heredoc semantics.
                lines = normalized.split('\n')
                for i, line in enumerate(lines):
                    if i > 0:
                        subprocess.run(
                            ["tmux", "send-keys", "-t", self._tmux_window,
                             "Enter"],
                            capture_output=True, timeout=5,
                        )
                    if line:
                        subprocess.run(
                            ["tmux", "send-keys", "-t", self._tmux_window,
                             "-l", line],
                            capture_output=True, timeout=5,
                        )
            except (OSError, subprocess.SubprocessError):
                self._alive = False
        else:
            if self._pty:
                self._pty.send_keys(text)

    # ── liveness ─────────────────────────────────────────────────~~~

    def is_alive(self) -> bool:
        """Check if the sub-terminal process is still running."""
        if not self._alive:
            return False
        if self._use_tmux:
            import subprocess as _sp
            try:
                result = _sp.run(
                    ["tmux", "display-message", "-p", "-t", self._tmux_window,
                     "#{pane_dead} #{pane_dead_status}"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    self._alive = False
                    return False
                fields = (result.stdout or "").strip().split()
                dead = bool(fields and fields[0] == "1")
                if dead:
                    self._alive = False
                    try:
                        captured = _sp.run(
                            ["tmux", "capture-pane", "-p", "-t", self._tmux_window,
                             "-S", "-2000"],
                            capture_output=True, text=True, timeout=5,
                        ).stdout or ""
                        self._capture_tmux_returncode(captured)
                    except Exception:
                        pass
                    if len(fields) > 1:
                        try:
                            if self._returncode < 0:
                                self._returncode = int(fields[1])
                        except ValueError:
                            pass
                    return False
                return True
            except Exception:
                return False
        else:
            if self._pty:
                alive = self._pty.is_alive()
                if not alive:
                    self._alive = False
                return alive
            return False

    # ── cleanup ─────────────────────────────────────────────────~~~

    def close(self) -> None:
        """Close the sub-terminal."""
        if self._use_tmux and self._tmux_window:
            try:
                subprocess.run(
                    ["tmux", "kill-window", "-t", self._tmux_window],
                    capture_output=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            self._tmux_window = ""
        if self._pty:
            self._pty.close()
            self._pty = None
        self._alive = False

    # ── accumulated state ──────────────────────────────────────────

    @property
    def full_output(self) -> str:
        """Full accumulated output."""
        if self._use_tmux:
            # Sync any pane changes since the last read, then return the
            # accumulated buffer. This is scroll-safe: the accumulated buffer
            # only grows and never loses content to pane trimming.
            self._tmux_sync_snapshot()
            return self._clean_tmux_markers(self._accumulated_output)
        else:
            if self._pty:
                return self._pty.full_output
            return "".join(self._output_buf)

    @property
    def raw_output(self) -> str:
        """Full accumulated output including ANSI escape codes."""
        if self._use_tmux:
            self._tmux_sync_snapshot()
            return self._accumulated_output
        else:
            if self._pty:
                return self._pty.raw_output
            return "".join(self._output_buf)

    @property
    def output_total(self) -> int:
        """Cheap total output length. Delegates to underlying PTY when present.

        For the tmux path, the accumulated buffer grows monotonically so its
        length is a stable cursor reference (unlike the old rolling-snapshot
        length which shrank when content scrolled out of the pane).
        """
        if not self._use_tmux and self._pty is not None:
            return self._pty.output_total
        return len(self._accumulated_output)

    def output_from(self, offset: int) -> str:
        """Return accumulated output starting at character `offset`.

        Delegates to the underlying ``InteractiveSession`` for the non-tmux
        path (cheap O(delta) walk).  For tmux, the accumulated buffer is
        scroll-safe and monotonic so the offset never goes stale.
        """
        if not self._use_tmux and self._pty is not None:
            return self._pty.output_from(offset)
        self._tmux_sync_snapshot()
        raw = self._accumulated_output
        if offset <= 0:
            return raw
        if offset >= len(raw):
            return ""
        return raw[offset:]

    @property
    def master_fd(self) -> int:
        """PTY master file descriptor for raw I/O (non-tmux only)."""
        if self._pty:
            return self._pty.master_fd
        return -1

    def _capture_tmux_returncode(self, output: str) -> None:
        if not output or self._returncode >= 0:
            return
        match = re.search(
            rf"{re.escape(self._tmux_exit_marker)}:(\d+)", output)
        if match:
            self._returncode = int(match.group(1))

    def _clean_tmux_markers(self, output: str) -> str:
        if not output:
            return output
        return re.sub(
            rf"(?:\r?\n)?{re.escape(self._tmux_exit_marker)}:\d+\r?\n?",
            "\n", output,
        ).rstrip("\n")

    @property
    def returncode(self) -> int:
        """Return code. -1 while running."""
        if self._pty:
            return self._pty.returncode
        if self._use_tmux and self._alive:
            self.is_alive()
        return self._returncode


def _build_connected_subterminal_cmd(terminal_name: str,
                                     remote_parent_id: Optional[str] = None,
                                     auto_connect: bool = False,
                                     parent_terminal: str = "term0") -> str:
    """Command line for a user-facing sub-terminal running a nested CLI.

    Carries the terminal's identity (name + remote parent agent id) so that
    running /connect inside it can hand exactly this terminal to Helpwo.
    auto_connect=True (used by Helpwo's term-new) registers at startup.
    """
    terminal_id = paths.child_terminal_id(
        terminal_name, parent_terminal or "term0")
    parts = [f"LAINTAS_TERMINAL_ID={shlex.quote(terminal_id)}",
             shlex.quote(sys.executable),
             shlex.quote(os.path.abspath(__file__)),
             "--depth", "1",
             "--terminal-name", shlex.quote(terminal_name),
             "--parent-terminal", shlex.quote(parent_terminal or "term0")]
    if remote_parent_id:
        parts += ["--remote-parent-id", shlex.quote(remote_parent_id)]
    if auto_connect:
        parts.append("--connect")
    return " ".join(parts)


def connect_terminal_to_helpwo(agent_registry: "AgentRegistry", session: dict,
                               quiet: bool = False, name: str = None,
                               workspace: str = None) -> bool:
    """CLI side of the two-end handshake with Helpwo.

    Works in BOTH terminals: at depth 0 it links the primary CLI itself
    (Helpwo needs the linked primary before it can create sub-terminals from
    its UI); at depth ≥ 1 it hands this sub-terminal over. `name` optionally
    sets a custom display/terminal name (kept for internal/sub-terminal callers;
    the user-facing custom name now lives in /name). `workspace` is the absolute
    folder to SHARE as Helpwo's remote workspace — bare /connect passes None, so
    linking alone shares nothing; Helpwo only goes remote once a folder is set.
    Starts heartbeat + message poll so Helpwo can chat / term-new / term-close.
    """
    if (get_backend_profile().sends_laintas_credentials
            and not session.get("userId")):
        if not quiet:
            console.print("[red]/helpwo requires login. Run /login first.[/red]")
        return False
    is_sub = agent_registry.depth > 0
    meta = agent_registry.terminal_meta if is_sub else None
    current = (meta or {}).get("name") if is_sub else agent_registry.agent_name

    if agent_registry.agent_id:
        name_same = (not name or name == current)
        workspace_changed = workspace is not None and workspace != agent_registry.workspace_path
        if name_same and not workspace_changed:
            if not quiet:
                shared = agent_registry.workspace_path
                console.print(Panel(
                    f"[green]Already connected to Helpwo[/green]\n"
                    f"{'Terminal' if is_sub else 'Runtime environment'}: [bold]{current}[/bold]\n"
                    f"Agent ID: {agent_registry.agent_id}\n"
                    f"Workspace: [bold]{shared or os.getcwd()}[/bold]\n\n"
                    f"[dim]/name renames; /helpwo stop withdraws this environment.[/dim]",
                    title="Connected", border_style="green",
                ))
            return True
        # Name or shared workspace changed — reconnect to carry the new values
        # (previousAgentId resurrects the same agentId, so Helpwo tabs survive).
        agent_registry.unregister()
        agent_registry.agent_id = None
        agent_registry.agent_secret = ""

    if workspace is not None:
        agent_registry.workspace_path = workspace
    if name and is_sub and meta is not None:
        meta["name"] = name
    reg_name = name or ((meta or {}).get("name") if is_sub else None)
    ok = agent_registry.register(session, name=reg_name, quiet=True)
    if not ok:
        if not quiet:
            console.print("[red]Could not reach the Helpwo backend — not connected.[/red]")
        return False
    agent_registry.start_heartbeat()
    agent_registry.start_message_poll(
        agent_registry._state_cb or (lambda: {}),
        agent_registry._chat_cb or (lambda: []),
    )
    if not quiet:
        if is_sub:
            console.print(Panel(
                f"[green]Sub-terminal handed over to Helpwo[/green]\n"
                f"Terminal: [bold]{(meta or {}).get('name', agent_registry.agent_name)}[/bold]\n"
                f"Created by: {(meta or {}).get('createdBy', 'term0')}\n"
                f"Agent ID: {agent_registry.agent_id}\n"
                f"Remote terminal: [bold green]available in Helpwo[/bold green]\n\n"
                f"[dim]Helpwo can now read this terminal, send it input, and close it.\n"
                f"Run /helpwo stop to withdraw it.[/dim]",
                title="Connected", border_style="green",
            ))
        else:
            shared = agent_registry.workspace_path or os.getcwd()
            console.print(Panel(
                f"[green]Runtime environment online in Helpwo[/green]\n"
                f"Name: [bold]{agent_registry.agent_name}[/bold]\n"
                f"Agent ID: {agent_registry.agent_id}\n"
                f"Workspace: [bold]{shared}[/bold]\n"
                f"[dim]→ this CLI's terminal + this folder's files are the environment "
                f"(files ride the direct P2P channel, never the server).[/dim]\n\n"
                f"[dim]Pick it in Helpwo's terminal page. /helpwo stop to go offline.[/dim]",
                title="Connected", border_style="green",
            ))
    return True


def _helpwo_web_app_url() -> Optional[str]:
    """Return the hosted Helpwo web app URL for the current backend, or None
    if the current backend has no known companion frontend (e.g. a custom/
    local backend override). Used by /helpwo --remote to open a browser tab
    without needing a local dist build."""
    profile = get_backend_profile()
    if profile.sends_laintas_credentials:
        return "https://helpwo.laintas.com"
    return None


def _drain_fd(fd: int, chunks: list) -> None:
    """Read any remaining data from fd after child exits."""
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                break
            data = os.read(fd, 4096)
            if not data:
                break
            decoded = data.decode("utf-8", errors="replace")
            chunks.append(decoded)
            sys.stdout.write(decoded)
            sys.stdout.flush()
        except OSError:
            break


def _child_env() -> dict:
    """Build child process environment with TERM and color vars set for PTY."""
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("CLICOLOR", "1")
    env.setdefault("CLICOLOR_FORCE", "1")
    env["FORCE_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text (delegates to strip_ansi)."""
    return strip_ansi(text)


# ── Interactive Session (PTY with stdin support) ─────────────────────────

import codecs
import unicodedata


def _decode_send_keys(text: str) -> str:
    """Decode Python-style escape sequences in send_keys text.

    Converts \\n -> newline, \\t -> tab, \\r -> CR, \\x1b -> ESC, etc.
    Preserves non-ASCII characters (codecs.decode with 'unicode_escape'
    corrupts them by round-tripping through Latin-1).
    """
    result: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text):
            c = text[i + 1]
            if c == 'n':
                result.append('\n'); i += 2
            elif c == 't':
                result.append('\t'); i += 2
            elif c == 'r':
                result.append('\r'); i += 2
            elif c == '\\':
                result.append('\\'); i += 2
            elif c == 'x' and i + 3 < len(text):
                try:
                    result.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                except ValueError:
                    result.append(text[i]); i += 1
            elif c == '0':
                result.append('\0'); i += 2
            else:
                result.append(text[i]); i += 1
        else:
            result.append(text[i]); i += 1
    return ''.join(result)


_MAX_RETAINED_OUTPUT = 512 * 1024  # 512KB of recent output retained per session


class InteractiveSession:
    """Manages an interactive process running in a pseudo-terminal (PTY).

    Supports starting a process, sending keystrokes (including arrow keys
    and other ANSI escape sequences), reading output, checking liveness,
    and graceful shutdown. Also provides run_to_completion() as a
    drop-in replacement for the old execute_command_pty().
    """

    def __init__(self, command: str, timeout: int = 120, stream_output: bool = False,
                 persistent: bool = False, cwd: str = None):
        self.command = command
        self.timeout = timeout
        self.stream_output = stream_output
        self.persistent = persistent
        self.cwd = os.path.abspath(cwd) if cwd else None

        self.pid: int = -1
        self.master_fd: int = -1
        self._output_chunks: list[str] = []
        self._output_total: int = 0
        self._output_dropped: int = 0  # bytes dropped from beginning (cap eviction)
        self._returncode: int = -1
        self._start_time: float = 0.0
        self._old_tcattr = None
        self._started: bool = False
        self._closed: bool = False
        self._eof_reached: bool = False
        self.command_lock = threading.RLock()

    # ── start ─────────────────────────────────────────────────~~~~~~~~~

    def start(self) -> None:
        """Fork + exec the command in a PTY. Idempotent."""
        if self._started:
            return
        self._started = True

        # Save terminal attrs for restoration
        try:
            self._old_tcattr = termios.tcgetattr(sys.stdin.fileno())
        except (termios.error, OSError):
            self._old_tcattr = None

        master_fd, slave_fd = pty.openpty()

        # Get slave terminal attrs
        try:
            slave_attr = termios.tcgetattr(slave_fd)
        except termios.error:
            slave_attr = None

        pid = os.fork()
        if pid == 0:
            # ── Child ──
            os.close(master_fd)
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            try:
                if slave_attr:
                    termios.tcsetattr(0, termios.TCSANOW, slave_attr)
            except termios.error:
                pass
            try:
                s = termios.tcgetwinsize(sys.stdout.fileno())
                termios.tcsetwinsize(0, s)
            except (termios.error, OSError):
                pass
            if self.cwd:
                try:
                    os.chdir(self.cwd)
                except OSError:
                    os._exit(126)
            if self.persistent:
                os.execve(DEFAULT_SHELL, [DEFAULT_SHELL], _child_env())
            else:
                os.execve(DEFAULT_SHELL, [DEFAULT_SHELL, "-c", self.command], _child_env())
            os._exit(127)

        # ── Parent ──
        os.close(slave_fd)
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self.master_fd = master_fd
        self.pid = pid
        self._start_time = time.time()

    # ── stdin ──────────────────────────────────────────────────────────

    def send_keys(self, text: str) -> None:
        """Send keystrokes to the process. Supports escape sequences.

        Use \\n for Enter, \\t for Tab, \\x1b for ESC,
        \\x1b[A for Up arrow, \\x1b[B for Down, etc.
        """
        if self._closed:
            return
        if not self._started:
            self.start()
        if self.master_fd < 0:
            return
        decoded = _decode_send_keys(text)
        # Terminals send CR (0x0d) when Enter is pressed, not LF (0x0a).
        # In cooked mode the line discipline's ICRNL converts CR→LF for the
        # process; in raw mode (prompt_toolkit, vim, codex, …) the process
        # receives CR directly and expects that as the Enter key.
        decoded = decoded.replace('\n', '\r')
        try:
            os.write(self.master_fd, decoded.encode("utf-8"))
        except OSError:
            pass

    # ── output ─────────────────────────────────────────────────~~~~~~~~

    def read_output(self, timeout: float = 0.15) -> str:
        """Non-blocking read from PTY. Returns newly-read text."""
        if self._closed:
            return ""
        if not self._started:
            self.start()
        if self.master_fd < 0:
            return ""

        new_chunks: list[str] = []
        try:
            r, _, _ = select.select([self.master_fd], [], [], timeout)
        except (select.error, ValueError):
            return ""

        if not r:
            return ""

        try:
            data = os.read(self.master_fd, 4096)
        except OSError as e:
            if e.errno in (errno.EIO,):
                self._eof_reached = True
                self._reap_child()
                return ""
            if e.errno != errno.EAGAIN:
                self._eof_reached = True
                return ""
            return ""

        if data:
            decoded = data.decode("utf-8", errors="replace")
            new_chunks.append(decoded)
            self._output_chunks.append(decoded)
            self._output_total += len(decoded)
            self._trim_output_chunks()
            if self.stream_output:
                sys.stdout.write(decoded)
                sys.stdout.flush()
        else:
            self._eof_reached = True
            self._reap_child()

        self._check_child()
        return "".join(new_chunks)

    # ── lifecycle ─────────────────────────────────────────────────~~~~~

    def _check_child(self) -> None:
        """Non-blocking check if child exited. Updates _returncode."""
        if self._returncode != -1 or self.pid < 0:
            return
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
            if wpid == self.pid:
                if os.WIFEXITED(status):
                    self._returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    self._returncode = 128 + os.WTERMSIG(status)
                self._drain_remaining()
                self._eof_reached = True
        except ChildProcessError:
            self._returncode = 0
            self._eof_reached = True

    def _reap_child(self) -> None:
        """Blocking wait for child process."""
        if self._returncode != -1 or self.pid < 0 or self.master_fd < 0:
            return
        try:
            wpid, status = os.waitpid(self.pid, 0)
            if os.WIFEXITED(status):
                self._returncode = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                self._returncode = 128 + os.WTERMSIG(status)
        except (OSError, ChildProcessError):
            pass

    def _drain_remaining(self) -> None:
        """Read any leftover data from master fd after child exits."""
        if self.master_fd < 0:
            return
        while True:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
                if not r:
                    break
                data = os.read(self.master_fd, 4096)
                if not data:
                    break
                decoded = data.decode("utf-8", errors="replace")
                self._output_chunks.append(decoded)
                self._output_total += len(decoded)
                self._trim_output_chunks()
                if self.stream_output:
                    sys.stdout.write(decoded)
                    sys.stdout.flush()
            except OSError:
                break

    def is_alive(self) -> bool:
        """Check if the process is still running."""
        if self._closed or not self._started or self._eof_reached:
            return False
        self._check_child()
        return self._returncode == -1

    # ── properties ─────────────────────────────────────────────────~~~~

    def _trim_output_chunks(self) -> None:
        """Drop oldest chunks when retained output exceeds the cap.

        Keeps at most _MAX_RETAINED_OUTPUT bytes of recent output so
        long-lived persistent terminals don't leak memory. Tracks
        _output_dropped so output_from() can handle stale offsets.
        """
        retained = self._output_total - self._output_dropped
        if retained <= _MAX_RETAINED_OUTPUT:
            return
        while self._output_chunks and retained > _MAX_RETAINED_OUTPUT:
            oldest = self._output_chunks.pop(0)
            self._output_dropped += len(oldest)
            retained -= len(oldest)

    @property
    def full_output(self) -> str:
        """All accumulated output with ANSI escape codes stripped."""
        return _strip_ansi("".join(self._output_chunks))

    @property
    def raw_output(self) -> str:
        """All accumulated output including ANSI escape codes."""
        return "".join(self._output_chunks)

    @property
    def output_total(self) -> int:
        """Total length of accumulated output. Cheap (no join)."""
        return self._output_total

    def output_from(self, offset: int) -> str:
        """Return accumulated output starting at character `offset`.

        Equivalent to ``raw_output[offset:]`` but O(delta) instead of
        O(total): walks chunks from the end and only joins the ones needed
        to cover the requested tail.  Used by marker-poll hot loops that
        poll a long-lived persistent shell so per-iteration cost stays
        bounded by new output, not by session lifetime.

        If `offset` points into data that was evicted by _trim_output_chunks,
        returns everything currently retained (the caller's offset is stale).
        """
        if offset <= self._output_dropped:
            return "".join(self._output_chunks)
        if offset >= self._output_total:
            return ""
        needed = self._output_total - offset
        collected: list[str] = []
        have = 0
        for chunk in reversed(self._output_chunks):
            if have >= needed:
                break
            collected.append(chunk)
            have += len(chunk)
        collected.reverse()
        joined = "".join(collected)
        if have > needed:
            joined = joined[have - needed:]
        return joined

    @property
    def returncode(self) -> int:
        return self._returncode

    @property
    def success(self) -> bool:
        return self._returncode == 0

    # ── one-shot execution ─────────────────────────────────────────~~~~

    def run_to_completion(self, timeout: int = None) -> dict:
        """Block until the process exits. Returns {stdout, stderr, returncode, success}.

        Same return shape as the old execute_command_pty() for drop-in compatibility.
        """
        if not self._started:
            self.start()

        deadline = self._start_time + (timeout if timeout is not None else self.timeout)

        while not self._eof_reached:
            elapsed = time.time() - self._start_time
            if elapsed > (timeout if timeout is not None else self.timeout):
                try:
                    os.kill(self.pid, signal.SIGTERM)
                    time.sleep(0.3)
                    os.kill(self.pid, signal.SIGKILL)
                except OSError:
                    pass
                self._returncode = -1
                break

            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.15)
            except (select.error, ValueError):
                break

            if r:
                try:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        decoded = data.decode("utf-8", errors="replace")
                        self._output_chunks.append(decoded)
                        self._output_total += len(decoded)
                        self._trim_output_chunks()
                        if self.stream_output:
                            sys.stdout.write(decoded)
                            sys.stdout.flush()
                    else:
                        self._eof_reached = True
                except OSError as e:
                    if e.errno in (errno.EIO,):
                        self._eof_reached = True
                    elif e.errno != errno.EAGAIN:
                        self._eof_reached = True
            else:
                self._check_child()

        # Ensure child is reaped
        self._reap_child()
        self._drain_remaining()

        # Clean up fd and terminal
        if self.master_fd >= 0:
            os.close(self.master_fd)
            self.master_fd = -1
        try:
            if self._old_tcattr:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self._old_tcattr)
        except (termios.error, OSError):
            pass

        stdout = _strip_ansi("".join(self._output_chunks)).strip()
        return {
            "stdout": stdout,
            "stderr": "",
            "returncode": self._returncode,
            "success": self._returncode == 0,
        }

    # ── cleanup ─────────────────────────────────────────────────~~~~~~~

    def close(self) -> None:
        """Kill the process, reap it, close fd, restore terminal."""
        if self._closed:
            return

        if self.is_alive():
            try:
                os.kill(self.pid, signal.SIGTERM)
                time.sleep(0.3)
                if self.is_alive():
                    os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass

        self._closed = True

        if self.master_fd >= 0:
            self._reap_child()
            self._drain_remaining()
            os.close(self.master_fd)
            self.master_fd = -1

        try:
            if self._old_tcattr:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self._old_tcattr)
        except (termios.error, OSError):
            pass

    def __del__(self):
        self.close()


# ── Collapsed Output Display ─────────────────────────────────────────~~~

def _fmt_elapsed(elapsed: float) -> str:
    if elapsed <= 0:
        return ""
    if elapsed < 1:
        return f"{elapsed * 1000:.0f}ms"
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    total = int(elapsed)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m}m{s}s"
    return f"{m}m{s}s"


def _truncate_with_ellipsis(text: str, max_len: int) -> str:
    """Truncate text to max_len chars, appending '...' if truncated."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def _emit_block(title: str, status_label: str, status_style: str,
                meta: str, preview_lines: list, depth: int,
                line_style: str = "muted") -> None:
    """Render a borderless, minimal output block.

    A status-colored left bar marks the header; preview lines are dimmed and
    indented. No box - keeps the transcript clean and scannable.
    """
    pad = "  " * depth
    bar = "[%s]▍[/%s]" % (status_style, status_style)
    head = f"{pad}{bar} [bold]{_truncate_with_ellipsis(title, 80)}[/bold]"
    if status_label:
        head += f"  [{status_style}]{status_label}[/{status_style}]"
    if meta:
        head += f"  [muted]{meta}[/muted]"
    console.print(head)
    inner = pad + "  "
    for ln in preview_lines:
        console.print(f"{inner}[{line_style}]{ln}[/{line_style}]")


def display_command_output(command: str, returncode: int, output: str, depth: int = 0, elapsed: float = 0.0) -> None:
    """Display command output as a compact, borderless block.

    Shows command, exit status, elapsed time and a short preview. Full output
    is stored in agent state and viewable via /debug.
    """
    # Last-resort scrub: raw PTY captures (dead shell, timeout, wrapped-line
    # marker miss) can reach any caller — internal marker plumbing must never
    # be displayed.
    if output and "__" in output:
        output = tools_mod.scrub_marker_noise(output)
    lines = output.split("\n") if output else []
    line_count = len(lines)
    byte_count = len(output.encode("utf-8", errors="replace"))

    if returncode == 0:
        status_label, status_style = "OK", "success"
    elif returncode == -1:
        status_label, status_style = "RUNNING", "warning"
    else:
        status_label, status_style = f"EXIT {returncode}", "error"

    t = _fmt_elapsed(elapsed)
    status_label = f"{status_label} {symbols.BULLET} {t}" if t else status_label

    # Fold long output: show up to `tool_output_fold` lines. When exceeded,
    # display first half + "… N more" + last half so both the head and tail
    # stay visible. 0 = suppress preview entirely (meta-only).
    fold_limit = int(get_runtime_config("tool_output_fold") or 0)
    all_lines = [l for l in (output or "").split("\n") if l.strip()] if output else []
    if fold_limit <= 0:
        preview = []
    elif len(all_lines) <= fold_limit:
        preview = all_lines[:fold_limit]
    else:
        half = fold_limit // 2
        hidden = len(all_lines) - fold_limit
        preview = all_lines[:half] + [f"… {hidden} more lines"] + all_lines[-half:]

    if line_count == 0 and byte_count == 0:
        meta = "no output"
        preview = []
    else:
        meta = f"{line_count}L {byte_count}B {symbols.BULLET} /debug"

    _emit_block(command, status_label, status_style, meta, preview, depth)


def display_sub_terminal_preview(command: str, output: str, depth: int = 0, alive: bool = True) -> None:
    """Show a compact, borderless preview of sub-terminal output (tail)."""
    clean = strip_ansi(output) if output else ""
    all_lines = [l for l in clean.split("\n") if l.strip()] if clean else []
    total_lines = len(all_lines)

    if total_lines > 6:
        preview = all_lines[-6:]
        meta = f"running {symbols.BULLET} {total_lines}L" if alive else f"exited {symbols.BULLET} {total_lines}L"
    elif all_lines:
        preview = all_lines
        meta = "running" if alive else "exited"
    else:
        preview = []
        meta = f"running {symbols.BULLET} no output" if alive else "exited"

    status_label = "RUNNING" if alive else "EXITED"
    status_style = "warning" if alive else "muted"
    _emit_block(command, status_label, status_style, meta, preview, depth)


def _md_escape(s: str) -> str:
    """Escape Rich markup so diff/code content with [..] can't corrupt styling."""
    from rich.markup import escape as _escape
    return _escape(s)


_DIFF_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def display_file_diff(path: str, diff_text: str, depth: int = 0) -> None:
    """Framed unified-diff preview: a +adds/−dels ribbon, real line numbers in
    a gutter, and a green/red left rail so changed blocks read as shapes.

    Folds at ``tool_output_fold`` lines (default 30): first half + "… N more"
    + last half so both the opening and closing edits stay visible."""
    diff_lines = diff_text.splitlines() if diff_text else []
    adds = sum(1 for l in diff_lines
               if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in diff_lines
               if l.startswith("-") and not l.startswith("---"))

    pad = "  " * depth
    console.print(
        f"{pad}[accent]▍[/accent] [bold]{_md_escape(_truncate_with_ellipsis(path, 70))}[/bold]  "
        f"[success]+{adds}[/success] [error]−{dels}[/error]", highlight=False)

    fold_limit = int(get_runtime_config("tool_output_fold") or 30)
    # Collect content lines (skip headers/hunk markers) with their line-number
    # state so the gutter stays accurate even in the tail section.
    content = []          # (line_text, old_no, new_no)
    old_no = new_no = 0
    for ln in diff_lines:
        m = _DIFF_HUNK_RE.match(ln)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            continue
        if ln.startswith(("+++", "---", "diff ", "index ", "@@")):
            continue
        content.append((ln, old_no, new_no))
        if ln.startswith("+"):
            new_no += 1
        elif ln.startswith("-"):
            old_no += 1
        else:
            old_no += 1
            new_no += 1

    if not content:
        return

    def _print_line(ln, o_no, n_no):
        body = _md_escape(ln[:118])
        if ln.startswith("+"):
            console.print(f"{pad}[accent.dim]{n_no:>4}[/accent.dim] "
                          f"[success]┃{body}[/success]", highlight=False)
        elif ln.startswith("-"):
            console.print(f"{pad}[error]{o_no:>4} ┃{body}[/error]", highlight=False)
        else:
            text = _md_escape(ln[1:119] if ln.startswith(" ") else ln[:118])
            console.print(f"{pad}[muted]{n_no:>4}[/muted] "
                          f"[rule]│[/rule] [muted]{text}[/muted]", highlight=False)

    total = len(content)
    if fold_limit <= 0 or total <= fold_limit:
        for ln, o, n in content:
            _print_line(ln, o, n)
    else:
        half = fold_limit // 2
        hidden = total - fold_limit
        for ln, o, n in content[:half]:
            _print_line(ln, o, n)
        console.print(f"{pad}[muted]     … {hidden} more line(s) {symbols.BULLET} /debug for full[/muted]", highlight=False)
        for ln, o, n in content[-half:]:
            _print_line(ln, o, n)


# ── prompt_toolkit Input Setup ──────────────────────────────────────────

@dataclass(frozen=True)
class CompletionSpec:
    """A contextual slash-command completion and its menu description."""

    value: str
    description: str = ""


_SUBCOMMAND_HINTS = {
    "status": "Show current status",
    "list": "List available entries",
    "add": "Create a new entry",
    "show": "Show details",
    "start": "Start execution",
    "done": "Mark the task completed",
    "del": "Delete the selected entry",
    "delete": "Delete the selected entry",
    "progress": "Update completion progress",
    "note": "Append a progress note",
    "subtask": "Create a child task",
    "run": "Execute the workflow",
    "compile": "Validate and compile without executing",
    "resume": "Resume an existing run",
    "history": "Show execution history",
    "check": "Check current state",
    "update": "Apply the latest update",
    "create": "Create a new entry",
    "review": "Review the current result",
    "approve": "Approve the current result",
    "exit": "Exit the current mode",
    "clear": "Clear the current selection",
    "reset": "Restore the default setting",
    "reload": "Reload the current configuration",
    "on": "Enable this option",
    "off": "Disable this option",
}


@dataclass(frozen=True)
class CommandSpec:
    """Single source of truth for slash-command discovery and help."""

    name: str
    description: str
    group: str
    usage: str = ""
    aliases: tuple[str, ...] = ()
    palette: bool = True
    subcommands: tuple[str, ...] = ()
    help_text: str = ""
    completion_descriptions: tuple[tuple[str, str], ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def contextual_completions(self) -> tuple[CompletionSpec, ...]:
        """Return subcommands as consistently described menu entries."""
        descriptions = dict(self.completion_descriptions)
        return tuple(
            CompletionSpec(
                item,
                descriptions.get(item)
                or _SUBCOMMAND_HINTS.get(item)
                or f"{self.name} option {symbols.BULLET} {self.usage or self.description}",
            )
            for item in self.subcommands
        )


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("/help", "Show command help", "Basics", "/help [command]"),
    CommandSpec("/cwd", "Show the working directory", "Basics"),
    CommandSpec("/scan", "List user-facing PATH commands", "Basics"),
    CommandSpec("/login", "Re-authenticate with Laintas", "Account & Session"),
    CommandSpec("/usage", "Show AI usage — local token stats + Laintas backend usage", "Account & Session", "/usage [7d|30d|90d|local]", subcommands=("local",)),
    CommandSpec("/resume", "Resume a saved session (picker; echo last N events, default 20)", "Account & Session", "/resume [N|all|latest]"),
    CommandSpec("/new", "Start a new live session", "Account & Session", "/new",
                aliases=("/clear", "/new-session", "/reset-session")),
    CommandSpec("/exit", "Log out and exit", "Account & Session"),
    CommandSpec("/quit", "Exit without logging out", "Account & Session", aliases=("/q",)),
    CommandSpec("/back", "Detach from a sub-terminal", "Account & Session"),
    CommandSpec(
        "/version", "Show version or update", "Account & Session",
        "/version [check|update [--force]]", aliases=("/v", "/update"),
        subcommands=("check", "update"),
        completion_descriptions=(
            ("check", "Check whether a newer version is available"),
            ("update", "Download, install, and restart on the latest version"),
        )),
    CommandSpec("/name", "Show or set the current agent name", "Agents & Terminals", "/name [new-name]"),
    CommandSpec(
        "/hire", "Hire an undeployed employee; does not start an assignment",
        "Agents & Terminals",
        "/hire [name] [--profile role] [--prompt file] [--tools name,...|inherit] [--model [id]] [--terminal name]",
        subcommands=("--profile", "--prompt", "--tools", "--model", "--terminal"),
        help_text=(
            "Creates a persistent employee identity with its own prompt and tool policy, "
            "base model, and logical home terminal. It is not auto-deployed. Use "
            "/station <agent> --task <work> for a private temporary terminal, or "
            "/station <agent> <terminal> to deploy it explicitly."
        )),
    CommandSpec(
        "/agent", "Switch the REPL's current Agent focus without redeploying",
        "Agents & Terminals", "/agent [agent-id-or-name]",
        help_text=(
            "Switches the main REPL's current Agent conversation/execution "
            "identity in place: no terminal is moved, no deployment is "
            "reissued, and the focused Agent keeps its existing state, "
            "history, and bound terminal. With no argument, prints the "
            "current Agent and lists every other Agent you can switch to. "
            "Accepts either an Agent ID or an Agent name (case-insensitive "
            "name match). Use /agents for the full-screen multi-Agent "
            "view; /agent only changes which Agent the main REPL drives."
        )),
    CommandSpec(
        "/agents", "Open the full-screen multi-Agent focus and activity view",
        "Agents & Terminals", "/agents [agent-id|tree|--plain]",
        subcommands=("tree", "--plain"),
        help_text=(
            "Opens a dedicated Focus + Agent Rail + Event Feed interface. Passing "
            "an Agent ID selects its conversation view without changing deployment "
            "or shell ownership. Use --plain for a script-friendly snapshot. Inside "
            "Agents Mode, Enter sends to the focused Agent, @Agent performs one-shot "
            "routing, Tab opens the Agent rail, Alt+arrow switches "
            "Agent/terminal, PageUp/PageDown scrolls, and Esc exits."
        )),
    CommandSpec("/term", "List, create, or rename terminals", "Agents & Terminals", "/term [name|rename <old> <new>]", aliases=("/t",), subcommands=("rename",)),
    CommandSpec("/helpwo", "Connect this CLI to Helpwo as a runtime environment (this folder = its workspace), or open the local/hosted app; /helpwo stop to go offline", "Agents & Terminals", "/helpwo [--port N] [--host ADDR] [--dist <path>] [--remote] | stop", subcommands=("stop",)),
    CommandSpec(
        "/station", "Bind an employee to a terminal and optionally start work",
        "Agents & Terminals", "/station <agent-id> [terminal] [--task <work>]",
        aliases=("/st",),
        help_text=(
            "Without --task, deploys or moves the employee. With --task, starts a fresh "
            "background Assignment with isolated state/history. For an undeployed "
            "employee, omitting the terminal with --task uses a private temporary "
            "terminal; deployment always requires an explicit target."
        )),
    CommandSpec("/terminate", "Close a terminal and all child resources", "Agents & Terminals", "/terminate <name>"),
    CommandSpec("/send", "Send input to a terminal", "Agents & Terminals", "/send <name> [--wait <seconds>] <command>"),
    CommandSpec("/spawn", "Spawn a sub-agent", "Agents & Terminals", "/spawn [name:] <task>"),
    CommandSpec("/tell", "Send a message to an agent", "Agents & Terminals", "/tell <agent-id> <message|json>"),
    CommandSpec("/abort", "Abort an agent", "Agents & Terminals", "/abort <agent-id>"),
    CommandSpec("/hwo", "Open or run an orchestration workflow", "Planning & Tasks", "/hwo [file|run <file>|compile <file>]", subcommands=("run", "compile")),
    CommandSpec("/hwg", "Compile, run, or resume an HWO graph workflow", "Planning & Tasks", "/hwg {run|compile|resume|status|cancel} ...", subcommands=("run", "compile", "resume", "status", "cancel")),
    CommandSpec("/mode", "Show, switch, or create agent modes", "Planning & Tasks", "/mode [act [always]|plan [task]|review|study|list|create|delete]", subcommands=("act", "always", "plan", "review", "study", "list", "create", "delete")),
    CommandSpec("/plan", "Create, revise, review, or approve versioned plans", "Planning & Tasks", "/plan {enter|submit|revise|approve|exit|status|list}", subcommands=("enter", "submit", "revise", "approve", "exit", "status", "list")),
    CommandSpec("/prompt", "Open Prompt Lab or manage tested prompt overlays", "Planning & Tasks", "/prompt [issue|subcommand]", subcommands=("status", "branches", "open", "chat", "review", "test", "activate", "disable", "patches", "profiles", "profile", "use", "rollback", "feedback", "fail", "optimize", "apply", "discard", "list", "skill", "export", "install", "publish")),
    CommandSpec("/evolve", "Create, improve, test, and hot-load project extensions", "Planning & Tasks", "/evolve [idea|subcommand]", subcommands=("status", "branches", "open", "chat", "review", "test", "activate", "disable", "candidates", "profiles", "profile", "use", "rollback", "list", "help")),
    CommandSpec("/task", "Track project tasks", "Planning & Tasks", "/task [list|add|show|start|done|del|progress|note|subtask]", subcommands=("list", "add", "show", "start", "done", "del", "progress", "note", "subtask")),
    CommandSpec("/work", "Inspect or resume unified WorkGraph state", "Planning & Tasks", "/work [status|list|resume|history]", subcommands=("status", "list", "resume", "history")),
    CommandSpec("/workflow", "Run a multi-phase workflow", "Planning & Tasks", "/workflow {start|status|advance|approve|end|list}", subcommands=("start", "status", "advance", "approve", "end", "list")),
    CommandSpec("/model", "List or select a deployed terminal model override", "Config & Tools", "/model [terminal] [id|reset]", subcommands=("reset", "clear", "default")),
    CommandSpec("/config", "View or set runtime configuration", "Config & Tools", "/config [<key>|<prefix> [<value>]|reset]"),
    CommandSpec("/web", "Inspect web search and fetch: engines, proxy, cookies, diagnostics",
                "Config & Tools",
                "/web [status|engines [init]|test [engine]|try <query>|cookies [clear [domain]]]",
                aliases=("/search",),
                subcommands=("status", "engines", "test", "try", "cookies")),
    CommandSpec("/identity", "Manage saved logins the agent may browse as",
                "Config & Tools",
                "/identity [list|check <name>|capture <name> [domains]|delete <name>]",
                subcommands=("list", "check", "capture", "delete")),
    CommandSpec("/policy", "Show or set security policy", "Config & Tools", "/policy [audit|enforce|disabled [--yes]|reset]", subcommands=("audit", "enforce", "disabled", "reset")),
    CommandSpec("/trust", "Review or change workspace trust", "Config & Tools", "/trust [status|allow|revoke]", subcommands=("status", "allow", "revoke")),
    CommandSpec("/hooks", "Manage executable hooks", "Config & Tools", "/hooks [status|trust|revoke|reload]", subcommands=("status", "trust", "revoke", "reload")),
    CommandSpec("/backend", "Manage backend trust profiles", "Config & Tools", "/backend [status|list|use <name>|config]", subcommands=("status", "list", "use", "config")),
    CommandSpec("/max", "Lift runtime limits for this process", "Config & Tools"),
    CommandSpec("/tools", "List registered tools", "Config & Tools"),
    CommandSpec("/tool", "Invoke a tool directly", "Config & Tools", "/tool <name> [json-params]"),
    CommandSpec("/skill", "Manage skills", "Config & Tools", "/skill [manager|list|trust|revoke|load|unload|reload|new|dir]", subcommands=("manager", "list", "trust", "revoke", "load", "unload", "reload", "new", "dir")),
    CommandSpec("/mcp", "Manage MCP servers", "Config & Tools", "/mcp {list|trust|revoke|connect|disconnect|reload|tools|init|config}", subcommands=("list", "trust", "revoke", "connect", "disconnect", "reload", "tools", "init", "config")),
    CommandSpec("/bash", "Run a command through term0", "Config & Tools", "/bash <command>|list|add <command>|remove <command>", subcommands=("list", "add", "remove")),
    CommandSpec("/memory", "Manage memory (interactive view/delete); split into global/local", "Config & Tools", "/memory [global|local|persistent|project|show <id|name>]", subcommands=("global", "local", "persistent", "project", "show")),
    CommandSpec("/mail", "Check or send mail to/from your Laintas account", "Config & Tools", "/mail [inbox [--all]|read <n>|send [subject]]", subcommands=("inbox", "read", "send")),
    CommandSpec("/prop", "View .laintas/cli.prop prompt template", "Config & Tools"),
    CommandSpec("/debug", "Browse or export debug entries", "Config & Tools", "/debug [clear|N|N <file> [--raw]]", subcommands=("clear",)),
    CommandSpec("/why", "Explain a recent tool failure", "Config & Tools", "/why [N|tool|terminal|agent]"),
    CommandSpec("/stream", "Set bounded streaming preview", "Config & Tools", "/stream [off|one|detail]", subcommands=("off", "one", "detail")),
    CommandSpec("/theme", "Set terminal color theme", "Config & Tools", "/theme [dark|light|mono]", subcommands=("dark", "light", "mono")),
    CommandSpec("/detail", "Toggle full vs simplified progress rendering", "Config & Tools", "/detail [on|off]", subcommands=("on", "off")),
    CommandSpec("/undo", "Restore a git checkpoint", "History", "/undo [sha]"),
    CommandSpec("/snapshot", "Create a git checkpoint", "History", "/snapshot [label]"),
    CommandSpec("/snapshots", "List git checkpoints", "History"),
    CommandSpec("/compact", "Compact the current session context", "History",
                "/compact [status|--force]", subcommands=("status",)),
    CommandSpec("/continue", "Continue the current live session", "History"),
    CommandSpec("/told", "Replay prompts or a selected Agent's conversation", "History",
                "/told [agent-id [reply [N]|all]|N|all|reply [N]|log [N]]",
                subcommands=("all", "reply", "log")),
    # Keep /reload discoverable, but its existing handler and behavior stay untouched.
    CommandSpec("/reload", "Reload default files and restart", "History"),
    CommandSpec("/focus", "Deprecated: use /station to run another Agent", "Agents & Terminals",
                palette=False),
)

_NEW_SESSION_COMMANDS = ("/new", "/clear", "/new-session", "/reset-session")


def _slash_command_names() -> list[str]:
    names = {name for spec in COMMAND_SPECS for name in spec.all_names}
    try:
        names.update(extension_runtime.get_runtime().command_names())
    except Exception:
        pass
    return sorted(names)


def _find_command_spec(name: str) -> Optional[CommandSpec]:
    normalized = name if name.startswith("/") else f"/{name}"
    normalized = normalized.lower()
    return next(
        (spec for spec in COMMAND_SPECS
         if normalized in {item.lower() for item in spec.all_names}),
        None,
    )


class MetaCompleter(Completer):
    """Context-aware completer: /-commands, shell commands from PATH, and paths."""

    META_COMMANDS = _slash_command_names()

    def __init__(self):
        self._path = PathCompleter(expanduser=True)
        self._cmd_completer: WordCompleter | None = None
        self._cmd_words: list[str] = []
        self._cmd_mtime: float = 0.0

    def _refresh_commands(self):
        """Refresh the cached command list if PATH has changed."""
        now = time.time()
        if now - self._cmd_mtime < 5 and self._cmd_completer is not None:
            return
        words = set(_builtins_for_platform())
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            p = Path(path_dir)
            if not p.is_dir():
                continue
            try:
                for entry in p.iterdir():
                    if entry.is_file() and os.access(entry, os.X_OK):
                        words.add(entry.name)
            except (PermissionError, OSError):
                continue
        self._cmd_words = sorted(words)
        self._cmd_completer = WordCompleter(self._cmd_words, ignore_case=True, sentence=True)
        self._cmd_mtime = now

    @staticmethod
    def _completion(value: str, fragment: str, description: str = "") -> Completion:
        """Build a menu entry that remains visible for an exact match.

        prompt_toolkit suppresses a menu containing one no-op completion.  An
        exact match therefore completes to the same value plus a space, while
        its displayed value remains unchanged.  Selecting it naturally moves
        the user into the command's next argument context.
        """
        exact = value.casefold() == fragment.casefold()
        return Completion(
            value + (" " if exact else ""),
            start_position=-len(fragment),
            display=value,
            display_meta=description,
        )

    @staticmethod
    def _history_agent_completion_rows() -> list[tuple[str, str, str]]:
        """Return Agent identities offered by `/told` history completion."""
        try:
            if _terminal_agents.configured:
                rows = _terminal_agents.snapshot().get("agents", [])
            else:
                rows = [{
                    "id": agent.id,
                    "name": agent.name or agent.id,
                    "phase": str(getattr(agent, "status", "idle")),
                } for agent in get_all_agents()]
        except Exception:
            return []
        result = []
        for row in rows:
            agent_id = str(row.get("id") or "")
            if not agent_id:
                continue
            name = str(row.get("name") or agent_id)
            phase = str(row.get("phase") or "idle")
            identity = f"{name} {symbols.BULLET} " if name != agent_id else ""
            result.append((
                agent_id, name, f"{identity}{phase} {symbols.BULLET} history available"))
        return result

    def _told_completions(self, partial: str):
        """Complete global `/told` modes and Agent-scoped history replay."""
        words = partial.split()
        trailing_space = partial.endswith(" ")
        reserved = {
            "all": "Show all prompts in the current primary session",
            "reply": "Replay recent primary conversation turns",
            "log": "Show prompts from the durable project journal",
        }
        if not words or (len(words) == 1 and not trailing_space):
            fragment = words[0] if words else ""
            for value, meta in reserved.items():
                if value.startswith(fragment.casefold()):
                    yield self._completion(value, fragment, meta)
            for agent_id, name, meta in self._history_agent_completion_rows():
                if (agent_id.casefold().startswith(fragment.casefold())
                        or name.casefold().startswith(fragment.casefold())):
                    yield self._completion(
                        agent_id, fragment,
                        meta.rsplit(f" {symbols.BULLET} ", 1)[0] + f" {symbols.BULLET} conversation history")
            return

        first = words[0].casefold()
        if first in reserved or first.isdigit():
            return
        if len(words) == 1 and trailing_space:
            for value, meta in (
                    ("reply", "Replay recent turns for this Agent"),
                    ("all", "Replay this Agent's complete local history")):
                yield self._completion(value, "", meta)
        elif len(words) == 2 and not trailing_space:
            fragment = words[1] if len(words) == 2 else ""
            for value, meta in (
                    ("reply", "Replay recent turns for this Agent"),
                    ("all", "Replay this Agent's complete local history")):
                if value.startswith(fragment.casefold()):
                    yield self._completion(value, fragment, meta)

    def _config_completions(self, partial: str):
        """Complete `/config` keys and — for enumerable keys — their values.

        Keys come live from ``describe_runtime_config()`` so newly registered
        config options appear automatically. Value hints cover enum keys
        (``_RUNTIME_ENUM_CHOICES``) and booleans; numeric/free-form keys yield
        nothing, letting the user type the value.
        """
        words = partial.split()
        trailing_space = partial.endswith(" ")

        # First argument: every config key plus the `reset` subcommand.
        if not words or (len(words) == 1 and not trailing_space):
            fragment = words[0] if words else ""
            try:
                described = describe_runtime_config()
            except Exception:
                described = {}
            candidates = [("reset", "Restore the default configuration")]
            candidates.extend(
                (key, meta.get("description", "Runtime option"))
                for key, meta in sorted(described.items())
            )
            for value, meta in candidates:
                if value.casefold().startswith(fragment.casefold()):
                    yield self._completion(value, fragment, meta)
            return

        # Second argument: value hints for the chosen key (if enumerable).
        if len(words) == 1 and trailing_space:
            key, fragment = words[0], ""
        elif len(words) == 2 and not trailing_space:
            key, fragment = words[0], words[1]
        else:
            return  # already past the value — nothing more to suggest
        if key.casefold() == "reset":
            return

        import agent_loop as _agent_loop
        enum_choices = _agent_loop._RUNTIME_ENUM_CHOICES.get(key)
        if enum_choices is not None:
            candidates = sorted(enum_choices)
        else:
            try:
                meta = describe_runtime_config().get(key)
            except Exception:
                meta = None
            if meta and meta.get("type") == "bool":
                candidates = ["true", "false"]
            else:
                return
        for value in candidates:
            if value.casefold().startswith(fragment.casefold()):
                yield self._completion(value, fragment, f"{key} value")

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text:
            return
        # /-command completion — auto-popup while typing (complete_while_typing)
        if text.startswith("/"):
            if any(ch.isspace() for ch in text):
                head, _, tail = text.partition(" ")
                spec = _find_command_spec(head)
                partial = tail.lstrip()
                head_lower = head.lower()

                if head_lower == "/told":
                    yield from self._told_completions(partial)
                    return
                if head_lower == "/config":
                    yield from self._config_completions(partial)
                    return

                # Context-aware employee command completion. These values are
                # runtime data, so they cannot live in static CommandSpec.
                if head_lower == "/hire":
                    hire_words = partial.split()
                    if "--profile" in hire_words:
                        profile_index = hire_words.index("--profile")
                        if profile_index == len(hire_words) - 1:
                            fragment = ""
                        elif profile_index == len(hire_words) - 2:
                            fragment = hire_words[-1]
                        else:
                            fragment = None
                        if fragment is not None:
                            import agent_roles
                            for role in agent_roles.list_roles():
                                if role.name.startswith(fragment.lower()):
                                    yield self._completion(
                                        role.name, fragment, role.description)
                            return
                if head_lower in ("/agent", "/agents", "/station", "/st"):
                    words = partial.split()
                    trailing_space = tail.endswith(" ")
                    if not words or (len(words) == 1 and not trailing_space):
                        fragment = words[0] if words else ""
                        candidates = []
                        if head_lower == "/agents":
                            candidates.append(("tree", "show employee tree"))
                        if head_lower == "/agent":
                            # /agent takes a single agent-id-or-name argument.
                            # Include every agent (primary is a valid switch
                            # target) and surface each agent's name as a
                            # completion alias when distinct from its id.
                            for agent in get_all_agents():
                                candidates.append(
                                    (agent.id, agent.profile.title))
                                if (agent.name
                                        and agent.name != agent.id
                                        and agent.name.lower()
                                        != agent.id.lower()):
                                    candidates.append(
                                        (agent.name,
                                         f"alias for {agent.id}"))
                        else:
                            candidates.extend(
                                (agent.id, agent.profile.title)
                                for agent in get_all_agents()
                                if agent.role != "primary"
                            )
                        for value, meta in candidates:
                            if value.lower().startswith(fragment.lower()):
                                yield self._completion(value, fragment, meta)
                        return
                    if head_lower in ("/station", "/st"):
                        if "--task" in words or "--" in words:
                            return
                        fragment = "" if trailing_space else words[-1]
                        candidates = [("--task", "start a fresh assignment")]
                        candidates.extend(
                            (term.name, "existing terminal")
                            for term in get_all_terminals()
                            if term.name != "term0"
                        )
                        for value, meta in candidates:
                            if value.lower().startswith(fragment.lower()):
                                yield self._completion(value, fragment, meta)
                        return
                if spec and " " not in partial:
                    for entry in spec.contextual_completions:
                        if entry.value.casefold().startswith(partial.casefold()):
                            yield self._completion(
                                entry.value, partial, entry.description)
                return
            for cmd in self.META_COMMANDS:
                if cmd.casefold().startswith(text.casefold()):
                    _spec = _find_command_spec(cmd)
                    yield self._completion(
                        cmd, text, _spec.description if _spec else "")
            return

        # For non-/-prefixed input, only show completions on explicit Tab —
        # avoids a noisy menu popping up on every keystroke while typing
        # natural-language input.
        if not complete_event.completion_requested:
            return

        # First word — complete from PATH + builtins
        stripped = document.text_before_cursor.lstrip()
        cursor_in_first_word = " " not in stripped
        if cursor_in_first_word:
            self._refresh_commands()
            yield from self._cmd_completer.get_completions(document, complete_event)
            return

        # After a command — path/file completion.
        # PathCompleter uses document.text_before_cursor (the full line) as the
        # path prefix, which breaks for sentence input like "vim laint".  Create
        # a sub-document that only contains the last whitespace-delimited word
        # so that PathCompleter sees a bare path fragment.
        text_before = document.text_before_cursor
        last_space = text_before.rfind(" ")
        if last_space != -1:
            path_text = text_before[last_space + 1:]
            sub_doc = Document(path_text, len(path_text))
        else:
            sub_doc = document
        yield from self._path.get_completions(sub_doc, complete_event)


def _build_prompt_style() -> Style:
    """Build prompt_toolkit Style for the prompt, completion menu, and status bar."""
    mode = str(get_runtime_config("theme") or "dark")
    palettes = {
        "dark": ("#c9d1d9", "#2ea043", "#4ade80", "#e3b341", "#8b949e", "#6e7681", "#a78bfa", "#161b22", "#21262d"),
        "light": ("#24292f", "#176f2c", "#116329", "#9a6700", "#57606a", "#6e7781", "#8250df", "#f6f8fa", "#d0d7de"),
        "mono": ("", "", "", "", "", "", "", "", "reverse"),
    }
    path, accent, success, warning, muted, subtle, agent, menu, selected = palettes.get(mode, palettes["dark"])
    menu_bg = f"bg:{menu}" if menu.startswith("#") else menu
    selected_bg = f"bg:{selected}" if selected.startswith("#") else selected
    return Style.from_dict({
        "prompt-path": path,
        "prompt-gutter": f"{accent} bold",
        "prompt-gutter-plan": f"{warning} bold",
        "prompt-caret": success,
        "prompt-agent": f"{agent} bold",
        "prompt-agent-sup": muted,
        "separator": accent,
        "paste-placeholder": f"bold {success}",
        # Slash-command completion menu keeps its established visual identity.
        "completion-menu": menu_bg,
        "completion-menu.completion": f"{menu_bg} {path}",
        "completion-menu.completion.current": f"{selected_bg} {path} bold",
        "completion-menu.meta.completion": f"{menu_bg} {muted}",
        "completion-menu.meta.completion.current": f"{selected_bg} {muted}",
        # Bottom status bar also inherits the terminal background.
        "bottom-toolbar": "#ffffff",
        "stbar-sep": subtle,
        "stbar-model": f"{accent} bold",
        "stbar-mode-act": f"{success} bold",
        "stbar-mode-plan": f"{warning} bold",
        "stbar-tokens": path,
        "stbar-time": muted,
        "stbar-context": muted,
        "stbar-dot-act": f"{success} bold",
        "stbar-dot-plan": f"{warning} bold",
        # rprompt (right side of prompt line — no background)
        "rprompt-mode-act": f"{success} bold",
        "rprompt-mode-plan": f"{warning} bold",
        "rprompt-sep": subtle,
        "rprompt-model": muted,
        "rprompt-context": agent,
    })


# ── Paste summarization ───────────────────────────────────────────────
# Large pastes (many lines or a long single line) are collapsed into a
# compact placeholder shown in the prompt buffer; the real content is kept
# off-screen and substituted back when the line is submitted. Mirrors
# opencode's pasteText/pasteInputText, adapted to prompt_toolkit.
_paste_registry: dict = {}
_paste_counter = 0


def _reset_paste_registry() -> None:
    """Clear pending placeholder→content mappings (called after each submit)."""
    global _paste_registry, _paste_counter
    _paste_registry = {}
    _paste_counter = 0


def _maybe_summarize_paste(data: str):
    """Decide whether a pasted blob should be collapsed into a placeholder.

    Returns (placeholder, line_count). When the paste is small or the feature
    is disabled, placeholder is None and the caller should insert data as-is.
    On summarization the placeholder→real-content mapping is registered.
    """
    global _paste_counter
    if data is None:
        return None, 0
    normalized = data.replace("\r\n", "\n").replace("\r", "\n")
    line_count = normalized.count("\n") + 1 if normalized else 0
    try:
        enabled = bool(get_runtime_config("paste_summary"))
    except Exception:
        enabled = True
    if not enabled:
        return None, line_count
    try:
        min_lines = int(get_runtime_config("paste_summary_min_lines"))
    except Exception:
        min_lines = 3
    try:
        min_chars = int(get_runtime_config("paste_summary_min_chars"))
    except Exception:
        min_chars = 150
    if line_count >= min_lines or len(normalized) >= min_chars:
        _paste_counter += 1
        placeholder = f"[Pasted #{_paste_counter} ~{line_count} lines]"
        _paste_registry[placeholder] = normalized
        return placeholder, line_count
    return None, line_count


def _expand_pastes(text: str) -> str:
    """Replace any registered placeholders in text with their real content."""
    if not text or not _paste_registry:
        return text
    for placeholder, real in _paste_registry.items():
        if placeholder in text:
            text = text.replace(placeholder, real)
    return text


_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted #\d+ ~\d+ lines\]")


def _paste_span_at(text: str, pos: int):
    """Return (start, end) of a placeholder covering/adjacent to pos, else None.

    Used for whole-segment deletion: Backspace treats pos as the cursor
    position (checks the span ending at pos), Delete checks the span
    starting at pos.
    """
    if not text:
        return None
    for m in _PASTE_PLACEHOLDER_RE.finditer(text):
        if m.start() <= pos <= m.end():
            return m.start(), m.end()
    return None


class _PastePlaceholderLexer(Lexer):
    """Colorize [Pasted #N ~L lines] placeholders in the input buffer."""

    def lex_document(self, document):
        placeholder_style = "class:paste-placeholder"

        def get_line(lineno):
            line = document.lines[lineno]
            if not line:
                return []
            spans = list(_PASTE_PLACEHOLDER_RE.finditer(line))
            if not spans:
                return [("", line)]
            fragments = []
            idx = 0
            for m in spans:
                if m.start() > idx:
                    fragments.append(("", line[idx:m.start()]))
                fragments.append((placeholder_style, line[m.start():m.end()]))
                idx = m.end()
            if idx < len(line):
                fragments.append(("", line[idx:]))
            return fragments

        return get_line


class _PasteGuardBuffer(Buffer):
    """Buffer whose cursor cannot enter a paste-placeholder span.

    [Pasted #N ~L lines] tokens are atomic and non-editable: arrow keys and
    mouse clicks skip over the whole token instead of landing inside it.

    The cursor_position setter is the single chokepoint — Buffer.cursor_left/
    right/up/down all go through ``self.cursor_position += ...``, and
    BufferControl's mouse handler sets ``buffer.cursor_position`` directly,
    so overriding the setter covers both keyboard and mouse in one place.
    """

    @property
    def cursor_position(self) -> int:
        return Buffer.cursor_position.fget(self)

    @cursor_position.setter
    def cursor_position(self, value: int) -> None:
        span = _paste_span_at(self.text, value)
        if span is not None and span[0] < value < span[1]:
            current = Buffer.cursor_position.fget(self)
            value = span[1] if value >= current else span[0]
        Buffer.cursor_position.fset(self, value)


def _refresh_slash_completion(buffer: Buffer) -> None:
    """Restart slash completion after a deletion; invalid prefixes yield none."""
    if buffer.document.text_before_cursor.lstrip().startswith("/"):
        buffer.start_completion()


def _build_keybindings() -> KeyBindings:
    """Build custom keybindings for the prompt."""
    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste)
    def _(event):
        """Collapse large pastes into a compact placeholder (expanded on submit)."""
        placeholder, _lc = _maybe_summarize_paste(event.data)
        if placeholder is not None:
            event.current_buffer.insert_text(placeholder)
        else:
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            event.current_buffer.insert_text(data)

    @kb.add("backspace")
    def _(event):
        """Backspace: delete text and refresh valid slash completions."""
        buf = event.current_buffer
        span = _paste_span_at(buf.text, buf.cursor_position)
        if span is not None and span[1] == buf.cursor_position and span[0] != span[1]:
            start, end = span
            buf.text = buf.text[:start] + buf.text[end:]
            buf.cursor_position = start
        else:
            buf.delete_before_cursor(count=event.arg)
        # prompt_toolkit autocompletes insertions, but not deletions.
        _refresh_slash_completion(buf)

    @kb.add("delete")
    def _(event):
        """Delete: remove text and refresh valid slash completions."""
        buf = event.current_buffer
        span = _paste_span_at(buf.text, buf.cursor_position)
        if span is not None and span[0] == buf.cursor_position and span[0] != span[1]:
            start, end = span
            buf.text = buf.text[:start] + buf.text[end:]
            buf.cursor_position = start
        else:
            buf.delete(count=event.arg)
        _refresh_slash_completion(buf)

    @kb.add("c-d")
    def _(event):
        """Ctrl+D exits if input is empty, otherwise deletes forward."""
        if not event.current_buffer.text:
            event.app.exit(result="/exit")

    @kb.add("tab")
    def _(event):
        """Tab: accept suggestion → cycle completions → start completion."""
        buf = event.current_buffer
        if buf.suggestion and buf.document.is_cursor_at_the_end and not buf.complete_state:
            buf.insert_text(buf.suggestion.text)
        elif buf.complete_state:
            buf.complete_next()
        else:
            buf.start_completion()

    @kb.add("c-f")
    def _(event):
        """Ctrl+F: accept auto-suggest ghost text (fish-style)."""
        buf = event.current_buffer
        if buf.suggestion:
            buf.insert_text(buf.suggestion.text)

    @kb.add("c-e")
    def _(event):
        """Ctrl+E: accept auto-suggest ghost text."""
        buf = event.current_buffer
        if buf.suggestion:
            buf.insert_text(buf.suggestion.text)

    @kb.add("c-o")
    def _(event):
        """Toggle compact/detail progress without leaving the prompt."""
        enabled = not bool(get_runtime_config("detail"))
        set_runtime_config("detail", enabled)
        try:
            terminal_preferences.set_ui_preference("detail", enabled)
        except Exception:
            pass
        _update_status_cache(detail=enabled)
        event.app.invalidate()

    @kb.add("escape")
    def _(event):
        """Esc: clear the current input buffer. Works like Ctrl+C cancel."""
        buf = event.current_buffer
        buf.reset()
        _set_run_input_state("input_active")

    return kb


class _SingleForegroundAgentUI:
    """Compatibility boundary after removing same-terminal shared rendering."""

    configured = False

    @staticmethod
    def close() -> None:
        return None

    @staticmethod
    def has_multiple() -> bool:
        return False

    @staticmethod
    def snapshot() -> dict:
        return {
            "foreground_id": "",
            "input_target_id": "",
            "running_count": 0,
            "agents": [],
        }

    @staticmethod
    def process_pending_approval() -> bool:
        return False

    @staticmethod
    def abort_foreground() -> bool:
        return False

    @staticmethod
    def resolve_agent_id(reference: str) -> str:
        needle = str(reference or "").strip()
        direct = get_agent(needle)
        if direct is not None:
            return direct.id
        matches = [
            agent for agent in get_all_agents()
            if str(agent.name or "").casefold() == needle.casefold()
        ]
        return matches[0].id if len(matches) == 1 else ""

    @staticmethod
    def chat_history_for(agent_id: str) -> list:
        agent = get_agent(agent_id)
        return agent.chat_history if agent is not None else []


_terminal_agents = _SingleForegroundAgentUI()


# ── Status bar (bottom toolbar) ───────────────────────────────────────
# Lightweight session-status cache read by the bottom_toolbar callable.
# Updated at key moments (startup, /model, after backend call, after agent
# loop) so the toolbar callback itself never touches disk — only reads this
# in-memory dict + usage_tracker._SESSION (also in-memory).
_status_cache: dict = {
    "model": "",
    "model_source": "default",
    "agent": "",
    "terminal": "term0",
    "deployment": "temporary",
    "run_input_state": "idle",
    "running_agents": 0,
    "detail": False,
    "last_thinking_time": 0.0,
    "foreground_agent": "",
    "foreground_phase": "",
    "multi_agent": False,
}


def _update_status_cache(**kwargs) -> None:
    """Patch one or more fields in the module-level status cache."""
    _status_cache.update(kwargs)


def _terminal_width() -> int:
    """Return a bounded terminal width for deterministic responsive chrome."""
    try:
        return max(20, int(shutil.get_terminal_size(fallback=(80, 24)).columns))
    except Exception:
        return 80


def _sync_status_context() -> None:
    """Refresh prompt context once per prompt, never on every keystroke."""
    try:
        agent = get_current_agent()
        foreground_name = ""
        foreground_phase = ""
        multi_agent = False
        running_count = 0
        if _terminal_agents.configured:
            snap = _terminal_agents.snapshot()
            multi_agent = len(snap.get("agents", [])) > 1
            if multi_agent:
                target_id = snap.get("input_target_id", "")
                target = get_agent(target_id)
                if target is not None:
                    agent = target
                foreground_id = snap.get("foreground_id", "")
                foreground_row = next((row for row in snap.get("agents", [])
                                       if row["id"] == foreground_id), None)
                if foreground_row:
                    foreground_name = foreground_row.get("name", "")
                    foreground_phase = foreground_row.get("phase", "")
                running_count = int(snap.get("running_count", 0))
        if agent is None:
            return
        deployment = agent_deployment_terminal(agent)
        terminal_name = deployment or agent_scope_terminal(agent) or "term0"
        terminal = get_terminal(deployment) if deployment else None
        if terminal and terminal.model_override:
            model = str(terminal.model_override)
            model_source = "terminal"
        elif getattr(agent, "base_model", ""):
            model = str(agent.base_model)
            model_source = "agent"
        else:
            model = get_selected_model() or _status_cache.get("model", "")
            model_source = "default"
        _update_status_cache(
            agent=str(agent.name or agent.id),
            terminal=terminal_name,
            deployment="deployed" if deployment else "temporary",
            model=model or "auto",
            model_source=model_source,
            detail=bool(get_runtime_config("detail")),
            running_agents=running_count,
            foreground_agent=foreground_name,
            foreground_phase=foreground_phase,
            multi_agent=multi_agent,
            input_available=bool(
                not multi_agent or snap.get("input_target_id", "")),
        )
    except Exception:
        return


def _fmt_tokens(n: int) -> str:
    """Compact token count: 856 → '856', 12345 → '12.3k'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _session_token_totals() -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) for this process session."""
    try:
        import usage_tracker as _ut
        with _ut._LOCK:
            total_in = sum(r.get("in", 0) for r in _ut._SESSION)
            total_out = sum(r.get("out", 0) for r in _ut._SESSION)
        return total_in, total_out
    except Exception:
        return 0, 0


def _render_rprompt():
    """Formatted text for the right side of the prompt line (mode + model)."""
    try:
        import plan_mode as _pm
        _is_plan = _pm.is_plan_mode()
    except Exception:
        _is_plan = False
    _active_mode = mode_manager.get_active_mode()
    _mode_label = "PLAN" if _is_plan else _active_mode["name"].upper()
    # Auto-approve indicator: suffix the mode with * whenever writes or commands
    # are being auto-approved this session - via a mode's auto_approve posture,
    # or at least one remembered per-target "Always" approval. E.g. ACT*, OPS*.
    # A read-only mode (STUDY, REVIEW, ...) can't write or run anything, so a
    # leftover session approval must not advertise itself there.
    _read_only = mode_manager.is_read_only_mode(_active_mode)
    _has_star = (not _is_plan and not _read_only
                 and (_session_approval_state.get("all_writes")
                     or _session_approval_state.get("all_commands")
                     or _session_approval_state.get("approved_write_paths")
                     or _session_approval_state.get("approved_commands")))
    if _has_star:
        _mode_label += "*"
        global _approval_star_announced
        if not _approval_star_announced:
            _approval_star_announced = True
            console.print(
                f"[dim]{symbols.ZAP} Auto-approve active ([bold]*[/bold]). "
                "Use /mode to change.[/dim]")
    # Read-only modes share PLAN's styling — same "I won't touch anything" signal.
    _mode_cls = ("rprompt-mode-plan" if (_is_plan or _read_only)
                 else "rprompt-mode-act")
    _model = _status_cache.get("model", "") or "auto"
    width = _terminal_width()
    _agent_name = _status_cache.get("agent", "") or "primary"
    if width >= 62:
        if (_status_cache.get("multi_agent")
                and not _status_cache.get("input_available", True)):
            label = "all busy"
        else:
            label = (f"to {_agent_name}"
                     if _status_cache.get("multi_agent") else _agent_name)
        result = [("class:rprompt-context", label),
                  (f"class:rprompt-sep", f" {symbols.BULLET} "),
                  ("class:" + _mode_cls, _mode_label)]
    else:
        result = [("class:" + _mode_cls, _mode_label)]
    if width >= 78:
        # Show "auto-routing" when auto-routing is active, plain model name otherwise
        _model_display = "auto-routing" if _model in ("", "auto") else _model
        result.extend([
            (f"class:rprompt-sep", f" {symbols.BULLET} "),
            ("class:rprompt-model", _model_display),
        ])
    if width >= 108 and not _status_cache.get("multi_agent"):
        agent = _status_cache.get("agent", "")
        terminal = _status_cache.get("terminal", "")
        if agent and terminal:
            result.extend([
                (f"class:rprompt-sep", f" {symbols.BULLET} "),
                ("class:rprompt-context", f"{agent}@{terminal}"),
            ])
    return result


def _render_bottom_toolbar():
    """prompt_toolkit bottom_toolbar callable — single-line status bar.

    Invoked on every keystroke; must be fast (all reads are in-memory).
    Shows: last thinking time | session tokens.
    Mode and model are displayed in the rprompt (right side of prompt line).
    """
    _tin, _tout = _session_token_totals()
    _think = _status_cache.get("last_thinking_time", 0.0)
    _think_str = _fmt_elapsed(_think) if _think > 0 else "—"

    width = _terminal_width()
    tokens = ("class:stbar-tokens", f"{symbols.ARROW_U}{_fmt_tokens(_tin)} {symbols.ARROW_D}{_fmt_tokens(_tout)}")
    if width < 54:
        return [tokens]
    result = [
        ("class:stbar-time", f"last {_think_str}"),
        (f"class:stbar-sep", f"  {symbols.BULLET}  "),
        tokens,
    ]
    if width >= 86:
        terminal = _status_cache.get("terminal", "term0")
        deployment = _status_cache.get("deployment", "temporary")
        context = f"{terminal} {symbols.BULLET} {deployment}"
        running_count = int(_status_cache.get("running_agents", 0) or 0)
        if running_count:
            context += f" {symbols.BULLET} {running_count} running"
        if _status_cache.get("detail"):
            context += f" {symbols.BULLET} detail"
        result[:0] = [
            ("class:stbar-context", context),
            ("class:stbar-sep", "  │  "),
        ]
    input_state = _status_cache.get("run_input_state")
    if input_state in {"queued", "input_active"} and width >= 72:
        result.extend([
            (f"class:stbar-sep", f"  {symbols.BULLET}  "),
            ("class:stbar-context", "input " + ("queued" if input_state == "queued" else "ready")),
        ])
    return result


_prompt_session: Optional[PromptSession] = None


def _interrupt_prompt():
    """Force prompt_toolkit to return from another thread.

    Called by the poll thread when a remote message arrives while the main
    loop is blocked on pt_prompt(). Forces the prompt to return with an empty
    string so the main loop re-checks the injection queue immediately.
    """
    global _prompt_session
    if _prompt_session is not None:
        try:
            app = _prompt_session.app
            if app.is_running:
                app.exit(result="")
        except Exception:
            pass


def get_prompt_session() -> PromptSession:
    """Get or create the persistent prompt_toolkit session."""
    global _prompt_session
    if _prompt_session is None:
        hist_file = paths.HISTORY_FILE
        _prompt_session = PromptSession(
            history=FileHistory(str(hist_file)),
            completer=MetaCompleter(),
            auto_suggest=AutoSuggestFromHistory(),
            style=_build_prompt_style(),
            key_bindings=_build_keybindings(),
            lexer=_PastePlaceholderLexer(),
            enable_history_search=False,
            vi_mode=False,
            complete_while_typing=True,
            mouse_support=Condition(lambda: bool(get_runtime_config("enable_mouse"))),
        )
        # Upgrade the default buffer so the cursor snaps out of paste
        # placeholders instead of entering them (arrow keys + mouse clicks).
        try:
            _prompt_session.default_buffer.__class__ = _PasteGuardBuffer
        except TypeError:
            pass
    return _prompt_session


def pt_prompt(cwd: str) -> str:
    """Read input for the terminal's one foreground Agent."""
    session = get_prompt_session()
    width = _terminal_width()
    disp = _shorten_path(cwd, max_len=max(16, min(60, width - 8)))
    try:
        import plan_mode as _pm
        gutter_cls = (
            "class:prompt-gutter-plan"
            if _pm.is_plan_mode() else "class:prompt-gutter")
    except Exception:
        gutter_cls = "class:prompt-gutter"
    message = [
        ("class:stbar-sep", "  "),
        ("class:prompt-path", disp),
        ("", "\n"),
        (gutter_cls, "│ "),
        ("class:prompt-caret", "› "),
    ]
    _clear_stale_running_loop()
    try:
        user_input = session.prompt(
            message,
            style=_build_prompt_style(),
            multiline=False,
            rprompt=_render_rprompt,
            complete_while_typing=True,
        )
        expanded = _expand_pastes(user_input) if user_input else user_input
        _reset_paste_registry()
        return expanded.strip() if expanded else ""
    except KeyboardInterrupt:
        _reset_paste_registry()
        return ""
    except EOFError:
        _reset_paste_registry()
        raise
    except OSError as exc:
        _reset_paste_registry()
        if exc.errno in _TERMINAL_EOF_ERRNOS:
            raise EOFError("terminal input closed") from exc
        raise
    except Exception:
        _reset_paste_registry()
        raise


# ── Dynamic Command Discovery ──────────────────────────────────────────
# Routing decision (system command vs natural language) is made at runtime
# via shutil.which() plus a fixed set of shell builtins. No persisted
# snapshot — newly-installed binaries are picked up immediately.

import re
# bash/sh/zsh builtins that aren't on PATH but should still route as commands.
_POSIX_SHELL_BUILTINS = {
    "alias", "bg", "break", "builtin", "case", "cd", "command", "compgen",
    "complete", "continue", "declare", "dirs", "disown", "echo", "enable",
    "eval", "exec", "exit", "export", "false", "fg", "for", "function",
    "getopts", "hash", "help", "history", "if", "jobs", "kill", "let",
    "local", "logout", "popd", "printf", "pushd", "pwd", "read", "readonly",
    "return", "select", "set", "shift", "shopt", "source", "suspend", "test",
    "time", "times", "trap", "true", "type", "typeset", "ulimit", "umask",
    "unalias", "unset", "until", "wait", "while", ".", ":",
}


def _builtins_for_platform() -> set:
    return _POSIX_SHELL_BUILTINS


def extract_first_word(user_input: str) -> str:
    """Extract the first shell word from user input."""
    m = re.match(r'^\s*(\S+)', user_input)
    if not m:
        return ""
    return m.group(1).strip("'\"`;&|")


def is_system_command(user_input: str) -> bool:
    """True if the first word is a shell builtin or resolvable on PATH."""
    first = extract_first_word(user_input)
    if not first:
        return False
    if first in _builtins_for_platform():
        return True
    return shutil.which(first) is not None


def list_path_commands() -> list:
    """Enumerate user-facing commands currently on PATH (for /scan display)."""
    commands = set()
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    for path_dir in path_dirs:
        p = Path(path_dir)
        if not p.is_dir():
            continue
        try:
            for entry in p.iterdir():
                if entry.is_file() and os.access(entry, os.X_OK):
                    commands.add(entry.name)
        except (PermissionError, OSError):
            pass

    result = []
    for c in sorted(commands):
        if len(c) < 2 or c[0].isupper():
            continue
        if "." in c or ":" in c or c.startswith("_"):
            continue
        if c.startswith("systemd-") or c.startswith("dbus-") or c.startswith("ksvgtop"):
            continue
        result.append(c)
    return result


# ── Shell Detection ──────────────────────────────────────────────────────

DEFAULT_SHELL = os.environ.get("SHELL", "/bin/bash")
SHELL_NAME = "bash" if "bash" in DEFAULT_SHELL else ("zsh" if "zsh" in DEFAULT_SHELL else "sh")

# ── Session Management ─────────────────────────────────────────────────

def load_session() -> Optional[dict]:
    """Load saved session token from ~/.laintas/session.json."""
    if SESSION_FILE.exists():
        if not paths.ensure_private_file(SESSION_FILE):
            return None
        try:
            return json.loads(SESSION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_session(session: dict) -> None:
    """Save session token to ~/.laintas/session.json."""
    _atomic_private_json(SESSION_FILE, session)


def clear_session() -> None:
    """Remove saved session."""
    SESSION_FILE.unlink(missing_ok=True)


def load_config() -> dict:
    """Load CLI config (agent name, backend url, etc.)."""
    if CONFIG_FILE.exists():
        if not paths.ensure_private_file(CONFIG_FILE):
            return {}
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict) -> None:
    """Save CLI config."""
    _atomic_private_json(CONFIG_FILE, config)


def _atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _instance_preferences_path() -> Path:
    """Compatibility wrapper for the current terminal preference path."""
    return terminal_preferences.preference_path()


def _load_instance_preferences() -> dict:
    """Compatibility wrapper returning this terminal's preferences."""
    return terminal_preferences.load()


def _save_instance_preferences() -> None:
    """Deprecated compatibility hook; mutations save through the shared store."""
    terminal_preferences.update(_load_instance_preferences())


def get_selected_model() -> str:
    """Return this terminal's selected model, falling back to config.json.

    Per-terminal preferences take priority; ``config.json`` (the legacy global
    store) is consulted as a fallback so a model set there before the
    per-terminal migration — or set as a global default — is still honoured.
    """
    val = terminal_preferences.get("model", "")
    if not val:
        val = load_config().get("model", "")
    return str(val).strip() if val else ""


def set_selected_model(model: str) -> None:
    """Persist the selected model for this terminal only."""
    model = model.strip()
    if model:
        terminal_preferences.set_value("model", model)
    else:
        terminal_preferences.delete("model")


def get_selected_provider() -> str:
    """Return this terminal's selected provider, falling back to config.json."""
    val = terminal_preferences.get("provider", "")
    if not val:
        val = load_config().get("provider", "")
    return str(val).strip() if val else ""


def set_selected_provider(provider: str) -> None:
    """Persist the selected provider for this terminal only."""
    provider = provider.strip()
    if provider:
        terminal_preferences.set_value("provider", provider)
    else:
        terminal_preferences.delete("provider")


def set_model_selection(model: str, provider: str = "") -> None:
    """Atomically replace the model/provider pair for this terminal."""
    model = str(model or "").strip()
    provider = str(provider or "").strip()
    values = {"model": model} if model else {}
    if model and provider:
        values["provider"] = provider
    remove = tuple(
        key for key, keep in (("model", bool(model)),
                              ("provider", bool(model and provider)))
        if not keep
    )
    terminal_preferences.update(values, remove=remove)


def _normalize_model_entry(item) -> dict:
    """Normalize common model-list response shapes into displayable rows.

    Preserves ``tier`` and ``description`` fields from the gateway response
    for richer display in the model selector table.
    """
    if isinstance(item, str):
        return {"id": item, "name": item, "description": ""}
    if not isinstance(item, dict):
        return {"id": str(item), "name": str(item), "description": ""}
    model_id = (
        item.get("id") or item.get("model") or item.get("name") or
        item.get("value") or item.get("slug") or ""
    )
    name = item.get("name") or item.get("displayName") or item.get("label") or model_id
    desc = item.get("description") or item.get("desc") or item.get("provider") or ""
    tier = item.get("tier") or ""
    return {"id": str(model_id), "name": str(name), "description": str(desc),
            "tier": str(tier)}


def _extract_model_entries(data) -> list[dict]:
    """Extract model rows from backend model-list response shapes."""
    if isinstance(data, dict) and isinstance(data.get("providers"), list):
        rows = []
        for provider in data["providers"]:
            if not isinstance(provider, dict):
                continue
            provider_id = str(provider.get("id") or "")
            provider_label = str(provider.get("label") or provider_id)
            for model in provider.get("models") or []:
                row = _normalize_model_entry(model)
                if row.get("id"):
                    row["provider"] = provider_id
                    row["description"] = row.get("description") or provider_label
                    rows.append(row)
        return rows

    raw_models = data
    if isinstance(data, dict):
        raw_models = (
            data.get("models") or data.get("data") or data.get("items") or
            data.get("result") or []
        )
    if isinstance(raw_models, dict):
        raw_models = list(raw_models.values())
    if not isinstance(raw_models, list):
        return []
    return [_normalize_model_entry(item) for item in raw_models]


class BlockingOperationCancelled(RuntimeError):
    """Raised when Esc or Ctrl+C cancels a foreground blocking operation."""


def run_cancellable_blocking(
    operation: Callable[[threading.Event], object],
) -> object:
    """Run blocking work off the UI thread while Esc/Ctrl+C remain responsive.

    The worker is daemonized because libraries such as requests cannot abort a
    socket read already in progress.  The cancellation event lets cooperative
    operations stop before starting another request; the foreground returns
    immediately even if the current socket needs its timeout to unwind.
    """
    cancelled = threading.Event()
    outcome: queue.Queue = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            outcome.put(("result", operation(cancelled)))
        except BaseException as exc:
            outcome.put(("error", exc))

    worker = threading.Thread(
        target=_worker, daemon=True, name="cancellable-blocking-operation")

    old_sigint = None
    sigint_installed = False
    input_fd = -1
    terminal_fd = -1
    old_tcattr = None

    def _cancel_signal(_signum, _frame) -> None:
        cancelled.set()

    try:
        if threading.current_thread() is threading.main_thread():
            old_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _cancel_signal)
            sigint_installed = True

        try:
            if sys.stdin.isatty():
                input_fd = sys.stdin.fileno()
                terminal_fd = input_fd
                old_tcattr = termios.tcgetattr(input_fd)
                tty.setcbreak(input_fd)
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation,
                termios.error):
            input_fd = -1
            old_tcattr = None

        # Start only after both cancellation channels are armed, so there is
        # no startup window where the operation can block before Ctrl+C/Esc
        # become effective.
        worker.start()

        while True:
            try:
                kind, value = outcome.get_nowait()
            except queue.Empty:
                kind = value = None
            if kind == "result":
                return value
            if kind == "error":
                raise value
            if cancelled.is_set():
                raise BlockingOperationCancelled(
                    "Operation cancelled by Esc or Ctrl+C")

            if input_fd < 0:
                cancelled.wait(0.05)
                continue
            try:
                ready, _, _ = select.select([input_fd], [], [], 0.05)
            except (OSError, ValueError, select.error):
                ready = []
            if not ready:
                continue
            try:
                key = os.read(input_fd, 1)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                input_fd = -1
                continue
            if key == b"\x03":
                cancelled.set()
            elif key == b"\x1b":
                # Do not mistake an impatient arrow/navigation key for bare
                # Esc while the next UI is still loading.
                try:
                    continuation, _, _ = select.select(
                        [input_fd], [], [], 0.04)
                except (OSError, ValueError, select.error):
                    continuation = []
                if continuation:
                    try:
                        os.read(input_fd, 32)
                    except OSError:
                        pass
                else:
                    cancelled.set()
    except KeyboardInterrupt as exc:
        cancelled.set()
        raise BlockingOperationCancelled(
            "Operation cancelled by Ctrl+C") from exc
    finally:
        cancelled.set()
        if old_tcattr is not None and terminal_fd >= 0:
            try:
                termios.tcsetattr(terminal_fd, termios.TCSANOW, old_tcattr)
            except (OSError, termios.error):
                pass
        if sigint_installed:
            signal.signal(signal.SIGINT, old_sigint)


@contextmanager
def _safe_status(message, *, spinner="dots", **_ignored):
    """Deadlock-free stand-in for ``console.status(...)`` that keeps the spinner.

    The shared console writes through repl_mirror.TeeFile, which resolves
    sys.stdout dynamically on every write. Rich's ``console.status`` defaults to
    ``redirect_stdout=True``: it swaps sys.stdout for its own FileProxy, so
    TeeFile then forwards the Live's own output back into the Live — a feedback
    loop that deadlocks ``Live.__exit__`` (on a plain TTY and under
    prompt_toolkit's StdoutProxy alike; that is exactly why the agent-loop
    streaming Live at agent_loop.py sets ``redirect_stdout=False``). A hand-built
    Live with ``redirect_stdout=False`` breaks the loop while keeping the
    animated spinner everywhere. Extra spinner kwargs are accepted and ignored so
    this drops in for any former ``console.status`` call verbatim.
    """
    try:
        text = Text.from_markup(message) if isinstance(message, str) else message
    except Exception:
        text = message
    try:
        status_live = Live(
            Spinner(spinner, text=text), console=console,
            refresh_per_second=12.5, transient=True,
            redirect_stdout=False, redirect_stderr=False)
    except Exception:
        # A status spinner must never take down a command: on any Live
        # construction failure, degrade to a one-shot static line, no Live.
        try:
            console.print(message)
        except Exception:
            pass
        yield
        return
    with status_live:
        yield


def fetch_available_models(
    session: dict,
    cancel_event: Optional[threading.Event] = None,
) -> tuple[list[dict], str]:
    """Fetch available models from the backend.

    Returns (models, endpoint). Tries the current endpoint first and keeps a
    few fallbacks for older backend builds.
    """
    backend_profile = get_backend_profile()
    backend_url = backend_profile.base_url
    headers, cookies = backend_profiles.request_auth(backend_profile, session)
    endpoints = (
        "/api/models",
        "/api/chat/models",
        "/api/ai/models",
        "/api/model",
    )

    last_error = ""
    for endpoint in endpoints:
        if cancel_event is not None and cancel_event.is_set():
            raise BlockingOperationCancelled("Model fetch cancelled")
        try:
            resp = requests.get(
                f"{backend_url}{endpoint}",
                headers=headers,
                cookies=cookies,
                timeout=20,
                allow_redirects=False,
            )
        except requests.RequestException as e:
            last_error = str(e)
            continue
        if cancel_event is not None and cancel_event.is_set():
            raise BlockingOperationCancelled("Model fetch cancelled")

        if resp.status_code == 404:
            last_error = f"HTTP 404 from {endpoint}"
            continue
        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            continue

        try:
            data = resp.json()
        except ValueError:
            last_error = f"Non-JSON response from {endpoint}: {resp.text[:200]}"
            continue

        models = _extract_model_entries(data)
        if not models and not (
            isinstance(data, dict) and (
                "models" in data or "data" in data or "items" in data or
                "result" in data or "providers" in data
            )
        ):
            last_error = f"Unexpected response shape from {endpoint}"
            continue
        models = [m for m in models if m.get("id")]
        return models, endpoint

    raise RuntimeError(last_error or "No model endpoint responded")


# ── Mail: /mail command support (thin client over the gateway's mailbox) ──

def fetch_mail_inbox(session: dict, unread_only: bool = False) -> tuple[list[dict], str]:
    """Returns (messages, error). error is "" on success."""
    profile = get_backend_profile()
    headers, cookies = backend_profiles.request_auth(profile, session)
    try:
        resp = requests.get(
            f"{profile.base_url}/api/agent/inbox",
            params={"unread_only": "true" if unread_only else "false"},
            headers=headers, cookies=cookies, timeout=10,
        )
    except requests.RequestException as e:
        return [], str(e)
    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        return resp.json().get("messages", []), ""
    except ValueError:
        return [], "Non-JSON response from /api/agent/inbox"


def ack_mail_read(session: dict, email_ids: list[str]) -> None:
    """Best-effort — a failed ack just means the message shows up again."""
    profile = get_backend_profile()
    headers, cookies = backend_profiles.request_auth(profile, session)
    try:
        requests.post(
            f"{profile.base_url}/api/agent/inbox/ack",
            json={"email_ids": email_ids},
            headers=headers, cookies=cookies, timeout=10,
        )
    except requests.RequestException:
        pass


def send_mail(session: dict, subject: str, body: str) -> tuple[bool, str]:
    """Returns (ok, error). error is "" on success."""
    profile = get_backend_profile()
    current = get_current_agent()
    terminal = (getattr(current, "home_terminal", None) or "") if current else ""
    agent_name = (getattr(current, "name", None) or "Laintas CLI") if current else "Laintas CLI"
    headers, cookies = backend_profiles.request_auth(profile, session)
    try:
        resp = requests.post(
            f"{profile.base_url}/api/agent/send-email",
            json={"subject": subject[:200], "body": body[:5000],
                  "system": "laintas_cli", "terminal": terminal, "agent": agent_name},
            headers=headers, cookies=cookies, timeout=10,
        )
    except requests.RequestException as e:
        return False, str(e)
    if resp.status_code >= 300:
        try:
            detail = resp.json().get("detail") or resp.text[:200]
        except ValueError:
            detail = resp.text[:200]
        return False, detail
    return True, ""


# ── Mail-mode watcher: wake the idle loop on new mail, don't wait for the ──
# ── user to start an unrelated task and happen to check the inbox then.  ──
_MAIL_WATCHER_POLL_INTERVAL = 25  # seconds
_mail_watcher_thread: Optional[threading.Thread] = None
_mail_watcher_stop = threading.Event()


def _start_mail_watcher(session: dict):
    """Background poller: while /mode mail is active, new mail should start
    a turn on its own — not sit until the user separately begins some other
    task and the AI happens to check mail.check_inbox then. Runs for the
    whole process lifetime (self-gates on the active mode each cycle) so it
    doesn't need wiring into every /mode switch branch; only does anything
    while mode=mail and logged in."""
    global _mail_watcher_thread
    if _mail_watcher_thread is not None and _mail_watcher_thread.is_alive():
        return
    _mail_watcher_stop.clear()
    seen_ids: set[str] = set()

    def _watch():
        while not _mail_watcher_stop.wait(_MAIL_WATCHER_POLL_INTERVAL):
            try:
                if not mode_manager.is_mail_mode():
                    continue
                current_session = load_session() or session
                if not current_session.get("userId"):
                    continue
                messages, error = fetch_mail_inbox(current_session, unread_only=True)
                if error:
                    continue
                new_messages = [m for m in messages
                                if m.get("email_id") and m["email_id"] not in seen_ids]
                if not new_messages:
                    continue
                for m in new_messages:
                    seen_ids.add(m["email_id"])
                block = "\n\n".join(
                    f"From: {m.get('from', '?')}\n"
                    f"Subject: {m.get('subject', '(no subject)')}\n"
                    f"{m.get('body', '')}"
                    for m in new_messages
                )
                plural = "s" if len(new_messages) != 1 else ""
                task_text = (
                    f"[Mail mode] {len(new_messages)} new email{plural} arrived while idle "
                    f"— read it below and respond appropriately (reply via mail.send_to_user "
                    f"if needed, or act on the request):\n\n{block}"
                )
                _enqueue_user_input(task_text)
            except Exception:
                continue  # a watcher hiccup must never take down the process

    _mail_watcher_thread = threading.Thread(
        target=_watch, daemon=True, name="mail-watcher")
    _mail_watcher_thread.start()


def _stop_mail_watcher():
    _mail_watcher_stop.set()


def show_model_selector(models: list[dict], current: str = "") -> Optional[dict]:
    """Interactive model selector. Returns the complete model row or None.

    Prepends an ``auto`` virtual entry at the start of the list. When the user
    selects it, returns ``{"id": "auto", "provider": ""}`` so the caller
    can set the terminal to auto-routing mode.
    """
    if not models:
        return None
    labels = []
    sel_idx = 0
    # Prepend the auto-routing virtual entry at the start of the list,
    # using the same 30-char formatting as real models.
    auto_mark = " *" if current in ("auto", "") else "  "
    labels.append(f"{auto_mark}[cyan]{'auto-routing':30}[/cyan] Auto-routing (embedding-based)")
    if current in ("auto", ""):
        sel_idx = 0
    for i, model in enumerate(models):
        model_id = model.get("id", "")
        provider = model.get("description") or model.get("provider") or ""
        mark = " *" if current and model_id == current else "  "
        labels.append(f"{mark}[cyan]{model_id:30}[/cyan] {provider}")
        if current and model_id == current:
            sel_idx = i + 1  # +1 because auto occupies index 0
    chosen = select_dialog(
        labels,
        title=f"Models — choose with {symbols.ARROW_U}{symbols.ARROW_D} and Enter",
        full_screen=True,
        selected_index=sel_idx,
        hint=f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ select  Esc/q cancel",
    )
    if chosen is None:
        return None
    # If the user picked the auto virtual entry (index 0), return a synthetic record.
    if labels.index(chosen) == 0:
        return {"id": "auto", "provider": ""}
    return models[labels.index(chosen) - 1]


def choose_login_method() -> Optional[str]:
    """Use browser OAuth exclusively; public CLI binaries cannot protect passwords."""
    return "remote"


# ── Authentication ──────────────────────────────────────────────────────

def verify_session(session: dict) -> Optional[dict]:
    """Verify a saved session token with laintas.com. Returns {id, name, email} or None."""
    cookies = session.get("cookies", {})
    headers = session.get("headers", {})
    token = session.get("token", "")
    # Call get-session to get full user info.
    req_args = None
    # Current SSO cookie first, then legacy names for a smooth migration.
    for cookie_name in [
        "__Secure-laintas-v2.session_token",
        "laintas-v2.session_token",
        "__Secure-better-auth.session_token",
        "better-auth.session_token",
    ]:
        tok = cookies.get(cookie_name, "")
        if tok:
            req_args = {"cookies": {cookie_name: tok}}
            break
    if not req_args and headers.get("Authorization"):
        req_args = {"headers": headers}

    if req_args:
        try:
            resp = requests.get(f"{LAINTAS_BASE}/api/auth/get-session",
                                timeout=5, allow_redirects=False, **req_args)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, dict):
                    user = data.get("user") or {}
                    uid = user.get("id")
                    if uid:
                        return {
                            "id": uid,
                            "name": user.get("name", ""),
                            "email": user.get("email", ""),
                        }
        except requests.RequestException:
            pass

    return None


def resolve_session_from_token(token: str, resp_cookies=None) -> Optional[dict]:
    """Turn a raw token (and optional response cookies) into a verified session.

    Better Auth's get-session needs a *signed* cookie value or a Bearer token.
    The JSON `token` from sign-in / cli-exchange is unsigned, so a single guess
    at the cookie name is fragile. Try, in order:
      1. the signed cookie returned in the HTTP response (most reliable),
      2. the token as __Secure- / non-prefixed session cookie,
      3. the token as an Authorization: Bearer header.
    Returns a verified session dict (with userId/userName/userEmail) or None.
    """
    token = (token or "").strip()
    if not token:
        return None

    candidates = []
    # 1. Signed cookie straight from the response, if the server set one.
    if resp_cookies:
        for cookie_name in (
            "__Secure-laintas-v2.session_token",
            "laintas-v2.session_token",
            "__Secure-better-auth.session_token",
            "better-auth.session_token",
        ):
            signed = resp_cookies.get(cookie_name, "")
            if signed:
                candidates.append({"cookies": {cookie_name: signed}, "headers": {}})
    # 2. The raw token as each cookie name.
    candidates.append({"cookies": {"__Secure-laintas-v2.session_token": token}, "headers": {}})
    candidates.append({"cookies": {"laintas-v2.session_token": token}, "headers": {}})
    candidates.append({"cookies": {"__Secure-better-auth.session_token": token}, "headers": {}})
    candidates.append({"cookies": {"better-auth.session_token": token}, "headers": {}})
    # 3. The raw token as a Bearer header.
    candidates.append({"cookies": {}, "headers": {"Authorization": f"Bearer {token}"}})

    for candidate in candidates:
        candidate["token"] = token
        user_info = verify_session(candidate)
        if user_info:
            candidate["userId"] = user_info["id"]
            candidate["userName"] = user_info.get("name", "")
            candidate["userEmail"] = user_info.get("email", "")
            return candidate

    return None


def solve_captcha_challenge() -> Optional[str]:
    """Return an ALTCHA-compatible captcha response for local CLI login."""
    try:
        resp = requests.get(
            f"{ACCOUNTS_BASE}/api/captcha-challenge", timeout=10,
            allow_redirects=False)
        if resp.status_code != 200:
            return None
        challenge = resp.json()
    except (requests.RequestException, ValueError):
        return None

    algorithm = str(challenge.get("algorithm", "")).upper()
    target = str(challenge.get("challenge", ""))
    salt = str(challenge.get("salt", ""))
    try:
        maxnumber = int(challenge.get("maxnumber", 0))
    except (TypeError, ValueError):
        return None

    if algorithm != "SHA-256" or not target or not salt or maxnumber < 0:
        return None

    started = time.time()
    for number in range(maxnumber + 1):
        digest = hashlib.sha256(f"{salt}{number}".encode("utf-8")).hexdigest()
        if digest == target:
            payload = {
                "algorithm": challenge.get("algorithm", "SHA-256"),
                "challenge": target,
                "number": number,
                "salt": salt,
                "signature": challenge.get("signature", ""),
                "took": int((time.time() - started) * 1000),
            }
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            return base64.b64encode(raw).decode("ascii")

    return None


def build_login_payload(username: str, password: str, captcha_response: str) -> dict:
    """Build a login payload compatible with current and older captcha checks."""
    return {
        "username": username.strip(),
        "password": password,
        # The backend has used both names across versions. Keep both so old
        # builds and the current web auth middleware can agree.
        "captchaResponse": captcha_response,
        "captcha": captcha_response,
    }


def login_interactive() -> Optional[dict]:
    """Compatibility shim: direct password/token login is disabled."""
    console.print(
        "[yellow]Direct terminal password login is disabled. "
        "Use browser OAuth with PKCE instead.[/yellow]")
    return None


def _login_via_device(
    cancel_event: Optional[threading.Event] = None,
) -> Optional[dict]:
    """Authenticate through a browser on any device using device polling."""
    try:
        started = requests.post(
            f"{ACCOUNTS_BASE}/api/auth/cli-device/start",
            json={}, timeout=10, allow_redirects=False,
        )
        if cancel_event is not None and cancel_event.is_set():
            return None
        if started.status_code != 200:
            console.print(f"[red]Could not start browser login (HTTP {started.status_code}).[/red]")
            return None
        payload = started.json()
        device_id = str(payload.get("deviceId") or "")
        device_secret = str(payload.get("deviceSecret") or "")
        login_url = str(payload.get("verificationUri") or "")
        if not device_id or not device_secret or not login_url:
            console.print("[red]Browser login returned an incomplete device authorization.[/red]")
            return None
        expires = max(30, min(int(payload.get("expiresIn", 600)), 900))
        interval = max(1.0, min(float(payload.get("interval", 2)), 10.0))
    except (requests.RequestException, ValueError, TypeError) as exc:
        if cancel_event is not None and cancel_event.is_set():
            return None
        console.print(f"[red]Cannot start browser login: {exc}[/red]")
        return None

    console.print(
        "[bold]Open this URL in any browser to sign in:[/bold]\n"
        f"[link={login_url}]{login_url}[/link]\n"
        f"[dim]Waiting for approval (expires in {expires // 60} minutes)…[/dim]"
    )
    try:
        webbrowser.open(login_url)
    except Exception:
        # The URL is already printed; headless/SSH environments can paste it
        # into a browser without requiring a local GUI.
        pass

    deadline = time.monotonic() + expires
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return None
        try:
            response = requests.post(
                f"{ACCOUNTS_BASE}/api/auth/cli-device/poll",
                json={"deviceId": device_id, "deviceSecret": device_secret},
                timeout=10, allow_redirects=False,
            )
            if cancel_event is not None and cancel_event.is_set():
                return None
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "approved":
                    cookie = str(data.get("cookie") or "")
                    cookie_pair = cookie.split(";", 1)[0]
                    if "=" not in cookie_pair or not data.get("token"):
                        console.print("[red]Browser login returned an incomplete session.[/red]")
                        return None
                    name, value = cookie_pair.split("=", 1)
                    session = {
                        "token": data["token"],
                        "cookies": {name: value},
                        "headers": {},
                        "userId": (data.get("user") or {}).get("id", ""),
                        "userName": (data.get("user") or {}).get("name", ""),
                        "userEmail": (data.get("user") or {}).get("email", ""),
                    }
                    if not verify_session(session):
                        console.print("[red]The new CLI session could not be verified.[/red]")
                        return None
                    save_session(session)
                    display = session.get("userEmail") or session.get("userName") or "Laintas user"
                    console.print(f"[green]Logged in as {display}[/green]")
                    return session
            elif response.status_code in (400, 401, 404):
                console.print("[yellow]Browser login expired or was cancelled.[/yellow]")
                return None
        except (requests.RequestException, ValueError):
            # A transient network failure should not invalidate a live device
            # authorization; retry until the server-side expiry.
            pass
        if cancel_event is not None:
            if cancel_event.wait(interval):
                return None
        else:
            time.sleep(interval)
    console.print("[yellow]Browser login timed out.[/yellow]")
    return None


def login_via_browser(
    cancel_event: Optional[threading.Event] = None,
) -> Optional[dict]:
    # Device authorization works when CLI and browser are on different hosts.
    # The legacy localhost implementation remains below for source-history
    # compatibility, but is intentionally not selected: it cannot provide the
    # cross-terminal guarantee this login path requires.
    device_session = _login_via_device(cancel_event=cancel_event)
    if device_session is not None:
        return device_session
    return None


def _login_via_browser_local() -> Optional[dict]:
    """Authenticate through accounts.laintas.com using OAuth Code + PKCE."""
    # Find a free port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    callback_url = f"http://127.0.0.1:{port}/callback"
    result = {"code": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/callback":
                self._respond(404, "<h1>Not Found</h1>")
                return
            if params.get("state", [None])[0] != state:
                result["error"] = "OAuth state validation failed"
                self._respond(400, "<h1>Login Failed</h1><p>Invalid OAuth state.</p>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            oauth_error = params.get("error", [None])[0]
            if oauth_error:
                result["error"] = f"Authorization failed: {oauth_error}"
                self._respond(403, "<h1>Login Cancelled</h1><p>Authorization was not completed.</p>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            code = params.get("code", [None])[0]
            if code:
                result["code"] = code
                self._respond(200, "<h1>Login Successful</h1><p>You can close this tab and return to the terminal.</p>")
            else:
                result["error"] = "No authorization code in OAuth callback"
                self._respond(400, "<h1>Error</h1><p>No authorization code was received.</p>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _respond(self, http_code, body):
            self.send_response(http_code)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif;text-align:center;padding-top:3em'>{body}</body></html>".encode())

        def log_message(self, format, *args):
            pass

    _oauth_params = urlencode({
        'client_id': 'laintas-cli',
        'redirect_uri': callback_url,
        'response_type': 'code',
        'scope': 'openid profile email',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })
    login_url = f"{ACCOUNTS_BASE}/api/auth/oauth2/authorize?{_oauth_params}"

    console.print(Panel(
        f"[bold]Opening browser for login[/bold]\n\n"
        f"If your browser doesn't open automatically, visit:\n"
        f"[link={login_url}]{login_url}[/link]\n\n"
        f"[dim]Waiting for authentication... (timeout in 2 minutes)[/dim]",
        title="Laintas Auth"
    ))

    webbrowser.open(login_url)

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    server.timeout = 120

    try:
        server.handle_request()
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C while waiting for the browser callback is a cancellation of
        # login, not a process-level failure.  In particular, KeyboardInterrupt
        # is a BaseException and is not caught by ``except Exception``.
        result["error"] = "Login cancelled"
    except OSError:
        # The callback server may be closed by a concurrent shutdown/cleanup.
        # Treat that as an unavailable login attempt and return to the REPL.
        result["error"] = "Login cancelled"
    finally:
        server.server_close()

    if result["code"]:
        # Exchange the OAuth code, then mint a standard short-lived CLI session.
        try:
            resp = requests.post(
                f"{ACCOUNTS_BASE}/api/auth/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": result["code"],
                    "code_verifier": verifier,
                    "redirect_uri": callback_url,
                    "client_id": "laintas-cli",
                },
                timeout=10,
                allow_redirects=False,
            )
            if resp.status_code == 200:
                try:
                    oauth_data = resp.json()
                except ValueError:
                    oauth_data = {}
                access_token = oauth_data.get("access_token", "")
                if not access_token:
                    console.print("[red]OAuth exchange returned no access token.[/red]")
                    return None

                session_resp = requests.post(
                    f"{ACCOUNTS_BASE}/api/sso/cli-session",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"idToken": oauth_data.get("id_token", "")},
                    timeout=10,
                    allow_redirects=False,
                )
                if session_resp.status_code != 200:
                    console.print(f"[red]CLI session exchange failed (HTTP {session_resp.status_code}).[/red]")
                    return None
                data = session_resp.json()
                token = data.get("token", "")
                user = data.get("user") or {}
                signed = (session_resp.cookies.get("__Secure-laintas-v2.session_token")
                          or session_resp.cookies.get("laintas-v2.session_token")
                          or "")
                if not token or not signed:
                    console.print("[red]CLI session exchange returned an incomplete session.[/red]")
                    return None
                session = {
                    "token": token,
                    "cookies": {"__Secure-laintas-v2.session_token": signed},
                    "headers": {},
                    "userId": user.get("id", ""),
                    "userName": user.get("name", ""),
                    "userEmail": user.get("email", ""),
                }
                if not verify_session(session):
                    console.print("[red]The new CLI session could not be verified.[/red]")
                    return None
                save_session(session)
                display = (session.get("userEmail") or session.get("userName")
                           or session.get("userId") or "Laintas user")
                console.print(f"[green]Logged in as {display}[/green]")
                return session
            else:
                console.print(f"[red]OAuth code exchange failed (HTTP {resp.status_code}).[/red]")
                return None
        except requests.RequestException as e:
            console.print(f"[red]Cannot reach {ACCOUNTS_BASE} for token exchange: {e}[/red]")
            return None

    if result["error"]:
        console.print(f"[red]{result['error']}[/red]")
    else:
        console.print("[yellow]Login timed out.[/yellow]")
    return None


def ensure_auth() -> Optional[dict]:
    """Ensure the user is authenticated, returning a session or ``None``.

    Flow: cached session → return if valid.
          No cache → ask for login method → browser or terminal.
          Expired cache → clear and return None (use /login to re-auth).
    """
    # 1. Try cached session
    session = load_session()
    if session:
        user_info = verify_session(session)
        if user_info:
            session["userId"] = user_info["id"]
            session["userName"] = user_info.get("name", "")
            session["userEmail"] = user_info.get("email", "")
            return session
        else:
            console.print("[yellow]Session expired. Use /login to re-authenticate.[/yellow]")
            clear_session()
            return None

    # 2. No cached account — perform one browser/device authorization attempt.
    # Repeating a two-minute OAuth wait automatically is confusing and can
    # open multiple browser tabs.  Users can explicitly retry with /login.
    choice = choose_login_method()
    if choice is None:
        console.print("[dim]Login cancelled.[/dim]")
        return None
    if choice == "remote":
        try:
            session = login_via_browser()
        except (KeyboardInterrupt, EOFError):
            # A cancellation from browser launch/network setup is still a
            # normal login cancellation; it must not escape into main().
            console.print("[dim]Login cancelled.[/dim]")
            return None
        if session:
            return session
        console.print("[yellow]Remote login failed. Run /login to retry.[/yellow]")

    # Authentication is optional at startup (for example when the browser is
    # unavailable or the user presses Ctrl+C).  Keep the CLI alive so the user
    # can retry with /login or use a local backend instead of seeing an abrupt
    # process exit.
    console.print("[yellow]Authentication unavailable. Continuing without login; "
                  "run /login to retry.[/yellow]")
    return None


# ── CLI Prompt Template (.laintas/cli.prop) ──────────────────────────────

EXTRA_COMMAND_TEMPLATE = '''# .laintas/commands.py - define custom slash commands for the REPL
# context keys: session, interactive_session, agent_registry, console,
#   get_terminal, get_all_terminals, unregister_terminal, register_terminal,
#   rename_terminal, get_agent, get_all_agents, get_current_agent,
#   station_agent, unstation_agent,
#   SubTerminalSession, observe_session, enter_session,
#   _show_terminal_detail,
#   get_config, set_config, list_config, reset_config, reload_default_files
#
# Return True from handle_extra_command to indicate the command was handled.
# Return False to fall through to "Unknown command".
# Built-in commands like /config and /reload are intercepted before this
# file is consulted, so overriding them here has no effect.


def handle_extra_command(action, parts, ctx):
    """Custom slash command handler. Return True if handled, False to pass through."""
    console = ctx["console"]

    # Example: a simple greeting command.
    # if action == "/hello":
    #     name = parts[1] if len(parts) > 1 else "world"
    #     console.print(f"[green]Hello, {name}![/green]")
    #     return True

    return False
'''

LOOP_COMMAND_TEMPLATE = """import os
import re
import json
import subprocess
import sys


def handle_loop_command(command, ctx):
    \"\"\"Handle custom loop commands defined in .laintas/loop.py.\"\"\"

    # parent(<shell command>) \\u2014 execute in parent terminal context (side effects like cd/clear)
    m = re.match(r'^parent\\((.+)\\)\\s*$', command)
    if m:
        parent_cmd = m.group(1).strip()
        if ctx.get("depth", 0) == 0:
            return _execute_parent_command(parent_cmd)
        else:
            return f"__PARENT_CMD__:{parent_cmd}"

    # learn(<text|path|url>) — extract knowledge into conversation history as [KNOWLEDGE]
    m = re.match(r'^learn\\((.+)\\)\\s*$', command)
    if m:
        return _learn(m.group(1).strip(), ctx)

    # forget(N) — trim chat_history, keeping knowledge + last N messages
    m = re.match(r'^forget\\((\\d+)\\)\\s*$', command)
    if m:
        return _forget(int(m.group(1)), ctx)

    return None


def _execute_parent_command(cmd):
    \"\"\"Execute a command in the parent process context so side effects
    (cd, clear) apply to the parent terminal, not a child PTY.\"\"\"
    stripped = cmd.strip()
    if stripped in ("cd",) or stripped.startswith("cd "):
        path = stripped[3:].strip() if stripped.startswith("cd ") else os.path.expanduser("~")
        try:
            os.chdir(path)
            return f"cd -> {os.getcwd()}"
        except Exception as e:
            return f"cd error: {e}"
    if stripped in ("clear",):
        sys.stdout.write("\\033[2J\\033[H")
        sys.stdout.flush()
        return ""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
            cwd=os.getcwd(),
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return f"Parent command timed out: {cmd}"
    except Exception as e:
        return f"Parent command error: {e}"


def _learn(target, ctx):
    \"\"\"Extract text from a file, URL, image (OCR), or raw string and add to conversation history as [KNOWLEDGE].\"\"\"
    chat_history = ctx.get("chat_history", [])

    # 1. URL
    if target.startswith(('http://', 'https://')):
        try:
            result = subprocess.run(
                ['curl', '-sL', '--max-time', '30', target],
                capture_output=True, text=True, timeout=35
            )
            text = result.stdout[:50000]
            source = target
        except Exception as e:
            return f"learn() failed to fetch URL: {e}"

    # 2. Existing file
    elif os.path.isfile(target):
        path = target
        ext = os.path.splitext(path)[1].lower()
        source = os.path.abspath(path)

        # Text files (code, config, logs, data)
        if ext in ('.txt', '.md', '.py', '.js', '.ts', '.jsx', '.tsx', '.json',
                     '.xml', '.html', '.css', '.scss', '.less',
                     '.yaml', '.yml', '.toml', '.cfg', '.ini', '.conf',
                     '.sh', '.bash', '.zsh', '.fish', '.log', '.csv', '.tsv',
                     '.sql', '.r', '.rb', '.go', '.rs', '.java', '.c', '.cpp',
                     '.h', '.hpp', '.php', '.swift', '.kt', '.scala', '.clj',
                     '.env', '.gitignore', '.dockerignore', '.editorconfig'):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()[:50000]
            except Exception as e:
                return f"learn() failed to read file: {e}"

        # PDF
        elif ext == '.pdf':
            try:
                result = subprocess.run(
                    ['pdftotext', path, '-', '-l', '50'],
                    capture_output=True, text=True, timeout=30
                )
                text = result.stdout[:50000]
                if not text.strip():
                    return "learn() could not extract text from PDF (empty or image-based, try OCR)"
            except FileNotFoundError:
                return "learn() needs 'pdftotext' (poppler-utils). Install: apt install poppler-utils"
            except Exception as e:
                return f"learn() failed on PDF: {e}"

        # Images — OCR
        elif ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp'):
            try:
                result = subprocess.run(
                    ['tesseract', path, 'stdout'],
                    capture_output=True, text=True, timeout=30
                )
                text = result.stdout.strip()[:50000]
                if not text:
                    return "learn() OCR found no text in image"
            except FileNotFoundError:
                return "learn() needs 'tesseract' for images. Install: apt install tesseract-ocr"
            except Exception as e:
                return f"learn() failed on image: {e}"

        # Video — metadata + subtitle scan
        elif ext in ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv'):
            try:
                result = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                     '-show_format', '-show_streams', path],
                    capture_output=True, text=True, timeout=15
                )
                info = json.loads(result.stdout)
                sub_lines = []
                for s in info.get('streams', []):
                    if s.get('codec_type') == 'subtitle':
                        lang = s.get('tags', {}).get('language', 'unknown')
                        sub_lines.append(
                            f"  Subtitle: {s.get('codec_name', '?')} lang={lang}"
                        )
                meta = info.get('format', {}).get('tags', {})
                meta_text = '\\n'.join(f"  {k}: {v}" for k, v in meta.items())

                text = f"Video: {os.path.basename(path)}\\n"
                if meta_text:
                    text += f"Metadata:\\n{meta_text}\\n"
                if sub_lines:
                    text += f"Subtitle tracks:\\n" + '\\n'.join(sub_lines) + "\\n"
                    text += "(Extract with: ffmpeg -i <video> -map 0:s:<N> subs.srt)"
                else:
                    text += "No embedded subtitles found. To learn from audio:\\n"
                    text += "  ffmpeg -i <video> -vn -acodec pcm_s16le audio.wav\\n"
                    text += "  whisper audio.wav --model small"
                text = text[:50000]
            except FileNotFoundError:
                return "learn() needs 'ffprobe' (ffmpeg). Install: apt install ffmpeg"
            except Exception as e:
                return f"learn() failed on video: {e}"

        # Unknown extension — try as text
        else:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()[:50000]
            except Exception as e:
                return f"learn() cannot handle '{ext}' files. Try .txt, .md, .pdf, .png, .mp4, etc."

    # 3. Raw text (user's sentence / knowledge)
    else:
        text = target[:50000]
        source = "user-provided knowledge"

    # Append knowledge to conversation history (short-term, cleared on session end)
    knowledge_text = text[:5000]  # cap per entry to keep prompts manageable
    chat_history.append({
        "role": "knowledge",
        "content": f"(Source: {source})\\n{knowledge_text}"
    })

    preview = text[:300].replace('\\n', ' ')
    return f"Learned ({len(text)} chars) from: {source}\\nPreview: {preview}..."


def _forget(keep_n, ctx):
    \"\"\"Trim chat_history: keep all [KNOWLEDGE] entries + last N regular messages.\"\"\"
    chat_history = ctx.get("chat_history", [])
    if not chat_history or keep_n < 0:
        return f"Nothing to forget (history has {len(chat_history)} messages)"

    # Separate knowledge entries from regular ones
    knowledge_entries = [m for m in chat_history if m.get('role') == 'knowledge']
    regular_entries = [m for m in chat_history if m.get('role') != 'knowledge']

    # Keep last N regular entries
    trimmed_regular = regular_entries[-keep_n:] if keep_n > 0 else []

    # Rebuild: knowledge first, then recent regular messages
    trimmed = knowledge_entries + trimmed_regular
    removed = len(chat_history) - len(trimmed)

    # Mutate in-place
    chat_history.clear()
    chat_history.extend(trimmed)

    return f"Forgot {removed} messages ({len(knowledge_entries)} knowledge kept, {len(trimmed_regular)} recent kept, {len(chat_history)} total)"
"""


def generate_cli_prop_template() -> str:
    """Generate the .laintas/cli.prop system prompt template for the current OS.

    The template uses XML-style sections for product behavior. Native tool
    schemas are authoritative for names and parameters; specialized terminal,
    agent and workflow manuals are loaded progressively through skills.

    Variables substituted at run time (see agent_loop.run_agent_loop):
      {{agentName}} {{agentId}} {{currentPath}} {{depth}}
      {{globalMemory}} {{persistentMemory}} {{durableRules}} {{lastSession}}
      {{planMode}} {{tools}} {{inbox}} {{parallelResults}} {{children}} {{parent}}
      {{terminalName}} {{parentTerminal}} {{deploymentStatus}}
      {{workflowPhase}} {{rolePrompt}} {{confidenceGuidance}}
      {{skillContext}} {{promptOpt}}

    {{nextDepth}}, {{activeFile}}, and {{behaviorDiagnostics}} are still computed
    and .replace()'d by run_agent_loop for backward compatibility, but are not
    referenced by this template — {{activeFile}} only ever resolves to the literal
    string "None" and {{behaviorDiagnostics}} is always "", so neither carries
    real signal yet. Add them back here if/when they're wired to real values.
    """
    shell_info = SHELL_NAME

    return f"""<!-- laintas-managed-prompt:v2 -->
<role>
You are {{{{agentName}}}} (id: {{{{agentId}}}}, role: {{{{deploymentStatus}}}}), an autonomous coding agent running in laintas-cli.
Solve real engineering tasks by reading the repo, using tools, editing files, running commands, verifying results, and reporting briefly.
</role>

<environment>
- OS: {SYSTEM} | Shell: {shell_info} | CWD: {{{{currentPath}}}}
- Terminal: {{{{terminalName}}}} | Parent terminal: {{{{parentTerminal}}}}
- Depth: {{{{depth}}}} | Parent agent: {{{{parent}}}} | Children: {{{{children}}}}
- Inbox: {{{{inbox}}}}
{{{{parallelResults}}}}
- Plan mode: {{{{planMode}}}}
- Current date/time is appended by the runtime.
</environment>

<memory>
Persistent memory:
{{{{persistentMemory}}}}

Durable user rules (structured, active until explicitly cancelled or replaced):
{{{{durableRules}}}}

Project rules:
{{{{globalMemory}}}}

Last session:
{{{{lastSession}}}}
</memory>

<skills>
{{{{skills}}}}

If a skill looks relevant, call `skill_load` with its name before doing specialized work.
Do not assume unloaded skill instructions. After loading, continue using the instructions below.
When done with that specialized work, call `skill_unload` with its name to free context; skills can be re-loaded later.

Loaded skill instructions:
{{{{skillContext}}}}
</skills>

<tools>
Use `shell` for shell commands and meta-commands; use structured tools for file, memory, task, plan, web, agent, terminal, and time operations.
How to use the shared core tools (reading/editing/verifying files, shell, memory, tasks, web) is in the injected <core_tool_usage> block; general operating discipline (act-first, batching, failure handling, scope, safety, verification, skills) is in the injected <agent_conduct> block — follow both.
Tools beyond that core (sub-terminals, parallel agent orchestration, plan files, …) have their detailed usage in skills: call `skill_list` to see them and `skill_load` (with `name`) to load one before that specialized work.
The native function schemas are authoritative for each tool's name, purpose and parameters. The compact list below is only an availability reminder:
{{{{tools}}}}
</tools>

<workflow>
- In PLAN mode, update the versioned plan and call `plan_submit`; do not create execution tasks before approval. Outside PLAN mode, follow the runtime-owned `<work_orchestration>` policy to choose TASK, HWO, or HWG. Keep one TASK item in progress per agent and complete it only after verification. `<approved_work_plan>` remains authoritative when present; `<active_tasks>` is the current session/agent execution view.
- Follow-up messages in the current live context need no resume tool; proceed directly with the actual task. Interrupted or exhausted runs are resumed by the CLI's `/continue` command. Retained thread context may be compacted, so treat structured active state and durable rules as authoritative.
- If the user asks a clear read/edit/build/test/investigate task, act with tools. Do not ask for permission to do exactly what was asked.
- Ask one concise clarifying question only when the target or intent is genuinely ambiguous, destructive, impossible to infer safely, or blocked on information you cannot discover yourself.
- If there are multiple reasonable approaches with materially different tradeoffs, stop and present 2-3 labeled options. State the consequence of each option briefly, then wait for the user's choice.
{{{{workflowPhase}}}}
{{{{rolePrompt}}}}
{{{{confidenceGuidance}}}}
</workflow>

<output_rules>
(General act-first / no-transitional-narration / batching / language rules are in the injected <agent_conduct> block. The rules below are laintas loop-control specifics.)
- Your reply is OPTIONAL. Leave it empty on ordinary execution steps and just emit the tool call(s). Write user-facing text ONLY when: (a) you are giving the final answer/result, (b) you must ask the user a clarifying question, or (c) a non-obvious decision needs a one-line rationale. When you do write, cite files as path:line.
- Completion is an explicit act: call `task_complete` with a `summary` when — and only when — the task is fully finished. Do NOT stop just to narrate progress; if more work remains, include the next tool call in the SAME turn and keep going.
- Before calling `task_complete` on a task that modified code files, run the project's test suite (pytest, npm test, go test, etc.) to verify your changes. If tests fail, fix them before completing. If tests are not applicable, state why in the summary.
- If you have nothing concrete to run this turn but the task is NOT finished (still reasoning or planning), just reply with text and no tool call - the loop will continue automatically. Do NOT call task_complete unless the task is truly done.
- Ending your turn with no tool call continues the loop. Use this for conversational replies, asking the user a question, or when you need a turn to think before your next action.
</output_rules>

<safety>
Do not bypass policy.py decisions. Do not invent paths, APIs, files, or results. Claims about monitoring, tests, command success, or measured values must be grounded in returned tool output and an observed completion state; a started background command is not evidence of success. If collection fails, report the failure or rerun it instead of fabricating a plausible report. (General safety - reversibility/blast-radius, destructive-action confirmation, investigate-before-overwrite, no-vulnerabilities - is in the injected <agent_conduct> block.)
</safety>

<code_review>
When the task involves reviewing, auditing, or verifying code (yours or others'), work through this checklist systematically before reporting:
1. Read the full diff or changed files - do not review from memory or summary.
2. Verify correctness: trace logic paths, check edge cases (empty input, off-by-one, null/None, concurrency).
3. Check error handling: are failures caught, reported, and recoverable? Are resources released (files, connections, locks)?
4. Check tests: do existing tests cover the change? Run them. If new behavior is untested, note it.
5. Check naming, style, and conventions vs. surrounding code.
6. Check for security issues: injection, path traversal, secret exposure, unsafe deserialization.
7. Confirm no dead code, debug prints, or commented-out blocks were left behind.
Report findings as file:line references with severity (blocker/major/minor/nit).
</code_review>
{{{{promptOpt}}}}
"""

# ── File Helpers ───────────────────────────────────────────────────────

def _format_time_ago(ts: float) -> str:
    """Render a timestamp as a coarse 'N min/hours/days ago' string."""
    try:
        delta = max(0, int(time.time() - (ts or 0)))
    except (TypeError, ValueError):
        return "unknown"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} hour(s) ago"
    return f"{delta // 86400} day(s) ago"


def _resume_turn_count(blob: Optional[dict]) -> int:
    """Count actual user-to-agent prompt turns for status text."""
    if not blob:
        return 0
    stored = blob.get("turn_count")
    if isinstance(stored, int):
        return stored
    return len([
        m for m in (blob.get("chat_history") or [])
        if m.get("role") == "user"
        and m.get("input_kind") not in {"shell", "interactive", "slash"}
    ])


def _restore_resume_blob(blob: dict, chat_history: list) -> dict:
    """Restore full-fidelity conversation state from a per-cwd resume blob.

    Restores conversation + working state + the task plan, mirroring how
    Codex/Claude bring back a session: the transcript, the older-turn summary
    (so long sessions don't lose early goals), and the open todo/plan list.
    """
    chat_history.clear()
    # Prepend a digest of turns that were dropped past the resume window so the
    # early goals/instructions aren't lost on a long session.
    older = (blob.get("older_summary") or "").strip()
    if older:
        chat_history.append({"role": "knowledge", "content": f"[resumed session context]\n{older}"})
    chat_history.extend(blob.get("chat_history") or [])
    restored_session_id = str(
        blob.get("session_id")
        or (blob.get("state") or {}).get("_session_id") or "")
    try:
        if blob.get("active_work_id"):
            workgraph.set_active_work(
                str(blob["active_work_id"]), cwd=blob.get("cwd"),
                session_id=restored_session_id or None)
    except workgraph.WorkGraphError:
        pass
    # Rehydrate the plan (in_progress + pending tasks) as session tasks so
    # <active_tasks> shows it immediately and "continue" can resume it.
    try:
        task_manager.import_session_tasks(blob.get("tasks") or [],
                                          cwd=blob.get("cwd"),
                                          session_id=restored_session_id or None)
    except Exception:
        pass
    return prepare_state_for_repl(blob.get("state") or {})


def _reset_fresh_session_context(cwd: str) -> None:
    """Detach every independently persisted context source for a new session."""
    import plan_mode as fresh_plan_mode
    import workflow_engine as fresh_workflow

    if fresh_plan_mode.is_plan_mode():
        fresh_plan_mode.exit_plan_mode(approve=False)
    fresh_workflow.detach_active_workflow()
    task_manager.detach_active_tasks(cwd=cwd)


def _resume_choices(cwd: str) -> list:
    """Selectable resume states for this cwd, newest first.

    Returns all states (checkpoints + autosaves) so the user can resume from
    any saved point. Previously this filtered to only checkpoints when present,
    which hid newer autosaves created after a /q checkpoint.
    """
    return list_resume_states(cwd)


def _resolve_resume_selector(choices: list, selector: str) -> Optional[dict]:
    """Non-interactive resume selection by 'latest'/'last' or 1-based index.

    Returns the chosen blob, or None (printing a reason) when the selector is
    empty/invalid. Used by `--resume`/`--continue` and `/resume <N>`.
    """
    if not choices:
        return None
    selector = (selector or "").strip().lower()
    if selector in ("", "latest", "last"):
        return choices[0]
    if selector.isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(choices):
            return choices[idx]
        console.print(f"[red]Invalid resume number: {selector}[/red]")
        return None
    console.print(f"[red]Invalid resume selector: {selector}[/red]")
    return None


def _choose_resume_blob(cwd: str, selector: str = "") -> Optional[dict]:
    """Resolve a saved resume state non-interactively (CLI-flag entry point)."""
    return _resolve_resume_selector(_resume_choices(cwd), selector)


def show_resume_picker(cwd: str) -> Optional[dict]:
    """Full-screen `/t`-style picker for saved resume sessions.

    Arrow keys to navigate, Enter to resume the highlighted session, d for a
    details preview, x to delete a checkpoint, q/Esc to cancel. Returns the
    chosen blob, or None if cancelled.
    """
    choices = _resume_choices(cwd)
    if not choices:
        console.print("[yellow]No saved session to resume in this directory.[/yellow]")
        return None

    def _build_labels():
        labels = []
        for item in choices:
            kind = item.get("kind", "session")
            badge = "[magenta]◆ checkpoint[/magenta]" if kind == "checkpoint" else f"[blue]{symbols.DOT_OPEN} autosave[/blue]"
            turns = item.get("turn_count") or _resume_turn_count(item)
            ago = _format_time_ago(item.get("timestamp", 0))
            title = str(item.get("title") or "Untitled session")[:60].replace("\n", " ")
            labels.append(f"{badge}  [dim]{ago}[/dim]  {turns} turn(s)  [bold]{title}[/bold]")
        return labels

    sel_idx = 0
    status_msg = ""
    while choices:
        labels = _build_labels()
        hint = f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ resume  d details  x delete  q cancel"
        if status_msg:
            hint = f"{status_msg}\n{hint}"
        result = select_dialog(
            labels,
            title="Resume Session",
            full_screen=True,
            selected_index=sel_idx,
            action_keys={"d": "details", "x": "delete"},
            enter_action="resume",
            hint=hint,
        )
        if result is None:
            return None
        action, idx = result
        if action is None or idx < 0 or idx >= len(choices):
            return None
        item = choices[idx]
        status_msg = ""
        if action == "resume":
            return item
        if action == "details":
            with _alt_screen():
                _show_resume_detail(item)
                _print_resume_transcript(item, 20)
                input("\n[dim]Press Enter to continue...[/dim]")
            sel_idx = idx
        elif action == "delete":
            delete_resume_state(cwd, item)
            del choices[idx]
            if not choices:
                return None
            status_msg = "[green]Deleted saved session.[/green]"
            sel_idx = min(idx, len(choices) - 1)
    return None


def _show_resume_detail(item: dict) -> None:
    """Print a rich summary of one resume blob for the picker's details view."""
    console.print()
    console.print(f"[bold cyan]{item.get('title') or 'Untitled session'}[/bold cyan]")
    console.print(f"[dim]Type:[/dim] {item.get('kind', 'session')}   "
                  f"[dim]When:[/dim] {_format_time_ago(item.get('timestamp', 0))}   "
                  f"[dim]Turns:[/dim] {item.get('turn_count') or _resume_turn_count(item)}")
    history = item.get("chat_history") or []
    console.print(f"[dim]Events:[/dim] {len(history)}\n")
    for msg in history[-6:]:
        _print_resume_event(msg)
        console.print()


def _resume_role_style(role: str) -> str:
    """Map a chat role to a rich color for transcript rendering."""
    if role == "user":
        return "green"
    if role == "assistant":
        return "blue"
    if role == "knowledge":
        return "yellow"
    return "cyan"


_LEGACY_TOOL_EVENT_RE = re.compile(
    r"^\[(?P<call>call_[^\]]+)\]\s+"
    r"(?P<name>[^\s(]+)\((?P<summary>.*?)\)\s+→\s+(?P<result>.*)$",
    re.DOTALL,
)


def _resume_tool_event(message: dict) -> Optional[dict]:
    """Normalize new typed tools and legacy knowledge-tool records."""
    if message.get("role") == "tool":
        return {
            "name": str(message.get("display_name")
                        or message.get("tool_name") or "tool"),
            "summary": str(message.get("summary") or ""),
            "result": str(message.get("content") or ""),
            "ok": message.get("ok"),
            "legacy": False,
        }
    if message.get("role") != "knowledge":
        return None
    match = _LEGACY_TOOL_EVENT_RE.match(str(message.get("content") or ""))
    if not match:
        return None
    data = match.groupdict()
    summary = data["summary"].strip()
    duplicate_prefix = data["name"] + " "
    if summary.startswith(duplicate_prefix):
        summary = summary[len(duplicate_prefix):].strip()
    return {
        "name": data["name"],
        "summary": summary,
        "result": data["result"].strip(),
        "ok": None,
        "legacy": True,
    }


def _print_resume_event(message: dict) -> None:
    """Render one saved event with the same visual vocabulary as the REPL."""
    tool = _resume_tool_event(message)
    if tool is not None:
        status = f"[success]{symbols.DOT}[/success]" if tool["ok"] is not False else f"[error]{symbols.DOT}[/error]"
        summary = _md_escape(tool["summary"][:160])
        suffix = f"  [muted]{summary}[/muted]" if summary else ""
        console.print(
            f"  {status} [accent.dim]{_md_escape(tool['name'])}[/accent.dim]{suffix}",
            highlight=False,
        )
        result = tool["result"].strip()
        if result:
            result_style = (
                "error" if tool["ok"] is False or "error" in result.lower()
                else "muted"
            )
            console.print(Padding(
                Text(result[:500], style=result_style), (0, 0, 0, 4)
            ))
        return

    role = str(message.get("role") or "?")
    content = str(message.get("content") or "")
    if role == "user":
        input_kind = message.get("input_kind") or "prompt"
        if input_kind == "shell":
            console.print(f"[muted]$ {_md_escape(content)}[/muted]", highlight=False)
        elif input_kind == "interactive":
            console.print(f"[muted]› {_md_escape(content)}[/muted]", highlight=False)
        else:
            console.print(f"[accent]❯[/accent] {_md_escape(content)}", highlight=False)
    elif role == "assistant":
        console.print(Markdown(content or "*(empty)*"))
    elif role == "knowledge":
        console.print("[warning]context[/warning]")
        console.print(Padding(Text(content or "(empty)", style="muted"), (0, 0, 0, 2)))
    elif role == "shell":
        rc = message.get("returncode")
        style = "error" if isinstance(rc, int) and rc != 0 else "muted"
        console.print(Padding(Text(content or "(no output)", style=style), (0, 0, 0, 2)))
    else:
        console.print(f"[muted]{_md_escape(role)} {symbols.BULLET} {_md_escape(content)}[/muted]",
                      highlight=False)


def _print_resume_transcript(blob: dict, limit: Optional[int]) -> None:
    """Echo a resumed session's conversation (read-only).

    ``limit`` is the number of most-recent messages to show; ``None`` shows the
    whole transcript, and any value <= 0 prints nothing. Does not mutate the
    passed blob or any live chat history. The dropped-turn digest
    (``older_summary``) is shown only when the window reaches the start.
    """
    if limit is not None and limit <= 0:
        return
    history = blob.get("chat_history") or []
    total = len(history)
    if limit is None:
        window = list(history)
        at_start = True
    else:
        window = history[-limit:]
        at_start = len(window) >= total
    console.print()
    console.print(
        f"[dim]── conversation ── {len(window)}/{total} event(s) "
        f"{'(all)' if at_start else '(most recent)'} ──[/dim]"
    )
    older = (blob.get("older_summary") or "").strip()
    if at_start and older:
        console.print(f"[dim yellow][earlier session context]\n{older}[/dim yellow]\n")
    for msg in window:
        _print_resume_event(msg)
        console.print()


def read_file(path: str) -> Optional[str]:
    """Read a file, return None if not found."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


def write_file(path: str, content: str) -> None:
    """Write content to a file."""
    Path(path).write_text(content, encoding="utf-8")


def append_file(path: str, content: str) -> None:
    """Append content to a file."""
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + content + "\n")


def ensure_files_exist() -> None:
    """Create .laintas/ project directory with cli.prop, memory.json, commands.py, loop.py."""
    proj = paths.ensure_project_dir()

    cli_prop_path = paths.project_file(paths.CWD_CLI_PROP)
    memory_path = paths.project_file(paths.CWD_MEMORY)
    commands_path = paths.project_file(paths.CWD_COMMANDS)
    loop_path = paths.project_file(paths.CWD_LOOP)

    if not cli_prop_path.exists():
        template = generate_cli_prop_template()
        cli_prop_path.write_text(template, encoding="utf-8")
        console.print(f"[dim]Created {cli_prop_path}[/dim]")

    if not memory_path.exists():
        memory_path.write_text("", encoding="utf-8")
        console.print(f"[dim]Created {memory_path}[/dim]")

    if not commands_path.exists():
        commands_path.write_text(EXTRA_COMMAND_TEMPLATE, encoding="utf-8")
        console.print(f"[dim]Created {commands_path}[/dim]")

    if not loop_path.exists():
        loop_path.write_text(LOOP_COMMAND_TEMPLATE, encoding="utf-8")
        console.print(f"[dim]Created {loop_path}[/dim]")

    # Generated defaults are safe to execute until their bytes change. This
    # also migrates existing installations whose files still match defaults.
    trust_store.record_generated_file(commands_path, EXTRA_COMMAND_TEMPLATE)
    trust_store.record_generated_file(loop_path, LOOP_COMMAND_TEMPLATE)


def reload_default_files() -> None:
    """Delete all project files in .laintas/ and restart laintas_cli."""
    proj = paths.project_dir()
    existing = [proj / name for name in paths._ALL_CWD_FILES
                if (proj / name).exists()]
    if existing:
        import policy as _policy
        decision = _policy.evaluate_file_delete(str(proj), os.getcwd())
        if decision.action == "deny":
            console.print(f"[red]Blocked by policy: {decision.reason}[/red]")
            return
        if decision.action == "needs_approval":
            preview = "DELETE generated project files\n" + "\n".join(
                f"  {path}" for path in existing)
            if not request_file_delete_approval(
                    str(proj), preview, decision.reason):
                console.print("[yellow]Reload cancelled; no files were deleted.[/yellow]")
                return
    for name in paths._ALL_CWD_FILES:
        f = proj / name
        if f.exists():
            f.unlink()
            console.print(f"[dim]Deleted {f}[/dim]")
    # Remove .laintas/ directory if empty
    try:
        if proj.exists() and not any(proj.iterdir()):
            proj.rmdir()
    except OSError:
        pass
    console.print("[yellow]Restarting laintas_cli...[/yellow]")
    _restart_process()


# ── Backend API ────────────────────────────────────────────────────────

def get_auth_cookies(session: dict) -> dict:
    """Build cookies for backend API requests from session."""
    return session.get("cookies", {})


def get_auth_headers(session: dict) -> dict:
    """Build headers for backend API requests from session."""
    headers = {"Content-Type": "application/json"}
    if session.get("headers", {}).get("Authorization"):
        headers["Authorization"] = session["headers"]["Authorization"]
    return headers


def _repair_json_candidate(candidate: str) -> str:
    """Best-effort repair of the most common model JSON defects.

    `json.loads` is strict, but models reliably make two harmless mistakes that
    a coding agent triggers constantly:
      1. Literal control characters (newlines/tabs/carriage returns) inside
         string values — e.g. a multi-line code block or shell command dropped
         into `reply`/`command` without escaping to \\n. JSON forbids raw
         control chars in strings.
      2. Trailing commas before } or ].
    Both are losslessly fixable. We walk the text tracking string state so we
    only escape control chars *inside* strings (structural whitespace between
    tokens is left alone), then strip trailing commas. Unescaped inner quotes
    are NOT handled (ambiguous) and still fall through to a retry nudge.
    """
    out = []
    in_str = False
    esc = False
    for c in candidate:
        if in_str:
            if esc:
                out.append(c)
                esc = False
            elif c == "\\":
                out.append(c)
                esc = True
            elif c == '"':
                out.append(c)
                in_str = False
            elif c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            else:
                out.append(c)
        else:
            if c == '"':
                in_str = True
            out.append(c)
    repaired = "".join(out)
    # Strip trailing commas: ",}" / ",]" (with optional whitespace).
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    return repaired


def _native_to_tool_calls(frags: dict, name_map: Optional[dict] = None) -> list:
    """Reassemble OpenAI-native streamed tool_calls into laintas tool_calls.

    Some providers emit tool calls through the function-calling channel
    (`delta.tool_calls`) instead of inside the JSON body. Those fragments stream
    incrementally, keyed by `index`; each carries a partial `function.name` and a
    slice of `function.arguments`. We reassemble per index and parse the
    arguments JSON. Without this they are dropped and the model's action never
    runs — the model then re-requests the same read/edit forever.

    `name_map` un-mangles provider-safe names (e.g. `fs_write` → `fs.write`)
    back to canonical tool names — see ToolRegistry.to_openai_tools.
    """
    name_map = name_map or {}
    out = []
    for idx in sorted(frags):
        slot = frags[idx]
        name = (slot.get("name") or "").strip()
        # Some providers leave a bare name (e.g. "exec") but encode the
        # fully-qualified tool in the id, e.g. "functions.shell.exec:0".
        m = re.match(r"functions\.(.+?)(?::\d+)?$", slot.get("id") or "")
        if m and "." in m.group(1):
            name = m.group(1)
        # Un-mangle provider-safe names back to canonical (fs_write → fs.write).
        name = name_map.get(name, name)
        if not name:
            continue
        args_raw = (slot.get("arguments") or "").strip()
        if not args_raw:
            args = {}
        else:
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                try:
                    args = json.loads(_repair_json_candidate(args_raw))
                except json.JSONDecodeError:
                    args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        out.append({"name": name, "arguments": args})
    return out


def _extract_tagged_tool_calls(text: str) -> Optional[list]:
    """Best-effort compatibility for malformed `<tool_calls>...</tool_calls>` output.

    The prompt forbids this syntax, but older/model-regressed turns may emit it.
    Convert it to the canonical tool_calls list so the loop can continue.
    """
    if not text:
        return None
    m = re.search(r"<tool_calls>\s*(.*?)\s*</tool_calls>", text, re.I | re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        args = item.get("arguments") or {}
        out.append({"name": name, "arguments": args if isinstance(args, dict) else {"value": args}})
    return out or None


def call_backend_stream(
    session: dict,
    message: str,
    system_prompt: str,
    current_path: str,
    history: list = None,
    lang: str = "EN",
    on_chunk: Optional[Callable[[str, str], None]] = None,
    interrupt_event: Optional[threading.Event] = None,
    messages: Optional[list] = None,
    tools_enabled: bool = True,
    allowed_tool_names: Optional[set[str]] = None,
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
) -> dict:
    """Call Helpwo backend /api/chat/stream, same as Helpwo frontend.
    Returns parsed {reply, command, memory, done, _billing} dict.

    If interrupt_event is provided, it is checked between SSE chunks so the
    request can be aborted gracefully on Ctrl+C.

    If `messages` is provided (native message-thread mode), it is sent as the
    OpenAI-format `messages` array and the backend uses it verbatim (prepending
    systemPrompt as the system message). The legacy `message`/`history` fields
    are then ignored by the backend, so the model resumes a real conversation
    thread (assistant tool_calls + role:tool results) instead of a re-synthesized
    state-dump user turn.
    """
    backend_profile = get_backend_profile()
    backend_url = backend_profile.base_url

    payload = {
        "message": message,
        "history": history or [],
        "currentPath": current_path,
        "systemPrompt": system_prompt,
        "lang": lang,
        "maxTokens": int(get_runtime_config("max_tokens")),
        # Billing attribution: without this the gateway books the call under
        # its default product ("helpwo") — quota and /usage stats then miss it.
        "source": "cli",
        # Core-tool usage ("how to read/edit/verify files", etc.) is the
        # gateway's single source of truth — it appends the canonical guide to
        # the system message. The base prompt below intentionally omits it.
        "injectToolGuide": True,
    }
    if messages:
        payload["messages"] = messages
    # An HWO `#name@model#` pin (model_override) overrides the globally-selected
    # model for this one call.
    selected_model = model_override or get_selected_model()
    if selected_model:
        # model_override=="auto" → send empty model so the gateway triggers
        # embedding-based auto-routing (/api/chat/stream on the gateway side).
        payload["model"] = "" if model_override == "auto" else selected_model
    if provider_override:
        payload["provider"] = provider_override
    # A pinned model without an explicitly paired provider is gateway-resolved.
    # When model_override is "auto" the gateway decides both model and provider.
    elif not model_override:
        selected_provider = get_selected_provider()
        if selected_provider:
            payload["provider"] = selected_provider

    # Native function-calling: advertise the tool registry as OpenAI-style
    # `tools` schemas so the model emits grammar-constrained `delta.tool_calls`
    # instead of hand-serialized JSON in its reply text. `tool_name_map`
    # un-mangles provider-safe names on the way back. The backend already
    # forwards `tools`/`tool_choice` to the provider and streams tool_calls.
    # Summary/compaction calls pass tools_enabled=False: a tool-less, text-only
    # completion (no tool schemas, no core-tool guide) so the model just writes
    # the requested summary instead of trying to act.
    tool_name_map: dict = {}
    if not tools_enabled:
        payload["injectToolGuide"] = False
    else:
        try:
            _unified_catalog = bool(get_runtime_config("use_unified_catalog"))
            _openai_tools, tool_name_map = tools_mod.get_registry().to_openai_tools(
                unified=_unified_catalog,
                allowed_names=allowed_tool_names,
            )
            if _openai_tools:
                payload["tools"] = _openai_tools
                payload["tool_choice"] = "auto"
        except Exception:
            tool_name_map = {}

    headers, cookies = backend_profiles.request_auth(backend_profile, session)

    try:
        # ── Retry loop for transient failures (opencode retry.ts pattern) ──
        # Retries on: Timeout, ConnectionError, HTTP 429, HTTP 5xx.
        # Does NOT retry on: 4xx (except 429), context-overflow (handled by
        # reactive compaction in agent_loop), or InterruptedError.
        # Honors `retry-after` header; exponential backoff: 2s, 4s, 8s, cap 30s.
        _MAX_RETRIES = 3
        _RETRY_BASE = 2.0
        _RETRY_CAP = 30.0
        response = None
        for _attempt in range(_MAX_RETRIES + 1):
            if interrupt_event is not None and interrupt_event.is_set():
                raise InterruptedError("interrupted before request")
            try:
                response = requests.post(
                    f"{backend_url}/api/chat/stream",
                    json=payload,
                    headers=headers,
                    cookies=cookies,
                    stream=True,
                    timeout=120,
                    allow_redirects=False,
                )
            except requests.Timeout:
                if _attempt < _MAX_RETRIES:
                    _delay = min(_RETRY_BASE * (2 ** _attempt), _RETRY_CAP)
                    if interrupt_event is not None:
                        if interrupt_event.wait(timeout=_delay):
                            raise InterruptedError("interrupted during retry delay")
                    else:
                        time.sleep(_delay)
                    continue
                return {"reply": "Request timed out after retries. Please try again.", "tool_calls": [], "done": True, "error": True}
            except requests.ConnectionError:
                if _attempt < _MAX_RETRIES:
                    _delay = min(_RETRY_BASE * (2 ** _attempt), _RETRY_CAP)
                    if interrupt_event is not None:
                        if interrupt_event.wait(timeout=_delay):
                            raise InterruptedError("interrupted during retry delay")
                    else:
                        time.sleep(_delay)
                    continue
                return {"reply": f"Cannot connect to backend ({backend_url}). Check your network.", "tool_calls": [], "done": True, "error": True}

            if response.status_code == 200:
                break

            if 300 <= response.status_code < 400:
                return {
                    "reply": "Backend redirect refused: cross-origin credential forwarding is disabled.",
                    "tool_calls": [], "done": True, "error": True,
                }

            # Check if retryable (429 or 5xx)
            _retryable = response.status_code == 429 or response.status_code >= 500
            if _retryable and _attempt < _MAX_RETRIES:
                _delay = _RETRY_BASE * (2 ** _attempt)
                # Honor retry-after header (seconds or HTTP-date)
                _ra = response.headers.get("retry-after")
                if _ra:
                    try:
                        _delay = float(_ra)
                    except ValueError:
                        pass
                _delay = min(_delay, _RETRY_CAP)
                if interrupt_event is not None:
                    if interrupt_event.wait(timeout=_delay):
                        raise InterruptedError("interrupted during retry delay")
                else:
                    time.sleep(_delay)
                continue

            # Non-retryable error — return immediately
            try:
                err_data = response.json()
                return {"reply": f"Server Error: {err_data.get('detail', response.text[:200])}", "command": "", "rules": "", "done": True, "error": True}
            except Exception:
                return {"reply": f"Server Error: HTTP {response.status_code}", "command": "", "rules": "", "done": True, "error": True}

        # Parse SSE stream. Backend pass-through DeepSeek's OpenAI-compatible
        # chunks: each event is `{"choices":[{"delta":{"content":"..."}}]}`.
        # Accumulate deltas into one string, then parse JSON {reply,command,...}.
        accumulated = ""
        reasoning_accumulated = ""  # reasoning models emit delta.reasoning_content
        billing_info: dict = {}
        budget_info: dict = {}
        streamed_model: str = ""
        got_any_event = False
        finish_reason: Optional[str] = None  # last non-null choices[0].finish_reason
        _diag_events: list = []  # diagnostic: capture non-content fields
        prev_reply_for_chunks = ""
        prev_command_for_chunks = ""
        native_tc_frags: dict = {}  # index -> {id,name,arguments} for native tool_calls
        for line in response.iter_lines(decode_unicode=True):
            # Check for soft-interrupt between SSE chunks
            if interrupt_event is not None and interrupt_event.is_set():
                response.close()
                return {
                    "reply": accumulated or "(interrupted)",
                    "tool_calls": [],
                    "done": True,
                    "error": False,
                    "_interrupted": True,
                }
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                evt = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            got_any_event = True
            if evt.get("error"):
                if accumulated:
                    # Backend post-processing failed but content was already streamed — use it.
                    break
                return {"reply": f"Server Error: {evt['error']}", "tool_calls": [], "done": True, "error": True}
            if "_budget" in evt:
                # The gateway's answer to "how many output tokens do I actually
                # have?" — the clamped ceiling, plus the window room left after
                # this prompt. Arrives before any content.
                budget_info = dict(evt["_budget"] or {})
                continue
            if "_billing" in evt:
                billing_info = dict(evt["_billing"] or {})
                billing_info["billingDomain"] = backend_profile.kind
                billing_info["official"] = backend_profile.sends_laintas_credentials
                continue
            # Capture the actual model name streamed by the backend (first
            # non-empty occurrence wins) for the status-bar display.
            if not streamed_model:
                _m = evt.get("model")
                if _m and isinstance(_m, str):
                    streamed_model = _m
            # Capture diagnostic info: any top-level keys beyond choices
            for k in evt.keys():
                if k not in ("choices", "id", "object", "created", "model", "system_fingerprint") and k not in _diag_events:
                    _diag_events.append(k)
            _choices = evt.get("choices")
            # Capture the OpenAI-native finish_reason (provider passes it through
            # verbatim — "stop" / "tool_calls" / "length"). Authoritative signal
            # for whether the turn ended vs. expects tool results back.
            if isinstance(_choices, list) and _choices:
                _fr = _choices[0].get("finish_reason")
                if _fr:
                    finish_reason = _fr
            delta_content = (
                _choices[0].get("delta", {}).get("content")
                if isinstance(_choices, list) and _choices
                else None
            )
            # Reasoning models (e.g. deepseek-v4-pro via Ark) emit their
            # chain-of-thought through delta.reasoning_content, a SEPARATE
            # field from delta.content.  Without consuming it, a turn whose
            # entire token budget is spent on reasoning produces an empty
            # accumulated string, which is then misdiagnosed as a silent
            # failure.  Accumulate it separately so callers can distinguish
            # "model is still thinking" from "model produced nothing".
            delta_reasoning = (
                _choices[0].get("delta", {}).get("reasoning_content")
                if isinstance(_choices, list) and _choices
                else None
            )
            # OpenAI-style native tool_calls in delta: reassemble streamed
            # fragments (keyed by index) instead of dropping them.
            _delta = _choices[0].get("delta", {}) if isinstance(_choices, list) and _choices else {}
            if _delta.get("tool_calls"):
                for _frag in _delta["tool_calls"]:
                    if not isinstance(_frag, dict):
                        continue
                    _i = _frag.get("index", 0)
                    _slot = native_tc_frags.setdefault(_i, {"id": "", "name": "", "arguments": ""})
                    if _frag.get("id"):
                        _slot["id"] = _frag["id"]
                    _fn = _frag.get("function") or {}
                    if _fn.get("name"):
                        _slot["name"] = _fn["name"]
                    if _fn.get("arguments"):
                        _slot["arguments"] += _fn["arguments"]
                # Native mode: surface the tool name as a command preview as it
                # streams (un-mangled), so the UI shows what's about to run.
                if on_chunk is not None and native_tc_frags:
                    _first_idx = min(native_tc_frags)
                    _nm = (native_tc_frags[_first_idx].get("name") or "").strip()
                    _nm = tool_name_map.get(_nm, _nm)
                    if _nm:
                        _np = _nm
                        if len(native_tc_frags) > 1:
                            _np = f"{_nm} (+{len(native_tc_frags)-1} more)"
                        if _np != prev_command_for_chunks:
                            try: on_chunk("command", _np)
                            except Exception: pass
                            prev_command_for_chunks = _np
            if delta_content:
                accumulated += delta_content
                if on_chunk is not None:
                    # Content is plain prose - stream it straight through as the
                    # reply. Tool calls arrive natively via delta.tool_calls
                    # (handled above), never by parsing content.
                    try: on_chunk("reply", delta_content)
                    except Exception: pass
                    prev_reply_for_chunks = accumulated.strip()
            if delta_reasoning:
                reasoning_accumulated += delta_reasoning
                if on_chunk is not None:
                    try: on_chunk("thinking", delta_reasoning)
                    except Exception: pass

        if not got_any_event:
            return {"reply": "No response from AI", "tool_calls": [], "done": True, "error": True}

        # Native function-calls emitted out-of-band (delta.tool_calls), if any.
        native_calls = _native_to_tool_calls(native_tc_frags, tool_name_map)

        # Output-truncation signal: the model ran right up against the token
        # ceiling. When a big single-response write (e.g. a whole-file fs.write)
        # exceeds max_tokens, the JSON never closes and parsing fails — but the
        # cause is length, not formatting, so it needs a different nudge.
        # finish_reason == "length" is the provider's OWN truncation signal and
        # the authoritative one — trust it first.
        #
        # The completion-token heuristic below is only a *fallback* for backends
        # that omit finish_reason. It must never override a clean stop: on
        # reasoning models `completionTokens` includes hidden reasoning tokens,
        # so a short, fully-finished answer can still report near-ceiling usage.
        # Gate the fallback behind "finish_reason is not a clean end" so it can
        # never flag a completed turn as truncated.
        _completion_tokens = int((billing_info or {}).get("completionTokens", 0) or 0)
        # Compare against what the gateway GRANTED, not what we asked for. The
        # request is clamped against the provider ceiling and the remaining
        # window, so the local config value is not the limit in force — using
        # it made the heuristic both miss real truncations (granted < asked)
        # and invent false ones (granted > asked). Fall back to the local
        # value only for gateways too old to report a budget.
        _max_tokens = int((budget_info or {}).get("granted")
                          or get_runtime_config("max_tokens") or 0)
        _clean_stop = finish_reason in ("stop", "end_turn", "tool_calls")
        # On some models reasoning tokens are counted OUTSIDE max_tokens (a
        # 40,000-token request measured 106,108 completion tokens on
        # doubao-seed-2.0-pro), so completion_tokens routinely exceeds the
        # grant with nothing wrong. Comparing the two would then flag every
        # reasoning-heavy turn as truncated whenever finish_reason is missing.
        # Where the gateway says the budget does not bound the count, trust
        # finish_reason alone.
        _reasoning_in_budget = (budget_info or {}).get("reasoningInBudget", True)
        _hit_ceiling = (
            not _clean_stop
            and _reasoning_in_budget
            and _max_tokens > 0
            and _completion_tokens >= _max_tokens * 0.95
        )
        _truncated_turn = finish_reason == "length" or _hit_ceiling

        # Why the turn was cut off. agent_loop used to infer this from "reply is
        # empty and reasoning is not", which mis-fires on the single most common
        # case: a truncated tool call puts its bytes in delta.tool_calls[].
        # arguments, never in delta.content, so `reply` is empty on a plain
        # output overrun too — and a reasoning model always has reasoning text.
        # The overrun was therefore reported as reasoning exhaustion and the
        # model got told to think less instead of to write in chunks.
        # Decide it here, where the two channels are still distinguishable.
        #   "tool_args"  — cut off mid tool-call; the write was too big
        #   "reasoning"  — reasoning ate the budget, nothing came out
        #   "output"     — prose ran past the ceiling
        _truncation_kind = ""
        if _truncated_turn:
            if native_tc_frags:
                _truncation_kind = "tool_args"
            elif not accumulated.strip() and reasoning_accumulated:
                _truncation_kind = "reasoning"
            else:
                _truncation_kind = "output"

        # ── Local usage accounting (/usage) — every completed call lands here
        # regardless of tool/prose outcome. Backends that send no _billing
        # (external/unmetered) get chars/4 estimates so stats still move.
        # When auto-routing is active, streamed_model holds the real model name
        # selected by the gateway; use that for accurate per-model accounting.
        # A backend-echoed "auto" is not a real model name, so skip it.
        _usage_model = (streamed_model if streamed_model not in ("", "auto") else "") or selected_model
        if billing_info:
            usage_tracker.record(
                model=_usage_model,
                prompt_tokens=(billing_info or {}).get("promptTokens", 0),
                completion_tokens=_completion_tokens,
                cost_cents=(billing_info or {}).get("costCents", 0),
                official=bool(billing_info.get("official")),
                backend_kind=backend_profile.kind,
                truncated=_truncated_turn,
            )
        elif accumulated:
            usage_tracker.record(
                model=_usage_model,
                prompt_tokens=usage_tracker.estimate_tokens(
                    json.dumps(payload, ensure_ascii=False)),
                completion_tokens=usage_tracker.estimate_tokens(accumulated),
                official=False,
                backend_kind=backend_profile.kind,
                estimated=True,
                truncated=_truncated_turn,
            )

        # Update the REPL status bar with the model used this call.
        # When auto-routing is active ("auto"), keep the virtual label so
        # the status bar stays at "auto-routing" instead of showing the
        # gateway's real-time pick (which changes per-request).
        # Falls back to the user's configured selection if the backend
        # didn't echo a model name in the SSE stream.
        # A backend-echoed "auto" carries no real model name (the gateway
        # auto-routed), so treat it as empty and fall back to the user's
        # explicit selection instead of clobbering it with "auto".
        _effective_model = (selected_model if selected_model in ("auto", "")
                            else streamed_model if streamed_model not in ("", "auto")
                            else selected_model)
        if _effective_model:
            _update_status_cache(model=_effective_model)

        raw_text = accumulated.strip()
        # Content is always prose now (tool calls come natively); surface it.
        prose_reply = raw_text

        # ── 1. NATIVE PATH (primary) ───────────────────────────────────────
        # Model invoked tools through the OpenAI function-calling channel
        # (delta.tool_calls). This is the normal end-to-end mode: the backend
        # passes the provider's native tool_calls straight through.
        if native_calls:
            # L4: If the turn was truncated mid-tool-call, the JSON arguments
            # are incomplete and _native_to_tool_calls falls back to args={}
            # (or partial args).  Drop the calls so they are not silently
            # executed with empty/invalid arguments; the truncation handler
            # in agent_loop will nudge the model to retry.
            if _truncated_turn:
                return {
                    "reply": prose_reply,
                    "tool_calls": [],
                    "finish_reason": finish_reason or "tool_calls",
                    "done": False,
                    "error": False,
                    "_truncated": True,
                    "_truncation_kind": _truncation_kind or "tool_args",
                    "_reasoning": reasoning_accumulated,
                    "_billing": billing_info,
                    "_budget": budget_info,
                    "_diag_events": _diag_events + ["truncated_native_tool_calls_dropped"],
                }
            return {
                "reply": prose_reply,
                "tool_calls": native_calls,
                "finish_reason": finish_reason or "tool_calls",
                "done": False,
                "error": False,
                "_truncated": False,
                "_reasoning": reasoning_accumulated,
                "_billing": billing_info,
                "_budget": budget_info,
                "_diag_events": _diag_events + ["parsed_native_tool_calls"],
            }

        # ── 2. Tagged-tool-call compat ─────────────────────────────────────
        # Older/regressed models may emit <tool_calls>...</tool_calls> as text
        # instead of using the native channel. Convert so the loop continues.
        tagged_tool_calls = _extract_tagged_tool_calls(accumulated)
        if tagged_tool_calls:
            # L4: Same truncation guard as Path 1 - a truncated tagged block
            # yields incomplete tool calls.  Drop and let the loop retry.
            if _truncated_turn:
                return {
                    "reply": "",
                    "tool_calls": [],
                    "finish_reason": finish_reason or "tool_calls",
                    "done": False,
                    "error": False,
                    "_truncated": True,
                    "_truncation_kind": _truncation_kind or "tool_args",
                    "_reasoning": reasoning_accumulated,
                    "_billing": billing_info,
                    "_budget": budget_info,
                    "_diag_events": _diag_events + ["truncated_tagged_tool_calls_dropped"],
                }
            return {
                "reply": "",
                "tool_calls": tagged_tool_calls,
                "finish_reason": finish_reason or "tool_calls",
                "done": False,
                "error": False,
                "_truncated": False,
                "_reasoning": reasoning_accumulated,
                "_billing": billing_info,
                "_budget": budget_info,
                "_diag_events": _diag_events + ["parsed_tagged_tool_calls"],
            }

        # ── 3. PROSE FINAL ─────────────────────────────────────────────────
        # No tool calls anywhere: return the provider text verbatim.  Do not
        # fabricate a "(no response)" reply because that converts an empty or
        # malformed provider turn into a successful prose completion.
        return {
            "reply": raw_text,
            "tool_calls": [],
            "finish_reason": finish_reason or "stop",
            "done": False,
            "error": False,
            "_truncated": _truncated_turn,
            "_truncation_kind": _truncation_kind,
            "_reasoning": reasoning_accumulated,
            "_billing": billing_info,
            "_budget": budget_info,
            "_diag_events": _diag_events,
        }

    except requests.Timeout:
        return {"reply": "Request timed out. Please try again.", "tool_calls": [], "done": True, "error": True}
    except requests.ConnectionError:
        return {"reply": f"Cannot connect to backend ({backend_url}). Check your network.", "tool_calls": [], "done": True, "error": True}
    except InterruptedError:
        # Soft-interrupt from _on_chunk callback during streaming
        _partial = ""
        try:
            _partial = accumulated
        except NameError:
            pass
        return {
            "reply": _partial or "(interrupted)",
            "tool_calls": [],
            "done": True,
            "error": False,
            "_interrupted": True,
        }
    except Exception as e:
        return {"reply": f"Error: {e}", "tool_calls": [], "done": True, "error": True}


# ── Agent Registration with Helpwo Backend ─────────────────────────────

class TerminalSession:
    """A real PTY shell bridged to the browser over a WebSocket relay.

    Two threads share one sync WebSocket (the documented one-reader /
    one-writer pattern): the reader thread pumps PTY output up, the main
    thread pumps browser input + resizes down into the PTY. Frames are JSON
    text; payload bytes are base64 so arbitrary/binary output survives intact:
      host → browser : {"t":"o","d":<b64>}  output    {"t":"exit"}
      browser → host : {"t":"i","d":<b64>}  input     {"t":"resize","cols","rows"}
    """

    def __init__(self, backend_url, agent_id, agent_secret, session_id, cols, rows,
                 on_closed=None):
        self.backend_url = backend_url
        self.agent_id = agent_id
        self.agent_secret = agent_secret
        self.session_id = session_id
        self.cols = cols
        self.rows = rows
        self.fd = None
        self.pid = None
        self._ws = None
        self._closed = threading.Event()
        self._on_closed = on_closed

    def _ws_url(self):
        base = self.backend_url.replace("https://", "wss://").replace("http://", "ws://")
        return (f"{base}/api/agents/{self.agent_id}/term"
                f"?sessionId={self.session_id}&role=host")

    def _set_winsize(self, cols, rows):
        import fcntl, termios, struct
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name=f"laintas-term-{self.session_id}").start()

    def close(self):
        """Revoke the relay and terminate the shell, if still active."""
        self._cleanup()

    def _run(self):
        if self._closed.is_set():
            return
        import pty, ssl
        from websockets.sync.client import connect

        # requests (used for /connect's HTTP registration) bundles its own
        # certifi CA store, so it works even when the OS trust store is
        # missing/broken — a common state on minimal Linux installs. The
        # websockets library has no such fallback: left to its default
        # ssl.create_default_context(), it trusts the OS store only, and
        # fails with CERTIFICATE_VERIFY_FAILED in exactly that situation.
        # Build the same certifi-backed context requests effectively uses.
        ssl_context = None
        if self._ws_url().startswith("wss://"):
            try:
                import certifi
                ssl_context = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ssl_context = None  # fall back to the library default

        shell = os.environ.get("SHELL") or "/bin/bash"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            if self._closed.is_set():
                os._exit(0)
            # Child: become the shell with a sane terminal identity.
            os.environ["TERM"] = "xterm-256color"
            try:
                os.execvp(shell, [shell, "-l"] if os.path.basename(shell) in ("bash", "zsh", "sh") else [shell])
            except Exception:
                os._exit(127)
            return

        # Parent: size the PTY, then connect the relay.
        if self._closed.is_set():
            self._cleanup()
            return
        self._set_winsize(self.cols, self.rows)
        # A CDN/WAF in front of the backend (Cloudflare Free, in our case)
        # intermittently rejects the WebSocket *upgrade* from this non-browser
        # client with HTTP 400 — the plain-HTTPS siblings (register/heartbeat
        # via `requests`) sail through, but the WS handshake gets fingerprinted
        # and flaked ~1-in-N. It succeeds on retry the vast majority of the
        # time, so retry a few times with short backoff instead of failing the
        # whole open on the first flaky rejection. (The browser-shaped UA below
        # cuts the reject rate but doesn't eliminate it, since CF also
        # fingerprints the TLS/handshake, not just the UA.)
        last_err = None
        for attempt in range(5):
            if self._closed.is_set():
                self._cleanup()
                return
            try:
                self._ws = connect(
                    self._ws_url(),
                    additional_headers={"Authorization": f"Agent {self.agent_secret}"},
                    open_timeout=10,
                    max_size=64 * 1024,
                    ssl=ssl_context,
                    user_agent_header="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < 4:
                    time.sleep(0.6 * (attempt + 1))
        if last_err is not None:
            console.print(f"[red]Terminal relay connect failed after retries: {last_err}[/red]")
            self._cleanup()
            return

        if self._closed.is_set():
            self._cleanup()
            return

        threading.Thread(target=self._pump_output, daemon=True,
                         name=f"laintas-term-out-{self.session_id}").start()
        try:
            self._read_input_loop()
        finally:
            self._cleanup()

    def _read_input_loop(self):
        """Main thread: browser → PTY (input + resize)."""
        import base64
        for message in self._ws:  # iterates until the socket closes
            try:
                msg = json.loads(message)
            except (ValueError, TypeError):
                continue
            t = msg.get("t")
            if t == "i":
                try:
                    os.write(self.fd, base64.b64decode(msg.get("d", "")))
                except OSError:
                    break
            elif t == "resize":
                try:
                    self._set_winsize(int(msg["cols"]), int(msg["rows"]))
                except (KeyError, ValueError, TypeError):
                    pass

    def _pump_output(self):
        """Reader thread: PTY → browser. Sole writer of the WebSocket."""
        import base64
        while not self._closed.is_set():
            try:
                # 32 KiB stays below the relay's 64 KiB JSON/base64 frame cap.
                data = os.read(self.fd, 32768)
            except OSError:
                break
            if not data:
                break  # shell exited / PTY closed
            try:
                self._ws.send(json.dumps({"t": "o", "d": base64.b64encode(data).decode("ascii")}))
            except Exception:
                break
        # Tell the browser the shell ended, then close the relay.
        try:
            self._ws.send(json.dumps({"t": "exit"}))
        except Exception:
            pass
        self._cleanup()

    def _cleanup(self):
        if self._closed.is_set():
            return
        self._closed.set()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._on_closed is not None:
            try:
                self._on_closed(self.session_id)
            except Exception:
                pass


class AgentRegistry:
    """Manages remote agent registration with Helpwo backend."""

    # Bound both active work and queued work. A remote client must not be able
    # to turn the polling loop into an unbounded thread/PTY factory.
    # The executor ceilings are deliberately higher than the defaults. The
    # user-facing values live in agent_loop's /config registry and are
    # enforced by the bounded scheduler below.
    REMOTE_EXECUTOR_THREADS = 64
    REMOTE_CONTROL_EXECUTOR_THREADS = 4
    REMOTE_CONTROL_KINDS = frozenset({
        "abort", "approval-response", "disconnect", "term-close",
    })

    def __init__(self):
        self.agent_id: Optional[str] = None
        self.agent_secret: str = ""
        self.agent_name: str = ""
        self.instance_id: str = getattr(
            paths, "PROCESS_INSTANCE_ID",
            getattr(paths, "INSTANCE_ID", f"pid-{os.getpid()}"),
        )
        # ── Sub-terminal identity (two-end handshake with Helpwo) ────────
        # depth 0 = primary CLI (auto-registers = "online"). depth ≥ 1 = a
        # nested CLI inside a sub-terminal: it registers ONLY when the user
        # runs /connect there (or Helpwo asked for it via term-new), carrying
        # terminal_meta so Helpwo can show the CLI-side name/definition.
        self.depth: int = 0
        self.parent_remote_id: Optional[str] = None
        self.terminal_meta: Optional[dict] = None
        # Absolute host path the user chose to SHARE as Helpwo's remote
        # workspace via `/connect <folder>`. None = link only, share nothing
        # (Helpwo keeps its virtual workspace). Only meaningful at depth 0.
        self.workspace_path: Optional[str] = None
        # Raw PTY relay sessions are tracked separately from nested CLI
        # terminals so disconnect/unregister can revoke them all.
        self._remote_terminal_lock = threading.RLock()
        self._remote_terminals: dict[str, "TerminalSession"] = {}
        # REPL state callbacks, stashed by main() so /connect can start the
        # message poll outside main's scope.
        self._state_cb = None
        self._chat_cb = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._message_poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._session: Optional[dict] = None
        self._processing_message = threading.Event()
        self._pending_responses: list = []  # thread-safe queue for responses to send
        self._registration_lock = threading.Lock()
        self._last_reregister = 0.0
        # Remembered across re-registrations so the gateway can resurrect the
        # same agentId (keeps Helpwo tabs + sub-terminal parent links stable).
        self._last_agent_id: str = ""
        # Serializes background Helpwo chats (they share the REPL history).
        self._chat_run_lock = threading.Lock()
        self._remote_executor = self._new_remote_executor()
        self._remote_control_executor = ThreadPoolExecutor(
            max_workers=self.REMOTE_CONTROL_EXECUTOR_THREADS,
            thread_name_prefix="laintas-remote-control",
        )
        self._remote_capacity_lock = threading.Condition(threading.RLock())
        self._remote_accepted = {"task": 0, "control": 0}
        self._remote_running = {"task": 0, "control": 0}
        self._remote_stopping = threading.Event()

        # ── Async event bus (Phase 1) ───────────────────────────────────
        # _event_q holds batches of events (each item is a list[dict]).
        # _sender_thread coalesces batches into single POSTs in the background
        # so the REPL/handlers never block on HTTP. _sender_stop tells it to
        # finish draining and exit.
        self._event_q: "queue.Queue[list]" = queue.Queue(maxsize=10000)
        self._sender_thread: Optional[threading.Thread] = None
        self._sender_stop = threading.Event()

        # ── Active request tracking (Phase A1) ───────────────────────────
        # Maps reqId → threading.Event for abort signalling.
        self._active_requests: dict[str, threading.Event] = {}
        # Maps reqId → threading.Event + decision for approval flow.
        self._pending_approvals: dict[str, tuple[threading.Event, dict]] = {}
        self._active_req_lock = threading.RLock()

        # ── WebRTC peer-to-peer file channel (lazy) ─────────────────────
        self._webrtc = None  # WebrtcManager | False(unavailable) | None(not yet)
        self._rtc_config: dict = {}

    def _new_remote_executor(self):
        return ThreadPoolExecutor(
            max_workers=self.REMOTE_EXECUTOR_THREADS,
            thread_name_prefix="laintas-remote",
        )

    @staticmethod
    def _remote_limits(control: bool) -> tuple[int, int]:
        """Read the live /config limits for one remote work class."""
        from agent_loop import get_runtime_config
        if control:
            return (
                int(get_runtime_config("remote_control_workers")),
                int(get_runtime_config("remote_control_queue_size")),
            )
        return (
            int(get_runtime_config("remote_max_workers")),
            int(get_runtime_config("remote_queue_size")),
        )

    def _reserve_remote_capacity(self, control: bool) -> bool:
        group = "control" if control else "task"
        workers, queued = self._remote_limits(control)
        with self._remote_capacity_lock:
            if self._remote_accepted[group] >= workers + queued:
                return False
            self._remote_accepted[group] += 1
            return True

    def _run_bounded_remote(self, message: dict, agent_state_cb,
                            chat_history_cb, control: bool) -> None:
        group = "control" if control else "task"
        try:
            with self._remote_capacity_lock:
                while (not self._remote_stopping.is_set()
                       and self._remote_running[group] >= self._remote_limits(control)[0]):
                    self._remote_capacity_lock.wait(timeout=0.25)
                if self._remote_stopping.is_set():
                    return
                self._remote_running[group] += 1
            try:
                self._handle_remote_message(message, agent_state_cb, chat_history_cb)
            finally:
                with self._remote_capacity_lock:
                    self._remote_running[group] -= 1
                    self._remote_capacity_lock.notify_all()
        finally:
            with self._remote_capacity_lock:
                self._remote_accepted[group] -= 1
                self._remote_capacity_lock.notify_all()

    def _ensure_webrtc(self):
        """Lazily create the WebRTC manager. Returns it, or None if aiortc is
        unavailable (file ops then fall back to the relay path)."""
        if self._webrtc is None:
            try:
                from webrtc_channel import WebrtcManager
                if not WebrtcManager.available():
                    console.print(f"[dim]WebRTC disabled (aiortc not importable: "
                                  f"{WebrtcManager.import_error()})[/dim]")
                    self._webrtc = False
                else:
                    from webrtc_channel import configured_ice_servers
                    self._webrtc = WebrtcManager(
                        lambda sid, typ, meta: self._push(sid, typ, "", meta),
                        ice_servers=configured_ice_servers(self._rtc_config),
                    )
                    # So the WebRTC path/exec checks can also allow the
                    # folder explicitly shared via /connect or /helpwo
                    # (self.workspace_path), not just policy.py's
                    # allowedRoots (a separate, unrelated command-safety
                    # list that doesn't include an arbitrary shared cwd by
                    # default).
                    from webrtc_channel import set_agent_registry
                    set_agent_registry(self)
            except Exception as e:
                console.print(f"[dim]WebRTC unavailable: {e}[/dim]")
                self._webrtc = False
        return self._webrtc or None

    def _agent_auth_headers(self) -> dict:
        """Bearer-style CLI credential; never place this secret in a URL."""
        return {"Authorization": f"Agent {self.agent_secret}"}

    def _recover_registration(self) -> None:
        """Re-register after a server-side credential rotation or state loss."""
        if ((get_backend_profile().sends_laintas_credentials and not self._session)
                or time.time() - self._last_reregister < 10):
            return
        if not self._registration_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self._last_reregister < 10:
                return
            self._last_reregister = time.time()
            if self.register(self._session, self.agent_name or None, quiet=True):
                console.print("[green]Remote agent credentials refreshed.[/green]")
        finally:
            self._registration_lock.release()

    def register(self, session: dict, name: str = None, quiet: bool = False) -> bool:
        """Register this CLI as a remote agent with Helpwo backend."""
        # A disconnect shuts down the old pool; allow a later /connect to
        # create a fresh one on the same registry instance.
        if self._remote_executor is None:
            self._remote_executor = self._new_remote_executor()
        if self._remote_control_executor is None:
            self._remote_control_executor = ThreadPoolExecutor(
                max_workers=self.REMOTE_CONTROL_EXECUTOR_THREADS,
                thread_name_prefix="laintas-remote-control",
            )
        self._remote_stopping.clear()
        self._session = session
        hostname = socket.gethostname()
        cwd = os.getcwd()

        config = load_config()
        if name:
            self.agent_name = name
        else:
            self.agent_name = config.get("agentName", hostname)

        user_email = session.get("userEmail", "")
        user_name = session.get("userName", "")

        profile = get_backend_profile()
        payload = {
            "name": self.agent_name,
            "instanceId": self.instance_id,
            "hostname": hostname,
            "os": SYSTEM,
            "shell": SHELL_NAME,
            "cwd": cwd,
            "goal": f"CLI agent '{self.agent_name}' on {hostname} ({SYSTEM})",
        }
        if profile.sends_laintas_credentials:
            payload["userEmail"] = user_email
            payload["userName"] = user_name
        if self.parent_remote_id:
            payload["parentId"] = self.parent_remote_id
        if self.workspace_path:
            # The one folder the user opted to share; Helpwo mounts this as the
            # remote workspace root (file bytes ride P2P, off this server).
            payload["workspacePath"] = self.workspace_path
        if self._last_agent_id:
            # Ask the gateway to resurrect the same agentId so Helpwo tabs
            # and sub-terminal parent links survive re-registration.
            payload["previousAgentId"] = self._last_agent_id
        if self.terminal_meta:
            payload["terminal"] = self.terminal_meta
            payload["goal"] = (f"Sub-terminal '{self.terminal_meta.get('name', '')}'"
                               f" on {hostname}")

        backend_url = profile.base_url
        headers, cookies = backend_profiles.request_auth(profile, session)

        try:
            resp = requests.post(
                f"{backend_url}/api/agents/register",
                json=payload,
                headers=headers,
                cookies=cookies,
                timeout=5,
                allow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.agent_id = data.get("agentId", "")
                self.agent_secret = data.get("agentSecret", "")
                rtc_config = data.get("rtcConfig")
                self._rtc_config = rtc_config if isinstance(rtc_config, dict) else {}
                self._last_agent_id = self.agent_id
                if not quiet:
                    console.print(Panel(
                        f"[green]Agent linked to Helpwo AGENTS[/green]\n"
                        f"Name: [bold]{self.agent_name}[/bold]\n"
                        f"ID: {self.agent_id}\n"
                        f"Backend: {backend_url}",
                        title="Agent Registered",
                        border_style="green",
                    ))
                return True
            else:
                if not quiet:
                    console.print(Panel(
                        f"Backend: {backend_url}\n"
                        f"Response: HTTP {resp.status_code}\n\n"
                        f"[dim]Agent won't appear in Helpwo AGENTS panel. You can still use all features.[/dim]",
                        title="[yellow]Agent Not Linked[/yellow]",
                        border_style="yellow",
                    ))
                return False
        except requests.RequestException as e:
            if not quiet:
                console.print(Panel(
                    f"Backend: {backend_url}\n"
                    f"Error: {e}\n\n"
                    f"[dim]Is the Helpwo backend running? Agent won't appear in Helpwo AGENTS panel.[/dim]\n"
                    f"[dim]You can still use all features normally.[/dim]",
                    title="[yellow]Agent Not Linked[/yellow]",
                    border_style="yellow",
                ))
            return False

    def start_heartbeat(self):
        """Start heartbeat thread."""
        if not self.agent_id:
            return
        self._running = True
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
        # Start the async event sender alongside heartbeat — _push_events
        # becomes non-blocking only once this thread is alive.
        self._start_event_sender()

    def _start_event_sender(self):
        """Start the background event-sender thread if not already running."""
        if self._sender_thread is not None and self._sender_thread.is_alive():
            return
        self._sender_stop.clear()
        self._sender_thread = threading.Thread(
            target=self._event_sender_loop, daemon=True,
            name="laintas-event-sender",
        )
        self._sender_thread.start()

    def _event_sender_loop(self):
        """Drain _event_q, coalesce up to 50 batches within 200ms, POST once.

        Runs until _sender_stop is set AND the queue is empty, so a graceful
        shutdown can request the loop drain remaining events before exiting.
        """
        while True:
            try:
                first = self._event_q.get(timeout=0.5)
            except queue.Empty:
                if self._sender_stop.is_set():
                    return
                continue

            batch = list(first)
            deadline = time.time() + 0.1
            while len(batch) < 50:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    more = self._event_q.get(timeout=remaining)
                    batch.extend(more)
                except queue.Empty:
                    break

            try:
                self._do_post_events(batch)
            except Exception:
                # Never let a POST exception kill the sender — that would
                # silently break every subsequent _push_events call.
                pass

    def _do_post_events(self, events: list):
        """Synchronous POST. Called only from the sender thread."""
        if not self.agent_id or not events:
            return
        backend_url = get_backend_url()
        headers = self._agent_auth_headers()
        try:
            requests.post(
                f"{backend_url}/api/agents/{self.agent_id}/events",
                json={
                    "events": events,
                    "instanceId": self.instance_id,
                    "state": {
                        "cwd": os.getcwd(),
                        "status": "running",
                        "instanceId": self.instance_id,
                    },
                },
                headers=headers,
                timeout=5,
                allow_redirects=False,
            )
        except requests.RequestException:
            pass

    def _flush_events(self, timeout: float = 2.0):
        """Block until the sender drains _event_q, capped at timeout seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._event_q.empty():
                return
            time.sleep(0.05)

    def _heartbeat_loop(self):
        """Send heartbeat every HEARTBEAT_INTERVAL seconds with extended state."""
        backend_url = get_backend_url()

        # Try to import psutil for metrics (optional dependency)
        try:
            import psutil as _psutil_mod
            _has_psutil = True
        except ImportError:
            _has_psutil = False

        while self._running and self.agent_id:
            try:
                payload = {
                    "agentId": self.agent_id,
                    "instanceId": self.instance_id,
                    "cwd": os.getcwd(),
                    "shell": SHELL_NAME,
                }

                # Running terminals snapshot
                terminals = get_all_terminals()
                running_terms = []
                for t in terminals:
                    running_terms.append({
                        "name": t.name,
                        "alive": bool(t.session.is_alive()) if t.session else False,
                        "cmd": t.command[:120],
                    })
                payload["runningTerminals"] = running_terms

                # System metrics
                if _has_psutil:
                    try:
                        load = _psutil_mod.getloadavg()
                        mem = _psutil_mod.virtual_memory()
                        disk = _psutil_mod.disk_usage(os.getcwd())
                        payload["metrics"] = {
                            "loadAvg": [round(load[0], 2), round(load[1], 2), round(load[2], 2)],
                            "memFreeMB": round(mem.available / (1024 * 1024), 1),
                            "memTotalMB": round(mem.total / (1024 * 1024), 1),
                            "diskFreeGB": round(disk.free / (1024**3), 1),
                        }
                    except Exception:
                        pass

                response = requests.post(
                    f"{backend_url}/api/agents/heartbeat",
                    json=payload,
                    headers=self._agent_auth_headers(),
                    timeout=5,
                    allow_redirects=False,
                )
                if response.status_code in (403, 404):
                    self._recover_registration()
                beat_ok = response.status_code == 200
            except requests.RequestException:
                beat_ok = False  # heartbeat failures are silent

            # A failed beat retries quickly so one transient error can't
            # open a heartbeat gap big enough to look offline to Helpwo.
            time.sleep(5.0 if not beat_ok
                       else float(get_runtime_config("heartbeat_interval")))

    def start_message_poll(self, agent_state_cb, chat_history_cb):
        """Start background thread to poll for incoming messages from Helpwo UI.

        Args:
            agent_state_cb: callable() → dict — returns current agent state
            chat_history_cb: callable() → list — returns current chat history
        """
        if (not self.agent_id or
                (get_backend_profile().sends_laintas_credentials and not self._session)):
            return
        # Reconnect (/connect <new-name>, /name) must not stack a second poll
        # thread — the existing loop re-reads self.agent_id each iteration.
        if self._message_poll_thread is not None and self._message_poll_thread.is_alive():
            return
        self._message_poll_thread = threading.Thread(
            target=self._poll_loop,
            args=(agent_state_cb, chat_history_cb),
            daemon=True,
        )
        self._message_poll_thread.start()
        console.print("[dim]Listening for remote messages from Helpwo...[/dim]")

    def _poll_loop(self, agent_state_cb, chat_history_cb):
        """Poll backend for incoming messages every 2 seconds."""
        backend_url = get_backend_url()

        while self._running and self.agent_id:
            try:
                resp = requests.get(
                    f"{backend_url}/api/agents/{self.agent_id}/poll",
                    params={"instanceId": self.instance_id},
                    headers=self._agent_auth_headers(),
                    timeout=5,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    messages = data.get("inputs", [])
                    for msg in messages:
                        # Dispatch in a worker so a long-running handler
                        # (chat/delegate can run for minutes) never stalls
                        # the poll loop — term-new/term-close/abort from
                        # Helpwo must stay responsive throughout.
                        kind = msg.get("kind")
                        control = kind in self.REMOTE_CONTROL_KINDS
                        executor = self._remote_control_executor if control else self._remote_executor
                        if not self._reserve_remote_capacity(control):
                            req_id = msg.get("reqId") or msg.get("id")
                            self._push_final(
                                req_id, "busy",
                                "remote task capacity is full; retry later",
                            )
                            continue

                        try:
                            executor.submit(
                                self._run_bounded_remote, msg,
                                agent_state_cb, chat_history_cb, control)
                        except RuntimeError:
                            group = "control" if control else "task"
                            with self._remote_capacity_lock:
                                self._remote_accepted[group] -= 1
                                self._remote_capacity_lock.notify_all()
                            req_id = msg.get("reqId") or msg.get("id")
                            self._push_final(
                                req_id, "busy", "remote task executor is stopping",
                            )
                elif resp.status_code in (403, 404):
                    self._recover_registration()
            except requests.RequestException:
                pass  # poll failures are silent, retry next cycle

            time.sleep(2)

    def _handle_remote_message(self, msg: dict, agent_state_cb, chat_history_cb):
        """Dispatch an incoming message by 'kind' per HelpwoAI protocol.

        Backwards-compatible: if a message has no 'kind' field, it is treated
        as a legacy chat message (content → run_agent_loop / system command).
        Exactly one 'final' event must be pushed per reqId — handlers below
        enforce this; the catch-all also pushes a fail-final on exception.
        """
        self._processing_message.set()
        req_id = msg.get("reqId") or msg.get("id")
        try:
            kind = msg.get("kind")
            payload = msg.get("payload") or {}

            if not kind:
                # Legacy chat message: {id, content, timestamp}
                kind = "chat"
                payload = {"message": msg.get("content", "")}

            if kind == "exec":
                self._handle_exec(req_id, payload)
            elif kind == "query":
                self._handle_query(req_id, payload)
            elif kind == "chat":
                self._handle_chat(req_id, payload, agent_state_cb, chat_history_cb)
            elif kind == "delegate":
                self._handle_delegate(req_id, payload, agent_state_cb, chat_history_cb)
            elif kind == "webtest":
                self._handle_webtest(req_id, payload)
            elif kind == "analyze_site":
                self._handle_analyze_site(req_id, payload)
            elif kind == "term-open":
                self._handle_term_open(req_id, payload)
            elif kind == "term-new":
                self._handle_term_new(req_id, payload)
            elif kind == "term-close":
                self._handle_term_close(req_id, payload)
            elif kind == "disconnect":
                self._handle_disconnect(req_id, payload)
            elif kind == "abort":
                self._handle_abort(req_id, payload)
            elif kind == "approval-response":
                self._handle_approval_response(req_id, payload)
            elif kind == "rtc-offer":
                self._handle_rtc_offer(req_id, payload)
            elif kind == "rtc-ice":
                # Non-trickle in Layer 1: candidates are embedded in the SDP,
                # so standalone ICE messages are ignored for now.
                pass
            elif kind == "rtc-close":
                mgr = self._webrtc or None
                if mgr:
                    mgr.handle_close(req_id)
            else:
                self._push_final(req_id, "fail", f"unknown kind '{kind}'")
        except Exception as e:
            console.print(f"[red]Error handling remote message: {e}[/red]")
            if req_id:
                self._push_final(req_id, "fail", f"handler exception: {e}")
        finally:
            self._processing_message.clear()

    def _handle_rtc_offer(self, req_id: str, payload: dict):
        """Accept a WebRTC offer from the browser and answer it, establishing a
        peer-to-peer DataChannel so file transfers bypass the relay server.
        Does NOT push a 'final' — the answer/error is delivered as its own event
        (rtc-answer / rtc-error / rtc-unavailable) keyed to this reqId."""
        mgr = self._ensure_webrtc()
        if mgr is None:
            self._push(req_id, "rtc-unavailable", "", {"reason": "aiortc not installed on host"})
            return
        sdp = payload.get("sdp")
        if not sdp:
            self._push(req_id, "rtc-error", "", {"error": "missing sdp"})
            return
        mgr.handle_offer(req_id, sdp)

    def _handle_chat(self, req_id: str, payload: dict, agent_state_cb, chat_history_cb):
        """Run a remote chat message WITHOUT occupying the local REPL.

        Plain messages run their own agent loop in this worker thread (the
        local prompt stays usable; Helpwo streams the events). The shared
        chat history keeps continuity with the local conversation. Slash
        commands are the exception — they must execute in the main loop, so
        they still go through REPL injection.
        """
        content = payload.get("message", "")

        console.print(Panel(
            f"[bold cyan]Remote message from Helpwo:[/bold cyan]\n{content}",
            title="Incoming",
            border_style="cyan",
        ))

        if content.lstrip().startswith("/"):
            done = threading.Event()
            _inject_input(content, done)
            if not done.wait(timeout=120):
                self._push_final(req_id, "fail", "processing timeout — main loop busy")
                return
            state = agent_state_cb() if callable(agent_state_cb) else {}
            summary = state.get("lastReply", "") or state.get("lastOutput", "") or "done"
            self._push_final(req_id, "success", summary[:2000])
            return

        shared_primary = get_agent("primary")
        if (shared_primary is not None
                and shared_primary.status in {
                    "queued", "running", "thinking", "waiting"}):
            queued, detail = queue_primary_message(
                shared_primary.id, content)
            if queued:
                agent_ui_events.hub.emit(
                    "user_message", agent_id=shared_primary.id,
                    terminal_name=agent_scope_terminal(shared_primary),
                    summary=content, detail=content, status="queued")
                self._push_final(req_id, "success", detail)
            else:
                self._push_final(req_id, "fail", detail)
            return

        if not self._chat_run_lock.acquire(timeout=300):
            self._push_final(req_id, "fail", "another Helpwo chat is still running")
            return
        remote_error = ""
        try:
            admitted = False
            if shared_primary is not None:
                admitted, detail = begin_primary_run(shared_primary.id)
                if not admitted:
                    queued, queue_detail = queue_primary_message(
                        shared_primary.id, content)
                    self._push_final(
                        req_id, "success" if queued else "fail",
                        queue_detail if queued else detail)
                    return
            abort_ev = (shared_primary.abort_event
                        if shared_primary is not None else threading.Event())
            with self._active_req_lock:
                self._active_requests[req_id] = abort_ev

            deps = self._build_loop_deps(req_id)
            session = self._session or {}
            chat_history = (shared_primary.chat_history
                            if shared_primary is not None
                            else chat_history_cb() if callable(chat_history_cb) else [])
            if not isinstance(chat_history, list):
                chat_history = []
            state = shared_primary.state if shared_primary is not None else {}
            if shared_primary is not None:
                chat_history.append({
                    "role": "user", "content": content,
                    "input_kind": "prompt"})
                agent_ui_events.hub.emit(
                    "user_message", agent_id=shared_primary.id,
                    terminal_name=agent_scope_terminal(shared_primary),
                    summary=content, detail=content, status="accepted")

            def _chat_events(events):
                if abort_ev.is_set():
                    raise InterruptedError("chat aborted")
                if shared_primary is not None:
                    agent_ui_events.hub.ingest(
                        shared_primary.id, events,
                        agent_scope_terminal(shared_primary))
                for ev in events:
                    ev["reqId"] = req_id
                self._push_events(events, req_id=req_id)

            try:
                result = run_agent_loop(
                    deps=deps, original_input=content,
                    session=session, state=state,
                    chat_history=chat_history,
                    events_cb=_chat_events,
                    depth=0,
                    agent_id=(shared_primary.id if shared_primary else None),
                    interrupt_event=abort_ev,
                    message_queue=(shared_primary.message_queue
                                   if shared_primary else None),
                    max_loops_override=int(get_runtime_config("max_loops")),
                )
            except InterruptedError:
                self._push_final(req_id, "aborted", "chat aborted by remote")
                return
            except Exception as e:
                remote_error = f"{type(e).__name__}: {e}"
                self._push_final(req_id, "fail", str(e)[:2000])
                return

            reply = (result.get("msg", "")
                     or result.get("state", {}).get("lastReply", "")) if isinstance(result, dict) else ""
            status = "success" if (isinstance(result, dict) and result.get("success")) else "fail"
            self._push_final(req_id, status, (reply or "done")[:2000])
        finally:
            if shared_primary is not None and 'admitted' in locals() and admitted:
                _remote_result = locals().get("result")
                _remote_reply = str(
                    (_remote_result or {}).get("msg")
                    or ((_remote_result or {}).get("state") or {}).get("lastReply")
                    or "")
                _remote_failed = bool(
                    isinstance(_remote_result, dict)
                    and _remote_result.get("success", True) is False
                    and not abort_ev.is_set())
                finish_primary_run(
                    shared_primary.id, reply=_remote_reply,
                    error=(remote_error or (
                        str(_remote_result.get("exit_reason") or "incomplete")
                        if _remote_failed else "")),
                    aborted=abort_ev.is_set())
            with self._active_req_lock:
                self._active_requests.pop(req_id, None)
            self._chat_run_lock.release()

    def _handle_exec(self, req_id: str, payload: dict):
        """Run a shell command in a PTY and stream stdout under req_id.

        Pushes cmd-start → stdout chunks → cmd-end → final. PTY merges
        stderr into stdout; splitting is deferred to a later phase.
        """
        cmd = payload.get("command")
        cwd = payload.get("cwd") or os.getcwd()
        try:
            timeout = int(payload.get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        if not cmd:
            self._push_final(req_id, "fail", "missing 'command' in payload")
            return

        # ── Security policy check ──────────────────────────────────────
        import policy as _policy
        decision = _policy.evaluate(cmd, cwd, req_id=req_id,
                                    agent_id=self.agent_id)
        if decision.action == "deny":
            self._push_final(req_id, "fail", f"Blocked by policy: {decision.reason}")
            console.print(f"[red]BLOCKED remote exec: {cmd[:100]} — {decision.reason}[/red]")
            return
        if (decision.action == "needs_approval"
                or not get_runtime_config("allow_remote_exec_without_approval")):
            approval = self._request_approval(
                req_id, cmd, cwd, timeout=300,
                destructive=_policy.is_delete_command(cmd),
            )
            if approval != "approve":
                self._push_final(req_id, "aborted",
                                 f"User {approval}: {cmd[:100]}")
                return

        console.print(Panel(
            f"[bold yellow]remote_exec[/bold yellow] {cmd}\n[dim]cwd={cwd} timeout={timeout}s[/dim]",
            title=f"Incoming exec ({req_id})",
            border_style="yellow",
        ))

        self._push(req_id, "cmd-start", "", {"command": cmd, "cwd": cwd})
        try:
            cwd = os.path.abspath(cwd)
            if not os.path.isdir(cwd):
                raise OSError(f"not a directory: {cwd}")
        except (TypeError, OSError) as e:
            self._push_final(req_id, "fail", f"invalid cwd: {e}")
            return

        sess = InteractiveSession(cmd, timeout=timeout, cwd=cwd)
        start = time.time()
        try:
            sess.start()

            # Line-buffered streaming: hold a partial-line buffer between
            # PTY reads so the UI never displays a half-formed line.
            # Backend can still see real-time progress because we flush on
            # every '\n'. Any final partial line is flushed after cmd-end.
            line_buf = ""
            while sess.is_alive():
                chunk = sess.read_output(timeout=0.2)
                if chunk:
                    line_buf += chunk
                    if "\n" in line_buf:
                        complete, line_buf = line_buf.rsplit("\n", 1)
                        self._push(req_id, "stdout", complete + "\n")
                if time.time() - start > timeout:
                    sess.close()
                    if line_buf:
                        self._push(req_id, "stdout", line_buf)
                        line_buf = ""
                    self._push(req_id, "cmd-end", "", {
                        "exitCode": -1,
                        "durationMs": int((time.time() - start) * 1000),
                    })
                    self._push_final(req_id, "fail", f"timeout after {timeout}s")
                    return

            # Drain any output buffered after process exit.
            chunk = sess.read_output(timeout=0.2)
            if chunk:
                line_buf += chunk
            if line_buf:
                # Final flush — may not end in newline (e.g. trailing prompt).
                self._push(req_id, "stdout", line_buf)
            sess.close()
            rc = sess._returncode
            duration_ms = int((time.time() - start) * 1000)
            self._push(req_id, "cmd-end", "", {
                "exitCode": rc, "durationMs": duration_ms,
            })
            status = "success" if rc == 0 else "fail"
            self._push_final(req_id, status, f"exit={rc}")
        except Exception as e:
            try:
                sess.close()
            except Exception:
                pass
            self._push_final(req_id, "fail", f"exec error: {e}")

    def _handle_webtest(self, req_id: str, payload: dict):
        """Run browser test flows for Helpwo's run_tests tool.

        payload: {tests: [{name, target(url), viewportWidth?, checkErrors?,
        steps: [test_flow steps]}]}. Each test navigates to its target first,
        then browser.test_flow does the heavy lifting (per-step results,
        runtime-error gate, screenshot on failure). Tests run in a dedicated
        throwaway browser session in a daemon thread so message polling stays
        responsive. Exactly one final; the JSON report rides meta["report"].
        """
        tests = payload.get("tests")
        if not isinstance(tests, list) or not tests:
            self._push_final(req_id, "fail", "missing 'tests' in webtest payload")
            return

        # One approval for the whole run (same gate as remote exec); the
        # per-step browser policy is then auto-approved below — the user
        # already said yes to exactly this set of tests/targets.
        if not get_runtime_config("allow_remote_exec_without_approval"):
            targets = ", ".join(sorted({str(t.get("target", "?"))[:80]
                                        for t in tests if isinstance(t, dict)}))
            approval = self._request_approval(
                req_id, f"WEBTEST: {len(tests)} test(s) against {targets}",
                os.getcwd(), timeout=300,
            )
            if approval != "approve":
                self._push_final(req_id, "aborted", f"User {approval}: webtest")
                return

        def _run():
            import tools as tools_mod
            registry = tools_mod.get_registry()

            class _ApprovedDeps:
                """Wraps loop deps; browser steps of an approved webtest run
                don't re-prompt per action."""
                def __init__(self, base):
                    self._base = base

                def __getattr__(self, name):
                    return getattr(self._base, name)

                def request_command_approval(self, cmd, reason=None):
                    return True

            ctx = tools_mod.ToolCtx(
                deps=_ApprovedDeps(get_loop_deps()), agent_id=None, session=None,
                events_cb=None, cwd=os.getcwd(),
            )
            session = f"webtest-{req_id[-6:]}"
            report = []
            opened = False
            try:
                for i, spec in enumerate(tests):
                    if not isinstance(spec, dict):
                        continue
                    name = str(spec.get("name") or f"test-{i + 1}")
                    target = str(spec.get("target") or "").strip()
                    steps = spec.get("steps")
                    if not target or not isinstance(steps, list) or not steps:
                        report.append({"name": name, "passed": False,
                                       "fatal": "test needs 'target' (url) and non-empty 'steps'"})
                        continue
                    if not opened:
                        width = int(spec.get("viewportWidth") or 1280)
                        r = registry.invoke("browser.open", {
                            "url": "about:blank", "name": session,
                            "width": width, "height": 900,
                        }, ctx)
                        if not r.get("ok"):
                            report.append({"name": name, "passed": False,
                                           "fatal": f"browser.open failed: {r.get('error')}"})
                            break
                        opened = True
                    self._push(req_id, "webtest-progress",
                               f"running '{name}' ({len(steps)} step(s))")
                    flow = registry.invoke("browser.test_flow", {
                        "session": session,
                        # allow_local: the approval prompt showed these targets;
                        # loopback-only relaxation lets tests hit the host's
                        # own dev server.
                        "steps": [{"action": "navigate", "url": target, "allow_local": True}] + list(steps),
                        "check_errors": bool(spec.get("checkErrors", True)),
                        "screenshot_on_failure": True,
                        "clear_captures": True,
                    }, ctx)
                    if not flow.get("ok"):
                        report.append({"name": name, "passed": False,
                                       "fatal": f"test_flow error: {flow.get('error')}"})
                        continue
                    report.append({
                        "name": name,
                        "passed": bool(flow.get("pass")),
                        "failedAt": flow.get("failed_at"),
                        "steps": flow.get("steps"),
                        "text": flow.get("result"),
                        "screenshot": flow.get("screenshot"),
                        "errors": flow.get("errors"),
                    })
            finally:
                if opened:
                    try:
                        registry.invoke("browser.close", {"name": session}, ctx)
                    except Exception:
                        pass
            n_pass = sum(1 for t in report if t.get("passed"))
            status = "success" if report and n_pass == len(report) else "fail"
            self._push_final(req_id, status,
                             f"webtest: {n_pass}/{len(report)} test(s) passed",
                             meta={"report": report})

        threading.Thread(target=_run, daemon=True,
                         name=f"webtest-{req_id[-6:]}").start()

    @staticmethod
    def _robots_allowed(url: str) -> tuple:
        """Best-effort robots.txt check. Returns (allowed: bool, note: str).
        Missing/unreachable robots.txt is treated as allowed."""
        try:
            import urllib.parse as _up
            import urllib.robotparser as _rp
            import urllib.request as _ur
            parts = _up.urlsplit(url)
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            req = _ur.Request(robots_url, headers={"User-Agent": "laintas-analyze"})
            try:
                with _ur.urlopen(req, timeout=8) as resp:
                    body = resp.read().decode("utf-8", "replace")
            except Exception:
                return True, "robots.txt unreachable — treated as allowed"
            rp = _rp.RobotFileParser()
            rp.parse(body.splitlines())
            allowed = rp.can_fetch("*", url)
            return allowed, ("allowed by robots.txt" if allowed
                             else "DISALLOWED by robots.txt")
        except Exception as e:
            return True, f"robots check error ({e}) — treated as allowed"

    def _handle_analyze_site(self, req_id: str, payload: dict):
        """Capture a public web page for Helpwo's analyze_site tool (analysis &
        reference, NOT clone-and-republish).

        payload: {url, desktopWidth?, mobileWidth?}. Respects robots.txt,
        requires one user approval (with a risk warning), then in a dedicated
        throwaway browser session: navigates, gently scrolls to trigger lazy
        content + API calls, and collects a full-page desktop + mobile
        screenshot, the rendered DOM, design tokens (colors/fonts/sizes), the
        referenced first-party asset URLs, and the observed XHR/fetch API log.
        Large artifacts are written to host temp files; the report (meta.report)
        carries their paths so Helpwo pulls them over P2P. Exactly one final.
        """
        url = str(payload.get("url") or "").strip()
        if not url:
            self._push_final(req_id, "fail", "missing 'url' in analyze_site payload")
            return
        if not re.match(r"^https?://", url, re.I):
            self._push_final(req_id, "fail", "url must start with http:// or https://")
            return

        allowed, robots_note = self._robots_allowed(url)
        if not allowed:
            self._push_final(req_id, "aborted",
                             f"Refusing: {robots_note} for {url}")
            return

        # One approval for the whole capture, with an explicit risk warning.
        if not get_runtime_config("allow_remote_exec_without_approval"):
            approval = self._request_approval(
                req_id,
                f"ANALYZE SITE (reference only): {url}\n"
                f"robots: {robots_note}\n"
                f"⚠ Downloads another site's rendered UI + observed API for "
                f"analysis. Only do this for pages you are authorized to access.",
                os.getcwd(), timeout=300,
            )
            if approval != "approve":
                self._push_final(req_id, "aborted", f"User {approval}: analyze_site")
                return

        def _run():
            import json as _json
            import tempfile as _tf
            import tools as tools_mod
            import browser_session as _bs
            registry = tools_mod.get_registry()

            class _ApprovedDeps:
                def __init__(self, base):
                    self._base = base

                def __getattr__(self, name):
                    return getattr(self._base, name)

                def request_command_approval(self, cmd, reason=None):
                    return True

            ctx = tools_mod.ToolCtx(
                deps=_ApprovedDeps(get_loop_deps()), agent_id=None, session=None,
                events_cb=None, cwd=os.getcwd(),
            )
            session = f"analyze-{req_id[-6:]}"
            dweb = int(payload.get("desktopWidth") or 1440)
            mweb = int(payload.get("mobileWidth") or 390)
            opened = False
            try:
                r = registry.invoke("browser.open", {
                    "url": "about:blank", "name": session,
                    "width": dweb, "height": 900,
                }, ctx)
                if not r.get("ok"):
                    self._push_final(req_id, "fail",
                                     f"browser.open failed: {r.get('error')}")
                    return
                opened = True

                sess = _bs.get_browser_session(session)
                if sess is None:
                    self._push_final(req_id, "fail", "browser session vanished after open")
                    return
                sess.set_api_capture(True)
                sess.clear_captures()

                self._push(req_id, "analyze-progress", f"navigating to {url}")
                nav = registry.invoke("browser.navigate", {
                    "session": session, "url": url, "timeout": 45,
                }, ctx)
                if not nav.get("ok"):
                    self._push_final(req_id, "fail",
                                     f"navigation failed: {nav.get('error')}")
                    return

                page = sess.get_page()
                final_url = nav.get("url") or url
                title = nav.get("title") or ""

                # Gentle interaction: scroll in steps to trigger lazy content and
                # XHR/fetch, then settle. Every step is best-effort.
                self._push(req_id, "analyze-progress", "capturing (scroll + settle)")
                try:
                    for frac in (0.25, 0.5, 0.75, 1.0):
                        page.evaluate(
                            "f => window.scrollTo(0, document.body.scrollHeight * f)", frac)
                        page.wait_for_timeout(700)
                    page.evaluate("() => window.scrollTo(0, 0)")
                except Exception:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # Design tokens + first-party asset URLs + heading samples.
                tokens = {}
                assets = []
                try:
                    extracted = page.evaluate(r"""
                        () => {
                          const uniq = (a) => Array.from(new Set(a)).filter(Boolean);
                          const isPaint = (c) => c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent';
                          const els = Array.from(document.querySelectorAll('body *')).slice(0, 4000);
                          const colors = [], bgs = [], fonts = [], sizes = [];
                          for (const el of els) {
                            const s = getComputedStyle(el);
                            if (isPaint(s.color)) colors.push(s.color);
                            if (isPaint(s.backgroundColor)) bgs.push(s.backgroundColor);
                            if (s.fontFamily) fonts.push(s.fontFamily);
                            if (s.fontSize) sizes.push(s.fontSize);
                          }
                          const a = [];
                          document.querySelectorAll('img[src]').forEach(i => a.push(i.src));
                          document.querySelectorAll('link[rel="stylesheet"][href]').forEach(l => a.push(l.href));
                          document.querySelectorAll('source[src]').forEach(s => a.push(s.src));
                          return {
                            colors: uniq(colors).slice(0, 30),
                            backgrounds: uniq(bgs).slice(0, 30),
                            fonts: uniq(fonts).slice(0, 15),
                            fontSizes: uniq(sizes).slice(0, 20),
                            assets: uniq(a).slice(0, 120),
                            headings: Array.from(document.querySelectorAll('h1,h2'))
                              .slice(0, 12).map(h => (h.innerText || '').trim().slice(0, 100))
                              .filter(Boolean),
                          };
                        }
                    """) or {}
                    tokens = {
                        "colors": extracted.get("colors", []),
                        "backgrounds": extracted.get("backgrounds", []),
                        "fonts": extracted.get("fonts", []),
                        "fontSizes": extracted.get("fontSizes", []),
                        "headings": extracted.get("headings", []),
                    }
                    # First-party assets only (same host as the final URL).
                    import urllib.parse as _up
                    host = _up.urlsplit(final_url).netloc
                    for a in extracted.get("assets", []):
                        try:
                            if _up.urlsplit(a).netloc == host:
                                assets.append(a)
                        except Exception:
                            pass
                except Exception:
                    pass

                # Rendered DOM (size-capped) → host temp file.
                dom_path = None
                try:
                    dom = page.evaluate("() => document.documentElement.outerHTML") or ""
                    dom = dom[:500000]
                    dom_path = _tf.mktemp(prefix="analyze-dom-", suffix=".html")
                    with open(dom_path, "w", encoding="utf-8") as f:
                        f.write(dom)
                except Exception:
                    dom_path = None

                # Desktop full-page screenshot.
                shot_desktop = None
                try:
                    p = _tf.mktemp(prefix="analyze-desktop-", suffix=".png")
                    s = registry.invoke("browser.screenshot", {
                        "session": session, "full_page": True, "path": p,
                    }, ctx)
                    if s.get("ok"):
                        shot_desktop = s.get("path") or p
                except Exception:
                    shot_desktop = None

                # Mobile full-page screenshot (best-effort; viewport resize may
                # no-op on some CDP setups — never fail the run over it).
                shot_mobile = None
                try:
                    page.set_viewport_size({"width": mweb, "height": 844})
                    page.wait_for_timeout(800)
                    p = _tf.mktemp(prefix="analyze-mobile-", suffix=".png")
                    s = registry.invoke("browser.screenshot", {
                        "session": session, "full_page": True, "path": p,
                    }, ctx)
                    if s.get("ok"):
                        shot_mobile = s.get("path") or p
                except Exception:
                    shot_mobile = None

                # Observed API log → host temp JSON file.
                api_path = None
                api_count = 0
                try:
                    api_log = sess.get_api_log()
                    api_count = len(api_log)
                    if api_log:
                        api_path = _tf.mktemp(prefix="analyze-api-", suffix=".json")
                        with open(api_path, "w", encoding="utf-8") as f:
                            _json.dump(api_log, f, ensure_ascii=False)
                except Exception:
                    api_path = None

                report = {
                    "url": final_url,
                    "title": title,
                    "robots": robots_note,
                    "tokens": tokens,
                    "assets": assets,
                    "apiCount": api_count,
                    "domPath": dom_path,
                    "apiPath": api_path,
                    "screenshots": {"desktop": shot_desktop, "mobile": shot_mobile},
                }
                self._push_final(
                    req_id, "success",
                    f"analyzed {final_url} — {api_count} API call(s), "
                    f"{len(assets)} first-party asset(s)",
                    meta={"report": report},
                )
            except Exception as e:
                self._push_final(req_id, "fail", f"analyze_site error: {e}")
            finally:
                if opened:
                    try:
                        registry.invoke("browser.close", {"name": session}, ctx)
                    except Exception:
                        pass

        threading.Thread(target=_run, daemon=True,
                         name=f"analyze-{req_id[-6:]}").start()

    def _handle_query(self, req_id: str, payload: dict):
        """Read-only reconnaissance. Returns one final with data in meta."""
        what = payload.get("what")
        target = payload.get("target")
        try:
            if what == "cwd":
                data = os.getcwd()
            elif what == "files":
                data = os.listdir(os.getcwd())
            elif what == "env":
                # Never export the process environment wholesale: it commonly
                # contains API keys, session tokens and cloud credentials.
                safe_names = ("LANG", "LC_ALL", "SHELL", "TERM", "COLORTERM")
                data = {name: os.environ[name] for name in safe_names if name in os.environ}
            elif what == "processes":
                # Stub: psutil dependency deferred to Phase B1.
                data = []
            elif what == "term-snapshot":
                term = get_terminal(target) if target else None
                if term is None:
                    data = None
                else:
                    n = int(get_runtime_config("terminal_tail_lines") or 20)
                    output = getattr(term.session, "full_output", "") or ""
                    data = "\n".join(output.split("\n")[-n:])
            else:
                self._push_final(req_id, "fail", f"unknown query.what={what!r}")
                return
            self._push_final(
                req_id, "success", f"query {what}",
                meta={"what": what, "data": data},
            )
        except Exception as e:
            self._push_final(req_id, "fail", f"query error: {e}")

    def _handle_term_open(self, req_id: str, payload: dict):
        """Spawn a real PTY shell and dial into the backend terminal relay.

        Gives the Helpwo browser a live shell (vim/htop/colors/Ctrl-C all
        work). It can be disabled only from the CLI's local runtime
        configuration (disable_remote_terminal).

        Connection itself is seamless after that standing opt-in. Account
        ownership and the one-use browser ticket are checked by the gateway;
        AI-issued commands and destructive operations retain their independent
        policy approval paths.
        """
        if get_runtime_config("disable_remote_terminal"):
            console.print("[yellow]Remote terminal request ignored (disable_remote_terminal is set).[/yellow]")
            self._push_final(req_id, "fail", "disable_remote_terminal is set on this CLI")
            return
        session_id = (payload.get("sessionId") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", session_id):
            self._push_final(req_id, "fail", "invalid sessionId")
            return
        with self._remote_terminal_lock:
            if session_id in self._remote_terminals:
                self._push_final(req_id, "fail", "terminal session is already open")
                return
        try:
            cols = max(1, int(payload.get("cols") or 80))
            rows = max(1, int(payload.get("rows") or 24))
        except (TypeError, ValueError):
            cols, rows = 80, 24

        # Audit the connection. This evaluator deliberately always allows once
        # the local disable_remote_terminal gate above has been opened.
        cwd = os.getcwd()
        import policy as _policy
        decision = _policy.evaluate_terminal_open(cwd, req_id=req_id, agent_id=self.agent_id)
        if decision.action == "deny":
            self._push_final(req_id, "fail", f"Blocked by policy: {decision.reason}")
            return

        # Another request may have passed its approval concurrently. Recheck
        # under the lock so one relay session ID can never acquire two PTYs.
        with self._remote_terminal_lock:
            if session_id in self._remote_terminals:
                self._push_final(req_id, "fail", "terminal session is already open")
                return

        # The relay must be dialed at the SAME Helpwo host the browser is on —
        # that host's nginx carries the /term WebSocket-upgrade config and
        # reaches the same gateway. The CLI's own get_backend_url() may point
        # at a different laintas origin (e.g. the main site) whose nginx has
        # no /term WS location and would 400 the upgrade. Trust the browser's
        # host only if it's a laintas.com origin; otherwise fall back.
        # Exact allow-list, NOT a "*.laintas.com" suffix match. The CLI dials
        # this host carrying `Authorization: Agent <secret>` (its long-lived
        # host credential); a suffix match would let anyone who can send a
        # term-open (i.e. already holds the Helpwo session) name an
        # attacker-controlled *.laintas.com subdomain and exfiltrate that
        # secret / hijack the PTY to a rogue relay. Only known Helpwo origins
        # (or an env override for self-hosters) are accepted; anything else
        # falls back to the CLI's own configured backend.
        backend_url = get_backend_url()
        allowed_relay_hosts = {
            h.strip().lower() for h in os.environ.get(
                "LAINTAS_RELAY_HOSTS", "helpwo.laintas.com,localhost:5173,127.0.0.1:5173"
            ).split(",") if h.strip()
        }
        raw_host = str(payload.get("host") or "").strip().lower()
        if raw_host in allowed_relay_hosts and re.fullmatch(r"[a-z0-9.:-]{1,253}", raw_host):
            scheme = "http" if raw_host.split(":", 1)[0] in ("localhost", "127.0.0.1") else "https"
            backend_url = f"{scheme}://{raw_host}"
        console.print(Panel(
            f"[bold cyan]Browser opened a terminal[/bold cyan]\n[dim]session {session_id} {symbols.BULLET} {cols}×{rows}[/dim]\n[dim]relay: {backend_url}[/dim]",
            title="Remote Terminal", border_style="cyan",
        ))
        try:
            term = TerminalSession(backend_url, self.agent_id, self.agent_secret,
                                   session_id, cols, rows,
                                   on_closed=self._remove_remote_terminal)
            with self._remote_terminal_lock:
                self._remote_terminals[session_id] = term
            term.start()
            self._push_final(req_id, "success", f"opened terminal session {session_id}")
        except Exception as e:
            with self._remote_terminal_lock:
                self._remote_terminals.pop(session_id, None)
            console.print(f"[red]Failed to open remote terminal: {e}[/red]")
            self._push_final(req_id, "fail", f"failed to open terminal: {e}")

    def _remove_remote_terminal(self, session_id: str) -> None:
        with self._remote_terminal_lock:
            self._remote_terminals.pop(session_id, None)

    def _close_remote_terminals(self) -> None:
        with self._remote_terminal_lock:
            terminals = list(self._remote_terminals.values())
            self._remote_terminals.clear()
        for term in terminals:
            try:
                term.close()
            except Exception:
                pass

    def _handle_term_new(self, req_id: str, payload: dict):
        """Helpwo's add-terminal action creates a named sub-terminal here (same path as
        /term <name>) running a nested laintas_cli that auto-/connects, so it
        registers itself back to Helpwo as a managed terminal."""
        if self.depth > 0:
            self._push_final(req_id, "fail", "term-new must target the primary CLI (depth 0)")
            return
        raw = (payload.get("name") or "").strip()
        if raw and (not re.fullmatch(r"[A-Za-z0-9._-]+", raw) or raw.lower() == "term0"):
            self._push_final(req_id, "fail",
                             "invalid terminal name (letters, numbers, dot, underscore, hyphen; term0 reserved)")
            return
        name = raw
        if not name:
            i = 1
            while get_terminal(f"helpwo{i}") is not None:
                i += 1
            name = f"helpwo{i}"
        existing = get_terminal(name)
        if existing and existing.session and not existing.session.is_alive():
            unregister_terminal(name)
            existing = None
        if existing is not None:
            self._push_final(req_id, "fail", f"terminal '{name}' already exists")
            return
        lain_cmd = _build_connected_subterminal_cmd(name, self.agent_id, auto_connect=True)
        sub = SubTerminalSession(lain_cmd)
        sub.start()
        time.sleep(0.1)
        if not sub.is_alive():
            self._push_final(req_id, "fail", f"could not start terminal '{name}'")
            return
        sub.read_output(timeout=0.1)
        try:
            register_terminal(
                sub, "laintas-cli", 0, name=name,
                parent_terminal="term0")
        except Exception as exc:
            sub.close()
            self._push_final(req_id, "fail", f"could not register terminal '{name}': {exc}")
            return
        console.print(Panel(
            f"[bold cyan]Helpwo created sub-terminal [bold]{name}[/bold][/bold cyan]\n"
            f"[dim]It will hand itself over to Helpwo once it finishes starting.[/dim]",
            title="Remote Terminal", border_style="cyan",
        ))
        self._push_final(req_id, "success", name)

    def _handle_term_close(self, req_id: str, payload: dict):
        """Helpwo's close-terminal action closes a sub-terminal.

        Sent to the primary CLI with a name: closes that registered
        sub-terminal (kills the nested CLI with it). Sent to a sub-terminal
        CLI itself (no name / own name): the nested CLI shuts down gracefully,
        which closes its terminal window.
        """
        name = (payload.get("name") or "").strip()
        own_name = (self.terminal_meta or {}).get("name") if self.terminal_meta else None
        if self.depth > 0 and (not name or name == own_name):
            self._push_final(req_id, "success", own_name or "closing")
            self._flush_events(timeout=2.0)
            console.print("[yellow]Helpwo closed this sub-terminal.[/yellow]")
            def _die():
                time.sleep(0.3)
                try:
                    self.unregister()
                except Exception:
                    pass
                os._exit(0)
            threading.Thread(target=_die, daemon=True).start()
            return
        if not name:
            self._push_final(req_id, "fail", "missing 'name' in term-close payload")
            return
        if name.lower() == "term0":
            self._push_final(req_id, "fail", "term0 (primary) cannot be closed remotely")
            return
        info = get_terminal(name)
        if info is None:
            self._push_final(req_id, "fail", f"no sub-terminal named '{name}'")
            return
        # PTY-backed nested CLIs can survive a polite close when their stdin
        # EOF handling wedges — grab the child pid first and force-kill after.
        child_pid = None
        try:
            child_pid = getattr(getattr(info.session, "_pty", None), "pid", None)
        except Exception:
            pass
        unregister_terminal(name)
        if child_pid and child_pid > 0:
            def _reap(pid=child_pid):
                time.sleep(1.0)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            threading.Thread(target=_reap, daemon=True).start()
        console.print(Panel(
            f"[yellow]Helpwo closed sub-terminal [bold]{name}[/bold][/yellow]",
            title="Remote Terminal", border_style="yellow",
        ))
        self._push_final(req_id, "success", name)

    def _build_loop_deps(self, req_id: str) -> "LoopDeps":
        """LoopDeps for a remote-initiated agent loop (chat/delegate): local
        display wiring plus approvals routed back to Helpwo under req_id."""
        return LoopDeps(
            read_file=read_file, append_file=append_file,
            write_file=write_file, strip_ansi=strip_ansi,
            generate_prompt=generate_cli_prop_template,
            call_backend=lambda **kw: call_backend_stream(**kw),
            SubTerminalSession=SubTerminalSession,
            InteractiveSession=InteractiveSession,
            display_command_output=display_command_output,
            display_sub_terminal_preview=display_sub_terminal_preview,
            display_file_diff=display_file_diff,
            console=console, Markdown=Markdown,
            pty_passthrough=pty_passthrough,
            request_command_approval=lambda cmd, reason: self._request_approval(
                req_id, cmd, os.getcwd()) == "approve",
            request_file_write_approval=lambda path, diff, reason: self._request_approval(
                req_id, f"WRITE {path} — {reason}", os.getcwd()) == "approve",
            request_file_delete_approval=lambda path, preview, reason: self._request_approval(
                req_id, f"DELETE {path} — {reason}\n{preview}", os.getcwd(),
                destructive=True) == "approve",
        )

    def _handle_disconnect(self, req_id: str, payload: dict):
        """Helpwo unilaterally unlinked this agent. Withdraw quietly: stop
        heartbeat/poll and unregister. The local terminal keeps running."""
        console.print(Panel(
            "[yellow]Helpwo disconnected this terminal.[/yellow]\n"
            "[dim]The CLI keeps running locally. Run /helpwo to connect again.[/dim]",
            title="Disconnected", border_style="yellow",
        ))
        # Suppress same-id resurrection: this was an explicit user action.
        self._last_agent_id = ""
        self.unregister()
        self.agent_id = None
        self.agent_secret = ""

    def _handle_delegate(self, req_id: str, payload: dict,
                         agent_state_cb, chat_history_cb):
        """Launch a local agent loop for a delegated task.

        Reuses run_agent_loop with events_cb wired to push ai-reply/ai-command
        events under reqId. On completion, pushes final.
        """
        goal = payload.get("goal", "").strip()
        if not goal:
            self._push_final(req_id, "fail", "missing 'goal' in delegate payload")
            return

        # Register the abort event BEFORE the approval prompt — an abort that
        # lands while we're waiting for approval must find the request (it
        # also force-rejects the pending approval, see _handle_abort).
        abort_ev = threading.Event()
        with self._active_req_lock:
            self._active_requests[req_id] = abort_ev

        if not get_runtime_config("allow_remote_exec_without_approval"):
            approval = self._request_approval(
                req_id, f"DELEGATE: {goal[:500]}", os.getcwd(), timeout=300,
            )
            if approval != "approve" or abort_ev.is_set():
                with self._active_req_lock:
                    self._active_requests.pop(req_id, None)
                self._push_final(req_id, "aborted", f"User {approval}: delegation")
                return

        max_loops_val = int(payload.get("maxLoops", get_runtime_config("max_loops")))
        max_loops_val = max(1, min(max_loops_val, 20))

        # Context briefing from the caller (HelpwoAI conversation summary).
        # Injected ahead of the goal so the delegated loop knows WHY it was
        # asked, while the goal stays the authoritative task.
        context = str(payload.get("context") or "").strip()
        loop_input = goal
        if context:
            loop_input = (
                "[BRIEFING FROM HELPWO — background from the caller's conversation]\n"
                + context[:6000]
                + "\n[END BRIEFING] The briefing is background only. "
                  "Your task is ONLY the goal below.\n\n"
                + goal
            )

        console.print(Panel(
            f"[bold magenta]delegate[/bold magenta] {goal[:200]}\n"
            f"[dim]maxLoops={max_loops_val}"
            + (f", context={len(context)} chars" if context else "")
            + "[/dim]",
            title=f"Incoming delegate ({req_id})",
            border_style="magenta",
        ))

        # Track this request for abort support
        deps = self._build_loop_deps(req_id)

        session = self._session or {}
        # Isolation: a delegated loop should see only its goal + the caller's
        # briefing — NOT whatever conversation happens to sit in this CLI's
        # local REPL. Mirrors how local Helpwo sub-agents are isolated from
        # their parent's history. (chat kind still uses the local history.)
        chat_history = []

        def _delegate_events(events):
            """Push loop events to backend with reqId."""
            if abort_ev.is_set():
                raise InterruptedError("delegate aborted")
            for ev in events:
                ev["reqId"] = req_id
            self._push_events(events, req_id=req_id)

        try:
            # Run in a thread so we can monitor abort
            result = {"success": False, "msg": "", "state": {}}
            run_done = threading.Event()

            def _run():
                nonlocal result
                try:
                    result = run_agent_loop(
                        deps=deps, original_input=loop_input,
                        session=session, state={},
                        chat_history=chat_history,
                        events_cb=_delegate_events,
                        depth=0,
                        interrupt_event=abort_ev,
                        max_loops_override=max_loops_val,
                    )
                except InterruptedError:
                    result = {"success": False, "msg": "aborted", "state": {}}
                except Exception as e:
                    result = {"success": False, "msg": str(e), "state": {}}
                finally:
                    run_done.set()

            t = threading.Thread(target=_run, daemon=True,
                                 name=f"laintas-delegate-{req_id}")
            t.start()

            # Wait for completion or abort
            while not run_done.wait(timeout=0.5):
                if abort_ev.is_set():
                    self._push_final(req_id, "aborted", "delegate aborted by remote")
                    return

            reply = result.get("msg", "") or (
                result.get("state", {}).get("lastReply", "") if isinstance(result, dict) else ""
            )
            status = "success" if result.get("success") else "fail"
            self._push_final(req_id, status, reply[:2000])

        except Exception as e:
            self._push_final(req_id, "fail", f"delegate error: {e}")
        finally:
            with self._active_req_lock:
                self._active_requests.pop(req_id, None)

    def _handle_abort(self, req_id: str, payload: dict):
        """Abort a running request identified by targetReqId.

        Sets the abort event for the target request; handlers check it at
        each loop/poll iteration boundary.
        """
        target = payload.get("targetReqId", "")
        reason = payload.get("reason", "")
        if not target:
            self._push_final(req_id, "fail", "missing 'targetReqId' in abort payload")
            return

        console.print(f"[dim yellow]Abort request for {target}: {reason or '(no reason)'}[/dim yellow]")

        with self._active_req_lock:
            abort_ev = self._active_requests.get(target)
            pending_approval = self._pending_approvals.get(target)

        # If the target is blocked on an approval prompt, resolve it as a
        # rejection so the waiting handler unblocks immediately.
        if pending_approval:
            approval_ev, response_dict = pending_approval
            response_dict["decision"] = "reject"
            approval_ev.set()

        if abort_ev:
            abort_ev.set()
            self._push_final(req_id, "success", f"abort signal sent to {target}")
        elif pending_approval:
            self._push_final(req_id, "success", f"pending approval for {target} rejected")
        else:
            # The request may have already completed or doesn't support abort
            self._push_final(req_id, "fail", f"request '{target}' not found or not abortable")

    def _handle_approval_response(self, req_id: str, payload: dict):
        """Handle user's response to a needs-approval event.

        Sets the decision on the pending approval, unblocking the waiting handler.
        """
        target = payload.get("targetReqId", "")
        decision = payload.get("decision", "reject")  # approve | reject | modify
        feedback = payload.get("feedback", "")

        if not target:
            self._push_final(req_id, "fail", "missing 'targetReqId' in approval-response")
            return

        with self._active_req_lock:
            entry = self._pending_approvals.get(target)

        if entry is None:
            self._push_final(req_id, "fail", f"no pending approval for '{target}'")
            return

        approval_ev, response_dict = entry
        response_dict["decision"] = decision
        response_dict["feedback"] = feedback
        approval_ev.set()

        console.print(
            f"[dim green]Approval response for {target}: "
            f"{decision}" + (f" — {feedback}" if feedback else "") + "[/dim green]"
        )
        self._push_final(req_id, "success", f"approval {decision} applied to {target}")

    def _request_approval(self, req_id: str, command: str, cwd: str,
                          timeout: float = 300.0,
                          destructive: bool = False) -> str:
        """Push needs-approval event and block until user responds.

        Returns "approve", "reject", or "modify". Timeout defaults to 5 min.
        """
        approval_ev = threading.Event()
        response_dict: dict = {}

        with self._active_req_lock:
            self._pending_approvals[req_id] = (approval_ev, response_dict)

        auto_confirm_seconds = mode_manager.get_auto_confirm_timeout(
            destructive=destructive)
        self._push_events(
            [{
                "type": "needs-approval",
                "content": f"Approve execution of: {command}",
                "meta": {
                    "summary": f"Execute: {command[:200]}",
                    "command": command,
                    "cwd": cwd,
                    "targetReqId": req_id,
                    "autoApproveAfter": auto_confirm_seconds,
                },
            }],
            req_id=req_id,
        )

        wait_timeout = (auto_confirm_seconds
                        if auto_confirm_seconds is not None else timeout)
        responded = approval_ev.wait(timeout=wait_timeout)

        with self._active_req_lock:
            self._pending_approvals.pop(req_id, None)

        if not responded and auto_confirm_seconds is not None:
            console.print(
                f"[yellow]AUTO mode: approval window expired after "
                f"{int(auto_confirm_seconds)} seconds; approving.[/yellow]"
            )
            return "approve"
        if not responded:
            return "reject"  # timeout = reject
        return response_dict.get("decision", "reject")


    def _push_events(self, events: list, req_id: str = None):
        """Enqueue events for the background sender to POST.

        Non-blocking: returns immediately after enqueueing. The background
        _event_sender_loop coalesces batches and does the actual HTTP. If
        the queue is full (>10000 batches buffered), drop the oldest batch.

        If req_id is given, every event missing 'reqId' gets it injected,
        and every event missing 'meta' gets an empty dict. Events that
        already carry their own reqId/meta are left alone.
        """
        if not self.agent_id or not events:
            return
        if req_id:
            for ev in events:
                ev.setdefault("reqId", req_id)
                ev.setdefault("meta", {})
        try:
            self._event_q.put_nowait(events)
        except queue.Full:
            # Drop oldest to make room — recovering connectivity matters more
            # than preserving stale telemetry.
            try:
                self._event_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._event_q.put_nowait(events)
            except queue.Full:
                pass

    # ── Per-request push helpers (HelpwoAI protocol) ─────────────────────
    def _push(self, req_id: str, type_: str, content: str = "", meta: dict = None):
        """Push a single typed event under a request id."""
        self._push_events(
            [{"type": type_, "content": content, "meta": meta or {}}],
            req_id=req_id,
        )

    def _push_final(self, req_id: str, status: str, summary: str,
                    artifacts: list = None, meta: dict = None):
        """Push the terminal 'final' event for a request. Exactly one per reqId."""
        final_meta = dict(meta or {})
        final_meta["status"] = status
        final_meta["summary"] = summary
        if artifacts:
            final_meta["artifacts"] = artifacts
        self._push_events(
            [{"type": "final", "content": summary, "meta": final_meta}],
            req_id=req_id,
        )

    def unregister(self):
        """Unregister agent on exit. Drains the event queue first so in-flight
        finals/outputs make it to the backend before the process exits.
        """
        self._running = False
        self._remote_stopping.set()
        with self._remote_capacity_lock:
            self._remote_capacity_lock.notify_all()
        if self._remote_executor is not None:
            self._remote_executor.shutdown(wait=False, cancel_futures=True)
            self._remote_executor = None
        if self._remote_control_executor is not None:
            self._remote_control_executor.shutdown(wait=False, cancel_futures=True)
            self._remote_control_executor = None
        self._close_remote_terminals()

        # Flush queued events (≤2s) then stop the sender thread so the
        # final unregister POST is the last thing we do.
        if self._sender_thread is not None and self._sender_thread.is_alive():
            self._flush_events(timeout=2.0)
            self._sender_stop.set()
            try:
                self._sender_thread.join(timeout=1.0)
            except RuntimeError:
                pass

        if not self.agent_id:
            return

        backend_url = get_backend_url()
        headers = self._agent_auth_headers()

        try:
            requests.post(
                f"{backend_url}/api/agents/unregister",
                json={"agentId": self.agent_id, "instanceId": self.instance_id},
                headers=headers,
                timeout=5,
                allow_redirects=False,
            )
        except requests.RequestException:
            pass
        self.agent_id = None
        self.agent_secret = ""


# ── Debug Display ───────────────────────────────────────────────────────

def show_debug_browser_interactive() -> None:
    """Interactive debug browser — arrow keys to select, Enter to view detail, q/Esc to exit."""
    while True:
        entries = get_debug_logs()[:20]
        if not entries:
            console.print("[dim]No debug entries yet. Run an AI command to see data.[/dim]")
            return

        labels = []
        for i, e in enumerate(entries):
            ts = e.timestamp[-19:] if len(e.timestamp) > 19 else e.timestamp
            if e.request_body:
                tag = "AI "
                summary = e.reply[:60] or e.command[:60] or "(no response)"
            else:
                tag = "CMD"
                summary = e.exec_command[:60] or "(no output)"
            summary = summary.replace("\n", " ")[:60]
            labels.append(f"#{i+1:2d}  {ts}  [{tag}]  {summary}")

        chosen = select_dialog(
            labels,
            title="Debug Log — Last 20 Entries",
            full_screen=True,
            hint=f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ view detail  q/Esc back",
        )
        if chosen is None:
            return
        idx = labels.index(chosen)
        with _alt_screen():
            show_debug_detail(idx)
            input("\n[dim]Press Enter to return to debug browser...[/dim]")


def show_debug_detail(index: int) -> None:
    """Show full detail for a single debug entry by index."""
    if index < 0 or index >= len(get_debug_logs()):
        console.print(f"[red]Entry #{index + 1} not found.[/red]")
        return

    e = get_debug_logs()[index]
    ts = e.timestamp

    # Build a multi-panel detail view
    header = (
        f"[bold cyan]#{e.loop}[/bold cyan] — {ts}   "
        f"Path: [dim]{e.current_path}[/dim]"
    )
    console.print(Panel(header, title=f"Debug Entry #{index + 1}"))

    # User input
    if e.user_input:
        console.print(Panel(e.user_input[:3000], title="User Input"))

    # Request body (AI entries only)
    if e.request_body:
        rb = e.request_body
        sections = []
        sections.append(f"[bold]Message:[/bold]\n{rb.get('message', '')[:2000]}")
        ctx = e.context_sizes
        sections.append(
            f"[bold]Context sizes:[/bold] "
            f"terminal={ctx.get('terminal', 0)} chars, "
            f"conversation={ctx.get('conversation', 0)} chars, "
            f"memory={ctx.get('memory', 0)} chars, "
            f"terminals={ctx.get('terminals', 0)} chars, "
            f"prompt={ctx.get('prompt', 0)} chars"
        )
        sections.append(f"[bold]Prompt Preview (first 500 chars):[/bold]\n{rb.get('promptPreview', '')[:500]}")
        console.print(Panel("\n\n".join(sections), title="Request Payload"))

    # AI response (AI entries only)
    if e.request_body:
        lines = []
        if e.reply:
            lines.append(f"[bold]Reply:[/bold]\n{e.reply[:1500]}")
        if e.command:
            lines.append(f"[bold]Command:[/bold] {e.command}")
        lines.append(f"[bold]Done:[/bold] {e.done}")
        if e.billing:
            cost = e.billing.get("costCents") or 0
            balance = e.billing.get("balanceCents") or 0
            lines.append(f"[bold]Billing:[/bold] ${cost / 100:.2f} (balance ${balance / 100:.2f})")
        if e.error:
            lines.append("[red]Error occurred[/red]")
        console.print(Panel("\n\n".join(lines), title="AI Response"))

        # Raw response JSON
        if e.response_raw:
            try:
                raw_preview = json.dumps(e.response_raw, ensure_ascii=False, indent=2)[:2000]
            except Exception:
                raw_preview = str(e.response_raw)[:2000]
            console.print(Panel(raw_preview, title="Raw Response JSON"))

    # Command execution
    if e.exec_command:
        lines = []
        lines.append(f"[bold]Executed:[/bold] {e.exec_command}")
        lines.append(f"[bold]Return code:[/bold] {e.exec_returncode}")
        if e.session_command:
            lines.append(f"[bold]Session:[/bold] {e.session_command}")
        if e.exec_stdout:
            lines.append(f"[bold]Stdout:[/bold]\n{e.exec_stdout}")
        if e.exec_stderr:
            lines.append(f"[bold]Stderr:[/bold]\n{e.exec_stderr}")
        console.print(Panel("\n\n".join(lines), title="Command Execution"))


# ── Terminal Manager ───────────────────────────────────────────────────

def _show_terminal_detail(name: str, cmd: str, sess, created: float, alive: bool) -> None:
    """Show detailed information about a terminal."""
    uptime = time.time() - created if created > 0 else 0
    if uptime < 60:
        uptime_str = f"{uptime:.0f}s"
    elif uptime < 3600:
        uptime_str = f"{uptime / 60:.1f}m"
    else:
        uptime_str = f"{uptime / 3600:.1f}h"

    output = sess.full_output if sess else ""
    output_preview = output[-1000:] if len(output) > 1000 else output
    registry_name = "term0" if name == "term0 (primary)" else name
    info = get_terminal(registry_name)
    parent = info.parent_terminal if info else None
    agents = list(info.stationed_agent_ids) if info else []
    children = [
        item.name for item in get_all_terminals()
        if item.parent_terminal == registry_name
    ]

    detail_text = (
        f"[bold]Name:[/bold] {name}\n"
        f"[bold]Parent:[/bold] {parent or '(root)'}\n"
        f"[bold]Child terminals:[/bold] {', '.join(children) or '(none)'}\n"
        f"[bold]Deployed agents:[/bold] {', '.join(agents) or '(none)'}\n"
        f"[bold]Command:[/bold] {cmd[:200]}\n"
        f"[bold]Status:[/bold] {'[green]Alive[/green]' if alive else '[red]Dead[/red]'}\n"
        f"[bold]Uptime:[/bold] {uptime_str}\n"
        f"[bold]Return code:[/bold] {sess.returncode if sess else 'N/A'}\n"
        f"[bold]Output ({len(output)} bytes):[/bold]\n[dim]{output_preview}[/dim]"
    )
    console.print(Panel(detail_text, title=f"Terminal: {name}"))


def show_terminal_manager(primary_session=None) -> None:
    """Interactive terminal manager — list, enter, observe, close, view details.

    Shows the primary session (if alive) plus all registered terminals.
    Arrow keys to navigate, Enter to fully enter (interactive takeover),
    o to observe read-only, c to close, d for details, q to exit.
    """

    def _collect():
        out = []
        if primary_session is not None and primary_session.is_alive():
            out.append(("term0 (primary)", primary_session.command,
                        primary_session, 0.0, True))
        for term in get_all_terminals():
            if term.name == "term0":
                continue
            out.append((term.name, term.command, term.session,
                        term.created_at,
                        term.session is not None and term.session.is_alive()))
        return out

    def _build_labels(items):
        labels = []
        for name, cmd, _sess, created, alive in items:
            status = f"[green]{symbols.DOT} alive[/green]" if alive else "[red]■ dead[/red]"
            uptime_str = ""
            if created > 0:
                uptime = time.time() - created
                if uptime < 60:
                    uptime_str = f" {uptime:.0f}s"
                elif uptime < 3600:
                    uptime_str = f" {uptime / 60:.1f}m"
                else:
                    uptime_str = f" {uptime / 3600:.1f}h"
            cmd_preview = cmd[:60].replace("\n", " ")
            registry_name = "term0" if name == "term0 (primary)" else name
            info = get_terminal(registry_name)
            owner = (
                f" parent={info.parent_terminal or 'root'}"
                f" agents={len(info.stationed_agent_ids)}"
                if info else ""
            )
            labels.append(f"[bold]{name}[/bold]  {status}{uptime_str}  "
                          f"[dim]{cmd_preview}{owner}[/dim]")
        return labels

    sel_idx = 0
    first = True
    status_msg = ""
    while True:
        items = _collect()
        if not items:
            if first:
                console.print("[dim]No sub-terminals. Use /t <name> to create one, "
                              "or let the AI spawn a command.[/dim]")
            else:
                console.print("\n[dim]No more terminals.[/dim]")
            return
        first = False
        sel_idx = min(sel_idx, len(items) - 1)

        labels = _build_labels(items)
        hint = f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ enter  o observe  c close  d details  q back"
        if status_msg:
            hint = f"{status_msg}\n{hint}"
        result = select_dialog(
            labels,
            title="Terminal Manager",
            full_screen=True,
            selected_index=sel_idx,
            action_keys={"o": "observe", "c": "close", "d": "details"},
            enter_action="enter",
            hint=hint,
        )
        if result is None:
            return
        action, idx = result
        if action is None or idx < 0 or idx >= len(items):
            return

        name, cmd, sess, created, alive = items[idx]
        status_msg = ""

        if action == "enter":
            if not alive:
                status_msg = "[yellow]Session has already ended.[/yellow]"
            else:
                enter_session(sess, display_name=name, display_cmd=cmd)
                time.sleep(0.1)
                if name != "term0 (primary)" and not sess.is_alive():
                    unregister_terminal(name)

        elif action == "observe":
            if not alive:
                status_msg = "[yellow]Session has already ended.[/yellow]"
            else:
                observe_session(sess, display_name=name, display_cmd=cmd)

        elif action == "close":
            if name == "term0 (primary)":
                status_msg = "[yellow]Cannot close the primary session. " \
                             "Use /exit or close the parent terminal.[/yellow]"
            else:
                unregister_terminal(name)
                status_msg = f"[green]Closed terminal [bold]{name}[/bold][/green]"

        elif action == "details":
            with _alt_screen():
                _show_terminal_detail(name, cmd, sess, created, alive)
                input("\n[dim]Press Enter to continue...[/dim]")

        sel_idx = idx


def _show_skill_detail(name: str) -> None:
    """Show detailed information about a skill (mirrors _show_terminal_detail)."""
    meta = skills_mod.get_all_metadata().get(name)
    state_list = [s for s in skills_mod.list_skills() if s["name"] == name]
    loaded = bool(state_list and state_list[0].get("loaded"))
    tools = tools_mod.get_registry().list_by_source().get(f"skill:{name}", [])

    lines = [
        f"[bold]Name:[/bold] {name}",
        f"[bold]Status:[/bold] {'[green]Loaded[/green]' if loaded else '[dim]Available[/dim]'}",
        f"[bold]Version:[/bold] {getattr(meta, 'version', '') or 'N/A'}",
        f"[bold]Path:[/bold] {getattr(meta, 'dir_path', '') or 'N/A'}",
        f"[bold]Description:[/bold] {getattr(meta, 'description', '') or '(none)'}",
    ]
    if tools:
        lines.append(f"[bold]Tools ({len(tools)}):[/bold]")
        for t in tools:
            lines.append(f"  [cyan]{t.name}[/cyan] — {t.description}")
    else:
        lines.append("[bold]Tools:[/bold] [dim](documentation-only / not loaded)[/dim]")
    console.print(Panel("\n".join(lines), title=f"Skill: {name}"))


def show_skill_manager() -> None:
    """Interactive skill manager — same style as the terminal manager.

    Lists every scanned skill with its loaded status; navigate with the arrow
    keys and load/unload/reload/inspect without leaving the page.
    {symbols.ARROW_U}{symbols.ARROW_D} navigate, ↵/space toggle load, l load, u unload, r reload all,
    d details, n hint for new, q back.
    """
    def _collect():
        groups = tools_mod.get_registry().list_by_source()
        out = []
        for s in skills_mod.list_skills():
            n = s["name"]
            out.append((n, s.get("description", ""), bool(s.get("loaded")),
                        len(groups.get(f"skill:{n}", []))))
        return out

    def _build_labels(items):
        labels = []
        for name, desc, loaded, ntools in items:
            badge = f"[green]{symbols.DOT} loaded[/green]" if loaded else f"[dim]{symbols.DOT_OPEN} available[/dim]"
            tool_str = f" [dim]({ntools} tool{'s' if ntools != 1 else ''})[/dim]" if ntools else ""
            desc_preview = (desc or "").replace("\n", " ")[:56]
            labels.append(f"[bold]{name}[/bold]  {badge}{tool_str}  "
                          f"[dim]{desc_preview}[/dim]")
        return labels

    sel_idx = 0
    status_msg = ""
    first = True
    while True:
        items = _collect()
        if not items:
            if first:
                console.print(f"[dim]No skills in {skills_mod.SKILLS_DIR}[/dim]")
                console.print("[dim]Create one with: /skill new <name>[/dim]")
            return
        first = False
        sel_idx = min(sel_idx, len(items) - 1)

        labels = _build_labels(items)
        hint = (f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵/space toggle  l load  u unload  "
                "r reload  d details  q back")
        if status_msg:
            hint = f"{status_msg}\n{hint}"

        result = select_dialog(
            labels,
            title="Skill Manager",
            full_screen=True,
            selected_index=sel_idx,
            action_keys={"l": "load", "u": "unload", "r": "reload",
                         "d": "details", "space": "toggle"},
            enter_action="toggle",
            hint=hint,
        )
        if result is None:
            return
        action, idx = result
        if action is None or idx < 0 or idx >= len(items):
            return

        name, desc, loaded, ntools = items[idx]
        status_msg = ""

        if action == "details":
            with _alt_screen():
                _show_skill_detail(name)
                input("\n[dim]Press Enter to continue...[/dim]")
        elif action == "reload":
            results = skills_mod.reload_all()
            status_msg = f"Reloaded: {len(results)} skill(s) re-scanned from disk."
        elif action in ("toggle", "load", "unload"):
            want_load = action == "load" or (action == "toggle" and not loaded)
            if action == "unload" or (action == "toggle" and loaded):
                want_load = False
            if want_load:
                ok, msg = skills_mod.load_skill(name)
            else:
                ok, msg = skills_mod.unload_skill(name)
            status_msg = ("[green]" if ok else "[red]") + msg + (
                "[/green]" if ok else "[/red]")

        sel_idx = idx


# ── Meta Commands ──────────────────────────────────────────────────────

def observe_session(session, display_name: str = "", display_cmd: str = "") -> None:
    """Read-only observation mode for a sub-terminal session.

    Full-screen display that shows the session output refreshing in real-time.
    Press 'q' or Escape to return to terminal 0 (the main REPL).
    No input is forwarded to the observed session.
    """
    if not session:
        console.print("[dim]No active sub-terminal session.[/dim]")
        return

    if not session.is_alive():
        console.print("[yellow]Session has already ended.[/yellow]")
        return

    kb = KeyBindings()

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result="quit")

    # Track whether session has died during observation
    _ended = [False]

    def _get_text():
        # Pull new output first (essential for PTY sessions)
        session.read_output(timeout=0.1)

        lines = []
        if display_name:
            cmd_display = f"{display_name}: {display_cmd[:60]}" if display_cmd else display_name
        else:
            # Try to extract original command from wrapper
            raw = session.command
            m = re.search(r"--execute\s+(.+)\s+--depth\s+\d+", raw)
            if m:
                cmd_display = m.group(1).strip("'\"")[:80]
            else:
                cmd_display = raw[:80]
        alive = session.is_alive()

        if alive:
            lines.append(("bold cyan", f"{symbols.DOT} Observing: {cmd_display}\n"))
        else:
            _ended[0] = True
            lines.append(("bold red", f"■ Session ended: {cmd_display}\n"))
        lines.append((f"dim", f"Read-only  {symbols.BULLET}  q/Esc to return to terminal 0\n"))
        lines.append(("dim", "─" * 60 + "\n\n"))

        output = session.full_output
        if not output:
            output = "(waiting for output...)"

        output = strip_ansi(output)
        output_lines = output.split("\n")
        # Show last 200 lines
        if len(output_lines) > 200:
            output_lines = output_lines[-200:]

        for line in output_lines:
            lines.append(("", line + "\n"))

        # Auto-exit after showing ended state for a moment
        if _ended[0]:
            lines.append(("\n", ""))
            lines.append(("dim", "Session ended. Press q/Esc to return.\n"))

        return lines

    text_control = FormattedTextControl(_get_text)
    window = Window(content=text_control, always_hide_cursor=True)
    layout = Layout(HSplit([window]))

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        refresh_interval=0.3,
    )

    try:
        app.run()
        _clear_stale_running_loop()
    except (KeyboardInterrupt, EOFError):
        # Key bindings normally handle Esc/Ctrl+C.  Keep startup, rendering,
        # and teardown races cancellable as well.
        _clear_stale_running_loop()
        return


def enter_session(session, display_name: str = "", display_cmd: str = "") -> None:
    """Full interactive takeover of a sub-terminal session.

    All keystrokes are forwarded to the session. Type /back or /q in the
    sub-terminal to detach without closing it. Ctrl+\\ also detaches.

    Session output streams directly to the terminal — exactly like
    using a real terminal.
    """
    if not session:
        console.print("[dim]No active sub-terminal session.[/dim]")
        return

    if not session.is_alive():
        console.print("[yellow]Session has already ended.[/yellow]")
        return

    mfd = getattr(session, 'master_fd', -1)
    if mfd < 0:
        console.print("[yellow]Session does not support raw PTY I/O (tmux mode).[/yellow]")
        return

    fd = sys.stdin.fileno()

    # Display info
    if display_name:
        cmd_display = f"{display_name}: {display_cmd[:60]}" if display_cmd else display_name
    else:
        cmd_display = getattr(session, 'command', 'sub-terminal')[:80]

    # Save terminal state
    old_tcattr = termios.tcgetattr(fd)
    old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    old_sigquit = signal.getsignal(signal.SIGQUIT)
    old_sigwinch = signal.getsignal(signal.SIGWINCH)

    def _get_winsize():
        """Return (rows, cols, xpix, ypix) of the outer terminal."""
        import struct
        try:
            ws = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                             struct.pack('HHHH', 0, 0, 0, 0))
            return struct.unpack('HHHH', ws)
        except (OSError, struct.error):
            return (24, 80, 0, 0)

    def _sync_winsize():
        """Push the outer terminal size into the sub-terminal PTY."""
        import struct
        try:
            ws = _get_winsize()
            fcntl.ioctl(mfd, termios.TIOCSWINSZ, struct.pack('HHHH', *ws))
        except (OSError, struct.error):
            pass

    # Sync window size before entering so programs render correctly
    _sync_winsize()

    # Clear screen and show a minimal header (no pending-output replay —
    # stale ANSI absolute-position sequences cause cursor misalignment).
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(f"\033[2m{symbols.DOT} {cmd_display}  │  /back or /q detach  │  Ctrl+\\ force-detach\033[0m\n")
    sys.stdout.write("─" * 60 + "\n")
    sys.stdout.flush()

    # Trigger the sub-terminal to redraw its current content
    try:
        import os as _os
        _os.write(mfd, b'\x0c')   # Ctrl+L — most shells/apps redraw on this
    except OSError:
        pass

    detached = False
    session_died = False

    def _on_sigquit(signum, frame):
        nonlocal detached
        detached = True

    def _on_sigwinch(signum, frame):
        """Forward terminal resize to the sub-terminal PTY."""
        _sync_winsize()

    signal.signal(signal.SIGQUIT, _on_sigquit)
    signal.signal(signal.SIGWINCH, _on_sigwinch)

    # Detach marker: emitted by laintas-cli when user types /back
    DETACH_MARKER = b'\x1b]777;LAINTAS_DETACH\x07'
    partial_buf = b''

    try:
        # Ensure stdin is in blocking mode — prompt_toolkit may leave O_NONBLOCK
        # set after app.run() exits, which makes select() report fd as always
        # readable and os.read() raise EAGAIN, causing the I/O loop to exit
        # immediately (resulting in read-only "observe" behavior).
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags & ~os.O_NONBLOCK)
        tty.setraw(fd)

        while session.is_alive() and not detached:
            try:
                r, _, _ = select.select([fd, mfd], [], [], 0.1)
            except (select.error, ValueError):
                break

            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    # Transient EAGAIN — shouldn't happen after clearing
                    # O_NONBLOCK, but guard anyway.
                    continue
                except OSError:
                    break
                if not data:
                    break
                # Ctrl+\ (byte 0x1c) → force detach
                if data == b'\x1c':
                    detached = True
                    break
                try:
                    os.write(mfd, data)
                except OSError:
                    break

            if mfd in r:
                try:
                    data = os.read(mfd, 4096)
                except OSError:
                    break
                if data:
                    # Prepend any partial marker from previous read
                    data = partial_buf + data
                    partial_buf = b''

                    # Check for /back detach marker
                    if DETACH_MARKER in data:
                        data = data.replace(DETACH_MARKER, b'')
                        detached = True

                    # Keep suffix that might be a partial marker
                    for i in range(len(DETACH_MARKER) - 1, 0, -1):
                        if data.endswith(DETACH_MARKER[:i]):
                            partial_buf = data[-i:]
                            data = data[:-i]
                            break

                    if data:
                        try:
                            sys.stdout.buffer.write(data)
                            sys.stdout.flush()
                        except (OSError, BrokenPipeError):
                            break
                else:
                    # EOF — sub-terminal exited
                    session_died = True
                    break

    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old_tcattr)
        except termios.error:
            pass
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        except OSError:
            pass
        signal.signal(signal.SIGQUIT, old_sigquit)
        signal.signal(signal.SIGWINCH, old_sigwinch)

    if session_died:
        console.print(f"\n[dim]{symbols.DOT} Sub-terminal exited. Returned to term0[/dim]")
    else:
        console.print(f"\n[green]{symbols.DOT} Detached. Returned to term0[/green]")


_extra_cmd_handler_cache = None
_extra_cmd_mtime_cache: float = 0
_extra_cmd_trust_warnings: set[str] = set()
_EXTRA_CMD_FAILED = object()


def _load_extra_commands():
    """Load .laintas/commands.py and return handle_extra_command() if defined."""
    global _extra_cmd_handler_cache, _extra_cmd_mtime_cache
    path = paths.project_file(paths.CWD_COMMANDS)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if mtime == _extra_cmd_mtime_cache:
        return _extra_cmd_handler_cache
    try:
        allowed, reason = trust_store.is_execution_allowed(path)
        if not allowed:
            warning_key = f"{path}:{mtime}:{reason}"
            if warning_key not in _extra_cmd_trust_warnings:
                _extra_cmd_trust_warnings.add(warning_key)
                console.print(
                    f"[yellow]Restricted Mode: not executing {path} ({reason}). "
                    "Use /trust allow after reviewing it.[/yellow]")
            _extra_cmd_handler_cache = None
            _extra_cmd_mtime_cache = mtime
            return None
        src = path.read_text(encoding="utf-8")
        ns = {}
        exec(compile(src, str(path), "exec"), ns)
        handler = ns.get("handle_extra_command")
        _extra_cmd_handler_cache = handler
        _extra_cmd_mtime_cache = mtime
        return handler
    except Exception as exc:
        if not isinstance(exc, FileNotFoundError):
            console.print(
                f"[red].laintas/commands.py failed to load: "
                f"{type(exc).__name__}: {exc}[/red]")
        _extra_cmd_handler_cache = None
        _extra_cmd_mtime_cache = mtime
        return None


def handle_version_command(parts: list) -> None:
    """Handle `/v` (show version + check) and `/v update` (self-update)."""
    import updater

    sub = parts[1].lower() if len(parts) > 1 else ""
    force = any(p in ("--force", "-f") for p in parts[2:])

    if sub not in ("", "update", "check"):
        console.print("[yellow]Usage: /v  |  /v check  |  /v update [--force][/yellow]")
        return

    console.print(f"[bold]laintas-cli[/bold] [cyan]v{updater.LOCAL_VERSION}[/cyan] "
                  f"([dim]{'binary' if updater.is_frozen() else 'source'} install[/dim])")

    # `/v` and `/v check` both just check; `/v update` also applies.
    try:
        manifest = updater.fetch_manifest()
    except Exception as e:
        console.print(f"[red]Could not reach the update server: {e}[/red]")
        return

    remote_ver = manifest.get("version", "?")
    channel_dir = os.environ.get("LAINTAS_UPDATE_CHANNEL", "latest")
    available = updater.is_newer(remote_ver, updater.LOCAL_VERSION)

    if available:
        console.print(f"[green]Update available:[/green] v{remote_ver}")
        if manifest.get("notes"):
            console.print(f"  [dim]{manifest['notes']}[/dim]")
    else:
        console.print(f"[dim]Latest is v{remote_ver} — you are up to date.[/dim]")

    if sub != "update":
        if available:
            console.print("[dim]Run [bold]/v update[/bold] to install it.[/dim]")
        return

    if not available and not force:
        console.print("[dim]Nothing to update. Use [bold]/v update --force[/bold] to re-apply the latest.[/dim]")
        return

    # ── apply ──
    if updater.is_frozen():
        console.print("[yellow]Binary install — replacing the whole executable "
                      "(partial update only applies to source installs).[/yellow]")
        new_path = updater.apply_frozen_update(manifest, channel_dir, console.print)
        if new_path:
            console.print(f"[green]Updated binary at {new_path}. Restarting...[/green]")
            stop_trigger_scanner()
            close_all_terminals()
            browser_mod.close_all_browser_sessions()
            # The updater returns the executable path it just replaced.  Do not
            # use _LAUNCH_SCRIPT_PATH here: for a PATH-based launch argv[0] may
            # be only "laintas-cli", which resolves against the launch cwd.
            _restart_process(new_path)
        return

    changed = updater.plan_changed_files(manifest)
    if not changed:
        console.print("[green]All files already match the latest — nothing to download.[/green]")
        return

    console.print(f"[bold]Downloading {len(changed)} changed file(s):[/bold]")
    ok = updater.apply_source_update(manifest, changed, channel_dir, console.print)
    if not ok:
        console.print("[red]Update failed — no changes applied.[/red]")
        return

    console.print(f"[green]Updated to v{remote_ver} "
                  f"({len(changed)} file(s) replaced). Restarting...[/green]")
    stop_trigger_scanner()
    close_all_terminals()
    browser_mod.close_all_browser_sessions()
    _restart_process()


class SlashCommandUsageError(ValueError):
    """A recoverable slash-command parsing/usage error."""


@dataclass(frozen=True)
class SlashArgRule:
    """Opt-in argument contract for a stable built-in slash-command leaf.

    Commands without a rule remain open-ended.  This is deliberate: project
    extensions and built-ins that consume free-form text must not be constrained
    by a global argument policy.
    """

    max_args: int
    usage: str
    flag_start: Optional[int] = None
    allowed_flags: frozenset[str] = frozenset()


def _arg_rule(max_args: int, usage: str, *, flag_start: Optional[int] = None,
              allowed_flags: tuple[str, ...] = ()) -> SlashArgRule:
    return SlashArgRule(
        max_args=max_args,
        usage=usage,
        flag_start=flag_start,
        allowed_flags=frozenset(allowed_flags),
    )


# Keys are (canonical command, optional subcommand).  Rules are intentionally
# opt-in and leaf-specific: adding a command or a free-form argument remains
# backwards compatible until that exact leaf is declared stable here.
_SLASH_ARG_RULES: dict[tuple[str, ...], SlashArgRule] = {
    **{
        (name,): _arg_rule(0, name)
        for name in (
            "/cwd", "/scan", "/login", "/max",
            "/tools", "/prop", "/snapshots", "/continue",
        )
    },
    ("/help",): _arg_rule(1, "/help [command]"),
    ("/helpwo",): _arg_rule(4, "/helpwo [--port N] [--dist <path>]"),
    ("/helpwo", "stop"): _arg_rule(1, "/helpwo stop"),
    ("/terminate",): _arg_rule(1, "/terminate <name>"),
    ("/abort",): _arg_rule(1, "/abort <agent-id>"),
    ("/agent",): _arg_rule(1, "/agent [agent-id-or-name]"),
    ("/undo",): _arg_rule(1, "/undo [sha]"),
    ("/detail",): _arg_rule(1, "/detail [on|off]"),
    ("/model", "reset"): _arg_rule(1, "/model reset"),
    ("/model", "clear"): _arg_rule(1, "/model clear"),
    ("/model", "default"): _arg_rule(1, "/model default"),
    ("/mode", "act"): _arg_rule(2, "/mode act [always]"),
    ("/mode", "always"): _arg_rule(1, "/mode always"),
    ("/mode", "review"): _arg_rule(1, "/mode review"),
    ("/mode", "study"): _arg_rule(1, "/mode study"),
    ("/mode", "approve"): _arg_rule(1, "/mode approve"),
    ("/mode", "list"): _arg_rule(1, "/mode list"),
    ("/mode", "status"): _arg_rule(1, "/mode status"),
    ("/plan", "submit"): _arg_rule(1, "/plan submit"),
    ("/plan", "approve"): _arg_rule(1, "/plan approve"),
    ("/plan", "exit"): _arg_rule(1, "/plan exit"),
    ("/plan", "status"): _arg_rule(1, "/plan status"),
    ("/plan", "list"): _arg_rule(1, "/plan list"),
    ("/memory", "project"): _arg_rule(1, "/memory project"),
    ("/memory", "persistent"): _arg_rule(1, "/memory persistent"),
    ("/memory", "global"): _arg_rule(1, "/memory global"),
    ("/memory", "local"): _arg_rule(1, "/memory local"),
    ("/memory", "show"): _arg_rule(2, "/memory show <id|name>"),
    ("/mail", "inbox"): _arg_rule(
        2, "/mail inbox [--all]", flag_start=1, allowed_flags=("--all",)),
    ("/mail", "read"): _arg_rule(2, "/mail read <n>"),
    ("/backend", "status"): _arg_rule(1, "/backend status"),
    ("/backend", "list"): _arg_rule(1, "/backend list"),
    ("/backend", "use"): _arg_rule(2, "/backend use <name>"),
    ("/backend", "config"): _arg_rule(1, "/backend config"),
    ("/work", "status"): _arg_rule(1, "/work status"),
    ("/work", "list"): _arg_rule(1, "/work list"),
    ("/work", "resume"): _arg_rule(2, "/work resume <id>"),
    ("/work", "history"): _arg_rule(1, "/work history"),
    ("/hwg", "status"): _arg_rule(2, "/hwg status [runId]"),
    ("/hwg", "cancel"): _arg_rule(2, "/hwg cancel <runId>"),
    ("/task", "list"): _arg_rule(1, "/task list"),
    ("/task", "show"): _arg_rule(2, "/task show <id>"),
    ("/task", "start"): _arg_rule(2, "/task start <id>"),
    ("/task", "done"): _arg_rule(2, "/task done <id>"),
    ("/task", "del"): _arg_rule(2, "/task del <id>"),
    ("/task", "progress"): _arg_rule(3, "/task progress <id> <n>"),
    ("/workflow", "status"): _arg_rule(1, "/workflow status"),
    ("/workflow", "list"): _arg_rule(1, "/workflow list"),
    ("/trust", "status"): _arg_rule(1, "/trust status"),
    ("/trust", "allow"): _arg_rule(
        2, "/trust allow [--yes]", flag_start=1, allowed_flags=("--yes",)),
    ("/trust", "revoke"): _arg_rule(1, "/trust revoke"),
    ("/hooks", "status"): _arg_rule(1, "/hooks status"),
    ("/hooks", "trust"): _arg_rule(
        2, "/hooks trust [--yes]", flag_start=1, allowed_flags=("--yes",)),
    ("/hooks", "revoke"): _arg_rule(1, "/hooks revoke"),
    ("/hooks", "reload"): _arg_rule(1, "/hooks reload"),
    ("/policy", "audit"): _arg_rule(1, "/policy audit"),
    ("/policy", "enforce"): _arg_rule(1, "/policy enforce"),
    ("/policy", "disabled"): _arg_rule(
        2, "/policy disabled [--yes]", flag_start=1,
        allowed_flags=("--yes",)),
    ("/policy", "reset"): _arg_rule(1, "/policy reset"),
    ("/skill", "manager"): _arg_rule(1, "/skill manager"),
    ("/skill", "list"): _arg_rule(1, "/skill list"),
    ("/skill", "trust"): _arg_rule(
        3, "/skill trust <name> [--yes]", flag_start=2,
        allowed_flags=("--yes",)),
    ("/skill", "revoke"): _arg_rule(2, "/skill revoke <name>"),
    ("/skill", "load"): _arg_rule(2, "/skill load <name>"),
    ("/skill", "unload"): _arg_rule(2, "/skill unload <name>"),
    ("/skill", "reload"): _arg_rule(1, "/skill reload"),
    ("/skill", "new"): _arg_rule(2, "/skill new <name>"),
    ("/skill", "dir"): _arg_rule(1, "/skill dir"),
    ("/mcp", "list"): _arg_rule(1, "/mcp list"),
    ("/mcp", "trust"): _arg_rule(
        3, "/mcp trust <name> [--yes]", flag_start=2,
        allowed_flags=("--yes",)),
    ("/mcp", "revoke"): _arg_rule(2, "/mcp revoke <name>"),
    ("/mcp", "tools"): _arg_rule(2, "/mcp tools <name>"),
    ("/mcp", "connect"): _arg_rule(2, "/mcp connect <name>"),
    ("/mcp", "disconnect"): _arg_rule(2, "/mcp disconnect <name>"),
    ("/mcp", "reload"): _arg_rule(1, "/mcp reload"),
    ("/mcp", "init"): _arg_rule(1, "/mcp init"),
    ("/mcp", "config"): _arg_rule(1, "/mcp config"),
    ("/bash", "list"): _arg_rule(1, "/bash list"),
    ("/bash", "add"): _arg_rule(2, "/bash add <command>"),
    ("/bash", "remove"): _arg_rule(2, "/bash remove <command>"),
    ("/debug", "clear"): _arg_rule(1, "/debug clear"),
    ("/told", "all"): _arg_rule(1, "/told all"),
    ("/told", "reply"): _arg_rule(2, "/told reply [N]"),
    ("/told", "log"): _arg_rule(2, "/told log [N]"),
    ("/version", "check"): _arg_rule(1, "/version check"),
    ("/version", "update"): _arg_rule(
        2, "/version update [--force]", flag_start=1,
        allowed_flags=("--force", "-f")),
    ("/version", "--force"): _arg_rule(0, "/version [check|update [--force]]"),
    ("/version", "-f"): _arg_rule(0, "/version [check|update [--force]]"),
    ("/update", "check"): _arg_rule(1, "/update check"),
    ("/update", "--force"): _arg_rule(1, "/update [--force]"),
    ("/update", "-f"): _arg_rule(1, "/update [-f]"),
}


def _validate_slash_args(action: str, args: list[str]) -> None:
    """Reject ignored arguments only for explicitly contracted built-ins."""
    spec = _find_command_spec(action)
    if spec is None:
        return
    canonical = spec.name.lower()
    sub = args[0].lower() if args else ""
    normalized_action = action.lower()
    rule = _SLASH_ARG_RULES.get((normalized_action, sub))
    if rule is None:
        rule = _SLASH_ARG_RULES.get((normalized_action,))
    if rule is None:
        rule = _SLASH_ARG_RULES.get((canonical, sub))
    if rule is None:
        rule = _SLASH_ARG_RULES.get((canonical,))
    if rule is None:
        return
    if len(args) > rule.max_args:
        raise SlashCommandUsageError(f"Usage: {rule.usage}")
    if rule.flag_start is not None:
        invalid = [item for item in args[rule.flag_start:]
                   if item not in rule.allowed_flags]
        if invalid:
            raise SlashCommandUsageError(
                f"Unexpected argument: {invalid[0]}. Usage: {rule.usage}")


def _parse_slash_command(cmd: str) -> tuple[str, str, list[str]]:
    """Return (action, raw_args, argv) without losing raw argument spacing."""
    stripped = (cmd or "").strip()
    if not stripped:
        raise SlashCommandUsageError("Empty slash command. Run /help for available commands.")
    match = re.match(r"^(\S+)(?:\s+(.*))?$", stripped, re.DOTALL)
    if match is None:
        raise SlashCommandUsageError("Could not parse command. Run /help for usage.")
    action = match.group(1).lower()
    raw_args = match.group(2) or ""
    try:
        args = shlex.split(raw_args) if raw_args else []
    except ValueError as exc:
        raise SlashCommandUsageError(
            f"Invalid quoting: {exc}. Close the quote or escape it, then retry."
        ) from exc
    return action, raw_args, [action, *args]


def _raw_tail_after_word(text: str) -> tuple[str, str]:
    """Consume one whitespace-delimited identifier and preserve the raw tail."""
    stripped = (text or "").lstrip()
    if not stripped:
        return "", ""
    match = re.match(r"(\S+)(?:\s+(.*))?$", stripped, re.DOTALL)
    if match is None:
        return stripped, ""
    return match.group(1), match.group(2) or ""


def _decode_text_arg(text: str) -> str:
    """Decode a single quoted argument; otherwise preserve the original text."""
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        parsed = shlex.split(raw)
    except ValueError:
        return raw
    return parsed[0] if len(parsed) == 1 else raw


def _normalize_slash_arg(token: str) -> str:
    """Return the token as a string (quotes already stripped by shlex)."""
    return str(token or "")


def _json_arg_candidates(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    decoded = _decode_text_arg(raw)
    return list(dict.fromkeys([raw, decoded]))


def _parse_hire_profile(
        args: list[str]) -> tuple[Optional[str], EmployeeProfile, dict]:
    """Parse /hire without coupling employee profiles to argparse globals."""
    import agent_roles

    args = [_normalize_slash_arg(item) for item in args]
    name: Optional[str] = None
    role_name: Optional[str] = None
    prompt = ""
    allowed_tools: Optional[list[str]] = None
    tools_explicit = False
    options = {"model": "", "provider": "", "terminal": "",
               "choose_model": False}
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--model":
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                options["choose_model"] = True
                i += 1
            else:
                options["model"] = args[i + 1]
                i += 2
            continue
        if token in {"--profile", "--prompt", "--tools", "--terminal"}:
            if i + 1 >= len(args):
                raise SlashCommandUsageError(
                    f"{token} requires a value. Usage: /hire [name] "
                    "[--profile role] [--prompt file] [--tools name,...] "
                    "[--model [id]] [--terminal name]")
            value = args[i + 1]
            if token == "--profile":
                role_name = value
            elif token == "--prompt":
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = Path.cwd() / path
                try:
                    prompt = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise SlashCommandUsageError(
                        f"Could not read employee prompt {path}: {exc}") from exc
                if len(prompt) > 100_000:
                    raise SlashCommandUsageError(
                        "Employee prompt exceeds the 100,000 character limit.")
            else:
                if token == "--terminal":
                    options["terminal"] = value
                else:
                    tools_explicit = True
                    if value.lower() == "inherit":
                        allowed_tools = None
                    else:
                        allowed_tools = [
                            item.strip() for item in value.split(",") if item.strip()
                        ]
            i += 2
            continue
        if token.startswith("--"):
            raise SlashCommandUsageError(f"Unknown /hire option: {token}")
        if name is not None:
            raise SlashCommandUsageError(
                "Usage: /hire [name] [--profile role] [--prompt file] "
                "[--tools name,...] [--model [id]] [--terminal name]")
        name = token
        i += 1

    role = agent_roles.get_role(role_name) if role_name else None
    if role_name and role is None:
        available = ", ".join(item.name for item in agent_roles.list_roles())
        raise SlashCommandUsageError(
            f"Unknown employee profile '{role_name}'. Available: {available}")
    if (not tools_explicit and allowed_tools is None
            and role is not None and role.allowed_tools):
        allowed_tools = list(role.allowed_tools)
    if not tools_explicit and allowed_tools is None and role is None:
        # Hiring freezes a capability snapshot. Newly installed tools are not
        # silently granted to existing employees; use --tools inherit to opt in.
        allowed_tools = sorted(
            tool.name for tool in tools_mod.get_registry().list())
    if tools_explicit and allowed_tools is not None:
        known = {tool.name for tool in tools_mod.get_registry().list()}
        unknown = sorted(set(allowed_tools) - known)
        if unknown:
            raise SlashCommandUsageError(
                f"Unknown employee tool(s): {', '.join(unknown)}")

    profile = EmployeeProfile(
        title=(role.name.replace("-", " ").title() if role else "General Agent"),
        description=(role.description if role else
                     "General-purpose autonomous employee"),
        specialist_role=role_name,
        prompt=prompt,
        capability_tags=([role.name] if role else ["general"]),
        tool_policy=AgentToolPolicy(allowed_tools=allowed_tools),
    )
    return name, profile, options


def _employee_capability_text(agent: AgentInfo) -> str:
    """Render an employee summary as a styled, copy-friendly information card."""
    profile = agent.profile
    allowed = profile.tool_policy.allowed_tools
    if allowed is None:
        tools_text = "inherit current company policy"
    elif not allowed:
        tools_text = "none"
    else:
        # Keep the default card scannable.  The complete tool list remains
        # available through /agents <id> and /detail on when inspecting runs.
        visible_tools = list(allowed[:6])
        tools_text = ", ".join(visible_tools)
        if len(allowed) > len(visible_tools):
            tools_text += f"  +{len(allowed) - len(visible_tools)} more"
    prompt_text = "custom overlay" if profile.prompt.strip() else (
        f"role:{profile.specialist_role}" if profile.specialist_role else
        "company default")
    assignment = agent.active_assignment
    assignment_text = (
        f"{assignment.id} [{assignment.status}] — {assignment.task}"
        if assignment else "(none)"
    )
    last_assignment = (agent.assignment_history[-1]
                       if agent.assignment_history else None)
    last_text = (
        f"{last_assignment.get('id')} [{last_assignment.get('status')}] — "
        f"{str(last_assignment.get('result') or last_assignment.get('error') or '')[:240]}"
        if last_assignment else "(none)"
    )
    def _v(value: object) -> str:
        return escape(str(value or ""))

    tags = " ".join(
        f"[cyan]#{_v(tag)}[/cyan]" for tag in (profile.capability_tags or [])) or "[dim]none[/dim]"
    model = _v(agent.base_model or "backend default")
    if agent.base_provider:
        model += f" [dim]({_v(agent.base_provider)})[/dim]"
    return (
        "[bold cyan]Identity[/bold cyan]\n"
        f"  [muted]ID[/muted]          {_v(agent.id)}\n"
        f"  [muted]Name[/muted]        [bold]{_v(agent.name)}[/bold]\n"
        f"  [muted]Role[/muted]        {_v(profile.title)}\n"
        f"  [muted]Description[/muted] {_v(profile.description)}\n\n"
        "[bold cyan]Capabilities[/bold cyan]\n"
        f"  {tags}\n"
        f"  [muted]Tools[/muted]       {_v(tools_text)}\n"
        f"  [muted]Prompt[/muted]      {_v(prompt_text)}\n\n"
        "[bold cyan]Runtime[/bold cyan]\n"
        f"  [muted]Home[/muted]        {_v(agent.home_terminal or '(none)')}\n"
        f"  [muted]Deployment[/muted]  {_v(agent_deployment_terminal(agent) or '(temporary when assigned)')}\n"
        f"  [muted]Model[/muted]       {model}\n"
        f"  [muted]Assignment[/muted]  {_v(assignment_text)}\n"
        f"  [muted]Completed[/muted]   {len(agent.assignment_history)}\n"
        f"  [muted]Last result[/muted] {_v(last_text)}"
    )


def _enqueue_user_input(text: str) -> bool:
    try:
        _inject_input(text, threading.Event())
        return True
    except (NameError, queue.Full):
        return False


_PROP_KNOWN_VARS = {
    "agentName", "agentId", "currentPath", "activeFile", "depth",
    "nextDepth", "globalMemory", "persistentMemory", "lastSession",
    "planMode", "tools", "skills", "skillContext", "inbox",
    "parallelResults", "children", "parent", "terminalName",
    "parentTerminal", "deploymentStatus", "workflowPhase", "rolePrompt",
    "confidenceGuidance", "behaviorDiagnostics", "promptOpt",
}
_PROP_REQUIRED_SECTIONS = ("role", "environment", "tools", "workflow", "safety")


def _load_project_memory_entries() -> tuple[list[dict], list[str], str]:
    path = paths.project_file(paths.CWD_MEMORY)
    raw = read_file(str(path)) or ""
    if not raw.strip():
        return [], [], raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"], raw
    if not isinstance(data, list):
        return [], ["Root value must be a JSON array."], raw
    entries: list[dict] = []
    errors: list[str] = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            errors.append(f"Entry {index} must be an object, got {type(item).__name__}.")
            continue
        if "id" not in item or "content" not in item:
            errors.append(f"Entry {index} must contain id and content fields.")
            continue
        if not isinstance(item.get("content"), str):
            errors.append(f"Entry {index} content must be a string.")
            continue
        entries.append(item)
    return entries, errors, raw


def _print_long_panel(content: str, title: str, border_style: str = "cyan") -> None:
    panel = Panel(content or "(empty)", title=title, border_style=border_style)
    if len(content) > 2000 and sys.stdin.isatty() and console.is_terminal:
        with console.pager(styles=True):
            console.print(panel)
    else:
        console.print(panel)


def _redact_sensitive_text(text: str) -> str:
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|token|secret|password|passwd|cookie)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)(\"(?:api[_-]?key|token|secret|password|passwd|cookie)\"\s*:\s*\")[^\"]+", r"\1[REDACTED]"),
    )
    redacted = text
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def _validate_prop_template(prop: str) -> tuple[list[str], list[str], list[str]]:
    raw_variables = re.findall(r"\{\{(.*?)\}\}", prop, re.DOTALL)
    variables = sorted({
        value for value in raw_variables
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value)
    })
    errors: list[str] = []
    warnings: list[str] = []
    invalid_variables = sorted({
        value for value in raw_variables
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value)
    })
    if invalid_variables:
        errors.append(
            "Malformed placeholders: "
            + ", ".join("{{" + value + "}}" for value in invalid_variables))
    if prop.count("{{") != prop.count("}}"):
        errors.append("Unbalanced placeholder braces")
    unknown = sorted(set(variables) - _PROP_KNOWN_VARS)
    if unknown:
        errors.append(f"Unknown placeholders: {', '.join(unknown)}")
    for section in _PROP_REQUIRED_SECTIONS:
        opens = prop.count(f"<{section}>")
        closes = prop.count(f"</{section}>")
        if opens == 0 or closes == 0:
            errors.append(f"Missing required <{section}>...</{section}> section")
        elif opens != closes:
            errors.append(f"Unbalanced <{section}> tags ({opens} open, {closes} close)")
        elif opens > 1:
            warnings.append(f"Required section <{section}> appears {opens} times")
    patch_count = prop.count("<prompt_opt_patch>")
    close_patch_count = prop.count("</prompt_opt_patch>")
    if patch_count != close_patch_count:
        errors.append("Unbalanced <prompt_opt_patch> tags")
    elif patch_count > 1:
        warnings.append(f"Multiple prompt optimization patches found ({patch_count})")
    if len(prop.strip()) < 200:
        warnings.append("Template is unusually short")
    return variables, errors, warnings


def _render_prop_effective(prop: str, redact: bool = True,
                           prompt_section: Optional[str] = None) -> str:
    import memory_system
    import plan_mode
    import workflow_engine

    current = get_current_agent()
    entries, memory_errors, _ = _load_project_memory_entries()
    global_memory = "\n".join(
        f"[{entry.get('id')}] {entry.get('content', '')}" for entry in entries
    ) or "(empty)"
    if memory_errors:
        global_memory = "(invalid project memory: " + "; ".join(memory_errors) + ")"
    groups = tools_mod.get_registry().list_by_source()
    tool_lines = [
        f"- {tool.name}: {tool.description}"
        for source in sorted(groups)
        for tool in groups[source]
    ]
    children = []
    if current:
        children = [cid for cid in current.child_ids if get_agent(cid)]
    values = {
        "globalMemory": global_memory,
        "persistentMemory": memory_system.get_memory_context(),
        "planMode": plan_mode.get_plan_prompt(),
        "promptOpt": (prompt_lab.get_prompt_lab_section()
                      if prompt_section is None else prompt_section),
        "agentName": current.name if current else "Laintas CLI",
        "agentId": current.id if current else "unknown",
        "currentPath": os.getcwd(),
        "activeFile": "None",
        "depth": str(current.depth if current else 0),
        "nextDepth": str((current.depth if current else 0) + 1),
        "inbox": "(empty)",
        "children": ", ".join(children) or "(none)",
        "parent": current.parent_id if current and current.parent_id else "(none)",
        "terminalName": (getattr(current, "home_terminal", None) if current else None) or "(none)",
        "parentTerminal": (getattr(current, "parent_terminal", None) if current else None) or "(none)",
        "deploymentStatus": getattr(current, "role", "primary") if current else "primary",
        "tools": "\n".join(tool_lines) or "(none)",
        "skills": skills_mod.describe_skills_for_prompt(),
        "skillContext": skills_mod.get_activated_skills_context(),
        "workflowPhase": workflow_engine.render_workflow_section(),
        "rolePrompt": "",
        "confidenceGuidance": "",
        "parallelResults": "",
        "behaviorDiagnostics": "",
        "lastSession": "",
    }
    effective = prop
    for name, value in values.items():
        effective = effective.replace("{{" + name + "}}", str(value or ""))
    custom_mode_section = (
        "" if plan_mode.is_plan_mode()
        else mode_manager.render_prompt_section()
    )
    if custom_mode_section:
        effective = effective.rstrip() + "\n\n" + custom_mode_section
    if values["promptOpt"] and "{{promptOpt}}" not in prop:
        effective = effective.rstrip() + "\n\n" + values["promptOpt"]
    return _redact_sensitive_text(effective) if redact else effective


def _prompt_lab_watch_worker(branch_id: str, agent_id: str,
                             purpose: str, prior_test_count: int = 0,
                             lab_root: Optional[str] = None) -> None:
    """Reconcile persisted lab state when a background worker terminates."""
    info = wait_for_agent(agent_id, timeout=1800)
    with prompt_lab.project_scope(lab_root):
        branch = prompt_lab.read_branch(branch_id)
        if branch is None:
            return
        if purpose == "diagnosis":
            if branch.get("candidate_patch_id"):
                return
            reply = (info.last_reply if info else "") or "Prompt Lab worker ended without a draft."
            prompt_lab.add_branch_note(branch_id, reply, kind="worker-result")
            prompt_lab.update_branch(
                branch_id,
                status="NEEDS_USER" if info and info.status == "done" else "FAILED",
            )
            return
        patch_id = str(branch.get("candidate_patch_id") or "")
        patch = prompt_lab.read_patch(patch_id) if patch_id else None
        if patch and len(patch.get("test_runs") or []) > prior_test_count:
            return
        reply = (info.last_reply if info else "") or "Prompt Lab test worker ended without a result."
        prompt_lab.add_branch_note(branch_id, reply, kind="test-worker-result")
        prompt_lab.update_branch(branch_id, status="FAILED")


def _prompt_lab_start_worker(branch_id: str, session: dict,
                             feedback: str = "") -> Optional[str]:
    parent = get_current_agent()
    if parent is None:
        return None
    task = prompt_lab.build_diagnosis_task(branch_id, feedback)
    lab_root = str(prompt_lab.project_root())
    child_id = spawn_subagent(
        parent_id=parent.id,
        task=task,
        deps=get_loop_deps(),
        name=f"prompt-lab-{branch_id[-8:]}",
        session=session,
        state_overrides={
            "_prompt_lab_branch": True,
            "_prompt_lab_root": lab_root,
        },
        report_to_parent=False,
    )
    if child_id:
        prompt_lab.update_branch(
            branch_id, status="DIAGNOSING", worker_agent_id=child_id)
        threading.Thread(
            target=_prompt_lab_watch_worker,
            args=(branch_id, child_id, "diagnosis", 0, lab_root),
            daemon=True,
            name=f"prompt-lab-watch-{branch_id[-8:]}",
        ).start()
    return child_id


def _evolution_lab_watch_worker(branch_id: str, agent_id: str,
                                lab_root: str) -> None:
    info = wait_for_agent(agent_id, timeout=1800)
    with evolution_lab.project_scope(lab_root):
        branch = evolution_lab.read_branch(branch_id)
        if branch is None or branch.get("candidate_id"):
            return
        reply = ((info.last_reply if info else "")
                 or "Evolution worker ended without drafting a candidate.")
        evolution_lab.add_branch_note(branch_id, reply, kind="worker-result")
        evolution_lab.update_branch(
            branch_id,
            status="NEEDS_USER" if info and info.status == "done" else "FAILED",
        )


def _evolution_lab_start_worker(branch_id: str, session: dict,
                                feedback: str = "") -> Optional[str]:
    parent = get_current_agent()
    if parent is None:
        return None
    task = evolution_lab.build_design_task(branch_id, feedback)
    lab_root = str(evolution_lab.project_root())
    child_id = spawn_subagent(
        parent_id=parent.id, task=task, deps=get_loop_deps(),
        name=f"evolve-{branch_id[-8:]}", session=session,
        state_overrides={
            "_evolution_lab_branch": True,
            "_evolution_lab_root": lab_root,
        },
        report_to_parent=False,
    )
    if child_id:
        evolution_lab.update_branch(
            branch_id, status="DESIGNING", worker_agent_id=child_id)
        threading.Thread(
            target=_evolution_lab_watch_worker,
            args=(branch_id, child_id, lab_root), daemon=True,
            name=f"evolution-watch-{branch_id[-8:]}",
        ).start()
    return child_id


def _prompt_lab_create(description: str, session: dict) -> dict:
    chat_history = getattr(handle_meta_command, "_last_chat_history", None) or []
    parent = get_current_agent()
    state = dict(parent.state) if parent else {}
    try:
        base_prompt = paths.project_file(paths.CWD_CLI_PROP).read_text(encoding="utf-8")
    except OSError:
        base_prompt = generate_cli_prop_template()
    lab_section = prompt_lab.get_prompt_lab_section()
    effective_prompt = base_prompt.replace("{{promptOpt}}", lab_section)
    if lab_section and "{{promptOpt}}" not in base_prompt:
        effective_prompt = effective_prompt.rstrip() + "\n\n" + lab_section
    branch = prompt_lab.capture_incident(
        description=description,
        chat_history=chat_history,
        agent_state=state,
        effective_prompt=effective_prompt,
    )
    child_id = _prompt_lab_start_worker(branch["id"], session)
    if child_id:
        branch = prompt_lab.read_branch(branch["id"]) or branch
    return branch


def _prompt_lab_start_test(patch_id: str, session: dict) -> tuple[bool, str]:
    patch = prompt_lab.read_patch(patch_id)
    if patch is None:
        return False, f"Patch {patch_id} not found."
    if not patch.get("tests"):
        return False, "The candidate has no regression cases. Refine it before testing."
    try:
        overlay = prompt_lab.compile_patch(patch)
    except ValueError as exc:
        return False, str(exc)
    branch_id = str(patch.get("branch_id") or "")
    if branch_id:
        prompt_lab.update_branch(branch_id, status="TESTING", test_agent_id=None)

    lab_root = str(prompt_lab.project_root())
    try:
        base_template = paths.project_file(paths.CWD_CLI_PROP).read_text(encoding="utf-8")
    except OSError:
        base_template = generate_cli_prop_template()
    baseline_section = prompt_lab.get_prompt_lab_section({patch_id})
    base_prompt = _render_prop_effective(
        base_template, redact=False, prompt_section=baseline_section)
    candidate_prompt = base_prompt.rstrip() + "\n\n" + overlay + "\n"
    branch = prompt_lab.read_branch(branch_id) if branch_id else None
    test_cwd = str(((branch or {}).get("snapshot") or {}).get("cwd") or os.getcwd())
    deps = get_loop_deps()

    def _backend(message: str, system_prompt: str) -> str:
        kwargs = {
            "session": session,
            "message": message,
            "system_prompt": system_prompt,
            "current_path": test_cwd,
            "history": [],
            "lang": "EN",
            "tools_enabled": False,
        }
        try:
            response = deps.call_backend(**kwargs)
        except TypeError:
            kwargs.pop("tools_enabled", None)
            response = deps.call_backend(**kwargs)
        if not isinstance(response, dict):
            raise RuntimeError("backend returned a non-object response")
        if response.get("error") and not response.get("reply"):
            raise RuntimeError(str(response.get("error")))
        return str(response.get("reply") or response.get("msg") or "")

    def _parse_judgment(text: str) -> dict:
        candidates = [text.strip()]
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "candidate_passed" in value:
                return value
        raise RuntimeError("test judge did not return valid JSON")

    def _runner() -> None:
        try:
            results = []
            for case in patch.get("tests") or []:
                user_input = str(case.get("input") or "")
                baseline_reply = _backend(user_input, base_prompt)
                candidate_reply = _backend(user_input, candidate_prompt)
                judge_input = json.dumps({
                    "test": case,
                    "baseline_reply": baseline_reply,
                    "candidate_reply": candidate_reply,
                }, ensure_ascii=False)
                judgment_text = _backend(
                    judge_input,
                    "You are a strict prompt regression judge. Compare the two replies "
                    "against expected and forbidden behavior. A forbidden safety behavior "
                    "is an automatic failure. Return JSON only with boolean keys "
                    "baseline_passed and candidate_passed, plus a short reason string.",
                )
                judgment = _parse_judgment(judgment_text)
                results.append({
                    "name": case.get("name"),
                    "baseline_passed": bool(judgment.get("baseline_passed")),
                    "candidate_passed": bool(judgment.get("candidate_passed")),
                    "reason": str(judgment.get("reason") or "")[:2000],
                    "baseline_reply": baseline_reply[:4000],
                    "candidate_reply": candidate_reply[:4000],
                })
            passed = bool(results) and all(item["candidate_passed"] for item in results)
            improved = sum(
                1 for item in results
                if item["candidate_passed"] and not item["baseline_passed"])
            report = (
                f"Candidate passed {sum(1 for item in results if item['candidate_passed'])}/"
                f"{len(results)} cases; baseline passed "
                f"{sum(1 for item in results if item['baseline_passed'])}/{len(results)}; "
                f"improved cases: {improved}."
            )
            with prompt_lab.project_scope(lab_root):
                prompt_lab.record_test_result(
                    patch_id, passed, report, cases=results)
            console.print(
                f"\n[{'green' if passed else 'red'}]Prompt Lab test "
                f"{patch_id}: {report}[/{'green' if passed else 'red'}]")
        except Exception as exc:
            with prompt_lab.project_scope(lab_root):
                prompt_lab.add_branch_note(
                    branch_id, f"Regression test failed: {exc}", kind="test-error")
                prompt_lab.update_branch(branch_id, status="FAILED")
            console.print(f"\n[red]Prompt Lab test {patch_id} failed: {exc}[/red]")

    threading.Thread(
        target=_runner, daemon=True,
        name=f"prompt-replay-test-{patch_id[-8:]}",
    ).start()
    return True, "Baseline/candidate replay test started in a no-tools background runner."


def _review_and_approve_current_plan() -> Optional[dict]:
    """Review and approve one immutable WorkGraph revision, failing closed."""
    import plan_mode as _pm
    import workgraph as _wg
    import workflow_engine as _workflow

    snapshot = _pm.get_review_snapshot()
    if snapshot is None or snapshot["work"].get("status") != "REVIEW_PENDING":
        snapshot = _pm.submit_current_plan()
    if snapshot is None:
        console.print("[red]The plan is not ready for review. Ask the AI to complete it first.[/red]")
        return None
    work = snapshot["work"]
    revision = snapshot["revision"]
    steps = snapshot.get("steps") or []
    diff_text = ""
    if int(revision["revision"]) > 1:
        import difflib
        previous = _wg.get_revision(
            work["id"], int(revision["revision"]) - 1,
            cwd=work.get("cwd"))
        if previous:
            diff_text = "\n\nChanges from previous revision:\n" + "\n".join(
                difflib.unified_diff(
                    previous["content"].splitlines(),
                    revision["content"].splitlines(),
                    fromfile=f"revision-{int(revision['revision']) - 1}",
                    tofile=f"revision-{revision['revision']}",
                    lineterm=""))
    body = (
        f"Work: {work['id']}\n"
        f"Revision: {revision['revision']}\n"
        f"SHA-256: {revision['content_sha']}\n"
        f"Steps: {len(steps)}\n\n"
        f"{revision['content']}{diff_text}"
    )
    choice = _blocking_approval_prompt(
        "Plan revision review",
        body,
        "Approve this exact revision and execute it?",
        allow_always=False,
    )
    if choice != "yes":
        console.print(
            "[yellow]Plan not approved. It remains available for AI revision; "
            "use /plan revise <feedback>.[/yellow]")
        return None
    approved = _pm.approve_submitted_plan(
        revision["revision"], revision["content_sha"])
    if approved is None:
        console.print("[red]Plan changed during review; review the latest revision again.[/red]")
        return None
    try:
        _wg.begin_execution(
            work["id"], revision["revision"], revision["content_sha"],
            cwd=approved.get("cwd"))
    except _wg.WorkGraphError as exc:
        console.print(f"[red]Could not start approved execution: {exc}[/red]")
        return None
    wf = _workflow.get_active_workflow()
    if (wf and not wf.completed and wf.current
            and wf.current.exit_condition == "user_confirm"):
        try:
            _workflow.advance_phase(
                f"User approved WorkGraph revision {revision['revision']} "
                f"({revision['content_sha'][:12]}).",
                user_confirmed=True)
        except _workflow.WorkflowTransitionError:
            pass
    approved["work_id"] = work["id"]
    approved["revision"] = revision["revision"]
    approved["content_sha"] = revision["content_sha"]
    return approved


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_cents(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _usage_daily_values(daily: list, days: int) -> list[int]:
    """Calls per day (balance + subscription), zero-filled, oldest → newest."""
    by_date = {d.get("date", ""): (int(d.get("calls", 0) or 0)
                                   + int(d.get("sub_calls", 0) or 0))
               for d in daily if isinstance(d, dict)}
    today = datetime.now()
    return [by_date.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), 0)
            for i in range(days - 1, -1, -1)]


def _usage_sparkline(values: list[int]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    peak = max(values) or 1
    return "".join(
        "[rule]▁[/rule]" if v == 0
        else f"[accent]{blocks[min(7, max(0, round(v / peak * 7)))]}[/accent]"
        for v in values)


_USAGE_BAR_W = 14


def _usage_bar(value: int, peak: int, style: str = "accent") -> str:
    """Proportional horizontal bar, one row per model."""
    if peak <= 0:
        return "[rule]" + "╌" * _USAGE_BAR_W + "[/rule]"
    filled = 0 if value <= 0 else max(1, round(value / peak * _USAGE_BAR_W))
    return (f"[{style}]{'━' * filled}[/{style}]"
            f"[rule]{'╌' * (_USAGE_BAR_W - filled)}[/rule]")


def _usage_section(marker_style: str, title: str, note: str = "") -> Text:
    head = Text()
    head.append("▍", style=marker_style)
    head.append(f" {title}", style="bold")
    if note:
        head.append(f"   {note}", style="muted")
    return head


def _usage_model_tiers(balance: dict) -> dict[str, str]:
    """Return the backend pricing map as lowercase model id -> T tier."""
    pricing = balance.get("pricing") if isinstance(balance, dict) else None
    tiers = pricing.get("tiers") if isinstance(pricing, dict) else None
    if not isinstance(tiers, list):
        return {}
    result: dict[str, str] = {}
    for entry in tiers:
        if not isinstance(entry, dict):
            continue
        tier = str(entry.get("tier") or "").strip().upper()
        if not tier:
            continue
        for model in entry.get("models") or []:
            model_id = str(model or "").strip().lower()
            if model_id:
                result[model_id] = tier
    return result


def _usage_pricing_note(balance: dict) -> str:
    """Return a compact user-facing summary of notable model tiers."""
    tiers = _usage_model_tiers(balance)
    if not tiers:
        return ""
    pricing = balance.get("pricing") if isinstance(balance, dict) else None
    default_tier = str(
        pricing.get("defaultTier") if isinstance(pricing, dict) else ""
    ).strip().upper()
    parts = []
    if default_tier:
        parts.append(f"unlisted models {default_tier}")
    return f"pricing {symbols.BULLET} " + f" {symbols.BULLET} ".join(parts) if parts else ""


def _show_usage_command(args: list, session: dict) -> None:
    """/usage — local token accounting + Laintas backend usage (product=cli)."""
    rng, local_only = "30d", False
    for a in args:
        a = a.strip().lower()
        if a == "local":
            local_only = True
        elif a in ("7d", "30d", "90d"):
            rng = a
        elif a:
            raise SlashCommandUsageError("Usage: /usage [7d|30d|90d|local]")
    days = {"7d": 7, "30d": 30, "90d": 90}[rng]

    # Fetch backend data before rendering so its pricing tier map can annotate
    # the local per-model table. /usage local remains network-free.
    profile = get_backend_profile()
    usage, bal, fail = None, {}, ""
    if (not local_only and profile.sends_laintas_credentials
            and session.get("userId")):
        headers, cookies = backend_profiles.request_auth(profile, session)

        def _fetch_backend_usage(cancel: threading.Event):
            try:
                resp = requests.get(
                    f"{profile.base_url}/api/usage",
                    params={"range": rng, "product": "cli"},
                    headers=headers, cookies=cookies, timeout=10)
                remote_usage = (
                    resp.json() if resp.status_code == 200 else None)
                remote_fail = (
                    "" if remote_usage is not None
                    else f"HTTP {resp.status_code}")
                if cancel.is_set():
                    raise BlockingOperationCancelled("Usage fetch cancelled")
                bresp = requests.get(
                    f"{profile.base_url}/api/balance",
                    headers=headers, cookies=cookies, timeout=10)
                remote_balance = (
                    bresp.json() if bresp.status_code == 200 else {})
                return remote_usage, remote_balance, remote_fail
            except requests.RequestException as exc:
                return None, {}, type(exc).__name__

        try:
            with _safe_status(
                    f"[dim]Fetching usage… {symbols.BULLET} Esc/Ctrl+C cancel[/dim]"):
                usage, bal, fail = run_cancellable_blocking(
                    _fetch_backend_usage)
        except BlockingOperationCancelled:
            console.print("[dim]Usage request cancelled.[/dim]")
            return
    model_tiers = _usage_model_tiers(bal)

    body: list = []

    # ── LOCAL — token accounting, works for every backend ────────────
    summary = usage_tracker.summarize(days=days)
    range_totals = summary["range"]["totals"]
    body.append(_usage_section(f"accent", "LOCAL", f"this machine {symbols.BULLET} all projects"))
    body.append(Text())

    if range_totals["calls"] == 0 and summary["session"]["totals"]["calls"] == 0:
        body.append(Text("  no calls recorded yet — stats begin with your next AI request",
                         style="muted"))
    else:
        scope = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 1))
        scope.add_column("", style="muted", min_width=10)
        for col in ("calls", "input", "output", "cost"):
            scope.add_column(col, justify="right", header_style="muted", min_width=7)
        for label, key in (("session", "session"), ("today", "today"),
                           (f"{days}d", "range")):
            t = summary[key]["totals"]
            approx = "~" if t["estimated"] else ""
            dimmed = t["calls"] == 0
            row_style = "muted" if dimmed else ""
            scope.add_row(
                label,
                Text(str(t["calls"]), style=row_style or "bold"),
                Text(approx + _fmt_tokens(t["in"]), style=row_style),
                Text(approx + _fmt_tokens(t["out"]), style=row_style),
                Text("—" if dimmed else _fmt_cents(t["costCents"]),
                     style=row_style or ("success" if t["costCents"] == 0 else "")),
            )
        body.append(Padding(scope, (0, 0, 0, 1)))

        models = summary["range"]["models"]
        if models:
            body.append(Text())
            ranked = sorted(models.items(),
                            key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
            peak = ranked[0][1]["in"] + ranked[0][1]["out"]
            mt = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 1))
            mt.add_column("model", style="bold", min_width=18)
            mt.add_column("", min_width=_USAGE_BAR_W)
            mt.add_column("calls", justify="right", header_style="muted")
            mt.add_column("tokens", justify="right", header_style="muted", min_width=9)
            mt.add_column("cost", justify="right", header_style="muted", min_width=7)
            mt.add_column("tier", justify="center", header_style="muted", min_width=4)
            for name, m in ranked[:8]:
                total = m["in"] + m["out"]
                approx = "~" if m["estimated"] else ""
                bar_style = "warning" if m["estimated"] else "accent"
                mt.add_row(
                    name,
                    Text.from_markup(_usage_bar(total, peak, bar_style)),
                    str(m["calls"]),
                    f"{approx}{_fmt_tokens(total)}",
                    _fmt_cents(m["costCents"]),
                    model_tiers.get(str(name).lower(), "—"),
                )
            body.append(Padding(mt, (0, 0, 0, 1)))
        if range_totals["estimated"]:
            body.append(Text("  ~ estimated — backend sent no token counts (chars/4)",
                             style="muted"))
        # Truncations are recovered silently during a task, so this is where a
        # persistent pattern becomes visible. Shown only when it is actually
        # happening, and phrased as a rate — one truncated call in a hundred is
        # noise, one in five means the model or the ceiling is wrong.
        _trunc = range_totals.get("truncated", 0)
        if _trunc:
            _rate = _trunc * 100.0 / max(1, range_totals["calls"])
            _worst = max(
                ((n, m) for n, m in summary["range"]["models"].items()
                 if m.get("truncated")),
                key=lambda kv: kv[1]["truncated"] / max(1, kv[1]["calls"]),
                default=None,
            )
            _detail = ""
            if _worst is not None:
                _wn, _wm = _worst
                _detail = (f" {symbols.BULLET} worst {_wn} "
                           f"{_wm['truncated']}/{_wm['calls']}")
            body.append(Text(
                f"  {_trunc} of {range_totals['calls']} calls hit the output "
                f"limit ({_rate:.0f}%){_detail}",
                style="warning" if _rate >= 10 else "muted"))

    # ── LAINTAS — backend usage, same gateway endpoints Helpwo uses ──
    footnotes: list[str] = []
    if not local_only:
        body.append(Text())
        if not profile.sends_laintas_credentials:
            body.append(_usage_section("warning", "BACKEND", profile.base_url))
            body.append(Text())
            body.append(Text(f"  {profile.billing_label} — not billed by Laintas; "
                             "local stats above are authoritative", style="muted"))
        elif not session.get("userId"):
            body.append(_usage_section("warning", "LAINTAS", "not logged in"))
            body.append(Text())
            body.append(Text.from_markup(
                "  run [bold]/login[/bold] to see backend usage, balance and plan"))
        else:
            if not isinstance(usage, dict):
                body.append(_usage_section("warning", "LAINTAS", "unreachable"))
                body.append(Text())
                body.append(Text(f"  backend usage unavailable ({fail})", style="warning"))
            else:
                ov = usage.get("overview") or {}
                spent = int(ov.get("total_spent_cents", 0) or 0)
                bal_calls = int(ov.get("balance_calls", 0) or 0)
                sub_calls = int(ov.get("sub_calls", 0) or 0)
                plan = ((bal.get("subscription") or {}).get("plan") or "").upper()

                body.append(_usage_section("agent", "LAINTAS",
                                           f"{profile.origin} {symbols.BULLET} product=cli {symbols.BULLET} {rng}"))
                body.append(Text())
                grid = Table(box=None, show_edge=False, show_header=False,
                             pad_edge=False, padding=(0, 1))
                grid.add_column(style="muted", min_width=10)
                grid.add_column()
                if bal.get("balanceFormatted"):
                    balance_val = Text(bal["balanceFormatted"], style="bold")
                    if plan:
                        balance_val.append("  ")
                        balance_val.append(f" {plan} ",
                                           style="black on #bb9af7" if plan == "GEN"
                                           else "black on #7aa2f7")
                    grid.add_row("balance", balance_val)
                grid.add_row("spent", Text(_fmt_cents(spent),
                                           style="bold" if spent else "muted"))
                calls_val = Text()
                calls_val.append(str(bal_calls), style="bold")
                calls_val.append(" balance", style="muted")
                calls_val.append(f"  {symbols.BULLET}  ", style="rule")
                calls_val.append(str(sub_calls), style="bold")
                calls_val.append(" subscription", style="muted")
                grid.add_row("calls", calls_val)
                values = _usage_daily_values(usage.get("daily") or [], days)
                if any(values):
                    spark = Text.from_markup(_usage_sparkline(values))
                    spark.append(f"  peak {max(values)}/day", style="muted")
                    grid.add_row("activity", spark)
                body.append(Padding(grid, (0, 0, 0, 1)))
                if sub_calls:
                    footnotes.append("subscription calls carry no token counts on the "
                                     "backend — LOCAL tokens are authoritative")

                pricing_note = _usage_pricing_note(bal)
                if pricing_note:
                    footnotes.append(pricing_note)

    if not get_runtime_config("show_billing"):
        footnotes.append("/config show_billing true prints cost after every reply")
    if footnotes:
        body.append(Text())
        for note in footnotes:
            body.append(Text(f"{symbols.BULLET} {note}", style="muted"))

    console.print()
    console.print(Panel(
        Group(*body),
        box=box.ROUNDED,
        border_style="rule",
        title="[bold accent]AI usage[/bold accent]",
        title_align="left",
        subtitle=f"[muted]/usage {symbols.BULLET} {rng}[/muted]",
        subtitle_align="right",
        padding=(1, 2),
    ))


# ── Meta-command handlers ──────────────────────────────────────────────
# _handle_meta_command_impl is a big elif-chain dispatcher (one branch per
# slash command). Extracting each branch's body into its own _cmd_* function
# is a gradual, mechanical decomposition of that function — each extraction
# preserves the exact original control flow (explicit `return` where the
# original branch returned early, none where it originally fell through to
# the dispatcher's final `return False`), it just gives the branch body a
# name and moves it out of the growing elif chain.

def _cmd_palette() -> bool:
    selected = show_command_palette()
    if selected:
        if not _enqueue_user_input(selected):
            console.print("[red]Could not queue the selected command. Please run it directly.[/red]")
    return False


def _cmd_exit(raw_args: str, agent_registry: AgentRegistry) -> bool:
    if raw_args:
        console.print("[yellow]Usage: /exit[/yellow]")
        return False
    stop_trigger_scanner()
    close_all_terminals()
    browser_mod.close_all_browser_sessions()
    agent_registry.unregister()
    clear_session()
    console.print("[green]Logged out. Goodbye![/green]")
    return True


def _cmd_quit(action: str, raw_args: str, agent_registry: AgentRegistry) -> bool:
    if raw_args:
        console.print(f"[yellow]Usage: {action}[/yellow]")
        return False
    if _IN_SUB_TERMINAL:
        # Running inside a sub-terminal — detach like /back instead of quitting
        sys.stdout.write("\x1b]777;LAINTAS_DETACH\x07")
        sys.stdout.flush()
        console.print("[green]Detaching...[/green]")
        return False
    stop_trigger_scanner()
    close_all_terminals()
    browser_mod.close_all_browser_sessions()
    agent_registry.unregister()
    console.print("[green]Goodbye![/green]")
    return True


def _cmd_back(raw_args: str) -> bool:
    if raw_args:
        console.print("[yellow]Usage: /back[/yellow]")
        return False
    if not _IN_SUB_TERMINAL:
        console.print("[dim]/back only detaches from a sub-terminal.[/dim]")
        return False
    # Signal parent enter_session to detach without closing this terminal
    sys.stdout.write("\x1b]777;LAINTAS_DETACH\x07")
    sys.stdout.flush()
    console.print("[green]Detaching...[/green]")
    return False


def _cmd_help(parts: list) -> None:
    show_help(parts[1] if len(parts) > 1 else "")


def _cmd_resume() -> None:
    console.print("[yellow]/resume is handled by the main REPL. Type it at the prompt to resume a saved session.[/yellow]")


def _cmd_new_session_notice() -> None:
    console.print("[yellow]/new is handled by the main REPL. Type it at the prompt to start a new session.[/yellow]")


def _cmd_login(session: dict, agent_registry: AgentRegistry) -> None:
    choice = choose_login_method()
    if choice == "remote":
        console.print(f"[dim]Starting browser login… {symbols.BULLET} Esc/Ctrl+C cancel[/dim]")
        try:
            new_session = run_cancellable_blocking(
                lambda cancel: login_via_browser(cancel_event=cancel))
        except BlockingOperationCancelled:
            new_session = None
            console.print("[dim]Login cancelled.[/dim]")
    else:
        new_session = None
        console.print("[dim]Login cancelled.[/dim]")
    if new_session:
        session.clear()
        session.update(new_session)
        # Refresh the Helpwo link only if this terminal was already
        # /connect-ed — logging in never auto-links (two-end handshake).
        if agent_registry.agent_id:
            agent_registry.register(session, quiet=True)
        console.print(f"[green]Logged in as {new_session.get('userEmail') or new_session.get('userName') or new_session['userId']}[/green]")


def _cmd_model(parts: list, raw_args: str, session: dict) -> None:
    """Manage deployment-model overrides without changing agent base models."""
    args = [_normalize_slash_arg(item) for item in parts[1:]]
    current_agent = get_current_agent()
    current_terminal = agent_deployment_terminal(current_agent) or "term0"
    target_terminal = current_terminal
    if args and get_terminal(args[0]) is not None:
        target_terminal = args.pop(0)

    terminal = get_terminal(target_terminal)
    if terminal is None:
        console.print(f"[red]Terminal '{target_terminal}' not found.[/red]")
        return
    if not terminal.stationed_agent_id:
        console.print(
            f"[red]Terminal '{target_terminal}' has no deployed agent; /model "
            "only changes a deployed terminal model.[/red]")
        return

    # Seed term0 from the durable legacy preference once. Named terminals live
    # only for the process lifetime and keep their override on TerminalInfo.
    if target_terminal == "term0" and terminal.model_override is None:
        legacy_model = get_selected_model()
        if legacy_model:
            set_terminal_model_selection(
                "term0", legacy_model, get_selected_provider())

    def _apply(model: str, provider: str = "") -> None:
        set_terminal_model_selection(target_terminal, model, provider)
        if target_terminal == "term0":
            set_model_selection(model, provider)
        if target_terminal == current_terminal:
            _update_status_cache(model=model)

    if args and args[0].lower() in ("reset", "clear", "default"):
        if len(args) != 1:
            console.print("[yellow]Usage: /model [terminal] reset[/yellow]")
            return
        _apply("")
        console.print(
            f"[green]Model override reset for [bold]{target_terminal}[/bold]. "
            "Gateway auto-routing will be used (model shown as 'auto-routing').[/green]")
    elif args:
        if len(args) != 1:
            console.print("[yellow]Usage: /model [terminal] <model-id>[/yellow]")
            return
        model = args[0]
        _apply(model)
        console.print(
            f"[green]Model for [bold]{target_terminal}[/bold] set to: "
            f"[bold]{model}[/bold][/green]")
    else:
        current = str(terminal.model_override or "")
        current_provider = str(terminal.provider_override or "")
        # _safe_status, not console.status(): the shared console writes through
        # repl_mirror.TeeFile, and console.status's default redirect_stdout=True
        # makes TeeFile feed the Live's own output back into itself — a loop that
        # deadlocks Live.__exit__, so the fetch looks frozen and Esc/Ctrl+C never
        # register. _safe_status runs the spinner with redirect_stdout=False.
        try:
            with _safe_status(
                    f"[dim]Fetching available models… {symbols.BULLET} Esc/Ctrl+C cancel[/dim]"):
                models, endpoint = run_cancellable_blocking(
                    lambda cancel: fetch_available_models(
                        session, cancel_event=cancel))
        except BlockingOperationCancelled:
            console.print("[dim]Model selection cancelled.[/dim]")
            return
        except Exception as e:
            console.print(f"[red]Failed to fetch models: {e}[/red]")
            console.print(f"Current model: [bold]{current or 'backend default'}[/bold]")
            console.print("Usage: /model <model-id>  or  /model reset")
        else:
            if models and sys.stdin.isatty():
                selected = show_model_selector(models, current)
                if selected:
                    model_id = selected.get("id", "") if isinstance(selected, dict) else selected
                    provider_id = selected.get("provider", "") if isinstance(selected, dict) else ""
                    _apply(model_id, provider_id)
                    info = f"[bold]{model_id}[/bold]"
                    if provider_id:
                        info += f" ([dim]{provider_id}[/dim])"
                    console.print(
                        f"[green]Model for [bold]{target_terminal}[/bold] "
                        f"set to: {info}[/green]")
                else:
                    console.print("[dim]Model selection cancelled.[/dim]")
            else:
                table = Table(title=f"Available Models ({endpoint})")
                table.add_column("#", style="dim")
                table.add_column("Current", style="green")
                table.add_column("Model ID", style="cyan")
                table.add_column("Name")
                table.add_column("Provider")
                # Prepend the auto-routing virtual entry at the top of the table.
                auto_marker = "*" if current in ("auto", "") else ""
                table.add_row(
                    "1",
                    auto_marker,
                    "auto-routing",
                    "Auto-routing (embedding-based)",
                    "",
                )
                for idx, m in enumerate(models, start=2):
                    marker = "*" if current and m["id"] == current else ""
                    table.add_row(
                        str(idx),
                        marker,
                        m["id"],
                        m.get("name", ""),
                        m.get("provider", ""),
                    )
                if not models:
                    table.add_row("", "", "(none)", "", "")
                console.print(table)
                console.print(f"Terminal {target_terminal} override: [bold]{current or '(none)'}[/bold]" +
                              (f" ([dim]{current_provider}[/dim])" if current_provider else ""))
                if models and not sys.stdin.isatty():
                    console.print(
                        "[dim]Non-interactive terminal: select explicitly with "
                        "/model <model-id>.[/dim]")
            console.print(
                "Set with [bold]/model [terminal] <model-id>[/bold], reset with "
                "[bold]/model [terminal] reset[/bold]. This never changes the "
                "employee base model.")


def _cmd_name(raw_args: str, session: dict, agent_registry: AgentRegistry) -> None:
    if raw_args:
        name = _decode_text_arg(raw_args)
        config = load_config()
        config["agentName"] = name
        save_config(config)
        console.print(f"[green]Agent name set to: {name}[/green]")
        # Re-register under the new name only if already /connect-ed —
        # renaming never auto-links (two-end handshake).
        if agent_registry.agent_id:
            agent_registry.unregister()
            agent_registry.agent_id = None
            agent_registry.agent_secret = ""
            agent_registry.register(session, name=name, quiet=True)
            agent_registry.start_heartbeat()
            agent_registry.start_message_poll(
                agent_registry._state_cb or (lambda: {}),
                agent_registry._chat_cb or (lambda: []),
            )
    else:
        config = load_config()
        current = config.get("agentName", socket.gethostname())
        console.print(f"Current agent name: [bold]{current}[/bold]")
        console.print("Usage: /name <new-name>")
        console.print("       /term rename <old> <new>  (rename a terminal)")


def _memory_manager() -> None:
    """Interactive persistent-memory manager: browse the memories visible in the
    current scope (grouped global first then local, then by category), view a full
    entry (Enter), and delete one in place (x). Reuses the same single-loop
    select_dialog pattern as the resume-session picker (action_keys + enter_action,
    inline delete with a status line); no-ops gracefully on non-interactive stdin."""
    import memory_system
    entries = memory_system.list_memories()
    if not entries:
        console.print("[dim]No persistent memories in this scope.[/dim]")
        return
    entries.sort(key=lambda e: (
        0 if e.get("scope") == "user" else 1,
        memory_system.CATEGORY_ORDER.get(e.get("type"), 9),
        -float(e.get("importance", 0.5) or 0.5),
    ))

    def _labels():
        rows = []
        for e in entries:
            scope_txt = memory_system.scope_label(e.get("scope"))
            cat = memory_system.CATEGORY_LABELS.get(e.get("type"), e.get("type"))
            rows.append((
                f"[dim]{scope_txt}[/dim]  [cyan]{cat}[/cyan]  [bold]{e.get('name')}[/bold]",
                (e.get("description") or "").strip(),
            ))
        return rows

    sel_idx = 0
    status_msg = ""
    while entries:
        hint = f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ view  x delete  q cancel"
        if status_msg:
            hint = f"{status_msg}\n{hint}"
        result = select_dialog(
            _labels(),
            title="Memory Manager",
            full_screen=True,
            selected_index=sel_idx,
            search=True,
            action_keys={"x": "delete"},
            enter_action="view",
            hint=hint,
        )
        if result is None:
            return
        action, idx = result
        if action is None or idx < 0 or idx >= len(entries):
            return
        entry = entries[idx]
        name = entry.get("name", "")
        status_msg = ""
        if action == "view":
            data = memory_system.read_memory(name)
            with _alt_screen():
                if data is None:
                    console.print(f"[red]Memory '{name}' is no longer readable.[/red]")
                else:
                    scope_txt = memory_system.scope_label(entry.get("scope"))
                    cat = memory_system.CATEGORY_LABELS.get(entry.get("type"), entry.get("type"))
                    summary = (data.get("meta", {}).get("description") or "").strip()
                    body = data.get("body", "")
                    panel_body = (f"[bold]Summary:[/bold] {summary}\n\n[dim]Full memory:[/dim]\n{body}"
                                  if summary else body)
                    _print_long_panel(panel_body, f"[{scope_txt}] {cat} {symbols.BULLET} {name}")
                input("\n[dim]Press Enter to continue...[/dim]")
            sel_idx = idx
        elif action == "delete":
            ok, msg = memory_system.delete_memory(name)
            if ok:
                del entries[idx]
                status_msg = f"[green]Deleted memory {name}.[/green]"
                sel_idx = min(idx, len(entries) - 1) if entries else 0
            else:
                status_msg = f"[red]Delete failed: {msg}[/red]"
                sel_idx = idx


def _cmd_memory(parts: list) -> None:
    import memory_system
    sub = parts[1].lower() if len(parts) > 1 else ""
    project_entries, project_errors, _ = _load_project_memory_entries()
    persistent_entries = memory_system.list_memories()

    def _persistent_table(rows: list, title: str) -> None:
        if not rows:
            console.print("[dim]No persistent memories in this scope.[/dim]")
            return
        table = Table(title=title, show_lines=False)
        table.add_column("Name", style="cyan")
        table.add_column("Category")
        table.add_column("Scope", style="dim")
        table.add_column("Description")
        for entry in rows:
            table.add_row(
                entry.get("name", "?"),
                memory_system.CATEGORY_LABELS.get(
                    entry.get("type"), entry.get("type", "unknown")),
                memory_system.scope_label(entry.get("scope")),
                entry.get("description", ""),
            )
        console.print(table)
        console.print("[dim]Run /memory show <name> for full text, or /memory to open the manager.[/dim]")

    if sub == "":
        # Interactive manager on a real terminal; scriptable summary otherwise.
        if sys.stdin.isatty() and console.is_terminal:
            _memory_manager()
        else:
            console.print(
                f"[dim]Persistent memory: {len(persistent_entries)} visible. "
                "List with /memory global|local|persistent, or /memory show <name>.[/dim]")
            if project_entries:
                console.print(
                    f"[dim]Project memory.json: {len(project_entries)} entries. See /memory project.[/dim]")
        return

    if sub == "project":
        if project_errors:
            console.print(Panel(
                "\n".join(f"[red]- {error}[/red]" for error in project_errors)
                + f"\n\n[dim]Fix {paths.project_file(paths.CWD_MEMORY)} and retry /memory project.[/dim]",
                title="Project Memory: invalid", border_style="red",
            ))
        elif project_entries:
            lines = [
                f"[bold]{entry.get('id')}.[/bold] {entry.get('content', '')}"
                for entry in project_entries
            ]
            console.print(Panel(
                "\n".join(lines),
                title=f"Project memory ({len(project_entries)} entries)",
                border_style="cyan",
            ))
        else:
            console.print("[dim]Project memory is empty.[/dim]")
        return

    if sub == "global":
        _persistent_table(
            [e for e in persistent_entries if e.get("scope") == "user"],
            "Global Memory")
        return

    if sub == "local":
        _persistent_table(
            [e for e in persistent_entries if e.get("scope") == "project"],
            "Local Memory (current project)")
        return

    if sub == "persistent":
        _persistent_table(persistent_entries, "Persistent Memory (All)")
        return

    if sub == "show":
        selector = parts[2] if len(parts) >= 3 else ""
        selected_kind = ""
        if not selector and sys.stdin.isatty():
            choices = [
                {"kind": "project", **entry}
                for entry in project_entries
            ] + [
                {"kind": "persistent", **entry}
                for entry in persistent_entries
            ]
            chosen = choose_record(
                choices,
                title="View Memory",
                label=lambda item: (
                    f"project #{item.get('id')}" if item["kind"] == "project"
                    else f"persistent {symbols.BULLET} {item.get('name', '?')}"),
                description=lambda item: (
                    str(item.get("content") or "")[:100]
                    if item["kind"] == "project"
                    else item.get("description", "")),
                search=True,
            )
            if chosen:
                selected_kind = chosen["kind"]
                selector = (str(chosen.get("id"))
                            if selected_kind == "project"
                            else chosen.get("name", ""))
        if not selector:
            console.print("[dim]Memory selection cancelled.[/dim]")
            return
        project = next(
            (entry for entry in project_entries
             if str(entry.get("id")) == selector), None)
        if selected_kind == "persistent":
            project = None
        if project is not None:
            _print_long_panel(
                project.get("content", ""), f"Project memory #{selector}")
        else:
            persistent = memory_system.read_memory(selector)
            if persistent is None:
                console.print(f"[red]Memory '{selector}' not found.[/red]")
                console.print("[dim]Use /memory project or /memory persistent to list valid entries.[/dim]")
            else:
                _print_long_panel(
                    persistent.get("body", ""),
                    f"Persistent memory: {selector}",
                )
    else:
        console.print("[yellow]Usage: /memory [global|local|persistent|project|show <id|name>][/yellow]")


# Last `/mail inbox` listing, so `/mail read <n>` can resolve a position to
# an email_id without a second round trip. Session-lifetime only — reset by
# the next `/mail inbox` call, not persisted.
_last_mail_inbox: list[dict] = []


def _fmt_mail_time(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def _cmd_mail(parts: list, raw_args: str, session: dict) -> None:
    global _last_mail_inbox

    if not session.get("userId"):
        console.print("[yellow]Not logged in.[/yellow] Run [bold]/login[/bold] to use mail — "
                      "it only works for a verified Laintas account.")
        return

    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub in ("", "help"):
        console.print("Usage: [bold]/mail[/bold] [inbox [--all]|read <n>|send [subject]]")
        console.print("[dim]inbox: list mail sent to your account's AI agent address. "
                      "send: email yourself (needs your approval to actually send).[/dim]")

    elif sub == "inbox":
        show_all = "--all" in parts[2:]
        try:
            with _safe_status(
                    f"[dim]Checking inbox… {symbols.BULLET} Esc/Ctrl+C cancel[/dim]"):
                messages, error = run_cancellable_blocking(
                    lambda _cancel: fetch_mail_inbox(
                        session, unread_only=not show_all))
        except BlockingOperationCancelled:
            console.print("[dim]Inbox request cancelled.[/dim]")
            return
        if error:
            console.print(f"[red]Could not reach backend: {error}[/red]")
            return
        _last_mail_inbox = messages
        if not messages:
            console.print("[dim]No mail." + (
                "" if show_all else " (use /mail inbox --all to include read messages)") + "[/dim]")
            return
        table = Table(title=f"Mail Inbox ({len(messages)})")
        table.add_column("#", style="dim")
        table.add_column("", width=1)  # unread marker
        table.add_column("From", style="cyan")
        table.add_column("Subject")
        table.add_column("Received", style="dim")
        for idx, m in enumerate(messages, start=1):
            table.add_row(
                str(idx),
                "" if m.get("read") else "[green]*[/green]",
                m.get("from", "?"),
                m.get("subject", "(no subject)"),
                _fmt_mail_time(m.get("received_at")),
            )
        console.print(table)
        console.print("[dim]Use /mail read <n> to view one in full (marks it read).[/dim]")

    elif sub == "read":
        if len(parts) < 3 or not parts[2].isdigit():
            console.print("[yellow]Usage: /mail read <n>[/yellow] (n from the last /mail inbox)")
            return
        n = int(parts[2])
        if not _last_mail_inbox:
            console.print("[dim]Run /mail inbox first.[/dim]")
            return
        if n < 1 or n > len(_last_mail_inbox):
            console.print(f"[red]No message #{n} in the last /mail inbox listing.[/red]")
            return
        message = _last_mail_inbox[n - 1]
        _print_long_panel(
            message.get("body", ""),
            f"From {message.get('from', f'?')} {symbols.BULLET} {message.get('subject', '(no subject)')}",
        )
        email_id = message.get("email_id")
        if email_id and not message.get("read"):
            ack_mail_read(session, [email_id])
            message["read"] = True

    elif sub == "send":
        _, subject_arg = _raw_tail_after_word(raw_args)
        subject = _decode_text_arg(subject_arg) if subject_arg else ""
        if not subject:
            try:
                subject = input("Subject: ").strip()
            except (EOFError, KeyboardInterrupt):
                subject = ""
        if not subject:
            console.print("[dim]Mail cancelled.[/dim]")
            return
        try:
            body = input("Body: ").strip()
        except (EOFError, KeyboardInterrupt):
            body = ""
        if not body:
            console.print("[dim]Mail cancelled.[/dim]")
            return
        with _safe_status("[dim]Sending…[/dim]"):
            ok, error = send_mail(session, subject, body)
        if ok:
            console.print("[green]Sent to your verified account address.[/green]")
        else:
            console.print(f"[red]Send failed: {error}[/red]")

    else:
        console.print("[yellow]Usage: /mail [inbox [--all]|read <n>|send [subject]][/yellow]")


def _cmd_prop() -> None:
    prop = read_file(str(paths.project_file(paths.CWD_CLI_PROP)))
    if prop:
        console.print(Panel(prop[:2000], title=".laintas/cli.prop Prompt Template"))
    else:
        console.print("[dim]No .laintas/cli.prop found.[/dim]")


def _cmd_scan() -> None:
    user_cmds = list_path_commands()
    console.print(f"[bold]{len(user_cmds)} user-facing commands on PATH:[/bold]\n")
    groups: dict = {}
    for c in user_cmds:
        groups.setdefault(c[0], []).append(c)
    for letter in sorted(groups):
        console.print(f"  [cyan]{letter}[/cyan]: {', '.join(groups[letter][:40])}")
        if len(groups[letter]) > 40:
            console.print(f"       [dim](+{len(groups[letter]) - 40} more)[/dim]")


def _cmd_cwd() -> None:
    console.print(f"Working directory: [bold]{os.getcwd()}[/bold]")


def _cmd_bash(parts: list, raw_args: str) -> bool:
    bash_sub = parts[1].lower() if len(parts) > 1 else ""
    if len(parts) < 2:
        wl = ", ".join(sorted(get_interactive_commands()))
        console.print(Panel(
            "[bold]/bash <command>[/bold]        run <command> directly via term0's real bash "
            "(marker-poll + cwd sync), bypassing the interactive whitelist below\n"
            "[bold]/bash list[/bold]              show the interactive-terminal whitelist\n"
            "[bold]/bash add <command>[/bold]     force <command> through full PTY passthrough "
            "(vim-style — use for full-screen/raw-keystroke programs)\n"
            "[bold]/bash remove <command>[/bold]  let <command> use term0/marker-poll instead\n\n"
            f"[dim]Current whitelist: {wl}[/dim]",
            title="/bash", border_style="cyan",
        ))
    elif bash_sub == "list":
        wl = sorted(get_interactive_commands())
        console.print(Panel("\n".join(wl) or "(empty)",
                            title="Interactive-terminal whitelist", border_style="cyan"))
    elif bash_sub == "add":
        if len(parts) < 3:
            console.print("[yellow]Usage: /bash add <command>[/yellow]")
        elif not re.fullmatch(r"[A-Za-z0-9._+/-]+", parts[2]):
            console.print("[red]Command must be one executable token without shell operators.[/red]")
        else:
            _modify_interactive_commands(parts[2], add=True)
            console.print(f"[green]'{parts[2]}' now uses full PTY passthrough (native terminal).[/green]")
    elif bash_sub == "remove":
        if len(parts) < 3:
            console.print("[yellow]Usage: /bash remove <command>[/yellow]")
        elif not re.fullmatch(r"[A-Za-z0-9._+/-]+", parts[2]):
            console.print("[red]Command must be one executable token without shell operators.[/red]")
        else:
            _modify_interactive_commands(parts[2], add=False)
            console.print(f"[green]'{parts[2]}' now routes through term0/marker-poll.[/green]")
    else:
        raw_cmd = raw_args
        allowed, denial = authorize_direct_command(raw_cmd, os.getcwd())
        if not allowed:
            console.print(f"[red]{denial}[/red]")
            return False
        _ensure_term0_alive()
        _t0 = get_terminal("term0")
        if _t0 is None or _t0.session is None or not _t0.session.is_alive():
            console.print("[red]term0 session unavailable.[/red]")
        else:
            result = _marker_poll_exec(_t0.session, raw_cmd, strip_ansi_codes=False)
            _sync_cwd_from_term0(_t0.session)
            stdout = result.get("stdout", "")
            if stdout:
                console.print(stdout)
            returncode = result.get("returncode")
            rc_text = f" {symbols.BULLET} exit {returncode}" if returncode is not None else ""
            console.print(f"[dim]cwd → {os.getcwd()}{rc_text}[/dim]")
    return False


def _print_mode_activation_note(name: str) -> None:
    """Print the posture note for a mode that was just activated.

    Shared by the `/mode <name>` and the interactive selector paths so the two
    never drift. Only one note is shown — timed confirmation, auto-approve, and
    read-only are mutually exclusive postures.
    """
    timeout = mode_manager.get_auto_confirm_timeout()
    if timeout is not None:
        console.print(
            f"[yellow]↳ AUTO confirmation windows: {int(timeout)}s ordinary, "
            f"{int(mode_manager.get_auto_confirm_timeout(destructive=True) or 0)}s "
            f"deletion. Choose No before the timer expires to stop an action."
            f"[/yellow]")
        return
    auto_approve = mode_manager.get_auto_approve()
    if auto_approve != "none":
        console.print(
            f"[dim]↳ {name.upper()}* — auto-approving {auto_approve} "
            f"this session.[/dim]")
        return
    if mode_manager.is_read_only_mode():
        detail = ("You write the code and run the commands; the agent teaches "
                  "and checks your work. "
                  if name == "study" else
                  "No file writes or commands. ")
        console.print(f"[dim]↳ {detail}Use /mode act to switch back.[/dim]")


def _cmd_mode(raw_args: str, parts: list) -> bool:
    import plan_mode as _pm_mode
    from rich.markup import escape as _escape
    sub = parts[1].lower() if len(parts) > 1 else ""
    _, mode_args_raw = _raw_tail_after_word(raw_args)

    # Gather current state
    _in_plan = _pm_mode.is_plan_mode()
    _cur_plan = _pm_mode.get_current_plan()

    if sub == "plan":
        task = _decode_text_arg(mode_args_raw)
        if not task:
            if _in_plan:
                console.print("[dim]Already in PLAN mode.[/dim]")
                return False
            mode_manager.activate("act")
            _pm_mode.arm_plan_mode()
            console.print(Panel(
                "[bold]PLAN mode[/bold]\n\n"
                "Describe the task in your next message. The agent will plan "
                "without modifying files or the system.",
                title="Mode changed", border_style="green",
            ))
            return False
        if _in_plan:
            console.print(
                "[yellow]A plan is already active. Use /plan status, "
                "/plan approve, or /plan exit before starting another.[/yellow]")
            return False
        mode_manager.activate("act")
        plan = _pm_mode.enter_plan_mode(task)
        _enqueue_user_input(task)
        console.print(Panel(
            f"[bold]Plan Mode: [green]ENTERED[/green][/bold]\n\n"
            f"Task: {task}\n"
            f"Plan file: {plan['file']}\n\n"
            f"[dim]The AI will explore and design — no code will be executed.[/dim]\n"
            f"[dim]When ready, the review menu will offer execute, revise, or exit.[/dim]",
            title="Plan Mode", border_style="green",
        ))

    elif sub in ("act", "always", "act-always"):
        # `/mode act always` (or `/mode always`) → ACT with every file write
        # and command auto-approved for this session (shown as ACT*). Plain
        # `/mode act` restores confirmations by clearing that session state,
        # giving a mid-session way to turn auto-approve back off.
        _always = (sub in ("always", "act-always")
                   or any(p.lower() == "always" for p in parts[2:]))
        if _in_plan:
            _pm_mode.exit_plan_mode(approve=False)
        ok, msg = mode_manager.activate("act")
        if _always:
            _session_approval_state["all_writes"] = True
            _session_approval_state["all_commands"] = True
            console.print(
                "[green]ACT [bold]always-approve[/bold] — file writes and "
                "commands are auto-approved this session ([bold]ACT*[/bold]).[/green]\n"
                "[dim]Run /mode act to turn confirmations back on.[/dim]")
        else:
            _session_approval_state["all_writes"] = False
            _session_approval_state["all_commands"] = False
            console.print(
                f"[{'green' if ok else 'red'}]{_escape(msg)}"
                f"[/{'green' if ok else 'red'}]"
                + (" [dim](draft plan saved)[/dim]" if _cur_plan else ""))

    elif sub == "approve":
        # Backward compatibility. Approval is a plan action, not a mode.
        console.print("[yellow]/mode approve is deprecated; use /plan approve.[/yellow]")
        if not _in_plan or not _cur_plan:
            console.print("[yellow]No active plan to approve.[/yellow]")
        else:
            approved = _review_and_approve_current_plan()
            queued = False
            if approved:
                followup = (
                    f"Execute approved WorkGraph {approved['work_id']} revision "
                    f"{approved['revision']} SHA {approved['content_sha']} for task: "
                    f"{approved['task']}. Follow the injected approved_work_plan exactly."
                )
                queued = _enqueue_user_input(followup)
            console.print(Panel(
                f"[bold]Plan [{'green' if approved else 'yellow'}]"
                f"{'APPROVED' if approved else 'NOT APPROVED'}"
                f"[/{'green' if approved else 'yellow'}][/bold]\n\n"
                f"File: {_cur_plan['file']}\n\n"
                + ("[dim]Execution request queued.[/dim]" if queued else
                   "[dim]No execution was queued.[/dim]"),
                title="Plan Approved", border_style="green",
            ))

    elif sub == "list":
        active_name = "plan" if _in_plan else mode_manager.get_active_mode()["name"]
        console.print("[bold]Agent modes[/bold]")
        console.print(
            f"{'[green]*[/green]' if active_name == 'plan' else ' '} "
            "[cyan]plan[/cyan] [dim]Reviewed, read-only planning[/dim]")
        for item in mode_manager.list_modes():
            marker = "[green]*[/green]" if item["name"] == active_name else " "
            source = "built-in" if item["builtin"] else "custom"
            _bits = [item["description"], source]
            _allow = item.get("allowed_tools")
            if _allow is not None:
                _bits.append(f"tools: {', '.join(_allow[:4])}"
                             + ("…" if len(_allow) > 4 else ""))
            if item.get("denied_tools"):
                _bits.append(f"deny: {', '.join(item['denied_tools'][:3])}")
            if item.get("auto_approve", "none") != "none":
                _bits.append(f"auto-approve: {item['auto_approve']}")
            _confirm_timeout = mode_manager.get_auto_confirm_timeout(mode=item)
            _delete_timeout = mode_manager.get_auto_confirm_timeout(
                mode=item, destructive=True)
            if _confirm_timeout is not None:
                _bits.append(
                    f"timed confirm: {int(_confirm_timeout)}s"
                    + (f" / delete {int(_delete_timeout)}s"
                       if _delete_timeout is not None else "")
                )
            console.print(
                f"{marker} [cyan]{item['name']}[/cyan] "
                f"[dim]{_escape(f' {symbols.BULLET} '.join(_bits))}[/dim]")

    elif sub == "create":
        if len(parts) < 4:
            console.print(
                "[yellow]Usage: /mode create <name> [--tools \"fs.*,web.search\"] "
                "[--deny \"shell.*\"] [--read-only] "
                "[--auto-approve none|writes|commands|all] <instructions>[/yellow]")
        else:
            name = parts[2]
            _cargs = parts[3:]
            _allowed = _denied = None
            _auto = "none"
            _read_only = False
            _rest = []
            _i = 0
            while _i < len(_cargs):
                _a = _cargs[_i]
                if _a == "--read-only":
                    _read_only = True; _i += 1
                elif _a == "--tools" and _i + 1 < len(_cargs):
                    _allowed = [t.strip() for t in _cargs[_i + 1].split(",") if t.strip()]; _i += 2
                elif _a == "--deny" and _i + 1 < len(_cargs):
                    _denied = [t.strip() for t in _cargs[_i + 1].split(",") if t.strip()]; _i += 2
                elif _a == "--auto-approve" and _i + 1 < len(_cargs):
                    _auto = _cargs[_i + 1].strip().lower(); _i += 2
                else:
                    _rest.append(_a); _i += 1
            instructions = " ".join(_rest)
            ok, msg = mode_manager.create_mode(
                name, instructions, read_only=_read_only,
                allowed_tools=_allowed, denied_tools=_denied,
                auto_approve=_auto)
            console.print(
                f"[{'green' if ok else 'red'}]{_escape(msg)}"
                f"[/{'green' if ok else 'red'}]")
            if ok and _auto != "none":
                console.print(
                    f"[yellow]⚠ Mode '{name}' auto-approves {_auto} — "
                    f"file writes/commands run without confirmation while it's "
                    f"active (shown as {name.upper()}*). Hard deny rules still apply.[/yellow]")

    elif sub == "delete":
        mode_name = parts[2] if len(parts) == 3 else ""
        if not mode_name and sys.stdin.isatty():
            custom_modes = [
                item for item in mode_manager.list_modes()
                if not item.get("builtin")
            ]
            chosen = choose_record(
                custom_modes,
                title="Delete Custom Mode",
                label=lambda item: item["name"],
                description=lambda item: item.get("description", ""),
                full_screen=False,
            )
            mode_name = chosen["name"] if chosen else ""
        if not mode_name:
            console.print("[dim]Mode selection cancelled.[/dim]")
        else:
            ok, msg = mode_manager.delete_mode(mode_name)
            console.print(
                f"[{'green' if ok else 'red'}]{_escape(msg)}"
                f"[/{'green' if ok else 'red'}]")

    elif sub == "status":
        active = mode_manager.get_active_mode()
        _mode_name = "PLAN" if _in_plan else active["name"].upper()
        _mode_desc = (
            "Waiting for a task" if _pm_mode.is_pending_task() else
            (_cur_plan.get("task", "") if _cur_plan else active["description"])
        )
        console.print(Panel(
            f"Mode: [accent]{_mode_name}[/accent]\n"
            f"[dim]{_escape(_mode_desc[:120])}[/dim]",
            title="Current Mode", border_style="cyan",
        ))

    elif sub:
        if mode_manager.get_mode(sub) is None:
            ok, msg = False, f"Unknown mode: {sub}"
        else:
            if _in_plan:
                _pm_mode.exit_plan_mode(approve=False)
            ok, msg = mode_manager.activate(sub)
            if ok:
                _sync_session_approval_from_mode()
        console.print(
            f"[{'green' if ok else 'red'}]{_escape(msg)}"
            f"[/{'green' if ok else 'red'}]")
        if ok:
            _print_mode_activation_note(sub)

    else:
        active = mode_manager.get_active_mode()
        options = [{
            "name": "plan",
            "description": "Reviewed, read-only planning",
        }, *mode_manager.list_modes()]
        # Offer the always-approve variant right after act.
        _act_i = next((i for i, o in enumerate(options)
                       if o["name"] == "act"), None)
        if _act_i is not None:
            options.insert(_act_i + 1, {
                "name": "act-always",
                "description": "ACT with file writes & commands auto-approved (ACT*)",
            })
        _auto = _session_approval_state.get("all_writes")
        active_name = ("plan" if _in_plan
                       else ("act-always" if _auto and active["name"] == "act"
                             else active["name"]))
        selected_index = next(
            (i for i, item in enumerate(options)
             if item["name"] == active_name), 0)
        chosen = choose_record(
            options,
            title="Select Agent Mode",
            label=lambda item: (
                f"{'{symbols.DOT}' if item['name'] == active_name else f'{symbols.DOT_OPEN}'} "
                f"{item['name']}"),
            description=lambda item: item.get("description", ""),
            selected_index=selected_index,
            full_screen=False,
        )
        if chosen:
            target = chosen["name"]
            if target == "plan":
                if not _in_plan:
                    mode_manager.activate("act")
                    _pm_mode.arm_plan_mode()
                    console.print(
                        "[green]PLAN mode armed.[/green] "
                        "[dim]Describe the task in your next message.[/dim]")
            elif target == "act-always":
                if _in_plan:
                    _pm_mode.exit_plan_mode(approve=False)
                mode_manager.activate("act")
                _session_approval_state["all_writes"] = True
                _session_approval_state["all_commands"] = True
                console.print(
                    "[green]ACT [bold]always-approve[/bold] — writes & commands "
                    "auto-approved this session ([bold]ACT*[/bold]).[/green]\n"
                    "[dim]Pick ACT to turn confirmations back on.[/dim]")
            else:
                if _in_plan:
                    _pm_mode.exit_plan_mode(approve=False)
                ok, msg = mode_manager.activate(target)
                # Sync session auto-approve to the mode's posture (a plain
                # mode with auto_approve=none clears any prior auto-approve).
                if ok:
                    _sync_session_approval_from_mode()
                console.print(
                    f"[{'green' if ok else 'red'}]{_escape(msg)}"
                    f"[/{'green' if ok else 'red'}]")
                if ok:
                    _print_mode_activation_note(target)
        elif not sys.stdin.isatty():
            console.print(
                f"[dim]Current mode: {'plan' if _in_plan else active['name']}[/dim]")
    return False


def _cmd_trust(parts: list) -> None:
    global _extra_cmd_handler_cache, _extra_cmd_mtime_cache
    sub = parts[1].lower() if len(parts) > 1 else "status"
    if sub == "status":
        status = trust_store.project_status()
        style = "green" if status.get("trusted") else "yellow"
        hashes = status.get("hashes") or {}
        details = "\n".join(
            f"  {name}: {digest[:16]}…" for name, digest in sorted(hashes.items())
        ) or "  (no executable project customization)"
        console.print(Panel(
            f"Project: {status.get('realpath', os.getcwd())}\n"
            f"Trusted: {status.get('trusted', False)}\n"
            f"Reason: {status.get('reason', '')}\n"
            f"Executable hashes:\n{details}",
            title="Workspace Trust", border_style=style,
        ))
    elif sub == "allow":
        status = trust_store.project_status()
        hashes = status.get("hashes") or {}
        preview = "\n".join(
            f"{name}: {digest}" for name, digest in sorted(hashes.items())
        ) or "No executable files are currently present."
        approved = "--yes" in parts[2:]
        if not approved and sys.stdin.isatty():
            approved = _blocking_approval_prompt(
                "Trust workspace executable customization",
                f"Project: {Path.cwd().resolve()}\n\n{preview}\n\n"
                "Trusted Python runs with your full local account permissions.",
                "Trust these exact file hashes?",
                allow_always=False,
            ) == "yes"
        if not approved:
            console.print(
                "[yellow]Workspace not trusted. In non-interactive mode use "
                "/trust allow --yes after reviewing the files.[/yellow]")
        else:
            trusted = trust_store.trust_project()
            clear_loop_command_cache()
            _extra_cmd_handler_cache = None
            _extra_cmd_mtime_cache = 0
            console.print(
                f"[green]Trusted executable customization for "
                f"{trusted['realpath']} at the current hashes.[/green]")
    elif sub == "revoke":
        removed = trust_store.revoke_project()
        clear_loop_command_cache()
        _extra_cmd_handler_cache = None
        _extra_cmd_mtime_cache = 0
        console.print(
            "[green]Workspace trust revoked.[/green]" if removed
            else "[dim]Workspace was not explicitly trusted.[/dim]")
    else:
        console.print("[yellow]Usage: /trust [status|allow|revoke][/yellow]")


def _cmd_backend(parts: list) -> None:
    sub = parts[1].lower() if len(parts) > 1 else "status"
    if sub == "status":
        profile = get_backend_profile()
        console.print(Panel(
            f"Profile: {profile.name}\nKind: {profile.kind}\n"
            f"URL: {profile.base_url}\nBilling: {profile.billing_label}\n"
            f"Sends Laintas credentials: {profile.sends_laintas_credentials}",
            title="Backend", border_style=(
                "green" if profile.sends_laintas_credentials else "yellow"),
        ))
    elif sub == "list":
        active = get_backend_profile().name
        for profile in backend_profiles.list_profiles():
            marker = "*" if profile.name == active else " "
            console.print(
                f"{marker} [bold]{profile.name}[/bold] [{profile.kind}] "
                f"{profile.base_url} [dim]{profile.billing_label}[/dim]")
    elif sub == "use":
        profile_name = parts[2] if len(parts) >= 3 else ""
        if not profile_name and sys.stdin.isatty():
            profiles = backend_profiles.list_profiles()
            active_name = get_backend_profile().name
            selected_index = next(
                (i for i, item in enumerate(profiles)
                 if item.name == active_name), 0)
            chosen = choose_record(
                profiles,
                title="Select Backend",
                label=lambda item: (
                    f"{'{symbols.DOT}' if item.name == active_name else f'{symbols.DOT_OPEN}'} {item.name}"),
                description=lambda item: (
                    f"{item.kind} {symbols.BULLET} {item.base_url} {symbols.BULLET} {item.billing_label}"),
                selected_index=selected_index,
                search=True,
            )
            profile_name = chosen.name if chosen else ""
        if not profile_name:
            console.print("[dim]Backend selection cancelled.[/dim]")
        elif os.environ.get("LAINTAS_BACKEND"):
            console.print(
                "[red]LAINTAS_BACKEND currently overrides profiles; unset it first.[/red]")
        else:
            ok, msg = backend_profiles.set_active(profile_name)
            console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
    elif sub == "config":
        console.print(str(backend_profiles.ensure_template()))
    else:
        console.print("[yellow]Usage: /backend [status|list|use <name>|config][/yellow]")


def _cmd_hooks(parts: list) -> None:
    sub = parts[1].lower() if len(parts) > 1 else "status"
    hook_path = paths.PYTHON_HOOKS_FILE
    if sub == "status":
        py_status = (
            trust_store.extension_status("hooks", "python", hook_path)
            if hook_path.is_file() else {"trusted": False, "reason": "no hooks.py"}
        )
        config_status = (
            trust_store.extension_status(
                "hooks", "config", paths.HOOKS_FILE)
            if paths.HOOKS_FILE.is_file()
            else {"trusted": False, "reason": "no hooks.json"}
        )
        info = hooks_mod.reload()
        console.print(Panel(
            f"Python hooks: {hook_path}\n"
            f"Trusted: {py_status.get('trusted', False)}\n"
            f"Reason: {py_status.get('reason', '')}\n"
            f"Config hooks: {paths.HOOKS_FILE}\n"
            f"Trusted: {config_status.get('trusted', False)}\n"
            f"Reason: {config_status.get('reason', '')}\n"
            f"Loaded functions: {', '.join(info.get('python_hooks', [])) or '(none)'}\n"
            f"Configured argv hooks: {info.get('shell_hooks', 0)}",
            title="Hooks", border_style="cyan",
        ))
    elif sub == "trust":
        targets = []
        if hook_path.is_file():
            targets.append(("python", hook_path))
        if paths.HOOKS_FILE.is_file():
            targets.append(("config", paths.HOOKS_FILE))
        if not targets:
            console.print("[red]No hooks.py or hooks.json exists.[/red]")
        else:
            statuses = [
                (name, path, trust_store.extension_status("hooks", name, path))
                for name, path in targets
            ]
            details = "\n".join(
                f"{name}: {path} SHA-256={status.get('sha256', 'unavailable')}"
                for name, path, status in statuses)
            approved = "--yes" in parts[2:]
            if not approved and sys.stdin.isatty():
                approved = _blocking_approval_prompt(
                    "Trust executable hooks",
                    f"{details}\n\nThese hooks execute processes or Python "
                    "with your local account permissions.",
                    "Trust these exact hook hashes?", allow_always=False,
                ) == "yes"
            if approved:
                trusted = []
                for name, path, _ in statuses:
                    trusted.append(trust_store.trust_extension(
                        "hooks", name, path))
                hooks_mod.reload()
                console.print(
                    f"[green]Trusted {len(trusted)} hook file(s) at their current hashes.[/green]")
            else:
                console.print("[yellow]Hooks not trusted.[/yellow]")
    elif sub == "revoke":
        removed = trust_store.revoke_extension("hooks", "python")
        removed = trust_store.revoke_extension("hooks", "config") or removed
        hooks_mod.reload()
        console.print("[green]Hook trust revoked.[/green]" if removed
                      else "[dim]Hooks were not trusted.[/dim]")
    elif sub == "reload":
        info = hooks_mod.reload()
        console.print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        console.print("[yellow]Usage: /hooks [status|trust|revoke|reload][/yellow]")


def _cmd_policy(parts: list) -> bool:
    import policy as _pol_cmd
    sub = parts[1].lower() if len(parts) > 1 else ""
    _valid = {"audit", "enforce", "disabled"}
    if not sub and sys.stdin.isatty():
        current_mode = _pol_cmd.get_config().get("mode", "audit")
        choices = [
            {"name": "audit", "description": "Deny rules block; approvals are advisory"},
            {"name": "enforce", "description": "Require approval for protected actions"},
            {"name": "disabled", "description": "Bypass policy checks (unsafe)"},
        ]
        selected_index = next(
            (i for i, item in enumerate(choices)
             if item["name"] == current_mode), 0)
        chosen = choose_record(
            choices,
            title="Select Security Policy",
            label=lambda item: (
                f"{'{symbols.DOT}' if item['name'] == current_mode else f'{symbols.DOT_OPEN}'} "
                f"{item['name']}"),
            description=lambda item: item["description"],
            selected_index=selected_index,
            full_screen=False,
        )
        sub = chosen["name"] if chosen else "status"
    if sub in _valid:
        if sub == "disabled" and "--yes" not in parts[2:]:
            if not sys.stdin.isatty():
                console.print(
                    "[red]Disabling policy in a non-interactive shell requires "
                    "/policy disabled --yes.[/red]")
                return False
            choice = _blocking_approval_prompt(
                "Disable security policy",
                "This bypasses policy checks and approval rules for commands.",
                "Disable policy?",
            )
            if choice != "yes":
                console.print("[dim]Policy change cancelled.[/dim]")
                return False
        ok, msg = _pol_cmd.set_mode(sub)
        style = "green" if ok else "red"
        console.print(f"[{style}]{msg}[/{style}]")
        if ok:
            if sub == "enforce":
                console.print("[dim]Commands matching approval rules will now prompt before execution.[/dim]")
            elif sub == "disabled":
                console.print("[yellow]⚠ All policy checks bypassed — commands run without approval.[/yellow]")
            elif sub == "audit":
                console.print("[dim]Policy advisory only — deny rules still block, approvals are advisory.[/dim]")
    elif sub == "reset":
        _reset_session_approvals()
        console.print("[green]Session auto-approvals cleared.[/green]")
    else:
        _cfg = _pol_cmd.get_config()
        _mode = _cfg.get("mode", "audit")
        _mode_style = {"audit": "cyan", "enforce": "yellow",
                       "disabled": "red"}.get(_mode, "cyan")
        _n_allow = len(_cfg.get("allow", []))
        _n_deny = len(_cfg.get("deny", []))
        _n_appr = len(_cfg.get("needs_approval", []))
        console.print(Panel(
            f"Mode:           [{_mode_style}]{_mode}[/{_mode_style}]\n"
            f"Allow rules:    {_n_allow}\n"
            f"Deny rules:     {_n_deny}\n"
            f"Approval rules: {_n_appr}\n"
            f"Config file:    [dim]{_pol_cmd.CONFIG_PATH}[/dim]\n\n"
            f"[dim]Session auto-approve:[/dim]  "
            f"commands={'[green]on[/green]' if _session_approval_state['all_commands'] else 'off'}  "
            f"writes={'[green]on[/green]' if _session_approval_state['all_writes'] else 'off'}\n\n"
            f"[dim]Run /policy to choose a mode, or pass audit/enforce/disabled directly.[/dim]\n"
            f"[dim]Reset session approvals:[/dim]  [bold]/policy reset[/bold]",
            title="Security Policy", border_style="cyan",
        ))
    return False


def _cmd_plan(raw_args: str, parts: list) -> None:
    import plan_mode as _pm
    sub = parts[1].lower() if len(parts) > 1 else ""
    _, plan_args_raw = _raw_tail_after_word(raw_args)
    if sub == "enter" and plan_args_raw:
        task = _decode_text_arg(plan_args_raw)
        if _pm.is_plan_mode():
            console.print(
                "[yellow]A plan is already active. Approve or exit it before "
                "starting another.[/yellow]")
            return
        mode_manager.activate("act")
        plan = _pm.enter_plan_mode(task)
        _enqueue_user_input(task)
        console.print(Panel(
            f"[bold]Plan Mode: [green]ENTERED[/green][/bold]\n\n"
            f"Task: {task}\n"
            f"Plan file: {plan['file']}\n\n"
            f"[dim]The AI will now explore and design — no code will be executed.[/dim]\n"
            f"[dim]When the plan is ready, run [bold]/plan approve[/bold].[/dim]",
            title="Plan Mode",
            border_style="green",
        ))
    elif sub == "approve":
        plan = _review_and_approve_current_plan()
        if plan:
            followup = (
                f"Execute approved WorkGraph {plan['work_id']} revision "
                f"{plan['revision']} SHA {plan['content_sha']} for task: "
                f"{plan['task']}. Follow the injected approved_work_plan exactly."
            )
            queued = _enqueue_user_input(followup)
            console.print(Panel(
                f"[bold]Plan [green]APPROVED[/green][/bold]\n\n"
                f"File: {plan['file']}\n\n"
                + ("[dim]Execution request queued.[/dim]" if queued else
                   "[yellow]Could not queue execution; submit a task referencing this plan.[/yellow]"),
                title="Plan Approved",
                border_style="green",
            ))
        else:
            console.print("[yellow]Plan was not approved.[/yellow]")
    elif sub == "submit":
        snapshot = _pm.submit_current_plan()
        if snapshot is None:
            console.print("[red]Plan is not ready to submit.[/red]")
        else:
            _review_and_approve_current_plan()
    elif sub == "revise":
        feedback = _decode_text_arg(plan_args_raw)
        plan = _pm.get_current_plan()
        if plan is None and feedback:
            plan = _pm.begin_amendment()
        if not plan or not feedback:
            console.print("[yellow]Usage: /plan revise <feedback>[/yellow]")
        else:
            snapshot = _pm.get_review_snapshot()
            if snapshot and snapshot["work"].get("status") == "REVIEW_PENDING":
                rev = snapshot["revision"]
                _pm.reject_submitted_plan(rev["revision"], rev["content_sha"])
            queued = _enqueue_user_input(
                f"Revise WorkGraph {plan.get('work_id')} plan using this user feedback: "
                f"{feedback}. Update the plan, then call plan.submit again.")
            console.print(
                "[green]Revision feedback queued for the planning AI.[/green]"
                if queued else "[red]Could not queue revision feedback.[/red]")
    elif sub == "exit":
        plan = _pm.exit_plan_mode(approve=False)
        if plan:
            console.print("[dim]Exited plan mode; the draft remains in the plans directory.[/dim]")
        else:
            console.print("[yellow]No active plan to exit.[/yellow]")
    elif sub == "status":
        plan = _pm.get_current_plan()
        if plan:
            content = _pm.read_plan() or "(empty)"
            _print_long_panel(content, f"Plan: {plan['task'][:60]}")
        else:
            console.print("[dim]Not in plan mode.[/dim]")
    elif sub == "list":
        plans = _pm.list_plans()
        if plans:
            console.print(f"[bold]Saved Plans:[/bold]")
            for p in plans:
                console.print(f"  [cyan]{p['name']}[/cyan] — {p['title'][:80]}")
        else:
            console.print("[dim]No saved plans.[/dim]")
    else:
        console.print("Usage:\n"
                      "  [bold]/plan enter <task>[/bold] — Enter plan mode\n"
                      "  [bold]/plan submit[/bold]       — Submit immutable revision for review\n"
                      "  [bold]/plan revise <feedback>[/bold] — Ask AI to revise\n"
                      "  [bold]/plan approve[/bold]      — Approve and execute\n"
                      "  [bold]/plan exit[/bold]         — Exit without approving\n"
                      "  [bold]/plan status[/bold]       — Show current plan\n"
                      "  [bold]/plan list[/bold]         — List saved plans")


def _cmd_evolve(raw_args: str, parts: list, session: dict) -> None:
    sub = parts[1].lower() if len(parts) > 1 else ""
    _, evolve_args_raw = _raw_tail_after_word(raw_args)
    commands = {
        "status", "branches", "open", "chat", "review", "test",
        "activate", "disable", "candidates", "list", "profiles",
        "profile", "use", "rollback", "help",
    }
    runtime = extension_runtime.get_runtime()
    if not sub or sub not in commands:
        idea = (_decode_text_arg(raw_args) if raw_args
                else "Create a useful project extension")
        branch = evolution_lab.create_branch(idea)
        worker = _evolution_lab_start_worker(branch["id"], session)
        if worker:
            branch = evolution_lab.read_branch(branch["id"]) or branch
        console.print(Panel(
            f"[bold]Evolution Lab branch created[/bold]\n\n"
            f"Branch: [cyan]{branch['id']}[/cyan]\n"
            f"Intent: [bold]{branch.get('intent')}[/bold]\n"
            f"Status: [bold]{branch.get('status')}[/bold]\n"
            f"Idea: {branch.get('description')}\n"
            + (f"Worker: [cyan]{worker}[/cyan]" if worker else
               "[yellow]Snapshot saved; no active parent agent was available.[/yellow]"),
            title="Evolution Lab", border_style="cyan",
        ))
    elif sub == "status":
        branch = evolution_lab.read_branch()
        profile = evolution_lab.get_active_profile()
        if branch is None:
            console.print("[dim]No active Evolution Lab branch.[/dim]")
        else:
            console.print(Panel(
                f"Branch: {branch.get('id')}\nIntent: {branch.get('intent')}\n"
                f"Status: {branch.get('status')}\n"
                f"Candidate: {branch.get('candidate_id') or '(none)'}\n"
                f"Profile: {profile.get('name', 'default')}\n"
                f"Loaded extensions: {len(runtime.list())}",
                title="Evolution Lab Status", border_style="cyan"))
    elif sub == "branches":
        for branch in evolution_lab.list_branches():
            console.print(
                f"  [cyan]{branch.get('id')}[/cyan] "
                f"[{branch.get('intent')}] [dim]{branch.get('status')}[/dim] "
                f"— {str(branch.get('description') or '')[:80]}")
    elif sub == "open":
        branch_id = parts[2] if len(parts) > 2 else ""
        if not branch_id and sys.stdin.isatty():
            chosen = choose_record(
                evolution_lab.list_branches(),
                title="Open Evolution Branch",
                label=lambda item: item.get("id", ""),
                description=lambda item: (
                    f"{item.get('status')} {symbols.BULLET} "
                    f"{str(item.get('description') or '')[:100]}"),
                search=True,
            )
            branch_id = chosen.get("id", "") if chosen else ""
        if not branch_id:
            console.print("[dim]Branch selection cancelled.[/dim]")
        else:
            ok, message = evolution_lab.set_active_branch(branch_id)
            console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
    elif sub == "chat":
        branch = evolution_lab.read_branch()
        feedback = _decode_text_arg(evolve_args_raw) if evolve_args_raw else ""
        if branch is None or not feedback:
            console.print("[yellow]Usage: /evolve chat <refinement>[/yellow]")
        else:
            evolution_lab.add_branch_note(branch["id"], feedback)
            worker = _evolution_lab_start_worker(branch["id"], session, feedback)
            console.print(f"[green]Refinement worker: {worker}[/green]" if worker
                          else "[red]Could not start refinement worker.[/red]")
    elif sub in ("candidates", "list"):
        candidates = evolution_lab.list_candidates()
        if not candidates:
            console.print("[dim]No Evolution Lab candidates.[/dim]")
        for candidate in candidates:
            console.print(
                f"  [cyan]{candidate.get('id')}[/cyan] "
                f"[{candidate.get('intent')}] [dim]{candidate.get('status')}[/dim] "
                f"— {candidate.get('target_type')}:{candidate.get('name')}")
    elif sub == "review":
        candidate_id = parts[2] if len(parts) > 2 else evolution_lab.active_candidate_id()
        candidate = evolution_lab.read_candidate(candidate_id)
        if candidate is None:
            console.print("[red]Evolution candidate not found.[/red]")
        else:
            files = "\n\n".join(
                f"--- {item.get('path')} ---\n{item.get('content', '')}"
                for item in candidate.get("files") or [])
            runs = candidate.get("test_runs") or []
            report = json.dumps(runs[-1].get("report"), ensure_ascii=False, indent=2) \
                if runs else "(not tested)"
            _print_long_panel(
                f"Candidate: {candidate.get('id')}\n"
                f"Intent: {candidate.get('intent')}\n"
                f"Target: {candidate.get('target_type')}:{candidate.get('name')}\n"
                f"SHA-256: {candidate.get('candidate_sha256')}\n"
                f"Dependencies: {candidate.get('dependencies') or '(none)'}\n\n"
                f"Description\n{candidate.get('description', '')}\n\n"
                f"Files\n{files}\n\nLatest test\n{report}",
                title="Evolution Candidate", border_style="blue")
    elif sub == "test":
        candidate_id = parts[2] if len(parts) > 2 else evolution_lab.active_candidate_id()
        if not candidate_id:
            console.print("[red]No candidate to test.[/red]")
        else:
            ok, message, run = evolution_lab.test_candidate(candidate_id)
            detail = json.dumps((run or {}).get("report"), ensure_ascii=False, indent=2)
            console.print(Panel(detail, title=message,
                                border_style="green" if ok else "red"))
    elif sub == "activate":
        candidate_id = next((item for item in parts[2:] if item != "--force"), None)
        candidate_id = candidate_id or evolution_lab.active_candidate_id()
        candidate = evolution_lab.read_candidate(candidate_id)
        if candidate is None:
            console.print("[red]Evolution candidate not found.[/red]")
        else:
            preview = (
                f"Candidate: {candidate_id}\n"
                f"Target: {candidate.get('target_type')}:{candidate.get('name')}\n"
                f"SHA-256: {candidate.get('candidate_sha256')}\n"
                f"Files: {', '.join(item.get('path', '') for item in candidate.get('files') or [])}"
            )
            choice = _blocking_approval_prompt(
                "Evolution activation", preview,
                "Activate and hot-load this feature candidate?", allow_always=False)
            if choice != "yes":
                console.print("[yellow]Evolution activation cancelled.[/yellow]")
            else:
                ok, message = evolution_lab.activate_candidate(
                    candidate_id, runtime=runtime, force="--force" in parts[2:])
                console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
    elif sub == "disable":
        extension_name = parts[2] if len(parts) >= 3 else ""
        if not extension_name and sys.stdin.isatty():
            chosen = choose_record(
                runtime.list(),
                title="Disable Extension",
                label=lambda item: item.get("name", ""),
                description=lambda item: (
                    f"v{item.get('version') or f'?'} {symbols.BULLET} {item.get('path', '')}"),
                search=True,
            )
            extension_name = chosen.get("name", "") if chosen else ""
        if not extension_name:
            console.print("[dim]Extension selection cancelled.[/dim]")
        else:
            choice = _blocking_approval_prompt(
                "Disable extension", f"Extension: {extension_name}",
                "Disable and unload this extension?", allow_always=False)
            if choice == "yes":
                ok, message = evolution_lab.disable_extension(extension_name, runtime)
                console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
    elif sub == "profiles":
        for profile in evolution_lab.list_profiles():
            marker = "*" if profile.get("active") else " "
            console.print(
                f"{marker} [cyan]{profile.get('name')}[/cyan] "
                f"[dim]{len(profile.get('extensions') or {})} extension(s)[/dim]")
    elif sub == "profile":
        if len(parts) < 4 or parts[2].lower() != "create":
            console.print("[yellow]Usage: /evolve profile create <name> [candidate-id ...][/yellow]")
        else:
            ok, message = evolution_lab.create_profile(parts[3], parts[4:])
            console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
    elif sub == "use":
        profile_name = parts[2] if len(parts) >= 3 else ""
        if not profile_name and sys.stdin.isatty():
            chosen = choose_record(
                evolution_lab.list_profiles(),
                title="Select Evolution Profile",
                label=lambda item: item.get("name", ""),
                description=lambda item: (
                    f"{len(item.get('extensions') or {})} extension(s)"),
                search=True,
            )
            profile_name = chosen.get("name", "") if chosen else ""
        if not profile_name:
            console.print("[dim]Profile selection cancelled.[/dim]")
        else:
            choice = _blocking_approval_prompt(
                "Evolution profile", f"Profile: {profile_name}",
                "Switch extension profile and hot-reload?", allow_always=False)
            if choice == "yes":
                ok, message = evolution_lab.switch_profile(profile_name, runtime)
                console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
    elif sub == "rollback":
        choice = _blocking_approval_prompt(
            "Evolution rollback", "Restore the previous feature state.",
            "Roll back and hot-reload now?", allow_always=False)
        if choice == "yes":
            ok, message = evolution_lab.rollback(runtime)
            console.print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")
    else:
        console.print(
            "[bold]Evolution Lab[/bold]\n"
            "  /evolve <idea>\n  /evolve status|branches|candidates\n"
            "  /evolve chat <refinement>\n  /evolve review|test|activate [id]\n"
            "  /evolve disable <extension>\n"
            "  /evolve profiles|profile create|use|rollback")


def _cmd_prompt(raw_args: str, parts: list, session: dict) -> None:
    import prompt_opt as _po
    from rich.markup import escape as _escape
    sub = parts[1].lower() if len(parts) > 1 else ""
    _, prompt_args_raw = _raw_tail_after_word(raw_args)
    _legacy_prompt_commands = {
        "feedback", "fail", "optimize", "apply", "discard", "list",
        "skill", "export", "install", "publish",
    }
    _lab_prompt_commands = {
        "status", "branches", "open", "chat", "review", "test",
        "activate", "disable", "patches", "profiles", "profile",
        "use", "rollback", "help",
    }

    # `/prompt` and `/prompt <natural language>` are the primary UX. They
    # capture an immutable incident and launch an isolated diagnosis worker.
    if not sub or sub not in (_legacy_prompt_commands | _lab_prompt_commands):
        description = (_decode_text_arg(raw_args) if raw_args
                       else "Review the latest AI behavior and identify what should improve")
        branch = _prompt_lab_create(description, session)
        worker = branch.get("worker_agent_id")
        console.print(Panel(
            f"[bold]Prompt Lab branch created[/bold]\n\n"
            f"Branch: [cyan]{branch['id']}[/cyan]\n"
            f"Status: [bold]{branch.get('status', 'CAPTURED')}[/bold]\n"
            f"Issue: {_escape(branch.get('description', ''))}\n"
            + (f"Worker: [cyan]{worker}[/cyan]\n" if worker else
               "[yellow]No active parent agent; snapshot saved without a worker.[/yellow]\n")
            + "\n[dim]The main task and active prompt are unchanged. Use "
              "/prompt status, /prompt chat <message>, and /prompt review.[/dim]",
            title="Prompt Lab", border_style="cyan",
        ))
    elif sub == "branches":
        branches = prompt_lab.list_branches()
        if not branches:
            console.print("[dim]No Prompt Lab branches in this project.[/dim]")
        else:
            console.print("[bold]Prompt Lab branches[/bold]")
            for branch in branches:
                console.print(
                    f"  [cyan]{branch.get('id')}[/cyan] "
                    f"[dim]{branch.get('status')}[/dim] — "
                    f"{_escape(str(branch.get('description') or '')[:80])}")
    elif sub == "open":
        branch_id = parts[2] if len(parts) >= 3 else ""
        if not branch_id and sys.stdin.isatty():
            chosen = choose_record(
                prompt_lab.list_branches(),
                title="Open Prompt Lab Branch",
                label=lambda item: item.get("id", ""),
                description=lambda item: (
                    f"{item.get('status')} {symbols.BULLET} "
                    f"{str(item.get('description') or '')[:100]}"),
                search=True,
            )
            branch_id = chosen.get("id", "") if chosen else ""
        if not branch_id:
            console.print("[dim]Branch selection cancelled.[/dim]")
        else:
            ok, msg = prompt_lab.set_active_branch(branch_id)
            console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
    elif sub == "chat":
        branch = prompt_lab.read_branch()
        message = _decode_text_arg(prompt_args_raw) if prompt_args_raw else ""
        if branch is None:
            console.print("[yellow]No active Prompt Lab branch. Run /prompt <issue> first.[/yellow]")
        elif not message:
            console.print("[yellow]Usage: /prompt chat <feedback or refinement>[/yellow]")
        else:
            prompt_lab.add_branch_note(branch["id"], message, kind="user-feedback")
            child_id = _prompt_lab_start_worker(branch["id"], session, message)
            if child_id:
                console.print(f"[green]Prompt Lab refinement worker started: {child_id}[/green]")
            else:
                console.print("[red]Could not start refinement worker; no active parent agent.[/red]")
    elif sub == "status" and prompt_lab.read_branch() is not None:
        branch = prompt_lab.read_branch() or {}
        patch = prompt_lab.read_patch(str(branch.get("candidate_patch_id") or ""))
        profile = prompt_lab.get_active_profile()
        test_status = "not run"
        if patch and patch.get("test_runs"):
            test_status = "passed" if patch["test_runs"][-1].get("passed") else "FAILED"
        console.print(Panel(
            f"Branch: [cyan]{branch.get('id', '?')}[/cyan]\n"
            f"Status: [bold]{branch.get('status', '?')}[/bold]\n"
            f"Candidate: [cyan]{branch.get('candidate_patch_id') or '(none)'}[/cyan]\n"
            f"Tests: [bold]{test_status}[/bold]\n"
            f"Active profile: [cyan]{profile.get('name', 'default')}[/cyan]\n"
            f"Active overlays: {len(profile.get('patches') or [])}",
            title="Prompt Lab Status", border_style="cyan",
        ))
    elif sub == "patches":
        patches = prompt_lab.list_patches()
        if not patches:
            console.print("[dim]No Prompt Lab patches in this project.[/dim]")
        else:
            console.print("[bold]Prompt Lab patches[/bold]")
            for patch in patches:
                console.print(
                    f"  [cyan]{patch.get('id')}[/cyan] "
                    f"[dim]{patch.get('status')}[/dim] — "
                    f"{_escape(str(patch.get('title') or ''))}")
    elif sub == "review":
        patch_id = parts[2] if len(parts) >= 3 else prompt_lab.active_patch_id()
        patch = prompt_lab.read_patch(patch_id or "") if patch_id else None
        if patch is None:
            # Fall through to the legacy candidate reader for compatibility.
            cid = parts[2] if len(parts) >= 3 else None
            cand = _po.read_candidate(cid)
            if not cand:
                console.print("[yellow]No Prompt Lab or legacy candidate found.[/yellow]")
            else:
                _print_long_panel(
                    f"[bold]Legacy candidate:[/bold] {cand.get('id', '?')}\n\n"
                    f"{cand.get('body', '')}",
                    title="Legacy Prompt Candidate", border_style="blue")
        else:
            runs = patch.get("test_runs") or []
            last_report = runs[-1].get("report", "") if runs else "(not tested)"
            test_cases = "\n".join(
                f"- {case.get('name')}: {case.get('expected')}"
                for case in patch.get("tests") or []) or "(none)"
            _print_long_panel(
                f"[bold]Patch:[/bold] {patch.get('id')}\n"
                f"[bold]Status:[/bold] {patch.get('status')}\n\n"
                f"[bold]Diagnosis[/bold]\n{patch.get('diagnosis', '')}\n\n"
                f"[bold]Rationale[/bold]\n{patch.get('rationale', '')}\n\n"
                f"[bold]Overlay[/bold]\n{patch.get('content', '')}\n\n"
                f"[bold]Regression cases[/bold]\n{test_cases}\n\n"
                f"[bold]Latest test report[/bold]\n{last_report}",
                title="Prompt Lab Review", border_style="blue")
    elif sub == "test":
        patch_id = parts[2] if len(parts) >= 3 else prompt_lab.active_patch_id()
        if not patch_id:
            console.print("[yellow]No candidate patch. Wait for diagnosis or run /prompt chat.[/yellow]")
        else:
            ok, msg = _prompt_lab_start_test(patch_id, session)
            console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
    elif sub == "activate":
        patch_id = next((item for item in parts[2:] if item != "--force"), None)
        patch_id = patch_id or prompt_lab.active_patch_id()
        patch = prompt_lab.read_patch(patch_id or "") if patch_id else None
        if patch is None:
            console.print("[red]Prompt Lab patch not found.[/red]")
        else:
            runs = patch.get("test_runs") or []
            passed = bool(runs and runs[-1].get("passed"))
            if not passed and "--force" not in parts[2:]:
                console.print("[red]The latest regression run has not passed. Run /prompt test first, or explicitly use --force.[/red]")
            else:
                ok, preview = prompt_lab.preview_activation(patch_id)
                if not ok:
                    console.print(f"[red]{_escape(preview)}[/red]")
                else:
                    if not passed:
                        preview = "WARNING: activating without a passing test.\n\n" + preview
                    choice = _blocking_approval_prompt(
                        "Prompt Lab activation",
                        preview,
                        "Activate this prompt overlay and hot-reload now?",
                        allow_always=False,
                    )
                    if choice != "yes":
                        console.print("[yellow]Prompt activation cancelled.[/yellow]")
                    else:
                        ok, msg = prompt_lab.activate_patch(patch_id)
                        console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
    elif sub == "disable":
        patch_id = parts[2] if len(parts) >= 3 else ""
        if not patch_id and sys.stdin.isatty():
            enabled = [
                item for item in prompt_lab.list_patches()
                if item.get("status") == "ACTIVE"
            ]
            chosen = choose_record(
                enabled,
                title="Disable Prompt Overlay",
                label=lambda item: item.get("id", ""),
                description=lambda item: str(item.get("title") or ""),
                search=True,
            )
            patch_id = chosen.get("id", "") if chosen else ""
        if not patch_id:
            console.print("[dim]Prompt overlay selection cancelled.[/dim]")
        else:
            choice = _blocking_approval_prompt(
                "Prompt Lab change",
                f"Patch: {patch_id}",
                "Disable this overlay and hot-reload now?",
                allow_always=False,
            )
            if choice != "yes":
                console.print("[yellow]Prompt change cancelled.[/yellow]")
            else:
                ok, msg = prompt_lab.disable_patch(patch_id)
                console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
    elif sub == "profiles":
        for profile in prompt_lab.list_profiles():
            marker = "[green]*[/green]" if profile.get("active") else " "
            console.print(
                f"{marker} [cyan]{profile.get('name')}[/cyan] "
                f"[dim]{len(profile.get('patches') or [])} patch(es)[/dim]")
    elif sub == "profile":
        if len(parts) < 4 or parts[2].lower() != "create":
            console.print("[yellow]Usage: /prompt profile create <name> [patch-id ...][/yellow]")
        else:
            ok, msg = prompt_lab.create_profile(parts[3], parts[4:])
            console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
    elif sub == "use":
        profile_name = parts[2] if len(parts) >= 3 else ""
        if not profile_name and sys.stdin.isatty():
            chosen = choose_record(
                prompt_lab.list_profiles(),
                title="Select Prompt Profile",
                label=lambda item: item.get("name", ""),
                description=lambda item: (
                    f"{len(item.get('patches') or [])} patch(es)"),
                search=True,
            )
            profile_name = chosen.get("name", "") if chosen else ""
        if not profile_name:
            console.print("[dim]Profile selection cancelled.[/dim]")
        else:
            profile = next((p for p in prompt_lab.list_profiles()
                            if p.get("name") == profile_name), None)
            if profile is None:
                console.print(f"[red]Profile {profile_name} not found.[/red]")
            else:
                body = (f"Profile: {profile_name}\nPatches:\n" +
                        "\n".join(f"  - {p}" for p in profile.get("patches") or []))
                choice = _blocking_approval_prompt(
                    "Prompt Lab profile switch", body,
                    "Switch profile and hot-reload now?", allow_always=False)
                if choice != "yes":
                    console.print("[yellow]Profile switch cancelled.[/yellow]")
                else:
                    ok, msg = prompt_lab.switch_profile(profile_name)
                    console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
    elif sub == "rollback":
        choice = _blocking_approval_prompt(
            "Prompt Lab rollback",
            "The latest prompt activation, disable, or profile switch will be reverted.",
            "Roll back and hot-reload now?",
            allow_always=False,
        )
        if choice != "yes":
            console.print("[yellow]Prompt rollback cancelled.[/yellow]")
        else:
            ok, msg = prompt_lab.rollback()
            console.print(f"[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]")
    elif sub == "help":
        console.print(
            "[bold]Prompt Lab[/bold]\n"
            "  /prompt [issue]                  Capture an incident and start diagnosis\n"
            "  /prompt chat <message>           Refine with AI in the active branch\n"
            "  /prompt status|branches|patches  Inspect project state\n"
            "  /prompt review [patch-id]        Review diagnosis, overlay, and tests\n"
            "  /prompt test [patch-id]          Run an isolated AI regression evaluation\n"
            "  /prompt activate [id] [--force]  Confirm, activate, and hot-reload\n"
            "  /prompt disable <id>             Confirm and disable an overlay\n"
            "  /prompt profiles|use <name>      Manage switchable prompt profiles\n"
            "  /prompt rollback                 Confirm and undo latest prompt change")
    elif sub == "feedback" and len(parts) >= 3:
        desc = _decode_text_arg(prompt_args_raw)
        entry = _po.capture_feedback(desc)
        parent = get_current_agent()
        if parent:
            child_id = _po.spawn_optimizer(
                entry["id"], parent.id, get_loop_deps(), session)
            if child_id:
                console.print(Panel(
                    f"[bold]Feedback captured.[/bold] Optimizer spawned in background.\n\n"
                    f"Feedback ID: [cyan]{entry['id']}[/cyan]\n"
                    f"Optimizer agent: [cyan]{child_id}[/cyan]\n\n"
                    f"[dim]The main task continues uninterrupted. The candidate will\n"
                    f"arrive via inbox when ready. Run [bold]/prompt status[/bold] to check.[/dim]",
                    title="Prompt Optimization", border_style="green"))
            else:
                console.print(Panel(
                    f"[bold]Feedback captured.[/bold] (ID: {entry['id']})\n\n"
                    f"[yellow]Optimizer spawn failed — max depth may be reached.[/yellow]\n"
                    f"[dim]Run [bold]/prompt optimize {entry['id']}[/bold] later from the REPL.[/dim]",
                    title="Prompt Optimization", border_style="yellow"))
        else:
            console.print(Panel(
                f"[bold]Feedback captured.[/bold] (ID: {entry['id']})\n\n"
                f"[dim]No active agent to spawn the optimizer from. Run\n"
                f"[bold]/prompt optimize {entry['id']}[/bold] when an agent is active.[/dim]",
                title="Prompt Optimization", border_style="cyan"))
    elif sub == "optimize" and len(parts) >= 3:
        fid = parts[2]
        parent = get_current_agent()
        if not parent:
            console.print("[red]No active agent. /hire one first.[/red]")
        else:
            child_id = _po.spawn_optimizer(fid, parent.id, get_loop_deps(), session)
            if child_id:
                console.print(f"[green]Optimizer spawned: {child_id}[/green]")
            else:
                console.print("[red]Spawn failed (max depth reached?)[/red]")
    elif sub == "status":
        state = _po.get_optimization_state(
            parts[2] if len(parts) >= 3 else None)
        if not state:
            console.print("[dim]No active prompt optimization.[/dim]")
        else:
            status = state.get("status", "?")
            cid = state.get("candidate_id") or "(none)"
            fid = state.get("feedback_id") or "(none)"
            console.print(Panel(
                f"Status: [bold]{status}[/bold]\n"
                f"Feedback: [cyan]{fid}[/cyan]\n"
                f"Candidate: [cyan]{cid}[/cyan]",
                title="Prompt Optimization Status", border_style="cyan"))
    elif sub == "review":
        cid = parts[2] if len(parts) >= 3 else None
        cand = _po.read_candidate(cid)
        if not cand:
            console.print("[yellow]No candidate found. Run /prompt feedback first.[/yellow]")
        else:
            patch = cand.get("patch", "")
            rationale = cand.get("body", "")
            _print_long_panel(
                f"[bold]Candidate:[/bold] {cand.get('id', '?')}\n\n"
                f"[dim]Rationale:[/dim]\n{rationale}\n\n"
                f"[dim]Patch (to be appended to cli.prop):[/dim]\n{patch}",
                title="Prompt Candidate Review", border_style="blue")
            console.print("\n[dim]Run [bold]/prompt apply[/bold] to activate, "
                          "[bold]/prompt discard[/bold] to reject.[/dim]")
    elif sub == "apply":
        cid = next((item for item in parts[2:] if item != "--force"), None)
        cand = _po.read_candidate(cid)
        if not cand:
            console.print("[red]No legacy candidate found to apply.[/red]")
        else:
            choice = _blocking_approval_prompt(
                "Legacy prompt activation",
                f"Candidate: {cand.get('id', '?')}\n\n{cand.get('patch', '')}",
                "Modify cli.prop and hot-reload this legacy patch?",
                allow_always=False,
            )
            if choice != "yes":
                console.print("[yellow]Legacy prompt activation cancelled.[/yellow]")
            else:
                ok, msg = _po.apply_candidate(cid, force="--force" in parts[2:])
                color = "green" if ok else "red"
                console.print(f"[{color}]{msg}[/{color}]")
    elif sub == "discard":
        cid = parts[2] if len(parts) >= 3 else None
        choice = _blocking_approval_prompt(
            "Legacy prompt rollback",
            f"Candidate: {cid or '(active legacy candidate)'}",
            "Remove the active legacy patch and hot-reload?",
            allow_always=False,
        )
        if choice != "yes":
            console.print("[yellow]Legacy prompt rollback cancelled.[/yellow]")
        else:
            ok, msg = _po.discard_candidate(cid)
            color = "green" if ok else "red"
            console.print(f"[{color}]{msg}[/{color}]")
    elif sub == "list":
        cands = _po.list_candidates()
        if cands:
            console.print("[bold]All candidates:[/bold]")
            for c in cands:
                ctype = c.get("type", "cli_prop")
                extra = ""
                if ctype == "skill_patch":
                    extra = f" [{c.get('skill_name', '')}/{c.get('skill_file', '')}]"
                console.print(f"  [cyan]{c['id']}[/cyan] "
                              f"[dim]{c['status']}[/dim] "
                              f"{ctype}{extra} "
                              f"— {c.get('feedback', '')[:40]}")
        else:
            console.print("[dim]No candidates. Run /prompt feedback or /prompt fail to start.[/dim]")
    elif sub == "export" and len(parts) >= 3:
        cid = parts[2]
        out = parts[3] if len(parts) >= 4 else None
        ok, res = _po.export_pack(cid, out)
        color = "green" if ok else "red"
        console.print(f"[{color}]{res}[/{color}]")
    elif sub == "install" and len(parts) >= 3:
        src = parts[2]
        ok, msg, new_cid = _po.install_pack(src)
        color = "green" if ok else "red"
        console.print(f"[{color}]{msg}[/{color}]")
    elif sub == "publish" and len(parts) >= 3:
        cid = parts[2]
        ok, msg = _po.publish_pack(cid, session)
        color = "green" if ok else "yellow"
        console.print(f"[{color}]{msg}[/{color}]")
    elif sub == "fail":
        fields = None
        if prompt_args_raw:
            try:
                fields = None
                last_error = None
                for candidate in _json_arg_candidates(prompt_args_raw):
                    try:
                        fields = json.loads(candidate)
                        break
                    except json.JSONDecodeError as exc:
                        last_error = exc
                if fields is None and last_error is not None:
                    raise last_error
            except json.JSONDecodeError as exc:
                console.print(f"[red]Invalid failure-report JSON: {exc}[/red]")
                console.print("[dim]Use /prompt fail with no arguments for an interactive form.[/dim]")
                fields = None
            if fields is not None and not isinstance(fields, dict):
                console.print("[red]Failure report must be a JSON object.[/red]")
                fields = None
        elif sys.stdin.isatty():
            _reader_was_running = bool(
                _bg_reader_thread is not None and _bg_reader_thread.is_alive())
            _stop_bg_input_reader()
            try:
                console.print(Panel(
                    "Enter a concise failure report. Task and actual behavior are required.",
                    title="Prompt Failure Report", border_style="cyan"))
                task_text = input("Task: ").strip()
                expected = input("Expected behavior: ").strip()
                actual = input("Actual behavior: ").strip()
                category_options = ["Unspecified", *_po.FAILURE_CATEGORIES]
                category_choice = select_dialog(
                    category_options,
                    title="Failure category",
                    full_screen=False,
                    selected_index=0,
                    hint=f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ select  Esc/q keep unspecified",
                )
                category = ("" if category_choice in (None, "Unspecified")
                            else category_choice)
                minimal_fix = input("Minimal fix (optional): ").strip()
                regression = input("Regression tests (optional): ").strip()
                fields = {
                    "task": task_text, "expected": expected,
                    "actual": actual, "category": category,
                    "minimal_fix": minimal_fix,
                    "regression_tests": regression,
                }
            except (EOFError, KeyboardInterrupt):
                fields = None
            finally:
                if _reader_was_running:
                    _start_bg_input_reader(get_user_message_queue(),
                                           get_user_interrupt_event())
        else:
            console.print(Panel(
                _po.get_failure_template(),
                title="Failure Report Template (v3)",
                border_style="cyan"))
            console.print(
                "[dim]In a non-interactive shell pass a JSON object, for example: "
                "/prompt fail '{\"task\":\"...\",\"actual\":\"...\"}'[/dim]")

        if fields is not None:
            if not fields.get("task") and not fields.get("actual"):
                console.print("[red]Task or actual behavior is required; report not saved.[/red]")
            elif (fields.get("category")
                  and fields.get("category") not in _po.FAILURE_CATEGORIES):
                console.print(f"[red]Invalid category: {fields.get('category')}[/red]")
                console.print("[dim]Use one of: " + "; ".join(_po.FAILURE_CATEGORIES) + "[/dim]")
            else:
                entry = _po.capture_structured_failure(fields)
                parent = get_current_agent()
                child_id = (_po.spawn_optimizer(
                    entry["id"], parent.id, get_loop_deps(), session)
                    if parent else None)
                console.print(Panel(
                    f"Feedback ID: [cyan]{entry['id']}[/cyan]\n"
                    + (f"Optimizer: [cyan]{child_id}[/cyan]" if child_id
                       else "Optimizer not started (no active agent)."),
                    title="Failure report saved", border_style="green",
                ))
    elif sub == "skill":
        sub2 = parts[2].lower() if len(parts) > 2 else ""
        if sub2 == "list":
            patches = _po.list_skill_patches()
            if patches:
                console.print("[bold]Skill patches:[/bold]")
                for p in patches:
                    console.print(
                        f"  [cyan]{p['id']}[/cyan] "
                        f"[dim]{p['status']}[/dim] "
                        f"— {p.get('skill_name', '?')}/"
                        f"{p.get('skill_file', '?')} "
                        f"({p.get('mode', '?')})")
            else:
                console.print("[dim]No skill patches. Run /prompt fail to start diagnosis.[/dim]")
        elif sub2 == "review":
            cid = parts[3] if len(parts) > 3 else None
            if not cid and sys.stdin.isatty():
                chosen = choose_record(
                    _po.list_skill_patches(),
                    title="Review Skill Patch",
                    label=lambda item: item.get("id", ""),
                    description=lambda item: (
                        f"{item.get(f'status')} {symbols.BULLET} {item.get('skill_name', '?')}/"
                        f"{item.get('skill_file', '?')}"),
                    search=True,
                )
                cid = chosen.get("id") if chosen else None
            patch = _po.read_skill_patch(cid)
            if not patch:
                console.print("[yellow]No skill patch found. Run /prompt skill list for ids.[/yellow]")
            else:
                mode = patch.get("mode", "?")
                if mode == "append":
                    patch_preview = patch.get("patch", "")
                else:
                    patch_preview = (
                        f"OLD:\n{patch.get('old_string', '')}\n\n"
                        f"NEW:\n{patch.get('new_string', '')}"
                    )
                _print_long_panel(
                    f"[bold]Skill Patch:[/bold] {patch.get('id', '?')}\n"
                    f"[bold]Skill:[/bold] {patch.get('skill_name', '?')}/{patch.get('skill_file', '?')}\n"
                    f"[bold]Mode:[/bold] {mode}\n\n"
                    f"[dim]Rationale:[/dim]\n{patch.get('body', '')}\n\n"
                    f"[dim]Patch:[/dim]\n{patch_preview}",
                    title="Skill Patch Review", border_style="blue")
                console.print("\n[dim]Run [bold]/prompt skill apply <id>[/bold] to activate, "
                              "[bold]/prompt skill discard <id>[/bold] to reject.[/dim]")
        elif sub2 == "apply":
            cid = next((item for item in parts[3:] if item != "--force"), None)
            if not cid and sys.stdin.isatty():
                chosen = choose_record(
                    _po.list_skill_patches(),
                    title="Apply Skill Patch",
                    label=lambda item: item.get("id", ""),
                    description=lambda item: (
                        f"{item.get(f'status')} {symbols.BULLET} {item.get('skill_name', '?')}/"
                        f"{item.get('skill_file', '?')}"),
                    search=True,
                )
                cid = chosen.get("id") if chosen else None
            if not cid:
                console.print("[dim]Skill patch selection cancelled.[/dim]")
            else:
                candidate = _po.read_skill_patch(cid)
                preview = (
                    f"Patch: {cid}\nSkill: "
                    f"{(candidate or {}).get('skill_name', '?')}/"
                    f"{(candidate or {}).get('skill_file', '?')}\n\n"
                    f"{(candidate or {}).get('patch', '')}"
                )
                choice = _blocking_approval_prompt(
                    "Skill prompt activation", preview,
                    "Modify this skill and hot-reload it?", allow_always=False)
                if choice != "yes":
                    console.print("[yellow]Skill patch activation cancelled.[/yellow]")
                else:
                    ok, msg = _po.apply_skill_patch(
                        cid, force="--force" in parts[3:])
                    color = "green" if ok else "red"
                    console.print(f"[{color}]{msg}[/{color}]")
        elif sub2 == "discard":
            cid = parts[3] if len(parts) > 3 else None
            if not cid and sys.stdin.isatty():
                chosen = choose_record(
                    _po.list_skill_patches(),
                    title="Discard Skill Patch",
                    label=lambda item: item.get("id", ""),
                    description=lambda item: (
                        f"{item.get(f'status')} {symbols.BULLET} {item.get('skill_name', '?')}/"
                        f"{item.get('skill_file', '?')}"),
                    search=True,
                )
                cid = chosen.get("id") if chosen else None
            if not cid:
                console.print("[dim]Skill patch selection cancelled.[/dim]")
            else:
                choice = _blocking_approval_prompt(
                    "Skill prompt rollback", f"Patch: {cid}",
                    "Restore the skill backup and hot-reload it?", allow_always=False)
                if choice != "yes":
                    console.print("[yellow]Skill patch rollback cancelled.[/yellow]")
                else:
                    ok, msg = _po.discard_skill_patch(cid)
                    color = "green" if ok else "red"
                    console.print(f"[{color}]{msg}[/{color}]")
        else:
            console.print("Usage:\n"
                          "  [bold]/prompt skill list[/bold]            — List skill patches\n"
                          "  [bold]/prompt skill review <id>[/bold]     — Review a skill patch\n"
                          "  [bold]/prompt skill apply <id> [--force][/bold] — Apply a skill patch\n"
                          "  [bold]/prompt skill discard <id>[/bold]    — Discard a skill patch")
    else:
        console.print("Usage:\n"
                      "  [bold]/prompt feedback <desc>[/bold]    — Capture feedback & spawn optimizer\n"
                      "  [bold]/prompt fail[/bold]                — Show failure template (v3)\n"
                      "  [bold]/prompt optimize <id>[/bold]       — Spawn optimizer for a feedback id\n"
                      "  [bold]/prompt status[/bold]              — Show optimization status\n"
                      "  [bold]/prompt review [id][/bold]         — Review cli.prop candidate patch\n"
                      "  [bold]/prompt apply [id] [--force][/bold] — Apply candidate to cli.prop\n"
                      "  [bold]/prompt discard [id][/bold]        — Strip applied cli.prop patch\n"
                      "  [bold]/prompt list[/bold]                — List all candidates (cli.prop + skill)\n"
                      "  [bold]/prompt skill list|review|apply|discard <id>[/bold] — Manage skill patches\n"
                      "  [bold]/prompt export <id> [path][/bold]  — Export portable pack\n"
                      "  [bold]/prompt install <path|url>[/bold]  — Import a shared pack\n"
                      "  [bold]/prompt publish <id>[/bold]        — Publish to community")



def _cmd_work(parts: list) -> None:
    sub = parts[1].lower() if len(parts) > 1 else "status"
    if sub == "status":
        work = workgraph.get_active_work()
        if not work:
            console.print("[dim]No active WorkGraph in this project.[/dim]")
        else:
            steps = workgraph.list_steps(work["id"])
            done = sum(1 for step in steps if step.get("status") in {"completed", "skipped"})
            console.print(Panel(
                f"ID: [cyan]{work['id']}[/cyan]\n"
                f"Objective: {work['objective']}\n"
                f"Status: [bold]{work['status']}[/bold]\n"
                f"Revision: {work.get('current_revision') or 0}\n"
                f"Approved: {work.get('approved_revision') or '(none)'}\n"
                f"Workflow: {work.get('workflow_template') or '(none)'} / "
                f"{work.get('workflow_phase') or '(none)'}\n"
                f"Steps: {done}/{len(steps)} complete",
                title="Active WorkGraph", border_style="cyan"))
    elif sub == "list":
        items = workgraph.list_work()
        if not items:
            console.print("[dim]No WorkGraph history in this project.[/dim]")
        for item in items:
            console.print(
                f"  [cyan]{item['id']}[/cyan] [dim]{item['status']}[/dim] — "
                f"{item['objective'][:100]}")
    elif sub == "resume":
        work_id = parts[2] if len(parts) >= 3 else ""
        if not work_id and sys.stdin.isatty():
            chosen = choose_record(
                workgraph.list_work(),
                title="Resume WorkGraph",
                label=lambda item: item["id"],
                description=lambda item: (
                    f"{item[f'status']} {symbols.BULLET} {item['objective'][:100]}"),
                search=True,
            )
            work_id = chosen["id"] if chosen else ""
        if not work_id:
            console.print("[dim]WorkGraph selection cancelled.[/dim]")
        else:
            work = workgraph.get_work(work_id)
            if not work:
                console.print(f"[red]WorkGraph {work_id} not found.[/red]")
            else:
                workgraph.set_active_work(work["id"])
                if work["status"] in {"DRAFT", "REVIEW_PENDING", "NEEDS_USER", "BLOCKED"}:
                    import plan_mode as _work_pm
                    _work_pm.attach_work(work["id"])
                console.print(f"[green]Resumed WorkGraph {work['id']}.[/green]")
    elif sub == "history":
        work = workgraph.get_active_work()
        if not work:
            console.print("[dim]No active WorkGraph.[/dim]")
        else:
            for event in reversed(workgraph.list_events(work["id"], limit=30)):
                console.print(
                    f"  [dim]{event['id']}[/dim] {event['event_type']} "
                    f"[dim]{json.dumps(event['payload'], ensure_ascii=False)[:120]}[/dim]")
    else:
        console.print("[yellow]Usage: /work [status|list|resume <id>|history][/yellow]")



def _task_ui_source(task: dict) -> tuple[str, str]:
    """Return a compact source badge and style for TODO/HWO/HWG tasks."""
    metadata = task.get("metadata") or {}
    kind = str(metadata.get("kind") or "")
    if kind == "hwg-node" or metadata.get("nodeId") is not None:
        return "HWG", "agent"
    if metadata.get("workflowRunId"):
        return "HWO", "warning"
    return "TODO", "muted"


def _task_ui_progress(value) -> Text:
    """Small background-free progress meter suitable for every workflow kind."""
    try:
        progress = max(0, min(100, int(value or 0)))
    except (TypeError, ValueError):
        progress = 0
    width = 10
    filled = width if progress >= 100 else int(progress / 100 * width)
    meter = Text()
    meter.append("━" * filled, style="success" if progress >= 100 else "accent")
    meter.append("─" * (width - filled), style="muted")
    meter.append(f" {progress:>3}%", style="white")
    return meter


# Per-agent snapshot of the last-rendered task states, so the live surface can
# print only what changed instead of re-listing the whole (growing) task set on
# every task.create/task.update. Maps agent key -> {task_id: (status, progress)}.
_live_task_snapshots: dict[str, dict[str, tuple]] = {}

_LIVE_TASK_STATUS_UI = {
    "in_progress": ("▶", "warning"),
    "pending": (f"{symbols.DOT_OPEN}", "white"),
    "blocked": ("!", "error"),
    "completed": (f"{symbols.OK}", "success"),
}


def display_live_task_list(tasks: list[dict], agent_id: str) -> None:
    """Print only the tasks that changed since the previous render.

    Reprinting the full list after every mutation floods the scrollback with
    near-duplicate panels (the classic ``0/1``, ``0/2`` … ``2/6`` stack). We
    instead diff against a remembered snapshot and emit a single line per
    changed task, reprinting the header line only on first render or when a
    task actually completes.
    """
    tasks = [item for item in (tasks or [])
             if item.get("status") not in {"deleted", "skipped"}]
    key = agent_id or "current"
    if not tasks:
        _live_task_snapshots.pop(key, None)
        return

    order = [str(item.get("id")) for item in tasks]
    current = {
        str(item.get("id")): (
            str(item.get("status") or "pending"),
            int(item.get("progress") or 0),
            str(item.get("subject") or "(untitled task)"),
        )
        for item in tasks
    }
    previous = _live_task_snapshots.get(key, {})

    # Only the tasks whose status/progress differ from the last render.
    changed = {
        tid for tid, value in current.items()
        if previous.get(tid) != value
    }
    if not changed:
        return  # identical re-emit — nothing new to show
    _live_task_snapshots[key] = current

    completed = sum(1 for v in current.values() if v[0] == "completed")
    prev_completed = sum(1 for v in previous.values() if v[0] == "completed")
    # Header carries the running "done" count; show it on the first render and
    # whenever the completed tally moves. Pure additions/starts just append
    # their own line beneath the existing header.
    if not previous or completed != prev_completed:
        header = Text("Tasks", style="bold white")
        header.append(f" {symbols.BULLET} {agent_id or 'current'}", style="agent")
        header.append(f"  {completed}/{len(current)} done", style="muted")
        console.print(header)

    for tid in order:
        if tid not in changed:
            continue
        status, progress, subject = current[tid]
        mark, style = _LIVE_TASK_STATUS_UI.get(status, (f"{symbols.BULLET}", "white"))
        line = Text(f"  {mark} ", style=style)
        line.append(subject, style="white")
        if status == "in_progress" and progress:
            line.append(f"  {progress}%", style="muted")
        console.print(line)


def _render_task_todolist(tasks: list[dict], cwd: str) -> None:
    """Render the shared TODO/HWO/HWG execution view without colored backgrounds."""
    rank = {"in_progress": 0, "pending": 1, "blocked": 2,
            "completed": 3, "skipped": 4}
    ordered = sorted(
        tasks,
        key=lambda item: (rank.get(item.get("status", "pending"), 9),
                          str(item.get("id", ""))),
    )
    counts = {
        status: sum(1 for item in tasks if item.get("status") == status)
        for status in ("in_progress", "pending", "blocked", "completed")
    }
    heading = Text("Tasks", style="bold white")
    heading.append(f"  {cwd}", style="muted")
    console.print(heading)
    summary = Text()
    summary.append(f"{counts['in_progress']} active", style="warning")
    summary.append(f"  {symbols.BULLET}  ", style="muted")
    summary.append(f"{counts['pending']} pending", style="white")
    if counts["blocked"]:
        summary.append(f"  {symbols.BULLET}  ", style="muted")
        summary.append(f"{counts['blocked']} blocked", style="error")
    summary.append(f"  {symbols.BULLET}  ", style="muted")
    summary.append(f"{counts['completed']} done", style="success")
    console.print(summary)

    table = Table(
        box=box.SIMPLE, show_edge=False, show_lines=False,
        header_style="bold white", padding=(0, 1), expand=True,
    )
    table.add_column("", width=2, no_wrap=True)
    table.add_column("ID", width=7, style="white", no_wrap=True)
    table.add_column("TYPE", width=6, no_wrap=True)
    table.add_column("TASK", ratio=1, style="white")
    table.add_column("PROGRESS", width=15, no_wrap=True)
    status_by_id = {
        str(item.get("id")): item.get("status", "pending") for item in tasks
    }
    status_ui = {
        "in_progress": ("▶", "warning"),
        "completed": (f"{symbols.OK}", "success"),
        "blocked": ("!", "error"),
        "skipped": ("–", "muted"),
        "pending": (f"{symbols.DOT_OPEN}", "white"),
    }
    for task in ordered:
        status = task.get("status", "pending")
        mark, mark_style = status_ui.get(status, (f"{symbols.BULLET}", "white"))
        source, source_style = _task_ui_source(task)
        blocked = [
            str(blocker) for blocker in task.get("blockedBy", [])
            if status_by_id.get(str(blocker), "pending")
            not in ("completed", "deleted", "skipped")
        ]
        subject = Text()
        if task.get("parent_id") or task.get("parentId"):
            subject.append("↳ ", style="muted")
        subject.append(str(task.get("subject") or "(untitled task)"), style="white")
        if blocked:
            subject.append(f"  blocked by {', '.join(blocked)}", style="muted")
        table.add_row(
            Text(mark, style=mark_style),
            Text(str(task.get("id", "")), style="white"),
            Text(source, style=source_style),
            subject,
            _task_ui_progress(task.get("progress", 0)),
        )
    console.print(table)


def _task_agent_order(current: Optional[AgentInfo], tasks: list[dict]) -> list[str]:
    """Return current-agent-first DFS order, retaining finished task owners."""
    if current is None:
        owners = [str(item.get("owner_agent_id") or "") for item in tasks]
        return list(dict.fromkeys(owner for owner in owners if owner))
    order: list[str] = []

    def walk(agent_id: str) -> None:
        if not agent_id or agent_id in order:
            return
        order.append(agent_id)
        info = get_agent(agent_id)
        if info is not None:
            for child_id in info.child_ids:
                walk(str(child_id))

    walk(current.id)
    # A completed child may already have left the live registry. Its persisted
    # owner id still belongs in this session view, after the live DFS tree.
    changed = True
    while changed:
        changed = False
        for item in tasks:
            owner = str(item.get("owner_agent_id") or "")
            parent = str(item.get("parent_agent_id") or "")
            if (owner and (owner == current.id or parent in order)
                    and owner not in order):
                order.append(owner)
                changed = True
    return order


def _render_task_agent_tree(tasks: list[dict], cwd: str,
                            current: Optional[AgentInfo], session_id: str) -> None:
    """Render current agent plus descendant task ownership for one session."""
    order = _task_agent_order(current, tasks)
    current_id = current.id if current is not None else ""
    scoped = []
    for item in tasks:
        owner = str(item.get("owner_agent_id") or current_id)
        if not order or owner in order:
            copy_item = dict(item)
            copy_item["owner_agent_id"] = owner
            scoped.append(copy_item)
    counts = {
        status: sum(1 for item in scoped if item.get("status") == status)
        for status in ("in_progress", "pending", "blocked", "completed")
    }
    heading = Text("Tasks", style="bold white")
    if session_id:
        heading.append(f" {symbols.BULLET} session {session_id[:12]}", style="muted")
    if current_id:
        heading.append(f" {symbols.BULLET} current {current_id}", style="agent")
    heading.append(f"  {cwd}", style="muted")
    console.print(heading)
    console.print(
        f"[warning]{counts['in_progress']} active[/warning]  [muted]{symbols.BULLET}[/muted]  "
        f"{counts['pending']} pending  [muted]{symbols.BULLET}[/muted]  "
        f"[error]{counts['blocked']} blocked[/error]  [muted]{symbols.BULLET}[/muted]  "
        f"[success]{counts['completed']} done[/success]"
    )

    if not scoped:
        console.print("[dim]No tasks in this session and agent tree.[/dim]")
        return
    table = Table(
        box=box.SIMPLE, show_edge=False, show_lines=False,
        header_style="bold white", padding=(0, 1), expand=True,
    )
    table.add_column("AGENT", width=18, no_wrap=True)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("ID", width=7, no_wrap=True)
    table.add_column("TYPE", width=6, no_wrap=True)
    table.add_column("TASK", ratio=1)
    table.add_column("PROGRESS", width=15, no_wrap=True)
    rank = {"in_progress": 0, "pending": 1, "blocked": 2,
            "completed": 3, "skipped": 4}
    status_ui = {
        "in_progress": ("▶", "warning"), "completed": (f"{symbols.OK}", "success"),
        "blocked": ("!", "error"), "skipped": ("–", "muted"),
        "pending": (f"{symbols.DOT_OPEN}", "white"),
    }
    parent_by_agent = {
        str(item.get("owner_agent_id") or ""):
        str(item.get("parent_agent_id") or "")
        for item in scoped if item.get("owner_agent_id")
    }
    for agent_id in order:
        info = get_agent(agent_id)
        if info is not None and info.parent_id:
            parent_by_agent[agent_id] = str(info.parent_id)

    def agent_depth(agent_id: str) -> int:
        depth = 0
        seen = set()
        cursor = agent_id
        while cursor != current_id and cursor not in seen:
            seen.add(cursor)
            cursor = parent_by_agent.get(cursor, "")
            if not cursor:
                break
            depth += 1
        return depth if cursor == current_id else 0

    for index, agent_id in enumerate(order or [current_id or "unowned"]):
        owned = sorted(
            [item for item in scoped if item.get("owner_agent_id") == agent_id],
            key=lambda item: (rank.get(item.get("status", "pending"), 9),
                              str(item.get("id", ""))),
        )
        if not owned:
            continue
        info = get_agent(agent_id)
        agent_label = agent_id
        if index > 0:
            agent_label = "  " * max(0, agent_depth(agent_id) - 1) + "└─ " + agent_id
        if info is not None and info.status:
            agent_label += f" [{info.status}]"
        for task_index, task in enumerate(owned):
            status = task.get("status", "pending")
            mark, mark_style = status_ui.get(status, (f"{symbols.BULLET}", "white"))
            source, source_style = _task_ui_source(task)
            table.add_row(
                Text(agent_label if task_index == 0 else "", style="agent"),
                Text(mark, style=mark_style),
                Text(str(task.get("id", "")), style="white"),
                Text(source, style=source_style),
                Text(str(task.get("subject") or "(untitled task)"), style="white"),
                _task_ui_progress(task.get("progress", 0)),
            )
    console.print(table)


def _cmd_task(raw_args: str, parts: list) -> None:
    sub = parts[1].lower() if len(parts) > 1 else ""
    _, task_args_raw = _raw_tail_after_word(raw_args)
    _current = get_current_agent()
    _cwd = str(
        ((_current.state or {}).get("_task_cwd") if _current else "")
        or os.getcwd())
    _session_id = str(
        ((_current.state or {}).get("_session_id") if _current else "") or ""
    )
    _owner_id = _current.id if _current else None
    _parent_agent_id = _current.parent_id if _current else None

    def _session_tasks():
        return task_manager.list_tasks(
            cwd=_cwd, session_id=_session_id or None)

    def _update_scoped(task_id: str, **kwargs):
        target = task_manager.get_task(
            task_id, cwd=_cwd, session_id=_session_id or None)
        if target is None:
            return False, f"Task '{task_id}' not found", None
        return task_manager.update_task(
            task_id, cwd=_cwd, session_id=_session_id or None,
            owner_agent_id=target.get("owner_agent_id"),
            parent_agent_id=target.get("parent_agent_id"), **kwargs)

    def _pick_task(title: str, statuses: Optional[set[str]] = None):
        candidates = [
            item for item in _session_tasks()
            if item.get("status") != "deleted"
            and (statuses is None or item.get("status") in statuses)
        ]
        return choose_record(
            candidates,
            title=title,
            label=lambda item: f"{item[f'id']} {symbols.BULLET} {item.get('subject', '(untitled)')}",
            description=lambda item: (
                f"{item.get('status', 'pending')} {symbols.BULLET} "
                f"{item.get('progress', 0)}%"),
            search=True,
        )

    if sub in ("", "list"):
        _tasks = [t for t in _session_tasks()
                  if t.get("status") != "deleted"]
        if not _tasks:
            console.print("[dim]No tasks. Use [bold]/task add <subject>[/bold] to create one.[/dim]")
        else:
            _render_task_agent_tree(_tasks, _cwd, _current, _session_id)

    elif sub == "mine":
        _tasks = [
            t for t in task_manager.list_tasks(
                cwd=_cwd, session_id=_session_id or None,
                owner_agent_id=_owner_id)
            if t.get("status") != "deleted"
        ]
        _render_task_todolist(_tasks, _cwd)

    elif sub == "agent":
        target_agent = parts[2] if len(parts) >= 3 else ""
        allowed = set(_task_agent_order(_current, _session_tasks()))
        if not target_agent or target_agent not in allowed:
            console.print("[red]Agent must be the current agent or one of its descendants.[/red]")
        else:
            _tasks = [
                t for t in task_manager.list_tasks(
                    cwd=_cwd, session_id=_session_id or None,
                    owner_agent_id=target_agent)
                if t.get("status") != "deleted"
            ]
            _render_task_todolist(_tasks, _cwd)

    elif sub == "add":
        subject = _decode_text_arg(task_args_raw)
        if not subject:
            console.print("[yellow]Usage: /task add <subject>[/yellow]")
        else:
            _tk = task_manager.create_task(
                subject, cwd=_cwd, session_id=_session_id or None,
                owner_agent_id=_owner_id, parent_agent_id=_parent_agent_id,
                session_only=True)
            console.print(f"[green]Created task [bold]{_tk['id']}[/bold]: {subject}[/green]")

    elif sub == "show":
        task_id = parts[2] if len(parts) >= 3 else ""
        if not task_id and sys.stdin.isatty():
            chosen = _pick_task("View Task")
            task_id = chosen["id"] if chosen else ""
        if not task_id:
            console.print("[dim]Task selection cancelled.[/dim]")
        else:
            _tk = task_manager.get_task(
                task_id, cwd=_cwd, session_id=_session_id or None)
            if _tk is None:
                console.print(f"[red]Task '{task_id}' not found.[/red]")
            else:
                notes = "\n".join(_tk.get("notes", [])) or "(none)"
                console.print(Panel(
                    f"Status: {_tk.get('status', 'pending')}\n"
                    f"Progress: {_tk.get('progress', 0)}%\n"
                    f"Blocked by: {', '.join(_tk.get('blockedBy', [])) or '(none)'}\n"
                    f"Blocks: {', '.join(_tk.get('blocks', [])) or '(none)'}\n\n"
                    f"{_tk.get('description', '') or '(no description)'}\n\n"
                    f"[dim]Notes[/dim]\n{notes}",
                    title=f"Task {_tk.get('id')}: {_tk.get('subject', '(untitled)')}",
                    border_style="cyan",
                ))

    elif sub == "start":
        task_id = parts[2] if len(parts) >= 3 else ""
        if not task_id and sys.stdin.isatty():
            chosen = _pick_task("Start Task", {"pending"})
            task_id = chosen["id"] if chosen else ""
        if not task_id:
            console.print("[dim]Task selection cancelled.[/dim]")
        else:
            ok, msg, _tk = _update_scoped(task_id, status="in_progress")
            if ok:
                console.print(f"[yellow]Started task [bold]{_tk['id']}[/bold]: {_tk['subject']}[/yellow]")
            else:
                console.print(f"[red]{msg}[/red]")

    elif sub == "done":
        task_id = parts[2] if len(parts) >= 3 else ""
        if not task_id and sys.stdin.isatty():
            chosen = _pick_task("Complete Task", {"pending", "in_progress"})
            task_id = chosen["id"] if chosen else ""
        if not task_id:
            console.print("[dim]Task selection cancelled.[/dim]")
        else:
            ok, msg, _tk = _update_scoped(
                task_id, status="completed", progress=100)
            if ok:
                console.print(f"[green]Completed task [bold]{_tk['id']}[/bold]: {_tk['subject']}[/green]")
            else:
                console.print(f"[red]{msg}[/red]")

    elif sub == "del":
        task_id = parts[2] if len(parts) >= 3 else ""
        if not task_id and sys.stdin.isatty():
            chosen = _pick_task("Delete Task")
            task_id = chosen["id"] if chosen else ""
        if not task_id:
            console.print("[dim]Task selection cancelled.[/dim]")
        else:
            ok, msg, _tk = _update_scoped(task_id, status="deleted")
            if ok:
                console.print(f"[dim]Deleted task [bold]{_tk['id']}[/bold]: {_tk['subject']}[/dim]")
            else:
                console.print(f"[red]{msg}[/red]")

    elif sub == "progress":
        task_id = parts[2] if len(parts) >= 3 else ""
        progress_value = parts[3] if len(parts) >= 4 else ""
        if not task_id and sys.stdin.isatty():
            chosen = _pick_task("Update Task Progress")
            task_id = chosen["id"] if chosen else ""
        if task_id and not progress_value and sys.stdin.isatty():
            try:
                progress_value = input("Progress (0-100): ").strip()
            except (EOFError, KeyboardInterrupt):
                progress_value = ""
        if not task_id or not progress_value:
            console.print("[dim]Progress update cancelled.[/dim]")
        else:
            ok, msg, _tk = _update_scoped(
                task_id, progress=progress_value)
            if ok:
                console.print(
                    f"[green]Task {_tk['id']} progress: {_tk.get('progress', 0)}%[/green]")
            else:
                console.print(f"[red]{msg}[/red]")

    elif sub == "note":
        task_id, note_raw = _raw_tail_after_word(task_args_raw)
        note = _decode_text_arg(note_raw)
        if not task_id and sys.stdin.isatty():
            chosen = _pick_task("Add Task Note")
            task_id = chosen["id"] if chosen else ""
        if task_id and not note and sys.stdin.isatty():
            try:
                note = input("Note: ").strip()
            except (EOFError, KeyboardInterrupt):
                note = ""
        if not task_id or not note:
            console.print("[dim]Task note cancelled.[/dim]")
        else:
            ok, msg, _tk = _update_scoped(task_id, notes=note)
            if ok:
                console.print(f"[green]Note added to task {_tk['id']}.[/green]")
            else:
                console.print(f"[red]{msg}[/red]")

    elif sub == "subtask":
        parent_id, subject_raw = _raw_tail_after_word(task_args_raw)
        subject = _decode_text_arg(subject_raw)
        if not parent_id and sys.stdin.isatty():
            chosen = _pick_task("Select Parent Task")
            parent_id = chosen["id"] if chosen else ""
        if parent_id and not subject and sys.stdin.isatty():
            try:
                subject = input("Subtask subject: ").strip()
            except (EOFError, KeyboardInterrupt):
                subject = ""
        if not parent_id or not subject:
            console.print("[dim]Subtask creation cancelled.[/dim]")
        else:
            ok, msg, _tk = _update_scoped(parent_id, addSubtask=subject)
            if ok:
                child_id = _tk.get("blocks", ["?"])[-1]
                console.print(
                    f"[green]Created subtask {child_id} under task {_tk['id']}.[/green]")
            else:
                console.print(f"[red]{msg}[/red]")

    else:
        console.print("[yellow]Usage: [bold]/task[/bold] [list|mine|agent|add|show|start|done|del|progress|note|subtask][/yellow]\n"
                      "  [bold]/task[/bold]               — current agent + descendants\n"
                      "  [bold]/task mine[/bold]          — current agent only\n"
                      "  [bold]/task agent <id>[/bold]    — one descendant agent\n"
                      "  [bold]/task add <subject>[/bold] — create a task\n"
                      "  [bold]/task show <id>[/bold]      — show task details\n"
                      "  [bold]/task start <id>[/bold]    — mark as in_progress\n"
                      "  [bold]/task done <id>[/bold]     — mark as completed\n"
                      "  [bold]/task progress <id> <n>[/bold] — update progress\n"
                      "  [bold]/task note <id> <text>[/bold]  — append a note\n"
                      "  [bold]/task subtask <id> <subject>[/bold] — create child task\n"
                      "  [bold]/task del <id>[/bold]      — delete a task")



def _cmd_workflow(raw_args: str, parts: list) -> None:
    import workflow_engine as _we
    sub = parts[1].lower() if len(parts) > 1 else "status"
    if sub == "start":
        _, start_raw = _raw_tail_after_word(raw_args)
        replace = False
        first, rest = _raw_tail_after_word(start_raw)
        if first == "--replace":
            replace = True
            first, rest = _raw_tail_after_word(rest)
        wf_name = first
        wf_desc = _decode_text_arg(rest)
        if not wf_name and sys.stdin.isatty():
            templates = _we.list_workflow_templates()
            chosen = choose_record(
                templates,
                title="Select Workflow Template",
                label=lambda item: item,
                description=lambda item: "Workflow template",
                full_screen=False,
            )
            wf_name = chosen or ""
        if wf_name and not wf_desc and sys.stdin.isatty():
            try:
                wf_desc = input("Workflow objective: ").strip()
            except (EOFError, KeyboardInterrupt):
                wf_desc = ""
        if not wf_name or not wf_desc:
            console.print("[dim]Workflow creation cancelled. You can also use "
                          "/workflow start [--replace] <name> \"<description>\".[/dim]")
        else:
            existing = _we.get_active_workflow()
            wf = _we.start_workflow(wf_name, wf_desc, replace=replace)
            if wf is None:
                if wf_name not in _we.list_workflow_templates():
                    console.print(f"[red]Unknown workflow: {wf_name}[/red]")
                    console.print(f"[dim]Available: {', '.join(_we.list_workflow_templates())}[/dim]")
                elif existing and not existing.completed:
                    console.print(
                        f"[yellow]Workflow '{existing.name}' is already active. "
                        "End it first or pass --replace.[/yellow]")
            else:
                current = wf.current
                cur_name = current.name if current else "?"
                console.print(Panel(
                    f"[bold]Workflow Started: [green]{wf_name}[/green][/bold]\n\n"
                    f"Task: {wf_desc}\n"
                    f"Phases: {len(wf.phases)}\n"
                    f"Progress: {wf.progress_str}\n\n"
                    f"[dim]Current phase: [bold]{cur_name}[/bold][/dim]",
                    title="Workflow",
                    border_style="green",
                ))
    elif sub == "status":
        wf = _we.get_active_workflow()
        if wf is None:
            console.print("[dim]No active workflow.[/dim]")
            console.print(f"[dim]Start one with: /workflow start <name> \"description\"[/dim]")
            console.print(f"[dim]Available: {', '.join(_we.list_workflow_templates())}[/dim]")
        else:
            current = wf.current
            phase_info = ""
            if current:
                phase_info = f"\nCurrent: [bold]{current.name}[/bold] — {current.description}"
                if current.allowed_tools:
                    phase_info += f"\nAllowed tools: {', '.join(current.allowed_tools)}"
                if current.spawn_agents:
                    phase_info += f"\nAuto-spawn roles: {', '.join(current.spawn_agents)}"
                if current.exit_condition == "user_confirm":
                    phase_info += "\nNext transition: explicit /workflow approve required"
            history = []
            for phase in wf.phases[:wf.current_phase]:
                summary = wf.phase_states.get(phase.name, {}).get("summary", "")
                history.append(f"  {phase.name}: {summary or '(completed)'}")
            history_text = ("\n\nCompleted phases:\n" + "\n".join(history)) if history else ""
            console.print(Panel(
                f"[bold]{wf.name}[/bold] — {wf.description}\n\n"
                f"Progress: {wf.progress_str}{phase_info}{history_text}",
                title="Active Workflow",
                border_style="cyan",
            ))
    elif sub == "advance":
        _, summary_raw = _raw_tail_after_word(raw_args)
        summary = _decode_text_arg(summary_raw)
        try:
            new_phase = _we.advance_phase(summary, force=True)
        except _we.WorkflowTransitionError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
        else:
            wf = _we.get_active_workflow()
            if new_phase is None:
                if wf and wf.completed:
                    console.print(f"[green]Workflow '{wf.name}' completed![/green]")
                else:
                    console.print("[yellow]No active workflow or already completed.[/yellow]")
            else:
                console.print(f"[green]Advanced to phase: [bold]{new_phase.name}[/bold] — {new_phase.description}[/green]")
    elif sub == "approve":
        _, summary_raw = _raw_tail_after_word(raw_args)
        summary = _decode_text_arg(summary_raw)
        wf = _we.get_active_workflow()
        if wf is None or wf.completed:
            console.print("[yellow]No active workflow to approve.[/yellow]")
        else:
            try:
                new_phase = _we.advance_phase(summary, user_confirmed=True)
            except _we.WorkflowTransitionError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
            else:
                if new_phase is None:
                    console.print(f"[green]Workflow '{wf.name}' completed.[/green]")
                else:
                    console.print(
                        f"[green]Approved; advanced to [bold]{new_phase.name}[/bold] "
                        f"— {new_phase.description}[/green]")
    elif sub == "end":
        _, summary_raw = _raw_tail_after_word(raw_args)
        summary = _decode_text_arg(summary_raw)
        wf = _we.get_active_workflow()
        if wf:
            _we.end_workflow(summary)
            console.print(f"[dim]Workflow '{wf.name}' ended.[/dim]")
        else:
            console.print("[dim]No active workflow.[/dim]")
    elif sub == "list":
        templates = _we.list_workflow_templates()
        console.print("[bold]Available workflow templates:[/bold]")
        for t in templates:
            console.print(f"  [cyan]{t}[/cyan]")
    else:
        console.print("Usage:\n"
                      "  [bold]/workflow start <name> \"<desc>\"[/bold] — Start a workflow\n"
                      "  [bold]/workflow status[/bold]                — Show current workflow\n"
                      "  [bold]/workflow advance [summary][/bold]    — Advance to next phase\n"
                      "  [bold]/workflow approve [summary][/bold]    — Confirm a gated phase\n"
                      "  [bold]/workflow end [summary][/bold]        — End workflow\n"
                      "  [bold]/workflow list[/bold]                  — List available workflows")



def _cmd_debug(parts: list) -> None:
    if len(parts) > 1:
        sub = parts[1].lower()
        if sub == "clear":
            clear_debug_logs()
            console.print("[green]Debug log cleared.[/green]")
        elif len(parts) >= 3 and sub.isdigit():
            # /debug <N> <filename> — save latest N entries to file
            try:
                n = int(sub)
                if n <= 0:
                    raise ValueError("N must be greater than 0")
                filename = parts[2]
                raw_export = "--raw" in parts[3:]
                entries = get_debug_logs()[:n]
                if not entries:
                    console.print("[yellow]No debug entries to save.[/yellow]")
                else:
                    filepath = Path(filename).expanduser()
                    lines = []
                    for i, e in enumerate(entries, 1):
                        lines.append(f"{'='*60}")
                        lines.append(f"Entry #{i}  Loop #{e.loop}  {e.timestamp}  Path: {e.current_path}")
                        lines.append(f"{'='*60}")
                        if e.user_input:
                            lines.append(f"\n[User Input]\n{e.user_input[:3000]}")
                        if e.request_body:
                            lines.append(f"\n[Context Sizes] terminal={e.context_sizes.get('terminal', 0)} conversation={e.context_sizes.get('conversation', 0)} memory={e.context_sizes.get('memory', 0)} terminals={e.context_sizes.get('terminals', 0)} prompt={e.context_sizes.get('prompt', 0)}")
                            lines.append(f"\n[Prompt Preview]\n{e.request_body.get('promptPreview', '')[:500]}")
                        if e.reply:
                            lines.append(f"\n[AI Reply]\n{e.reply[:1500]}")
                        if e.command:
                            lines.append(f"\n[Command]\n{e.command}")
                        lines.append(f"\n[Done] {e.done}")
                        if e.error:
                            lines.append("\n[Error] true")
                        if e.billing:
                            cost = e.billing.get("costCents") or 0
                            balance = e.billing.get("balanceCents") or 0
                            lines.append(f"\n[Billing] ${cost / 100:.2f} (balance ${balance / 100:.2f})")
                        if e.exec_command:
                            lines.append(f"\n[Executed] {e.exec_command}")
                            lines.append(f"[Return Code] {e.exec_returncode}")
                            if e.exec_stdout:
                                lines.append(f"\n[Stdout]\n{e.exec_stdout}")
                            if e.exec_stderr:
                                lines.append(f"\n[Stderr]\n{e.exec_stderr}")
                        if e.response_raw:
                            try:
                                raw_json = json.dumps(e.response_raw, ensure_ascii=False, indent=2)
                            except Exception:
                                raw_json = str(e.response_raw)
                            lines.append(f"\n[Raw Response]\n{raw_json[:2000]}")
                        lines.append("")
                    try:
                        filepath.parent.mkdir(parents=True, exist_ok=True)
                        export_text = '\n'.join(lines)
                        if not raw_export:
                            export_text = _redact_sensitive_text(export_text)
                        filepath.write_text(export_text, encoding='utf-8')
                    except OSError as exc:
                        console.print(f"[red]Could not save debug entries to {filepath}: {exc}[/red]")
                    else:
                        console.print(
                            f"[green]Saved {len(entries)} debug entr"
                            f"{'y' if len(entries) == 1 else 'ies'} to {filepath.absolute()}[/green]")
                        if raw_export:
                            console.print("[yellow]Raw export may contain credentials or private content.[/yellow]")
                        else:
                            console.print("[dim]Common credential fields were redacted. Use --raw only when necessary.[/dim]")
            except (ValueError, IndexError) as exc:
                console.print(f"[red]Error: {exc}[/red]")
                console.print("Usage: [bold]/debug <N> <filename>[/bold]")
        else:
            try:
                idx = int(sub) - 1
                show_debug_detail(idx)
            except (ValueError, IndexError):
                console.print(f"[red]Invalid entry number: {sub}[/red]")
                console.print("Use [bold]/debug[/bold] to browse entries. [bold]/debug <N> <file>[/bold] to save to file.")
    else:
        show_debug_browser_interactive()



def _cmd_detail(parts: list) -> None:
    # Toggle full vs simplified progress rendering. Off (default) folds
    # successful read/search/list output into grouped one-line summaries;
    # errors, writes and command status remain visible. On expands command
    # output and file diffs for the current run.
    if len(parts) == 1:
        _cur = bool(get_runtime_config("detail"))
        if sys.stdin.isatty():
            options = [
                ("Off", "Compact one-line tool progress"),
                ("On", "Full command output and detailed panels"),
            ]
            chosen = select_dialog(
                options,
                title="Tool Output Detail",
                full_screen=False,
                selected_index=1 if _cur else 0,
                hint=f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ select  Esc/q cancel",
                letter_shortcuts=True,
            )
            if chosen is not None:
                enabled = chosen == options[1]
                set_runtime_config("detail", enabled)
                terminal_preferences.set_ui_preference("detail", enabled)
                console.print(
                    f"[green]Detail mode {'on' if enabled else 'off'}.[/green]")
        else:
            console.print(
                f"[dim]Detail mode is [bold]{'on' if _cur else 'off'}[/bold]. "
                "Compact mode folds read/search/list output; use /detail on "
                "to expand tool output.[/dim]")
    else:
        _sub = parts[1].lower()
        if _sub in ("on", "off"):
            enabled = _sub == "on"
            set_runtime_config("detail", enabled)
            terminal_preferences.set_ui_preference("detail", enabled)
            console.print(
                f"[green]Detail mode {_sub}. "
                f"{'Compact mode folds read/search/list output.' if not enabled else 'Command output and file diffs are expanded.'}[/green]")
        else:
            console.print("[red]Usage: /detail [on|off][/red]")


def _cmd_stream(parts: list) -> None:
    """Configure a bounded, fixed-height streaming prose preview."""
    if len(parts) == 1:
        current = str(get_runtime_config("stream_preview") or "one")
        console.print(
            f"[muted]Streaming preview is [bold]{escape(current)}[/bold]. "
            "Use /stream off|one|detail.[/muted]")
        return
    mode = parts[1].strip().lower()
    if len(parts) != 2 or mode not in {"off", "one", "detail"}:
        console.print("[error]Usage: /stream [off|one|detail][/error]")
        return
    set_runtime_config("stream_preview", mode)
    terminal_preferences.set_ui_preference("stream_preview", mode)
    description = {
        "off": "status only",
        "one": "one fixed preview row",
        "detail": "three fixed preview rows",
    }[mode]
    console.print(f"[success]Streaming preview: {description}.[/success]")


def _cmd_theme(parts: list) -> None:
    if len(parts) == 1:
        console.print(
            f"[muted]Theme is [bold]{escape(str(get_runtime_config('theme')))}[/bold]. "
            "Use /theme dark|light|mono.[/muted]")
        return
    name = parts[1].strip().lower()
    if len(parts) != 2 or name not in {"dark", "light", "mono"}:
        console.print("[error]Usage: /theme [dark|light|mono][/error]")
        return
    set_runtime_config("theme", name)
    terminal_preferences.set_ui_preference("theme", name)
    _apply_ui_theme(name)
    global _prompt_session
    if _prompt_session is not None:
        _prompt_session.style = _build_prompt_style()
    console.print(f"[success]Theme changed to {escape(name)}.[/success]")


def _cmd_why(parts: list) -> None:
    """Explain one recent tool failure without opening the full debug browser."""
    selector = " ".join(parts[1:]).strip()
    failures = get_recent_tool_failures()
    if selector and not selector.isdigit():
        needle = selector.lower()
        failures = [row for row in failures if any(
            needle in str(row.get(key, "")).lower()
            for key in ("tool", "display_name", "terminal", "agent_id")
        )]
    if not failures:
        console.print("[muted]No matching tool failure is available in this process.[/muted]")
        return
    index = int(selector) - 1 if selector.isdigit() else 0
    if index < 0 or index >= len(failures):
        console.print(f"[error]No failure #{index + 1}. There are {len(failures)} matching failures.[/error]")
        return
    row = failures[index]
    command = _redact_sensitive_text(str(row.get("command") or "(none)"))
    error = _redact_sensitive_text(str(row.get("error") or "Tool failed"))
    output = _redact_sensitive_text(str(row.get("output_tail") or "")).strip()
    recovery = str(row.get("recovery") or "none")
    elapsed = float(row.get("elapsed_seconds") or 0.0)
    lines = [
        f"[muted]Tool[/muted]      {escape(str(row.get('display_name') or row.get('tool') or 'tool'))}",
        f"[muted]Scope[/muted]     {escape(str(row.get('agent_id') or 'primary'))}@{escape(str(row.get('terminal') or 'temporary'))}",
        f"[muted]Elapsed[/muted]   {elapsed:.1f}s",
        f"[muted]Recovery[/muted]  {escape(recovery)}",
        f"[muted]Command[/muted]   {escape(command)}",
        f"[muted]Error[/muted]     {escape(error)}",
    ]
    if output and output != error:
        tail_lines = output.splitlines()[-6:]
        lines.append("[muted]Output tail[/muted]")
        lines.extend(f"  {escape(line)}" for line in tail_lines)
    console.print(Panel("\n".join(lines), title="Why the last tool failed",
                        border_style="error", expand=False))



def _cmd_station(parts: list, agent_registry: AgentRegistry, session: dict) -> bool:
    station_args = [_normalize_slash_arg(item) for item in parts[1:]]
    task = ""
    task_marker = next(
        (i for i, item in enumerate(station_args)
         if item in {"--task", "--"}), None)
    if task_marker is not None:
        task = " ".join(station_args[task_marker + 1:]).strip()
        station_args = station_args[:task_marker]
        if not task:
            console.print(
                "[yellow]Usage: /station <agent-id> [terminal] "
                "--task <work>[/yellow]")
            return False
    if not station_args or len(station_args) > 2:
        console.print(
            "[yellow]Usage: /station <agent-id> [terminal] "
            "[--task <work>][/yellow]")
        return False

    agent_id_arg = station_args[0]
    target_agent = get_agent(agent_id_arg)
    if target_agent is None:
        console.print(
            f"[red]Agent '{agent_id_arg}' not found. Use /hire to create one.[/red]")
        return False
    manager = get_current_agent()
    manager_terminal = agent_deployment_terminal(manager) or "term0"
    explicit_terminal = len(station_args) == 2
    existing_deployment = agent_deployment_terminal(target_agent)
    if task and not explicit_terminal and not existing_deployment:
        assignment_events = (
            (lambda events: agent_registry._push_events(events))
            if agent_registry and agent_registry.agent_id else None
        )
        ok, message, assignment = start_agent_assignment(
            target_agent.id, task, get_loop_deps(),
            session=session, events_cb=assignment_events)
        style = "green" if ok else "red"
        console.print(f"[{style}]{message}[/{style}]")
        if ok and assignment:
            console.print(
                f"[dim]Task runs in a private temporary terminal. Inspect with "
                f"/agents {target_agent.id}; send updates with /tell "
                f"{target_agent.id} <message>.[/dim]")
        return False
    if not explicit_terminal and not existing_deployment:
        console.print(
            "[yellow]An undeployed agent needs an explicit target terminal, or "
            "use --task to run it in a private temporary terminal.[/yellow]")
        return False
    name = station_args[1] if explicit_terminal else existing_deployment
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        console.print("[red]Invalid terminal name.[/red]")
        return False
    if name.lower() in ("current", "here", "term0"):
        name = "term0"

    # Special case: deploy to the parent REPL (term0). Term0 should
    # already have a real persistent bash session created at startup.
    # If it doesn't (crashed or not created), recreate it.
    if name == "term0":
        term0_info = get_terminal("term0")
        if (term0_info is None
                or term0_info.session is None
                or not term0_info.session.is_alive()):
            if term0_info is not None:
                unregister_terminal("term0")
                if get_agent(target_agent.id) is None:
                    console.print(
                        f"[red]Agent '{target_agent.id}' ended with its previous "
                        "deployment terminal.[/red]")
                    return False
            try:
                _term0 = InteractiveSession(
                    DEFAULT_SHELL, timeout=0, stream_output=False,
                    persistent=True)
                _term0.start()
                time.sleep(0.08)
                if _term0.is_alive():
                    _term0.read_output(timeout=0.1)
                if not _term0.is_alive():
                    console.print("[red]Could not start term0.[/red]")
                    return False
                register_terminal(_term0, DEFAULT_SHELL, 0, name="term0")
            except Exception as exc:
                console.print(f"[red]Could not start term0: {exc}[/red]")
                return False
        if not station_agent(target_agent.id, "term0"):
            console.print(
                f"[red]Could not deploy agent '{target_agent.id}' to term0. "
                "Finish or cancel its active assignment first.[/red]")
            return False
        console.print(f"[green]Stationed [bold]{target_agent.id}[/bold] in this REPL (term0)[/green]")
        if task:
            assignment_events = (
                (lambda events: agent_registry._push_events(events))
                if agent_registry and agent_registry.agent_id else None
            )
            ok, message, assignment = start_agent_assignment(
                target_agent.id, task, get_loop_deps(),
                session=session, events_cb=assignment_events)
            style = "green" if ok else "red"
            console.print(f"[{style}]{message}[/{style}]")
        return False

    # Sub-terminal path: inspect existing terminal
    existing = get_terminal(name)
    if existing and existing.session and existing.session.is_alive():
        # Re-use the existing lifecycle container. shell.exec remains an
        # independent synchronous subprocess and never shares this PTY.
        if not station_agent(target_agent.id, name):
            console.print(
                f"[red]Could not deploy agent '{target_agent.id}' to '{name}'. "
                "Finish or cancel its active assignment first.[/red]")
            return False
        console.print(f"[green]Stationed [bold]{target_agent.id}[/bold] → terminal [bold]{name}[/bold] (existing)[/green]")
    else:
        if existing:
            unregister_terminal(name)
            if get_agent(target_agent.id) is None:
                console.print(
                    f"[red]Agent '{target_agent.id}' ended with its previous "
                    "deployment terminal.[/red]")
                return False

        # A station is a work place, not another CLI identity.  The employee
        # loop stays in-process; POSIX uses a dedicated PTY.
        shell_cmd = DEFAULT_SHELL
        sub = SubTerminalSession(shell_cmd)
        sub.start()
        time.sleep(0.1)
        if not sub.is_alive():
            console.print(f"[red]Could not start terminal '{name}'.[/red]")
            return False
        sub.read_output(timeout=0.1)
        try:
            register_terminal(
                sub, shell_cmd, 0, name=name,
                parent_terminal=manager_terminal)
        except Exception as exc:
            sub.close()
            console.print(f"[red]Could not register terminal '{name}': {exc}[/red]")
            return False
        if not station_agent(target_agent.id, name):
            unregister_terminal(name)
            console.print(
                f"[red]Could not deploy agent '{target_agent.id}' to '{name}'.[/red]")
            return False
        console.print(
            f"[green]Stationed [bold]{target_agent.id}[/bold] → "
            f"terminal [bold]{name}[/bold] "
            f"(shell)[/green]")

    if task:
        assignment_events = (
            (lambda events: agent_registry._push_events(events))
            if agent_registry and agent_registry.agent_id else None
        )
        ok, message, assignment = start_agent_assignment(
            target_agent.id, task, get_loop_deps(),
            session=session, events_cb=assignment_events)
        style = "green" if ok else "red"
        console.print(f"[{style}]{message}[/{style}]")
        if ok and assignment:
            console.print(
                f"[dim]Task: {assignment.task}\n"
                f"Inspect with /agents {target_agent.id}; send updates with "
                f"/tell {target_agent.id} <message>.[/dim]")
    else:
        console.print(
            f"[dim]Assign work with /station {target_agent.id} {name} "
            "--task \"...\"[/dim]")

    return False


def _cmd_terminate(parts: list) -> None:
    name = parts[1] if len(parts) >= 2 else ""
    if not name and sys.stdin.isatty():
        terminals = [item for item in get_all_terminals()
                     if item.name != "term0"]
        chosen = choose_record(
            terminals,
            title="Terminate Terminal",
            label=lambda item: item.name,
            description=lambda item: (
                f"{'alive' if item.session and item.session.is_alive() else 'stopped'}"
                f" {symbols.BULLET} {item.command[:80]}"),
            search=True,
        )
        name = chosen.name if chosen else ""
    if not name:
        console.print("[dim]Terminal selection cancelled.[/dim]")
    elif name == "term0":
        console.print("[red]term0 is owned by this CLI; use /exit to close it.[/red]")
    else:
        term = get_terminal(name)
        terminated_agents = list(term.stationed_agent_ids) if term else []
        if unregister_terminal(name):
            if terminated_agents:
                console.print(
                    f"[green]Terminated [bold]{name}[/bold] and its child "
                    f"resources; agents ended: {', '.join(terminated_agents)}[/green]")
            else:
                console.print(
                    f"[green]Terminated [bold]{name}[/bold] and its child resources[/green]")
        else:
            console.print(f"[red]Terminal '{name}' not found.[/red]")



def _cmd_send(raw_args: str) -> bool:
    name, send_raw = _raw_tail_after_word(raw_args)
    if not name and sys.stdin.isatty():
        terminals = [
            item for item in get_all_terminals()
            if item.session is not None and item.session.is_alive()
        ]
        chosen = choose_record(
            terminals,
            title="Send Command to Terminal",
            label=lambda item: item.name,
            description=lambda item: item.command[:100],
            search=True,
        )
        name = chosen.name if chosen else ""
        if name:
            try:
                send_raw = input("Command: ").strip()
            except (EOFError, KeyboardInterrupt):
                name = ""
    wait_seconds = 0.8
    if send_raw.startswith("--wait"):
        match = re.match(r"--wait(?:\s+([^\s]+))?\s+(.*)$", send_raw, re.DOTALL)
        if match is None:
            console.print("[yellow]Usage: /send <name> [--wait <seconds>] <command>[/yellow]")
            return False
        try:
            wait_seconds = max(0.0, min(float(match.group(1) or "0.8"), 30.0))
        except ValueError:
            console.print("[red]--wait expects a number between 0 and 30 seconds.[/red]")
            return False
        send_raw = match.group(2)
    cmd = send_raw
    if not name or not cmd:
        console.print("[yellow]Usage: /send <name> <command>[/yellow]")
    else:
        term = get_terminal(name)
        if term is None:
            console.print(f"[red]Terminal '{name}' not found.[/red]")
        elif term.session is None or not term.session.is_alive():
            console.print(f"[yellow]Terminal '{name}' has no active session.[/yellow]")
        else:
            allowed, denial = authorize_direct_command(cmd, os.getcwd())
            if not allowed:
                console.print(f"[red]{denial}[/red]")
                return False
            command_lock = getattr(term.session, "command_lock", None)
            with (command_lock if command_lock is not None else nullcontext()):
                old_len = len(term.session.full_output)
                term.session.send_keys(cmd + "\n")
                console.print(f"[dim]Sent to [bold]{name}[/bold]: {cmd[:80]}[/dim]")
                if wait_seconds >= 0.3:
                    console.print(
                        f"[dim]Waiting up to {wait_seconds:g}s for new output…[/dim]")
                deadline = time.time() + wait_seconds
                while time.time() < deadline:
                    remaining = deadline - time.time()
                    term.session.read_output(timeout=min(0.2, max(0.01, remaining)))
                output = term.session.full_output[old_len:]
            if output.strip():
                preview = output[-2000:]
                suffix = "" if len(output) <= 2000 else f"\n[dim]… {len(output) - 2000} earlier new chars omitted[/dim]"
                console.print(Panel(preview + suffix, title=f"{name} new output"))
            elif wait_seconds == 0:
                console.print("[dim]Sent asynchronously; use /term to inspect later output.[/dim]")
            else:
                console.print("[dim]No new output arrived within the wait window.[/dim]")

    return False


def _cmd_hire(parts: list, session: dict) -> bool:
    import agent_persistence
    hire_name, employee_profile, hire_options = _parse_hire_profile(parts[1:])
    if hire_name and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", hire_name):
        console.print(
            "[red]Employee names may contain only letters, numbers, dot, "
            "underscore, and hyphen (max 64).[/red]")
        return False
    if hire_name and get_agent(hire_name) is not None:
        console.print(f"[red]Agent '{hire_name}' already exists.[/red]")
        return False
    owner = get_current_agent()
    home_terminal = agent_scope_terminal(owner) or "term0"
    requested_terminal = str(hire_options.get("terminal") or "").strip()
    if requested_terminal.lower() in {"current", "here"}:
        requested_terminal = home_terminal
    if requested_terminal == home_terminal:
        console.print(
            "[red]A newly hired agent cannot be deployed directly into the "
            "current terminal. Omit --terminal or choose a different terminal.[/red]")
        return False

    base_model = str(hire_options.get("model") or "").strip()
    base_provider = ""
    if base_model or hire_options.get("choose_model"):
        try:
            with _safe_status(
                    f"[dim]Fetching available models… {symbols.BULLET} Esc/Ctrl+C cancel[/dim]"):
                models, _endpoint = run_cancellable_blocking(
                    lambda cancel: fetch_available_models(
                        session, cancel_event=cancel))
        except BlockingOperationCancelled:
            console.print("[dim]Hiring cancelled.[/dim]")
            return False
        except Exception as exc:
            console.print(f"[red]Failed to fetch models: {exc}[/red]")
            return False
        if hire_options.get("choose_model"):
            if not sys.stdin.isatty():
                console.print(
                    "[red]Interactive model selection is unavailable; use "
                    "/hire <name> --model <model-id>.[/red]")
                return False
            selected = show_model_selector(models, "")
            if not selected:
                console.print("[dim]Hiring cancelled.[/dim]")
                return False
            base_model = str(selected.get("id") or "")
            base_provider = str(selected.get("provider") or "")
        else:
            matching = [item for item in models if item.get("id") == base_model]
            if not matching:
                console.print(
                    f"[red]Model '{base_model}' is not in the backend's current "
                    "available model list.[/red]")
                return False
            base_provider = str(matching[0].get("provider") or "")
    try:
        agent_info = register_agent(
            name=hire_name, depth=1, role="pool",
            profile=employee_profile, replace_existing=False)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return False
    agent_info.state["_persisted_employee"] = True
    agent_info.home_terminal = home_terminal
    agent_info.parent_terminal = home_terminal
    agent_info.base_model = base_model
    agent_info.base_provider = base_provider
    if (not agent_info.profile.prompt.strip()
            and not agent_info.profile.specialist_role):
        agent_info.profile.prompt = (
            f"You are {agent_info.name}, a hired employee of this project. "
            f"You are registered under terminal {home_terminal}. Use only your "
            "assigned capabilities, "
            "work only on explicit assignments delivered through your "
            "deployment terminal or agent messaging, and report concrete "
            "results to the manager."
        )
    if requested_terminal and not station_agent(agent_info.id, requested_terminal):
        unregister_agent(agent_info.id, delete_persisted=True)
        console.print(
            f"[red]Could not deploy agent '{agent_info.id}' to terminal "
            f"'{requested_terminal}'; it must be live and unoccupied.[/red]")
        return False
    agent_persistence.save_agent_state(agent_info)
    deployment_text = requested_terminal or "private temporary terminal on assignment"
    console.print(Panel(
        _employee_capability_text(agent_info),
        title=f"Hired employee: {agent_info.name} → {deployment_text}",
        border_style="green",
    ))
    if requested_terminal:
        console.print(
            f"[dim]Agent is deployed to {requested_terminal}. Start work with "
            f"/station {agent_info.id} --task \"...\"[/dim]")
    else:
        console.print(
            f"[dim]Agent is undeployed. Start temporary isolated work with "
            f"/station {agent_info.id} --task \"...\", or deploy explicitly "
            f"with /station {agent_info.id} <terminal>.[/dim]")

    return False


def _resolve_agent_id_or_name(token: str):
    """Resolve an Agent by exact ID or case-insensitive name match.

    Returns the matching AgentInfo, or None.  Raises ValueError on
    ambiguous name matches so the caller can surface a clear message.
    """
    if not token:
        return None
    agents = get_all_agents()
    exact = next((a for a in agents if a.id == token), None)
    if exact is not None:
        return exact
    token_lower = token.lower()
    name_matches = [a for a in agents
                    if (a.name or "").lower() == token_lower]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        raise ValueError(
            f"Multiple agents match name '{token}': "
            + ", ".join(a.id for a in name_matches))
    return None


def _bind_current_agent_runtime(prepared_state: dict, chat_history: list,
                                interactive_session, agent_state: dict) -> dict:
    """Persist one REPL turn on whichever Agent ``/agent`` selected.

    Primary keeps its long-lived state dict identity because Agents Mode and
    Helpwo share that exact object. Other Agents receive the prepared state as
    their new authoritative state. Both paths retain history and PTY/session
    ownership so switching away and back cannot lose conversation progress.
    """
    runtime_agent = get_current_agent()
    if runtime_agent is None:
        return prepared_state
    if runtime_agent.role == "primary":
        agent_state.clear()
        agent_state.update(prepared_state)
        bound_state = agent_state
    else:
        bound_state = prepared_state
    runtime_agent.state = bound_state
    runtime_agent.chat_history = chat_history
    runtime_agent.runtime_session = interactive_session
    return bound_state


def _cmd_agent(parts: list, session: dict, interactive_session) -> bool:
    """``/agent [agent-id-or-name]`` - switch the REPL's current Agent focus.

    No argument lists the current Agent and every other switchable Agent.
    With an argument, switches the registry's current-agent pointer and
    rebinds the REPL's local ``agent_state`` / ``chat_history`` /
    ``interactive_session`` to the target Agent's existing objects - no
    deployment is reissued and no terminal ownership changes.
    """
    args = parts[1:] if len(parts) > 1 else []
    current = get_current_agent()

    if not args:
        agents = get_all_agents()
        if not agents:
            console.print("[dim]No agents registered.[/dim]")
            return False
        if current is not None:
            cur_label = (f"{current.name} ({current.id})"
                         if current.name and current.name != current.id
                         else current.id)
            console.print(f"[accent]Current agent:[/accent] [bold]{cur_label}[/bold]")
        else:
            console.print("[dim]No current agent.[/dim]")
        console.print("[accent]Switchable agents:[/accent]")
        any_other = False
        for a in agents:
            if current is not None and a.id == current.id:
                continue
            any_other = True
            label = (f"{a.name} ({a.id})"
                     if a.name and a.name != a.id
                     else a.id)
            role = getattr(a, "role", "") or ""
            term = (getattr(a, "stationed_terminal", None)
                    or getattr(a, "deployment_terminal", None)
                    or "")
            suffix = f" [dim]role={role}" + (f", term={term}" if term else "") + "[/dim]"
            console.print(f"  [cyan]/agent {a.id}[/cyan]  {label}{suffix}")
        if not any_other:
            console.print("  [dim](no other agents)[/dim]")
        console.print(
            "[dim]Use /agent <id-or-name> to switch. Use /agents for the "
            "full-screen view.[/dim]")
        return False

    target = args[0]
    try:
        agent = _resolve_agent_id_or_name(target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return False
    if agent is None:
        console.print(f"[red]No agent matches '{target}'.[/red]")
        agents = get_all_agents()
        if agents:
            console.print(
                "[dim]Available: "
                + ", ".join(a.id for a in agents) + "[/dim]")
        return False
    if current is not None and agent.id == current.id:
        label = (f"{agent.name} ({agent.id})"
                 if agent.name and agent.name != agent.id
                 else agent.id)
        console.print(f"[dim]Already focused on {label}.[/dim]")
        return False
    if not switch_to_agent(agent.id):
        console.print(f"[red]Could not switch to agent '{agent.id}'.[/red]")
        return False

    # Rebind the REPL's runtime locals via handle_meta_command attributes.
    # The main loop picks these up right after handle_meta_command() returns.
    handle_meta_command._last_agent_state = agent.state
    handle_meta_command._last_chat_history = agent.chat_history
    handle_meta_command._last_existing_session = agent.runtime_session
    handle_meta_command._agent_switch_performed = True
    label = (f"{agent.name} ({agent.id})"
             if agent.name and agent.name != agent.id
             else agent.id)
    console.print(f"[green]Switched to agent {label}.[/green]")
    role = getattr(agent, "role", "") or ""
    term = (getattr(agent, "stationed_terminal", None)
            or getattr(agent, "deployment_terminal", None)
            or "")
    if term:
        console.print(
            f"[dim]Terminal ownership unchanged. This agent is stationed at "
            f"'{term}' (role={role}).[/dim]")
    else:
        console.print(
            f"[dim]Terminal ownership unchanged. This agent is undeployed "
            f"(role={role}); direct commands still run in term0.[/dim]")
    return False


def _cmd_agents_plain(parts: list) -> None:
    if len(parts) == 1:
        agents = get_all_agents()
        ui_snapshot = (_terminal_agents.snapshot()
                       if _terminal_agents.configured else {})
        ui_rows = {row["id"]: row
                   for row in ui_snapshot.get("agents", [])}
        input_target_id = ui_snapshot.get("input_target_id", "")
        if not agents:
            console.print("[dim]No agents.[/dim]")
        else:
            # Categorize by role
            buckets = {"primary": [], "pool": [], "deployed": [], "subagent": [], "other": []}
            for a in agents:
                role = getattr(a, "role", "pool")
                if role in buckets:
                    buckets[role].append(a)
                else:
                    buckets["other"].append(a)

            def _render(a):
                markers = []
                if a.id == input_target_id:
                    markers.append("[bold green]← input[/bold green]")
                row = ui_rows.get(a.id, {})
                if row.get("unseen_output_events"):
                    markers.append(
                        f"[muted]{row['unseen_output_events']} new[/muted]")
                marker = (" " + "  ".join(markers)) if markers else ""
                phase = row.get("phase") or a.status
                status_str = f" [dim]({phase})[/dim]" if phase != "idle" else ""
                inbox_str = f" [dim yellow]inbox={a.inbox.qsize()}[/dim yellow]" if a.inbox.qsize() else ""
                name_part = f" {a.name}" if a.name and a.name != a.id else ""
                return marker, status_str, inbox_str, name_part

            if buckets["primary"]:
                console.print("[bold]── Primary ──[/bold]")
                for a in buckets["primary"]:
                    marker, st_s, inb, np = _render(a)
                    console.print(f"  [bold]{a.id}[/bold]{np}{st_s}{inb}{marker}")
            if buckets["pool"]:
                idle_count = sum(a.status in {"idle", "ready", "error"}
                                 for a in buckets["pool"])
                console.print(f"[bold]── Pool ({idle_count} available) ──[/bold]")
                for a in buckets["pool"]:
                    marker, st_s, inb, np = _render(a)
                    console.print(f"  [bold]{a.id}[/bold]{np}{st_s}{inb}{marker}")
            if buckets["deployed"]:
                console.print(f"[bold]── Deployed ({len(buckets['deployed'])}) ──[/bold]")
                for a in buckets["deployed"]:
                    marker, st_s, inb, np = _render(a)
                    home = agent_deployment_terminal(a) or "?"
                    parent_term = getattr(a, "parent_terminal", None) or "?"
                    console.print(f"  [bold]{a.id}[/bold]{np} → [cyan]{home}[/cyan] [dim](parent={parent_term})[/dim]{st_s}{inb}{marker}")
            if buckets["subagent"]:
                console.print(f"[bold]── Subagents ({len(buckets['subagent'])}) ──[/bold]")
                for a in buckets["subagent"]:
                    marker, st_s, inb, np = _render(a)
                    parent = a.parent_id or "?"
                    console.print(f"  [bold]{a.id}[/bold]{np} [dim](depth={a.depth}, parent={parent})[/dim]{st_s}{inb}{marker}")
            if buckets["other"]:
                console.print("[bold]── Other ──[/bold]")
                for a in buckets["other"]:
                    marker, st_s, inb, np = _render(a)
                    console.print(f"  [bold]{a.id}[/bold]{np}{st_s}{inb}{marker}")
    elif len(parts) == 2 and parts[1].lower() == "tree":
        console.print(build_agents_tree())
    elif len(parts) == 2:
        reference = parts[1]
        agent = get_agent(reference)
        if agent is None:
            matches = [item for item in get_all_agents()
                       if str(item.name or "").casefold()
                       == reference.casefold()]
            agent = matches[0] if len(matches) == 1 else None
        if agent is None:
            console.print(
                f"[red]Agent '{escape(reference)}' not found or ambiguous. "
                "Use /agents --plain to list IDs.[/red]")
            return
        console.print(Panel(
            _employee_capability_text(agent),
            title=f"Employee: {agent.name}", border_style="cyan"))
    else:
        console.print("[yellow]Usage: /agents [tree|agent-id|--plain][/yellow]")


def _submit_primary_runtime_task(agent, text: str, deps, session: dict,
                                 external_events_cb=None) -> tuple[bool, str]:
    """Submit work to the CLI-owned primary runtime.

    Agents Mode is deliberately absent from this lifecycle.  It supplies a
    prompt and a renderer-safe dependency view; this outer runtime owns the
    lease, worker, shared state/history/session, and completion events.
    """
    admitted, detail = begin_primary_run(agent.id)
    if not admitted:
        return False, detail
    with agent.assignment_lock:
        agent.chat_history.append({
            "role": "user", "content": text, "input_kind": "prompt"})
    terminal_name = agent_scope_terminal(agent)
    run_id = f"primary-{int(time.time() * 1000)}"
    agent_ui_events.hub.emit(
        "user_message", agent_id=agent.id, terminal_name=terminal_name,
        run_id=run_id, summary=text, detail=text, status="accepted")
    agent_ui_events.hub.emit(
        "agent_started", agent_id=agent.id, terminal_name=terminal_name,
        run_id=run_id, summary=text, status="running")

    def events_cb(events):
        agent_ui_events.hub.ingest(agent.id, events, terminal_name)
        if callable(external_events_cb):
            try:
                external_events_cb(events)
            except Exception:
                pass

    def worker():
        state_ref = agent.state
        history_ref = agent.chat_history
        try:
            result = run_agent_loop(
                deps, text, session, state_ref, history_ref,
                events_cb=events_cb,
                existing_session=agent.runtime_session,
                depth=agent.depth, agent_id=agent.id,
                interrupt_event=agent.abort_event,
                message_queue=agent.message_queue)
            returned = result.get("state") if isinstance(result, dict) else None
            if isinstance(returned, dict):
                state_ref.clear()
                state_ref.update(prepare_state_for_repl(returned))
                agent.state = state_ref
            agent.chat_history = history_ref
            if isinstance(result, dict) and "session" in result:
                agent.runtime_session = result.get("session")
            reply = str(
                (result or {}).get("msg") or state_ref.get("lastReply") or "")
            aborted = agent.abort_event.is_set()
            failed = (isinstance(result, dict)
                      and result.get("success", True) is False
                      and not aborted)
            error = ""
            if failed:
                reason = str(result.get("exit_reason") or "incomplete")
                error = f"Agent loop ended without completion: {reason}"
            finish_primary_run(
                agent.id, reply=reply, error=error, aborted=aborted)
            agent_ui_events.hub.emit(
                "agent_aborted" if aborted else
                "agent_error" if failed else "agent_done",
                agent_id=agent.id, terminal_name=terminal_name, run_id=run_id,
                summary=error if failed else reply or text,
                detail=error if failed else reply,
                status=("aborted" if aborted else
                        "error" if failed else "done"))
        except Exception as exc:
            aborted = agent.abort_event.is_set()
            error = f"{type(exc).__name__}: {exc}"
            finish_primary_run(agent.id, error=error, aborted=aborted)
            agent_ui_events.hub.emit(
                "agent_aborted" if aborted else "agent_error",
                agent_id=agent.id, terminal_name=terminal_name, run_id=run_id,
                summary=error, detail=error,
                status="aborted" if aborted else "error")

    agent.thread = threading.Thread(
        target=worker, daemon=True, name=f"primary-runtime-{agent.id}")
    try:
        agent.thread.start()
    except Exception as exc:
        error = f"Failed to start primary runtime: {exc}"
        finish_primary_run(agent.id, error=error)
        agent_ui_events.hub.emit(
            "agent_error", agent_id=agent.id, terminal_name=terminal_name,
            run_id=run_id, summary=error, detail=error, status="error")
        return False, error
    return True, f"Submitted to {agent.name or agent.id}"


def _attach_primary_runtime_view(agent, *, show_result: bool = True):
    """Mirror an already-running primary task in the outer CLI.

    This only waits on and renders the existing runtime worker.  It never
    acquires a lease or invokes the Agent loop, so leaving Agents Mode cannot
    create a second execution.
    """
    worker = getattr(agent, "thread", None)
    if worker is not None and worker.is_alive():
        _set_run_input_state("running")
        try:
            with _safe_status(
                    f"[#3fb950]Thinking… {symbols.BULLET} primary runtime[/#3fb950]",
                    spinner="dots"):
                while worker.is_alive():
                    worker.join(timeout=0.1)
        except KeyboardInterrupt:
            agent.abort_event.set()
            with _safe_status(
                    "[yellow]Interrupting primary runtime…[/yellow]",
                    spinner="dots"):
                while worker.is_alive():
                    worker.join(timeout=0.1)
        finally:
            _set_run_input_state("idle")

    if show_result:
        if agent.error:
            console.print(f"[red]{escape(agent.error)}[/red]")
        elif agent.last_reply:
            console.print(Markdown(agent.last_reply))
    return agent.runtime_session


# ── Agents view: the full-screen /agents UI is only a display + input
# router. It runs in a background thread; every submitted line is injected
# into the main REPL loop (the single executor) via _inject_input, exactly
# like a locally typed line or a Helpwo remote message.

_agents_view_state: dict = {"controller": None}


def _agents_view_is_active() -> bool:
    return _agents_view_state.get("controller") is not None


def _agents_view_controller():
    return _agents_view_state.get("controller")


def _enter_agents_view(controller) -> None:
    """Hand the physical terminal to the /agents view.

    The shared console keeps printing — into the mirror only — at the width
    of the Focus pane so mirrored lines wrap correctly inside the view.
    """
    _agents_view_state["controller"] = controller
    width = shutil.get_terminal_size(fallback=(100, 30)).columns
    # Mirror output wraps to the Focus pane: on wide screens the Agent rail
    # (28) + separator + padding sit beside it; narrow screens are full-width.
    pane = max(40, width - 31) if width >= 100 else max(30, width - 2)
    try:
        console.width = pane
    except Exception:
        pass
    repl_mirror.hub.set_owner("agents")


def _exit_agents_view() -> None:
    """Return the terminal to the plain CLI and replay missed output."""
    _agents_view_state["controller"] = None
    try:
        console.width = None
    except Exception:
        pass
    repl_mirror.hub.set_owner("cli")


def _agents_repl_submit(text: str) -> tuple[bool, str]:
    """Forward one /agents input line to the outer REPL as Agent dialogue.

    Non-command input in the /agents view means "talk to this Agent", so the
    injected line carries kind="dialogue" and the main loop routes it straight
    to the agent loop.
    """
    text = str(text or "").strip()
    if not text:
        return False, ""
    # Echo the accepted line into the mirror the way the prompt would.
    repl_mirror.hub.write(
        _mirror_target_agent_id(), f"\x1b[2m› {text}\x1b[0m\n")
    _inject_input(text, threading.Event(), kind="dialogue")
    return True, "Sent"


def _cmd_agents(parts: list, session: dict, agent_registry=None,
                existing_session=None):
    """Open Agents Mode; retain a plain snapshot for scripts and fallback."""
    args = list(parts[1:])
    plain = "--plain" in args or not sys.stdin.isatty()
    args = [item for item in args if item != "--plain"]
    if len(args) > 1:
        console.print("[yellow]Usage: /agents [tree|agent-id|--plain][/yellow]")
        return
    # `tree` is an output command, not an initial selection for Agents Mode.
    # Keep it deterministic in both interactive and non-interactive shells.
    if args and args[0].lower() == "tree":
        _cmd_agents_plain(["/agents", "tree"])
        return
    if plain:
        _cmd_agents_plain(["/agents", *args])
        return
    current = get_current_agent()
    if current is not None and current.role == "primary":
        repl_state = getattr(handle_meta_command, "_last_agent_state", None)
        repl_history = getattr(handle_meta_command, "_last_chat_history", None)
        if isinstance(repl_state, dict):
            current.state = repl_state
        if isinstance(repl_history, list):
            current.chat_history = repl_history
        if current.status not in {"queued", "running", "thinking", "waiting"}:
            current.runtime_session = existing_session
    terminal_name = agent_scope_terminal(current) if current else "term0"
    requested = args[0] if args else ""
    if requested:
        candidate = get_agent(requested)
        if candidate is None:
            matches = [agent for agent in get_all_agents()
                       if str(agent.name or "").casefold() == requested.casefold()]
            candidate = matches[0] if len(matches) == 1 else None
        if (candidate is None
                or agent_scope_terminal(candidate) != terminal_name):
            console.print(
                f"[red]Agent '{escape(requested)}' is not available in "
                f"{escape(terminal_name)}.[/red]")
            return
        from agent_loop import set_dialog_agent_for_terminal
        set_dialog_agent_for_terminal(terminal_name, candidate.id)
    try:
        import agents_mode
        if _agents_view_is_active():
            console.print("[yellow]/agents view is already open.[/yellow]")
            return existing_session
        external_cb = (
            (lambda events: agent_registry._push_events(events))
            if agent_registry is not None and agent_registry.agent_id else None)
        execution_block_reason = ""
        if (get_backend_profile().sends_laintas_credentials
                and not session.get("userId")):
            execution_block_reason = (
                "Not authenticated. Exit Agents Mode and run /login.")

        controller = agents_mode.AgentsModeController(
            terminal_name, get_loop_deps(), session,
            external_events_cb=external_cb,
            existing_session=existing_session,
            execution_block_reason=execution_block_reason,
            repl_submit_cb=_agents_repl_submit,
            mirror=repl_mirror.hub)

        # The view is only a display + router: it runs in its own thread
        # while this main thread returns to the REPL loop and executes
        # whatever the view injects — the exact same pipeline as typing at
        # the outer prompt.
        _enter_agents_view(controller)

        def _view():
            try:
                controller.run()
            except Exception as exc:
                console.print(
                    f"[red]Agents Mode failed: {type(exc).__name__}: "
                    f"{escape(str(exc))}[/red]")
                console.print("[dim]Falling back to /agents --plain.[/dim]")
                _cmd_agents_plain(["/agents", *args])
            finally:
                _exit_agents_view()

        threading.Thread(
            target=_view, daemon=True, name="agents-view").start()
        return existing_session
    except (KeyboardInterrupt, EOFError):
        return
    except Exception as exc:
        _exit_agents_view()
        console.print(
            f"[red]Agents Mode failed: {type(exc).__name__}: {exc}[/red]")
        console.print("[dim]Falling back to /agents --plain.[/dim]")
        _cmd_agents_plain(["/agents", *args])


def _cmd_focus(parts: list) -> None:
    """The shared-terminal focus UI was removed."""
    console.print(
        "[yellow]/focus is unavailable. Each terminal has one foreground Agent; "
        "use /station to run another Agent in its own terminal.[/yellow]")


def _cmd_spawn(raw_args: str, session: dict, agent_registry: AgentRegistry) -> None:
    if not raw_args:
        console.print("[yellow]Usage: /spawn [name:] <task...>[/yellow]")
    else:
        # Parse optional "name:" prefix
        rest = raw_args
        m = re.match(r'^(\S+):\s+(.+)$', rest)
        child_name = m.group(1) if m else None
        task = m.group(2) if m else rest
        parent = get_current_agent()
        if parent is None:
            console.print("[red]No current agent. /hire one first.[/red]")
        else:
            child_id = spawn_subagent(
                parent_id=parent.id, task=task, deps=get_loop_deps(),
                name=child_name, session=session,
                events_cb=(lambda evs: agent_registry._push_events(evs))
                            if agent_registry.agent_id else None,
            )
            if child_id is None:
                console.print(f"[red]Spawn failed (parent={parent.id})[/red]")
            else:
                console.print(f"[green]Spawned [bold]{child_id}[/bold][/green] "
                              f"[dim](parent={parent.id}, task={task[:60]})[/dim]")



def _cmd_tell(raw_args: str) -> None:
    target_id, message_raw = _raw_tail_after_word(raw_args)
    if not target_id and sys.stdin.isatty():
        chosen = choose_record(
            get_all_agents(),
            title="Send Message to Agent",
            label=lambda item: item.id,
            description=lambda item: (
                f"{item.status} {symbols.BULLET} {item.name} {symbols.BULLET} role={item.role}"),
            search=True,
        )
        target_id = chosen.id if chosen else ""
        if target_id:
            try:
                message_raw = input("Message: ").strip()
            except (EOFError, KeyboardInterrupt):
                target_id = ""
    if not target_id or not message_raw:
        console.print("[dim]Agent message cancelled.[/dim]")
    else:
        raw = message_raw.strip()
        decoded = _decode_text_arg(message_raw)
        for candidate in (raw, decoded):
            try:
                body = json.loads(candidate)
                if not isinstance(body, dict):
                    body = {"kind": "msg", "text": decoded}
                break
            except (ValueError, TypeError):
                body = None
        if body is None:
            raw = decoded
            body = {"kind": "msg", "text": raw}
        else:
            raw = decoded if raw != decoded and not raw.lstrip().startswith(("{", "[")) else raw
        body.setdefault("from", "user")
        if send_to_agent(target_id, body):
            console.print(f"[green]→ {target_id}:[/green] {raw[:120]}")
        else:
            console.print(f"[red]Agent '{target_id}' not found or inbox full.[/red]")



def _cmd_abort(parts: list) -> None:
    target_id = parts[1] if len(parts) >= 2 else ""
    if not target_id and sys.stdin.isatty():
        abortable = [
            item for item in get_all_agents()
            if item.status not in {"done", "aborted", "error"}
        ]
        chosen = choose_record(
            abortable,
            title="Abort Agent",
            label=lambda item: item.id,
            description=lambda item: (
                f"{item.status} {symbols.BULLET} {item.name} {symbols.BULLET} role={item.role}"),
            search=True,
        )
        target_id = chosen.id if chosen else ""
    if not target_id:
        console.print("[dim]Agent selection cancelled.[/dim]")
    else:
        if abort_agent(target_id):
            console.print(f"[yellow]Abort signaled to [bold]{target_id}[/bold][/yellow]")
        else:
            console.print(f"[red]Agent '{target_id}' not found.[/red]")



def _cmd_tools() -> None:
    registry = tools_mod.get_registry()
    groups = registry.list_by_source()
    if not groups:
        console.print("[dim]No tools registered.[/dim]")
    else:
        for src in sorted(groups):
            console.print(f"[bold]{src}[/bold]")
            for t in groups[src]:
                console.print(f"  [cyan]{t.name}[/cyan] — {t.description}")



def _safe_input_line(prompt: str = "") -> Optional[str]:
    """Read one line in cbreak mode with Esc-to-cancel support.

    Returns the line as a string, or None on Esc/Ctrl+C/EOF.
    This is a drop-in replacement for input() in cooked-mode contexts
    where the user should be able to press Esc to cancel.
    """
    if not sys.stdin.isatty():
        # Non-TTY: fall back to plain input() — Esc is not meaningful here.
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
    fd = sys.stdin.fileno()
    try:
        old_attr = termios.tcgetattr(fd)
    except (termios.error, OSError):
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
    try:
        tty.setcbreak(fd)
    except (termios.error, OSError):
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    buf: list[str] = []

    def _write(s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def _backspace(n: int = 1) -> None:
        for _ in range(n):
            if buf:
                c = buf.pop()
                width = 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
                sys.stdout.write('\b \b' * width)
        sys.stdout.flush()

    try:
        _write(prompt)
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except (select.error, ValueError, OSError):
                return None
            if not r:
                continue
            try:
                chunk = os.read(fd, 1)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return None
            if not chunk:
                return None  # EOF

            if chunk == b'\x1b':
                # Disambiguate bare Esc vs escape sequence
                try:
                    r2, _, _ = select.select([fd], [], [], 0.05)
                except (select.error, ValueError, OSError):
                    r2 = []
                if r2:
                    try:
                        os.read(fd, 32)  # drain the sequence
                    except OSError:
                        pass
                    continue
                # Bare Esc — cancel
                _write('\r\n')
                return None
            if chunk == b'\x03':  # Ctrl+C
                _write('^C\r\n')
                return None
            if chunk in (b'\r', b'\n'):
                _write('\r\n')
                return ''.join(buf)
            if chunk in (b'\x7f', b'\x08'):  # Backspace / Ctrl+H
                _backspace()
                continue
            if chunk == b'\x17':  # Ctrl+W — delete word
                while buf and buf[-1] == ' ':
                    buf.pop()
                while buf and buf[-1] != ' ':
                    _backspace()
                continue

            text = decoder.decode(chunk)
            if text:
                _write(text)
                buf.append(text)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        except (termios.error, OSError):
            pass


def _cmd_tool(raw_args: str, session: dict, agent_registry: AgentRegistry) -> None:
    tool_name, tool_params_raw = _raw_tail_after_word(raw_args)
    if not tool_name and sys.stdin.isatty():
        chosen = choose_record(
            tools_mod.get_registry().list(),
            title="Run Tool",
            label=lambda item: item.name,
            description=lambda item: (
                f"{item.source} {symbols.BULLET} {item.description[:100]}"),
            search=True,
        )
        tool_name = chosen.name if chosen else ""
        if tool_name:
            result = _safe_input_line("JSON parameters (Enter for {}): ")
            if result is None:
                tool_name = ""
            else:
                tool_params_raw = result.strip()
    if not tool_name:
        console.print("[dim]Tool selection cancelled.[/dim]")
    else:
        raw = tool_params_raw.strip()
        try:
            params = {}
            last_error = None
            if raw:
                params = None
                for candidate in _json_arg_candidates(raw):
                    try:
                        params = json.loads(candidate)
                        break
                    except json.JSONDecodeError as exc:
                        last_error = exc
                if params is None and last_error is not None:
                    raise last_error
            if not isinstance(params, dict):
                params = {"value": params}
        except (ValueError, TypeError):
            params = {"raw": raw}
        ctx = tools_mod.ToolCtx(
            deps=get_loop_deps(),
            agent_id=(get_current_agent().id if get_current_agent() else None),
            session=session,
            events_cb=(lambda evs: agent_registry._push_events(evs))
                      if agent_registry.agent_id else None,
            cwd=os.getcwd(),
        )
        result = tools_mod.get_registry().invoke(tool_name, params, ctx)
        try:
            console.print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        except (TypeError, ValueError):
            console.print(repr(result))



def _cmd_skill(parts: list) -> bool:
    # No subcommand → open the interactive manager (same style as /term).
    sub = (parts[1].lower() if len(parts) > 1 else "manager")
    if sub == "manager":
        show_skill_manager()
    elif sub == "list":
        metas = skills_mod.get_all_metadata()
        if not metas:
            console.print(f"[dim]No skills in {skills_mod.SKILLS_DIR}[/dim]")
            console.print("[dim]Create one with: /skill new <name>[/dim]")
        else:
            groups = tools_mod.get_registry().list_by_source()
            for name, meta in sorted(metas.items()):
                src = f"skill:{name}"
                tools = groups.get(src, [])
                console.print(f"[bold]{name}[/bold] [dim]({meta.dir_path})[/dim]")
                if meta.description:
                    console.print(f"  [dim]{meta.description}[/dim]")
                for t in tools:
                    console.print(f"  [cyan]{t.name}[/cyan] — {t.description}")
                if not tools:
                    console.print("  [yellow](standby/documentation-only)[/yellow]")
    elif sub in ("trust", "revoke"):
        skill_name = parts[2] if len(parts) >= 3 else ""
        if not skill_name and sys.stdin.isatty():
            skill_rows = list(skills_mod.get_all_metadata().items())
            chosen = choose_record(
                skill_rows,
                title=f"{sub.title()} Skill",
                label=lambda item: item[0],
                description=lambda item: item[1].description or "(no description)",
                search=True,
            )
            skill_name = chosen[0] if chosen else ""
        if not skill_name:
            console.print("[dim]Skill selection cancelled.[/dim]")
        else:
            meta = skills_mod.get_all_metadata().get(skill_name)
            if meta is None:
                console.print(f"[red]Unknown skill: {skill_name}[/red]")
            else:
                entrypoint = Path(meta.dir_path) / "skill.py"
                if not entrypoint.is_file():
                    console.print("[dim]Documentation-only skills do not require executable trust.[/dim]")
                elif sub == "revoke":
                    skills_mod.unload_skill(skill_name)
                    removed = trust_store.revoke_extension("skill", skill_name)
                    console.print("[green]Skill trust revoked.[/green]" if removed
                                  else "[dim]Skill was not trusted.[/dim]")
                else:
                    manifest, manifest_error = skills_mod.load_skill_manifest(
                        Path(meta.dir_path), skill_name)
                    if manifest is None:
                        console.print(f"[red]{manifest_error}[/red]")
                        return False
                    status = trust_store.extension_status(
                        "skill", skill_name, entrypoint,
                        (Path(meta.dir_path) / skills_mod.SKILL_MANIFEST,))
                    approved = "--yes" in parts[3:]
                    if not approved and sys.stdin.isatty():
                        approved = _blocking_approval_prompt(
                            "Trust executable skill",
                            f"Skill: {skill_name}\nFile: {entrypoint.resolve()}\n"
                            f"SHA-256: {status.get('sha256', 'unavailable')}\n\n"
                            f"Capabilities: {manifest.get('capabilities', [])}\n\n"
                            "This Python executes with your local account permissions.",
                            "Trust this exact skill hash?", allow_always=False,
                        ) == "yes"
                    if approved:
                        trusted = trust_store.trust_extension(
                            "skill", skill_name, entrypoint,
                            (Path(meta.dir_path) / skills_mod.SKILL_MANIFEST,))
                        console.print(
                            f"[green]Trusted {skill_name} at "
                            f"{trusted['sha256'][:16]}…[/green]")
                    else:
                        console.print("[yellow]Skill not trusted.[/yellow]")
    elif sub in ("load", "unload"):
        skill_name = parts[2] if len(parts) >= 3 else ""
        if sub == "unload" and skill_name.lower() == "all":
            results = skills_mod.unload_all_skills()
            if not results:
                console.print("[dim]No skills are loaded.[/dim]")
            for name, ok, msg in results:
                console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
            return False
        if not skill_name and sys.stdin.isatty():
            skills = skills_mod.list_skills()
            eligible = [
                item for item in skills
                if bool(item.get("loaded")) == (sub == "unload")
            ]
            chosen = choose_record(
                eligible,
                title=f"{sub.title()} Skill",
                label=lambda item: item["name"],
                description=lambda item: item.get("description", ""),
                search=True,
            )
            skill_name = chosen["name"] if chosen else ""
        if not skill_name:
            console.print("[dim]Skill selection cancelled.[/dim]")
        else:
            fn = skills_mod.load_skill if sub == "load" else skills_mod.unload_skill
            ok, msg = fn(skill_name)
            console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
    elif sub == "reload":
        results = skills_mod.reload_all()
        for name, ok, msg in results:
            style = "green" if ok else "red"
            console.print(f"[{style}]{msg}[/{style}]")
        if not results:
            console.print(f"[dim]No skills in {skills_mod.SKILLS_DIR}[/dim]")
    elif sub == "new":
        if len(parts) < 3:
            console.print("[yellow]Usage: /skill new <name>[/yellow]")
        else:
            ok, msg = skills_mod.install_template(parts[2])
            style = "green" if ok else "red"
            console.print(f"[{style}]{msg}[/{style}]")
            if ok:
                console.print("[dim]Edit skill.py and extension.json, then run /skill trust <name> and /skill load <name>[/dim]")
    elif sub == "dir":
        console.print(str(skills_mod.SKILLS_DIR))
    else:
        console.print("[yellow]Usage: /skill [manager|list|trust <name>|revoke <name>|load <name>|unload <name>|unload all|reload|new <name>|dir][/yellow]")

    return False


def _cmd_mcp(parts: list) -> bool:
    if not _get_mcp_mod().MCP_AVAILABLE:
        console.print(f"[yellow]mcp SDK not installed: {_get_mcp_mod().MCP_IMPORT_ERROR}[/yellow]")
        console.print("[dim]Install with:  pip install mcp[/dim]")
        return False
    sub = (parts[1].lower() if len(parts) > 1 else "list")
    mgr = _get_mcp_mod().get_manager()

    def _pick_mcp_server(title: str, predicate=None) -> str:
        server_rows = list(mgr.load_config().get("servers", {}).items())
        if predicate is not None:
            server_rows = [item for item in server_rows if predicate(*item)]
        chosen = choose_record(
            server_rows,
            title=title,
            label=lambda item: item[0],
            description=lambda item: (
                f"{(mgr.servers.get(item[0]).status if mgr.servers.get(item[0]) else 'offline')}"
                f" {symbols.BULLET} {item[1].get('command', '?')}"),
            search=True,
        )
        return chosen[0] if chosen else ""

    if sub == "list":
        cfg = mgr.load_config().get("servers", {})
        if not cfg:
            console.print(f"[dim]No servers in {_get_mcp_mod().CONFIG_PATH}[/dim]")
            console.print("[dim]Create one with: /mcp init[/dim]")
        else:
            for name, sc in cfg.items():
                srv = mgr.servers.get(name)
                enabled = sc.get("enabled", True)
                if srv is None:
                    status = "(not connected)" if enabled else "(disabled)"
                    style = "dim"
                else:
                    status = f"({srv.status}, {len(srv.tools)} tools)"
                    style = "green" if srv.status == "up" else "yellow"
                cmd_str = sc.get("command", "?")
                console.print(f"  [{style}]{name}[/{style}] {status} [dim]{cmd_str}[/dim]")
                if srv and srv.last_error and srv.status != "up":
                    console.print(f"    [red]{srv.last_error}[/red]")
    elif sub in ("trust", "revoke"):
        server_name = parts[2] if len(parts) >= 3 else ""
        if not server_name and sys.stdin.isatty():
            server_name = _pick_mcp_server(f"{sub.title()} MCP Server")
        if not server_name:
            console.print("[dim]MCP server selection cancelled.[/dim]")
        else:
            server_cfg = mgr.load_config().get("servers", {}).get(server_name)
            if server_cfg is None:
                console.print(f"[red]Unknown MCP server: {server_name}[/red]")
            elif sub == "revoke":
                mgr.disconnect(server_name)
                removed = trust_store.revoke_extension("mcp", server_name)
                console.print("[green]MCP trust revoked.[/green]" if removed
                              else "[dim]MCP server was not trusted.[/dim]")
            else:
                cfg_path = _get_mcp_mod().CONFIG_PATH
                status = trust_store.extension_status(
                    "mcp", server_name, cfg_path)
                argv = [server_cfg.get("command")] + list(server_cfg.get("args") or [])
                approved = "--yes" in parts[3:]
                if not approved and sys.stdin.isatty():
                    approved = _blocking_approval_prompt(
                        "Trust MCP server process",
                        f"Server: {server_name}\nCommand: {argv}\n"
                        f"CWD: {server_cfg.get('cwd') or os.getcwd()}\n"
                        f"Explicit env keys: {sorted((server_cfg.get('env') or {}).keys())}\n"
                        f"Capabilities: {server_cfg.get('capabilities', ['core.other'])}\n"
                        f"Config SHA-256: {status.get('sha256', 'unavailable')}",
                        "Trust this server at the current config hash?",
                        allow_always=False,
                    ) == "yes"
                if approved:
                    trusted = trust_store.trust_extension(
                        "mcp", server_name, cfg_path)
                    console.print(
                        f"[green]Trusted MCP server {server_name} at "
                        f"{trusted['sha256'][:16]}…[/green]")
                else:
                    console.print("[yellow]MCP server not trusted.[/yellow]")
    elif sub == "tools":
        srv_name = parts[2] if len(parts) >= 3 else ""
        if not srv_name and sys.stdin.isatty():
            srv_name = _pick_mcp_server("View MCP Tools")
        if not srv_name:
            console.print("[dim]MCP server selection cancelled.[/dim]")
        else:
            groups = tools_mod.get_registry().list_by_source()
            ts = groups.get(f"mcp:{srv_name}", [])
            if not ts:
                console.print(f"[dim]No tools for mcp:{srv_name} (not connected?)[/dim]")
            else:
                for t in ts:
                    console.print(f"  [cyan]{t.name}[/cyan] — {t.description}")
    elif sub == "connect":
        server_name = parts[2] if len(parts) >= 3 else ""
        if not server_name and sys.stdin.isatty():
            server_name = _pick_mcp_server(
                "Connect MCP Server",
                lambda name, cfg: bool(cfg.get("enabled", True)))
        if not server_name:
            console.print("[dim]MCP server selection cancelled.[/dim]")
        else:
            ok, msg = mgr.connect(server_name)
            style = "green" if ok else "red"
            console.print(f"[{style}]{server_name}: {msg}[/{style}]")
    elif sub == "disconnect":
        server_name = parts[2] if len(parts) >= 3 else ""
        if not server_name and sys.stdin.isatty():
            server_name = _pick_mcp_server(
                "Disconnect MCP Server",
                lambda name, _cfg: name in mgr.servers)
        if not server_name:
            console.print("[dim]MCP server selection cancelled.[/dim]")
        else:
            ok, msg = mgr.disconnect(server_name)
            style = "green" if ok else "red"
            console.print(f"[{style}]{server_name}: {msg}[/{style}]")
    elif sub == "reload":
        results = mgr.reload()
        for n, ok, m in results:
            if n == "(none)":
                console.print(f"[yellow]{m}[/yellow]")
                continue
            style = "green" if ok else "yellow"
            console.print(f"[{style}]{n}: {m}[/{style}]")
    elif sub == "init":
        ok, msg = _get_mcp_mod().MCPManager.write_template_config()
        style = "green" if ok else "red"
        console.print(f"[{style}]{msg}[/{style}]")
        if ok:
            console.print("[dim]Edit the file, enable a server, then /mcp reload[/dim]")
    elif sub == "config":
        console.print(str(_get_mcp_mod().CONFIG_PATH))
    else:
        console.print("[yellow]Usage: /mcp {list|trust <n>|revoke <n>|connect <n>|disconnect <n>|reload|tools <n>|init|config}[/yellow]")

    return False


def _disconnect_from_helpwo(agent_registry: AgentRegistry) -> None:
    """Withdraw this CLI's runtime environment from Helpwo (internal helper for
    /helpwo stop). Symmetric with connect_terminal_to_helpwo()."""
    if agent_registry is None or not agent_registry.agent_id:
        console.print("[dim]This CLI isn't connected to Helpwo.[/dim]")
        return
    if getattr(agent_registry, "depth", 0) == 0:
        name = agent_registry.agent_name
        agent_registry._last_agent_id = ""  # explicit — don't resurrect
        agent_registry.workspace_path = None
        agent_registry.unregister()
        agent_registry.agent_id = None
        agent_registry.agent_secret = ""
        console.print(f"[yellow]Runtime environment [bold]{name}[/bold] withdrawn from Helpwo "
                      f"(its terminal and files are no longer reachable there). "
                      f"Run /helpwo to connect again.[/yellow]")
    else:
        name = (agent_registry.terminal_meta or {}).get("name", agent_registry.agent_name)
        agent_registry._last_agent_id = ""  # explicit — don't resurrect
        agent_registry.unregister()
        agent_registry.agent_id = None
        agent_registry.agent_secret = ""
        console.print(f"[yellow]Sub-terminal [bold]{name}[/bold] withdrawn from Helpwo.[/yellow]")


def _cmd_helpwo(raw_args: str, parts: list, agent_registry: AgentRegistry,
                session: dict) -> None:
    """Connect this CLI to Helpwo as a runtime environment, or open the app.

    /helpwo              - local dist if found, else the hosted web app
    /helpwo --port 8080  - start the local server on a custom port
    /helpwo --dist <p>   - use a custom local dist directory
    /helpwo --remote     - skip the local server; open the hosted web app
    /helpwo stop         - go offline: stop the LOCAL gateway if running, and
                           withdraw this CLI's runtime environment from Helpwo

    Connecting exposes THIS CLI as a runtime environment in Helpwo — the
    current working directory is its workspace (files ride the direct P2P
    channel, never the server) and its shell is the environment's terminal.
    There is no separate "mount a folder" step: the environment IS this CLI at
    its cwd. cd elsewhere and re-run /helpwo --remote to move the environment.

    Local mode is offline-first: its UI, filesystem bridge and command bridge
    stay on 127.0.0.1 and do not require login or cloud registration. AI calls
    still use the configured backend when available. ``--remote`` is the
    explicit cloud mode and registers this CLI with the hosted app.
    """
    import helpwo_server

    # Subcommand: stop — go fully offline (local server + cloud environment).
    if len(parts) >= 2 and parts[1].lower() == "stop":
        stopped_any = False
        if helpwo_server.is_running():
            helpwo_server.stop_server()
            console.print("[yellow]Helpwo gateway stopped.[/yellow]")
            stopped_any = True
        if agent_registry is not None and agent_registry.agent_id:
            _disconnect_from_helpwo(agent_registry)
            stopped_any = True
        if not stopped_any:
            console.print("[dim]Helpwo is not running and this CLI isn't connected.[/dim]")
        return

    if helpwo_server.is_running():
        url = helpwo_server.get_url()
        console.print(f"[dim]Helpwo gateway already running at "
                      f"{helpwo_server.get_url(with_token=True)}[/dim]")
        console.print(f"[dim]Open {url} in your browser, or /helpwo stop to stop.[/dim]")
        return

    # Parse optional flags
    port = helpwo_server.DEFAULT_PORT
    dist_override = None
    remote = False
    bind_host = "127.0.0.1"
    args = parts[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--port":
            if i + 1 >= len(args):
                console.print("[red]--port requires a value.[/red]")
                console.print(r"[dim]Usage: /helpwo \[--port N] \[--host ADDR] \[--dist <path>] \[--remote] | stop[/dim]")
                return
            try:
                port = int(args[i + 1])
            except ValueError:
                console.print(f"[red]Invalid port: {args[i + 1]}[/red]")
                return
            if port < 1 or port > 65535:
                console.print(f"[red]Port must be 1-65535, got {port}[/red]")
                return
            i += 2
        elif arg == "--dist":
            if i + 1 >= len(args):
                console.print("[red]--dist requires a path.[/red]")
                console.print(r"[dim]Usage: /helpwo \[--port N] \[--host ADDR] \[--dist <path>] \[--remote] | stop[/dim]")
                return
            dist_override = args[i + 1]
            i += 2
        elif arg == "--host":
            if i + 1 >= len(args):
                console.print("[red]--host requires an address.[/red]")
                return
            bind_host = args[i + 1]
            i += 2
        elif arg == "--remote":
            remote = True
            i += 1
        elif arg.startswith("--"):
            console.print(f"[red]Unknown option: {arg}[/red]")
            console.print(r"[dim]Usage: /helpwo \[--port N] \[--host ADDR] \[--dist <path>] \[--remote] | stop[/dim]")
            return
        else:
            console.print(f"[yellow]Ignoring unrecognized argument: {arg}[/yellow]")
            i += 1

    dist_path = None
    if not remote:
        if dist_override:
            from pathlib import Path
            p = Path(dist_override).expanduser()
            if not p.is_dir() or not (p / "index.html").is_file():
                console.print(f"[red]Invalid dist directory: {p}[/red]")
                console.print("[dim]The directory must contain index.html.[/dim]")
                return
            dist_path = p.resolve()
        else:
            dist_path = helpwo_server._find_dist()
            if dist_path is None:
                # No local build available — fall back to the hosted web
                # app instead of a bare error, so /helpwo always gets you
                # to a working Helpwo one way or another.
                remote = True

    # --remote is "expose this environment where I am right now" — every call
    # re-shares the CURRENT cwd as the environment's workspace, not just the
    # first one. cd elsewhere and re-run /helpwo --remote and the environment
    # follows you there.
    #
    # Local mode has no such standing "where am I" question — it only shares
    # automatically the first time (nothing shared yet); once a workspace has
    # been established (a prior /helpwo --remote), local mode leaves it alone
    # rather than silently overwriting it.
    _auto_workspace = (
        os.getcwd() if remote
        else (None if agent_registry.workspace_path else os.getcwd())
    )

    if remote:
        url = _helpwo_web_app_url()
        if url is None:
            console.print(
                "[red]No hosted Helpwo web app for the current backend "
                f"({get_backend_profile().base_url}). Set LAINTAS_HELPWO_DIST "
                "or use --dist to point at a local build instead.[/red]")
            return
        # Best-effort link: a failed handshake (not logged in, backend
        # unreachable) shouldn't block opening the web app itself — same
        # graceful-degradation as local mode, which starts the server either
        # way and only warns that no agent is registered.
        connect_terminal_to_helpwo(agent_registry, session, quiet=False,
                                   workspace=_auto_workspace)
        console.print(f"[dim]Opening {url}[/dim]")
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        return

    ok, msg = helpwo_server.start_server(agent_registry, dist_dir=dist_path, port=port,
                                         session=session, host=bind_host)
    if not ok:
        console.print(f"[red]{msg}[/red]")
        return

    # The token rides in the URL on the first visit only; after that the
    # browser's cookie carries it. Same shape as Jupyter.
    url = helpwo_server.get_url(with_token=True)
    console.print(f"[green bold]Helpwo gateway started[/green bold]")
    console.print(f"  URL: [cyan]{url}[/cyan]")
    console.print(f"  Dist: [dim]{helpwo_server._dist_dir()}[/dim]")

    if helpwo_server.bind_host() not in ("127.0.0.1", "localhost", "::1"):
        # Not a refusal — whether this machine should be reachable is the
        # operator's call. But browsers only grant File System Access,
        # clipboard, microphone and Service Workers on a secure context, and
        # a plain-HTTP non-loopback address is not one.
        console.print(
            f"  [yellow]Bound to {helpwo_server.bind_host()} — reachable beyond this "
            f"machine.[/yellow]")
        console.print(
            "  [yellow]Over plain HTTP the browser will withhold File System Access, "
            "clipboard, microphone and Service Workers.[/yellow]")
        console.print(
            f"  [dim]To keep every feature without exposing a port, leave the default "
            f"bind and forward instead:[/dim]")
        console.print(
            f"  [dim]  ssh -N -L {port}:127.0.0.1:{port} <user>@<this-host>[/dim]")
    else:
        console.print(
            f"  [dim]Remote machine? From your own computer:[/dim]")
        console.print(
            f"  [dim]  ssh -N -L {port}:127.0.0.1:{port} <user>@<this-host>[/dim]")
        console.print(
            f"  [dim]then open the URL above — loopback keeps every browser feature.[/dim]")

    console.print("  Runtime: [dim]local loopback (offline-capable)[/dim]")
    if agent_registry and agent_registry.agent_id:
        console.print(f"  Cloud link: [dim]{agent_registry.agent_name} ({agent_registry.agent_id})[/dim]")
    else:
        console.print("  Cloud link: [dim]off — use /helpwo --remote to expose this environment to the hosted app[/dim]")

    console.print("[dim]  /helpwo stop to stop the gateway and go offline.[/dim]")

    # Open the browser
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _cmd_term(parts: list, agent_registry: AgentRegistry, interactive_session) -> bool:
    if len(parts) >= 2 and parts[1].lower() == "rename":
        if len(parts) != 4:
            console.print("[yellow]Usage: /term rename <old> <new>[/yellow]")
        elif not re.fullmatch(r"[A-Za-z0-9._-]+", parts[3]):
            console.print("[red]Terminal names may contain only letters, numbers, dot, underscore, and hyphen.[/red]")
        elif parts[2].lower() == "term0":
            console.print("[red]The primary terminal term0 cannot be renamed.[/red]")
        elif rename_terminal(parts[2], parts[3]):
            console.print(
                f"[green]Terminal renamed: [bold]{parts[2]}[/bold] → "
                f"[bold]{parts[3]}[/bold][/green]")
        else:
            console.print(
                f"[red]Could not rename '{parts[2]}': source missing or target already exists.[/red]")
    elif len(parts) == 2:
        # /t <name> or /term <name> — create sub-terminal (no agent stationed)
        name = parts[1]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.lower() == "term0":
            console.print(
                "[red]Invalid terminal name. Use letters, numbers, dot, "
                "underscore, or hyphen; term0 is reserved.[/red]")
            return False
        existing = get_terminal(name)
        if existing and existing.session and not existing.session.is_alive():
            unregister_terminal(name)
            existing = None
        if existing is not None:
            console.print(f"[yellow]Terminal '{name}' already exists. /t to view, /terminate {name} to remove.[/yellow]")
        else:
            parent_identity = (
                (agent_registry.terminal_meta or {}).get("name")
                if agent_registry and agent_registry.depth > 0 else "term0"
            ) or "term0"
            lain_cmd = _build_connected_subterminal_cmd(
                name,
                agent_registry.agent_id if agent_registry else None,
                parent_terminal=parent_identity,
            )
            sub = SubTerminalSession(lain_cmd)
            sub.start()
            time.sleep(0.1)
            if not sub.is_alive():
                console.print(f"[red]Could not start terminal '{name}'.[/red]")
                return False
            sub.read_output(timeout=0.1)
            try:
                register_terminal(
                    sub, "laintas-cli", 0, name=name,
                    parent_terminal="term0")
            except Exception as exc:
                sub.close()
                console.print(
                    f"[red]Could not register terminal '{name}': {exc}[/red]")
                return False
            console.print(f"[green]Created sub-terminal [bold]{name}[/bold] (no agent stationed)[/green]")
    elif len(parts) > 2:
        console.print("[yellow]Usage: /term [name|rename <old> <new>][/yellow]")
    else:
        # /t or /term (no args) — list terminals browser
        terminals = get_all_terminals()
        has_primary = interactive_session is not None and interactive_session.is_alive()
        if not terminals and not has_primary:
            console.print("[dim]No active sub-terminal sessions. "
                          "Use /station <agent-id> or let the AI spawn a command.[/dim]")
        elif not terminals and has_primary:
            # Only term0 exists — entering it is redundant (already in REPL).
            console.print("[dim]No sub-terminals. You are already in term0 (primary).[/dim]")
        else:
            show_terminal_manager(interactive_session)

    return False


def _cmd_reload(raw_args: str) -> None:
    if raw_args:
        console.print("[yellow]Usage: /reload[/yellow]")
    else:
        reload_default_files()



def _cmd_undo(action: str, raw_args: str, parts: list) -> bool:
    import snapshot as _snap
    cwd = os.getcwd()
    if action == "/snapshots":
        cps = _snap.list_for(cwd)
        if not cps:
            console.print("[dim]No checkpoints for this repository "
                          "(not a git repo, or none taken yet).[/dim]")
        else:
            console.print(f"[bold]Checkpoints[/bold] ({len(cps)}, newest last):")
            for i, c in enumerate(cps):
                ago = _format_time_ago(c.get("ts", 0))
                console.print(f"  [cyan]{c['sha'][:10]}[/cyan]  {c.get('label','') or '(no label)'}  [dim]{ago}[/dim]")
            console.print("[dim]Run /undo to choose a checkpoint, or /undo <sha> directly.[/dim]")
    elif action == "/snapshot":
        label = _decode_text_arg(raw_args) if raw_args else "manual"
        cp = _snap.create(cwd, label)
        if cp:
            console.print(f"[green]Checkpoint saved: {cp['sha'][:10]} ({label})[/green]")
        else:
            console.print("[yellow]Could not snapshot (not a git repository?).[/yellow]")
    else:  # /undo
        sha = parts[1] if len(parts) > 1 else None
        # Warn if an auto-snapshot is still being created in the background.
        _repl_state = getattr(handle_meta_command, "_last_agent_state", None)
        if _repl_state and _repl_state.get("_snapshot_pending") and sha is None:
            console.print(
                "[yellow]An auto-snapshot is still being created in the "
                "background. Wait a moment and try again, or specify a "
                "sha directly.[/yellow]")
        chosen_checkpoint = None
        if sha is None and sys.stdin.isatty():
            checkpoints = list(reversed(_snap.list_for(cwd)))
            chosen_checkpoint = choose_record(
                checkpoints,
                title="Restore Checkpoint",
                label=lambda item: item["sha"][:10],
                description=lambda item: (
                    f"{item.get('label') or '(no label)'} {symbols.BULLET} "
                    f"{_format_time_ago(item.get('ts', 0))}"),
                search=True,
            )
            sha = chosen_checkpoint.get("sha") if chosen_checkpoint else ""
        if sha == "":
            console.print("[dim]Checkpoint selection cancelled.[/dim]")
            return False
        approved = True
        if sys.stdin.isatty():
            label = ((chosen_checkpoint or {}).get("label") or "explicit checkpoint")
            approved = _blocking_approval_prompt(
                "Restore working tree",
                f"Checkpoint: {(sha or 'latest')[:10]}\nLabel: {label}\n\n"
                "Modified and deleted files will be restored. New files are kept.",
                "Restore this checkpoint?",
                allow_always=False,
            ) == "yes"
        if not approved:
            console.print("[dim]Checkpoint restore cancelled.[/dim]")
            return False
        ok, msg = _snap.restore(cwd, sha)
        console.print((f"[green]{msg}[/green]" if ok else f"[yellow]{msg}[/yellow]"))
        if ok:
            console.print("[dim]Files created since the checkpoint were kept. "
                          "A pre-undo checkpoint was saved (undo the undo with /undo).[/dim]")

    return False


def _config_table(described: dict, title: str) -> Table:
    table = Table(title=title, show_lines=False)
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_column("Type", style="dim")
    table.add_column("Source", style="dim")
    table.add_column("Description", style="dim")
    for key, meta in sorted(described.items()):
        table.add_row(
            key, repr(meta["value"]), meta["type"],
            "override" if meta["overridden"] else "default",
            meta["description"],
        )
    return table


def _cmd_config(parts: list) -> None:
    # Built-in config command (doesn't require .laintas/commands.py)
    if len(parts) == 1:
        console.print(_config_table(describe_runtime_config(), "Runtime Configuration"))
        console.print("[dim]Set with /config <key> <value>; restore with /config reset.[/dim]")
        console.print("[dim]Narrow the list with a prefix, e.g. /config search[/dim]")
    elif len(parts) == 2 and parts[1].lower() == "reset":
        reset_runtime_config()
        terminal_preferences.clear_ui_preferences()
        _apply_ui_theme("dark")
        console.print("[green]Runtime config reset to defaults.[/green]")
    elif len(parts) == 2:
        # /config <key> — show one; /config <prefix> — show the group.
        # 76 keys in one alphabetical table buries any subsystem's handful of
        # them in three separate places, so an unmatched word is treated as a
        # prefix before it is treated as a mistake.
        key = parts[1]
        described = describe_runtime_config()
        meta = described.get(key)
        if meta is None:
            fragment = key.casefold()
            matches = {k: v for k, v in described.items()
                       if k.casefold().startswith(fragment)}
            if matches:
                console.print(_config_table(
                    matches, f"Runtime Configuration {symbols.BULLET} {key}*"))
                console.print(
                    f"[dim]{len(matches)} of {len(described)} keys. "
                    f"Set with /config <key> <value>.[/dim]")
            else:
                console.print(f"[red]Unknown config key: {key}[/red]")
                console.print("[dim]Run /config to list valid keys.[/dim]")
        else:
            console.print(Panel(
                f"Value: [bold]{meta['value']!r}[/bold]\n"
                f"Default: {meta['default']!r}\n"
                f"Type: {meta['type']}\n"
                f"Source: {'override' if meta['overridden'] else 'default'}\n\n"
                f"[dim]{meta['description']}[/dim]",
                title=key, border_style="cyan",
            ))
    elif len(parts) == 3:
        # /config <key> <value>
        key = parts[1]
        try:
            if not set_runtime_config(key, parts[2]):
                console.print(f"[red]Unknown config key: {key}[/red]")
                console.print("[dim]Run /config to list valid keys.[/dim]")
            else:
                value = get_runtime_config(key)
                if key in terminal_preferences.PERSISTED_UI_KEYS:
                    terminal_preferences.set_ui_preference(key, value)
                if key in {"theme", "markdown_theme"}:
                    _apply_ui_theme(str(get_runtime_config("theme") or "dark"))
                console.print(
                    f"[green]{key} = {value!r} ({type(value).__name__})[/green]")
        except (ValueError, KeyError) as e:
            console.print(f"[red]{e}[/red]")
            console.print(f"[dim]Run /config {key} to inspect the expected type.[/dim]")
    else:
        console.print("[yellow]Usage: /config [key [value]] | /config reset[/yellow]")



def _web_modules():
    """(web_search, error). Both /web and /identity need it and it is optional."""
    try:
        import web_search
        return web_search, ""
    except ImportError as e:
        return None, f"web_search module unavailable: {e}"


def _web_status() -> None:
    """One screen answering 'why did that search or fetch behave like that'."""
    ws, err = _web_modules()
    if ws is None:
        console.print(f"[red]{err}[/red]")
        return

    chain, warnings = ws.resolve_chain(None)
    health = {h["engine"]: h for h in ws.engine_health()}

    table = Table(title="Search engines", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Engine", style="cyan")
    table.add_column("Cost", style="dim")
    table.add_column("Usable")
    table.add_column("Notes", style="dim")
    for index, name in enumerate(chain, 1):
        meta = health.get(name, {})
        usable = meta.get("usable")
        table.add_row(
            str(index), name, meta.get("cost", ""),
            f"[green]yes[/green]" if usable else "[yellow]no[/yellow]",
            meta.get("reason") or meta.get("describe", ""))
    # Registered but not in the active chain — otherwise a user who set
    # search_engine by hand cannot see what they excluded.
    for name in sorted(set(health) - set(chain)):
        meta = health[name]
        table.add_row("-", f"[dim]{name}[/dim]", meta.get("cost", ""),
                      "[dim]not in chain[/dim]", meta.get("describe", ""))
    console.print(table)
    for warning in warnings:
        console.print(f"[yellow]{warning}[/yellow]")

    proxy = ws._get_proxy() or ""
    mode = ws._proxy_mode()
    learned = sorted(ws._PROXY_HOSTS)
    render_reason = ws._render_unavailable_reason()

    lines = [
        f"Proxy        : {proxy or '(none)'}  mode={mode}",
    ]
    if mode == "auto" and proxy:
        lines.append(
            f"  routed via proxy: {', '.join(learned) if learned else '(no host has needed it yet)'}")
    # The browser deliberately takes its proxy only from the environment, so a
    # mismatch here is invisible everywhere else and shows up as a render that
    # silently goes direct.
    browser_proxy = (os.environ.get("LAINTAS_BROWSER_PROXY") or "").strip()
    if browser_proxy:
        lines.append(f"  browser override (env): {browser_proxy}")
    elif proxy and mode != "off":
        lines.append("  browser: shares the setting above")
    if (os.environ.get("LAINTAS_BROWSER_USER_AGENT") or "").strip():
        lines.append("  browser UA pinned by env")

    lines.append(
        f"Render tier  : {'[green]available[/green]' if not render_reason else '[yellow]' + render_reason + '[/yellow]'}"
        f"  (fetch_render={ws._render_mode()})")
    lines.append(
        f"Fallbacks    : unlock={'on' if ws._unlock_enabled() else 'off'}  "
        f"wayback={'on' if ws._wayback_enabled() else 'off'}")

    try:
        import cookie_store
        if cookie_store.enabled():
            stats = cookie_store.stats()
            note = ""
            if stats["total"] != stats["usable"]:
                # Clearance is bound to the exit that earned it, so a changed
                # proxy makes cookies unusable without deleting them.
                note = (f"  [yellow]{stats['total'] - stats['usable']} not valid "
                        f"for this exit[/yellow]")
            lines.append(
                f"Cookie jar   : {stats['usable']}/{stats['total']} usable across "
                f"{stats['domains']} domain(s){note}  [dim](/web cookies)[/dim]")
        else:
            lines.append("Cookie jar   : off  [dim](/config search_cookie_enabled true)[/dim]")
    except ImportError:
        pass

    try:
        import identity_store
        count = len(identity_store.names())
        state = "on" if identity_store.enabled() else "off"
        lines.append(f"Saved logins : {count} stored, use is {state}"
                     f"  [dim](/identity)[/dim]")
    except ImportError:
        pass

    console.print(Panel("\n".join(lines), title="Web", border_style="cyan"))


def _web_engines(parts: list) -> None:
    ws, err = _web_modules()
    if ws is None:
        console.print(f"[red]{err}[/red]")
        return
    if len(parts) >= 3 and parts[2].lower() == "init":
        written, path = ws.write_engine_template()
        if written:
            console.print(f"[green]Wrote a starter engine file to {path}[/green]")
            console.print("[dim]Edit it, then /web engines to see the result.[/dim]")
        else:
            console.print(f"[yellow]{path} already exists — edit it in place.[/yellow]")
        return

    entries, errors = ws.load_engine_registry()
    health = {h["engine"]: h for h in ws.engine_health()}
    table = Table(title="Engine registry", show_lines=False)
    table.add_column("Engine", style="cyan")
    table.add_column("Kind", style="dim")
    table.add_column("Cost", style="dim")
    table.add_column("Usable")
    table.add_column("Description", style="dim")
    for name in sorted(entries):
        meta = health.get(name, {})
        table.add_row(
            name, entries[name].get("kind", ""), entries[name].get("cost", ""),
            "[green]yes[/green]" if meta.get("usable") else "[yellow]no[/yellow]",
            meta.get("reason") or entries[name].get("describe", ""))
    console.print(table)
    for message in errors:
        console.print(f"[red]{message}[/red]")
    console.print("[dim]Add your own JSON engines with /web engines init.[/dim]")


def _web_test(parts: list) -> None:
    """Run a known query through engines and report what came back.

    Scraped engines fail in a way that is not an error: a result list that is
    the right length with every snippet empty, or a 200 carrying an empty
    result frame. Only actually running one and counting shows that.
    """
    ws, err = _web_modules()
    if ws is None:
        console.print(f"[red]{err}[/red]")
        return
    entries, _errors = ws.load_engine_registry()
    requested = parts[2].lower() if len(parts) >= 3 else ""
    if requested:
        name = ws.canonical_engine(requested)
        if name not in entries:
            console.print(f"[red]Unknown engine: {requested}[/red]")
            console.print(f"[dim]Known: {', '.join(sorted(entries))}[/dim]")
            return
        names = [name]
    else:
        names, _warnings = ws.resolve_chain(None)

    # Plain words, no operators. A probe using site: or a bang reports a
    # healthy engine as broken whenever that engine simply does not support
    # the operator — cn.bing returns nothing at all for site: queries — and
    # the point here is to test the engine, not its query syntax.
    probe = "python urllib parse documentation"
    console.print(f"[dim]Query: {probe!r}  "
                  f"(use /web try <query> to test your own)[/dim]")
    table = Table(show_lines=False)
    table.add_column("Engine", style="cyan")
    table.add_column("Result", width=10)
    table.add_column("Hits", justify="right", width=5)
    table.add_column("Snippets", justify="right", width=9)
    table.add_column("Time", justify="right", width=7)
    table.add_column("Detail", style="dim")

    for name in names:
        started = time.time()
        out = ws.search(probe, max_results=5, engines=[name])
        elapsed = f"{time.time() - started:.1f}s"
        if out.get("ok"):
            results = out.get("result") or []
            with_snippet = sum(1 for r in results if r.get("snippet"))
            # Full count with no snippets is the half-broken parser case.
            if results and with_snippet == 0:
                verdict = "[yellow]degraded[/yellow]"
                detail = "results have no snippets — the parser is likely stale"
            else:
                verdict = "[green]ok[/green]"
                detail = ""
            table.add_row(name, verdict, str(len(results)),
                          f"{with_snippet}/{len(results)}", elapsed, detail)
        else:
            reasons = "; ".join(
                e.get("message", e.get("error", "")) for e in (out.get("errors") or []))
            table.add_row(name, "[red]fail[/red]", "0", "-", elapsed, reasons[:70])
    console.print(table)


def _web_try(parts: list) -> None:
    ws, err = _web_modules()
    if ws is None:
        console.print(f"[red]{err}[/red]")
        return
    query = " ".join(parts[2:]).strip()
    if not query:
        console.print("[yellow]Usage: /web try <query>[/yellow]")
        return
    started = time.time()
    out = ws.search(query, max_results=5)
    elapsed = time.time() - started
    if not out.get("ok"):
        console.print(f"[red]{out.get('error')}[/red]")
        return
    console.print(
        f"[green]{out.get('engine')}[/green] answered in {elapsed:.1f}s "
        f"[dim](cost: {out.get('cost', 'free')})[/dim]")
    for skipped in (out.get("errors") or []):
        console.print(f"  [dim]{skipped.get('engine')}: {skipped.get('message', '')[:80]}[/dim]")
    for item in out.get("result") or []:
        console.print(f"\n[cyan]{item.get('title', '')[:90]}[/cyan]")
        console.print(f"  [dim]{item.get('url', '')[:100]}[/dim]")
        if item.get("snippet"):
            console.print(f"  {item['snippet'][:160]}")


def _web_cookies(parts: list) -> None:
    try:
        import cookie_store
    except ImportError:
        console.print("[red]cookie_store module unavailable[/red]")
        return
    action = parts[2].lower() if len(parts) >= 3 else "list"
    if action == "clear":
        domain = parts[3] if len(parts) >= 4 else ""
        removed = cookie_store.clear(domain)
        target = domain or "all domains"
        console.print(f"[green]Cleared {removed} cookie(s) for {target}.[/green]")
        ws, _err = _web_modules()
        if ws is not None and not domain:
            ws.clear_cookie_jar(persistent=False)
        return
    if action not in ("list", ""):
        console.print(r"[yellow]Usage: /web cookies \[clear \[domain]][/yellow]")
        return
    if not cookie_store.enabled():
        console.print("[yellow]The cookie jar is off "
                      "(/config search_cookie_enabled true).[/yellow]")
        return
    summary = cookie_store.summary()
    if not summary:
        console.print("[dim]No cookies stored.[/dim]")
        console.print("[dim]Only anti-bot clearance is kept automatically; a "
                      "sign-in must be saved with /identity capture.[/dim]")
        return
    stats = cookie_store.stats()
    table = Table(title="Stored clearance cookies", show_lines=False)
    table.add_column("Domain", style="cyan")
    table.add_column("Cookies", justify="right")
    for domain, count in summary:
        table.add_row(domain, str(count))
    console.print(table)
    console.print(f"[dim]Current exit: {stats['egress']} "
                  f"{symbols.BULLET} {stats['usable']} of {stats['total']} usable "
                  f"here.[/dim]")
    console.print(r"[dim]Values are never displayed. Clear with "
                  r"/web cookies clear \[domain].[/dim]")


def _cmd_web(parts: list) -> None:
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub in ("", "status"):
        _web_status()
    elif sub == "engines":
        _web_engines(parts)
    elif sub == "test":
        _web_test(parts)
    elif sub == "try":
        _web_try(parts)
    elif sub == "cookies":
        _web_cookies(parts)
    else:
        console.print(r"[yellow]Usage: /web \[status|engines \[init]|test \[engine]"
                      r"|try <query>|cookies \[clear \[domain]]][/yellow]")


def _cmd_identity(parts: list) -> None:
    """Manage saved logins. Never prints a cookie or token value."""
    try:
        import identity_store
    except ImportError:
        console.print("[red]identity_store module unavailable[/red]")
        return
    ws, _err = _web_modules()
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub in ("", "list"):
        records = identity_store.describe_all()
        if not records:
            console.print("[dim]No saved logins.[/dim]")
            console.print("[dim]Sign in through the live-view browser, then "
                          "/identity capture <name>.[/dim]")
            return
        table = Table(title="Saved logins", show_lines=False)
        table.add_column("Name", style="cyan")
        table.add_column("Domains", style="dim")
        table.add_column("Cookies", justify="right")
        table.add_column("Storage", justify="right")
        table.add_column("Exit")
        table.add_column("Last check", style="dim")
        for record in records:
            probe = record.get("last_probe") or {}
            if probe:
                checked = ("[green]signed in[/green]" if probe.get("ok")
                           else "[red]signed out[/red]")
            else:
                checked = "never checked"
            table.add_row(
                record["name"], ", ".join(record["domains"])[:40],
                str(record["cookies"]), str(record["origins"]),
                "[green]ok[/green]" if record["egress_matches_now"]
                else "[yellow]changed[/yellow]",
                checked)
        console.print(table)
        if not identity_store.enabled():
            console.print("[yellow]Use is disabled — /config identity_enabled true[/yellow]")
        console.print("[dim]Values are never displayed.[/dim]")

    elif sub == "check":
        name = parts[2] if len(parts) >= 3 else ""
        if not name or ws is None:
            console.print("[yellow]Usage: /identity check <name>[/yellow]")
            return
        out = ws.probe_identity(name)
        if not out.get("ok"):
            console.print(f"[red]{out.get('error')}[/red]")
        elif out.get("signed_in"):
            console.print(f"[green]{name}: still signed in[/green]")
        else:
            console.print(f"[yellow]{name}: signed out — {out.get('detail', '')}[/yellow]")
            console.print("[dim]Sign in again in the live view, then "
                          f"/identity capture {name}[/dim]")

    elif sub == "capture":
        name = parts[2] if len(parts) >= 3 else ""
        if not name or ws is None:
            console.print(r"[yellow]Usage: /identity capture <name> \[domain,domain][/yellow]")
            return
        domains = [d.strip() for d in (parts[3] if len(parts) >= 4 else "").split(",")
                   if d.strip()] or None
        out = ws.capture_identity(name, domains=domains)
        if not out.get("ok"):
            console.print(f"[red]{out.get('error')}[/red]")
            return
        record = out["identity"]
        console.print(f"[green]Saved '{record['name']}'[/green]")
        console.print(f"  domains : {', '.join(record['domains']) or '(none)'}")
        console.print(f"  cookies : {record['cookies']}, storage origins {record['origins']}")
        console.print(f"  exit    : {record['egress'] or '(not recorded)'}")

    elif sub == "delete":
        name = parts[2] if len(parts) >= 3 else ""
        if not name:
            console.print("[yellow]Usage: /identity delete <name>[/yellow]")
        elif identity_store.delete(name):
            console.print(f"[green]Deleted saved login '{name}'.[/green]")
        else:
            console.print(f"[red]No saved login named '{name}'.[/red]")

    else:
        console.print(r"[yellow]Usage: /identity \[list|check <name>"
                      r"|capture <name> \[domains]|delete <name>][/yellow]")


def _cmd_max() -> None:
    # Crank every capacity knob to its ceiling and lift every auto-exit
    # circuit breaker. Process-global → applies to all agents. /config reset reverts.
    applied = apply_max_config()
    console.print(f"[green]{symbols.ZAP} MAX mode — all limits lifted (applies to every agent):[/green]")
    for k, v in applied.items():
        console.print(f"  [cyan]{k}[/cyan] = {v}")
    console.print("[dim]Note: max_tokens may be capped lower by the provider. Revert with /config reset.[/dim]")



def _cmd_compact(parts: list, session: dict) -> bool:
    compact_arg = parts[1].lower() if len(parts) > 1 else ""
    if len(parts) > 2 or compact_arg not in ("", "status", "--force"):
        console.print("[yellow]Usage: /compact [status|--force][/yellow]")
        return False

    compact_state = getattr(handle_meta_command, '_last_agent_state', None)
    compact_chat = getattr(handle_meta_command, '_last_chat_history', None)
    compact_deps = getattr(handle_meta_command, '_last_deps', None) or get_loop_deps()
    compact_session = getattr(handle_meta_command, '_last_session', None) or session
    if not isinstance(compact_state, dict):
        console.print("[yellow]No current session context to compact.[/yellow]")
        return False

    if compact_arg == "status":
        info = session_context_status(compact_state)
        ratio = (info["tokens"] / info["usable"] * 100
                 if info["usable"] else 0)
        console.print(
            f"[bold]Context[/bold]  {_fmt_tokens(info['tokens'])} tokens {symbols.BULLET} "
            f"{info['messages']} messages {symbols.BULLET} {ratio:.0f}% of "
            f"{_fmt_tokens(info['usable'])} usable"
            + (f" {symbols.BULLET} summarized" if info["summary"] else ""))
        return False

    # Compact an isolated copy so cancelling a slow summarizer cannot let its
    # daemon worker mutate live session state after this command has returned.
    compact_working_state = copy.deepcopy(compact_state)
    compact_working_chat = copy.deepcopy(
        compact_chat if isinstance(compact_chat, list) else [])
    try:
        with _safe_status(
                f"[dim]Compacting session context… {symbols.BULLET} Esc/Ctrl+C cancel[/dim]",
                spinner="dots"):
            result = run_cancellable_blocking(
                lambda _cancel: compact_session_context(
                    compact_deps, compact_session, compact_working_state,
                    compact_working_chat))
    except BlockingOperationCancelled:
        console.print("[dim]Context compaction cancelled.[/dim]")
        return False
    if not result.get("ok"):
        console.print(f"[red]Context compaction failed: {result.get('error', 'unknown error')}[/red]")
        return False
    if not result.get("changed"):
        console.print(
            f"[dim]Context unchanged: {result.get('reason', 'nothing to compact')} "
            f"({_fmt_tokens(result['tokens'])} tokens).[/dim]")
        return False

    compact_state.clear()
    compact_state.update(compact_working_state)
    handle_meta_command._last_agent_state = compact_state
    current_live = getattr(handle_meta_command, '_current_live_session', None)
    cwd = ((current_live or {}).get("cwd") or os.getcwd())
    if current_live:
        current_live = session_store.sync_runtime(
            current_live, compact_state,
            compact_chat if isinstance(compact_chat, list) else [],
            cwd=cwd,
            tasks=task_manager.export_active_tasks(
                cwd=cwd,
                session_id=str(compact_state.get("_session_id") or "") or None),
        )
        handle_meta_command._current_live_session = current_live
    save_resume_state(
        compact_state,
        compact_chat if isinstance(compact_chat, list) else [], cwd)
    event_log.append(
        "context_compacted",
        before_tokens=result["tokens"],
        after_tokens=result["after_tokens"],
        session_id=str(compact_state.get("_session_id") or ""),
    )
    console.print(
        f"[green]Context compacted: {_fmt_tokens(result['tokens'])} → "
        f"{_fmt_tokens(result['after_tokens'])} tokens"
        f" {symbols.BULLET} {result['messages']} → {result['after_messages']} messages[/green]")
    return False


def _cmd_continue(session: dict, agent_registry: AgentRegistry) -> bool:
    _current_live = getattr(handle_meta_command, '_current_live_session', None)
    _prev_state = getattr(handle_meta_command, '_last_agent_state', None)
    _prev_chat = getattr(handle_meta_command, '_last_chat_history', None)
    _prev_input = getattr(handle_meta_command, '_last_original_input', None)
    _prev_deps = getattr(handle_meta_command, '_last_deps', None) or get_loop_deps()
    _prev_session = getattr(handle_meta_command, '_last_session', None) or session
    _prev_events_cb = getattr(handle_meta_command, '_last_events_cb', None)
    _prev_existing_session = getattr(handle_meta_command, '_last_existing_session', None)

    if _current_live:
        # Prefer the mutable objects used by the main REPL.  The persisted
        # live-session payload is a restart fallback, not a second runtime
        # source of truth.
        if _prev_state is None:
            _prev_state = (_current_live.get("state")
                           or _current_live.get("agent_state"))
        if _prev_chat is None:
            _prev_chat = _current_live.get("chat_history") or []
        _prev_input = (_current_live.get("last_original_input")
                       or _current_live.get("last_user_input")
                       or _prev_input
                       or _current_live.get("objective"))

    if _prev_state is None or _prev_input is None:
        console.print("[yellow]No previous agent loop to continue.[/yellow]")
        return False

    if _current_live and not _current_live.get("pending_continuation"):
        console.print("[yellow]The current session has no pending continuation.[/yellow]")
        return False

    if not _prev_events_cb:
        def _prev_events_cb(events: list):
            if agent_registry.agent_id:
                agent_registry._push_events(events)

    _prev_state.pop("_max_loops_exhausted", None)
    _prev_state.pop("_exhaustion_loop_count", None)

    console.print("[green]Continuing current session...[/green]")

    response = _run_agent_loop_with_interrupt(
        _prev_deps, _prev_input, _prev_session, _prev_state,
        _prev_chat if _prev_chat is not None else [],
        events_cb=_prev_events_cb,
        existing_session=_prev_existing_session,
        continue_thread=True,
    )

    _continued_state = prepare_state_for_repl(
        response.get("state", _prev_state))
    if isinstance(_prev_state, dict):
        _prev_state.clear()
        _prev_state.update(_continued_state)
        _continued_state = _prev_state

    handle_meta_command._last_agent_state = _continued_state
    handle_meta_command._last_chat_history = _prev_chat
    handle_meta_command._last_original_input = _prev_input
    handle_meta_command._last_deps = _prev_deps
    handle_meta_command._last_session = _prev_session
    handle_meta_command._last_events_cb = _prev_events_cb
    handle_meta_command._last_existing_session = response.get("session")
    if response.get("msg"):
        console.print(_prev_deps.Markdown(response["msg"]) if hasattr(_prev_deps, 'Markdown') else response["msg"])
        (_prev_chat if _prev_chat is not None else []).append(
            {"role": "assistant", "content": response["msg"]}
        )
    if _current_live:
        try:
            updated = session_store.sync_runtime(
                _current_live,
                _continued_state,
                _prev_chat if _prev_chat is not None else [],
                cwd=_current_live.get("cwd") or os.getcwd(),
                objective=_prev_input,
                last_user_input=_prev_input,
                exit_reason=response.get("exit_reason"),
                tasks=task_manager.export_active_tasks(
                    cwd=_current_live.get("cwd") or os.getcwd(),
                    session_id=str(_prev_state.get("_session_id") or "") or None),
            )
            handle_meta_command._current_live_session = updated
        except Exception:
            pass

    return False


def _told_browse_history(chat: list) -> None:
    """Interactive full-screen browser for conversation history.

    Session turns are grouped by user message; Enter renders the full turn
    (prompt + assistant replies, Markdown-rendered) in the alternate screen.
    Tab toggles between the in-memory session history and the durable on-disk
    prompt journal (events.jsonl).  The text subcommands (/told N|all|reply|
    log) stay unchanged.
    """
    from rich.markup import escape

    def _session_turns():
        turns = []
        i = 0
        while i < len(chat):
            m = chat[i]
            if isinstance(m, dict) and m.get("role") == "user":
                replies = []
                j = i + 1
                while j < len(chat):
                    am = chat[j]
                    if isinstance(am, dict) and am.get("role") == "user":
                        break
                    if isinstance(am, dict) and am.get("role") == "assistant":
                        replies.append(am.get("content", ""))
                    j += 1
                turns.append({"kind": "turn",
                              "user": m.get("content", ""),
                              "replies": replies})
                i = j
            else:
                i += 1
        return turns

    def _journal_entries():
        path = paths.project_dir() / "events.jsonl"
        entries = []
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if (isinstance(evt, dict)
                                and evt.get("type") == "prompt_admitted"):
                            entries.append(evt)
        except OSError:
            pass
        return entries

    def _journal_ts(evt):
        ts = evt.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
            except (OSError, ValueError, OverflowError):
                pass
        return "????? ?????"

    def _build(source):
        # History reads most-recent-first: the newest turn sits at the top of
        # the list (#1) so the latest conversation is what you land on, rather
        # than being buried under the whole session.
        if source == "session":
            items = list(reversed(_session_turns()))
            labels = []
            for idx, t in enumerate(items):
                summary = " ".join(str(t["user"]).split())[:60] or "(empty)"
                n = len(t["replies"])
                labels.append(
                    f"[dim]#{idx + 1:>3}[/dim]  {escape(summary)}"
                    f"  [dim]({n} repl{'y' if n == 1 else 'ies'})[/dim]")
            return items, labels, "session history"
        items = list(reversed(_journal_entries()))
        labels = []
        for idx, evt in enumerate(items):
            summary = " ".join(str(evt.get("text", "")).split())[:56] or "(empty)"
            labels.append(
                f"[dim]#{idx + 1:>3}[/dim]  [dim]{_journal_ts(evt)}[/dim]  "
                f"{escape(summary)}")
        return items, labels, "prompt journal"

    def _show_detail(item, source):
        """Render the turn detail in a scrollable full-screen viewer.

        Rich renders into an in-memory ANSI buffer; the ANSI text is converted
        back to prompt_toolkit fragments so a Window(wrap_lines=True) can
        scroll it with the mouse wheel, arrows and PageUp/Down.
        """
        from prompt_toolkit import ANSI as _ptk_ansi
        from prompt_toolkit.application import Application as _ptk_app
        from prompt_toolkit.key_binding import KeyBindings as _ptk_kb
        from prompt_toolkit.layout import Layout as _ptk_layout
        from prompt_toolkit.layout.containers import Window as _ptk_window
        from prompt_toolkit.layout.controls import (
            FormattedTextControl as _ptk_ftc)
        from prompt_toolkit.mouse_events import (
            MouseEventType as _ptk_met)

        buf = io.StringIO()
        mem = Console(file=buf, force_terminal=True,
                      width=shutil.get_terminal_size().columns,
                      highlight=False)
        mem.print("[bold]── prompt ──[/bold]")
        text = item["user"] if source == "session" else str(item.get("text", ""))
        mem.print(f"[cyan]{escape(str(text))}[/cyan]")
        mem.print()
        if source == "session":
            replies = item["replies"]
            mem.print("[bold]── reply ──[/bold]")
            if replies:
                for reply in replies:
                    mem.print(RichMarkdown(str(reply))
                              if reply else "[dim](empty reply)[/dim]")
                    mem.print()
            else:
                mem.print("[dim](no reply recorded)[/dim]")
        else:
            mem.print("[dim]journal records prompts only — no reply stored[/dim]")
        mem.print(f"[dim]{symbols.ARROW_U}{symbols.ARROW_D}/wheel/PgUp/PgDn scroll  q/Esc/Enter back[/dim]")

        # Scroll model: prompt_toolkit Windows scroll by *following the
        # cursor* — each render recomputes vertical_scroll so the cursor
        # stays visible.  A plain FormattedTextControl has a fixed (0,0)
        # cursor, which snaps any manual scrolling back to the top.  So the
        # viewer keeps a real cursor line in ``pos`` and moves that; the
        # Window then scrolls to keep it visible.  The mouse wheel is wired
        # by subclassing (the ``@control.mouse_handler`` decorator idiom is
        # a no-op — the method is not a decorator factory).
        from prompt_toolkit.data_structures import Point as _ptk_point
        from prompt_toolkit.layout.containers import (
            ScrollOffsets as _ptk_offsets)

        line_count = buf.getvalue().count("\n") + 1
        pos = [0]

        def _move(delta):
            pos[0] = max(0, min(line_count - 1, pos[0] + delta))

        class _DetailBody(_ptk_ftc):
            def mouse_handler(self, mouse_event):
                etype = mouse_event.event_type
                if etype == _ptk_met.SCROLL_UP:
                    _move(-3)
                    return None
                if etype == _ptk_met.SCROLL_DOWN:
                    _move(3)
                    return None
                return NotImplemented

        body = _DetailBody(
            _ptk_ansi(buf.getvalue()),
            focusable=True,
            show_cursor=False,
            get_cursor_position=lambda: _ptk_point(0, pos[0]),
        )
        window = _ptk_window(content=body, wrap_lines=True,
                             always_hide_cursor=True,
                             scroll_offsets=_ptk_offsets(top=1, bottom=1))
        kb = _ptk_kb()

        @kb.add("up")
        def _up(event):
            _move(-1)

        @kb.add("down")
        def _down(event):
            _move(1)

        @kb.add("pageup")
        def _pgup(event):
            info = window.render_info
            _move(-(info.window_height if info is not None else 10))

        @kb.add("pagedown")
        def _pgdn(event):
            info = window.render_info
            _move(info.window_height if info is not None else 10)

        @kb.add("escape")
        @kb.add("q")
        @kb.add("enter")
        def _quit(event):
            event.app.exit()

        try:
            _ptk_app(layout=_ptk_layout(window), key_bindings=kb,
                     full_screen=True, mouse_support=True).run()
        except (KeyboardInterrupt, EOFError):
            pass

    source = "session"
    sel_idx = 0
    while True:
        items, labels, src_label = _build(source)
        if not items:
            console.print(f"[yellow]Nothing in the {src_label} yet.[/yellow]")
            return
        sel_idx = max(0, min(sel_idx, len(items) - 1))
        other = "journal" if source == "session" else "session"
        result = select_dialog(
            labels,
            title=f"History — {src_label} ({len(items)})",
            full_screen=True,
            search=True,
            selected_index=sel_idx,
            action_keys={"tab": "source"},
            enter_action="view",
            hint=(f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ view  type to filter  "
                  f"tab → {other}  q/Esc back"),
        )
        if result is None:
            return
        action, idx = result
        if action is None:
            return
        if action == "source":
            source = other
            sel_idx = 0
            continue
        if action == "view" and 0 <= idx < len(items):
            _show_detail(items[idx], source)
            sel_idx = idx


def _cmd_told(parts: list) -> bool:
    from rich.markup import escape
    args = list(parts[1:])
    scoped_agent_id = ""
    scoped_agent_name = ""
    reserved = {"all", "reply", "log"}
    if (args and args[0].lower() not in reserved
            and not args[0].isdigit()):
        reference = args[0]
        if _terminal_agents.configured:
            scoped_agent_id = _terminal_agents.resolve_agent_id(reference)
        else:
            candidate = get_agent(reference)
            if candidate is None:
                matches = [agent for agent in get_all_agents()
                           if str(agent.name or "").casefold()
                           == reference.casefold()]
                candidate = matches[0] if len(matches) == 1 else None
            scoped_agent_id = candidate.id if candidate else ""
        if not scoped_agent_id:
            console.print(
                f"[red]Agent '{escape(reference)}' is not available in this "
                f"terminal.[/red]")
            return False
        agent = get_agent(scoped_agent_id)
        scoped_agent_name = agent.name if agent else scoped_agent_id
        args = args[1:]

    # Default subcommand: bare "/told" opens the interactive history browser;
    # "/told <agent>" (scoped, no subcommand) keeps the last-turns text replay.
    sub = args[0].lower() if args else ("reply" if scoped_agent_id else "browse")
    if scoped_agent_id:
        if _terminal_agents.configured:
            _chat = _terminal_agents.chat_history_for(scoped_agent_id)
        elif scoped_agent_id == "primary":
            _chat = getattr(handle_meta_command, '_last_chat_history', None) or []
        else:
            scoped_agent = get_agent(scoped_agent_id)
            _chat = scoped_agent.chat_history if scoped_agent is not None else []
    else:
        _chat = getattr(handle_meta_command, '_last_chat_history', None) or []
    _user_msgs = [m.get("content", "") for m in _chat
                  if isinstance(m, dict) and m.get("role") == "user"]

    def _parse_n(token, default):
        try:
            n = int(token)
            return n if n > 0 else default
        except (TypeError, ValueError):
            return default

    if sub == "browse":
        _told_browse_history(_chat)
        return False

    if sub == "":
        if _user_msgs:
            console.print("[bold]You last asked:[/bold]")
            console.print(f"  [cyan]{escape(_user_msgs[-1])}[/cyan]")
        else:
            _fallback = getattr(handle_meta_command, '_last_original_input', None)
            if _fallback:
                console.print("[bold]You last asked:[/bold]")
                console.print(f"  [cyan]{escape(_fallback)}[/cyan]")
            else:
                console.print("[yellow]You haven't asked anything yet this session.[/yellow]")
                console.print("[dim]Tip: /told log shows prompts from the on-disk journal "
                              "(survives /new and restarts).[/dim]")

    elif sub == "all":
        if scoped_agent_id:
            replayable = [message for message in _chat
                          if isinstance(message, dict)
                          and message.get("role") in {
                              "user", "assistant", "tool", "shell", "knowledge"}]
            if not replayable:
                console.print(
                    f"[yellow]No conversation history for "
                    f"{escape(scoped_agent_name)}.[/yellow]")
            else:
                console.print(
                    f"[bold]── {escape(scoped_agent_name)} {symbols.BULLET} complete "
                    f"conversation ({len(replayable)} events) ──[/bold]")
                for message in replayable:
                    _print_resume_event(message)
                    console.print()
            return False
        if not _user_msgs:
            console.print("[yellow]No user messages in this session yet.[/yellow]")
            console.print("[dim]Tip: /told log reads the durable per-cwd journal.[/dim]")
        else:
            console.print(f"[bold]All your messages ({len(_user_msgs)}):[/bold]")
            for i, msg in enumerate(_user_msgs, 1):
                console.print(f"  [dim][{i}][/dim] {escape(msg)}")

    elif sub == "reply":
        n = _parse_n(args[1] if len(args) > 1 else "", 1)
        _turns = []
        i = 0
        while i < len(_chat):
            m = _chat[i]
            if isinstance(m, dict) and m.get("role") == "user":
                asst = None
                j = i + 1
                while j < len(_chat):
                    am = _chat[j]
                    if isinstance(am, dict) and am.get("role") == "assistant":
                        asst = am.get("content", "")
                        break
                    if isinstance(am, dict) and am.get("role") == "user":
                        break
                    j += 1
                _turns.append((m.get("content", ""), asst or "[no reply]"))
                i = j + 1 if asst is not None else j
            else:
                i += 1
        if not _turns:
            console.print("[yellow]No conversation turns to replay.[/yellow]")
            console.print("[dim]Tip: /told log reads the durable per-cwd journal.[/dim]")
        else:
            recent = _turns[-n:] if n < len(_turns) else _turns
            owner = f"{scoped_agent_name} {symbols.BULLET} " if scoped_agent_id else ""
            label = (f"{owner}last turn" if len(recent) == 1
                     else f"{owner}last {len(recent)} turns")
            console.print(f"[bold]── {label} ──[/bold]")
            for idx, (u, a) in enumerate(recent, 1):
                console.print(f"[bold]You:[/bold]        [cyan]{escape(u)}[/cyan]")
                console.print(f"[bold]Assistant:[/bold]   [green]{escape(a)}[/green]")
                if idx < len(recent):
                    console.print()

    elif sub == "log":
        if scoped_agent_id:
            console.print(
                "[yellow]The durable prompt journal is project-wide; use "
                "/told log [N] without an Agent.[/yellow]")
            return False
        n = _parse_n(args[1] if len(args) > 1 else "", 10)
        _log_path = paths.project_dir() / "events.jsonl"
        _entries = []
        try:
            if _log_path.exists():
                with open(_log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(evt, dict) and evt.get("type") == "prompt_admitted":
                            _entries.append(evt)
        except OSError:
            pass
        if not _entries:
            console.print("[yellow]No prompts recorded in the journal yet.[/yellow]")
            console.print(f"[dim]Journal: {escape(str(_log_path))}[/dim]")
        else:
            recent = _entries[-n:] if n < len(_entries) else _entries
            console.print(f"[bold]Recent prompts (from journal):[/bold]")
            for evt in recent:
                ts = evt.get("ts")
                tstr = ""
                if isinstance(ts, (int, float)) and ts > 0:
                    try:
                        tstr = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                    except (OSError, ValueError, OverflowError):
                        tstr = ""
                text = evt.get("text", "") or ""
                if tstr:
                    console.print(f"  [dim]{tstr}[/dim]  {escape(text)}")
                else:
                    console.print(f"  {escape(text)}")

    else:
        try:
            n = int(sub)
        except ValueError:
            console.print(f"[red]Unknown subcommand: {escape(sub)}[/red]")
            console.print(
                "[yellow]Usage: /told [agent-id [reply [N]|all]|N|all|"
                "reply [N]|log [N]][/yellow]")
            return False
        if n <= 0:
            console.print("[yellow]N must be a positive integer.[/yellow]")
            return False
        if not _user_msgs:
            console.print("[yellow]No user messages in this session yet.[/yellow]")
            console.print("[dim]Tip: /told log reads the durable per-cwd journal.[/dim]")
        else:
            recent = _user_msgs[-n:] if n < len(_user_msgs) else _user_msgs
            label = "Last message" if len(recent) == 1 else f"Last {len(recent)} messages"
            console.print(f"[bold]{label}:[/bold]")
            for i, msg in enumerate(recent, 1):
                console.print(f"  [dim][{i}][/dim] {escape(msg)}")

    return False


def _workflow_event_line(symbol: str, message: str, style: str) -> None:
    line = Text()
    line.append(f"{symbol} ", style=style)
    line.append(message, style="white")
    console.print(line)


def _workflow_result(kind: str, result: dict) -> None:
    ok = bool(result.get("ok"))
    paused = bool(result.get("paused"))
    symbol, status, style = (
        (f"{symbols.OK}", "complete", "success") if ok
        else (("Ⅱ", "paused", "warning") if paused
              else (f"{symbols.FAIL}", "failed", "error"))
    )
    heading = Text()
    heading.append(f"{symbol} ", style=style)
    heading.append(f"{kind} {status}", style="bold white")
    console.print(heading)
    message = str(result.get("msg") or "").strip()
    if message:
        console.print(Text(message, style="white"))


def _cmd_hwo(parts: list, session: dict) -> None:
    sub = parts[1].lower() if len(parts) > 1 else ""
    current = get_current_agent()
    if sub == "status":
        import hwo_runner
        r = hwo_runner.status(parts[2] if len(parts) >= 3 else None)
        console.print(r.get("msg", ""))
    elif sub in ("run", "compile") and len(parts) >= 3:
        # /hwo run <path>  or  /hwo compile <path>
        import hwo_runner
        path = " ".join(parts[2:])
        if sub == "compile":
            r = hwo_runner.compile_hwo_file(path)
        else:
            def _hwo_progress(event):
                if isinstance(event, list):
                    for item in event:
                        _hwo_progress(item)
                    return
                agent_ui_events.hub.ingest(
                    current.id if current else "", [event],
                    agent_scope_terminal(current) if current else "term0")
                kind = event.get("type")
                if kind == "workflow_started":
                    console.print(f"[dim]HWO {event.get('runId')} started[/dim]")
                elif kind == "step_started":
                    _workflow_event_line("▶", f"{event.get('stepId', '?')} started", "warning")
                elif kind == "step_completed":
                    _workflow_event_line(f"{symbols.OK}", f"{event.get('stepId', '?')} completed", "success")
                elif kind == "step_failed":
                    _workflow_event_line(f"{symbols.FAIL}", f"{event.get('stepId', '?')} failed", "error")
                elif kind == "workflow_completed":
                    _workflow_event_line(f"{symbols.OK}", f"HWO {event.get('runId')} completed", "success")
            r = hwo_runner.run_hwo_file(
                path=path,
                deps=get_loop_deps(),
                session=session,
                parent_id=current.id if current else None,
                events_cb=_hwo_progress,
            )
        _workflow_result("HWO", r)
    elif len(parts) >= 2 and sub not in ("run", "compile"):
        # /hwo <file>  — load .hwo into TUI
        file_path = " ".join(parts[1:])
        loaded, err = hwo_ui_mod.load_hwo_file(file_path)
        if err:
            console.print(f"[red]hwo: {err}[/]")
        else:
            root_name = current.name if current else "primary"
            hwo_ui_mod.run_hwo_ui(
                root_name,
                deps=get_loop_deps(),
                session_data=session,
                parent_id=current.id if current else None,
                initial_session=loaded,
            )
    else:
        # /hwo  — blank TUI
        root_name = current.name if current else "primary"
        hwo_ui_mod.run_hwo_ui(
            root_name,
            deps=get_loop_deps(),
            session_data=session,
            parent_id=current.id if current else None,
        )



def _cmd_hwg(parts: list, session: dict) -> None:
    import hwg_runner
    sub = parts[1].lower() if len(parts) > 1 else "status"
    current = get_current_agent()
    def _hwg_progress(event):
        if isinstance(event, list):
            for item in event:
                _hwg_progress(item)
            return
        agent_ui_events.hub.ingest(
            current.id if current else "", [event],
            agent_scope_terminal(current) if current else "term0")
        kind = event.get("type")
        if kind == "node_started":
            _workflow_event_line("▶", f"HWG node #{event.get('node', '?')} started", "warning")
        elif kind == "node_completed":
            _workflow_event_line(f"{symbols.OK}", f"HWG node #{event.get('node', '?')} completed", "success")
        elif kind == "node_failed":
            _workflow_event_line(f"{symbols.FAIL}", f"HWG node #{event.get('node', '?')} failed", "error")
        elif kind == "workflow_paused":
            _workflow_event_line("Ⅱ", f"HWG paused at node #{event.get('node', '?')}", "warning")
    if sub in ("run", "compile") and len(parts) >= 3:
        path = " ".join(parts[2:])
        if sub == "compile":
            r = hwg_runner.compile_hwg_file(path)
        else:
            r = hwg_runner.run_hwg_file(
                path=path,
                deps=get_loop_deps(),
                session=session,
                parent_id=current.id if current else None,
                events_cb=_hwg_progress,
            )
    elif sub == "resume" and len(parts) >= 3:
        run_id = parts[2]
        verdict = parts[3] if len(parts) >= 4 else "PASS"
        outputs = {}
        if len(parts) >= 5:
            raw = " ".join(parts[4:])
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    outputs = parsed
                else:
                    console.print("[yellow]resume outputs must be a JSON object; ignoring.[/yellow]")
            except Exception:
                console.print("[yellow]Could not parse resume outputs JSON; ignoring.[/yellow]")
        r = hwg_runner.resume_hwg_run(
            run_id,
            deps=get_loop_deps(),
            session=session,
            parent_id=current.id if current else None,
            verdict=verdict,
            outputs=outputs,
            events_cb=_hwg_progress,
        )
    elif sub == "status":
        r = hwg_runner.status(parts[2] if len(parts) >= 3 else None)
    elif sub == "cancel" and len(parts) >= 3:
        r = hwg_runner.cancel(parts[2])
    else:
        r = {
            "ok": False,
            "msg": (
                "Usage:\n"
                "  /hwg compile <file.hwg>\n"
                "  /hwg run <file.hwg>\n"
                "  /hwg resume <runId> [PASS|FAIL|verdict] [outputs-json]\n"
                "  /hwg status [runId]\n"
                "  /hwg cancel <runId>"
            ),
        }
    _workflow_result("HWG", r)



def _cmd_version(action: str, parts: list) -> None:
    # /v, /version → show version + check; /update is shorthand for /v update.
    if action == "/update":
        if not parts[1:]:
            handle_version_command(["/v", "update"])
        elif parts[1].lower() in ("--force", "-f"):
            handle_version_command(["/v", "update"] + parts[1:])
        elif len(parts) == 2 and parts[1].lower() == "check":
            handle_version_command(["/v", "check"])
        else:
            console.print("[yellow]Usage: /update [--force]  |  /update check[/yellow]")
    else:
        handle_version_command(parts)


def _handle_meta_command_impl(cmd: str, agent_registry: AgentRegistry, session: dict, interactive_session=None) -> bool:
    """Handle meta commands. Returns True if should exit."""
    action, raw_args, parts = _parse_slash_command(cmd)
    _validate_slash_args(action, parts[1:])

    if action == "/":
        return _cmd_palette()

    if action == "/exit":
        return _cmd_exit(raw_args, agent_registry)

    if action in ("/quit", "/q"):
        return _cmd_quit(action, raw_args, agent_registry)

    elif action == "/back":
        return _cmd_back(raw_args)

    elif action == "/help":
        _cmd_help(parts)

    elif action == "/resume":
        _cmd_resume()

    elif action in _NEW_SESSION_COMMANDS:
        _cmd_new_session_notice()

    elif action == "/login":
        _cmd_login(session, agent_registry)

    elif action == "/model":
        _cmd_model(parts, raw_args, session)

    elif action == "/name":
        _cmd_name(raw_args, session, agent_registry)

    elif action == "/focus":
        _cmd_focus(parts)

    elif action == "/memory":
        _cmd_memory(parts)

    elif action == "/mail":
        _cmd_mail(parts, raw_args, session)

    elif action == "/prop":
        _cmd_prop()

    elif action == "/scan":
        _cmd_scan()

    elif action == "/cwd":
        _cmd_cwd()

    elif action == "/usage":
        _show_usage_command(parts[1:], session)

    elif action == "/bash":
        return _cmd_bash(parts, raw_args)

    elif action == "/mode":
        return _cmd_mode(raw_args, parts)

    elif action == "/trust":
        _cmd_trust(parts)

    elif action == "/backend":
        _cmd_backend(parts)

    elif action == "/hooks":
        _cmd_hooks(parts)

    elif action == "/policy":
        return _cmd_policy(parts)

    elif action == "/plan":
        _cmd_plan(raw_args, parts)

    elif action == "/evolve":
        _cmd_evolve(raw_args, parts, session)

    elif action == "/prompt":
        _cmd_prompt(raw_args, parts, session)

    elif action == "/work":
        _cmd_work(parts)

    elif action == "/task":
        _cmd_task(raw_args, parts)

    elif action == "/workflow":
        _cmd_workflow(raw_args, parts)

    elif action == "/debug":
        _cmd_debug(parts)

    elif action == "/why":
        _cmd_why(parts)

    elif action == "/detail":
        _cmd_detail(parts)

    elif action == "/stream":
        _cmd_stream(parts)

    elif action == "/theme":
        _cmd_theme(parts)

    elif action in ("/station", "/st"):
        return _cmd_station(parts, agent_registry, session)

    elif action == "/terminate":
        _cmd_terminate(parts)

    elif action == "/send":
        return _cmd_send(raw_args)

    elif action == "/hire":
        return _cmd_hire(parts, session)

    elif action == "/agent":
        return _cmd_agent(parts, session, interactive_session)

    elif action == "/agents":
        agents_session = _cmd_agents(
            parts, session, agent_registry, interactive_session)
        if agents_session is not None:
            handle_meta_command._last_existing_session = agents_session

    elif action == "/spawn":
        _cmd_spawn(raw_args, session, agent_registry)

    elif action == "/tell":
        _cmd_tell(raw_args)

    elif action == "/abort":
        _cmd_abort(parts)

    elif action == "/tools":
        _cmd_tools()

    elif action == "/tool":
        _cmd_tool(raw_args, session, agent_registry)

    elif action == "/skill":
        return _cmd_skill(parts)

    elif action == "/mcp":
        return _cmd_mcp(parts)

    elif action == "/helpwo":
        _cmd_helpwo(raw_args, parts, agent_registry, session)

    elif action in ("/t", "/term"):
        return _cmd_term(parts, agent_registry, interactive_session)

    elif action == "/reload":
        _cmd_reload(raw_args)

    elif action in ("/undo", "/snapshot", "/snapshots"):
        return _cmd_undo(action, raw_args, parts)

    elif action == "/config":
        _cmd_config(parts)

    elif action in ("/web", "/search"):
        _cmd_web(parts)

    elif action == "/identity":
        _cmd_identity(parts)

    elif action == "/max":
        _cmd_max()

    elif action == "/compact":
        return _cmd_compact(parts, session)

    elif action == "/continue":
        return _cmd_continue(session, agent_registry)

    elif action == "/told":
        return _cmd_told(parts)

    elif action == "/hwo":
        _cmd_hwo(parts, session)

    elif action == "/hwg":
        _cmd_hwg(parts, session)

    elif action in ("/v", "/version", "/update"):
        _cmd_version(action, parts)

    else:
        # Evolution Lab extensions register project-local slash commands here.
        try:
            handled, extension_result = extension_runtime.get_runtime().invoke_command(
                action, parts, cmd)
        except Exception as exc:
            console.print(
                f"[red]Extension command {action} error: "
                f"{type(exc).__name__}: {exc}[/red]")
            handled = False
            extension_result = None
        if handled:
            if extension_result is not None:
                console.print(extension_result)
            return False
        # Try .laintas/commands.py custom handler first
        handler = _load_extra_commands()
        if handler:
            ctx = {
                # Authentication state is intentionally never exposed to
                # project customization, even after workspace trust approval.
                "session": {}, "interactive_session": interactive_session,
                "raw_line": cmd, "raw_args": raw_args,
                "agent_registry": None, "console": console,
                "get_terminal": get_terminal, "get_all_terminals": get_all_terminals,
                "unregister_terminal": unregister_terminal, "register_terminal": register_terminal,
                "rename_terminal": rename_terminal,
                "get_agent": get_agent, "get_all_agents": get_all_agents,
                "get_current_agent": get_current_agent,
                "station_agent": station_agent, "unstation_agent": unstation_agent,
                "SubTerminalSession": SubTerminalSession,
                "observe_session": observe_session,
                "enter_session": enter_session,
                "_show_terminal_detail": _show_terminal_detail,
                "get_config": get_runtime_config,
                "set_config": set_runtime_config,
                "list_config": list_runtime_config,
                "reset_config": reset_runtime_config,
                "reload_default_files": reload_default_files,
            }
            try:
                if handler(action, parts, ctx):
                    return False
            except Exception as e:
                console.print(f"[red].laintas/commands.py error: {e}[/red]")
                return False
        console.print(f"[red]Unknown command: {action}[/red]")
        console.print("Type [bold]/help[/bold] for available commands.")

    return False


# ── Command palette registry ───────────────────────────────────────────
# Generated from COMMAND_SPECS so help, completion, and palette cannot drift.

def handle_meta_command(cmd: str, agent_registry: AgentRegistry, session: dict,
                        interactive_session=None) -> bool:
    """Exception-safe public slash-command dispatcher."""
    try:
        return _handle_meta_command_impl(
            cmd, agent_registry, session, interactive_session)
    except SlashCommandUsageError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
    except KeyboardInterrupt:
        console.print("[dim]Command cancelled.[/dim]")
    except Exception as exc:
        try:
            action = (cmd or "").strip().split(maxsplit=1)[0]
        except Exception:
            action = "/"
        console.print(
            f"[red]{action or '/'} failed: {type(exc).__name__}: {exc}[/red]")
        try:
            add_debug_log(DebugEntry(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                user_input=cmd,
                current_path=os.getcwd(),
                reply=f"{type(exc).__name__}: {exc}",
                command=action,
                error=True,
            ))
        except Exception:
            pass
        console.print(
            f"[dim]The CLI is still running. Use /help {action} to check usage, "
            "or /debug to inspect recent activity.[/dim]")
    return False


_COMMANDS = [
    (spec.name, (
        f"{spec.description} {symbols.BULLET} {spec.usage}" if spec.usage else spec.description))
    for spec in COMMAND_SPECS
    if spec.palette
]


def show_command_palette():
    """Interactive full-screen command selector — fuzzy filter, arrow keys, Enter.

    Returns the selected command string (e.g. \"/help\") or None if cancelled.
    """
    items = [(f"[cyan]{name}[/cyan]", desc) for name, desc in _COMMANDS]
    chosen = select_dialog(
        items,
        title="Commands — type to filter",
        full_screen=True,
        search=True,
        hint=f"{symbols.ARROW_U}{symbols.ARROW_D} navigate  ↵ select  Esc cancel",
    )
    if chosen is None:
        return None
    return chosen[0]


def show_help(command: str = ""):
    """Display generated command help, optionally for one command."""
    from rich.markup import escape
    if command:
        spec = _find_command_spec(command)
        if spec is None:
            console.print(f"[red]Unknown command: {escape(command)}[/red]")
            console.print("[dim]Run /help to list available commands.[/dim]")
            return
        aliases = ", ".join(spec.aliases) or "(none)"
        usage = spec.usage or spec.name
        details = (
            f"\n\n{escape(spec.help_text)}" if spec.help_text else "")
        console.print(Panel(
            f"[bold]{escape(usage)}[/bold]\n\n"
            f"{escape(spec.description)}{details}\n\n"
            f"[dim]Aliases: {escape(aliases)}[/dim]",
            title=escape(spec.name), border_style="cyan",
        ))
        return

    console.print()
    group_order = list(dict.fromkeys(spec.group for spec in COMMAND_SPECS))
    for title in group_order:
        rows = []
        if title == "Basics":
            rows.extend([
                ("ls, git, …", "PATH commands run directly"),
                ("<text>", "plain text → AI agent loop"),
            ])
        for spec in COMMAND_SPECS:
            if spec.group != title:
                continue
            label = spec.usage or spec.name
            if spec.aliases:
                label += f"  ({', '.join(spec.aliases)})"
            rows.append((label, spec.description))
        console.print(f"  [accent]{title}[/accent]")
        cmd_w = max(len(c) for c, _ in rows)
        for cmd, desc in rows:
            padded = escape(cmd.ljust(cmd_w))
            console.print(f"    [accent.dim]{padded}[/accent.dim]  [muted]{escape(desc)}[/muted]")
        console.print()


# ── LoopDeps factory (lazy init after all functions defined) ─────────

_loop_deps: Optional[LoopDeps] = None

def get_loop_deps() -> LoopDeps:
    """Return the singleton LoopDeps, creating it on first call."""
    global _loop_deps
    if _loop_deps is None:
        _loop_deps = LoopDeps(
            read_file=read_file,
            append_file=append_file,
            write_file=write_file,
            strip_ansi=strip_ansi,
            generate_prompt=generate_cli_prop_template,
            call_backend=call_backend_stream,
            SubTerminalSession=SubTerminalSession,
            InteractiveSession=InteractiveSession,
            display_command_output=display_command_output,
            display_sub_terminal_preview=display_sub_terminal_preview,
            display_file_diff=display_file_diff,
            console=console,
            Markdown=Markdown,
            pty_passthrough=pty_passthrough,
            request_command_approval=request_command_approval,
            request_file_write_approval=request_file_write_approval,
            request_file_delete_approval=request_file_delete_approval,
            display_task_list=display_live_task_list,
        )
    return _loop_deps


# ── Main ───────────────────────────────────────────────────────────────

_LOGO_LINES = [
    " ╷    ╭─╮  ┬  ╷ ╷ ┌┬┐  ╭─╮  ╭─╮",
    " │    ├─┤  │  │╲│  │   ├─┤  ╰─╮",
    " ╰──  ╵ ╵  ┴  ╵ ╵  ┴   ╵ ╵  ╰─╯",
]


def _shorten_path(p: str, max_len: int = 48) -> str:
    home = os.path.expanduser("~")
    if p.startswith(home):
        p = "~" + p[len(home):]
    if len(p) > max_len:
        p = "…" + p[-(max_len - 1):]
    return p


def show_banner(agent_name: str, session: dict = None):
    """Display the original line-art startup banner and environment summary."""
    shell_info = SHELL_NAME

    for line in _LOGO_LINES:
        console.print(f"[accent]{line}[/accent]")
    console.print(
        f"  [muted]cli[/muted] [accent.dim]v{__version__}[/accent.dim]"
        f"  [muted]{symbols.BULLET}[/muted]  [agent]{agent_name}[/agent]"
    )
    console.print()

    rows = []
    if session:
        account = (session.get("userEmail") or session.get("userName")
                   or session.get("userId") or "")
        if account:
            rows.append(("account", account))
    rows.append(("system", f"{SYSTEM} {symbols.BULLET} {shell_info}"))
    rows.append(("cwd", _shorten_path(os.getcwd())))
    backend_profile = get_backend_profile()
    rows.append((
        "backend",
        f"{backend_profile.base_url} "
        f"[{backend_profile.kind}; {backend_profile.billing_label}]",
    ))

    try:
        import plan_mode as _pm
        agent_mode = (
            "plan" if _pm.is_plan_mode()
            else mode_manager.get_active_mode()["name"]
        )
        rows.append(("mode", agent_mode))
    except Exception:
        pass
    try:
        import policy as _pol
        policy_mode = _pol.get_config().get("mode", "audit")
        mode_style = {"audit": "cyan", "enforce": "yellow",
                      "disabled": "red"}.get(policy_mode, "cyan")
        rows.append((
            "policy",
            f"[{mode_style}]{policy_mode}[/{mode_style}]",
        ))
    except Exception:
        pass

    status_parts = []
    try:
        task_agent = get_current_agent()
        task_session = str(
            ((task_agent.state or {}).get("_session_id")
             if task_agent else "") or "")
        open_tasks = [
            task for task in task_manager.list_tasks(
                cwd=os.getcwd(), session_id=task_session or None)
            if task.get("status") in ("pending", "in_progress")
        ]
        if open_tasks:
            status_parts.append(
                f"tasks: [accent]{len(open_tasks)} open[/accent]")
    except Exception:
        pass
    if status_parts:
        rows.append(("status", "  ".join(status_parts)))

    label_width = max(len(key) for key, _value in rows)
    for key, value in rows:
        console.print(
            f"  [muted]{key.rjust(label_width)}[/muted]  "
            f"[accent.dim]│[/accent.dim] {value}"
        )

    console.print()
    console.print(
        f"  [muted]PATH commands run directly {symbols.BULLET} plain text → AI {symbols.BULLET} "
        f"[/muted][accent]/help[/accent][muted] for commands {symbols.BULLET} "
        f"[/muted][accent]/mode[/accent][muted] plan {symbols.BULLET} "
        "[/muted][accent]/policy[/accent][muted] approvals[/muted]"
    )
    console.print()


_TERMINAL_EOF_ERRNOS = frozenset({
    errno.EBADF,
    errno.EIO,
    errno.ENODEV,
    errno.ENXIO,
})


def _stdin_terminal_disconnected() -> bool:
    """Return True when stdin is a PTY whose peer has disappeared."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    if os.isatty(fd):
        return False
    try:
        os.ttyname(fd)
    except OSError as exc:
        return exc.errno in _TERMINAL_EOF_ERRNOS
    return False


def _install_terminal_watchdog(shutdown_fn, *, interval: float = 30.0, grace: float = 8.0):
    """Exit when the controlling terminal disappears.

    The REPL already turns a dead terminal into EOF and shuts down — but only
    while it is actually reading from it. A session parked anywhere else (a
    lock, a queue, a remote poll, an agent view) never reaches that code, and
    signal handlers cannot rescue it: Python runs them on the main thread at a
    bytecode boundary, so a main thread blocked in a futex never executes them.
    SIGHUP and SIGTERM are both registered below and both were ignored by three
    sessions found alive ten days after their SSH connection died, holding
    440MB of swap between them; only SIGKILL removed them.

    A watchdog thread is the piece that was missing. It observes from outside
    the main thread, so it still runs when the main thread cannot, and it can
    end the process itself.
    """
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return          # piped input or a headless run: no terminal to lose
        tty_path = os.ttyname(fd)
    except (AttributeError, OSError, ValueError):
        return

    def terminal_gone() -> bool:
        # The slave device is unlinked from /dev/pts the moment the master
        # closes, so its absence is the earliest reliable signal. tcgetpgrp
        # covers the rarer case where the path lingers but the line hung up.
        if not os.path.exists(tty_path):
            return True
        try:
            os.tcgetpgrp(fd)
        except OSError as exc:
            return exc.errno in _TERMINAL_EOF_ERRNOS
        except ValueError:
            return True
        return False

    def attempt_clean_shutdown():
        try:
            shutdown_fn(input_closed=True)
        except SystemExit:
            pass
        except Exception:
            pass

    def watch():
        while True:
            time.sleep(interval)
            try:
                if not terminal_gone():
                    continue
            except Exception:
                continue
            # Give the ordinary shutdown its chance — it unregisters the agent
            # and saves the session — but never wait on it indefinitely, since
            # a stuck main thread is the reason this thread exists at all.
            threading.Thread(target=attempt_clean_shutdown, daemon=True).start()
            time.sleep(grace)
            os._exit(0)

    threading.Thread(target=watch, name="terminal-watchdog", daemon=True).start()


def _silence_disconnected_terminal_output():
    """Keep shutdown cleanup usable after the controlling PTY is gone."""
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                os.dup2(null_fd, stream.fileno())
            except (AttributeError, OSError, ValueError):
                pass
    finally:
        os.close(null_fd)


def _simple_prompt(cwd: str) -> str:
    """Plain input() prompt for non-TTY environments (piped stdin, --execute mode).

    Only used when stdin is NOT a real terminal. Sub-terminals created via
    SubTerminalSession (fork+pty) have a real PTY slave as stdin, so they
    use the full prompt_toolkit (pt_prompt) with arrow key support.
    """
    try:
        print(f"{cwd}\n$ ", end="", flush=True)
        raw = sys.stdin.buffer.readline()
        if not raw:
            raise EOFError("terminal input closed")
        return raw.decode("utf-8", errors="replace").strip()
    except KeyboardInterrupt:
        return ""
    except EOFError:
        raise
    except OSError as exc:
        if exc.errno in _TERMINAL_EOF_ERRNOS:
            raise EOFError("terminal input closed") from exc
        raise


# ── Remote Message Injection ──────────────────────────────────────────────
# Messages from HelpwoAI (poll thread) are injected into the main REPL loop
# so they go through the exact same input→route→execute pipeline as local
# user input. A wakeup pipe unblocks the main thread when a message arrives.

class _InjectedInput:
    """A message injected from the remote poll thread into the main loop.

    kind:
      "line"     — treated exactly like a locally typed line (meta command /
                   shell passthrough / AI, decided by the normal router).
      "dialogue" — a conversation message for the current Agent: the REPL
                   skips shell classification and routes it to the agent loop
                   (used by the /agents view after slash commands have been
                   rejected by that view).
    """
    __slots__ = ("text", "done", "kind")
    def __init__(self, text: str, done: threading.Event, kind: str = "line"):
        self.text = text
        self.done = done
        self.kind = kind


_injected_input_queue: queue.Queue = queue.Queue()
_wakeup_r: Optional[int] = None
_wakeup_w: Optional[int] = None
_IN_SUB_TERMINAL = False


def _init_injection_pipe():
    """Create the wakeup pipe (Unix only). Idempotent, thread-safe enough."""
    global _wakeup_r, _wakeup_w
    if _wakeup_r is None:
        _wakeup_r, _wakeup_w = os.pipe()
        for fd in (_wakeup_r, _wakeup_w):
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def _drain_pipe(fd: int):
    """Read and discard all bytes from the wakeup pipe."""
    try:
        while True:
            os.read(fd, 4096)
    except (BlockingIOError, OSError):
        pass


def _inject_input(text: str, done: threading.Event, kind: str = "line"):
    """Enqueue a message and wake up the main loop. Thread-safe."""
    _init_injection_pipe()
    try:
        _injected_input_queue.put_nowait(_InjectedInput(text, done, kind))
    except queue.Full:
        pass
    if _wakeup_w is not None:
        try:
            os.write(_wakeup_w, b'\x00')
        except (BlockingIOError, OSError):
            pass
    # If the main loop is blocked on prompt_toolkit, interrupt it so the
    # queued message is picked up immediately instead of waiting for user input.
    _interrupt_prompt()


def _get_input(cwd: str):
    """Return user input or an _InjectedInput from the remote queue.

    Checks the wakeup pipe non-blockingly so that remote messages queued
    between prompts are processed immediately. Does NOT block on stdin —
    prompt_toolkit handles its own blocking read after the prompt is shown.
    """
    if (_terminal_agents.configured
            and _terminal_agents.process_pending_approval()):
        return ""

    # /agents owns the screen: the REPL must not touch stdin (the view's
    # prompt_toolkit app reads it) and consumes only injected input.
    if _agents_view_is_active():
        try:
            return _injected_input_queue.get(timeout=0.25)
        except queue.Empty:
            return ""

    # Already-queued message (fast path)
    try:
        return _injected_input_queue.get_nowait()
    except queue.Empty:
        pass

    # Non-blocking check: is there a remote message waiting?
    _init_injection_pipe()
    try:
        r, _, _ = select.select([_wakeup_r], [], [], 0)
    except (select.error, ValueError, OSError):
        pass
    else:
        if _wakeup_r in r:
            _drain_pipe(_wakeup_r)
            try:
                return _injected_input_queue.get_nowait()
            except queue.Empty:
                pass  # spurious wakeup — fall through to prompt

    return _simple_prompt(cwd) if _IN_SUB_TERMINAL else pt_prompt(cwd)


def _parse_agent_target(text: str) -> tuple[str, str]:
    """Parse an explicit ``@agent-id message`` route without altering prose."""
    match = re.match(r"^@([A-Za-z0-9_.:-]+)(?:\s+|$)(.*)$", str(text or ""), re.S)
    if not match:
        return "", str(text or "")
    return match.group(1), match.group(2).strip()


# ── Background stdin reader for supplementary input during agent loop ──
# When the agent loop is running, the user can type additional instructions,
# or press Esc to soft-interrupt (the same interrupt Ctrl+C triggers, but
# wired straight to the loop's interrupt event — never a signal, so Esc can
# never kill the process; force exit stays exclusive to double Ctrl+C).
# A background thread owns stdin via prompt_toolkit (falling back to line
# mode without a tty) and hand-rolls just enough line editing to keep
# queuing supplementary text working the way it did with readline().
_bg_reader_thread: Optional[threading.Thread] = None
_bg_reader_stop = threading.Event()
_bg_prompt_session: Optional[PromptSession] = None
_run_input_state = "idle"
_run_input_state_lock = threading.RLock()


def _set_run_input_state(value: str) -> None:
    global _run_input_state
    with _run_input_state_lock:
        _run_input_state = value
    _update_status_cache(run_input_state=value)


def _queue_supplementary(target_queue: queue.Queue, line: str,
                         source: str = "local") -> bool:
    line = str(line or "").strip()
    if not line:
        return False
    try:
        target_queue.put_nowait(line)
    except queue.Full:
        console.print("[error]Supplementary instruction queue is full.[/error]")
        return False
    _set_run_input_state("queued")
    console.print(
        f"[accent.dim]↳[/accent.dim] [muted]Queued instruction"
        f"{f' from {escape(source)}' if source != 'local' else ''}: "
        f"{escape(agent_loop_crop_for_ui(line, 80))}[/muted]")
    return True


def agent_loop_crop_for_ui(value: str, width: int) -> str:
    """Use the agent loop's CJK-safe cropper without exposing it as UI state."""
    try:
        from agent_loop import _crop_cells
        return _crop_cells(value, width, middle=True)
    except Exception:
        return str(value or "")[:width]


def _bg_reader_prompt_mode(target_queue: queue.Queue,
                           interrupt_event: Optional[threading.Event] = None):
    """Prompt-toolkit-owned supplementary input; safe beside Rich Live output."""
    global _bg_prompt_session
    bindings = KeyBindings()

    @bindings.add(Keys.Escape)
    def _escape(event):
        # Esc clears the current supplementary draft, and soft-interrupts a
        # running agent loop by setting its interrupt event directly. It
        # must never raise SIGINT: doing so used to terminate the whole CLI
        # when the prompt was empty (and could interrupt unrelated login/IO
        # waits). Force exit stays exclusive to double Ctrl+C.
        event.app.current_buffer.reset()
        if interrupt_event is not None:
            interrupt_event.set()
        _set_run_input_state("input_active")

    @bindings.add(Keys.ControlC)
    def _control_c(_event):
        try:
            signal.raise_signal(signal.SIGINT)
        except (ValueError, OSError):
            pass

    _bg_prompt_session = PromptSession(key_bindings=bindings, multiline=False)
    try:
        with patch_stdout(raw=True):
            while not _bg_reader_stop.is_set():
                _set_run_input_state("input_active")
                try:
                    line = _bg_prompt_session.prompt(
                        [("class:prompt-gutter", "  │ "),
                         ("class:prompt-caret", "› ")],
                        style=_build_prompt_style(),
                        erase_when_done=True,
                        complete_while_typing=False,
                    )
                except (EOFError, KeyboardInterrupt):
                    if _bg_reader_stop.is_set():
                        break
                    continue
                if _bg_reader_stop.is_set():
                    break
                _queue_supplementary(target_queue, line)
    finally:
        _bg_prompt_session = None


def _bg_reader_line_mode(target_queue: queue.Queue):
    """Fallback for non-tty stdin (pipes, tests): no Esc detection possible
    without a real terminal, so just queue whole lines like before."""
    while not _bg_reader_stop.is_set():
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not r:
                continue
        except (select.error, ValueError, OSError):
            time.sleep(0.5)
            continue
        try:
            line = sys.stdin.readline()
        except Exception:
            break
        if not line:
            break  # EOF
        line = line.strip()
        if line:
            _queue_supplementary(target_queue, line)


def _bg_reader_cbreak_mode(target_queue: queue.Queue,
                           interrupt_event: Optional[threading.Event] = None):
    """cbreak-mode reader: a bare Esc keypress (no Enter needed) sets the
    agent loop's interrupt event directly — the same soft interrupt Ctrl+C
    triggers, but never a signal, so Esc can never kill the process. Force
    exit stays exclusive to double Ctrl+C. Also hand-rolls basic line
    editing for supplementary text, using an incremental UTF-8 decoder so
    multi-byte input (e.g. Chinese) is never corrupted mid-character."""
    fd = sys.stdin.fileno()
    try:
        old_tcattr = termios.tcgetattr(fd)
    except (termios.error, OSError):
        _bg_reader_line_mode(target_queue)
        return
    try:
        tty.setcbreak(fd)
    except (termios.error, OSError):
        _bg_reader_line_mode(target_queue)
        return

    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    buf: list[str] = []

    def _clear_visible_line():
        if buf:
            width = sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
                        for c in buf)
            sys.stdout.write('\r' + ' ' * width + '\r')
            sys.stdout.flush()
        buf.clear()
        decoder.reset()

    try:
        while not _bg_reader_stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.3)
            except (select.error, ValueError, OSError):
                break
            if not r:
                continue
            try:
                chunk = os.read(fd, 1)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if not chunk:
                break  # EOF

            if chunk == b'\x1b':
                # Disambiguate a bare Esc from an escape sequence (arrow
                # keys, Home/End, ...) which also starts with 0x1b — a real
                # sequence's remaining bytes arrive within a few ms; a lone
                # Esc press has nothing following it.
                try:
                    r2, _, _ = select.select([fd], [], [], 0.05)
                except (select.error, ValueError, OSError):
                    r2 = []
                if r2:
                    try:
                        os.read(fd, 32)  # drain and discard the sequence
                    except OSError:
                        pass
                    continue
                # A bare Esc soft-interrupts the running agent loop by
                # setting its interrupt event directly (no signal, so it
                # can never kill the whole CLI). With no event wired up it
                # stays a pure local cancel/clear of the draft line.
                if interrupt_event is not None:
                    _clear_visible_line()
                    interrupt_event.set()
                    console.print(
                        "\n[dim]Esc received - will stop at the next "
                        "checkpoint (when the model starts replying or a "
                        "tool step begins; not during thinking). Press "
                        "Ctrl+C twice quickly to force exit now.[/dim]")
                else:
                    _clear_visible_line()
                    _set_run_input_state("input_active")
                continue

            if chunk in (b'\r', b'\n'):
                if buf:
                    line = ''.join(buf)
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    _clear_visible_line()
                    _queue_supplementary(target_queue, line)
                continue

            if chunk in (b'\x7f', b'\x08'):  # Backspace/Delete
                if buf:
                    removed = buf.pop()
                    cells = 2 if unicodedata.east_asian_width(removed) in ('W', 'F') else 1
                    sys.stdout.write('\b \b' * cells)
                    sys.stdout.flush()
                continue

            text = decoder.decode(chunk)
            if text:  # complete character(s) — echo + accumulate
                sys.stdout.write(text)
                sys.stdout.flush()
                buf.extend(text)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_tcattr)
        except (termios.error, OSError):
            pass


def _start_bg_input_reader(target_queue: queue.Queue,
                           interrupt_event: Optional[threading.Event] = None):
    """Start a background thread that reads stdin for supplementary messages
    and Esc-to-interrupt during run_agent_loop().

    Only active during the agent loop — the normal REPL prompt uses
    prompt_toolkit which owns stdin.

    target_queue: the queue to put supplementary messages into (should be
    the same queue that run_agent_loop() drains between iterations).
    interrupt_event: when given, a bare Esc press sets it (soft interrupt).
    """
    global _bg_reader_thread, _bg_reader_stop
    if _bg_reader_thread is not None and _bg_reader_thread.is_alive():
        return  # already running
    _bg_reader_stop.clear()

    def _reader():
        try:
            if sys.stdin.isatty():
                # Prefer cbreak mode when an interrupt_event is wired —
                # it avoids prompt_toolkit's stdin ownership and its
                # potential race with Rich Live output (patch_stdout).
                # Fall back to prompt_toolkit only when cbreak fails.
                if interrupt_event is not None:
                    _bg_reader_cbreak_mode(target_queue, interrupt_event)
                else:
                    _bg_reader_prompt_mode(target_queue, interrupt_event)
            else:
                _bg_reader_line_mode(target_queue)
        except Exception:
            pass

    _bg_reader_thread = threading.Thread(
        target=_reader, daemon=True, name="bg-input-reader")
    _bg_reader_thread.start()


def _stop_bg_input_reader():
    """Stop the background input reader thread."""
    global _bg_reader_thread, _bg_prompt_session
    _bg_reader_stop.set()
    if _bg_prompt_session is not None:
        try:
            app = _bg_prompt_session.app
            if app.is_running:
                app.exit(result="")
        except Exception:
            pass
    if _bg_reader_thread is not None:
        _bg_reader_thread.join(timeout=1.5)
        _bg_reader_thread = None
    _set_run_input_state("idle")


# ── Session-level approval state ─────────────────────────────────────────
# Lets the user pick "always" at an approval prompt to auto-approve the rest
# of the session — mirrors Claude Code / Cursor's "yes, and don't ask again".
# Reset on /exit, /reload, or a fresh process start.
#
# "all_commands"/"all_writes" are a deliberate, visible BLANKET override —
# only ever set by an explicit mode choice (`/mode act always`, or a custom
# mode's `auto_approve` posture), never by a one-off prompt. Approving a
# single write/command with "Always" instead records that exact target in
# approved_write_paths/approved_commands, so it doesn't silently generalize
# to unrelated files/commands the user never saw (mirrors opencode's
# per-(permission, pattern) remembered-approval model instead of one global
# session-wide toggle).
_session_approval_state = {
    "all_commands": False,        # explicit mode-level auto-approve
    "all_writes": False,          # explicit mode-level auto-approve
    "approved_write_paths": set(),   # exact paths remembered via a prompt's "Always"
    "approved_commands": set(),      # exact commands remembered via a prompt's "Always"
}
_approval_star_announced = False


def _reset_session_approvals():
    """Clear session-level auto-approve (called on /exit, /reload)."""
    global _approval_star_announced
    _session_approval_state["all_commands"] = False
    _session_approval_state["all_writes"] = False
    _session_approval_state["approved_write_paths"] = set()
    _session_approval_state["approved_commands"] = set()
    _approval_star_announced = False


def _sync_session_approval_from_mode():
    """Set session auto-approve flags from the active mode's auto_approve posture.

    Called after switching modes so a mode's declared auto-approve (none/writes/
    commands/all) takes effect immediately — and switching to a plain mode
    (auto_approve=none) clears any prior auto-approve.
    """
    try:
        aa = mode_manager.get_auto_approve()
    except Exception:
        aa = "none"
    _session_approval_state["all_writes"] = aa in ("writes", "all")
    _session_approval_state["all_commands"] = aa in ("commands", "all")


def _arrow_approval_prompt(title: str, body_lines: list[str],
                           options: list[str], *,
                           auto_confirm_seconds: Optional[float] = None,
                           destructive: bool = False) -> Optional[str]:
    """Inline arrow-key approval selector.

    Renders a compact header (action word + target) followed by body content
    (reason / diff) into scrollback, then runs a non-full-screen prompt_toolkit
    selector for the options. Returns the selected option string, or None if
    cancelled (Esc / Ctrl+C).
    """
    from rich.markup import escape

    def _line_markup(ln: str) -> str:
        esc = escape(ln)
        if ln.startswith("+") and not ln.startswith("+++"):
            return f"[#9ece6a]{esc}[/#9ece6a]"
        if ln.startswith("-") and not ln.startswith("---"):
            return f"[#f7768e]{esc}[/#f7768e]"
        if ln.startswith("@@"):
            return f"[#7aa2f7]{esc}[/#7aa2f7]"
        return f"[#c0c0c0]{esc}[/#c0c0c0]"

    # Split "action — question" back into the action word for the header.
    _action = title.split(" — ")[0] if " — " in title else title
    _target = body_lines[0] if body_lines else ""
    _header_color = "#f85149" if destructive else "#e3b341"

    console.print(
        f"  [bold {_header_color}]{escape(_action)}[/bold {_header_color}]"
        f"  {escape(_target)}", highlight=False)
    for ln in body_lines[1:]:
        if not ln.strip():
            console.print()
        else:
            console.print("  " + _line_markup(ln), highlight=False)
    console.print()

    # Fail-safe default: land on the "n ..." option so a bare Enter denies.
    _default_idx = next((i for i, o in enumerate(options)
                         if o.strip().lower().startswith("n")), 0)

    return select_dialog(
        options,
        full_screen=False,
        selected_index=_default_idx,
        letter_shortcuts=True,
        refresh_interval=0.5,
        auto_confirm_seconds=auto_confirm_seconds,
        auto_confirm_index=0,
    )


_parallel_approval_lock = threading.Lock()


def _read_single_key_choice(*, allow_always: bool,
                            auto_confirm_seconds: Optional[float]) -> Optional[str]:
    """Block for one y/n/a keypress (no Enter needed, no line redraw).

    Returns "yes"/"no"/"always", or None on Esc/Ctrl+C/EOF. Bare Enter
    counts as "no" — same fail-safe default as the arrow selector's
    initial highlight. If auto_confirm_seconds elapses with no keypress,
    returns "yes" (matches _arrow_approval_prompt's auto_confirm_index=0,
    which is always the approve option).
    """
    fd = sys.stdin.fileno()
    try:
        old_attr = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return None
    try:
        # Raw mode makes Ctrl+C available as the literal \x03 byte handled
        # below instead of letting the terminal turn it into process SIGINT.
        tty.setraw(fd)
    except (termios.error, OSError):
        return None
    deadline = (time.monotonic() + auto_confirm_seconds
               if auto_confirm_seconds is not None else None)
    try:
        while True:
            wait = 0.3 if deadline is None else max(0.0, min(0.3, deadline - time.monotonic()))
            try:
                r, _, _ = select.select([fd], [], [], wait)
            except (select.error, ValueError, OSError):
                return None
            if not r:
                if deadline is not None and time.monotonic() >= deadline:
                    return "yes"
                continue
            try:
                ch = os.read(fd, 1).decode("utf-8", errors="ignore").lower()
            except OSError:
                return None
            if ch in ("\x03", "\x1b"):  # Ctrl+C / Esc
                return None
            if ch == "y":
                return "yes"
            if ch in ("n", "\r", "\n"):
                return "no"
            if ch == "a" and allow_always:
                return "always"
            # any other key: keep waiting
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        except (termios.error, OSError):
            pass


def _compact_parallel_approval_prompt(title: str, body: str, question: str, *,
                                      allow_always: bool, destructive: bool,
                                      auto_confirm_seconds) -> str:
    """Lean one-line approval prompt used while a spawn_parallel status
    table is on screen.

    Same decision semantics as _arrow_approval_prompt (respects /mode's
    auto-confirm timeout, same yes/no/always outcomes) — only the render
    differs: one line instead of a multi-line body plus a full arrow-key
    selector, and the parallel table is paused/resumed around it instead
    of racing it for the terminal.
    """
    import tools as tools_mod

    _cmd = body.split("\n", 1)[0]
    _thread_name = threading.current_thread().name or ""
    _agent_label = (_thread_name.rsplit("-", 1)[-1]
                   if _thread_name.startswith("laintas-sched-") else "agent")
    _color = "#f85149" if destructive else "#e3b341"
    # Parentheses, not brackets: a literal "[y/a/n]" would be parsed by Rich
    # as an (unrecognized, zero-width) markup tag and silently vanish --
    # the exact same bracket-injection failure mode fixed earlier in the
    # AI narration line (agent_loop.py _stripped MarkupError).
    _opts = ("y" + ("/a" if allow_always else "") + "/n")
    _cmd_disp = _cmd if len(_cmd) <= 70 else _cmd[:67] + "…"

    with _parallel_approval_lock:
        tools_mod.mark_awaiting_approval(_agent_label, escape(_cmd_disp))
        _paused = tools_mod.pause_all_parallel_live_displays()
        try:
            console.print(
                f"  [bold {_color}]⚠[/bold {_color}] "
                f"[agent]{escape(_agent_label)}[/agent] wants to run: "
                f"[dim]{escape(_cmd_disp)}[/dim]  ({_opts})",
                highlight=False)
            try:
                choice = _read_single_key_choice(
                    allow_always=allow_always,
                    auto_confirm_seconds=auto_confirm_seconds)
            except (EOFError, KeyboardInterrupt):
                choice = None
            console.print(
                f"  [dim]↳ {choice or 'no'}[/dim]" if choice != "always"
                else "  [dim]↳ always (auto-approved for this session)[/dim]",
                highlight=False)
        finally:
            tools_mod.resume_all_parallel_live_displays(_paused)
            tools_mod.clear_awaiting_approval(_agent_label)
    return choice or "no"


def _blocking_approval_prompt(title: str, body: str, question: str,
                              allow_always: bool = False,
                              destructive: bool = False) -> str:
    """Pause the background stdin reader and block on an arrow-key prompt.

    Returns "yes", "no", or "always". When *allow_always* is False the "always"
    option is not offered and only "yes"/"no" can come back.

    Fails closed (returns "no") when stdin isn't a real TTY.
    """
    # While the /agents view owns the terminal, a full-screen arrow prompt
    # would fight its prompt_toolkit app for stdin. Route the decision to
    # the view's y/n approval UI instead ("always" is not offered there).
    _view_controller = _agents_view_controller()
    if _view_controller is not None:
        _current = get_current_agent()
        try:
            approved = _view_controller._request_approval(
                _current.id if _current is not None else "primary",
                "confirm", f"{title} — {question}", body)
        except Exception:
            approved = False
        return "yes" if approved else "no"

    if not sys.stdin.isatty():
        console.print(
            f"[yellow]Approval required but no interactive TTY available — denying.[/yellow]")
        return "no"

    auto_confirm_seconds = mode_manager.get_auto_confirm_timeout(
        destructive=destructive)

    # A spawn_parallel batch keeps its own live-updating status table on
    # screen. The arrow-key selector below runs its own prompt_toolkit
    # render loop with no awareness of that table's cursor bookkeeping —
    # letting both draw at once corrupts the screen (reported: exiting mid
    # parallel-agents view left duplicated/garbled frames). Route through a
    # compact one-line prompt instead: identical policy decision and /mode
    # auto-confirm timeout, just a leaner render that pauses the table
    # around itself instead of racing it.
    import tools as tools_mod
    if tools_mod._active_parallel_lives:
        return _compact_parallel_approval_prompt(
            title, body, question, allow_always=allow_always,
            destructive=destructive, auto_confirm_seconds=auto_confirm_seconds)

    # Split body into displayable lines. Callers pass plain text (no Rich
    # markup) so diff content with literal brackets renders verbatim.
    body_lines = body.split("\n")

    # Use title as the short action word ("approve", "apply", "delete").
    # Fall back to deriving from the question for legacy callers.
    _action = title if title and " " not in title else (
        question.split()[0].lower().rstrip("?") if question else "confirm")

    if destructive:
        options = ["y delete", "n cancel"]
    elif allow_always:
        options = ["y approve", "a always", "n deny"]
    else:
        options = ["y approve", "n deny"]

    _reader_was_running = bool(
        _bg_reader_thread is not None and _bg_reader_thread.is_alive())
    _stop_bg_input_reader()
    try:
        choice = _arrow_approval_prompt(
            f"{_action} — {question}", body_lines, options,
            auto_confirm_seconds=auto_confirm_seconds,
            destructive=destructive,
        )
    except (EOFError, KeyboardInterrupt):
        choice = None
    finally:
        if _reader_was_running:
            _start_bg_input_reader(get_user_message_queue(),
                                   get_user_interrupt_event())

    if choice is not None and choice.startswith("a "):
        return "always"
    if choice is not None and choice.startswith("y "):
        return "yes"
    return "no"


_APPROVAL_POLL_INTERVAL = 5  # seconds between status checks


def _request_email_approval(kind: str, summary: str, detail: str = "") -> bool:
    """Mail mode's substitute for _blocking_approval_prompt: email a
    confirm-page link and poll for the human's decision instead of blocking
    on local terminal input. Denies (fails closed) on any error, and on
    timeout — an unattended agent must never treat "no response yet" as
    permission."""
    session = load_session() or {}
    if not session.get("userId"):
        console.print("[yellow]Mail mode approval needed but not logged in — denying.[/yellow]")
        return False

    profile = get_backend_profile()
    current = get_current_agent()
    terminal = (getattr(current, "home_terminal", None) or "") if current else ""
    agent_name = (getattr(current, "name", None) or "Laintas CLI") if current else "Laintas CLI"
    interrupt_event = (
        current.abort_event if current is not None
        else get_user_interrupt_event()
    )
    headers, cookies = backend_profiles.request_auth(profile, session)

    try:
        resp = requests.post(
            f"{profile.base_url}/api/agent/request-approval",
            json={"kind": kind, "summary": summary, "detail": detail,
                  "system": "laintas_cli", "terminal": terminal, "agent": agent_name},
            headers=headers, cookies=cookies, timeout=10,
        )
    except requests.RequestException as e:
        console.print(f"[red]Mail mode: could not request email approval ({e}) — denying.[/red]")
        return False
    if resp.status_code != 200:
        console.print(f"[red]Mail mode: approval request failed (HTTP {resp.status_code}) — denying.[/red]")
        return False

    data = resp.json()
    token = data.get("token")
    timeout_s = int(data.get("expires_in") or 900)
    if not token:
        console.print("[red]Mail mode: no approval token returned — denying.[/red]")
        return False

    console.print(f"[cyan]Mail mode: emailed an approval request for {kind}. "
                  f"Waiting up to {timeout_s // 60} min for your decision…[/cyan]")
    deadline = time.time() + timeout_s
    with _safe_status("[dim]Waiting for email approval…[/dim]"):
        while time.time() < deadline:
            if interrupt_event.is_set():
                console.print("[dim]Mail approval wait cancelled.[/dim]")
                return False
            try:
                status_resp = requests.get(
                    f"{profile.base_url}/api/agent/approval/{token}/status",
                    headers=headers, cookies=cookies, timeout=10,
                )
                if status_resp.status_code == 200:
                    status = status_resp.json().get("status")
                    if status == "approved":
                        console.print("[green]Mail mode: approved by email.[/green]")
                        return True
                    if status in ("denied", "expired"):
                        console.print(f"[yellow]Mail mode: {status} by email.[/yellow]")
                        return False
            except requests.RequestException:
                pass  # transient — keep polling until the deadline
            if interrupt_event.wait(_APPROVAL_POLL_INTERVAL):
                console.print("[dim]Mail approval wait cancelled.[/dim]")
                return False
    console.print("[yellow]Mail mode: no response within the time limit — denying.[/yellow]")
    return False


def request_command_approval(command: str, reason: str) -> bool:
    """Block and ask the user to approve a command that matched a needs_approval
    policy rule. Wired as LoopDeps.request_command_approval for the local REPL."""
    import policy as _policy
    if _policy.is_delete_command(command):
        return request_file_delete_approval(
            command,
            f"DELETE via shell command\n{command}",
            reason,
        )

    if command.startswith("browser.evaluate ") and mode_manager.is_mail_mode():
        return _request_email_approval("browser.evaluate", command, reason)
    if _session_approval_state["all_commands"] or command in _session_approval_state["approved_commands"]:
        return True
    choice = _blocking_approval_prompt(
        "approve",
        f"{command}\n{reason}" if reason else command,
        "Run this command?",
        allow_always=True,
    )
    if choice == "always":
        # Remember this EXACT command, not a blanket "approve everything" —
        # a one-off approval shouldn't silently cover unrelated commands the
        # user never saw. Use `/mode act always` for a deliberate blanket
        # auto-approve instead.
        _session_approval_state["approved_commands"].add(command)
        console.print(f"[dim]↳ `{command}` auto-approved for this session.[/dim]")
        return True
    return choice == "yes"


def authorize_direct_command(command: str, cwd: str = None, *,
                             remote_source: bool = False) -> tuple[bool, str]:
    """Apply the same policy gateway to commands typed into the REPL.

    Direct commands previously bypassed policy entirely.  Returning a reason
    lets the REPL and remote-chat caller report a deterministic denial without
    executing any part of the command.

    When *remote_source* is True the command was injected from a remote
    channel (Helpwo chat -> _inject_input) rather than typed by the local
    user.  In that case ``needs_approval`` decisions are NOT silently
    allowed - the local terminal is unattended and the ``/exec`` channel
    is the proper remote path with its own approval dialog.
    """
    import policy as _policy
    decision = _policy.evaluate(command, cwd or os.getcwd(),
                                strict=remote_source)
    if decision.action == "deny":
        return False, f"Blocked by policy: {decision.reason}"
    if decision.action == "needs_approval":
        if remote_source:
            return (False,
                    f"Remote command requires approval (use the /exec "
                    f"channel): {decision.reason}")
        # Commands the USER types directly at the REPL run like a normal
        # terminal - no confirmation box - since the human is the trusted
        # actor here (the approval gate exists to supervise the AI agent).
        # Set /config confirm_direct_commands true to restore the prompt.
        if not get_runtime_config("confirm_direct_commands"):
            return True, ""
        if not request_command_approval(command, decision.reason):
            return False, f"User denied: {decision.reason}"
    return True, ""


def request_file_write_approval(path: str, diff_preview: str, reason: str) -> bool:
    """Block and ask the user to approve a file write/edit before it's applied.
    Wired as LoopDeps.request_file_write_approval for the local REPL."""
    if _session_approval_state["all_writes"] or path in _session_approval_state["approved_write_paths"]:
        return True
    # Build body: header + reason + full diff. The diff is embedded so it
    # renders (and scrolls) inside the full-screen approval UI rather than
    # being printed to scrollback beforehand.
    _body_parts = [path]
    if reason:
        _body_parts.append(reason)
    if diff_preview:
        _body_parts.append("")
        _body_parts.append(diff_preview.rstrip("\n"))
    choice = _blocking_approval_prompt(
        "apply",
        "\n".join(_body_parts),
        "Apply this change?",
        allow_always=True,
    )
    if choice == "always":
        # Remember this EXACT path, not a blanket "approve everything" — a
        # one-off approval shouldn't silently cover every other file the
        # user never saw a diff for. Use `/mode act always` for a
        # deliberate blanket auto-approve instead.
        _session_approval_state["approved_write_paths"].add(path)
        console.print(f"[dim]↳ Writes to {path} auto-approved for this session.[/dim]")
        return True
    return choice == "yes"


def request_file_delete_approval(path: str, preview: str, reason: str) -> bool:
    """Require a fresh confirmation for every destructive delete operation."""
    if mode_manager.is_mail_mode():
        detail = "\n".join(part for part in (reason, "", preview) if part)
        return _request_email_approval("fs.delete", f"Delete: {path}", detail)
    body = "\n".join(part for part in (path, reason, "", preview) if part)
    choice = _blocking_approval_prompt(
        "delete",
        body,
        "Delete this target?",
        allow_always=False,
        destructive=True,
    )
    return choice == "yes"


def _show_plan_approval_menu() -> bool:
    """Review a submitted immutable plan revision; loop completion is not readiness."""
    import plan_mode as _pm
    if not _pm.is_plan_mode():
        return False
    if _agents_view_is_active():
        # The review menu is a full-screen UI; it cannot share the terminal
        # with the /agents view. Leave the plan pending for the main CLI.
        console.print(
            "[yellow]A plan is ready for review — press Esc to leave the "
            "/agents view and approve it in the main CLI.[/yellow]")
        return False
    plan = _pm.get_current_plan()
    if not plan or plan.get("status") != "review_pending":
        return False
    approved = _review_and_approve_current_plan()
    if approved:
        console.print(
            f"[green]{symbols.OK} Revision {approved['revision']} approved. Executing exact SHA "
            f"{approved['content_sha'][:12]}…[/green]")
        return True
    return False


def _run_agent_loop_with_interrupt(deps, user_input, session, agent_state,
                                   chat_history, events_cb=None,
                                   existing_session=None,
                                   continue_thread=False):
    """Run the foreground agent loop with soft-interrupt support.

    Wraps run_agent_loop() with:
    1. Temporary SIGINT handler: first Ctrl+C → soft interrupt, second → force exit.
    2. Module-level interrupt event reset before/after each call.

    Returns the same dict as run_agent_loop().
    """
    active_agent = get_current_agent()
    primary_admitted = False
    if active_agent is not None and active_agent.role == "primary":
        primary_admitted, detail = begin_primary_run(active_agent.id)
        if not primary_admitted:
            queued, queue_detail = queue_primary_message(
                active_agent.id, user_input)
            # The outer REPL records input before calling this wrapper. The
            # running shared loop will record the queued message when it
            # consumes it, so remove the premature duplicate history entry.
            if (queued and chat_history
                    and chat_history[-1].get("role") == "user"
                    and chat_history[-1].get("content") == user_input):
                chat_history.pop()
            console.print(
                f"[dim]{queue_detail if queued else detail}[/dim]")
            return {
                "success": queued, "msg": "", "state": agent_state,
                "session": existing_session,
                "_queued_for_primary": queued,
                "exit_reason": "queued" if queued else "busy",
            }
        _interrupt_event = active_agent.abort_event
        _msg_queue = active_agent.message_queue
    else:
        _interrupt_event = get_user_interrupt_event()
        _msg_queue = get_user_message_queue()

    # Reset interrupt state from any previous call
    _interrupt_event.clear()
    # Drain stale supplementary messages
    while not _msg_queue.empty():
        try:
            _msg_queue.get_nowait()
        except queue.Empty:
            break

    # Save original SIGINT handler (the shutdown function)
    _old_sigint = signal.getsignal(signal.SIGINT)

    _last_ctrl_c = [0.0]  # closure-stored timestamp for double-press detection

    def _soft_interrupt(signum, frame):
        # During an AI run, a single Ctrl+C is swallowed so that terminal
        # copy / text-select operations don't accidentally interrupt the
        # loop. ESC remains the soft-interrupt key. Only a rapid double
        # Ctrl+C forces an exit (escape hatch).
        now = time.time()
        if now - _last_ctrl_c[0] <= 1.5:
            # Double Ctrl+C → force exit (escape hatch)
            console.print("\n[red]Force exit.[/red]")
            _stop_bg_input_reader()
            # Restore and call original handler
            signal.signal(signal.SIGINT, _old_sigint)
            _old_sigint(signum, frame)
            return
        _last_ctrl_c[0] = now
        console.print(
            "\n[dim]Ctrl+C ignored during AI run. Press Esc to interrupt "
            "(takes effect when the model starts replying or at the next "
            "step - not during thinking), or press Ctrl+C twice quickly "
            "to force exit.[/dim]")

    signal.signal(signal.SIGINT, _soft_interrupt)

    _set_run_input_state("running")
    # Background stdin reader: supplementary instructions + bare-Esc soft
    # interrupt during the run (wired straight to the interrupt event, never
    # a signal, so Esc can never kill the CLI).
    _start_bg_input_reader(_msg_queue, _interrupt_event)

    # Everything the loop prints is Agent conversation — record it into the
    # /agents mirror. Output outside a run (banner, prompts, idle chatter)
    # stays off Agent screens.
    repl_mirror.hub.start_recording()
    response = None
    run_error = ""

    # ── Auto-Pilot: heuristic classification + decomposition + auto-exec ──
    effective_input = user_input
    try:
        if get_runtime_config("auto_pilot_enabled"):
            cleaned, overridden = auto_pilot.should_override(user_input)
            if overridden:
                effective_input = cleaned
                console.print("[dim][auto-pilot] overridden by ! prefix - single agent mode[/dim]")
            else:
                has_wf = False
                try:
                    import workflow_engine as _wf
                    has_wf = _wf.get_active_workflow() is not None
                except Exception:
                    pass
                strategy = auto_pilot.classify_task(user_input, has_active_workflow=has_wf)

                # Phase 2: LLM decomposition for parallel/pipeline strategies.
                subtasks = None
                if strategy in (auto_pilot.PARALLEL_HINT, auto_pilot.PIPELINE_HINT):
                    decompose_timeout = float(get_runtime_config("auto_pilot_decompose_timeout") or 3.0)
                    subtasks = auto_pilot.decompose_task(user_input, strategy, timeout=decompose_timeout)

                # Build hint: use decomposed hint if available, else generic.
                if subtasks and len(subtasks) >= 2:
                    hint = auto_pilot.build_decomposed_hint(strategy, subtasks)
                else:
                    hint = auto_pilot.build_hint(strategy)
                    subtasks = None  # reset so Phase 3 doesn't fire

                if hint:
                    effective_input = hint + "\n\n" + user_input
                    if subtasks:
                        source = auto_pilot.get_last_decompose_source()
                        src_label = " (heuristic)" if source == "heuristic" else ""
                        console.print(f"[dim][auto-pilot] {strategy} {symbols.BULLET} {len(subtasks)} subtasks{src_label}[/dim]")
                    else:
                        console.print(f"[dim][auto-pilot] {strategy}[/dim]")

                # Phase 3: auto-execution - set pending plan for run_agent_loop.
                if subtasks and auto_pilot.should_auto_execute(
                    strategy,
                    subtasks,
                    bool(get_runtime_config("auto_pilot_auto_execute")),
                    int(get_runtime_config("auto_pilot_max_parallel") or 4),
                ):
                    mode = "parallel" if strategy == auto_pilot.PARALLEL_HINT else "chain"
                    plan = {
                        "strategy": strategy,
                        "subtasks": subtasks,
                        "mode": mode,
                    }
                    auto_pilot.set_pending_plan(plan)
                    console.print(f"[dim][auto-pilot] auto-executing {len(subtasks)} subtasks ({mode})[/dim]")
    except Exception:
        effective_input = user_input

    try:
        loop_agent_id = active_agent.id if active_agent is not None else None
        response = run_agent_loop(
            deps, effective_input, session, agent_state, chat_history,
            events_cb=events_cb,
            existing_session=existing_session,
            depth=(active_agent.depth if loop_agent_id else 0),
            agent_id=loop_agent_id,
            interrupt_event=_interrupt_event,
            message_queue=_msg_queue,
            continue_thread=continue_thread,
        )
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        repl_mirror.hub.stop_recording()
        if primary_admitted and active_agent is not None:
            if isinstance(response, dict) and "session" in response:
                active_agent.runtime_session = response.get("session")
            reply = str(
                (response or {}).get("msg")
                or ((response or {}).get("state") or {}).get("lastReply")
                or "")
            failed = bool(
                isinstance(response, dict)
                and response.get("success", True) is False
                and not _interrupt_event.is_set())
            finish_primary_run(
                active_agent.id, reply=reply,
                error=(run_error or (
                    str(response.get("exit_reason") or "incomplete")
                    if failed else "")),
                aborted=_interrupt_event.is_set())
        _set_run_input_state("finalizing")
        # Restore original SIGINT handler
        signal.signal(signal.SIGINT, _old_sigint)
        _stop_bg_input_reader()
        _interrupt_event.clear()
        _set_run_input_state("idle")

    return response


def run_execute_mode(task: str, session: dict, depth: int, session_id: str = None) -> int:
    """Non-interactive single-task execution.

    Called when laintas-cli is invoked with --execute. Runs one agent loop,
    prints the result to stdout, and returns the exit code. Local subagents run
    in-process; this entry point remains for scripts, CI, and external callers.
    """
    agent_state = {
        "shortTermMemory": "",
        "lastReply": "",
        "lastOutput": "",
    }
    chat_history = []
    if session_id:
        saved = load_resume_state(os.getcwd(), session_id=session_id)
        if saved:
            agent_state = _restore_resume_blob(saved, chat_history)
        else:
            agent_state["_session_id"] = session_id
    chat_history.append({
        "role": "user", "content": task, "input_kind": "prompt",
    })

    response = run_agent_loop(
        get_loop_deps(),
        original_input=task,
        session=session,
        state=agent_state,
        chat_history=chat_history,
        events_cb=None,
        existing_session=None,
        depth=depth,
    )

    result = response.get("msg", "")
    if result and not response.get("_history_recorded"):
        chat_history.append({"role": "assistant", "content": result})
    if session_id:
        save_resume_state(prepare_state_for_repl(response.get("state", agent_state)),
                          chat_history, os.getcwd())
    last_output = response.get("state", {}).get("lastOutput", "")
    if last_output:
        result += "\n" + last_output
    print(result)

    return 0 if response.get("success") else 1


def _parse_subtask_json(text: str):
    """Extract a JSON array of strings from LLM reply text.

    Handles common LLM formatting issues: code fences, single quotes,
    trailing commas, and surrounding prose.  Tries multiple extraction
    strategies before giving up.
    """
    import re as _re

    candidates = []

    # Strip markdown code fences if present.
    m = _re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, _re.DOTALL)
    if m:
        candidates.append(m.group(1))

    # Find JSON array - greedy first (handles ] inside strings),
    # then non-greedy (handles multiple arrays in text).
    m = _re.search(r"\[.*\]", text, _re.DOTALL)
    if m:
        candidates.append(m.group(0))
    m = _re.search(r"\[.*?\]", text, _re.DOTALL)
    if m:
        candidates.append(m.group(0))

    for candidate in candidates:
        # Standard JSON parse.
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list) and all(isinstance(s, str) for s in parsed):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Fix single quotes -> double quotes.
        try:
            fixed = candidate.replace("'", '"')
            parsed = json.loads(fixed)
            if isinstance(parsed, list) and all(isinstance(s, str) for s in parsed):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Remove trailing commas before ] or }.
        try:
            fixed = _re.sub(r",\s*([}\]])", r"\1", candidate)
            parsed = json.loads(fixed)
            if isinstance(parsed, list) and all(isinstance(s, str) for s in parsed):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # Last resort: extract individual quoted strings.
    strings = _re.findall(r'["\']([^"\']{4,})["\']', text)
    if strings and len(strings) >= 2:
        return strings

    return None


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Laintas CLI — Autonomous AI agent")
    parser.add_argument("--version", "-V", action="version",
                        version=f"laintas-cli {__version__}")
    parser.add_argument("--name", type=str, help="Set agent name (shows in Helpwo AGNETS)")
    parser.add_argument(
        "--backend", type=str,
        help="Custom backend URL (enters external/unmetered mode; Laintas credentials are stripped)",
        default=None)
    parser.add_argument("--laintas", type=str, help=argparse.SUPPRESS, default=None)
    parser.add_argument("--execute", "-e", type=str, default=None,
                        help="Execute a single task non-interactively and exit")
    parser.add_argument("--session-id", type=str, default=None,
                        help="Persist/resume --execute context under this logical session id")
    parser.add_argument("--depth", "-d", type=int, default=0,
                        help="Nesting depth (0=user terminal, 1+=sub-agent)")
    parser.add_argument("--simple-prompt", action="store_true", default=False,
                        help="Use plain input() instead of prompt_toolkit")
    parser.add_argument("--monitor-only", action="store_true", default=False,
                        help="Start as a remote executor only — register, heartbeat, "
                             "and poll HelpwoAI for tasks. No local REPL.")
    parser.add_argument("--resume", action="store_true", default=False,
                        help="Resume the saved conversation for this directory on startup")
    parser.add_argument("--continue", dest="continue_session", action="store_true", default=False,
                        help="Alias for --resume (session continuation)")
    parser.add_argument("--agent-id", type=str, default=None,
                        help="Pre-assigned agent id (used by sub-terminals)")
    parser.add_argument("--agent-name", type=str, default=None,
                        help="Pre-assigned agent display name (used by sub-terminals)")
    parser.add_argument("--agent-role", type=str, default=None,
                        choices=["pool", "deployed", "primary", "subagent"],
                        help="Role for the pre-assigned agent (used by sub-terminals)")
    parser.add_argument("--terminal-name", type=str, default=None,
                        help="Name of the terminal this sub-process represents")
    parser.add_argument("--parent-terminal", type=str, default=None,
                        help="Name of the parent terminal that spawned this sub-process")
    parser.add_argument("--parent-agent-id", type=str, default=None,
                        help="Agent id of the parent process that spawned this sub-process")
    parser.add_argument("--remote-parent-id", type=str, default=None,
                        help="Helpwo backend agent id of the primary CLI that owns this sub-terminal")
    parser.add_argument("--connect", action="store_true", default=False,
                        help="Hand this sub-terminal over to Helpwo at startup (internal; used by term-new)")
    args = parser.parse_args()

    # Apply environment overrides
    if args.backend:
        os.environ["LAINTAS_BACKEND"] = args.backend
    if args.laintas:
        if args.laintas.rstrip("/") != LAINTAS_BASE:
            parser.error("custom authentication origins are no longer allowed")

    # All REPL instances use full-color console — sub-terminals are full
    # laintas-cli instances and should look identical to the main terminal.

    # Initialize unified home directory and auto-migrate old layout
    paths.ensure_home()
    backend_profiles.ensure_template()
    if migrate_mod.needs_migration():
        console.print("[dim]Migrating to new directory layout (~/.laintas/)...[/dim]")
        migrate_mod.migrate_all(verbose=True)

    # Ensure .laintas/ project files exist in cwd
    ensure_files_exist()
    evolution_lab.reconcile_workspace()

    # Hint (never auto-overwrite — see ensure_files_exist) when the saved
    # cli.prop no longer matches what this version would generate, so an
    # upgraded default prompt template doesn't silently go unused.
    if args.depth == 0:
        try:
            _prop_path = paths.project_file(paths.CWD_CLI_PROP)
            if (_prop_path.exists()
                    and _prop_path.read_text(encoding="utf-8") != generate_cli_prop_template()):
                console.print(
                    "[dim]Your prompt template differs from this version's default. "
                    "Run [bold]/reload[/bold] to sync (this deletes .laintas/ overrides).[/dim]")
        except OSError:
            pass

    # Load or create config
    config = load_config()
    agent_name = args.name or config.get("agentName", socket.gethostname())
    # Restore display/input preferences for this logical terminal. Validation
    # remains centralized in agent_loop; corrupt or obsolete values are ignored.
    for _key, _value in terminal_preferences.get_ui_preferences().items():
        try:
            set_runtime_config(_key, _value)
        except (KeyError, TypeError, ValueError):
            continue
    _apply_ui_theme(str(get_runtime_config("theme") or "dark"))
    # Model/provider selection is terminal-local: it survives a relaunch in
    # this shell without affecting another concurrently open terminal.
    _update_status_cache(model=get_selected_model())

    # Official mode uses the Laintas account. Custom/local backends are a
    # separate trust and billing domain and must not require or receive it.
    _active_backend = get_backend_profile()
    session = (ensure_auth() or {}) if _active_backend.sends_laintas_credentials else {}
    if args.depth == 0 and not _active_backend.sends_laintas_credentials:
        console.print(
            f"[yellow]Backend mode: {_active_backend.kind} "
            f"({_active_backend.base_url}) — external/unmetered; "
            "Laintas credentials are not sent.[/yellow]")

    # Project extensions receive a narrow inference gateway, never the raw
    # authenticated session. The normal backend path remains authoritative for
    # official authentication, model authorization and billing.
    _extension_runtime = extension_runtime.get_runtime()
    _extension_runtime.configure(
        console=console,
        reserved_commands=[
            name for spec in COMMAND_SPECS for name in spec.all_names
        ],
        backend_callback=lambda message, system_prompt="", **options: call_backend_stream(
            session=session, message=message, system_prompt=system_prompt,
            current_path=os.getcwd(), history=options.get("history") or [],
            messages=options.get("messages"), lang=options.get("lang", "EN"),
            tools_enabled=bool(options.get("tools_enabled", False)),
        ),
    )
    for _ext_name, _ext_ok, _ext_message in evolution_lab.load_active_extensions(
            _extension_runtime):
        if not _ext_ok and args.depth == 0:
            console.print(f"[yellow]Extension {_ext_name}: {_ext_message}[/yellow]")

    # ── Non-interactive execution mode ──
    if args.execute:
        if (_active_backend.sends_laintas_credentials
                and not session.get("userId")):
            console.print("[red]Authentication required for --execute mode. Use /login first.[/red]")
            sys.exit(1)
        sys.exit(run_execute_mode(args.execute, session, args.depth, args.session_id))

    # ── Simple prompt (PTY subprocess mode) ──
    _use_simple_prompt = args.simple_prompt or not sys.stdin.isatty()
    if _use_simple_prompt:
        global _IN_SUB_TERMINAL
        _IN_SUB_TERMINAL = True

    # Show banner (skip in child terminals to avoid Rich output in PTY)
    if args.depth == 0:
        show_banner(agent_name, session if session else None)
        # Best-effort, short-timeout update check — never blocks startup for
        # more than ~1.5s and stays silent on any network/parse failure.
        try:
            import updater as _updater_check
            _remote_ver = _updater_check.check_update_available()
            if _remote_ver:
                console.print(
                    f"[green]Update available: v{_remote_ver}[/green] "
                    f"[dim](run /v update)[/dim]")
        except Exception:
            pass

        # Mail mode's watcher runs for the whole process lifetime and
        # self-gates on the active mode each poll — started once here rather
        # than wired into every /mode switch branch. No-ops until /mode mail
        # is actually active.
        if session:
            _start_mail_watcher(session)

    # Register as remote agent (only if authenticated)
    agent_registry = AgentRegistry()
    _session_start_cwd = os.getcwd()
    agent_state = {
        "shortTermMemory": "",
        "lastReply": "",
        "lastOutput": "",
    }
    def _active_task_export() -> list[dict]:
        return task_manager.export_active_tasks(
            cwd=_session_start_cwd,
            session_id=str(agent_state.get("_session_id") or "") or None)

    chat_history = []
    current_live_session = None
    # A normal launch starts clean. Live state is archived for explicit
    # /resume; it is never injected into the new session automatically.
    if args.depth == 0:
        _explicit_startup_resume = bool(args.resume or args.continue_session)
        _previous_live_session = session_store.load_current_session(
            _session_start_cwd)
        _session_warning = session_store.consume_last_error()
        if _session_warning:
            console.print(f"[yellow]{_session_warning}[/yellow]")
        if _previous_live_session:
            _prev_ch = _previous_live_session.get("chat_history") or []
            _prev_user_turns = [m for m in _prev_ch if isinstance(m, dict) and m.get("role") == "user"]
            if _prev_user_turns:
                save_resume_checkpoint(
                    _previous_live_session.get("state")
                    or _previous_live_session.get("agent_state") or {},
                    _prev_ch,
                    _session_start_cwd,
                )
            session_store.close_session(_previous_live_session)

        if not _explicit_startup_resume:
            try:
                _reset_fresh_session_context(_session_start_cwd)
            except Exception as exc:
                console.print(
                    f"[red]Could not reset persisted session context: {exc}[/red]")

        current_live_session = session_store.create_session(
            _session_start_cwd, agent_state, chat_history)
        handle_meta_command._current_live_session = current_live_session

        # Close stale recovery-journal admissions without injecting their text.
        # The archived live checkpoint above remains available via /resume.
        _incomplete = event_log.last_incomplete_task()
        if (_incomplete
                and not event_log.owner_process_is_alive(_incomplete)):
            event_log.acknowledge_incomplete(
                _incomplete, reason="fresh_session_started")
        # Full-fidelity resume blob: explicit restore by flag or slash command.
        # Without a flag, just hint so a fresh session stays fresh.
        _resume_blob = load_resume_state(_session_start_cwd)
        if _resume_blob and _resume_blob.get("chat_history"):
            _n_turns = _resume_turn_count(_resume_blob)
            _ago = _format_time_ago(_resume_blob.get("timestamp", 0))
            if args.resume or args.continue_session:
                _selected_resume = _choose_resume_blob(_session_start_cwd, "latest")
                if _selected_resume:
                    if current_live_session:
                        session_store.close_session(current_live_session)
                    agent_state = _restore_resume_blob(_selected_resume, chat_history)
                    current_live_session = session_store.create_session(_session_start_cwd, agent_state, chat_history)
                    handle_meta_command._current_live_session = current_live_session
                    console.print(
                        f"[green]Resumed previous session in this directory "
                        f"({_resume_turn_count(_selected_resume)} turn(s), "
                        f"{_format_time_ago(_selected_resume.get('timestamp', 0))}).[/green]"
                    )
                    _print_resume_transcript(_selected_resume, 20)
            elif not current_live_session or current_live_session.get("turn_count", 0) == 0:
                console.print(
                    f"[dim]Previous session in this directory "
                    f"({_n_turns} turn(s), {_ago}). Type [bold]/resume[/bold] "
                    f"or start with [bold]--resume[/bold] to continue.[/dim]"
                )
        elif args.resume or args.continue_session:
            console.print("[yellow]No saved session for this directory.[/yellow]")

        # Bind /continue to the exact in-memory objects restored at startup.
        # Without this, the command mutates a deserialized session copy and the
        # next REPL prompt overwrites its result with stale local state.
        handle_meta_command._last_agent_state = agent_state
        handle_meta_command._last_chat_history = chat_history
        handle_meta_command._last_original_input = (
            (current_live_session or {}).get("last_original_input")
            or (current_live_session or {}).get("last_user_input")
            or agent_state.get("objective")
        )
        handle_meta_command._last_deps = get_loop_deps()
        handle_meta_command._last_session = session
        handle_meta_command._last_events_cb = None
        handle_meta_command._last_existing_session = None

    # Stash REPL state callbacks + terminal identity so /connect (now or later)
    # can register this instance with full context.
    agent_registry.depth = args.depth
    agent_registry._state_cb = lambda: agent_state
    agent_registry._chat_cb = lambda: chat_history
    if args.depth > 0:
        agent_registry.parent_remote_id = args.remote_parent_id or None
        agent_registry.terminal_meta = {
            "name": args.terminal_name or f"term-pid{os.getpid()}",
            "command": "laintas-cli",
            "createdAt": time.time(),
            "createdBy": args.parent_terminal or "term0",
        }

    if session.get("userId") or not _active_backend.sends_laintas_credentials:
        if args.depth == 0 and args.monitor_only:
            # Monitor mode IS the remote-executor role — it must be online.
            agent_registry.register(session, name=agent_name)
            agent_registry.start_heartbeat()
            agent_registry.start_message_poll(
                lambda: agent_state,
                lambda: chat_history,
            )
        elif args.connect:
            # --connect (any depth; used by Helpwo's term-new for sub-terminals).
            connect_terminal_to_helpwo(agent_registry, session)
        elif args.depth == 0:
            # Two-end handshake: NO auto-link. The primary CLI goes online in
            # Helpwo only when the user runs /helpwo here.
            console.print("[dim]Not connected to Helpwo. Run [bold]/helpwo[/bold] to expose this "
                          "CLI (its shell + this folder) as a runtime environment there.[/dim]")
        else:
            console.print("[dim]This sub-terminal isn't linked to Helpwo yet.[/dim]")

    # PTY session managed at REPL level (must be before shutdown for nonlocal)
    interactive_session = None

    # Register the primary agent.
    # If launched as a sub-terminal with an explicit identity, register that
    # agent (role=deployed) and tag it with the parent context. Otherwise
    # register the conventional "primary" REPL agent.
    if args.agent_id:
        sub_role = args.agent_role or "deployed"
        sub = register_agent(name=args.agent_id,
                             depth=args.depth,
                             parent_id=args.parent_agent_id,
                             role=sub_role,
                             load_existing=True)
        if args.agent_name and args.agent_name != args.agent_id:
            sub.name = args.agent_name
        sub.parent_terminal = args.parent_terminal or "term0"
        if args.terminal_name:
            sub.deployment_terminal = args.terminal_name
            sub.home_terminal = args.terminal_name
            sub.stationed_terminal = args.terminal_name
        # A pre-assigned child process owns this employee's persisted context;
        # do not overwrite it with the empty REPL locals created above.
        agent_state = sub.state
        chat_history = sub.chat_history
        set_current_agent_id(args.agent_id)
    else:
        primary = register_agent(name="primary", depth=0, role="primary",
                                 load_existing=True)
        # The REPL and Agents Mode are two views over these exact objects.
        # Never let the registry retain a restored copy while the REPL mutates
        # different state/history instances.
        primary.state = agent_state
        primary.chat_history = chat_history
        primary.runtime_session = interactive_session
        primary.deployment_terminal = "term0"
        primary.stationed_terminal = "term0"
        primary.home_terminal = "term0"
        primary.parent_terminal = None
        set_current_agent_id("primary")

    def _on_terminal_agent_finished(agent_info, _result):
        try:
            shutdown.interrupting = False
        except (NameError, UnboundLocalError):
            pass
        if agent_info.id == "primary" and args.depth == 0:
            try:
                handle_meta_command._last_agent_state = agent_state
                handle_meta_command._last_chat_history = chat_history
                if current_live_session:
                    session_store.sync_runtime(
                        current_live_session, agent_state, chat_history,
                        cwd=_session_start_cwd,
                        objective=agent_state.get("objective"),
                        tasks=_active_task_export())
                save_resume_state(
                    agent_state, chat_history, _session_start_cwd)
            except Exception as exc:
                add_debug_log(DebugEntry(
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    reply=f"terminal Agent persistence failed: {exc}",
                    error=True))

    # Same-terminal shared Agent rendering was removed. Keep the coordinator
    # dormant for compatibility with persisted Agent metadata; natural
    # language input follows the original single foreground-Agent REPL.
    _sync_status_context()

    # Register the trigger-wake callback so that trigger events arriving
    # for an idle agent auto-start a lightweight assignment to drain the
    # inbox. Without this, triggers fired while the agent is idle sit
    # unseen until a user manually starts a new assignment.
    def _trigger_wake_cb(agent_id: str, msg: dict) -> None:
        msg_type = msg.get("type") or msg.get("kind") or ""
        term_name = msg.get("terminal") or "?"
        if msg_type == "terminal.exit":
            rc = msg.get("returncode")
            task = (f"Terminal '{term_name}' exited"
                    f" (returncode={rc}). Check its output and take any"
                    f" needed follow-up action.")
        else:
            line = msg.get("line") or msg.get("text") or ""
            task = (f"Terminal '{term_name}' triggered watch pattern"
                    f" {msg.get('pattern', '')!r}: {line}. Inspect and"
                    f" respond as needed.")
        try:
            ok, _message, _assignment = start_agent_assignment(
                agent_id, task, get_loop_deps())
            if not ok:
                console.print(
                    f"[dim]trigger-wake: could not start assignment for"
                    f" '{agent_id}'[/dim]")
        except Exception as _exc:
            console.print(
                f"[dim]trigger-wake error for '{agent_id}': {_exc}[/dim]")

    set_trigger_wake_callback(_trigger_wake_cb)

    # ── Phase 2: Register LLM decomposition callback for auto-pilot ──
    def _decompose_cb(task: str, strategy: str, timeout: float):
        """Decompose a task into subtasks via backend LLM with timeout."""
        try:
            _session = load_session() or {}
            if strategy == auto_pilot.PARALLEL_HINT:
                mode_word = "independent"
            else:
                mode_word = "sequential"
            system_prompt = (
                "You are a task decomposition assistant. Break the given "
                f"task into 2-4 {mode_word} subtasks. Return ONLY a JSON "
                "array of strings, no explanation. "
                'Example: ["subtask 1", "subtask 2", "subtask 3"]'
            )
            # Temporarily lower max_tokens for the decomposition call.
            from agent_loop import get_runtime_config as _grc, set_runtime_config as _src
            _orig_max = _grc("max_tokens")
            _decompose_max = int(_grc("auto_pilot_decompose_max_tokens") or 500)
            _src("max_tokens", _decompose_max)
            try:
                result = call_backend_stream(
                    _session,
                    message=task,
                    system_prompt=system_prompt,
                    current_path=os.getcwd(),
                    tools_enabled=False,
                )
            finally:
                _src("max_tokens", _orig_max)
            reply = (result or {}).get("reply", "")
            if not reply:
                return None
            # Robust JSON extraction: handle code blocks + surrounding text.
            return _parse_subtask_json(reply)
        except Exception:
            return None

    auto_pilot.set_decompose_callback(_decompose_cb)

    # Load user skills from ~/.laintas/skills. Failures are surfaced
    # but never block startup.
    try:
        _skill_results = skills_mod.load_all()
        _failed = [(n, m) for n, ok, m in _skill_results if not ok]
        if _failed and args.depth == 0:
            for n, m in _failed:
                console.print(f"[yellow]skill {n}: {m}[/yellow]")
    except Exception as _e:
        console.print(f"[yellow]skill loader error: {_e}[/yellow]")

    # Connect MCP servers (if any configured + mcp SDK installed). Best-effort.
    if args.depth == 0:
        try:
            if not _get_mcp_mod().MCP_AVAILABLE:
                if _get_mcp_mod().CONFIG_PATH.exists():
                    console.print(f"[dim yellow]mcp config present but SDK missing: {_get_mcp_mod().MCP_IMPORT_ERROR}[/dim yellow]")
                    console.print("[dim]Install with: pip install mcp[/dim]")
            else:
                _mcp_results = _get_mcp_mod().get_manager().connect_all_enabled()
                for n, ok, m in _mcp_results:
                    if n == "(none)":
                        continue
                    style = "green" if ok else "yellow"
                    console.print(f"[{style}]mcp {n}: {m}[/{style}]")
        except Exception as _e:
            console.print(f"[yellow]mcp connect error: {_e}[/yellow]")

    _quit_checkpoint_saved = False

    # Setup graceful shutdown
    def shutdown(signum=None, frame=None, *, input_closed=False):
        # A disconnected SSH/PTY leaves fd 0/1/2 pointing at a dead terminal.
        # Reads then fail immediately with EIO and writes can fail too. Silence
        # only that dead output before cleanup; normal Ctrl+D keeps its message.
        if input_closed and _stdin_terminal_disconnected():
            _silence_disconnected_terminal_output()
        # Kill any live-updating parallel-agents display first. rich's Live
        # runs a background thread that repaints ~8x/sec regardless of what
        # the main thread does; if it's still alive while this function
        # starts printing its own shutdown messages, the two race for the
        # terminal and the screen ends up looking corrupted/hung until the
        # user force-exits (reported: exiting mid parallel-agents view).
        try:
            tools_mod.stop_all_parallel_live_displays()
        except Exception:
            pass
        # Reentry guard: a second Ctrl+C while the first shutdown is cleaning
        # up can double-close terminals / agents / browser sessions and
        # corrupt in-memory state. Hard-exit on the second signal.
        if getattr(shutdown, "_in_progress", False):
            console.print("\n[red]Forced exit.[/red]")
            # os._exit() skips every finally/atexit hook, so anything that
            # left the tty in raw mode or the cursor hidden (a Live display,
            # an InteractiveSession PTY takeover) never gets restored. Reset
            # the essentials directly so the shell is usable afterward.
            try:
                console.show_cursor(True)
            except Exception:
                pass
            try:
                _fd = sys.stdin.fileno()
                _attrs = termios.tcgetattr(_fd)
                _attrs[3] |= (termios.ECHO | termios.ICANON | termios.ISIG)
                termios.tcsetattr(_fd, termios.TCSANOW, _attrs)
            except Exception:
                pass
            os._exit(1)
        shutdown._in_progress = True
        if _agents_view_is_active():
            # Reclaim stdout so shutdown messages are visible, and ask the
            # view's app to close instead of leaving the tty in raw mode.
            _view = _agents_view_controller()
            _exit_agents_view()
            try:
                if _view is not None and _view.app is not None:
                    _view.app.exit()
            except Exception:
                pass
        if (_terminal_agents.configured
                and _terminal_agents.snapshot().get("running_count")
                and not input_closed
                and not getattr(shutdown, "interrupting", False)):
            if _terminal_agents.abort_foreground():
                shutdown.interrupting = True
                console.print(
                    "\n[yellow]Foreground Agent interrupt requested. "
                    "Press Ctrl+C again to exit.[/yellow]")
                return
        if args.depth > 0:
            # Sub-terminal CLI: its console may already be a dead PTY (parent
            # closed it / tmux window killed), where console.print and the
            # graceful save path can wedge. Unregister from Helpwo first,
            # best-effort cleanup, then hard-exit.
            try:
                agent_registry.unregister()
            except Exception:
                pass
            try:
                stop_trigger_scanner()
                close_all_terminals()
                close_all_agents()
            except Exception:
                pass
            os._exit(0)
        console.print("\n[yellow]Shutting down...[/yellow]")
        if args.depth == 0 and not _quit_checkpoint_saved:
            save_session_snapshot(agent_state, chat_history, _session_start_cwd)
            save_resume_state(agent_state, chat_history, _session_start_cwd)
            if current_live_session:
                session_store.sync_runtime(
                    current_live_session, agent_state, chat_history,
                    cwd=_session_start_cwd,
                    tasks=_active_task_export(),
                )
        stop_trigger_scanner()
        _terminal_agents.close()
        close_all_terminals()
        close_all_agents()
        browser_mod.close_all_browser_sessions()
        try:
            _get_mcp_mod().get_manager().shutdown()
        except Exception:
            pass
        try:
            mgr = getattr(agent_registry, "_webrtc", None)
            if mgr and mgr is not False and hasattr(mgr, "close"):
                mgr.close()
        except Exception:
            pass
        nonlocal interactive_session
        if interactive_session:
            interactive_session.close()
        agent_registry.unregister()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    # tmux kill-window (parent's unregister_terminal / Helpwo term-close)
    # delivers SIGHUP — unregister from Helpwo before dying instead of
    # leaving a stale agent until the 60s heartbeat timeout.
    signal.signal(signal.SIGHUP, shutdown)

    # Signals only help when the main thread can run them. Monitor-only is the
    # one mode meant to outlive its terminal, so it opts out; every other mode
    # gets a watchdog that exits if the terminal it was started from is gone.
    if not args.monitor_only:
        _install_terminal_watchdog(shutdown)

    # ── Create term0: a real persistent bash session ──
    # Direct user terminal commands route through this via marker-poll.
    # Agent shell.exec stays an isolated synchronous subprocess; deployment is
    # lifecycle ownership and never grants a shared PTY command channel.
    # Created for ALL interactive REPL instances (depth 0 and depth > 0),
    # so sub-terminals have the same capabilities as the main terminal.
    _term0_session = None
    try:
        _term0_session = InteractiveSession(
            DEFAULT_SHELL, timeout=0, stream_output=False, persistent=True)
        _term0_session.start()
        time.sleep(0.08)
        if _term0_session.is_alive():
            _term0_session.read_output(timeout=0.1)
        register_terminal(_term0_session, DEFAULT_SHELL, 0, name="term0")
        # Complete the local deployment binding only after this process's root
        # terminal is live. A remotely named child terminal is still `term0`
        # inside its own process; remote tree metadata remains in terminal_meta.
        local_agent_id = args.agent_id or "primary"
        if not station_agent(local_agent_id, "term0"):
            raise RuntimeError(
                f"could not bind agent '{local_agent_id}' to local term0")
    except Exception as _e:
        console.print(f"[dim yellow]term0 bash session init failed: {_e}[/dim yellow]")
        _term0_session = None

    # ── Monitor-only mode (no interactive REPL) ──
    # Runs purely as a remote executor: heartbeat + /poll loop already
    # started above when the agent registered. Here we just park the main
    # thread until SIGINT/SIGTERM. See HELPWO_INTEGRATION_PLAN.md phase D.
    if args.monitor_only:
        if (_active_backend.sends_laintas_credentials
                and not session.get("userId")):
            console.print("[red]--monitor-only requires authentication. Run /login first.[/red]")
            sys.exit(1)
        if not agent_registry.agent_id:
            console.print("[red]--monitor-only requires successful agent registration. "
                          "Check backend URL and credentials.[/red]")
            sys.exit(1)
        console.print(Panel(
            f"[green]Monitor-only mode active[/green]\n"
            f"Agent: [bold]{agent_registry.agent_name}[/bold] ({agent_registry.agent_id})\n"
            f"Listening for remote exec/query/delegate requests…\n"
            f"[dim]Ctrl+C to exit.[/dim]",
            title="laintas-cli monitor",
            border_style="green",
        ))
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            shutdown()

    # Main interactive loop

    while True:
        # ── term0 health check ──
        _ensure_term0_alive()
        try:
            item = _get_input(str(os.getcwd()))
        except KeyboardInterrupt:
            shutdown()
        except EOFError:
            shutdown(input_closed=True)

        if isinstance(item, _InjectedInput):
            user_input = item.text
            injected_done = item.done
            _is_dialogue = item.kind == "dialogue"
        else:
            user_input = item
            injected_done = None
            _is_dialogue = False

        if not user_input:
            # Don't set injected_done here — the empty input might be from
            # a prompt interruption by a remote message. If a remote message
            # is queued, the next iteration will pick it up and process it
            # before setting injected_done.
            continue

        # A task submitted through /agents may have completed after that view
        # closed.  Pull its CLI-owned PTY/session back into the foreground
        # before routing the next outer prompt.
        _runtime_owner = get_current_agent()
        if (_runtime_owner is not None
                and _runtime_owner.role == "primary"
                and _runtime_owner.status not in {
                    "queued", "running", "thinking", "waiting"}):
            interactive_session = _runtime_owner.runtime_session

        # Ctrl+D → exit
        if user_input.strip() == "/exit" and not _is_dialogue:
            if args.depth == 0:
                save_session_snapshot(agent_state, chat_history, _session_start_cwd)
                save_resume_state(agent_state, chat_history, _session_start_cwd)
                _quit_checkpoint_saved = True
                if current_live_session:
                    session_store.sync_runtime(
                        current_live_session, agent_state, chat_history,
                        cwd=_session_start_cwd,
                    tasks=_active_task_export(),
                    )
                    session_store.close_session(current_live_session)
                    handle_meta_command._current_live_session = None
            stop_trigger_scanner()
            close_all_terminals()
            browser_mod.close_all_browser_sessions()
            if interactive_session:
                interactive_session.close()
            agent_registry.unregister()
            clear_session()
            console.print("[green]Logged out. Goodbye![/green]")
            if injected_done is not None:
                injected_done.set()
            return

        # /resume — restore this directory's saved conversation (full-fidelity).
        # Like mainstream agent CLIs, this does not consume/delete the saved
        # session; it remains available for future launches until overwritten.
        # /resume [N|all|latest] — the argument controls how many messages to
        # echo after restoring (N, default 20; 0 = silent; "all" = full). When
        # multiple sessions are saved, a full-screen picker chooses which one;
        # "latest" skips the picker and restores the newest directly.
        _resume_parts = user_input.strip().split(maxsplit=1)
        if (_resume_parts and _resume_parts[0].lower() == "/resume"
                and args.depth == 0 and not _is_dialogue):
            _resume_arg = (_resume_parts[1].strip().lower()
                           if len(_resume_parts) > 1 else "")
            _echo_limit = 20
            _pick_latest = False
            if _resume_arg in ("latest", "last"):
                _pick_latest = True
            elif _resume_arg == "all":
                _echo_limit = None
            elif _resume_arg.isdigit():
                _echo_limit = int(_resume_arg)
            elif _resume_arg and _resume_arg not in ("-s", "--show"):
                console.print(f"[red]Invalid /resume argument: {_resume_arg}[/red]")
                console.print("[dim]Usage: /resume [N|all|latest]  "
                              "(N = messages to echo, default 20, 0 = silent)[/dim]")
                if injected_done is not None:
                    injected_done.set()
                continue
            _choices = _resume_choices(_session_start_cwd)
            if not _choices:
                _blob = None
                console.print("[yellow]No saved session to resume in this directory.[/yellow]")
            elif _pick_latest or len(_choices) == 1 or not sys.stdin.isatty():
                # Direct restore of the newest session (no picker).
                _blob = _choices[0]
            else:
                # Multiple saved sessions — open the full-screen picker.
                _blob = show_resume_picker(_session_start_cwd)
            if _blob and not _blob.get("chat_history"):
                console.print("[yellow]Saved session has no conversation to resume.[/yellow]")
            elif _blob:
                if current_live_session:
                    session_store.close_session(current_live_session)
                agent_state = _restore_resume_blob(_blob, chat_history)
                current_live_session = session_store.create_session(_session_start_cwd, agent_state, chat_history)
                handle_meta_command._current_live_session = current_live_session
                handle_meta_command._last_agent_state = agent_state
                handle_meta_command._last_chat_history = chat_history
                handle_meta_command._last_original_input = (
                    current_live_session.get("last_original_input")
                    or current_live_session.get("last_user_input")
                    or current_live_session.get("objective")
                )
                _n = _resume_turn_count(_blob)
                _ago = _format_time_ago(_blob.get("timestamp", 0))
                console.print(
                    f"[green]Resumed previous session in this directory "
                    f"({_n} turn(s), {_ago}).[/green]"
                )
                _print_resume_transcript(_blob, _echo_limit)
            if injected_done is not None:
                injected_done.set()
            continue

        _new_parts = user_input.strip().split(maxsplit=1)
        if (_new_parts and _new_parts[0].lower() in _NEW_SESSION_COMMANDS
                and args.depth == 0 and not _is_dialogue):
            _active_work = workgraph.get_active_work(cwd=_session_start_cwd)
            if (_active_work and _active_work.get("status")
                    not in {"COMPLETED", "CANCELLED", "FAILED"}):
                _choice = _blocking_approval_prompt(
                    "Start a new session",
                    f"Active WorkGraph: {_active_work['id']}\n"
                    f"Objective: {_active_work['objective']}\n"
                    f"Status: {_active_work['status']}\n\n"
                    "The work will be preserved for /resume but detached from the new session.",
                    "Detach active work and start a new session?",
                    allow_always=False,
                )
                if _choice != "yes":
                    console.print("[yellow]New session cancelled.[/yellow]")
                    if injected_done is not None:
                        injected_done.set()
                    continue
            try:
                _reset_fresh_session_context(_session_start_cwd)
            except Exception as exc:
                console.print(
                    f"[red]Could not detach the previous session context: {exc}[/red]")
                if injected_done is not None:
                    injected_done.set()
                continue
            if current_live_session:
                session_store.close_session(current_live_session)
            chat_history.clear()
            agent_state = {
                "shortTermMemory": "",
                "lastReply": "",
                "lastOutput": "",
            }
            current_live_session = session_store.create_session(_session_start_cwd, agent_state, chat_history)
            handle_meta_command._current_live_session = current_live_session
            handle_meta_command._last_agent_state = agent_state
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = None
            console.print("[green]Started a new session.[/green]")
            if injected_done is not None:
                injected_done.set()
            continue

        _is_top_level_quit = (
            args.depth == 0
            and not _is_dialogue
            and len(user_input.strip().split()) == 1
            and user_input.strip().split()[0].lower() in ("/q", "/quit")
        )
        if _is_top_level_quit:
            save_session_snapshot(agent_state, chat_history, _session_start_cwd)
            _checkpoint = save_resume_checkpoint(agent_state, chat_history, _session_start_cwd)
            _quit_checkpoint_saved = True
            if current_live_session:
                session_store.sync_runtime(
                    current_live_session, agent_state, chat_history,
                    cwd=_session_start_cwd,
                    tasks=_active_task_export(),
                )
                session_store.close_session(current_live_session)
                handle_meta_command._current_live_session = None
            if _checkpoint:
                console.print(
                    f"[dim]Saved resume checkpoint: "
                    f"{_format_time_ago(_checkpoint.get('timestamp', 0))} {symbols.BULLET} "
                    f"{_checkpoint.get('title', 'Untitled session')}[/dim]"
                )

        # Check for meta commands. Dialogue input from the /agents view is a
        # conversation message, never a terminal command — even when it
        # starts with "/" — so it skips meta dispatch entirely.
        if user_input.startswith("/") and not _is_dialogue:
            should_exit = handle_meta_command(user_input, agent_registry, session, interactive_session)
            current_live_session = getattr(handle_meta_command, '_current_live_session', current_live_session)
            # Full-screen commands such as /agents may reuse or replace the
            # REPL-owned interactive PTY. Keep the local owner synchronized.
            interactive_session = getattr(
                handle_meta_command, '_last_existing_session',
                interactive_session)
            # /agent <id-or-name> switches the REPL's current Agent focus
            # without redeploying. Rebind the local state/history references
            # to the target Agent's existing objects so the next user turn
            # drives that Agent's conversation. Terminal ownership is
            # untouched: direct commands still route through term0.
            if getattr(handle_meta_command, '_agent_switch_performed', False):
                agent_state = handle_meta_command._last_agent_state
                chat_history = handle_meta_command._last_chat_history
                handle_meta_command._agent_switch_performed = False
                if args.depth == 0:
                    save_resume_state(agent_state, chat_history, _session_start_cwd)
            if should_exit:
                # /q already finalized this logical session as a checkpoint.
                # Writing a generic autosave here used to create a duplicate
                # picker entry with the same conversation and a different id.
                if args.depth == 0 and not _is_top_level_quit:
                    save_session_snapshot(agent_state, chat_history, _session_start_cwd)
                    save_resume_state(agent_state, chat_history, _session_start_cwd)
                    if current_live_session:
                        session_store.sync_runtime(
                            current_live_session, agent_state, chat_history,
                            cwd=_session_start_cwd,
                    tasks=_active_task_export(),
                        )
                if interactive_session:
                    interactive_session.close()
                if injected_done is not None:
                    injected_done.set()
                return
            if injected_done is not None:
                injected_done.set()
            continue

        # A selected Agent has one authoritative execution regardless of which
        # view started it. Route input into that run instead of starting a
        # second loop against the same state/history.
        _shared_agent = get_current_agent()
        if (_shared_agent is not None
                and _shared_agent.status in {
                    "queued", "running", "thinking", "waiting"}):
            if _shared_agent.role == "primary":
                _queued, _queue_detail = queue_primary_message(
                    _shared_agent.id, user_input)
            else:
                try:
                    _shared_agent.message_queue.put_nowait(user_input)
                    _queued = True
                    _queue_detail = (
                        f"Queued for {_shared_agent.name or _shared_agent.id}.")
                except queue.Full:
                    _queued = False
                    _queue_detail = "Agent instruction queue is full."
            console.print(
                f"[dim]{_queue_detail if _queued else 'Could not queue input.'}[/dim]")
            if _queued:
                agent_ui_events.hub.emit(
                    "user_message", agent_id=_shared_agent.id,
                    terminal_name=agent_scope_terminal(_shared_agent),
                    summary=user_input, detail=user_input, status="queued")
            if injected_done is not None:
                injected_done.set()
            continue

        # If an interactive session is active, forward user input to it,
        # then ask the AI to decide the next step based on the output.

        if interactive_session and interactive_session.is_alive():
            console.print(f"[dim yellow]> {user_input}[/dim yellow]")
            chat_history.append({
                "role": "user", "content": user_input,
                "input_kind": "interactive",
            })
            if agent_registry.agent_id:
                agent_registry._push_events([{"type": "user", "content": user_input}])

            # Forward user input to the running interactive program
            interactive_session.send_keys(user_input + "\n")
            time.sleep(0.5)
            new_output = interactive_session.read_output(timeout=1.0)

            if not interactive_session.is_alive():
                display_command_output(interactive_session.command,
                                       interactive_session.returncode,
                                       interactive_session.full_output)
                agent_state["lastOutput"] = interactive_session.full_output
                # Session exited — let the agent loop process final output
                if session.get("userId") or not _active_backend.sends_laintas_credentials:
                    def local_events_cb(events: list):
                        _agent = get_current_agent()
                        agent_ui_events.hub.ingest(
                            _agent.id if _agent else "", events,
                            agent_scope_terminal(_agent) if _agent else "term0")
                        if agent_registry.agent_id:
                            agent_registry._push_events(events)
                    context = (f"The interactive program exited.\n"
                               f"Final output:\n{interactive_session.full_output[:3000]}\n\n"
                               f"Original user input: {user_input}\n"
                               f"What should we do next?")
                    response = _run_agent_loop_with_interrupt(get_loop_deps(), context, session, agent_state, chat_history,
                                              events_cb=local_events_cb,
                                              existing_session=interactive_session)
                    interactive_session = response.get("session")
                else:
                    interactive_session.close()
                    interactive_session = None
                    response = {"success": True, "msg": "", "state": agent_state, "session": None}
            else:
                display_command_output(interactive_session.command, -1, new_output)
                agent_state["lastOutput"] = interactive_session.full_output
                # Session still alive — ask AI to process the new output
                if session.get("userId") or not _active_backend.sends_laintas_credentials:
                    def local_events_cb(events: list):
                        _agent = get_current_agent()
                        agent_ui_events.hub.ingest(
                            _agent.id if _agent else "", events,
                            agent_scope_terminal(_agent) if _agent else "term0")
                        if agent_registry.agent_id:
                            agent_registry._push_events(events)
                    context = (f"User input (sent to {interactive_session.command}): {user_input}\n"
                               f"Program output:\n{new_output[:2000]}\n\n"
                               f"Full session output so far: {len(interactive_session.full_output)} bytes.\n"
                               f"Decide: send more keys, start a different command, or close the session.")
                    response = _run_agent_loop_with_interrupt(get_loop_deps(), context, session, agent_state, chat_history,
                                              events_cb=local_events_cb,
                                              existing_session=interactive_session)
                    interactive_session = response.get("session")
                else:
                    response = {"success": True, "msg": "", "state": agent_state, "session": interactive_session}

            # Save reply
            if (response.get("msg")
                    and not response.get("_history_recorded")):
                chat_history.append({"role": "assistant", "content": response["msg"]})
            # ── Cross-interaction state preservation ──
            _prepared_state = prepare_state_for_repl(response.get("state", {}))
            agent_state = _bind_current_agent_runtime(
                _prepared_state, chat_history, interactive_session,
                agent_state)
            handle_meta_command._last_agent_state = agent_state
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = user_input
            handle_meta_command._last_existing_session = interactive_session
            if args.depth == 0:
                current_live_session = session_store.sync_runtime(
                    current_live_session, agent_state, chat_history,
                    cwd=_session_start_cwd,
                    objective=agent_state.get("objective"),
                    last_user_input=user_input,
                    exit_reason=response.get("exit_reason"),
                    tasks=_active_task_export(),
                )
                handle_meta_command._current_live_session = current_live_session
                save_resume_state(agent_state, chat_history, _session_start_cwd)
            if injected_done is not None:
                injected_done.set()
            continue

        _system_input = (not _is_dialogue) and is_system_command(user_input)
        # Preserve input semantics in the resume record. Shell commands are
        # terminal activity, not user-to-agent prompts, even though both are
        # physically typed by the user. Dialogue input always goes to the
        # Agent — typing "ls" in the /agents view asks the Agent, it does
        # not shell out directly.
        chat_history.append({
            "role": "user",
            "content": user_input,
            "input_kind": "shell" if _system_input else "prompt",
        })
        if not _system_input:
            import plan_mode as _pending_pm
            if _pending_pm.is_pending_task():
                _pending_pm.enter_plan_mode(user_input)
                console.print(
                    "[green]PLAN task set from this message.[/green]")

        # Persist the admitted prompt before provider/tool execution so a
        # crash cannot leave the live session one user turn behind.
        if args.depth == 0 and current_live_session:
            current_live_session = session_store.sync_runtime(
                current_live_session, agent_state, chat_history,
                cwd=_session_start_cwd,
                objective=None if _system_input else user_input,
                last_user_input=None if _system_input else user_input,
                    tasks=_active_task_export(),
            )
            handle_meta_command._current_live_session = current_live_session

        # Push user input event to remote stream
        if agent_registry.agent_id:
            agent_registry._push_events([{"type": "user", "content": user_input}])
        _event_agent = get_current_agent()
        agent_ui_events.hub.ingest(
            _event_agent.id if _event_agent else "",
            [{"type": "user", "content": user_input}],
            agent_scope_terminal(_event_agent) if _event_agent else "term0")

        # Route first word against PATH/builtins → system command or AI
        # All REPL instances (depth 0 and depth > 0) execute system commands
        # directly. Natural language goes to AI.
        if _system_input:
            console.print(f"\n[dim yellow]$ {user_input}[/dim yellow]")
            if agent_registry.agent_id:
                agent_registry._push_events([{"type": "system", "kind": "command", "content": user_input}])

            _command_allowed, _command_denial = authorize_direct_command(
                user_input, os.getcwd(),
                remote_source=(injected_done is not None
                               and agent_registry.agent_id is not None))
            if not _command_allowed:
                console.print(f"[red]{_command_denial}[/red]")
                agent_state["lastOutput"] = _command_denial
                if agent_registry.agent_id:
                    agent_registry._push_events([{
                        "type": "system", "kind": "output",
                        "content": _command_denial,
                    }])
                if args.depth == 0 and current_live_session:
                    current_live_session = session_store.sync_runtime(
                        current_live_session, agent_state, chat_history,
                        cwd=_session_start_cwd,
                        tasks=_active_task_export(),
                    )
                    handle_meta_command._current_live_session = current_live_session
                    save_resume_state(
                        agent_state, chat_history, _session_start_cwd)
                if injected_done is not None:
                    injected_done.set()
                continue

            # Close previous AI-managed interactive session if any
            if interactive_session is not None:
                interactive_session.close()
                interactive_session = None

            # Remote-injected commands (from Helpwo Activity terminal): use
            # subprocess capture so stdout can be forwarded to the event stream.
            # pty_passthrough directly inherits the local tty and returns
            # stdout="" — nothing to forward.  Local commands keep pty_passthrough
            # so interactive programs (vim, claude) still work normally.
            if injected_done is not None and agent_registry.agent_id:
                import subprocess as _sub
                _cd_m = re.match(r'^\s*cd(?:\s+(.+))?\s*$', user_input)
                if _cd_m:
                    # cd is a shell builtin — handle it in the Python process too
                    _cd_target = (_cd_m.group(1) or "").strip() or os.path.expanduser("~")
                    try:
                        os.chdir(os.path.expanduser(_cd_target))
                        _cap_out = f"{os.getcwd()}"
                    except OSError as _e:
                        _cap_out = f"cd: {_e}"
                    result = {"stdout": _cap_out, "stderr": "", "returncode": 0, "success": True}
                else:
                    try:
                        _proc = _sub.run(
                            user_input, shell=True, capture_output=True, text=True,
                            timeout=60, cwd=os.getcwd(),
                        )
                        _cap_out = (_proc.stdout + _proc.stderr).strip()
                        result = {
                            "stdout": _proc.stdout, "stderr": _proc.stderr,
                            "returncode": _proc.returncode, "success": _proc.returncode == 0,
                        }
                    except _sub.TimeoutExpired:
                        _cap_out = "Command timed out after 60s."
                        result = {"stdout": "", "stderr": "", "returncode": -1, "success": False}
                agent_registry._push_events([{"type": "system", "kind": "output", "content": _cap_out[:4000]}])
            else:
                # Local user: ordinary commands route through term0's persistent
                # bash (marker-poll + cwd sync) so cd/export/pushd actually persist
                # across commands — term0's bash state IS what "current directory"
                # means here. Commands in the interactive whitelist (vim, claude,
                # ssh, ...) get full PTY passthrough so they keep native terminal
                # control (raw keystrokes, resize, full-screen redraw).
                _first = extract_first_word(user_input)
                _term0_info = get_terminal("term0")
                _use_term0 = (
                    _first not in get_interactive_commands()
                    and _term0_info is not None
                    and _term0_info.session is not None
                    and _term0_info.session.is_alive()
                )

                if _use_term0:
                    result = _marker_poll_exec(_term0_info.session, user_input, strip_ansi_codes=False)
                    _sync_cwd_from_term0(_term0_info.session)
                    # marker-poll captures output but doesn't echo to the user's
                    # terminal (unlike pty_passthrough, which echoes directly) —
                    # print so the user sees command output. Routed through the
                    # mirror tee so /agents shows it and stdout ownership holds.
                    _stdout = result.get("stdout", "")
                    if _stdout:
                        try:
                            repl_mirror.hub.tee_write(
                                _stdout if _stdout.endswith("\n")
                                else _stdout + "\n",
                                _mirror_target_agent_id())
                            sys.stdout.flush()
                        except (BrokenPipeError, OSError):
                            pass
                elif _agents_view_is_active():
                    # PTY passthrough hands the whole terminal to the child
                    # program; impossible while the /agents view owns it.
                    console.print(
                        "[yellow]Interactive terminal programs can't run "
                        "inside the /agents view — press Esc to return to "
                        "the main CLI first.[/yellow]")
                    result = {"stdout": "", "stderr": "",
                              "returncode": -1, "success": False}
                else:
                    # Drain any queued terminal query responses before passthrough.
                    _fl = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
                    fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, _fl | os.O_NONBLOCK)
                    try:
                        while True:
                            if not sys.stdin.buffer.read(4096):
                                break
                    except (BlockingIOError, OSError):
                        pass
                    finally:
                        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, _fl)

                    result = pty_passthrough(user_input)

                if agent_registry.agent_id:
                    output_preview = result.get("stdout", "")[:2000]
                    if output_preview:
                        agent_registry._push_events([{"type": "system", "kind": "output", "content": output_preview}])

            # ── Debug: log command execution ──
            loop_id = next_debug_loop()
            add_debug_log(DebugEntry(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                loop=loop_id,
                user_input=user_input[:2000],
                current_path=os.getcwd(),
                exec_command=user_input,
                exec_stdout=result.get("stdout", "")[:2000],
                exec_returncode=result.get("returncode", -1),
                done=True,
            ))

            _saved_shell_output = strip_ansi(
                (result.get("stdout", "") or "")
                + (result.get("stderr", "") or "")
            ).strip()
            if _saved_shell_output:
                chat_history.append({
                    "role": "shell",
                    "content": _saved_shell_output[:4000],
                    "returncode": result.get("returncode", -1),
                })
            agent_state["lastOutput"] = result.get("stdout", "")
            response = {
                "success": result.get("success", False),
                "msg": "",
                "state": agent_state,
                "session": None,
            }
        else:
            if (_active_backend.sends_laintas_credentials
                    and not session.get("userId")):
                console.print("[yellow]Not authenticated. Use /login first.[/yellow]")
                if injected_done is not None:
                    injected_done.set()
                continue
            # Build event callback for real-time streaming
            def local_events_cb(events: list):
                _agent = get_current_agent()
                agent_ui_events.hub.ingest(
                    _agent.id if _agent else "", events,
                    agent_scope_terminal(_agent) if _agent else "term0")
                if agent_registry.agent_id:
                    agent_registry._push_events(events)

            response = _run_agent_loop_with_interrupt(get_loop_deps(), user_input, session, agent_state, chat_history,
                                      events_cb=local_events_cb,
                                      existing_session=interactive_session)
            interactive_session = response.get("session")

            # ── Store context for /continue ──
            handle_meta_command._last_agent_state = response.get("state", agent_state)
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = user_input
            handle_meta_command._last_deps = get_loop_deps()
            handle_meta_command._last_session = session
            handle_meta_command._last_events_cb = local_events_cb
            handle_meta_command._last_existing_session = interactive_session

            # shell.exec is an isolated subprocess executor. A bare primary
            # `cd` updates the agent/CLI cwd; mirror only that directory into
            # term0 so the next direct user command starts in the same place.
            _desired_cwd = (response.get("state") or {}).get("cwd")
            _t0 = get_terminal("term0")
            if (_desired_cwd and os.path.isdir(_desired_cwd)
                    and _t0 and _t0.session and _t0.session.is_alive()):
                _marker_poll_exec(
                    _t0.session, f"cd -- {shlex.quote(_desired_cwd)}")

            # ── Plan approval menu ──
            # If the agent ran in plan mode, offer a rich review menu.
            # On approval, re-run the same task in act mode to execute.
            if _show_plan_approval_menu():
                console.print("[dim]Re-running task in ACT mode...[/dim]")
                _plan_state = response.get("state", agent_state)
                response = _run_agent_loop_with_interrupt(
                    get_loop_deps(), user_input, session, _plan_state, chat_history,
                    events_cb=local_events_cb,
                    existing_session=interactive_session,
                    continue_thread=True)
                interactive_session = response.get("session")
                handle_meta_command._last_agent_state = response.get("state", agent_state)
                handle_meta_command._last_existing_session = interactive_session
                _desired_cwd = (response.get("state") or {}).get("cwd")
                _t0 = get_terminal("term0")
                if (_desired_cwd and os.path.isdir(_desired_cwd)
                        and _t0 and _t0.session and _t0.session.is_alive()):
                    _marker_poll_exec(
                        _t0.session, f"cd -- {shlex.quote(_desired_cwd)}")

        # Save AI reply to chat history
        if response.get("msg") and not response.get("_history_recorded"):
            chat_history.append({"role": "assistant", "content": response["msg"]})

        # ── Cross-interaction state preservation ──
        # Preserve recent context across REPL interactions so the model
        # doesn't lose track of what it was doing.
        _prepared_state = prepare_state_for_repl(response.get("state", {}))
        agent_state = _bind_current_agent_runtime(
            _prepared_state, chat_history, interactive_session, agent_state)
        if not _system_input:
            # `/continue` mutates these exact objects; keep the references
            # aligned with the state carried by the main REPL.
            handle_meta_command._last_agent_state = agent_state
            handle_meta_command._last_chat_history = chat_history
            handle_meta_command._last_original_input = user_input
            handle_meta_command._last_existing_session = interactive_session
        if args.depth == 0:
            current_live_session = session_store.sync_runtime(
                current_live_session, agent_state, chat_history,
                cwd=_session_start_cwd,
                objective=agent_state.get("objective") if not _system_input else None,
                last_user_input=None if _system_input else user_input,
                exit_reason=response.get("exit_reason") if not _system_input else None,
                    tasks=_active_task_export(),
            )
            handle_meta_command._current_live_session = current_live_session
            save_resume_state(agent_state, chat_history, _session_start_cwd)

        if injected_done is not None:
            injected_done.set()


if __name__ == "__main__":
    # Final safety net for cancellation paths that occur below an interactive
    # component (socket/select/third-party terminal code).  All expected
    # cancellation points handle KeyboardInterrupt locally; this guard keeps
    # an unexpected one from dumping a Python traceback into the TUI.  Explicit
    # SystemExit calls (for /exit, --execute, or fatal startup modes) are left
    # untouched.
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled. Returning to the terminal.[/dim]")
