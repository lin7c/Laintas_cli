"""Text colors chosen for the terminal's own background.

The CLI never repaints the terminal. Body text keeps the terminal's default
foreground, so it is exactly as bright as everything else the user reads
there; only secondary and status text carry a color, picked for a dark or a
light background.

Most call sites still spell their styles the old way (``[dim]``,
``[yellow]``, ``style="cyan"``). Those names would otherwise render with the
terminal's 16-color palette and the faint attribute, which is where washed-out
output came from. ``adapt`` and ``StyleResolver`` reroute them onto the
palette, so the whole CLI follows ``/theme`` without touching those sites.
Styles that paint their own background are left alone: they carry their own
paired foreground.

This module imports no agent state, so the updater's progress bar can use it.
"""
from __future__ import annotations

import os
import re
import sys
import time

THEMES = ("auto", "dark", "light", "mono")

# "dark" is text for a dark terminal, "light" is text for a light one.
PALETTES = {
    "dark": {
        "muted": "#a3adb8", "subtle": "#8e98a4",
        "success": "#56d364", "warning": "#e3b341", "error": "#ff7b72",
        "info": "#79d8e0", "agent": "#b9a3ff",
        "accent": "#3fb950", "accent_dim": "#2ea043", "rule": "#586069",
        # Paired colors for the few places that paint a background.
        "plate_fg": "#e6edf3", "plate_bg": "#161b22",
        "selected_fg": "#f0f6fc", "selected_bg": "#21262d",
        # The slash-command menu keeps its established look.
        "menu_fg": "#c9d1d9", "menu_meta": "#8b949e",
        "track": "#484f58",
    },
    "light": {
        "muted": "#57606a", "subtle": "#626a73",
        "success": "#17752f", "warning": "#8a5a00", "error": "#cf222e",
        "info": "#0f6f78", "agent": "#7a44d6",
        "accent": "#176f2c", "accent_dim": "#238636", "rule": "#afb8c1",
        "plate_fg": "#24292f", "plate_bg": "#f6f8fa",
        "selected_fg": "#24292f", "selected_bg": "#d0d7de",
        "menu_fg": "#24292f", "menu_meta": "#57606a",
        "track": "#d0d7de",
    },
}

# Standard ANSI color number -> palette role. None means "terminal default".
_ANSI_ROLES = {
    0: None, 1: "error", 2: "success", 3: "warning", 4: "info", 5: "agent",
    6: "info", 7: None, 8: "subtle", 9: "error", 10: "success", 11: "warning",
    12: "info", 13: "agent", 14: "info", 15: None,
}

_active = "dark"
_detected: str | None = None
_probed = False
#: Keys the user typed while the terminal was being asked for its background.
#: The probe has to read them to find the reply; the caller puts them back.
_typeahead = ""


def color(role: str, mode: str | None = None) -> str:
    """Hex color for a palette role; empty string in mono."""
    mode = mode or _active
    return "" if mode == "mono" else PALETTES.get(mode, PALETTES["dark"])[role]


def active() -> str:
    """The resolved mode in effect: dark, light or mono."""
    return _active


def set_active(mode: str) -> None:
    global _active
    if mode not in ("dark", "light", "mono"):
        raise ValueError("resolved theme must be dark, light, or mono")
    _active = mode


# ── Terminal background ───────────────────────────────────────────────

_OSC11 = re.compile(
    rb"\x1b\]11;rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")
_DA1 = re.compile(rb"\x1b\[\?[0-9;]*c")
_REPLIES = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[\?[0-9;]*c")
_OTHER_SEQUENCES = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1bO.|\x1b.?")


def typed_during_probe(reply: bytes) -> str:
    """What the user typed alongside the terminal's replies, as text.

    Editing keys are dropped rather than replayed out of context; printable
    text and Enter survive.
    """
    rest = _OTHER_SEQUENCES.sub(b"", _REPLIES.sub(b"", reply or b""))
    text = rest.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch.isprintable())


