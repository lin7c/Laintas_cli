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

# ── Tree ───────────────────────────────────────────────────────────────────
TREE_BRANCH = "├─"   # branch node (has sibling below)
TREE_LAST = "└─"     # last child node (no sibling below)
TREE_VERT = "│"      # vertical continuation line

# ── Special ────────────────────────────────────────────────────────────────
RETURN = "⏎"     # line return / enter

# ── Spinners ───────────────────────────────────────────────────────────────
# A fixed Laintas "L" launches a forward pulse. Every frame is exactly two
# terminal cells wide, so status text never shifts as the animation advances.
# The old names remain aliases for compatibility with project extensions.
SPINNER_RELAY = ("L·", "L›", "L»", "L›")
SPINNER_INTERVAL_MS = 140.0
SPINNER_BRAILLE = SPINNER_RELAY
SPINNER_GEO = SPINNER_RELAY
SQUARE_OPEN = "□"
