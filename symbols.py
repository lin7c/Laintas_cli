"""Centralized UI symbol constants.

All terminal-display symbols used across laintas_cli are defined here so that
the visual language stays consistent and easy to audit.

Design principles:
  - Pure text Unicode symbols only — no colored emoji (U+1F000+ or
    Dingbats with emoji presentation).  Text symbols render identically in
    every terminal, while emoji depend on the OS font and break alignment.
  - One canonical symbol per semantic meaning (✓ for success, ✗ for failure,
    ⚠ for warning) — no mixing ✅/✓ or ❌/✗/✕.
"""

# ── Status indicators ──────────────────────────────────────────────────────
OK = "✓"          # success / done / approved
FAIL = "✗"        # failure / error / denied
WARN = "⚠"        # warning (no VS16 — plain text, never ⚠️)
INFO = "›"        # info / notice / detail marker
BULLET = "·"      # separator / minor bullet
DOT = "●"         # active / running marker
DOT_HALF = "◐"    # in-progress / thinking marker
DOT_OPEN = "○"    # idle / waiting marker
DOT_DASH = "◌"    # queued marker

# ── Arrows ─────────────────────────────────────────────────────────────────
ARROW_R = "→"     # rightwards (maps, transformations)
ARROW_L = "←"     # leftwards
ARROW_U = "↑"     # up (input tokens)
ARROW_D = "↓"     # down (output tokens)
ARROW_RR = "↳"    # sub-item / continuation

# ── Special ────────────────────────────────────────────────────────────────
ZAP = "⚡"        # interrupt / fast action
RETURN = "⏎"     # line return / enter

# ── Spinners ───────────────────────────────────────────────────────────────
SPINNER_BRAILLE = tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
SPINNER_GEO = tuple("◰◳◲◱")
SQUARE_OPEN = "□"