def take_typeahead() -> str:
    """Return and clear the text typed during the last probe."""
    global _typeahead
    text, _typeahead = _typeahead, ""
    return text


def parse_background_reply(data: bytes) -> tuple[float, float, float] | None:
    """Parse an OSC 11 reply into 0..1 RGB, or None when it carries none."""
    match = _OSC11.search(data or b"")
    if not match:
        return None
    return tuple(int(part, 16) / (16 ** len(part) - 1) for part in match.groups())


def classify(rgb: tuple[float, float, float]) -> str:
    """dark or light, by relative luminance of the background."""
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4
    red, green, blue = (linear(c) for c in rgb)
    # 0.18 is the luminance where black and white text contrast equally.
    return "light" if 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.18 else "dark"


def from_colorfgbg(value: str | None) -> str | None:
    """Read the background half of COLORFGBG ("15;0", "0;default;15")."""
    try:
        index = int(str(value or "").split(";")[-1])
    except ValueError:
        return None
    if not 0 <= index <= 15:
        return None
    return "light" if index in (7, 15) else "dark"


def _query_terminal(first_byte: float = 0.4, total: float = 1.0) -> bytes:
    """Ask the terminal for its background through the terminal arbiter.

    The query is followed by a device-attributes request. Every terminal
    answers that one, so its reply marks the end of the exchange: a terminal
    without OSC 11 costs one round trip instead of a full timeout, and no late
    reply is left behind to land in the prompt.
    """
    out = sys.__stdout__
    if os.environ.get("TERM", "dumb") in ("", "dumb"):
        return b""
    try:
        if out is None or not out.isatty():
            return b""
        import terminal_arbiter
        arbiter = terminal_arbiter.get_arbiter()
        if not arbiter.interactive:
            return b""
        reply = b""
        with arbiter.hold("theme-probe", terminal_arbiter.Mode.CBREAK,
                          timeout=0.5, raw_bytes=True) as term:
            if not term.interactive:
                return b""
            os.write(out.fileno(), b"\x1b]11;?\x07\x1b[c")
            started = time.monotonic()
            while True:
                elapsed = time.monotonic() - started
                limit = total if reply else first_byte
                if elapsed >= limit:
                    break
                chunk = term.read_bytes(timeout=limit - elapsed)
                if not chunk:
                    break
                reply += chunk
                if _DA1.search(reply):
                    break
        return reply
    except Exception:
        # Detection is a convenience; a busy or unusual terminal falls back.
        return b""


def detect_background(*, refresh: bool = False, query=_query_terminal) -> str | None:
    """dark, light, or None when the terminal does not say. Probed once."""
    global _detected, _probed, _typeahead
    if _probed and not refresh:
        return _detected
    _probed = True
    reply = query()
    _typeahead += typed_during_probe(reply)
    rgb = parse_background_reply(reply)
    _detected = classify(rgb) if rgb else from_colorfgbg(os.environ.get("COLORFGBG"))
    return _detected


def resolve(name: str | None, *, refresh: bool = False) -> str:
    """Turn a theme setting into the mode to render: dark, light or mono."""
    normalized = str(name or "auto").strip().lower()
    if normalized in ("dark", "light", "mono"):
        return normalized
    if normalized != "auto":
        raise ValueError("theme expects auto, dark, light, or mono")
    return detect_background(refresh=refresh) or "dark"


def resolve_quietly(name: str | None) -> str:
    """resolve() for render paths: never probes, never raises."""
    normalized = str(name or "auto").strip().lower()
    if normalized in ("dark", "light", "mono"):
        return normalized
    return _detected or (_active if _active != "mono" else "dark")


# ── Rich styles ───────────────────────────────────────────────────────

_ATTRIBUTES = ("bold", "italic", "underline", "blink", "blink2", "reverse",
               "conceal", "strike", "underline2", "frame", "encircle", "overline")


def _rebuild(style, *, color_value, dim):
    from rich.style import Style
    return Style(color=color_value, bgcolor=style.bgcolor, dim=dim, link=style.link,
                 meta=style.meta or None,
                 **{name: getattr(style, name) for name in _ATTRIBUTES})


