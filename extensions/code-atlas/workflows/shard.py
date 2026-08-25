#!/usr/bin/env python3
"""Deterministic shard splitter: module-level nodes -> shard files.

Usage:
  python3 shard.py <atlas_dir> <out_dir> [--shards N]

Writes shard-01.json ... shard-NN.json; each is a JSON array of node ids
(module-level nodes only, sorted). Deterministic: same graph -> same shards.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    atlas_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    n_shards = 4
    if "--shards" in sys.argv:
        n_shards = int(sys.argv[sys.argv.index("--shards") + 1])

    con = sqlite3.connect(str(atlas_dir / "graph.db"))
    mods = [r[0] for r in con.execute(
        "SELECT id FROM nodes WHERE kind='module' ORDER BY id")]
    con.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    # clean old shard files (deterministic regeneration)
    for old in out_dir.glob("shard-*.json"):
        old.unlink()

    size = (len(mods) + n_shards - 1) // n_shards
    for i in range(n_shards):
        chunk = mods[i * size:(i + 1) * size]
        if not chunk:
            break
        (out_dir / f"shard-{i + 1:02d}.json").write_text(
            json.dumps(chunk, indent=1), encoding="utf-8")
        print(f"shard-{i + 1:02d}.json: {len(chunk)} modules")
    print(f"total {len(mods)} modules in {min(n_shards, len(mods))} shards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
