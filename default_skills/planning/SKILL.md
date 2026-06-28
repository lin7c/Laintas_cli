---
name: planning
description: Read and maintain durable plan files for multi-phase work.
version: 1.0.0
---

# Planning

Plan files are durable, named design documents that survive context compression —
distinct from the per-turn task list (`task_create`/`task_update`). Use them for
multi-phase work where the approach itself is worth recording and revisiting.

Tools:
- `plan.list()` — list existing plans by name.
- `plan.read({ name? })` — read a plan (omit `name` for the current one).
- `plan.update({ name?, content })` — write/replace a plan's content.

When to use:
- Reach for a plan file when the work has several phases or non-obvious design
  decisions that a future session (or a reviewer) should be able to pick up.
- For ordinary multi-step execution, the task list is enough — don't create a
  plan file for routine work.

Discipline:
- Keep the plan in sync as the approach changes; a stale plan is worse than none.
- The plan states intent and structure; the task list tracks execution status.
  Use both together — plan = "what and why", tasks = "what's done / next".
