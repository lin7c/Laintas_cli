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
- `terminal.create({ name })` — start a new named sub-terminal (a laintas-cli
  instance). Names are unique; reuse the name to address it later.
- `terminal.exec({ name, command, trigger? })` — run a shell command in a
  background sub-terminal. Set `trigger` (a regex) to have any matching output
  line push a `watch.trigger` event to your inbox — use this to react to
  "Compiled successfully", "Listening on", error lines, etc. without polling.
- `terminal.send({ name, command })` — send a command/keystrokes to a running
  terminal (e.g. answer a prompt, drive a REPL).
- `terminal.watch({ name, pattern })` — set/replace the trigger on an existing
  terminal; empty `pattern` clears it.
- `terminal.list()` — list terminals and their status.
- `terminal.terminate({ name })` — stop and destroy one terminal.
- `session.close` — close sub-sessions when done.

Discipline:
- Name terminals for their job (`dev`, `build`, `api`) so later turns can find them.
- Prefer a `trigger` over busy-waiting: launch with `terminal.exec`, then continue
  other work; act when the inbox event arrives.
- Always `terminal.terminate` (or `session.close`) processes you started once the
  task is done — don't leave orphaned servers/watchers running.
- Keep one terminal per logical process; don't multiplex unrelated commands.
