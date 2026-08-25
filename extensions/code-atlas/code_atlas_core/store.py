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
