#!/usr/bin/env python3
"""The annotation pipeline, as a script.

This replaces ``annotate.hwg``. The pipeline it runs is the same one that
graph described — overview -> feature -> annotate (shards in parallel) ->
consolidate -> bounded resolve -> verify -> publish — and the prompts are the
same prompts, now plain files under ``prompts/``. What is gone is the DSL
between them.

The reason is measured, not aesthetic. The HWG run of this exact pipeline
(``.laintas/events.jsonl``, 396 events, 61 minutes) spent three of its
seventeen critic checkpoints stuck on the workflow language's own syntax —
the model was writing HWO, failing its validator, and rewriting it, while the
codebase it was supposed to be reading sat untouched. One shard of four came
out. For a pipeline whose shape is fixed and known at build time, a graph DSL
buys nothing that a `for` loop does not, and it charges an extra language for
the model to get right at runtime.

What the DSL *did* provide is kept, because it was the valuable part:

  durable state / resume  -> a checkpoint file, `--resume` skips finished stages
  bounded loops           -> `resolve` runs at most MAX_RESOLVE_ROUNDS times
  parallel agents         -> a thread pool over the shards
  per-node tool limits    -> a project mode that narrows the agent's tools
  a verdict gate          -> stages fail on their *output file*, not on prose

And two stages that were agents are not agents any more: `verify` is
``verify.py`` and `publish` is a dictionary merge. Asking a model to run a
checker and report its result faithfully is a chance for it to report a pass
that did not happen; asking it to concatenate JSON is paying tokens for
``json.load``.

Usage:
    python3 run.py <atlas_dir> <source_root> [--lang en] [--shards 4]
                   [--resume] [--only STAGE] [--timeout SECONDS]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PROMPTS = HERE / "prompts"

CLI = Path(os.environ.get("LAINTAS_CLI") or shutil.which("laintas-cli")
           or "/usr/local/bin/laintas-cli")
MAX_RESOLVE_ROUNDS = 3
DEFAULT_TIMEOUT = 1800

# The agent writes files and runs read-only shell one-liners against the
# indexed tree; it has no business spawning more agents (which is also how a
# single run quietly becomes several runs' worth of spend) or touching the
# network.
MODE = {
    "description": "code-atlas annotation stage",
    "instructions": (
        "You are one stage of a deterministic code-atlas pipeline. Produce "
        "exactly the output file you were asked for, then stop. Never modify "
        "the indexed source tree, graph.db or graph.json."),
    "allowed_tools": ["fs.read", "fs.ls", "fs.glob", "fs.grep", "fs.write",
                      "fs.edit", "fs.multi_edit", "shell.exec", "task.*"],
    "denied_tools": ["agent.spawn", "fs.delete", "web.*", "browser.*"],
    "auto_approve": "all",
}


# ── plumbing ──────────────────────────────────────────────────────────────

def log(stage: str, message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {stage:<11} {message}", flush=True)


def graph_hash(atlas_dir: Path) -> str:
    con = sqlite3.connect(str(atlas_dir / "graph.db"))
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key='graph_hash'").fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit(f"{atlas_dir}/graph.db has no graph_hash")
    return row[0]


def load_state(atlas_dir: Path) -> dict:
    path = atlas_dir / ".pipeline.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"stages": {}}


def save_state(atlas_dir: Path, state: dict) -> None:
    (atlas_dir / ".pipeline.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def prepare_workspace(cwd: Path) -> None:
    """Give the agent a project mode with a narrowed tool set.

    The mode is project-level (``<cwd>/.laintas/modes.json``) so loosening
    approvals here does not loosen them anywhere else, and it is selected
    through a pinned terminal id because a subprocess has no tty to derive one
    from — without the pin the preference file the CLI reads is a different
    file each run and the mode silently never activates.
    """
    (cwd / ".laintas").mkdir(parents=True, exist_ok=True)
    config = {"version": 1, "active": "atlas", "modes": {"atlas": MODE}}
    (cwd / ".laintas" / "modes.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")

    prefs_dir = Path(os.path.expanduser("~/.laintas/sessions"))
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / f"{terminal_id()}_preferences.json").write_text(
        json.dumps({"version": 1, "mode": "atlas"}), encoding="utf-8")


def terminal_id() -> str:
    return os.environ.get("ATLAS_TERMINAL_ID", "code-atlas-pipeline")


def run_agent(prompt: str, cwd: Path, timeout: int) -> tuple[bool, str]:
    """One stage = one unattended agent run. Its verdict is its output file."""
    env = dict(os.environ)
    env["LAINTAS_TERMINAL_ID"] = terminal_id()
    try:
        proc = subprocess.run(
            [sys.executable, str(CLI), "--execute", prompt],
            cwd=str(cwd), env=env, timeout=timeout,
            capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return (False, f"timed out after {timeout}s")
    tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-1000:]
    return (proc.returncode == 0, tail)


def read_prompt(name: str, **fields) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").format(**fields)


def valid_json(path: Path, kind=None) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, kind) if kind else True


# ── stages ────────────────────────────────────────────────────────────────

def stage_shard(ctx: dict) -> dict:
    """Deterministic: split the module list into N shards."""
    out = subprocess.run(
        [sys.executable, str(HERE / "shard.py"), str(ctx["atlas_dir"]),
         str(ctx["atlas_dir"] / "shards"), "--shards", str(ctx["shards"])],
        capture_output=True, text=True)
    files = sorted((ctx["atlas_dir"] / "shards").glob("shard-*.json"))
    if not files:
        raise SystemExit(f"sharding produced nothing: {out.stdout}{out.stderr}")
    return {"shards": [f.name for f in files]}


def stage_overview(ctx: dict) -> dict:
    target = ctx["atlas_dir"] / "overview.json"
    ok, tail = run_agent(read_prompt(
        "overview", atlas_dir=ctx["atlas_dir"], source_root=ctx["source_root"],
        graph_hash=ctx["graph_hash"], output_lang=ctx["lang"]),
        ctx["cwd"], ctx["timeout"])
    if not valid_json(target, dict):
        raise StageFailed(f"overview.json missing or unreadable. {tail[-400:]}")
    data = json.loads(target.read_text(encoding="utf-8"))
    return {"features": len(data.get("features") or [])}


def stage_feature(ctx: dict) -> dict:
    target = ctx["atlas_dir"] / "features.json"
    run_agent(read_prompt(
        "feature", atlas_dir=ctx["atlas_dir"], source_root=ctx["source_root"],
        graph_hash=ctx["graph_hash"], output_lang=ctx["lang"]),
        ctx["cwd"], ctx["timeout"])
    if not valid_json(target, dict):
        raise StageFailed("features.json missing or unreadable")
    data = json.loads(target.read_text(encoding="utf-8"))
    return {"features": len(data.get("features") or [])}


def stage_annotate(ctx: dict) -> dict:
    """The parallel stage: one agent per shard, all at once."""
    ann_dir = ctx["atlas_dir"] / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    shards = sorted((ctx["atlas_dir"] / "shards").glob("shard-*.json"))

    def one(shard: Path) -> tuple[str, int, str]:
        stem = shard.stem                       # shard-01
        target = ann_dir / f"{stem}.json"
        if valid_json(target, list):            # resume mid-stage
            return (stem, len(json.loads(target.read_text())), "cached")
        prompt = read_prompt(
            "annotate", shard_file=shard, output_file=target,
            prefix=f"{stem}-", atlas_dir=ctx["atlas_dir"],
            source_root=ctx["source_root"], graph_hash=ctx["graph_hash"],
            output_lang=ctx["lang"])
        ok, tail = run_agent(prompt, ctx["cwd"], ctx["timeout"])
        if not valid_json(target, list):
            return (stem, 0, tail[-200:] or "no output file")
        return (stem, len(json.loads(target.read_text())), "ok")

    with ThreadPoolExecutor(max_workers=min(4, len(shards) or 1)) as pool:
        results = list(pool.map(one, shards))

    for stem, count, note in results:
        log("annotate", f"{stem}: {count} annotation(s) [{note}]")
    written = [r for r in results if r[1] > 0]
    if not written:
        raise StageFailed("no shard produced annotations")
    if len(written) < len(results):
        # Partial output is the pipeline's normal failure mode and the one
        # that used to pass unnoticed: the run "succeeded" with a quarter of
        # the codebase described.
        log("annotate", f"WARNING: {len(results) - len(written)} shard(s) "
                        f"produced nothing")
    return {"shards": {stem: count for stem, count, _ in results}}


def stage_consolidate(ctx: dict) -> dict:
    ann_dir = ctx["atlas_dir"] / "annotations"
    # Clear this stage's own outputs first. Otherwise a stage that produces
    # nothing inherits the previous run's files and the pipeline publishes a
    # glossary and a conflict list belonging to a graph that no longer exists
    # — the failure looks exactly like success.
    for name in ("conflicts.json", "glossary-updates.json"):
        (ann_dir / name).unlink(missing_ok=True)
    run_agent(read_prompt(
        "consolidate", atlas_dir=ctx["atlas_dir"],
        graph_hash=ctx["graph_hash"], output_lang=ctx["lang"]),
        ctx["cwd"], ctx["timeout"])
    conflicts = ann_dir / "conflicts.json"
    glossary = ann_dir / "glossary-updates.json"
    for path, kind in ((conflicts, list), (glossary, list)):
        if not valid_json(path, kind):
            path.write_text("[]", encoding="utf-8")   # absent = none found
    return {"conflicts": len(json.loads(conflicts.read_text(encoding="utf-8"))),
            "glossary": len(json.loads(glossary.read_text(encoding="utf-8")))}


def stage_resolve(ctx: dict) -> dict:
    conflicts = ctx["atlas_dir"] / "annotations" / "conflicts.json"
    rounds = 0
    while rounds < MAX_RESOLVE_ROUNDS:
        open_now = json.loads(conflicts.read_text(encoding="utf-8")) \
            if valid_json(conflicts, list) else []
        if not open_now:
            return {"rounds": rounds, "remaining": 0}
        rounds += 1
        log("resolve", f"round {rounds}: {len(open_now)} conflict(s)")
        run_agent(read_prompt(
            "resolve", atlas_dir=ctx["atlas_dir"],
            source_root=ctx["source_root"], output_lang=ctx["lang"],
            round=rounds, max_rounds=MAX_RESOLVE_ROUNDS),
            ctx["cwd"], ctx["timeout"])
    left = json.loads(conflicts.read_text(encoding="utf-8")) \
        if valid_json(conflicts, list) else []
    return {"rounds": rounds, "remaining": len(left)}


def stage_verify(ctx: dict) -> dict:
    """Deterministic gate. Never an agent: a checker that can be talked out
    of its result is not a gate."""
    proc = subprocess.run(
        [sys.executable, str(HERE / "verify.py"), str(ctx["atlas_dir"]),
         str(ctx["atlas_dir"] / "annotations")],
        capture_output=True, text=True)
    summary = (proc.stdout or proc.stderr).strip()
    log("verify", summary.splitlines()[0] if summary else "(no output)")
    if proc.returncode != 0:
        raise StageFailed(summary)
    return {"summary": summary}


def stage_publish(ctx: dict) -> dict:
    """Deterministic merge + report. Also a plain function, for the same
    reason: concatenating JSON is not a judgement call."""
    atlas_dir = ctx["atlas_dir"]
    ann_dir = atlas_dir / "annotations"
    annotations: list[dict] = []
    for path in sorted(ann_dir.glob("shard-*.json")):
        if path.name.endswith(".rejected.json"):
            continue
        annotations.extend(json.loads(path.read_text(encoding="utf-8")))
    glossary = []
    gpath = ann_dir / "glossary-updates.json"
    if valid_json(gpath, list):
        glossary = json.loads(gpath.read_text(encoding="utf-8"))

    merged = {"annotations": annotations, "glossary_updates": glossary,
              "graph_hash": ctx["graph_hash"]}
    (atlas_dir / "annotations.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")

    # the scene the front ends render, rebuilt with the new annotations
    sys.path.insert(0, str(REPO))
    from code_atlas_core.scene import write_scene
    scene_path = write_scene(atlas_dir)

    web_public = REPO / "web" / "public"
    if web_public.is_dir():
        shutil.copy(atlas_dir / "annotations.json", web_public / "annotations.json")
        shutil.copy(scene_path, web_public / "scene.json")
        shutil.copy(atlas_dir / "graph.json", web_public / "graph.json")
        for name in ("overview.json", "features.json"):
            if (atlas_dir / name).is_file():
                shutil.copy(atlas_dir / name, web_public / name)

    by_type: dict[str, int] = {}
    by_trust: dict[str, int] = {}
    for ann in annotations:
        by_type[ann.get("target_type", "?")] = by_type.get(ann.get("target_type", "?"), 0) + 1
        by_trust[ann.get("trust_level", "?")] = by_trust.get(ann.get("trust_level", "?"), 0) + 1
    conflicts = []
    cpath = ann_dir / "conflicts.json"
    if valid_json(cpath, list):
        conflicts = json.loads(cpath.read_text(encoding="utf-8"))

    report = [
        f"# Annotation report", "",
        f"- graph_hash: `{ctx['graph_hash']}`",
        f"- annotations: {len(annotations)}",
        f"- by target_type: {by_type}",
        f"- by trust_level: {by_trust}",
        f"- glossary additions: {len(glossary)}",
        f"- conflicts left unresolved: {len(conflicts)}",
        "",
        "## Verifier", "",
        "```", str(ctx.get("verify_summary", "")).strip(), "```", "",
    ]
    (atlas_dir / "ANNOTATION-REPORT.md").write_text(
        "\n".join(report), encoding="utf-8")
    return {"annotations": len(annotations), "glossary": len(glossary),
            "conflicts": len(conflicts), "scene": str(scene_path)}


class StageFailed(RuntimeError):
    """A stage did not produce the file it was asked for."""


STAGES = [
    ("shard", stage_shard),
    ("overview", stage_overview),
    ("feature", stage_feature),
    ("annotate", stage_annotate),
    ("consolidate", stage_consolidate),
    ("resolve", stage_resolve),
    ("verify", stage_verify),
    ("publish", stage_publish),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="code-atlas annotation pipeline")
    ap.add_argument("atlas_dir")
    ap.add_argument("source_root")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--resume", action="store_true",
                    help="skip stages already recorded as done")
    ap.add_argument("--only", default=None, help="run a single stage")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args()

    atlas_dir = Path(args.atlas_dir).resolve()
    source_root = Path(args.source_root).resolve()
    if not (atlas_dir / "graph.db").is_file():
        raise SystemExit(f"no graph.db in {atlas_dir} — run the indexer first")

    cwd = REPO
    prepare_workspace(cwd)
    state = load_state(atlas_dir) if args.resume else {"stages": {}}
    ctx = {
        "atlas_dir": atlas_dir, "source_root": source_root, "cwd": cwd,
        "graph_hash": graph_hash(atlas_dir), "lang": args.lang,
        "shards": args.shards, "timeout": args.timeout,
    }

    started = time.time()
    for name, fn in STAGES:
        if args.only and name != args.only:
            continue
        done = state["stages"].get(name, {})
        if args.resume and done.get("status") == "done":
            log(name, "skipped (already done)")
            if name == "verify":
                ctx["verify_summary"] = done.get("result", {}).get("summary", "")
            continue
        log(name, "start")
        t0 = time.time()
        try:
            result = fn(ctx)
        except StageFailed as e:
            state["stages"][name] = {"status": "failed", "error": str(e),
                                     "ts": time.time()}
            save_state(atlas_dir, state)
            log(name, f"FAILED: {e}")
            return 1
        if name == "verify":
            ctx["verify_summary"] = result.get("summary", "")
        state["stages"][name] = {"status": "done", "result": result,
                                 "seconds": round(time.time() - t0, 1),
                                 "ts": time.time()}
        save_state(atlas_dir, state)
        log(name, f"done in {time.time() - t0:.0f}s {result}")

    log("pipeline", f"complete in {(time.time() - started) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
