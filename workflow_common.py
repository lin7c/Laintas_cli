"""Shared primitives for the HWO and HWG workflow runners.

Extracted from hwo_runner.py / hwg_runner.py to avoid duplicated logic
between the two runners (see docs/code-reuse-analysis.md).
"""
from __future__ import annotations

import re
from typing import Optional


def literal_value(raw: Optional[str], empty_as_none: bool = True):
    """Parse a workflow-DSL literal: quoted string, bool, or number.

    ``empty_as_none=True`` preserves the original hwo_runner semantics
    (empty/whitespace input parses to None); ``False`` preserves the
    original hwg_runner semantics (empty input parses to "").
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None if empty_as_none else s
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        if re.match(r"^-?\d+(?:\.\d+)?$", s):
            return float(s) if "." in s else int(s)
    except Exception:
        pass
    return s
