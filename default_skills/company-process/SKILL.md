---
name: company-process
description: Run Helpwo-authored company pipelines on the CLI on a schedule — each stage runs as an employee owned by its role terminal, with conditional loop-backs, so the whole company runs autonomously even when the browser is closed.
version: 1.0.0
triggers: []
---

# Company process runtime (CLI side)

A company PROCESS is a Helpwo-authored pipeline: an ordered set of stages, each
run by an employee, with conditional edges (loop-backs) and a schedule. This
skill runs those pipelines on the CLI using the CLI's own systems. The runtime
creates or reuses each role's terminal before deploying its employee there, so
the work is visible and controllable through the normal agent/terminal channels.
The role terminal owns the employee's lifecycle, and the scheduler fires work on
the machine's LOCAL clock.

Tools:
- `company.deploy({ process })` — register a process bundle, prepare each role
  terminal, and deploy its employees. `process` =
  `{ name, description?, schedule?, stages, roles }`, where
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

Ownership: each role employee has at most one deployment terminal, and each
role terminal hosts exactly one deployed employee. Terminating a role terminal
also ends its deployed employee and descendant terminals, so process teardown
should clean up the terminal tree instead of separately orphaning agents.
