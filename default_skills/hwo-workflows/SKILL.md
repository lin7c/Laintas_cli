---
name: hwo-workflows
description: Author, validate, or debug HWO multi-agent workflows and HWG graphs.
version: 1.0.0
---

# HWO and HWG Workflows

Load this skill before creating or modifying `.hwo` or `.hwg` files. For an
existing file, prefer `hwo({action: "compile", path: ...})` before execution.

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

Keep names unique among siblings, use relative prompt/workflow paths, and make
all input/output bindings explicit so compile-time validation can catch scope
errors before execution.
