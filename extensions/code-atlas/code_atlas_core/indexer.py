"""Deterministic Python indexer: source tree -> Graph.

Three phases (no LLM involvement):
  0. skeleton   — dir/module nodes + file hashes (whole tree, order-independent)
  1. declare    — classes/functions per module (glossary seeds from docstrings)
  2. references — imports / calls / inheritance edges, with all symbols known

Resolution policy (documented, deterministic):
  - absolute imports: `<pkg>.<module>` maps onto the indexed package tree;
    anything else (stdlib, third-party) is counted as external, never guessed.
  - relative imports (`from .` / `from ..`): resolved against the module's
    package chain; misses are recorded as notes (they should never happen).
  - calls: module-local functions resolve; names imported via `from X import Y`
    resolve to X.Y when that symbol exists; attribute calls resolve only when
    the attribute name matches exactly one indexed class/function, preferring
    the same module; everything else is counted as external.
  - typing.overload stubs are skipped (they are type stubs, not definitions).
"""
from __future__ import annotations

import ast
import hashlib
import os
from collections import Counter
from pathlib import Path

from .graph import Graph, Node

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", "build", "dist"}


class Indexer:
    def __init__(self, root: str | os.PathLike, package_hint: str | None = None):
        self.root = Path(root).resolve()
        self.package_hint = package_hint
        self.graph = Graph(root_name=self.root.name)
        self.stats: Counter = Counter()
        self._py_files: list[tuple[Path, Path]] = []   # (rel, abs)
        self._trees: dict[Path, ast.Module] = {}       # rel -> parsed tree

    # ---- public API ----

    def run(self) -> Graph:
        pkgs = self._find_packages()
        # phase 0: skeleton (order-independent)
        for pkg_dir, chain, pkg_name in pkgs:
            self._skeleton(pkg_dir, chain, pkg_name)
        # phase 1: declarations
        for rel, path in self._py_files:
            self._declare(rel, path)
        # phase 2: references (all symbols known)
        for rel, path in self._py_files:
            self._references(rel)
        self.graph.stats = dict(self.stats)
        return self.graph

    # ---- phase 0 ----

    def _find_packages(self) -> list[tuple[Path, tuple[str, ...], str]]:
        if self.package_hint:
            pkg_dir = self.root / self.package_hint
            if not pkg_dir.is_dir():
                raise FileNotFoundError(f"package dir not found: {pkg_dir}")
            chain = tuple(pkg_dir.relative_to(self.root).parts)
            return [(pkg_dir, chain, pkg_dir.name)]
        out = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and child.name not in SKIP_DIRS:
                if any(py.is_file() for py in child.glob("*.py")):
                    out.append((child, (child.name,), child.name))
        return out

    def _skeleton(self, pkg_dir: Path, chain: tuple[str, ...],
                  pkg_name: str) -> None:
        # dir node for the package root and every subdir containing py files
        dirs: set[Path] = set()
        for p in pkg_dir.rglob("*.py"):
            if "__pycache__" in p.parts or p.name.startswith("."):
                continue
            dirs.add(p.parent)
        for d in sorted(dirs):
            rel = d.relative_to(self.root)
            nid = "dir:" + str(rel)
            if nid not in self.graph.nodes:
                parent = self._dir_parent(rel)
                self.graph.add_node(Node(nid, "dir", d.name, parent,
                                         None, None))
        for p in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in p.parts or p.name.startswith("."):
                continue
            rel = p.relative_to(self.root)
            mod_id = self._mod_id(rel)
            parent = "dir:" + str(rel.parent)
            self.graph.add_node(Node(mod_id, "module", p.stem, parent,
                                     str(rel), 1, "", not p.stem.startswith("_")))
            self.graph.files[str(rel)] = self._file_hash(p)
            self._py_files.append((rel, p))

    def _dir_parent(self, rel: Path) -> str | None:
        return "dir:" + str(rel.parent) if rel.parent != Path(".") else None

    def _mod_id(self, rel: Path) -> str:
        return "mod:" + ".".join(rel.with_suffix("").parts)

    def _file_hash(self, p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]

    # ---- phase 1 ----

    def _declare(self, rel: Path, path: Path) -> None:
        mod_id = self._mod_id(rel)
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=str(rel))
        except SyntaxError as e:
            self.stats["syntax_errors"] += 1
            self.graph.notes.append(f"syntax-error {rel}:{e.lineno} {e.msg}")
            return
        self._trees[rel] = tree

        doc = ast.get_docstring(tree, clean=True)
        if doc:
            self.graph.glossary.append({
                "term": mod_id.split(":")[-1],
                "definition": doc.split("\n")[0],
                "source": str(rel) + ":1"})
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                self._declare_class(stmt, mod_id, rel)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not self._is_overload(stmt):
                    self._declare_function(stmt, mod_id, rel, None)

    def _declare_class(self, stmt: ast.ClassDef, mod_id: str, rel: Path) -> None:
        cid = self._member_id(mod_id, None, stmt.name)
        doc = ast.get_docstring(stmt, clean=True) or ""
        self.graph.add_node(Node(cid, "class", stmt.name, mod_id, str(rel),
                                 stmt.lineno, doc.split("\n")[0] if doc else "",
                                 not stmt.name.startswith("_")))
        if doc:
            self.graph.glossary.append({
                "term": f"{mod_id.split(':')[-1]}.{stmt.name}",
                "definition": doc.split("\n")[0],
                "source": f"{rel}:{stmt.lineno}"})
        for item in stmt.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    not self._is_overload(item):
                self._declare_function(item, mod_id, rel, cid)

    def _declare_function(self, stmt, mod_id: str, rel: Path,
                          parent_id: str | None) -> None:
        fid = self._member_id(mod_id, parent_id.split(".")[-1] if parent_id
                              else None, stmt.name)
        doc = ast.get_docstring(stmt, clean=True) or ""
        public = not stmt.name.startswith("_")
        self.graph.add_node(Node(fid, "function", stmt.name, parent_id or mod_id,
                                 str(rel), stmt.lineno,
                                 doc.split("\n")[0] if doc else "",
                                 public))
        # A documented public module-level function is a term: `click.echo` is
        # the first thing anyone learns about click, and leaving functions out
        # of the glossary made the most citable symbols in a library the ones
        # a note was not allowed to cite.
        if doc and public and parent_id is None:
            self.graph.glossary.append({
                "term": f"{mod_id.split(':')[-1]}.{stmt.name}",
                "definition": doc.split("\n")[0],
                "source": f"{rel}:{stmt.lineno}"})

    # ---- phase 2 ----

    def _references(self, rel: Path) -> None:
        tree = self._trees.get(rel)
        if tree is None:
            return  # syntax-error file, already noted
        mod_id = self._mod_id(rel)

        imported_names: dict[str, str] = {}     # local name -> module id
        imported_from: dict[str, list[str]] = {}  # module id -> names
        for stmt in tree.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    target = self._resolve_absolute(alias.name)
                    local = alias.asname or alias.name.split(".")[0]
                    if target:
                        imported_names[local] = target
                        self.graph.add_edge(mod_id, target, "imports",
                                            str(rel), stmt.lineno)
                    else:
                        self.stats["external_imports"] += 1
            elif isinstance(stmt, ast.ImportFrom):
                self._handle_import_from(stmt, mod_id, rel,
                                         imported_names, imported_from)
            elif isinstance(stmt, ast.ClassDef):
                self._resolve_bases(stmt, mod_id, rel)
                self._scan_function_bodies(stmt.body, mod_id, rel, stmt.name,
                                           imported_names, imported_from)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    not self._is_overload(stmt):
                self._scan_function(stmt, mod_id, rel, None,
                                    imported_names, imported_from)

    def _handle_import_from(self, stmt: ast.ImportFrom, mod_id: str,
                            rel: Path, imported_names: dict,
                            imported_from: dict) -> None:
        if stmt.module == "__future__":
            return
        if stmt.level > 0:
            target = self._resolve_relative(mod_id, stmt.level, stmt.module)
            if target:
                self._bind_import(target, stmt, mod_id, rel,
                                  imported_names, imported_from)
            else:
                self.stats["unresolved_relative_imports"] += 1
                self.graph.notes.append(
                    f"unresolved-relative-import {rel}:{stmt.lineno} "
                    f"level={stmt.level} module={stmt.module}")
            return
        target = self._resolve_absolute(stmt.module or "")
        if target:
            self._bind_import(target, stmt, mod_id, rel,
                              imported_names, imported_from)
        else:
            self.stats["external_imports"] += 1

    def _bind_import(self, target: str, stmt: ast.ImportFrom, mod_id: str,
                     rel: Path, imported_names: dict,
                     imported_from: dict) -> None:
        self.graph.add_edge(mod_id, target, "imports", str(rel), stmt.lineno)
        imported_from[target] = [a.name for a in stmt.names]
        for alias in stmt.names:
            imported_names[alias.asname or alias.name] = target

    def _resolve_absolute(self, dotted: str) -> str | None:
        if not dotted:
            return None
        nodes = self.graph.nodes
        direct = "mod:" + dotted
        if direct in nodes:
            return direct
        # map package-local absolute imports (<pkg>.<mod>) onto the indexed tree
        for pkg_dir, chain, pkg_name in self._find_packages():
            parts = dotted.split(".")
            if parts[0] == pkg_name and len(parts) > 1:
                cand = "mod:" + ".".join(chain + tuple(parts[1:]))
                if cand in nodes:
                    return cand
        return None

    def _resolve_relative(self, mod_id: str, level: int,
                          module_dotted: str | None) -> str | None:
        chain = tuple(mod_id[len("mod:"):].split(".")[:-1])  # module's package
        if level > len(chain):
            return None
        base = chain[:len(chain) - level + 1]
        parts = list(base)
        if module_dotted:
            parts += module_dotted.split(".")
        cand = "mod:" + ".".join(parts)
        return cand if cand in self.graph.nodes else None

    def _resolve_bases(self, stmt: ast.ClassDef, mod_id: str, rel: Path) -> None:
        cid = self._member_id(mod_id, None, stmt.name)
        for base in stmt.bases:
            name = self._base_name(base)
            if not name:
                continue
            matches = [n.id for n in self.graph.nodes.values()
                       if n.kind in ("class", "function")
                       and n.id.endswith("." + name)]
            if not matches:
                self.stats["external_imports"] += 1  # stdlib/external base
                continue
            same_mod = [m for m in matches if m.startswith(mod_id + ".")]
            target = (same_mod[0] if len(same_mod) == 1 else
                      matches[0] if len(matches) == 1 else None)
            if target and target != cid:
                self.graph.add_edge(cid, target, "inherits", str(rel),
                                    stmt.lineno)
            elif target is None:
                self.stats["ambiguous_calls"] += 1

    # ---- call scanning ----

    def _scan_function_bodies(self, body, mod_id: str, rel: Path,
                              class_name: str, imported_names: dict,
                              imported_from: dict) -> None:
        for item in body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    not self._is_overload(item):
                self._scan_function(item, mod_id, rel, class_name,
                                    imported_names, imported_from)

    def _scan_function(self, fn, mod_id: str, rel: Path, class_name: str | None,
                       imported_names: dict, imported_from: dict) -> None:
        fid = self._member_id(mod_id, class_name, fn.name)
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                self._handle_call(node, fid, mod_id, rel,
                                  imported_names, imported_from)

    def _handle_call(self, call: ast.Call, fid: str, mod_id: str, rel: Path,
                     imported_names: dict, imported_from: dict) -> None:
        f = call.func
        line = getattr(call, "lineno", None)
        if isinstance(f, ast.Name):
            self._call_by_name(f.id, fid, mod_id, rel, line,
                               imported_names, imported_from)
        elif isinstance(f, ast.Attribute):
            self._call_by_attr(f.attr, fid, mod_id, rel, line)
        else:
            self.stats["external_calls"] += 1

    def _call_by_name(self, name: str, fid: str, mod_id: str, rel: Path,
                      line, imported_names: dict,
                      imported_from: dict) -> None:
        local = self._member_id(mod_id, None, name)
        if local in self.graph.nodes and \
                self.graph.nodes[local].kind == "function":
            self.graph.add_edge(fid, local, "calls", str(rel), line)
            return
        if name in imported_names:
            tmod = imported_names[name]
            if tmod in imported_from and name in imported_from[tmod]:
                cand = f"{tmod}.{name}"
                if cand in self.graph.nodes:
                    self.graph.add_edge(fid, cand, "calls", str(rel), line)
                    return
            self.graph.add_edge(fid, tmod, "calls", str(rel), line)
            return
        self.stats["external_calls"] += 1

    def _call_by_attr(self, name: str, fid: str, mod_id: str, rel: Path,
                      line) -> None:
        if name.startswith("__"):
            self.stats["external_calls"] += 1
            return
        matches = [n.id for n in self.graph.nodes.values()
                   if n.kind in ("class", "function")
                   and n.id.endswith("." + name)]
        if not matches:
            self.stats["external_calls"] += 1
            return
        same_mod = [m for m in matches if m.startswith(mod_id + ".")]
        if len(same_mod) == 1:
            target = same_mod[0]
        elif len(matches) == 1:
            target = matches[0]
        else:
            self.stats["ambiguous_calls"] += 1
            self.graph.notes.append(
                f"ambiguous-call {rel}:{line} {name} ({len(matches)} matches)")
            return
        if target != fid:
            self.graph.add_edge(fid, target, "calls", str(rel), line)

    # ---- helpers ----

    def _is_overload(self, stmt) -> bool:
        for d in getattr(stmt, "decorator_list", []):
            if isinstance(d, ast.Name) and d.id == "overload":
                return True
            if isinstance(d, ast.Attribute) and d.attr == "overload":
                return True
        return False

    def _member_id(self, mod_id: str, class_name: str | None, name: str) -> str:
        if class_name:
            return f"{mod_id}.{class_name}.{name}"
        return f"{mod_id}.{name}"

    def _base_name(self, base: ast.AST) -> str:
        if isinstance(base, ast.Name):
            return base.id
        if isinstance(base, ast.Attribute):
            parts, node = [], base
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
            return ".".join(reversed(parts))
        return ""
