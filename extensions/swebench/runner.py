#!/usr/bin/env python3
"""Generate SWE-bench predictions with laintas-cli.

This adapter deliberately stops at `predictions.jsonl`. Scoring belongs to the
official `swebench.harness.run_evaluation` running in its own containers: a
benchmark whose author also writes the scorer is not evidence anybody outside
the project should accept. What we produce is the one artifact the official
harness consumes, plus everything a reader needs to re-run it.

Per instance the flow is: check out the repo at `base_commit`, hand the agent
ONLY the problem statement, then take `git diff` as the model patch. The
instance's `test_patch` never touches the workspace -- the harness applies it
afterwards, and an agent that had seen it would be scoring itself.

Usage
-----
    python3 runner.py --dataset swe_bench_verified.jsonl --out ./run-01 \
                      --model deepseek-v4-flash --limit 50

    python3 runner.py --self-test        # plumbing check, no dataset, no network
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

ADAPTER_VERSION = "1.0.0"

# Narrow, unattended posture. The agent has to read and write the checkout and
# run tests inside it; it has no business spawning more agents (which quietly
# multiplies the spend of a 500-instance run) or reaching the network, and a
# benchmark run has nobody at the keyboard to approve anything.
MODE = {
    "description": "SWE-bench instance solving",
    "instructions": (
        "You are solving one issue in a checked-out repository, unattended. "
        "Make the smallest change that fixes the reported problem. Do not add, "
        "modify or delete test files -- the grader supplies its own tests. Do "
        "not commit, and do not touch git history."),
    "allowed_tools": ["fs.read", "fs.ls", "fs.glob", "fs.grep", "fs.diff",
                      "fs.write", "fs.edit", "fs.multi_edit", "shell.exec",
                      "task.*"],
    "denied_tools": ["agent.spawn", "web.*", "browser.*", "fs.delete"],
    "auto_approve": "all",
}

PROMPT = """You are working in a checked-out repository at {cwd}.

Fix the following issue. Change only what the fix requires.

<issue>
{problem}
</issue>

Rules for this run:
- Do NOT create, edit or delete any test file. The grader brings its own tests.
- Do NOT run `git commit`, `git checkout`, `git reset` or anything else that
  rewrites history; leave your work as uncommitted changes in the working tree.
- When the fix is in place, stop.
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def git(cwd, *args, timeout=600, check=False):
    proc = subprocess.run(("git",) + args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr[:400]}")
    return proc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ── dataset ────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement")


def load_instances(path: Path) -> list[dict]:
    """Read a SWE-bench split as JSONL or JSON, and reject a malformed row loudly.

    A missing `base_commit` silently becomes "solve the issue against whatever
    HEAD happens to be", which produces a patch that cannot be evaluated. Better
    to fail here than to discover it 500 instances later.
    """
    text = path.read_text(encoding="utf-8")
    rows: list[dict]
    stripped = text.lstrip()
    if stripped.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    for i, row in enumerate(rows):
        missing = [f for f in REQUIRED_FIELDS if not row.get(f)]
        if missing:
            raise ValueError(f"instance #{i} is missing {missing}")
    return rows


# ── workspace ──────────────────────────────────────────────────────────────

def prepare_workspace(inst: dict, work_root: Path, cache_root: Path) -> Path:
    """Check the instance's repo out at `base_commit`, and only that.

    Clones are cached per repo because a 500-instance split revisits the same
    dozen repositories many times, and a cold clone of e.g. sympy dominates the
    per-instance cost otherwise.
    """
    repo = inst["repo"]                      # "owner/name"
    cache = cache_root / repo.replace("/", "__")
    if not (cache / ".git").is_dir():
        cache.parent.mkdir(parents=True, exist_ok=True)
        log(f"cloning {repo} (first use)")
        git(cache_root, "clone", "--quiet", f"https://github.com/{repo}.git",
            str(cache), timeout=3600, check=True)

    ws = work_root / inst["instance_id"]
    if ws.exists():
        shutil.rmtree(ws)
    ws.parent.mkdir(parents=True, exist_ok=True)
    git(cache_root, "clone", "--quiet", "--no-checkout", "--local",
        str(cache), str(ws), timeout=900, check=True)

    fetched = git(ws, "checkout", "--quiet", inst["base_commit"])
    if fetched.returncode != 0:
        # A shallow or stale cache may not have the commit yet.
        git(cache, "fetch", "--quiet", "origin", timeout=1800)
        git(cache_root, "clone", "--quiet", "--no-checkout", "--local",
            str(cache), str(ws), timeout=900)
        git(ws, "checkout", "--quiet", inst["base_commit"], check=True)

    # The test patch is the grader's, and an agent that can read it is grading
    # itself. Assert rather than trust: this is the one mistake that silently
    # invalidates every number the run produces.
    assert "test_patch" not in os.listdir(ws), "test patch leaked into workspace"
    return ws


