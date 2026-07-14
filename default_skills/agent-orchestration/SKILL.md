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
  caller's terminal ownership and ends when that terminal subtree ends.
- `agent.hire` creates a persistent employee and deploys it directly to the
  caller's current terminal. Hiring defines identity and capabilities; it does
  not start work by itself.
- Give hired employees work through an explicit assignment or agent message.
  Use `agent.station` only to move an idle employee to another live terminal;
  it is not a required second step after hiring.
- A terminal may host multiple employees, but each employee has exactly one
  deployment terminal and ends when that terminal ends.
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
