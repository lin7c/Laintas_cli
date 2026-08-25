#!/usr/bin/env python3
"""code-atlas CLI entry: index a Python codebase into graph.db + graph.json.

Usage:
  python3 main.py index <root> [--package <dir>] [--out <dir>]
  python3 main.py verify <root> [--package <dir>] [--out <dir>]

The indexer is deterministic: same input -> same output. No LLM involvement.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_atlas_core.indexer import Indexer
from code_atlas_core.store import read_db, write_db


def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    g = Indexer(root, package_hint=args.package).run()
    db_path = out / "graph.db"
    json_path = out / "graph.json"
    write_db(g, db_path)
    g.dump(str(json_path))

    n_mods = sum(1 for n in g.nodes.values() if n.kind == "module")
    n_edges = len(g.edges)
    print(f"indexed {root}")
    print(f"  modules: {n_mods}, nodes: {len(g.nodes)}, edges: {n_edges}")
    print(f"  graph.db: {db_path}")
    print(f"  graph.json: {json_path}")
    print(f"  stats: {dict(g.stats)}")
    print(f"  notes: {len(g.notes)} (ambiguous calls / unresolved relative imports)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Deterministic post-conditions on the indexed graph."""
    out = Path(args.out).resolve()
    g = read_db(out / "graph.db")
    errors = []

    if not g.nodes:
        errors.append("no nodes indexed")
    # every non-dir node must have a parent chain reaching a dir
    for n in g.nodes.values():
        if n.kind == "dir":
            continue
        cur = n.parent_id
        ok = False
        while cur is not None:
            p = g.nodes.get(cur)
            if p is None:
                break
            if p.kind == "dir":
                ok = True
                break
            cur = p.parent_id
        if not ok:
            errors.append(f"orphan node {n.id}")
    # no dangling edges (add_edge already drops them; assert none exist)
    for e in g.edges:
        if e.src not in g.nodes or e.dst not in g.nodes:
            errors.append(f"dangling edge {e.src}->{e.dst}")
    # every file entry has a module node
    for f in g.files:
        mod_id = "mod:" + ".".join(Path(f).with_suffix("").parts)
        if mod_id not in g.nodes:
            errors.append(f"file without module node: {f}")

    if errors:
        print("VERIFY FAIL")
        for e in errors[:20]:
            print("  -", e)
        return 1
    print(f"VERIFY OK: {len(g.nodes)} nodes, {len(g.edges)} edges, "
          f"{len(g.files)} files, {len(g.glossary)} glossary seeds")
    return 0


def cmd_scene(args: argparse.Namespace) -> int:
    """Lay the indexed graph out once, for every front end to render."""
    from code_atlas_core.scene import write_scene

    out = Path(args.out).resolve()
    path = write_scene(out, max_depth=args.depth)
    scene = __import__("json").loads(path.read_text(encoding="utf-8"))
    print(f"scene: {path}")
    print(f"  {len(scene['shapes'])} shapes, "
          f"{len(scene['connectors'])} connectors")
    print(f"  {scene['subtitle']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="code-atlas deterministic indexer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    scene_p = sub.add_parser("scene")
    scene_p.add_argument("root", nargs="?", default=".")
    scene_p.add_argument("--out", default=".atlas", help="atlas dir")
    scene_p.add_argument("--depth", type=int, default=4,
                         help="deepest node level to lay out")
    scene_p.set_defaults(fn=cmd_scene)

    for name, fn in (("index", cmd_index), ("verify", cmd_verify)):
        p = sub.add_parser(name)
        p.add_argument("root")
        p.add_argument("--package", default=None, help="package dir inside root")
        p.add_argument("--out", default=".atlas", help="output dir")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
