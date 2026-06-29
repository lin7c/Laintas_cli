# CLAUDE.md

This file provides guidance for AI coding assistants when working with code in this repository.

## Authoritative References

- `PROJECT.md` — full architecture spec (components, data flow, runtime config table, file artifacts). Read this before non-trivial changes.
- The HelpwoAI remote-executor protocol (originally specified in `HELPWO_INTEGRATION_PLAN.md`, deleted at `7e7324d` as "superseded" once it shipped) is fully implemented: `laintas_cli.py` handles `kind` values `exec`/`query`/`delegate`/`abort`/`approval-response`/`chat` with `reqId`-tagged events (see the request dispatcher and `needs-approval`/`approval-response` flow around `laintas_cli.py:3254-3733`). Treat this as done, not in-progress — there is no separate plan doc to consult.

## Run / Develop

```bash
source venv/bin/activate                 # Python venv lives in ./venv
python laintas_cli.py                    # Run from source (interactive REPL)
python laintas_cli.py --execute "task"   # Non-interactive single task
python laintas_cli.py --backend URL      # Override LAINTAS_BACKEND
```

There is no test suite, no linter config, and no Makefile — iterate by running the CLI directly. After editing files, force-reload the dev session by typing `/reload` inside the REPL (deletes `.laintas/` project files and restarts).

## Package Builds

```bash
./build/linux/build_deb.sh [VERSION]     # Requires fpm (gem install fpm); outputs build/linux/laintas-cli_X.Y.Z_amd64.deb
pyinstaller build/windows/laintas_cli.spec   # Outputs Windows exe; spec lists hiddenimports for rich/prompt_toolkit
```

The .deb launcher (`/usr/bin/laintas-cli`) lazy-installs `requirements.txt` via pip on first run and `cd`s into `$LAINTAS_WORKSPACE` (default `~/laintas_workspace`). Windows builds also have an NSIS installer (`build/windows/installer.nsi`).

A companion React/Vite download site lives in `laintas_cli_download/` (separate build, not part of the Python package).

**To publish a release to the download page, read `build/RELEASE.md`** — it documents the full flow for the 4 download artifacts (Linux/macOS/source built locally via `build/release/build_download_assets.sh`; the Windows `.exe` is **CI-only**, rebuilt on `windows-latest` when `laintas_cli.py`/`build/windows/**` is pushed to `main`).

## Architecture (read PROJECT.md for full detail)

The project has grown from two core modules to ten, organized in layers:

**Core (original two):**
- **`laintas_cli.py`** (~4900 lines) — Entry point, REPL, PTY execution (`InteractiveSession`, `SubTerminalSession`), OAuth/session auth, `AgentRegistry` (remote heartbeat + polling), meta-command dispatch, prompt template generation, debug TUI.
- **`agent_loop.py`** (~2400 lines) — Library: `run_agent_loop()`, runtime config, debug ring buffer, terminal registry (named persistent sub-terminals), agent registry (tree hierarchy, spawning, inboxes, abort), context compression, memory formatting, policy integration.

**Tools & extensibility layer (Phase 3):**
- **`tools.py`** (~1400 lines) — `ToolRegistry` singleton: structured `Tool` dataclass (name, JSONSchema params, invoke callable) + `ToolCtx`. Built-in tools registered at import time. All modules share one registry.
- **`skills.py`** (~200 lines) — Loads user-installed skill directories from `~/.laintas/skills/`. Each skill is a `skill.py` that exposes `get_tools() -> list[Tool]`. Tags tools with `source="skill:<name>"`.
- **`mcp_client.py`** (~370 lines) — Bridges async `mcp` SDK to the sync Tool registry. Runs a dedicated asyncio thread, one child subprocess per configured MCP server, registers tools as `source="mcp:<server>"`. Config lives in `~/.laintas/mcp.json`.

**Cross-cutting subsystems:**
- **`policy.py`** (~370 lines) — Security policy engine: evaluates every command as allow/needs_approval/deny via regex rules. Config in `~/.laintas/policy.json` (mtime-cached, zero-restart updates). Audit log in `~/.laintas/audit.log`. Three modes: audit, enforce, disabled.
- **`memory_system.py`** (~290 lines) — Cross-session persistent memory: 4 types (user/feedback/project/reference), stored as markdown files with frontmatter in `~/.laintas/memory/`, indexed by `MEMORY.md`.
- **`hooks.py`** (~270 lines) — Event-driven hook system: pre_command, post_command, pre_tool, post_tool, on_session_start/end, on_error, on_memory_change. Config in `~/.laintas/hooks.json`; Python hooks in `~/.laintas/hooks.py` (mtime-cached).
- **`plan_mode.py`** (~260 lines) — Structured planning: `/plan enter` → AI explores & designs → writes plan to `~/.laintas/plans/<name>.md` → `/plan approve` → execution.
- **`task_manager.py`** (~180 lines) — Persistent task tracking in `~/.laintas/tasks.json` with status workflow (pending→in_progress→completed) and dependency links.

