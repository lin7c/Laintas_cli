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
- `terminal.send({ name, command })` — send a command/keystrokes to a running
  terminal (e.g. answer a prompt, drive a REPL).
- `terminal.watch({ name, pattern })` — set/replace the trigger on an existing
  terminal; empty `pattern` clears it.
- `terminal.list()` — list terminals and their status.
- `terminal.terminate({ name })` — stop and destroy a terminal subtree. This
  recursively ends its child terminals, deployed agents, and owned temporary
  agents.
- `session.close` — close the caller's temporary interactive session; it is not
  a persistent-terminal cleanup operation.

Ownership:
- `term0` is the root terminal. Every other terminal requires one live parent
  and cannot outlive it.
- A terminal can host multiple deployed agents, but an agent can be deployed to
  only one terminal.
- `agent.hire` deploys the new employee directly to the current terminal.
- Ending a terminal ends all agents deployed to it. Move an idle employee with
  `agent.station` before terminating its old terminal if it must survive there.

Discipline:
- Name terminals for their job (`dev`, `build`, `api`) so later turns can find them.
- Prefer a `trigger` over busy-waiting: launch with `terminal.exec`, then continue
  other work; act when the inbox event arrives.
- Use `terminal.terminate` for persistent terminals and `session.close` for
  temporary sessions once their work is done.
- Keep one terminal per logical process; don't multiplex unrelated commands.
