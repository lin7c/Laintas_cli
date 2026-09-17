# Automatic task routing and the Station management UI

Status: core routing, the shared Station service and the management UI are implemented; runtime configuration is unchanged.

Implementation boundaries: the request-idempotency record is process-local (at most 4,096 entries; when full, new admissions are refused rather than evicting old records and risking duplicate execution). Automatic routing conservatively chooses an existing read-only specialist role or an isolated generic sub-agent; persistent employees are dispatched only by explicit selection. `auto_pilot_budget_tokens` is wired to the provider's actual reported usage plus ancestor-run accumulation; it is a stop condition once the threshold is reached, not a hard in-flight quota reservation, and missing provider usage is never faked. Dependency graphs and cross-restart recovery stay with the existing WorkGraph/HWG; Station adds no persistent task store and no automatic replay. The design principles and acceptance criteria for later extensions are kept below; no automatic model switching or global learned ranking was added.

## Goals and boundaries

Automatic routing picks an executor for a task whose boundaries are already clear. Decomposing the task, choosing an executor, claiming a concurrency slot, running, and accepting the result are distinct stages. Do not add a second agent registry, a second queue, or a second task state.

`/station` with no arguments opens the unified management UI; existing parameterized commands stay compatible.
The UI, the commands, and the automatic router call the same service entry point. Closing the UI does not stop tasks.
A delegation `branch` in this design is a supervision unit for a run, not a `/fork` conversation branch.

## Source basis and reuse points

| Current implementation | Existing capability | Integration approach |
|---|---|---|
| `auto_pilot.py` | Classification, decomposition, auto-run switch, thread-local pending plan | Keep the entry point; move gradually to structured tasks; keywords as hints only |
| `agent_loop.py:EmployeeProfile` | Specialist roles, capability tags, tool policy | Match employee capabilities; do not build a second personnel registry |
| `start_agent_assignment` | Employee assignment lock, new-task status, temporary run terminal | Adapter for employee execution; add unified acceptance and supervision hooks |
| `spawn_subagent` | Parent/child relation, depth limits, worktree, result return | Adapter for temporary sub-agent execution |
| `schedule_agent` | Shared concurrency cap, FIFO, cancel-from-queue callbacks | The single execution-slot queue |
| `branch.py` | Run ownership, timeout, stall detection, result convergence | The single delegation supervision mechanism; confirm employee assignments are isolated by assignment identity |
| `agent_contract.py` | Output contracts, evidence validation, file scope | Pre-route validation and post-completion acceptance |
| `workgraph.py` / HWG | Persistent work and dependency execution | Reuse when dependencies or restart recovery are needed; do not write a second DAG executor |
| `station_agent` / `swap_station` | Atomic stationing, terminal exclusivity | The single underlying implementation for binding and switching |
| `agent_ui_events.py` | Bounded run-event stream | The notification source for UI activity and routing explanations |
| `resource_ui.py:ResourceBrowser` | Two-pane, search, in-place actions, periodic refresh | The UI shell for Station |
| `agents_mode.py` | Agent conversations, run state, output display | Extract shared display helpers; keep the conversation entry point |

Gaps found before implementation (kept as review evidence):

- `_cmd_station` mixes argument parsing, terminal startup, binding, task submission and output, and duplicates the task-dispatch logic.
- Auto-Pilot receives string subtasks with no explicit dependencies, tool requirements, file scope, or acceptance constraints.
- The automatic path uses `subtasks[:max_parallel]`; the concurrency limit acts as truncation, and the overflow is not saved for later scheduling.
- The automatic path creates a budget-tracking object but never wires it to later state or token updates, so a hard budget limit cannot be claimed.
- The decomposition callback temporarily mutates the global `max_tokens`, risking concurrent interference; it should be a per-request parameter.
- `get_pool_agents` does not actually filter for idle; routing cannot rely on names or comments to decide dispatchability.
- Employee assignments and temporary sub-agents have different lifecycles; completion cannot be unified on `status == done` alone.

## Three relationships that must stay separate

1. **Delegation**: `parent_id → child_ids`. Determines task responsibility and messaging rights.
   Keep the existing direct parent/child-only communication constraint; shared terminals or shared tags must not widen it.
2. **Stationing**: agent → persistent terminal. One agent holds at most one stationed terminal; one terminal holds at most one stationed agent.
   `home_terminal` marks ownership; it is not deployment and grants no PTY read/write access.
3. **Execution**: one assignment / child run → executor, resources, contract, result.
   A temporary PTY belongs to that execution and never joins the permanent terminal roster.

A persistent employee reuses its profile with a fresh context per assignment; a temporary sub-agent's identity converges when its task ends.
A persistent agent must not silently rewrite its `parent_id` just because another supervisor picked it.

## Automatic routing flow

`task request → boundary validation → candidate filter → ranking → atomic admission → shared scheduling → acceptance → report to parent`

