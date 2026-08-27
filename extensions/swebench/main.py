"""Extension: swebench

Drives the SWE-bench adapter in ``runner.py`` from inside the CLI.

    /swebench self-test              check the plumbing (no dataset, no network)
    /swebench run --dataset D --out R [--limit N] [--timeout S]
    /swebench status [run-dir]       progress and outcome mix so far
    /swebench score [run-dir]        print the official evaluation command

The split matters: this extension produces `predictions.jsonl` and never scores
anything. Grading is the official `swebench.harness.run_evaluation` in its own
containers -- a benchmark whose author also writes the scorer is not evidence,
and the whole point of a published number is that someone else can check it.

A run takes hours, so `/swebench run` starts a detached process and returns.
Killing the CLI does not kill the run, and the run resumes from its own
`predictions.jsonl` if it is restarted.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path

_ctx = None
_HERE = Path(__file__).resolve().parent
_RUNNER = _HERE / "runner.py"


def _say(text: str) -> None:
    if _ctx is not None and getattr(_ctx, "console", None) is not None:
        _ctx.console.print(text)
    else:
        print(text)


def _default_run_dir() -> Path:
    return Path.cwd() / "swebench-run"


def _resolve_run_dir(argv: list) -> Path:
    return Path(argv[0]).expanduser().resolve() if argv else _default_run_dir()


def _cmd_self_test() -> None:
    proc = subprocess.run([sys.executable, str(_RUNNER), "--self-test"],
                          capture_output=True, text=True, timeout=300)
    _say((proc.stdout or "").rstrip() or "(no output)")
    if proc.returncode != 0:
        _say(f"[red]self-test failed ({proc.returncode})[/red]")
        if proc.stderr:
            _say(proc.stderr.strip()[:1500])


def _cmd_run(argv: list) -> None:
    """Start a detached run; a 500-instance split outlives any REPL session."""
    if "--dataset" not in argv:
        _say("Usage: /swebench run --dataset <split.jsonl> --out <run-dir> [--limit N]")
        return
    out = Path(argv[argv.index("--out") + 1]).expanduser().resolve() \
        if "--out" in argv and argv.index("--out") + 1 < len(argv) else _default_run_dir()
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(_RUNNER), *argv]
    if "--out" not in argv:
        cmd += ["--out", str(out)]
    console_log = out / "runner.out"
    with open(console_log, "ab") as sink:
        proc = subprocess.Popen(cmd, cwd=str(Path.cwd()), stdout=sink,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)
    (out / "runner.pid").write_text(str(proc.pid), encoding="utf-8")
    _say(f"started (pid {proc.pid}) -> {out}")
    _say(f"  progress: /swebench status {out}")
    _say(f"  console:  tail -f {console_log}")


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue        # a run killed mid-write leaves one partial line
    return rows


def _cmd_status(argv: list) -> None:
    out = _resolve_run_dir(argv)
    if not out.is_dir():
        _say(f"no run directory at {out}")
        return
    preds = _read_jsonl(out / "predictions.jsonl")
    meta = _read_jsonl(out / "run_log.jsonl")
    manifest = {}
    if (out / "manifest.json").is_file():
        try:
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}

    total = manifest.get("instances")
    pid_file = out / "runner.pid"
    alive = False
    if pid_file.is_file():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            alive = True
        except (OSError, ValueError):
            alive = False

    _say(f"run: {out}")
    if manifest:
        _say(f"  model {manifest.get('model') or '(unset)'} | "
             f"cli {str(manifest.get('laintas_cli_commit'))[:9]}"
             f"{' +dirty' if manifest.get('laintas_cli_dirty') else ''} | "
             f"timeout {manifest.get('timeout_s')}s")
    _say(f"  {len(preds)}{'/' + str(total) if total else ''} predictions"
         f"  |  {'running' if alive else 'not running'}")

    # An empty patch is the failure that matters most: the agent finished
    # cleanly and changed nothing, which scores zero but looks like a success
    # in any count that only tracks exit codes.
    empty = sum(1 for p in preds if not (p.get("model_patch") or "").strip())
    if preds:
        _say(f"  empty patches: {empty}/{len(preds)}")
    if meta:
        for status, n in Counter(m.get("status", "?") for m in meta).most_common():
            _say(f"    {n:>4}  {status}")
        secs = [m.get("seconds") or 0 for m in meta]
        _say(f"  median {sorted(secs)[len(secs) // 2]:.0f}s per instance")
        dropped = sum(len(m.get("dropped_test_edits") or []) for m in meta)
        if dropped:
            _say(f"  stripped {dropped} test-file edit(s) from patches")


def _cmd_score(argv: list) -> None:
    """Print the official command. Never run it: scoring is not ours to do."""
    out = _resolve_run_dir(argv)
    preds = out / "predictions.jsonl"
    if not preds.is_file():
        _say(f"no predictions at {preds}")
        return
    _say("Score with the official harness (needs Docker and a large disk):")
    _say(f"  python -m swebench.harness.run_evaluation \\")
    _say(f"      --predictions_path {shlex.quote(str(preds))} \\")
    _say(f"      --run_id {out.name} --max_workers 8")
    _say("Publish alongside it: manifest.json, run_log.jsonl and logs/ .")


def handle(parts, raw_line: str = "") -> None:
    """Dispatch /swebench [<sub> ...]."""
    argv = [str(p).strip() for p in parts[1:] if str(p).strip()]
    action = argv[0].lower() if argv else "status"
    rest = argv[1:]
    if action in ("self-test", "selftest"):
        _cmd_self_test()
    elif action == "run":
        _cmd_run(rest)
    elif action == "status":
        _cmd_status(rest)
    elif action == "score":
        _cmd_score(rest)
    else:
        _say("Usage: /swebench self-test | run --dataset D --out R | status [dir] | score [dir]")


def setup(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_command(
        "swebench", handle,
        description="Generate SWE-bench predictions with this CLI (scoring stays with the official harness)",
        subcommands=[
            ("self-test", "Check the adapter plumbing without a dataset or network"),
            ("run --dataset D --out R", "Start a detached prediction run"),
            ("status [dir]", "Progress, outcome mix and empty-patch count"),
            ("score [dir]", "Print the official evaluation command"),
        ])