def adapt(style, mode: str | None = None):
    """Move a style off the terminal's 16 colors and the faint attribute.

    Explicit RGB and 256-color values are the author's choice and pass
    through, as does anything that paints a background.
    """
    from rich.color import ColorType
    mode = mode or _active
    shade = style.color
    standard = shade is not None and shade.type == ColorType.STANDARD
    if mode == "mono":
        return _rebuild(style, color_value=None, dim=style.dim) if standard else style
    if style.bgcolor is not None or style.reverse:
        return style
    if not standard and not style.dim:
        return style
    value = shade
    if standard:
        role = _ANSI_ROLES.get(shade.number)
        value = color(role, mode) if role else None
    if style.dim and value is None:
        value = color("muted", mode)
    return _rebuild(style, color_value=value, dim=False if style.dim else style.dim)


def rich_styles(mode: str) -> dict:
    """Every Rich style name the CLI renders with, for one resolved mode."""
    from rich.default_styles import DEFAULT_STYLES
    styles = {name: adapt(style, mode) for name, style in DEFAULT_STYLES.items()}
    # Headings are body text set apart by weight; Rich's magenta would read
    # as the agent identity color.
    for level in range(1, 8):
        heading = styles.get(f"markdown.h{level}")
        if heading is not None:
            styles[f"markdown.h{level}"] = _rebuild(heading, color_value=None, dim=False)
    if mode == "mono":
        styles.update({
            "accent": "bold", "accent.dim": "dim", "success": "bold",
            "error": "bold reverse", "warning": "bold", "info": "", "foreground": "",
            "muted": "dim", "subtle": "dim", "surface": "", "selected": "reverse",
            "agent": "bold", "path": "", "glyph": "bold", "rule": "dim", "track": "dim",
            "markdown.code": "", "markdown.code_block": "",
        })
        return styles
    p = PALETTES[mode]
    styles.update({
        "foreground": "", "path": "",
        "muted": p["muted"], "subtle": p["subtle"],
        "success": p["success"], "warning": p["warning"], "error": "bold " + p["error"],
        "info": p["info"], "agent": p["agent"],
        "accent": p["accent"], "accent.dim": p["accent_dim"], "glyph": p["accent"],
        "rule": p["rule"], "track": p["track"],
        "surface": f"{p['plate_fg']} on {p['plate_bg']}",
        "selected": f"bold {p['selected_fg']} on {p['selected_bg']}",
        "markdown.code": "", "markdown.code_block": "",
        # Rich quotes in magenta, which here is the agent identity color.
        "markdown.block_quote": "italic " + p["muted"],
    })
    return styles


class StyleResolver:
    """Resolve style strings that are not theme names (``dim yellow``).

    Rich parses those straight into ANSI colors, bypassing the theme. This
    resolves them word by word through the theme and adapts what is left.
    """

    _KEYWORDS = frozenset({"on", "not", "link"})

    def __init__(self, styles: dict, mode: str):
        self._styles = styles
        self._mode = mode
        self._cache: dict = {}

    def __call__(self, name: str):
        """A Style, or None to let Rich resolve the name itself."""
        try:
            return self._cache[name]
        except KeyError:
            pass
        if len(self._cache) > 4096:
            self._cache.clear()
        result = self._cache[name] = self._resolve(name)
        return result

    def _resolve(self, name: str):
        from rich.style import Style
        from rich import errors
        if name in self._styles:
            return None
        try:
            return adapt(Style.parse(name), self._mode)
        except errors.StyleSyntaxError:
            pass
        words = name.split()
        if len(words) < 2 or self._KEYWORDS.intersection(words):
            return None
        parts = []
        for word in words:
            known = self._styles.get(word)
            if known is None:
                try:
                    known = adapt(Style.parse(word), self._mode)
                except errors.StyleSyntaxError:
                    return None
            parts.append(known)
        return Style.combine(parts)
