---
name: company-process
description: Run Helpwo-authored company pipelines on the CLI on a schedule — each stage runs as a hired employee stationed on a sub-terminal, with conditional loop-backs, so the whole company runs autonomously even when the browser is closed.
version: 1.0.0
triggers: []
---

# Company process runtime (CLI side)

A company PROCESS is a Helpwo-authored pipeline: an ordered set of stages, each
run by an employee, with conditional edges (loop-backs) and a schedule. This
skill runs those pipelines on the CLI using the CLI's own systems — every stage
is a hired employee (`/hire`) stationed on a sub-terminal (`/station`), so the
work is visible and controllable through the normal agent/terminal channels; the
scheduler fires them on the machine's LOCAL clock.

Tools:
- `company.deploy({ process })` — register a process bundle and pre-hire its
  employees. `process` = `{ name, description?, schedule?, stages, roles }`, where
  each role = `{ name, persona }` and each stage = `{ id, role, task, manual?,
  next:[{ to?, on?, maxLoops? }] }`. `runNow:true` also runs one cycle now. This
  is what Helpwo's "deploy this process" sends over.
- `company.run({ name })` — run one cycle of a registered process immediately.
- `company.list()` — list registered processes.
- `company.status({ name })` — the last run's status + workspace.

Behaviour: stages run in order; an edge's `on` (e.g. PASS/FAIL) picks the branch
from the stage's final `VERDICT:` line; `maxLoops` bounds a loop-back; a `manual`
stage pauses the run (a human must act); each run gets its own workspace under
`company_runs/<process>/<timestamp>/`; the scheduler skips a missed window (no
catch-up) and hard-stops at the `deadline`.
