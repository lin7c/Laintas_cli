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

Tree policy (documented, deterministic):
  - packages are the root's subdirectories that hold .py files, plus the root
    itself scanned non-recursively, so a flat repo's top-level application is
    on the map and packages below it are still indexed exactly once.
  - hidden directories and SKIP_DIRS are excluded everywhere: `.laintas/
    worktrees/` holds whole copies of the repo being indexed, and a copy is
    not a second module.
  - a path that cannot be imported as a module (a dot or dash in a component)
    gets a digest-suffixed id, so it can never collide with a package path.
  - a symbol defined twice in one scope (a property and its setter) is one
    node -- the first in source order -- and the rest become notes.
"""
from __future__ import annotations

import ast
import hashlib
import os
from collections import Counter
from pathlib import Path

from .graph import Graph, Node

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv",
             "build", "dist", "site-packages"}


ROOT_DIR_ID = "dir:."


def mod_id_for(rel: Path) -> str:
    """Module node id for a repo-relative path. The one implementation.

    The verifier checks that every indexed file has a module node, so it has
    to derive ids by exactly this rule; deriving it twice is how a hash
    contract drifts.
    """
    parts = rel.with_suffix("").parts
    mid = "mod:" + ".".join(parts)
    if all(part.isidentifier() for part in parts):
        return mid          # importable: the dotted id *is* the import path
    # Not importable as a module (a dot or a dash in a path component), so its
    # dotted form can collide with a real package path -- `a.b.py` and `a/b.py`
    # both flattened to `mod:a.b`. Disambiguate by path digest; no dotted
    # import string can produce an id containing "#", so _resolve_absolute
    # correctly never resolves onto one of these.
    return mid + "#" + hashlib.sha256(str(rel).encode()).hexdigest()[:8]


def file_hash(p: Path) -> str:
    """Content hash of one source file. The one implementation."""
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _skip_dir(name: str) -> bool:
    """Directories that never carry indexable first-party source.

    Hidden directories are skipped as a class rather than by name: `.git` and
    `.venv` were already listed individually, and `.laintas/worktrees/` holds
    whole copies of the repository being indexed -- which is how the same
    module reached the graph twice and tripped the duplicate-id assertion.
    """
    return name in SKIP_DIRS or name.startswith(".")


class Indexer:
    def __init__(self, root: str | os.PathLike, package_hint: str | None = None):
        self.root = Path(root).resolve()
        self.package_hint = package_hint
        self.graph = Graph(root_name=self.root.name)
        self.stats: Counter = Counter()
        self._py_files: list[tuple[Path, Path]] = []   # (rel, abs)
        self._trees: dict[Path, ast.Module] = {}       # rel -> parsed tree
        self._pkgs: list[tuple[Path, tuple[str, ...], str, bool]] | None = None
        self._by_name: dict[str, list[str]] = {}   # short name -> node ids

    # ---- public API ----

    def run(self) -> Graph:
        pkgs = self._find_packages()
        # The graph has exactly one root. Top-level modules of a flat repo have
        # nowhere else to hang, and the verifier requires every non-dir node's
        # parent chain to end at a dir.
        self.graph.add_node(Node(ROOT_DIR_ID, "dir", self.root.name, None,
                                 None, None))
        # phase 0: skeleton (order-independent)
        for pkg_dir, chain, pkg_name, recursive in pkgs:
            self._skeleton(pkg_dir, chain, pkg_name, recursive)
        # phase 1: declarations
        for rel, path in self._py_files:
            self._declare(rel, path)
        # phase 2: references (all symbols known)
        self._index_names()
        for rel, path in self._py_files:
            self._references(rel)
        self.graph.stats = dict(self.stats)
        return self.graph

    # ---- phase 0 ----

    def _find_packages(self) -> list[tuple[Path, tuple[str, ...], str, bool]]:
        """(dir, package chain, package name, index recursively).

        Memoised: phase 2 resolves every absolute import against this list, so
        recomputing it per import statement meant one directory scan per import.
        """
        if self._pkgs is not None:
            return self._pkgs
        if self.package_hint:
            pkg_dir = self.root / self.package_hint
            if not pkg_dir.is_dir():
                raise FileNotFoundError(f"package dir not found: {pkg_dir}")
            chain = tuple(pkg_dir.relative_to(self.root).parts)
            self._pkgs = [(pkg_dir, chain, pkg_dir.name, True)]
            return self._pkgs
        out = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and not _skip_dir(child.name):
                if any(py.is_file() for py in child.glob("*.py")):
                    out.append((child, (child.name,), child.name, True))
        # A flat repository keeps its application at the top level; indexing
        # only subdirectories left every top-level module off the map. The root
        # is indexed non-recursively, so packages below it are still indexed
        # exactly once by their own entry above.
        if any(p.is_file() for p in self.root.glob("*.py")):
            out.append((self.root, (), self.root.name, False))
        self._pkgs = out
        return out

    def _skeleton(self, pkg_dir: Path, chain: tuple[str, ...],
                  pkg_name: str, recursive: bool = True) -> None:
        files = self._py_sources(pkg_dir, recursive)
        # dir node for the package root and every subdir containing py files
        for d in sorted({p.parent for p in files}):
            if d == self.root:
                continue        # already added as ROOT_DIR_ID
            rel = d.relative_to(self.root)
            nid = "dir:" + str(rel)
            if nid not in self.graph.nodes:
                parent = self._dir_parent(rel)
                self.graph.add_node(Node(nid, "dir", d.name, parent,
                                         None, None))
        for p in files:
            rel = p.relative_to(self.root)
            mod_id = self._mod_id(rel)
            if mod_id in self.graph.nodes:
                # Deterministic first-wins: `files` is sorted, so the same tree
                # always keeps the same file. Recorded rather than asserted --
                # a colliding path is a fact about the tree, not a bug here.
                self.stats["duplicate_modules"] += 1
                self.graph.notes.append(
                    f"duplicate-module-id {rel} collides with "
                    f"{self.graph.nodes[mod_id].file}")
                continue
            parent = (ROOT_DIR_ID if rel.parent == Path(".")
                      else "dir:" + str(rel.parent))
            self.graph.add_node(Node(mod_id, "module", p.stem, parent,
                                     str(rel), 1, "", not p.stem.startswith("_")))
            self.graph.files[str(rel)] = self._file_hash(p)
            self._py_files.append((rel, p))

    def source_files(self) -> list[tuple[Path, Path]]:
        """(rel, abs) for every file this indexer would index -- without parsing.

        The staleness check has to compare against exactly the set the index
        covers. Deriving that set a second time is how two implementations of
        one rule drift apart, so it is derived here, from the same two calls
        the skeleton phase makes.
        """
        seen: dict[str, tuple[Path, Path]] = {}
        for pkg_dir, _chain, _name, recursive in self._find_packages():
            for p in self._py_sources(pkg_dir, recursive):
                rel = p.relative_to(self.root)
                seen.setdefault(mod_id_for(rel), (rel, p))
        return sorted(seen.values())

    def _py_sources(self, pkg_dir: Path, recursive: bool) -> list[Path]:
        """Indexable .py files under pkg_dir, sorted (order-independent input)."""
        it = pkg_dir.rglob("*.py") if recursive else pkg_dir.glob("*.py")
        out = []
        for p in it:
            if p.name.startswith(".") or not p.is_file():
                continue
            if any(_skip_dir(part) for part in p.relative_to(pkg_dir).parts[:-1]):
                continue
            out.append(p)
        return sorted(out)

    def _dir_parent(self, rel: Path) -> str | None:
        return ("dir:" + str(rel.parent) if rel.parent != Path(".")
                else ROOT_DIR_ID)

    def _mod_id(self, rel: Path) -> str:
        return mod_id_for(rel)

    def _file_hash(self, p: Path) -> str:
        return file_hash(p)

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
        if not self._add_member(Node(cid, "class", stmt.name, mod_id, str(rel),
                                     stmt.lineno,
                                     doc.split("\n")[0] if doc else "",
                                     not stmt.name.startswith("_"))):
            return
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
        if not self._add_member(Node(fid, "function", stmt.name,
                                     parent_id or mod_id, str(rel), stmt.lineno,
                                     doc.split("\n")[0] if doc else "",
                                     public)):
            return
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

    def _index_names(self) -> None:
        """Short name -> class/function ids, in node insertion order.

        Attribute-call and base-class resolution used to scan every node for
        every name, which is quadratic: on a 5.5k-node tree that scan was the
        whole run time. Phase 1 adds every class and function before phase 2
        starts and phase 2 adds only edges, so one snapshot taken here is
        exactly the list the old scan produced -- same members, same order.
        """
        self._by_name = {}
        for n in self.graph.nodes.values():
            if n.kind in ("class", "function"):
                self._by_name.setdefault(n.name, []).append(n.id)

    def _named(self, name: str) -> list[str]:
        return self._by_name.get(name, [])

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
        for pkg_dir, chain, pkg_name, _rec in self._find_packages():
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
            matches = self._named(name)
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
        matches = self._named(name)
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

    def _add_member(self, node: Node) -> bool:
        """Add a class/function node, or record a redefinition and decline.

        A property and its setter are two `def cursor_position` statements for
        one attribute; conditional definitions do the same. From a caller's
        side there is one symbol, so the map keeps one node -- the first in
        source order, which is deterministic -- and records the rest.
        """
        seen = self.graph.nodes.get(node.id)
        if seen is not None:
            self.stats["redefinitions"] += 1
            self.graph.notes.append(
                f"redefinition {node.file}:{node.line} {node.id} "
                f"(first defined at line {seen.line})")
            return False
        self.graph.add_node(node)
        return True

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
