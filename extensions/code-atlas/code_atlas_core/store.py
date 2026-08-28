"""SQLite persistence for the graph (graph.db).

The indexer is the only writer. Integrations and agents read via queries here.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .graph import Graph


def graph_hash(graph: Graph) -> str:
    """Deterministic aggregate hash over indexed files (content-addressed)."""
    import hashlib
    h = hashlib.sha256()
    for path in sorted(graph.files):
        h.update(path.encode())
        h.update(graph.files[path].encode())
    return h.hexdigest()

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, kind TEXT, name TEXT, parent_id TEXT,
  file TEXT, line INTEGER, doc TEXT, public INTEGER
);
CREATE TABLE IF NOT EXISTS edges (
  src TEXT, dst TEXT, kind TEXT, file TEXT, line INTEGER
);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def write_db(graph: Graph, db_path: str | Path) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(SCHEMA)
        # tables have no natural primary key for edges; a re-index must not
        # accumulate duplicates, so clear before writing (idempotent writes)
        for table in ("nodes", "edges", "files", "meta"):
            con.execute(f"DELETE FROM {table}")
        con.executemany(
            "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?)",
            [(n.id, n.kind, n.name, n.parent_id, n.file, n.line, n.doc,
              1 if n.is_public else 0) for n in graph.nodes.values()])
        con.executemany(
            "INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?)",
            [(e.src, e.dst, e.kind, e.file, e.line) for e in graph.edges])
        con.executemany(
            "INSERT OR REPLACE INTO files VALUES (?,?)",
            list(graph.files.items()))
        con.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            [("root", graph.root_name),
             ("glossary", json.dumps(graph.glossary, ensure_ascii=False)),
             ("notes", json.dumps(graph.notes, ensure_ascii=False)),
             ("stats", json.dumps(graph.stats, ensure_ascii=False)),
             ("graph_hash", graph_hash(graph)),
             ("schema_version", "1")])
        con.commit()
    finally:
        con.close()


def read_db(db_path: str | Path) -> Graph:
    """Load a graph back from graph.db (used by verify/integrations)."""
    from .graph import Node

    con = sqlite3.connect(str(db_path))
    try:
        graph = Graph()
        rows = con.execute("SELECT id,kind,name,parent_id,file,line,doc,public FROM nodes")
        for r in rows:
            graph.nodes[r[0]] = Node(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]))
        for r in con.execute("SELECT src,dst,kind,file,line FROM edges"):
            graph.add_edge(r[0], r[1], r[2], r[3], r[4])
        graph.files = dict(con.execute("SELECT path,hash FROM files"))
        for key, value in con.execute("SELECT key,value FROM meta"):
            if key == "root":
                graph.root_name = value
            elif key == "glossary":
                graph.glossary = json.loads(value)
            elif key == "notes":
                graph.notes = json.loads(value)
            elif key == "stats":
                graph.stats = json.loads(value)
        return graph
    finally:
        con.close()


def dependency_paths(db_path: str | Path, src_module: str,
                     dst_module: str, max_depth: int = 6) -> list[list[str]]:
    """Deterministic transitive dependency paths between two modules (BFS).

    Returns up to 20 paths, shortest first. Used by atlas.lookup integrations.
    """
    con = sqlite3.connect(str(db_path))
    try:
        adj: dict[str, set[str]] = {}
        for s, d, _k, _f, _l in con.execute("SELECT src,dst,kind,file,line FROM edges"):
            adj.setdefault(s, set()).add(d)
        paths: list[list[str]] = []
        if src_module == dst_module:
            return [[src_module]]
        queue = [[src_module]]
        seen = {src_module}
        depth = 0
        while queue and depth < max_depth:
            depth += 1
            nxt = []
            for path in queue:
                for d in sorted(adj.get(path[-1], ())):
                    if d == dst_module:
                        paths.append(path + [d])
                        if len(paths) >= 20:
                            return paths
                    if d not in seen:
                        seen.add(d)
                        nxt.append(path + [d])
            queue = nxt
        return paths
    finally:
        con.close()


# ---- agent-facing queries ------------------------------------------------
#
# These exist so an agent can ask the index a question instead of grepping the
# tree: where is this symbol, what is in this module, who touches this node,
# and -- the one that keeps the other three honest -- is the index still
# describing the files on disk.

_NODE_COLS = "id,kind,name,parent_id,file,line,doc,public"


def _row(r) -> dict:
    return {"id": r[0], "kind": r[1], "name": r[2], "parent": r[3],
            "file": r[4], "line": r[5], "doc": r[6], "public": bool(r[7])}


def _module_id(con, module: str) -> str | None:
    """Accept a module id, a dotted name, or a path; return the node id."""
    if module.startswith("mod:"):
        cand = module
    else:
        rel = module[:-3] if module.endswith(".py") else module
        cand = "mod:" + rel.replace("/", ".").replace("\\", ".")
    row = con.execute("SELECT id FROM nodes WHERE id=? AND kind='module'",
                      (cand,)).fetchone()
    if row:
        return row[0]
    # a path is unambiguous even when the id rule mangled it (dotted filenames)
    row = con.execute("SELECT id FROM nodes WHERE kind='module' AND file=?",
                      (module,)).fetchone()
    return row[0] if row else None


def find_symbol(db_path: str | Path, name: str, kind: str | None = None,
                limit: int = 50) -> dict:
    """Locate a class or function by name -> file:line.

    Exact matches only; if there are none the same query runs as a substring
    match and the result says so, because "no exact match, here are near
    misses" and "here is your symbol" must not look alike to the caller.
    """
    con = sqlite3.connect(str(db_path))
    try:
        kinds = (kind,) if kind else ("class", "function", "module")
        marks = ",".join("?" * len(kinds))
        rows = con.execute(
            f"SELECT {_NODE_COLS} FROM nodes WHERE name=? AND kind IN ({marks})"
            " ORDER BY id", (name, *kinds)).fetchall()
        how = "exact"
        if not rows:
            rows = con.execute(
                f"SELECT {_NODE_COLS} FROM nodes WHERE name LIKE ?"
                f" AND kind IN ({marks}) ORDER BY id",
                (f"%{name}%", *kinds)).fetchall()
            how = "substring"
        return {"query": name, "match": how, "truncated": len(rows) > limit,
                "matches": [_row(r) for r in rows[:limit]]}
    finally:
        con.close()


def outline(db_path: str | Path, module: str,
            include_private: bool = False) -> dict:
    """What a module declares, without reading the file.

    Classes carry their methods, so one call answers "what is in here" for a
    file that would otherwise cost a full read.
    """
    con = sqlite3.connect(str(db_path))
    try:
        mod_id = _module_id(con, module)
        if mod_id is None:
            return {"module": module, "error": "not in the index"}
        rows = con.execute(
            f"SELECT {_NODE_COLS} FROM nodes WHERE id LIKE ?"
            " AND kind IN ('class','function') ORDER BY line, id",
            (mod_id + ".%",)).fetchall()
        node = con.execute(f"SELECT {_NODE_COLS} FROM nodes WHERE id=?",
                           (mod_id,)).fetchone()
        classes: dict[str, dict] = {}
        functions: list[dict] = []
        for r in rows:
            item = _row(r)
            if not include_private and not item["public"]:
                continue
            if item["kind"] == "class":
                classes[item["id"]] = {**item, "methods": []}
            elif item["parent"] in classes:
                classes[item["parent"]]["methods"].append(
                    {"name": item["name"], "line": item["line"],
                     "doc": item["doc"]})
            else:
                functions.append(item)
        return {"module": mod_id, "file": node[4] if node else None,
                "doc": node[6] if node else "",
                "classes": list(classes.values()), "functions": functions}
    finally:
        con.close()


def neighbors(db_path: str | Path, node_id: str, limit: int = 100) -> dict:
    """Who reaches this node and what it reaches, with file:line evidence.

    The reverse direction is the one grep is worst at: finding every caller
    means searching for a name that also appears as an attribute, a string and
    a comment, whereas the graph already resolved it.
    """
    con = sqlite3.connect(str(db_path))
    try:
        if not con.execute("SELECT 1 FROM nodes WHERE id=?",
                           (node_id,)).fetchone():
            return {"node": node_id, "error": "not in the index"}

        def side(column: str, other: str) -> list[dict]:
            rows = con.execute(
                f"SELECT e.{other},e.kind,e.file,e.line,n.kind,n.file,n.line "
                f"FROM edges e LEFT JOIN nodes n ON n.id=e.{other} "
                f"WHERE e.{column}=? ORDER BY e.kind,e.{other},e.line",
                (node_id,)).fetchall()
            return [{"id": r[0], "edge": r[1], "at": f"{r[2]}:{r[3]}",
                     "kind": r[4], "defined_at": f"{r[5]}:{r[6]}"
                     if r[5] else None} for r in rows[:limit]]

        return {"node": node_id,
                "out": side("src", "dst"), "in": side("dst", "src")}
    finally:
        con.close()


def stale(db_path: str | Path, root: str | Path) -> dict:
    """Is the index still describing the files on disk?

    Without this an agent cannot tell a correct map from a stale one, and a
    stale map is worse than none: it answers confidently and wrongly. Compares
    the stored content hashes against the tree the indexer would cover now.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from .indexer import Indexer, file_hash

    con = sqlite3.connect(str(db_path))
    try:
        stored = dict(con.execute("SELECT path,hash FROM files"))
    finally:
        con.close()

    current = {}
    for rel, abs_path in Indexer(root).source_files():
        try:
            current[str(rel)] = file_hash(abs_path)
        except OSError:
            continue        # vanished mid-scan: reported as removed below
    changed = sorted(p for p, h in current.items()
                     if p in stored and stored[p] != h)
    added = sorted(set(current) - set(stored))
    removed = sorted(set(stored) - set(current))
    return {"stale": bool(changed or added or removed),
            "indexed_files": len(stored), "current_files": len(current),
            "changed": changed[:50], "added": added[:50],
            "removed": removed[:50],
            "counts": {"changed": len(changed), "added": len(added),
                       "removed": len(removed)}}
