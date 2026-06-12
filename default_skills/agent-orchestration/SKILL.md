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
