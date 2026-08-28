---
name: hwo-workflows
description: Author, validate, or debug HWO multi-agent workflows and HWG graphs.
version: 1.0.0
triggers:
  - hwo
  - hwg
  - workflow
  - pipeline
  - staged
  - stages
  - resumable
  - conditional workflow
  - loop until
  - multi agent workflow
  - orchestrate
---

# HWO and HWG Workflows

Load this skill before creating or modifying `.hwo` or `.hwg` files. For an
existing file, prefer `hwo({action: "compile", path: ...})` before execution.

## When to use HWO (and when not to)

HWO is a **durable, reusable** workflow definition with structured input/output
contracts. Use HWO only when:
- The orchestration will be **run more than once** with different inputs.
- Specialist agents need **declared file I/O contracts** (`in()` / `out()`).
- The workflow should be version-controlled and compiled before execution.

Do NOT use HWO for one-off parallel work like code review, batch analysis, or
multi-file edits. Use `spawn_parallel` or `spawn_chain` instead - they are
throwaway delegations that don't require a `.hwo` file. If the orchestration
proves reusable, promote to HWO later.

## HWO essentials

- `@line [in(topic: string), out(report: file)]` declares the file contract.
- `#name# [in(x = $input.x), out(y: string)] { ... }` declares an agent.
- Use `->` for each ordered step in a non-trivial agent body.
- `(prompt.md)#name#` or `#name#(prompt.md)` adds a role prompt overlay; it does
  not replace platform, product, tool, lifecycle, or completion rules.
- `#name@model-id#` pins a model.
- `// ... //` is a parallel-agent block, not a comment. Use fenced backticks
  for comments.
- `$input.x` is a workflow input; `$self.x` is the current agent's bound value;
  `#agent.output` is available only after that earlier agent completes.
- Bind inputs explicitly. `in(prompt)` declares a name but does not read it;
  body text must use `$input.prompt` or `$self.prompt`.
- Declare every downstream value in `out(...)` and submit matching keys with
  `agent_return({"key": value})` as the final ordered step.
- `agent_return` records outputs and does not terminate the agent. Complete any
  remaining ordered steps, then use the normal task-completion protocol.
- Parallel siblings cannot read each other's outputs while running. Later
  parent-scope agents may read declared outputs after the block completes.
- Use `agent_send` and `agent_receive` only for runtime coordination; mailbox
  messages are not structured outputs.
- `$input.x` / `$self.x` / `#agent.output` are rendered into the body as
  `type:value` (e.g. `string:3`), not as the bare value. Describe what to do
  with the value; never paste one straight into a shell command, or the command
  runs on the literal text `string:3`.

## HWG essentials

- `@graph [in(topic: string), out(report = #publish.report)]` declares the graph.
- `(file.hwo)#node# [in(x = $input.x), out(y: string)]` binds an HWO node.
- `#a# -> #b#` is a plain edge.
- Put edge policy between arrow and target:
  `#review# -> { on: verdict == PASS } #publish#`.
- Every branch with multiple outgoing edges needs an `on:` condition per edge.
- Every cycle needs a bounded `maxLoops` edge and a separate exit path.
- A graph has exactly one start node and at least one end node.
- `!(file.hwo)#manual#` pauses for explicit user input; resume the same run with
  `/hwg resume <runId> [PASS|FAIL|verdict] [outputs-json]`.
- HWG sees only HWO file-level declared outputs, never internal agent-local
  values.

## Runtime enforcement (HWG)

The runtime holds a node to what it declared. Three rules fire at run time, not
compile time:

- **Declared outputs must be returned.** A node with `out(report: string)` that
  finishes without `agent_return({"report": ...})` fails the run — it is not a
  PASS. Same for `out(verdict: ...)`: the verdict must come from
  `agent_return`, a bare `[RETURN #node#]: PASS` line, or a `#RESULT: PASS#`
  marker. Silence is a protocol error.
- **Contract failures are retried, then stop the graph.** They do not travel
  down an `on: FAIL` edge — a node that returned nothing has judged nothing, so
  routing it as a domain verdict would be a lie. Fix the node body instead.
- **Required graph inputs must be supplied.** Anything in `@graph in(...)`
  without a default must have a value at launch, or the run refuses to start
  rather than resolving it to null inside every node.

Nodes that declare no `out(...)` are unaffected: their verdict is still PASS on
success, FAIL on failure.

- **An unhandled node failure fails the run.** If a node fails and the edge the
  graph took carries no `on:` condition, the author never said what a failure
  there means — so the run ends failed and names the node, even if every later
  node succeeded. Routing it explicitly (`on: verdict == "FAIL"`, or any
  condition) counts as handling it and the run can still succeed.

## Branching on facts, not on claims

