# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Authoritative References

- `PROJECT.md` — full architecture spec (components, data flow, runtime config table, file artifacts). Read this before non-trivial changes.
- `HELPWO_INTEGRATION_PLAN.md` — in-progress protocol (2026-05-19) for turning the CLI into a remote executor for HelpwoAI. The HelpwoAI side is already shipped; the CLI side is the work item. New `_poll_loop` kinds (`exec`/`query`/`delegate`/`abort`/`approval-response`) and `reqId`-tagged events are the contract.

## Run / Develop

```bash
source venv/bin/activate                 # Python venv lives in ./venv
python laintas_cli.py                    # Run from source (interactive REPL)
python laintas_cli.py --execute "task"   # Non-interactive single task
python laintas_cli.py --backend URL      # Override LAINTAS_BACKEND
```

There is no test suite, no linter config, and no Makefile — iterate by running the CLI directly. After editing files, force-reload the dev session by typing `/reload` inside the REPL (deletes `.cli.prop`, `.extra_command.py`, `.loop_command.py` and restarts).

## Package Builds

```bash
./build/linux/build_deb.sh [VERSION]     # Requires fpm (gem install fpm); outputs build/linux/laintas-cli_X.Y.Z_amd64.deb
pyinstaller build/windows/laintas_cli.spec   # Outputs Windows exe; spec lists hiddenimports for rich/prompt_toolkit
```

The .deb launcher (`/usr/bin/laintas-cli`) lazy-installs `requirements.txt` via pip on first run and `cd`s into `$LAINTAS_WORKSPACE` (default `~/laintas_workspace`). Windows builds also have an NSIS installer (`build/windows/installer.nsi`).

A companion React/Vite download site lives in `laintas_cli_download/` (separate build, not part of the Python package).

## Architecture (read PROJECT.md for full detail)

The project has grown from two core modules to ten, organized in layers:

**Core (original two):**
- **`laintas_cli.py`** (~4900 lines) — Entry point, REPL, PTY execution (`InteractiveSession`, `SubTerminalSession`), OAuth/session auth, `AgentRegistry` (remote heartbeat + polling), meta-command dispatch, prompt template generation, debug TUI.
- **`agent_loop.py`** (~2400 lines) — Library: `run_agent_loop()`, runtime config, debug ring buffer, terminal registry (named persistent sub-terminals), agent registry (tree hierarchy, spawning, inboxes, abort), context compression, memory formatting, policy integration.

**Tools & extensibility layer (Phase 3):**
- **`tools.py`** (~1400 lines) — `ToolRegistry` singleton: structured `Tool` dataclass (name, JSONSchema params, invoke callable) + `ToolCtx`. Built-in tools registered at import time. All modules share one registry.
- **`skills.py`** (~200 lines) — Loads user-installed skill directories from `~/.laintas_cli_skills/`. Each skill is a `skill.py` that exposes `get_tools() -> list[Tool]`. Tags tools with `source="skill:<name>"`.
- **`mcp_client.py`** (~370 lines) — Bridges async `mcp` SDK to the sync Tool registry. Runs a dedicated asyncio thread, one child subprocess per configured MCP server, registers tools as `source="mcp:<server>"`. Config lives in `~/.laintas_cli_mcp.json`.

**Cross-cutting subsystems:**
- **`policy.py`** (~370 lines) — Security policy engine: evaluates every command as allow/needs_approval/deny via regex rules. Config in `~/.laintas_cli_policy.json` (mtime-cached, zero-restart updates). Audit log in `~/.laintas_cli_audit.log`. Three modes: audit, enforce, disabled.
- **`memory_system.py`** (~290 lines) — Cross-session persistent memory mirroring Claude Code's architecture: 4 types (user/feedback/project/reference), stored as markdown files with frontmatter in `~/.laintas_cli_memory/`, indexed by `MEMORY.md`.
- **`hooks.py`** (~270 lines) — Event-driven hook system: pre_command, post_command, pre_tool, post_tool, on_session_start/end, on_error, on_memory_change. Config in `~/.laintas_cli_hooks.json`; Python hooks in `~/.laintas_cli_hooks.py` (mtime-cached).
- **`plan_mode.py`** (~260 lines) — Structured planning: `/plan enter` → AI explores & designs → writes plan to `~/.laintas_cli_plans/<name>.md` → `/plan approve` → execution.
- **`task_manager.py`** (~180 lines) — Persistent task tracking in `~/.laintas_cli_tasks.json` with status workflow (pending→in_progress→completed) and dependency links.

### Input Routing (REPL classifies every line)

