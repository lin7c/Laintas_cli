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
* atlas.lookup is also exposed as a model-callable tool (toolPrefix
  "atlas.") so agents can query exact dependency paths instead of grepping.
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
}


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
            ("serve [--out <dir>] [--port <p>]", "Open the layered browser viewer"),
            ("workflow <out_dir> <source_root> [--lang <l>]", "Print the annotation pipeline command"),
        ])
    try:
        ctx.register_tool("atlas.lookup", atlas_lookup, TOOL_SPEC["atlas.lookup"])
    except Exception:
        pass  # older CLI without tool registration: commands still work