An edge condition is an atom — `PASS`, `verdict == "PASS"`, `score >= 3`,
`status in [OK, WARN]`, `exists(path)` — and atoms compose with `and` / `or` /
`not` (`&&` / `||` / `!`), grouped with parentheses:

```
#build# -> { on: verdict == "PASS" and exists(dist/app.js) } #ship#
#build# -> { on: verdict != "PASS" or not exists(dist/app.js) } #fix#
```

`exists(path)` is evaluated by the runtime, so it is the one branch condition a
node cannot get wrong. Use it wherever the next step depends on an artifact
actually being on disk: a node reporting PASS is a claim, the file is evidence.
Paths may embed `$input.x` and `#node.field#`; if a reference resolves to
nothing the whole predicate is false, so a half-built path is never tested.

A condition that reaches for this grammar and gets it wrong (`verdict == "PASS"
and`) is a compile error, not a silently-mismatched edge. Conditions outside the
grammar keep their old meaning: the whole string is compared to the verdict.

## Loop-aware nodes

`$loop.count` binds the number of times the graph has entered this node on the
current path — 1 on the first run, 2 on the first retry. Bind it like any other
input so a node can escalate instead of repeating itself:

```
(fix.hwo)#fix# [in(attempt: int = $loop.count), out(verdict: string)]
```

## Sharing contracts between graphs

`@include "lib/contracts.hwg"` splices that file's declarations in place. Put
the node declarations several graphs agree on in one file and let each graph
supply only its own edges:

```
@include "lib/contracts.hwg"     ``` declares (review.hwo)#review# [out(verdict: string)] ```
(write.hwo)#write# [out(report: file)]
#write# -> #review#
```

The path is relative to the file holding the `@include`. A file reached twice
(two libraries including the same contracts) is spliced once, so the diamond
case is not a duplicate-id error; a file that includes itself is refused with
the cycle named. An included file only has to parse — it does not need to be a
runnable graph on its own, so a bare list of node declarations is fine.

## Fan-out and join

`->` picks one next node; `=>` takes every branch:

```
#plan# => #lint#
#plan# => #test#
#plan# => #types#
#lint#  -> #merge#
#test#  -> #merge#
#types# -> #merge#
(merge.hwo)#merge# [in(f = #lint.findings#, x = #test.failures#)] { join: "all" }
```

Rules the compiler enforces: a node uses `->` or `=>` for all its outgoing
edges, never both; a fan-out needs at least two branches; `=>` edges carry no
`maxLoops`; and every branch must reach the same node declaring
`{ join: "all" }`. Without that last rule a join could wait forever for a branch
that can never arrive — so the graph is rejected instead of hanging.

The join node runs **once**, after the last branch reaches it, and reads each
branch's declared outputs through `#branch.field#`. If a branch walks off
somewhere else and never arrives, the run fails and names the waiting join
rather than quietly merging a partial set.

Ready fan-out branches run **concurrently** (up to six at a time). Their
results are committed in source/queue order rather than completion order, so
history, events, and the join's inputs stay deterministic even when a later
branch finishes first. A successor is not eligible until the frontier that
produced its inputs has committed, and the join still runs exactly once after
every branch arrives.

## Tool scope

`{ tools: [...] }` limits every agent in that node's `.hwo` file to those tools:

```
(audit.hwo)#audit# [out(verdict: string, findings: string)] { tools: [fs.read, fs.ls, fs.grep, fs.glob] }
```

That audit node is now structurally unable to write — not asked not to, unable.
Use it whenever a node's job is to look rather than to change; it is the
difference between a prompt that says "don't modify anything" and a node that
cannot.

Rules worth knowing:

- **Narrowing only.** The scope is intersected with the mode, role,
  workflow-phase and security-policy restrictions already in force. It can take
  access away, never grant it.
- **Inherited by spawned agents.** A scoped node cannot delegate its way out —
  any sub-agent it starts carries the same scope.
- **The completion protocol always survives.** `task.complete` and
  `agent_return` are never removed: a node that cannot report its result is
  hung, not contained.
- Entries are exact names or globs (`fs.*`). `tools: []` is a compile error —
  omit the key to inherit the full set.
- **laintas_cli only.** Helpwo has no way to hold an agent to a scope, so it
  refuses to *run* a graph whose nodes declare one rather than running it
  unrestricted. Compiling and visualising still work.

## Node cache

`cache: "1h"` keys on the node file, its inputs *and* a fingerprint of the
workspace, because a node's result depends on the files it read. When the
workspace cannot be fingerprinted (laintas_cli: not a git repo) the cache is
skipped rather than trusted — a missing speed-up is cheaper than a stale answer.

Keep names unique among siblings, use relative prompt/workflow paths, and make
all input/output bindings explicit so compile-time validation can catch scope
errors before execution.