1. Starts with `/` → meta command. Built-ins first (`/help`, `/login`, `/term`, `/debug`, `/name`, `/memory`, `/prop`, `/scan`, `/cwd`, `/clear`, `/exit`); unhandled `/` commands fall through to `.extra_command.py:handle_extra_command`.
2. First whitespace token resolves via `shutil.which(...)` or matches a shell/cmd builtin → direct PTY passthrough, no AI. `cd` is special-cased at the REPL to mutate parent CWD (PTY subshell can't).
3. Otherwise → `run_agent_loop()` (natural language).

Routing uses live `shutil.which()` lookups plus a fixed builtin set (`_POSIX_SHELL_BUILTINS` / `_WINDOWS_CMD_BUILTINS`) — newly-installed binaries are picked up immediately, no snapshot to refresh. `/scan` is a display-only enumerator.

### Auto-Generated Working-Directory Files

On first run in any cwd the CLI creates these (all listed in `.gitignore`, never check them in):

| File | Purpose |
|---|---|
| `.cli.prop` | AI system prompt template (Jinja-ish `{{var}}` substitution — see `generate_cli_prop_template`) |
| `.helpwo` | Persisted AI memory / project rules |
| `.extra_command.py` | User-extensible `handle_extra_command(action, parts, ctx)` for custom `/cmds` |
| `.loop_command.py` | User-extensible `handle_loop_command(command, ctx)` — return str to inject synthetic output, None to execute normally |

Both `.extra_command.py` and `.loop_command.py` are **mtime-cached** — edits take effect mid-session without restart. When adding behavior that the user should be able to override or extend, prefer wiring it through one of these hooks over hardcoding in `laintas_cli.py`.

### PTY Model

`InteractiveSession` is the workhorse: `os.fork()` + `pty.openpty()` + non-blocking `fcntl` reads + `termios` restore. Windows has no PTY — Unix-only stdlib imports (`pty`, `fcntl`, `termios`, `tty`, `select`) are guarded by `if not IS_WINDOWS:` near the top of `laintas_cli.py`, and Windows takes a `subprocess.run` fallback. **Any new code touching those modules must keep the `IS_WINDOWS` guard** — recent commits (`9cc8d07`, `8f53d4b`) fixed regressions caused by missing guards.

`SubTerminalSession` inside tmux spawns a new tmux window (`tmux new-window -d`) so interactive programs (`vim`, `claude`, REPLs) get native passthrough while the AI loop keeps running in the main pane. Outside tmux it degrades to a backgrounded `InteractiveSession`.

### Terminal & Agent Registries

Named sub-terminals (`/term new <name> <cmd>`) and sub-agents survive across loop iterations. `close_all_terminals()` / `close_all_agents()` are wired to SIGINT, SIGTERM, `/exit`, and normal shutdown — when adding new resources that own subprocesses, hook them into the same cascade.

The agent registry now supports tree hierarchies: parent/child links via `spawn_subagent()`, per-agent inbox queues (thread-safe, `queue.Queue`), `abort_agent()` with threading events, and `wait_for_agent()`. Registry mutations are protected by `_registry_lock` (`threading.RLock`).

### Runtime Configuration

All tunable parameters are accessible via `get_runtime_config()`/`set_runtime_config()` and modifiable at runtime via `/config`:

| Key | Default | Description |
|---|---|---|
| `max_loops` | 30 | Max AI loop iterations per task |
| `max_tokens` | 2000 | Max tokens for AI API response |
| `max_debug_entries` | 50 | Debug ring buffer size |
| `loop_delay` | 1.5 | Seconds between loop iterations |
| `output_truncate` | 3000 | Char limit for `lastOutput` tail |
| `poll_timeout` | 10.0 | Seconds to wait for first command output |
| `terminal_tail_lines` | 20 | Lines shown in sub-terminal snapshot |
| `heartbeat_interval` | 30 | Seconds between agent heartbeats |
| `staleness_limit` | 3 | Consecutive no-command steps before auto-exit |

### Backend API

`call_backend_stream` POSTs to `{backend}/api/chat/stream` and consumes SSE, returning `{reply, command, memory, done}`. Auth is cookie-or-Bearer (cached in `~/.laintas_cli_session.json`, chmod 600). Backend defaults to laintas.com; override with `LAINTAS_BACKEND` env or `--backend`.

`AgentRegistry` is the remote-control surface — `/api/agents/register`, `/api/agents/heartbeat`, `/api/agents/<id>/poll`, `/api/agents/<id>/events`. The HelpwoAI integration plan extends this with `reqId`-tagged events and structured `kind` payloads.

### User-Configurable Dotfiles (home directory)

These config files are mtime-cached (edits take effect without restart) and auto-created with safe defaults on first access:

| File | Module | Purpose |
|---|---|---|
| `~/.laintas_cli_policy.json` | `policy.py` | Command allow/approve/deny regex rules |
| `~/.laintas_cli_hooks.json` | `hooks.py` | Shell-command-based hook definitions |
| `~/.laintas_cli_hooks.py` | `hooks.py` | Python function hooks (mtime-cached) |
| `~/.laintas_cli_mcp.json` | `mcp_client.py` | MCP server configurations |
| `~/.laintas_cli_memory/` | `memory_system.py` | Cross-session persistent memory files |
| `~/.laintas_cli_plans/` | `plan_mode.py` | Saved execution plans |
| `~/.laintas_cli_tasks.json` | `task_manager.py` | Structured task list |
| `~/.laintas_cli_audit.log` | `policy.py` | JSONL audit trail of command decisions |

## Conventions

- Working directory commits should never include the auto-generated dotfiles (`.cli.prop`, `.helpwo`, `.extra_command.py`, `.loop_command.py`) — `.gitignore` already excludes them.
- Runtime-tunable knobs live in `agent_loop._runtime_config`. To add one: extend the `_DEFAULT_CONFIG` dict, and it becomes settable via `/config <key> <value>` automatically.
- Debug captures: wrap any agent-touching change with a `DebugEntry` write so it shows up in `/debug` TUI.
- When adding remote-protocol features, follow `HELPWO_INTEGRATION_PLAN.md`: every event must carry `reqId`, and each request has **exactly one** `final` event.
- New tools should register via `tools.ToolRegistry.register()` with a proper JSONSchema — they become automatically available to the AI agent. Use `source=` tags to enable bulk unregistration (`"builtin"`, `"skill:<name>"`, `"mcp:<server>"`).
- User-config files in `~/` that are mtime-cached should follow the pattern in `hooks.py` or `policy.py`: load on demand, cache mtime, re-read when stale.
