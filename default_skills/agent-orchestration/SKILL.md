---
name: agent-orchestration
description: Guidance for spawning or coordinating sub-agents and terminals.
version: 1.0.0
triggers:
  - delegate
  - sub agent
  - subagent
  - spawn
  - in parallel
  - at the same time
  - split the work
  - independent parts
  - separate tracks
  - second opinion
  - review my work
  - explore while
  - team of agents
  - hire
  - worker
---

# Agent Orchestration

Choose the smallest arrangement that finishes reliably. Delegate when it reduces
real wall-clock time or isolates risk; keep small, sequential, tightly coupled,
or coordination-heavy work local.

- Use `agent.spawn` for independent read-only exploration, separate investigation tracks, or review tasks.
- For substantial repository analysis or review, first identify whether at least
  two disjoint file sets, subsystems, or questions can advance independently. If
  so, prefer bounded parallel agents with exclusive scopes and explicit evidence
  requirements. Do not create several broad readers for the same files.
- Do not point multiple agents at the same file for writes.
- Give spawned agents self-contained goals: target paths, expected output, and constraints.
- Use `agent.wait` before relying on child results.
- The parent retains the original requirements, verifies material child claims,
  and owns the final synthesis. With default asynchronous spawns, continue
  useful non-overlapping work instead of repeatedly polling child status.
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
  and return `batch_id` plus child IDs immediately. Each member MUST own
  DIFFERENT files — decompose strictly by file boundaries so two agents never
  write the same file. Results continue arriving through the parent inbox, so
  continue useful independent work instead of polling. Use for independent work
  (e.g. edit three unrelated modules).
- `spawn_parallel({ tasks: […], wait: true })` — compatibility barrier mode:
  wait for ALL and return the combined report. Use it only when the very next
  action truly depends on every child result.
- `spawn_chain({ steps: [{goal, hint?}, …] })` — a sequential pipeline (2–6
  steps) with automatic handoff documents between steps. Use for DEPENDENT work:
  analyze → implement → verify. If a step fails, the chain aborts.
- `await_spawns({ batch_id? | agent_ids? })` — explicit result barrier for an
  asynchronous batch or selected children; omit both to wait for all children.
  Do not call it immediately after spawning when the parent has independent
  work it can perform first.
- Before `task_complete`, consume every child result the final answer depends on
  and explicitly abort disposable children that are no longer needed. Do not
  finish the parent task while silently leaving unnecessary children running.

Pick the smallest that fits: one agent for a single errand, `spawn_parallel` for
independent fan-out, `spawn_chain` for an ordered pipeline.

## A branch is the unit of delegated work

Every fan-out and every spawn opens a BRANCH: one object holding its members,
their budget, the supervisor watching them and the outcome of each one. Two
things follow, and both are mechanisms rather than advice.

**It is supervised whether or not you wait.** A member that stops making
observable progress is nudged to wrap up, then stopped, and whatever partial
conclusion it had reached is rescued. This runs on the branch's own thread, so
an asynchronous batch is watched exactly as closely as a blocking one.

**It closes, and you cannot walk away from it.** Each member ends as
`verified`, `rejected` (with the gaps) or `aborted` (with the reason) - there is
no fourth outcome, and "still running" is not one. If you call `task_complete`
with a branch still open you are refused once and shown its state; decide then:
`await_spawns` what you need, abort what you do not, and finish.

`branch_status` shows every member's outcome or current activity without
blocking you, and the same summary appears in your context each turn. Use it to
decide whether to wait rather than waiting to find out.

## When a child gets stuck, it asks you

A child that hits a wall belonging to you - a tool it was not given, a task
that contradicts its scope, a choice with consequences outside its own task -
calls `agent_ask_parent` and BLOCKS, holding everything it has worked out so
far. The request arrives in your inbox as a `child-help` entry naming the
question, the blocker, and the capabilities it would need.

Answer it with `agent_answer(agent_id, decision, guidance)`. The child resumes
where it stopped; nothing it had learned is thrown away.

This is the cheapest moment in the child's life to intervene, and the most
expensive one to ignore: an unanswered child spends the rest of its budget
working around the wall and then reports the wall in its post-mortem, when the
only remaining options are re-spawning it or accepting less. Measured on a real
batch: a review child died after 8 tool calls on a task its tool scope could not
satisfy, and its caller - who could have widened the scope in one call - only
found out once it was gone.

When a child FAILS, the runtime does not retry it. You get its failure kind,
contract gaps, capability gaps and whatever partial result it had, and you
decide: accept the partial result, revise the task and follow up, re-spawn, or
stop. Re-spawning an agent that failed for a reason you have not removed is how
a batch burns twice the tokens for the same answer.

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
