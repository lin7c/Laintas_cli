#!/usr/bin/env python3
"""Copy AI-PoW's modules from an ai-pow checkout into extensions/ai-pow.

The extension vendors them byte-for-byte; `tests/test_aipow_extension.py`
fails when they drift from a sibling `../ai-pow` checkout.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("ai_pow.py", "ai_pow_scoring.py", "ai_pow_report.py")


def main(argv: list) -> int:
    source = Path(argv[0] if argv else ROOT.parent / "ai-pow").expanduser().resolve()
    missing = [name for name in MODULES if not (source / name).is_file()]
    if missing:
        print(f"not an ai-pow checkout: {source} (missing {', '.join(missing)})",
              file=sys.stderr)
        return 1
    target = ROOT / "extensions" / "ai-pow"
    for name in MODULES:
        shutil.copyfile(source / name, target / name)
        print(f"{source / name} -> {target / name}")
    print("Bump extensions/ai-pow/extension.json's version to publish the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