def install_mode(ws: Path, terminal_id: str, home: Path) -> None:
    """Project mode + pinned terminal id.

    Both halves are required. The mode file alone does nothing: mode selection
    is read from a per-terminal preferences file, and a subprocess with no tty
    derives a different terminal id on every run, so the mode silently never
    activates. Pinning the id makes the preference file stable.
    """
    (ws / ".laintas").mkdir(parents=True, exist_ok=True)
    (ws / ".laintas" / "modes.json").write_text(json.dumps(
        {"version": 1, "active": "swebench", "modes": {"swebench": MODE}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    prefs = home / "sessions"
    prefs.mkdir(parents=True, exist_ok=True)
    (prefs / f"{terminal_id}_preferences.json").write_text(
        json.dumps({"version": 1, "mode": "swebench"}), encoding="utf-8")


# ── patch extraction ───────────────────────────────────────────────────────

TEST_HINTS = ("tests/", "test/", "testing/")


def _is_test_path(path: str) -> bool:
    base = os.path.basename(path)
    return (base.startswith("test_") or base.endswith("_test.py")
            or base in ("conftest.py",)
            or any(seg in path for seg in TEST_HINTS))


def extract_patch(ws: Path, strip_tests: bool = True) -> tuple[str, list[str]]:
    """The agent's work as a unified diff, with test edits removed by default.

    The grader force-applies its own test patch after ours, so a model patch
    that also edits tests is at best ignored and at worst conflicts. Stripping
    them keeps the submission honest and the apply clean; the removed paths are
    returned so the run record can show exactly what was dropped.
    """
    # `git add -A -N` makes new files visible to `git diff` without staging
    # content, so a fix that adds a module is not silently lost.
    git(ws, "add", "-A", "-N")
    changed = [l.strip() for l in
               git(ws, "diff", "--name-only").stdout.splitlines() if l.strip()]
    dropped = [p for p in changed if strip_tests and _is_test_path(p)]
    keep = [p for p in changed
            if p not in dropped and not p.startswith(".laintas/")]
    if not keep:
        return "", dropped
    diff = git(ws, "diff", "--no-color", "--", *keep, timeout=300)
    return diff.stdout, dropped


# ── one instance ───────────────────────────────────────────────────────────

def solve(inst: dict, args, home: Path, work_root: Path, cache_root: Path,
          logs: Path) -> dict:
    iid = inst["instance_id"]
    started = time.time()
    record = {"instance_id": iid, "model_name_or_path": args.run_name,
              "model_patch": ""}
    meta = {"instance_id": iid, "status": "", "seconds": 0.0,
            "dropped_test_edits": [], "returncode": None}
    try:
        ws = prepare_workspace(inst, work_root, cache_root)
    except Exception as exc:
        meta["status"] = f"workspace-failed: {exc}"
        meta["seconds"] = round(time.time() - started, 1)
        return {"prediction": record, "meta": meta}

    terminal_id = f"swebench-{iid}"[:60]
    install_mode(ws, terminal_id, home)

    env = dict(os.environ)
    env["LAINTAS_HOME"] = str(home)
    env["LAINTAS_TERMINAL_ID"] = terminal_id
    prompt = PROMPT.format(cwd=ws, problem=inst["problem_statement"].strip())

    try:
        proc = subprocess.run(
            [sys.executable, str(args.cli), "--execute", prompt],
            cwd=str(ws), env=env, timeout=args.timeout,
            capture_output=True, text=True)
        meta["returncode"] = proc.returncode
        out, err = proc.stdout or "", proc.stderr or ""
        meta["status"] = "ok" if proc.returncode == 0 else "agent-nonzero"
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        meta["status"] = f"timeout-{args.timeout}s"

    # The transcript is part of the deliverable: a published result nobody can
    # inspect is a claim, not evidence.
    (logs / f"{iid}.log").write_text(
        f"=== stdout ===\n{out}\n=== stderr ===\n{err}\n", encoding="utf-8")

    try:
        patch, dropped = extract_patch(ws, strip_tests=not args.keep_test_edits)
        record["model_patch"] = patch
        meta["dropped_test_edits"] = dropped
        if not patch and meta["status"] == "ok":
            meta["status"] = "empty-patch"
    except Exception as exc:
        meta["status"] = f"diff-failed: {exc}"

    meta["seconds"] = round(time.time() - started, 1)
    if not args.keep_workspaces:
        shutil.rmtree(ws, ignore_errors=True)
    return {"prediction": record, "meta": meta}


# ── run ────────────────────────────────────────────────────────────────────

# Settings that must not change between the first run and a resume: each one
# would put predictions produced under different conditions into a single
# predictions.jsonl while the manifest still claims one set of conditions.
PINNED = ("run_name", "model", "provider", "strip_test_edits", "dataset_sha256")


def write_manifest(out: Path, args, dataset: Optional[Path], n: int) -> None:
    """Everything a third party needs to reproduce or challenge the run.

    Written once. A resume verifies the pinned settings against it and refuses
    to continue when they differ -- resuming a half-finished run with another
    model silently mixes two models' work into one submission, and the manifest
    would record only the second.
    """
    cli_dir = Path(args.cli).resolve().parent
    manifest = {
        "adapter_version": ADAPTER_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_name": args.run_name,
        "model": args.model,
        "provider": args.provider,
        "instances": n,
        "timeout_s": args.timeout,
        "strip_test_edits": not args.keep_test_edits,
        "dataset": str(dataset) if dataset else None,
        "dataset_sha256": sha256_file(dataset) if dataset and dataset.is_file() else None,
        "laintas_cli_commit": git(cli_dir, "rev-parse", "HEAD").stdout.strip() or None,
        "laintas_cli_dirty": bool(git(cli_dir, "status", "--porcelain").stdout.strip()),
        "python": sys.version.split()[0],
        "mode": MODE,
        "prompt_template": PROMPT,
    }
    path = out / "manifest.json"
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}
        drift = [k for k in PINNED
                 if existing.get(k) is not None and existing.get(k) != manifest.get(k)]
        if drift:
            raise SystemExit(
                "refusing to resume: this run directory was created with "
                f"different settings ({', '.join(drift)}). Existing: "
                + ", ".join(f"{k}={existing.get(k)!r}" for k in drift)
                + ". Use a fresh --out directory.")
        return                       # keep the original record intact
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def done_ids(predictions: Path) -> set:
    """Instance ids already written, so a killed run resumes instead of restarting."""
    if not predictions.is_file():
        return set()
    out = set()
    for line in predictions.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.add(json.loads(line)["instance_id"])
            except (ValueError, KeyError):
                continue
    return out


def run(args) -> int:
    out = Path(args.out).resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    work_root = out / "workspaces"
    cache_root = Path(args.cache).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)

    dataset = Path(args.dataset).resolve()
    instances = load_instances(dataset)
    if args.instances:
        wanted = {i.strip() for i in args.instances.split(",") if i.strip()}
        instances = [i for i in instances if i["instance_id"] in wanted]
    if args.limit:
        instances = instances[: args.limit]

    predictions = out / "predictions.jsonl"
    already = done_ids(predictions)
    todo = [i for i in instances if i["instance_id"] not in already]
    write_manifest(out, args, dataset, len(instances))
    log(f"{len(instances)} instance(s); {len(already)} already done; {len(todo)} to run")

    meta_path = out / "run_log.jsonl"
    for n, inst in enumerate(todo, 1):
        log(f"[{n}/{len(todo)}] {inst['instance_id']}")
        result = solve(inst, args, home, work_root, cache_root, out / "logs")
        with open(predictions, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(result["prediction"], ensure_ascii=False) + "\n")
        with open(meta_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(result["meta"], ensure_ascii=False) + "\n")
        m = result["meta"]
        log(f"    {m['status']} in {m['seconds']}s, "
            f"patch {len(result['prediction']['model_patch'])} bytes")
    log(f"predictions -> {predictions}")
    log("score with: python -m swebench.harness.run_evaluation "
        f"--predictions_path {predictions} --run_id {args.run_name}")
    return 0


# ── self test ──────────────────────────────────────────────────────────────

def self_test(args) -> int:
    """Exercise the plumbing on a local repo: no dataset, no network, no agent.

    Checks the parts that silently ruin a real run -- dataset validation, patch
    extraction, test-edit stripping, and resume bookkeeping.
    """
    import tempfile
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else f"  {extra}"))
        ok = ok and bool(cond)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "a@b.invalid")
        git(repo, "config", "user.name", "T")
        (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_app.py").write_text("def test():\n    pass\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")

        # An agent that fixes source AND edits a test.
        (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        (repo / "tests" / "test_app.py").write_text("def test():\n    assert True\n", encoding="utf-8")
        (repo / "newmod.py").write_text("VALUE = 3\n", encoding="utf-8")

        patch, dropped = extract_patch(repo, strip_tests=True)
        chk("patch contains the source fix", "app.py" in patch and "return 2" in patch)
        chk("patch contains a newly added file", "newmod.py" in patch, patch[:200])
        chk("test edit is stripped", "test_app.py" not in patch)
        chk("stripped path is reported", dropped == ["tests/test_app.py"], dropped)

        patch2, _ = extract_patch(repo, strip_tests=False)
        chk("keep_test_edits keeps them", "test_app.py" in patch2)

        chk("test path detector", all(_is_test_path(p) for p in
            ("tests/test_x.py", "a/test/b.py", "pkg/foo_test.py", "conftest.py")))
        chk("non-test path detector", not any(_is_test_path(p) for p in
            ("src/latest.py", "core/contest.py", "app.py")))

        ds = tmp / "d.jsonl"
        ds.write_text(json.dumps({"instance_id": "x__y-1", "repo": "x/y",
                                  "base_commit": "abc", "problem_statement": "p"}) + "\n",
                      encoding="utf-8")
        chk("dataset loads", len(load_instances(ds)) == 1)
        bad = tmp / "bad.jsonl"
        bad.write_text(json.dumps({"instance_id": "z", "repo": "x/y"}) + "\n", encoding="utf-8")
        try:
            load_instances(bad)
            chk("malformed instance is rejected", False)
        except ValueError as exc:
            chk("malformed instance is rejected", "base_commit" in str(exc))

        pred = tmp / "p.jsonl"
        pred.write_text(json.dumps({"instance_id": "a", "model_patch": ""}) + "\n"
                        + "garbage\n"
                        + json.dumps({"instance_id": "b", "model_patch": ""}) + "\n",
                        encoding="utf-8")
        chk("resume skips finished ids and tolerates junk",
            done_ids(pred) == {"a", "b"}, done_ids(pred))

        ws = tmp / "ws"
        ws.mkdir()
        home = tmp / "home"
        install_mode(ws, "swebench-selftest", home)
        cfg = json.loads((ws / ".laintas" / "modes.json").read_text())
        chk("mode file names the active mode", cfg["active"] == "swebench")
        chk("mode auto-approves for unattended runs",
            cfg["modes"]["swebench"]["auto_approve"] == "all")
        chk("pinned terminal preference is written",
            (home / "sessions" / "swebench-selftest_preferences.json").is_file())
        chk("agent cannot spawn agents or reach the network",
            "agent.spawn" in MODE["denied_tools"] and "web.*" in MODE["denied_tools"])
        chk("prompt forbids touching tests and history",
            "Do NOT create, edit or delete any test file" in PROMPT
            and "git commit" in PROMPT)

        # A resume that changes the model must be refused, not silently mixed.
        class A:
            cli = sys.executable
            run_name = "r1"; model = "m1"; provider = "p"; timeout = 60
            keep_test_edits = False
        out_dir = tmp / "run"; out_dir.mkdir()
        write_manifest(out_dir, A(), None, 1)
        first = (out_dir / "manifest.json").read_text()
        A.model = "m2"
        try:
            write_manifest(out_dir, A(), None, 1)
            chk("resume with a different model is refused", False)
        except SystemExit as exc:
            chk("resume with a different model is refused", "model" in str(exc))
        chk("original manifest survives the refusal",
            (out_dir / "manifest.json").read_text() == first)
        A.model = "m1"
        write_manifest(out_dir, A(), None, 1)
        chk("matching resume keeps the first manifest",
            (out_dir / "manifest.json").read_text() == first)

    print("\nself-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate SWE-bench predictions with laintas-cli")
    p.add_argument("--dataset", help="SWE-bench split as .jsonl or .json")
    p.add_argument("--out", default="./swebench-run", help="run directory")
    p.add_argument("--run-name", default="laintas-cli", help="model_name_or_path in predictions")
    p.add_argument("--model", default="", help="model id (recorded; set it in LAINTAS_HOME config)")
    p.add_argument("--provider", default="", help="provider id (recorded)")
    p.add_argument("--cli", default=str(Path(__file__).resolve().parents[2] / "laintas_cli.py"))
    p.add_argument("--home", default=str(Path.home() / ".laintas"), help="LAINTAS_HOME for the run")
    p.add_argument("--cache", default="./swebench-repos", help="repo clone cache")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--instances", default="", help="comma-separated instance ids")
    p.add_argument("--timeout", type=int, default=1800, help="seconds per instance")
    p.add_argument("--keep-workspaces", action="store_true")
    p.add_argument("--keep-test-edits", action="store_true",
                   help="do not strip the agent's edits to test files")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.self_test:
        return self_test(args)
    if not args.dataset:
        p.error("--dataset is required (or use --self-test)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