### Input Routing (REPL classifies every line)

1. Starts with `/` → meta command. Built-ins first (`/help`, `/login`, `/term`, `/debug`, `/name`, `/memory`, `/prop`, `/scan`, `/cwd`, `/clear`, `/exit`); unhandled `/` commands fall through to `.laintas/commands.py:handle_extra_command`.
2. First whitespace token resolves via `shutil.which(...)` or matches a shell/cmd builtin → direct PTY passthrough, no AI. `cd` is special-cased at the REPL to mutate parent CWD (PTY subshell can't).
3. Otherwise → `run_agent_loop()` (natural language).

Routing uses live `shutil.which()` lookups plus a fixed builtin set (`_POSIX_SHELL_BUILTINS` / `_WINDOWS_CMD_BUILTINS`) — newly-installed binaries are picked up immediately, no snapshot to refresh. `/scan` is a display-only enumerator.

### Auto-Generated Working-Directory Files

On first run in any cwd the CLI creates a `.laintas/` subdirectory (listed in `.gitignore`, never check it in):

| File | Purpose |
|---|---|
| `.laintas/cli.prop` | AI system prompt template (Jinja-ish `{{var}}` substitution — see `generate_cli_prop_template`) |
| `.laintas/memory.json` | Persisted AI memory / project rules |
| `.laintas/commands.py` | User-extensible `handle_extra_command(action, parts, ctx)` for custom `/cmds` |
| `.laintas/loop.py` | User-extensible `handle_loop_command(command, ctx)` — return str to inject synthetic output, None to execute normally |

Both `.laintas/commands.py` and `.laintas/loop.py` are **mtime-cached** — edits take effect mid-session without restart. When adding behavior that the user should be able to override or extend, prefer wiring it through one of these hooks over hardcoding in `laintas_cli.py`.

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

`call_backend_stream` POSTs to `{backend}/api/chat/stream` and consumes SSE, returning `{reply, command, memory, done}`. Auth is cookie-or-Bearer (cached in `~/.laintas/session.json`, chmod 600). Backend defaults to laintas.com; override with `LAINTAS_BACKEND` env or `--backend`.

`AgentRegistry` is the remote-control surface — `/api/agents/register`, `/api/agents/heartbeat`, `/api/agents/<id>/poll`, `/api/agents/<id>/events`. The HelpwoAI integration plan extends this with `reqId`-tagged events and structured `kind` payloads.

### User-Configurable Files (all under `~/.laintas/`)

All configuration lives in a single `~/.laintas/` directory (override with `LAINTAS_HOME` env var). Config files are mtime-cached (edits take effect without restart) and auto-created with safe defaults on first access:

| File | Module | Purpose |
|---|---|---|
| `~/.laintas/policy.json` | `policy.py` | Command allow/approve/deny regex rules |
| `~/.laintas/hooks.json` | `hooks.py` | Shell-command-based hook definitions |
| `~/.laintas/hooks.py` | `hooks.py` | Python function hooks (mtime-cached) |
| `~/.laintas/mcp.json` | `mcp_client.py` | MCP server configurations |
| `~/.laintas/memory/` | `memory_system.py` | Cross-session persistent memory files |
| `~/.laintas/plans/` | `plan_mode.py` | Saved execution plans |
| `~/.laintas/tasks.json` | `task_manager.py` | Structured task list |
| `~/.laintas/audit.log` | `policy.py` | JSONL audit trail of command decisions |
| `~/.laintas/session.json` | `laintas_cli.py` | Authentication session (chmod 600) |
| `~/.laintas/config.json` | `laintas_cli.py` | Global settings (agentName, backendUrl) |
| `~/.laintas/history` | `laintas_cli.py` | REPL command history |
| `~/.laintas/agents/` | `agent_persistence.py` | Per-agent state and chat history |
| `~/.laintas/skills/` | `skills.py` | User-installed skill directories |

All paths are centralized in `paths.py`. On first launch, `migrate.py` auto-migrates any old `~/.laintas_cli_*` files to the new layout.

## Conventions

- Working directory commits should never include the `.laintas/` project directory — `.gitignore` already excludes it.
- Runtime-tunable knobs live in `agent_loop._runtime_config`. To add one: extend the `_DEFAULT_CONFIG` dict, and it becomes settable via `/config <key> <value>` automatically.
- Debug captures: wrap any agent-touching change with a `DebugEntry` write so it shows up in `/debug` TUI.
- When adding remote-protocol features, follow `HELPWO_INTEGRATION_PLAN.md`: every event must carry `reqId`, and each request has **exactly one** `final` event.
- New tools should register via `tools.ToolRegistry.register()` with a proper JSONSchema — they become automatically available to the AI agent. Use `source=` tags to enable bulk unregistration (`"builtin"`, `"skill:<name>"`, `"mcp:<server>"`).
- User-config files in `~/` that are mtime-cached should follow the pattern in `hooks.py` or `policy.py`: load on demand, cache mtime, re-read when stale.
