# swebench

Generates SWE-bench predictions with laintas-cli. **It does not score them.**

Scoring belongs to the official `swebench.harness.run_evaluation`, running in
its own containers. A benchmark whose author also writes the scorer is not
evidence anybody outside the project should accept, so this adapter stops at
the one artifact the official harness consumes.

## Commands

    /swebench self-test                 plumbing check (no dataset, no network)
    /swebench run --dataset D --out R   detached prediction run
    /swebench status [run-dir]          progress, outcome mix, empty-patch count
    /swebench score [run-dir]           print the official evaluation command

`runner.py` is also a standalone entry point, which is how CI and an external
harness should call it:

    python3 runner.py --dataset swe_bench_verified.jsonl --out ./run-01 \
        --run-name laintas-cli-v1.20.0 --model deepseek-v4-flash \
        --home /path/to/bench-LAINTAS_HOME --timeout 1800

## What one instance does

1. Clone the instance's repo at `base_commit` (clones are cached per repo).
2. Install a project mode + a **pinned terminal id**. Both are required: mode
   selection is read from a per-terminal preferences file, and a subprocess
   with no tty derives a different terminal id every run, so the mode silently
   never activates and the first write is denied for want of an approval.
3. Run `laintas_cli --execute` with the problem statement only.
4. Take `git diff` as `model_patch`.

The instance's `test_patch` never enters the workspace. The grader applies it
afterwards; an agent that could read it would be grading itself.

## Patch hygiene

Edits to test files are stripped by default (`--keep-test-edits` disables it)
and the removed paths are recorded in `run_log.jsonl`. The grader force-applies
its own tests after ours, so a model patch that also edits tests is at best
ignored and at worst conflicts.

## Reproducibility

`manifest.json` is written once and pins the run: adapter version, run name,
model, provider, timeout, dataset sha256, laintas-cli commit and dirty flag,
the mode, and the exact prompt template. A resume that changes any pinned
setting is **refused** — resuming with a different model would mix two models'
work into one submission while the manifest recorded only one.

## Publishing a result

Publish the whole run directory, not just the score:

    manifest.json        what was run, and against which commit
    predictions.jsonl    the submission
    run_log.jsonl        per-instance status, duration, stripped test edits
    logs/<id>.log        the agent transcript for every instance

A number without these is a claim, not a result. Note also that the run must
not be preceded by tuning against the same split.
