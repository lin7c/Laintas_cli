"""Extension: code-atlas

Deterministic code topology graph + HWO/HWG semantic annotation pipeline.

Commands
--------
    /atlas index <root> [--package <dir>] [--out <dir>]
                            deterministic index -> graph.db + graph.json
    /atlas verify <out_dir> deterministic post-conditions on the index
    /atlas annotations <out_dir> [ann_dir]
                            run the annotation verifier gate
    /atlas lookup <out_dir> <src_module> <dst_module>
                            deterministic transitive dependency paths
    /atlas find <name> [--out <dir>] [--kind class|function|module]
                            where a symbol is defined -> file:line
    /atlas outline <module> [--out <dir>] [--private]
                            what a module declares, without reading the file
    /atlas neighbors <node_id> [--out <dir>]
                            who reaches a node and what it reaches
    /atlas stale [--out <dir>]
                            whether the index still matches the files on disk
    /atlas map [<out_dir>] [--depth N]
                            open the atlas on the terminal's infinite canvas
                            (wheel zooms, and zooming goes deeper: packages ->
                            modules -> classes -> functions)
    /atlas serve <out_dir> [--port <p>]
                            serve the multi-layer web view (DSM + nested graph)
    /atlas workflow <out_dir> <source_root> [--lang <lang>]
                            print the ready-to-run command for the semantic
                            annotation pipeline (it is long-running and spends
                            tokens, so it is not started from here)

Design notes
------------
* The indexer is deterministic and lives in the standalone code-atlas repo.
  This extension is a thin adapter: it invokes that repo's main.py and
  core/store.py, and never reimplements indexing logic.
* The query commands are also model-callable tools (toolPrefix "atlas.") so
  agents can ask the index instead of grepping the tree. atlas.stale is the
  one that keeps the rest honest: a cached index that quietly stopped
  matching the files answers confidently and wrongly, which is the failure
  mode every other tool here would otherwise hide.
* The annotation pipeline is workflows/run.py (a script, not a workflow
  graph — workflows/legacy-hwg/README.md records why). /atlas workflow prints
  the command with inputs filled in rather than launching a job that runs for
  a quarter of an hour and bills tokens.
* /atlas map renders the same scene.json the web view renders, so a codebase
  looks the same in the terminal and in the browser.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

_ctx = None

# The official package vendors its deterministic core, workflows, and browser
# viewer so it never depends on a separate Code Atlas checkout.
_REPO = Path(__file__).resolve().parent


def _py(root: str, *args: str) -> int:
    r = subprocess.run(
        [sys.executable, str(_REPO / "atlas_cli.py"), *args],
        cwd=root, capture_output=False)
    return r.returncode


def handle(parts, raw_line: str = "") -> None:
    if not parts:
        _print(__doc__)
        return
    cmd, args = parts[0], parts[1:]
    cwd = str(Path(_ctx.cwd).resolve())

    if cmd == "index":
        if not args:
            _print("usage: /atlas index <root> [--package <dir>] [--out <dir>]")
            return
        code = _py(cwd, "index", *args)
        out = _arg(args, "--out") or ".atlas"
        atlas_dir = Path(cwd) / out
        if (atlas_dir / "graph.json").is_file():
            sys.path.insert(0, str(_REPO))
            from code_atlas_core.scene import write_scene
            write_scene(atlas_dir)
        _print(f"index exit={code}")

    elif cmd == "verify":
        out = _arg(args, "--out") or ".atlas"
        code = _py(cwd, "verify", cwd, "--out", out)
        _print(f"verify exit={code}")

    elif cmd == "annotations":
        out = args[0] if args else ".atlas"
        ann = _arg(args, "--dir") or str(Path(out) / "annotations")
        r = subprocess.run(
            [sys.executable, str(_REPO / "workflows" / "verify.py"),
             str(Path(cwd) / out), ann],
            capture_output=True, text=True)
        _print(r.stdout or r.stderr)

    elif cmd == "lookup":
        if len(args) < 3:
            _print("usage: /atlas lookup <out_dir> <src_module> <dst_module>")
            return
        out_dir, src, dst = args[0], args[1], args[2]
        sys.path.insert(0, str(_REPO))
        from code_atlas_core.store import dependency_paths
        paths = dependency_paths(Path(cwd) / out_dir / "graph.db", src, dst)
        if not paths:
            _print(f"no dependency path {src} -> {dst} (depth<=6)")
            return
        _print(f"{len(paths)} path(s) {src} -> {dst}:")
        for p in paths:
            _print("  " + " -> ".join(x.replace("mod:", "") for x in p))

    elif cmd in ("find", "outline", "neighbors", "stale"):
        out = _arg(args, "--out") or ".atlas"
        db = Path(cwd) / out / "graph.db"
        if not db.is_file():
            _print(f"no index at {db} -- run /atlas index {cwd} first")
            return
        positional = [a for a in args if not a.startswith("--")]
        if cmd != "stale" and not positional:
            _print(f"usage: /atlas {cmd} <argument> [--out <dir>]")
            return
        sys.path.insert(0, str(_REPO))
        from code_atlas_core import store
        if cmd == "find":
            result = store.find_symbol(db, positional[0], _arg(args, "--kind"))
        elif cmd == "outline":
            result = store.outline(db, positional[0], "--private" in args)
        elif cmd == "neighbors":
            result = store.neighbors(db, positional[0])
        else:
            result = store.stale(db, cwd)
        _print(json.dumps(result, ensure_ascii=False, indent=1))

    elif cmd == "map":
        # The same scene the web view renders, on the terminal's infinite
        # canvas. Layout comes from code_atlas_core/scene.py so both front ends put a
        # given module in the same place — a map you learned in one is the
        # map you get in the other.
        out = args[0] if (args and not args[0].startswith("-")) else (
            _arg(args, "--out") or ".atlas")
        atlas_dir = Path(cwd) / out
        if not (atlas_dir / "graph.json").is_file():
            _print(f"no graph.json in {atlas_dir} — run /atlas index first")
            return
        sys.path.insert(0, str(_REPO))
        from code_atlas_core.scene import build_from_dir
        depth = int(_arg(args, "--depth") or 4)
        data = build_from_dir(atlas_dir, max_depth=depth)
        try:
            import canvas_view
            import infinite_canvas
        except ImportError as e:
            _print(f"this CLI has no canvas viewer ({e}); "
                   f"run `python3 main.py scene --out {out}` and open scene.json")
            return
        scene = infinite_canvas.scene_from_json(data)
        _print(f"{data['subtitle']} — wheel zooms, / searches, q quits")
        canvas_view.open_scene(scene, title=f"atlas: {data['title']}")

    elif cmd == "serve":
        out = _arg(args, "--out") or ".atlas"
        port = _arg(args, "--port") or "5173"
        atlas_dir = Path(cwd) / out
        if not (atlas_dir / "graph.json").is_file():
            _print(f"no graph.json in {atlas_dir} — run /atlas index first")
            return
        site = atlas_dir / ".viewer"
        shutil.copytree(_REPO / "viewer", site, dirs_exist_ok=True)
        # scene.json is what the view renders; build it if the atlas has been
        # re-indexed since the last one was written.
        sys.path.insert(0, str(_REPO))
        from code_atlas_core.scene import write_scene
        write_scene(atlas_dir)
        for name in ("scene.json", "graph.json", "annotations.json",
                     "overview.json", "features.json"):
            src = atlas_dir / name
            if src.is_file():
                (site / name).write_bytes(src.read_bytes())
        _print(f"serving code-atlas web on http://localhost:{port} ...")
        subprocess.Popen(
            [sys.executable, "-m", "http.server", port, "--bind", "127.0.0.1",
             "--directory", str(site)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        webbrowser.open(f"http://localhost:{port}")

    elif cmd == "workflow":
        if len(args) < 2:
            _print("usage: /atlas workflow <out_dir> <source_root> [--lang <lang>]")
            return
        out_dir, source_root = args[0], args[1]
        lang = _arg(args, "--lang") or "en"
        atlas_abs = (Path(cwd) / out_dir).resolve()
        source_abs = (Path(cwd) / source_root).resolve()
        # The pipeline is a script, not a workflow graph (see
        # workflows/legacy-hwg/README.md for why). It is long-running and
        # spends real tokens, so this prints the command rather than starting
        # it behind the user's back.
        _print("Run the annotation pipeline:")
        _print(f"  python3 {_REPO}/workflows/run.py {atlas_abs} {source_abs} "
               f"--lang {lang} --shards 4")
        _print("  (add --resume to continue an interrupted run)")

    else:
        _print(f"unknown subcommand: {cmd}\n" + __doc__)


def _arg(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _print(msg) -> None:
    try:
        _ctx.print(msg)
    except Exception:
        print(msg)


# ---- model-callable tool surface (toolPrefix "atlas.") ----

TOOL_SPEC = {
    "atlas.lookup": {
        "description": "Query deterministic transitive dependency paths "
                       "between two modules in the indexed code graph. "
                       "Returns exact paths (no guessing, no embeddings).",
        "parameters": {
            "type": "object",
            "properties": {
                "out_dir": {"type": "string",
                            "description": "atlas output dir (default .atlas)"},
                "src": {"type": "string", "description": "source module id"},
                "dst": {"type": "string", "description": "target module id"},
            },
            "required": ["src", "dst"],
        },
    },
    "atlas.find": {
        "description": "Locate a class, function or module by name in the "
                       "deterministic code index. Returns exact file:line "
                       "definitions -- use this instead of grepping for a "
                       "definition.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "symbol name"},
                "kind": {"type": "string", "enum": ["class", "function", "module"],
                         "description": "restrict to one kind (optional)"},
                "out_dir": {"type": "string",
                            "description": "atlas output dir (default .atlas)"},
            },
            "required": ["name"],
        },
    },
    "atlas.outline": {
        "description": "List what a module declares (classes with their "
                       "methods, and top-level functions) with line numbers, "
                       "without reading the file.",
        "parameters": {
            "type": "object",
            "properties": {
                "module": {"type": "string",
                           "description": "module id, dotted name, or path"},
                "include_private": {"type": "boolean",
                                    "description": "include _underscore names"},
                "out_dir": {"type": "string",
                            "description": "atlas output dir (default .atlas)"},
            },
            "required": ["module"],
        },
    },
    "atlas.neighbors": {
        "description": "Every edge touching a node: what it imports, calls or "
                       "inherits, and -- the direction grep is worst at -- "
                       "everything that reaches it, with file:line evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string",
                            "description": "node id, e.g. mod:gateway.serve"},
                "out_dir": {"type": "string",
                            "description": "atlas output dir (default .atlas)"},
            },
            "required": ["node_id"],
        },
    },
    "atlas.stale": {
        "description": "Whether the index still describes the files on disk. "
                       "Call this before trusting the other atlas tools on a "
                       "tree that may have changed; if it reports stale, "
                       "re-index or fall back to reading the files.",
        "parameters": {
            "type": "object",
            "properties": {
                "out_dir": {"type": "string",
                            "description": "atlas output dir (default .atlas)"},
            },
            "required": [],
        },
    },
}


def _db(out_dir: str) -> Path:
    return Path(str(Path(_ctx.cwd).resolve())) / out_dir / "graph.db"


def _query(fn_name: str, out_dir: str, *args) -> str:
    db = _db(out_dir)
    if not db.is_file():
        return json.dumps({"error": f"no index at {db}",
                           "hint": "run /atlas index <root> first"})
    sys.path.insert(0, str(_REPO))
    from code_atlas_core import store
    return json.dumps(getattr(store, fn_name)(db, *args), ensure_ascii=False)


def atlas_find(name: str = "", kind: str | None = None,
               out_dir: str = ".atlas") -> str:
    return _query("find_symbol", out_dir, name, kind)


def atlas_outline(module: str = "", include_private: bool = False,
                  out_dir: str = ".atlas") -> str:
    return _query("outline", out_dir, module, include_private)


def atlas_neighbors(node_id: str = "", out_dir: str = ".atlas") -> str:
    return _query("neighbors", out_dir, node_id)


def atlas_stale(out_dir: str = ".atlas") -> str:
    return _query("stale", out_dir, str(Path(_ctx.cwd).resolve()))


def atlas_lookup(out_dir: str = ".atlas", src: str = "", dst: str = "") -> str:
    cwd = str(Path(_ctx.cwd).resolve())
    sys.path.insert(0, str(_REPO))
    from code_atlas_core.store import dependency_paths
    paths = dependency_paths(Path(cwd) / out_dir / "graph.db", src, dst)
    if not paths:
        return json.dumps({"paths": [], "src": src, "dst": dst})
    return json.dumps({
        "paths": [[x.replace("mod:", "") for x in p] for p in paths],
        "src": src, "dst": dst})


def setup(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_command(
        "atlas", handle,
        description="Deterministic code topology maps and semantic annotations",
        subcommands=[
            ("index <root> [--package <dir>] [--out <dir>]", "Build a deterministic index"),
            ("verify [--out <dir>]", "Verify index postconditions"),
            ("annotations <out_dir>", "Verify semantic annotation files"),
            ("lookup <out_dir> <src> <dst>", "Find exact transitive dependency paths"),
            ("find <name> [--kind k] [--out d]", "Where a symbol is defined -> file:line"),
            ("outline <module> [--private] [--out d]", "What a module declares"),
            ("neighbors <node_id> [--out d]", "Who reaches a node and what it reaches"),
            ("stale [--out d]", "Does the index still match the files on disk"),
            ("serve [--out <dir>] [--port <p>]", "Open the layered browser viewer"),
            ("workflow <out_dir> <source_root> [--lang <l>]", "Print the annotation pipeline command"),
        ])
    for tool_name, fn in (("atlas.lookup", atlas_lookup),
                          ("atlas.find", atlas_find),
                          ("atlas.outline", atlas_outline),
                          ("atlas.neighbors", atlas_neighbors),
                          ("atlas.stale", atlas_stale)):
        try:
            ctx.register_tool(tool_name, fn, TOOL_SPEC[tool_name])
        except Exception:
            pass  # older CLI without tool registration: commands still work