A new `agent_router.py` holds pure decision logic only: it creates no threads, terminals or worktrees and mutates no registry.
Inputs are immutable snapshots; the output is a `RouteDecision`, fully reproducible in unit tests.

A task request carries at least:

- `request_id`, `session_id`, `run_id`, `owner_agent_id`; prevents picking up the wrong task after a session switch.
- Task text, `cwd`, specialist role, required tools, read/write scope.
- Dependency references, output contract, the parent's remaining budget, timeout, and whether creating sub-agents is allowed.
- An explicitly named executor if any; an explicit user choice outranks automatic matching.

These are routing inputs, not a new persistent task store; persistent requests live in the existing work graph.

Hard candidate filtering precedes ranking:

- Within the current session/run authorization and the existing delegation tree; the main agent and other parents' employees are not borrowed automatically.
- Not terminated; no active assignment; not queued/running/waiting.
- Both the specialist capability and the actual tool permissions match. Tags express intent; tool permissions decide executability.
- The named terminal is still alive with valid ownership; the required context, cwd and execution isolation can be provided.
- Depth, budget and resource conditions fit the limits; the parent task is not cancelled.

Survivors are ranked by an explainable stable order: explicit match, specialist role, capability coverage, context fit, stable ID.
Uncalibratable "model self-reported confidence" is never used to grant permissions.
The first version adds no success-rate learning, vector services, or automatic model switching.

A decision can only be: reuse a qualified idle employee, spawn a temporary sub-agent, wait, let the parent execute, or refuse.
Refusal for missing tool permissions is distinct from a mere ranking miss: unmet permissions must not degrade into silently widened permissions.
With no qualified employee, the existing `spawn_subagent` is used only if the parent explicitly allows creation and resources permit.

A match does not guarantee admission. The candidate may be taken by another task after the decision; the service must re-verify under the existing lock.
Losing the race returns a structured reason with a bounded number of re-selections; with no viable candidate it returns wait/parent-executes.
Calling models, starting PTYs, or waiting on threads inside the lock is forbidden.

## The shared service and the run loop

A new `station_service.py` provides:

- `snapshot(scope)`: a consistent, immutable agent/terminal/assignment view gathered under a short lock.
- `assign(request)`: shared validation, idempotent admission and scheduling for both manual assignment and automatic matching.
- `deploy(agent, terminal)` / `undeploy(agent)`: wrap existing stationing logic with resource-creation rollback.
- `cancel(run_ref)`: cancables a specific run; a reusable agent ID alone must not cancel its later, different tasks.

The service receives a runtime adapter explicitly; no reverse imports of `laintas_cli` and no dynamic local imports to dodge circular dependencies.
Keep the two execution adapters (employee assignment, temporary sub-agent) with shared admission and completion reporting; do not force their lifecycles together.

These invariants must hold:

- `(session_id, run_id, request_id)` idempotency: retrying one request yields exactly one dispatch result.
  The same IDs with different content is a conflict; legitimate duplicate requests must not be deduplicated by task text.
- The concurrency cap limits only simultaneous execution. Queued tasks are all kept and enter the existing scheduler; unmet dependencies do not steal slots.
- One assignment has exactly one executor; one agent holds at most one assignment at a time; a slot is released exactly once.
- Unified reporting distinguishes "returned a result" from "passed acceptance"; keep the existing contract/stage and branch outcomes.
  A clean exit without a contract is marked "contract not verified", never implied correct.
- A parent cancel first blocks new admissions, then cancels queued items, then stops running ones, then waits for resources to be released; cancellation is idempotent.
- Worktrees, PTYs and threads each have one clear owner; clean up only what this run created, and roll back on mid-flight failure.
- A private PTY is not filesystem isolation. The employee path cannot assume it owns the sub-agent worktree mechanism;
  parallel writing tasks must use the existing worktree isolation or file-scope mutual exclusion, or run serially when non-Git and not provably safe.
- Budget accounting comes from actual runs with atomic reservation and settlement; until that loop is closed, do not claim hard token caps.
  The already-available concurrency, round and deadline limits can be enforced first.
- After a process restart, old runs are marked interrupted; resources and artifacts are checked before any replay — tasks with side effects are never replayed automatically.
- A session fork may inherit plan text but not live assignments, PTYs, threads or scheduler-slot ownership.

The event stream is for notifications; the run snapshot is the source of truth for queries. If the UI drops events it refetches the snapshot instead of rebuilding state from events.
The closed loop reuses the current assignment/stage/branch; fields are added only for missing transitions — no parallel state machine.

## UI structure

The management UI is a read-mostly console over the snapshot: agents, terminals and runs in the familiar browser shell.
The layout keeps `ResourceBrowser`'s two panes: left for the roster, right for detail. Detail includes run status, routing explanation, resource ownership and recent events.
In-place actions cover deploy/undeploy, cancel run, view events; confirmations required for irreversible ones (cancel, undeploy).
Output from the unified dispatch path reuses the shared display helpers so interactive and dispatched runs render identically.
