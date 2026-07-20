---
name: agent-orchestration
description: Guidance for spawning or coordinating sub-agents and terminals.
version: 1.0.0
---

# Agent Orchestration

Default to doing the work yourself. Delegate only when it reduces real wall-clock time or isolates risk.

- Use `agent.spawn` for independent read-only exploration, separate investigation tracks, or review tasks.
- Do not point multiple agents at the same file for writes.
- Give spawned agents self-contained goals: target paths, expected output, and constraints.
- Use `agent.wait` before relying on child results.
- Use terminals for stateful interactive processes; use agents for reasoning and task execution.
- Keep depth shallow. At depth 2 or more, prefer finishing locally unless delegation is clearly valuable.

## Temporary agents and hired employees

- `agent.spawn` creates a disposable child for one bounded task. It inherits the
  caller's terminal scope for communication, never the caller's deployment
  lease or shell stream.
- `agent.hire` creates a persistent, initially undeployed employee. Hiring
  defines identity, capabilities, and a base model; it does not start work.
- Give hired employees work through an explicit assignment or agent message.
  Without deployment, assignments use a private temporary terminal. Use
  `agent.station` to claim a different live, unoccupied terminal explicitly.
- A terminal may host exactly one deployed employee, and each employee has at
  most one deployment terminal.
- Direct messages are limited to same-terminal peers, direct parent/child
  terminal neighbors, and direct agent parent/child relationships.
- Use spawned agents for one-off delegation and hired employees for reusable,
  terminal-scoped roles.

## Parallel and chained spawns

For batches of related work, prefer the structured spawn tools over hand-managed
single `agent.spawn` calls:

- `spawn_parallel({ tasks: [{goal, hint?}, …] })` — run up to 6 agents at once
  and wait for ALL. Each member MUST own DIFFERENT files — decompose strictly by
  file boundaries so two agents never write the same file. Returns a combined
  report. Use for independent work (e.g. edit three unrelated modules).
- `spawn_chain({ steps: [{goal, hint?}, …] })` — a sequential pipeline (2–6
  steps) with automatic handoff documents between steps. Use for DEPENDENT work:
  analyze → implement → verify. If a step fails, the chain aborts.
- `await_spawns({ agent_ids? })` — collect results from agents you spawned with
  bare `agent.spawn`; omit `agent_ids` to wait for all children.

Pick the smallest that fits: one agent for a single errand, `spawn_parallel` for
independent fan-out, `spawn_chain` for an ordered pipeline.

## HWO vs spawn_parallel / spawn_chain

`spawn_parallel` and `spawn_chain` are **throwaway** orchestration - the
delegation exists only for the current run. HWO (`.hwo` file) is a **durable,
reusable** workflow definition with structured input/output contracts.

Use `spawn_parallel` / `spawn_chain` when:
- The work is one-off (code review, batch analysis, multi-file edit).
- You do not need to re-run the same orchestration later.
- Handoff between stages is simple (or absent for parallel).

Use HWO only when:
- The orchestration is **reusable** - you or someone else will run the same
  workflow again with different inputs.
- Specialist agents need **structured I/O contracts** (declared `in()` / `out()`
  file bindings, not just freeform text).
- The workflow should be version-controlled and compiled before execution.

If unsure, use `spawn_parallel` / `spawn_chain`. You can always promote to HWO
later if the orchestration proves reusable.
