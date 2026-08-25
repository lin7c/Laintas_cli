"""Graph data model for code-atlas.

Deterministic contract: the ONLY writer of this model is the indexer.
Agents (HWO/HWG) may read it but never write it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


NODE_KINDS = ("dir", "module", "class", "function")
EDGE_KINDS = ("contains", "imports", "calls", "inherits", "references")


@dataclass
class Node:
    id: str                      # qualified name, e.g. "click.core.Context.invoke"
    kind: str                    # dir | module | class | function
    name: str                    # short name, e.g. "invoke"
    parent_id: str | None        # "contains" edge target (None for root)
    file: str | None             # relative path (None for dir nodes)
    line: int | None             # 1-based line where defined
    doc: str = ""                # docstring first line (glossary seed)
    is_public: bool = False      # not underscore-prefixed

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "parentId": self.parent_id,
            "file": self.file,
            "line": self.line,
            "doc": self.doc,
            "public": self.is_public,
        }


@dataclass
class Edge:
    src: str
    dst: str
    kind: str                     # imports | calls | inherits | references
    file: str | None              # where the reference occurs
    line: int | None

    def to_json(self) -> dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "kind": self.kind,
                "file": self.file, "line": self.line}


class Graph:
    def __init__(self, root_name: str = "root"):
        self.root_name = root_name
        self.nodes: dict[str, Node] = {}
        # contains edges are implicit in Node.parent_id; explicit edges for the rest
        self.edges: list[Edge] = []
        self.files: dict[str, str] = {}       # relpath -> sha256 hex
        self.glossary: list[dict[str, str]] = []
        self.notes: list[str] = []            # unresolved/approximate decisions (deterministic)
        self.stats: dict[str, int] = {}       # aggregate counters (external calls, etc.)

    def add_node(self, n: Node) -> None:
        assert n.id not in self.nodes, f"duplicate node id {n.id}"
        self.nodes[n.id] = n

    def add_edge(self, src: str, dst: str, kind: str,
                 file: str | None = None, line: int | None = None) -> None:
        if src not in self.nodes or dst not in self.nodes:
            return  # deterministically drop dangling edges (recorded as note)
        self.edges.append(Edge(src, dst, kind, file, line))

    # ---- queries used by integrations and the web UI ----

    def children(self, node_id: str) -> list[str]:
        return [n.id for n in self.nodes.values() if n.parent_id == node_id]

    def module_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == "module"]

    def edge_matrix(self, src_ids: list[str], dst_ids: list[str]) -> list[list[int]]:
        """Dependency matrix: rows=src modules, cols=dst modules.
        Cell = count of edges from any node under src to any node under dst."""
        idx = {m: i for i, m in enumerate(src_ids)}
        out = [[0] * len(dst_ids) for _ in src_ids]
        for e in self.edges:
            s = self._module_of(e.src)
            d = self._module_of(e.dst)
            if s in idx and d in dst_ids:
                out[idx[s]][dst_ids.index(d)] += 1
        return out

    def _module_of(self, node_id: str) -> str | None:
        n = self.nodes.get(node_id)
        while n is not None:
            if n.kind == "module":
                return n.id
            n = self.nodes.get(n.parent_id) if n.parent_id else None
        return None

    # ---- serialization ----

    def to_json(self, edges: bool = True) -> dict[str, Any]:
        return {
            "root": self.root_name,
            "nodes": [n.to_json() for n in self.nodes.values()],
            "edges": [e.to_json() for e in self.edges] if edges else [],
            "files": self.files,
            "glossary": self.glossary,
            "notes": self.notes,
            "stats": self.stats,
        }

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, indent=1)
