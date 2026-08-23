---
name: sub-terminals
description: Run and watch stateful/long-running processes in named background sub-terminals.
version: 1.0.0
---

# Sub-Terminals

Use named sub-terminals for stateful or long-running processes that must persist
across agent loop iterations — dev servers, watchers, REPLs, builds, log tails.
For one-shot commands that return and exit, use plain `shell` instead.

Tools:
- `terminal.create({ name })` — start a new named persistent child terminal (a
  laintas-cli instance) owned by the caller's current terminal. Names are
  unique; reuse the name to address it later.
- `terminal.exec({ name, command, trigger? })` — run a shell command in a
  background sub-terminal. Set `trigger` (a regex) to have any matching output
  line push a `watch.trigger` event to your inbox — use this to react to
  "Compiled successfully", "Listening on", error lines, etc. without polling.
  Success from this call means the job was started, not that it exited with 0.
- `terminal.send({ name, input, mode? })` — send interactive input/keystrokes
  to a running terminal (e.g. answer a prompt or drive a REPL). `mode` is
  `line` (default, appends Enter) or `raw`. Success means the bytes were sent;
  it does not mean a shell command completed and carries no exit code. The
  legacy `command` parameter remains accepted for compatibility.
- `terminal.watch({ name, pattern })` — set/replace the trigger on an existing
  terminal; empty `pattern` clears it.
- `terminal.read({ name, cursor?, max_chars? })` — read only output added since
  this agent's previous send/read cursor. It reports `running` or `completed`
  and includes the real exit code once known. Finished `terminal.exec` jobs and
  their final output remain readable for 10 minutes or until explicitly
  terminated/replaced.
- `terminal.wait({ name, timeout?, poll_interval?, cursor?, max_chars? })` —
  wait for a background job to finish and return its output delta, completion
  state, and real exit code. `timeout` bounds SILENCE, not runtime: a job that
  keeps printing is waited on for as long as it keeps printing, and the wait
  ends only once it has produced nothing for `timeout` seconds (default 60). A
  timeout is not process completion; inspect `completed`/`timed_out` rather
  than guessing.
- `terminal.list()` — list terminals and their status.
- `terminal.terminate({ name })` — stop and destroy a terminal subtree. This
  recursively ends its child terminals, deployed agents, and owned temporary
  agents.
- `session.close` — close the caller's temporary interactive session; it is not
  a persistent-terminal cleanup operation.

Ownership:
- `term0` is the root terminal. Every other terminal requires one live parent
  and cannot outlive it.
- A terminal can host exactly one deployed agent, and an agent can be deployed
  to at most one terminal. Deployment claims are atomic.
- `agent.hire` leaves the new employee undeployed unless a different explicit
  target terminal is supplied. Undeployed assignment commands run in a private
  temporary terminal.
- Ending a terminal ends all agents deployed to it. Move an idle employee with
  `agent.station` before terminating its old terminal if it must survive there.

Discipline:
- Name terminals for their job (`dev`, `build`, `api`) so later turns can find them.
- Prefer a `trigger` over busy-waiting: launch with `terminal.exec`, then continue
  other work; act when the inbox event arrives.
- For a finite measurement/build that must finish before reporting, use
  `terminal.exec` followed by `terminal.wait`, not `sleep` plus a hopeful read.
  Use one synchronous `shell.exec` instead when no concurrent work is needed.
- Never report samples, test results, performance ranges, or exit success unless
  the corresponding tool returned the underlying output and completion state.
  If a read/wait fails, say it failed or rerun it; do not reconstruct plausible
  results from the intended command.
- Use `terminal.terminate` for persistent terminals and `session.close` for
  temporary sessions once their work is done.
- Keep one terminal per logical process; don't multiplex unrelated commands.
- Never use `terminal.send` for a one-shot probe or to infer a process exit
  code. Use `shell.exec`; use `terminal.watch`/`terminal.read` semantics for
  asynchronous progress instead of sending repeated `echo` probes.
