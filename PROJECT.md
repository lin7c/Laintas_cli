# Laintas CLI — Project Documentation

## Overview

Laintas CLI is an autonomous AI agent for the terminal. Users type natural-language tasks at a prompt; the AI agent loop calls a backend API (Laintas/Helpwo), receives a reply with a shell command, executes that command in a pseudo-terminal, feeds the output back to the AI, and iterates until the task is done. System commands typed directly are executed via PTY passthrough with no AI involvement.

**Version:** 0.1.1
**Python:** >= 3.10
**Files:** 12 Python modules (~11,500 lines total) — see CLAUDE.md for the full module table

---

## Architecture

### High-Level Design

```
┌──────────────────────────────────────────────────┐
│                   User Terminal                   │
│  (prompt_toolkit input with auto-suggest/history) │
└─────────────────┬────────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────────┐
│                  REPL (main loop)                 │
│                                                  │
│  ┌──────────────┐  ┌───────────────────────────┐ │
│  │ System Cmd   │  │  Natural Language Input   │ │
│  │ (direct exec)│  │  → AI Agent Loop          │ │
│  └──────┬───────┘  └───────────┬───────────────┘ │
│         │                      │                  │
│         ▼                      ▼                  │
│  ┌──────────────┐  ┌───────────────────────────┐ │
│  │ pty_passthru │  │ run_agent_loop()          │ │
│  │ (full TTY)   │  │ - backend API call        │ │
│  └──────────────┘  │ - command execution       │ │
│                    │ - state management        │ │
│                    └───────────┬───────────────┘ │
│                                │                  │
│                                ▼                  │
│                    ┌───────────────────────────┐ │
│                    │ AgentRegistry             │ │
│                    │ - heartbeat               │ │
│                    │ - remote message polling  │ │
│                    │ - event streaming         │ │
│                    └───────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

### Input Routing

User input is classified at the REPL level:

1. **`/` prefix** → Meta commands handled locally. First tries hardcoded handlers (`/help`, `/login`, `/name`, `/debug`, `/term`, `/config`, `/reload`, etc.), then falls through to `.extra_command.py` if present.
2. **First word matches a PATH executable** → Direct system command execution via PTY passthrough
3. **Everything else** → Natural language → AI agent loop

### Module Split

- **`laintas_cli.py`** — CLI entry point: REPL, PTY execution (`InteractiveSession`, `SubTerminalSession`), backend API client (`call_backend_stream`), OAuth authentication, agent registry (remote heartbeat/messaging), prompt template generation, file management, debug TUI, meta-command dispatch, `/.extra_command.py` loader.
- **`agent_loop.py`** — Library module exposing: runtime config, debug system (`DebugEntry`), terminal registry (named persistent sub-terminals), and `run_agent_loop()` itself. All hardcoded constants are replaced with runtime-configurable values.

---

## Core Components

### 1. PTY Command Execution (`InteractiveSession`)

Commands run inside a pseudo-terminal (PTY) so they get real TTY semantics — colors, progress bars, and interactive programs work correctly.

- `start()` — forks and execs the command in a PTY
- `send_keys(text)` — sends keystrokes with escape sequence support (`\n`, `\t`, `\x1b[A`)
- `read_output(timeout)` — non-blocking read from PTY master fd
- `is_alive()` — checks if child process is still running
- `close()` — SIGTERM → SIGKILL cleanup
- `run_to_completion()` — blocking one-shot execution for simple commands

Uses `os.fork()`, `pty.openpty()`, `os.execve()`, non-blocking I/O via `fcntl`, and `termios` preservation. Linux-only (Unix-only stdlib imports).

### 2. Sub-Terminal Session (`SubTerminalSession`)

For interactive programs (`claude`, `vim`, `python` REPL), the command runs in a separate terminal context:
- **Inside tmux:** Creates a new tmux window (`tmux new-window -d`) for native terminal passthrough while the main terminal continues the AI loop
- **Outside tmux:** Falls back to a background `InteractiveSession` (PTY)

Provides the same interface as `InteractiveSession`.

### 3. Terminal Registry (Named Persistent Sub-Terminals)

Named sub-terminals survive across loop iterations and after `done: true`. Managed through:
- `register_terminal(session, command, depth, name)` — registers with auto-generated or explicit name, closes old terminal on name collision
- `unregister_terminal(name)` — close and remove
- `get_terminal(name)` / `get_all_terminals()` — lookup
- `close_all_terminals()` — cascading cleanup on exit
- `rename_terminal(old, new)` — rename (replaces target if exists)

The AI creates/manages them via `/term new <name> <cmd>`, `/term send <name> <keys>`, and `/term close <name>` meta-commands. Users manage them with the same `/term` commands or the interactive `/term` browser TUI. A snapshot of the latest 20 lines from each alive terminal is included in the AI's context at each loop iteration.

### 4. AI Agent Loop (`run_agent_loop`)

Each iteration:
1. Reads `.helpwo` (project memory/rules) and `.cli.prop` (system prompt template)
2. Builds conversation history from chat context
3. Substitutes template variables (`{{currentPath}}`, `{{lastOutput}}`, `{{globalMemory}}`, etc.)
4. Calls backend API (`/api/chat/stream`) with SSE streaming
5. Processes AI response: prints `reply`, writes `memory` to `.helpwo`, executes `command` or `/term` meta-command
6. Updates execution state, feeds back output for next iteration
7. Checks `.loop_command.py` before executing normal commands — if the handler returns a string, uses it as synthetic output; if None, executes normally

### 5. Runtime Configuration

All tunable parameters are accessible via `get_runtime_config()`/`set_runtime_config()` and modifiable at runtime via `/config`:

| Key | Default | Description |
|---|---|---|
| `max_loops` | 10 | Max AI loop iterations per task |
| `max_tokens` | 2000 | Max tokens for AI API response |
| `max_debug_entries` | 50 | Debug ring buffer size |
| `loop_delay` | 1.5 | Seconds between loop iterations |
| `output_truncate` | 3000 | Char limit for `lastOutput` tail |
| `poll_timeout` | 10.0 | Seconds to wait for first command output |
| `terminal_tail_lines` | 20 | Lines shown in sub-terminal snapshot |
| `heartbeat_interval` | 30 | Seconds between agent heartbeats |
| `search_engine` | `auto` | Search engine: `auto` (Google→DDG→laintas_search chain), `google`, `duckduckgo`, `laintas_search` |
| `search_laintas_api_key` | _(none)_ | API key for `search.laintas.com` (required for laintas_search engine) |
| `search_laintas_api_url` | `https://search.laintas.com` | Base URL for laintas_search API |
| `search_proxy` | _(none)_ | Proxy URL for web.search/web.fetch (e.g. `socks5://127.0.0.1:1080`, `http://proxy:8080`); env `LAINTAS_HTTP_PROXY` |
| `search_cookie_enabled` | `false` | Enable shared cookie jar for web.search/web.fetch (Google consent cookie auto-injected) |

### 6. Backend API Client (`call_backend_stream`)

Calls the Laintas backend `/api/chat/stream` via SSE: sends `message`, `history`, `currentPath`, `systemPrompt`, `lang`, `maxTokens`. Authenticates via cookies or `Authorization` header. Returns parsed JSON with `reply`, `command`, `memory`, `done`.

### 7. Extensible Command System

Two auto-generated files provide pluggable command handling:

- **`.extra_command.py`** — Defines `handle_extra_command(action, parts, ctx)` for custom slash commands (`/config`, `/reload`). Loaded with mtime-based caching so edits take effect without restart.
- **`.loop_command.py`** — Defines `handle_loop_command(command, ctx)` for custom loop-level commands (`wait(N)`). If the handler returns a string, it becomes `lastOutput`; if None, the command executes normally.

Both files are created empty (or from templates) alongside `.cli.prop` on first run.

### 8. Agent Registry (`AgentRegistry`)

Optional remote registration with a Laintas backend: registers with hostname/OS/shell/CWD, sends periodic heartbeats, polls for remote messages from the Laintas web UI, streams events (user input, AI replies, command output) in real time. Works standalone without registration.

### 9. Authentication

Three methods: cached session (`~/.laintas_cli_session.json`, chmod 600), browser-based OAuth login (local HTTP callback server + laintas.com), or terminal username/password login.

### 10. Prompt System (`.cli.prop`)

The AI system prompt is templated from `.cli.prop` (auto-generated if missing). Template variables: `{{currentPath}}`, `{{activeFile}}`, `{{globalMemory}}`, `{{lastOutput}}`, `{{conversationHistory}}`, `{{depth}}`, `{{nextDepth}}`.

`/prompt [issue]` opens a project-scoped Prompt Lab branch. It snapshots the
current conversation, effective prompt, tool events, and agent state, then runs
a read-only background diagnosis without injecting results into the main task.
The AI drafts a structured overlay plus regression cases. `/prompt test` runs a
no-side-effects evaluator. Activation, profile switching, disabling, and
rollback require an interactive confirmation and are hot-reloaded on the next
agent-loop iteration; the base `cli.prop` is not rewritten.

### 10.1 Evolution Lab and project extensions

`/evolve <idea>` mirrors Prompt Lab for executable project creativity. It
creates a project-scoped branch, asks a restricted design worker to draft an
extension or focused `commands.py`/`loop.py` improvement, compares the current
and candidate implementations in a temporary subprocess environment, and
requires user confirmation before activation. Candidates, runs, profiles and
history live under `.laintas/evolution-lab/`; activated standalone extensions
live under `.laintas/extensions/` and register commands, tools or loop
interceptors through `setup(ctx)`. Extensions hot-load independently and
survive `/reload`. Integrated inference is exposed as `ctx.backend.chat()` so
raw authentication is not placed in the extension context.

### 11. Debug System

`DebugEntry` dataclass captures full state of each agent interaction. In-memory ring buffer (configurable size). Interactive `prompt_toolkit` TUI browser with arrow-key navigation, detail view (user input, request payload, AI response, raw JSON, command output).

### 11.1 Unified WorkGraph

Plans, executable steps (`/task`), workflow phase, approvals, and resume identity
share one project-local SQLite authority at `.laintas/workgraph.db`. Plans are
immutable revisions identified by SHA-256. `plan.submit` is the only readiness
signal; user approval binds the exact revision/SHA before ACT mode begins.
Markdown files under `~/.laintas/plans/` and legacy JSON files are compatibility
projections/import sources, not authorities. Plan implementation steps are
transactionally projected into WorkGraph Steps, with foreign-key dependencies,
cycle detection, normalized progress/status, and an append-only event history.

### 12. Meta Commands (Slash Commands)

| Command | Description |
|---|---|
| `/help` | Show available commands |
| `/login` | Re-authenticate |
| `/name [n]` | Set/view agent name; `/name term<N> <new>` renames a terminal |
| `/memory` | View `.helpwo` contents |
| `/prop` | View `.cli.prop` template |
| `/prompt [issue]` | Capture a behavior incident and open Prompt Lab |
| `/mode [act|plan|review|study|list]` | View or switch agent behavior mode (`study` = read-only mentor: the user writes the code, the agent teaches and checks it) |
| `/mode create <name> [--read-only] <instructions>` | Create a declarative project mode |
| `/mode delete <name>` | Delete a custom project mode |
| `/work [status|list|resume|history]` | Inspect unified work state |
| `/plan enter|submit|revise|approve` | Manage versioned, reviewed plans |
| `/task` | View or update the active WorkGraph steps |
| `/scan` | Rescan PATH for executables |
| `/debug` | Browse AI interaction debug logs (TUI) |
| `/cwd` | Show current working directory |
| `/hire [name] [--profile role] [--prompt file] [--tools names\|inherit]` | Define a persistent employee capability profile without starting work |
| `/station <agent> [terminal] --task <work>` | Give an employee a fresh assignment (dedicated PTY) |
| `/agents [agent-id [info]]` | List employees; `/agents <id>` switches the displayed Agent (replays buffered output); `info` shows capabilities and assignment history |
| `/t, /term` | Sub-terminal manager: `new <n> <cmd>`, `send <n> <k>`, `close <n>`, `details <n>` |
| `/config [key] [value]` | View/set runtime config (from `.extra_command.py`) |
| `/reload` | Delete all default files and restart laintas-cli (from `.extra_command.py`) |
| `/clear` | Clear screen |
| `/exit, /quit` | Exit (cascading cleanup of all terminals) |

### 13. Web Search & Fetch (`web_search.py`)

The `web.search` and `web.fetch` tools are backed by `web_search.py`, a standalone
module that implements a multi-engine search chain with proxy support, cookie
sharing, and structured error reporting.

**Engine chain** (default order, tried sequentially on failure):

1. **Google** — HTML scrape of `www.google.com/search`. When cookies are enabled,
   a `CONSENT=YES+` cookie is auto-injected to bypass the EU consent wall.
2. **DuckDuckGo** — HTML scrape of `html.duckduckgo.com/html/` (lite endpoint).
3. **laintas_search** — Native HTTP API (`POST /search` with `X-API-KEY` header).
   Does **not** go through the user's proxy (HTTPS to `search.laintas.com`;
   GFW typically does not block it). Requires `search_laintas_api_key`.

When `search_engine` is `auto`, engines are tried in order. A failed engine
(captcha, rate limit, network error) enters a 5-minute fast-fail cooldown and
is skipped on subsequent calls. laintas_search is exempt from fast-fail.

**Proxy support:** `search_proxy` (or `LAINTAS_HTTP_PROXY` env) accepts
`http://`, `https://`, `socks5://`, and `socks5h://` URLs. SOCKS5 support uses
PySocks (a core dependency). `web.fetch` always uses the user's network/proxy.

**Cookie jar:** When `search_cookie_enabled` is true, `web.search` and
`web.fetch` share a process-level `RequestsCookieJar`. This helps maintain
search sessions and bypass consent walls. Cookies can be cleared via
`web_search.clear_cookie_jar()`.

**Structured errors:** Failures are classified into `SearchErrorType` enums so
the AI agent can reason about cause: `captcha`, `rate_limited`, `network`,
`proxy_error`, `consent_page`, `empty`, `degraded`, `api_error`.

---

## File System Artifacts

| File | Location | Purpose |
|---|---|---|
| `modes.json` | `<project>/.laintas/` | Active mode and declarative custom modes |
| `~/.laintas_cli_session.json` | Home directory | Cached auth session (chmod 600) |
| `~/.laintas_cli_config.json` | Home directory | CLI config (agent name, preferences) |
| `~/.laintas_cli_history` | Home directory | prompt_toolkit command history |
| `.cli.prop` | Working directory | AI system prompt template (auto-generated) |
| `.helpwo` | Working directory | AI memory / project rules (auto-created empty) |
| `.extra_command.py` | Working directory | Custom slash command handlers (auto-created) |
| `.loop_command.py` | Working directory | Custom loop command handlers (auto-created) |
| `.laintas/prompt-lab/` | Project directory | Prompt Lab branches, tested overlays, profiles, and activation history |
| `.laintas/workgraph.db` | Project directory | Transactional objective, plan revisions, steps, workflow, approvals, and events |

---

## Data Flow

### System Command
```
User: "ls -la"
  → is_system_command() → True (shutil.which("ls") resolves)
  → pty_passthrough("ls -la")
  → InteractiveSession forks, execs, output streams to terminal
```

### AI Agent
```
User: "find all Python files and count lines"
  → is_system_command() → False
  → run_agent_loop()
    → Loop 1: API call → AI: command="find . -name '*.py' | xargs wc -l"
    → Loop 2: API call with output → AI: reply="Found N files, M lines", done=true
```

### Named Terminal (AI-managed)
```
AI: /term new srv npm run dev
  → SubTerminalSession created, registered as "srv"
  → Survives across loop iterations
AI: /term send srv ls\n
  → Keystrokes sent to "srv", output captured and fed to next iteration
AI: /term close srv
  → Terminal destroyed
```

---

## Dependencies

```
requests>=2.28.0      # HTTP client for backend API, web.search, web.fetch
rich>=13.0.0          # Terminal UI (Panel, Markdown, Table, Live, Spinner)
prompt_toolkit>=3.0.0 # Interactive prompt with completion, history, keybindings
pysocks>=1.7          # SOCKS5 proxy support for web.search / web.fetch
certifi>=2024.0.0     # TLS certificate bundle
```

Unix-specific stdlib: `pty`, `select`, `fcntl`, `termios`, `http.server`

---

## CLI Usage

```bash
laintas-cli                          # Interactive session
laintas-cli --name my-server         # Custom agent name
laintas-cli --backend URL            # Custom backend URL
laintas-cli --execute "task"         # Non-interactive single task
laintas-cli --execute "task" --depth 1  # Sub-agent with explicit depth
laintas-cli --simple-prompt          # PTY-safe simple input mode
```

**Environment variables:** `LAINTAS_BACKEND`, `LAINTAS_BASE`

---

## Key Design Decisions

1. **PTY over subprocess** — Real TTY semantics for colors, progress bars, interactive programs
2. **tmux for interactive sub-programs** — Native terminal passthrough while AI loop keeps running
3. **Two-file architecture** — REPL/CLI logic in `laintas_cli.py`, agent loop library in `agent_loop.py`
4. **Extensible commands** — `.extra_command.py` and `.loop_command.py` with mtime-based caching for zero-restart iteration
5. **Runtime-configurable** — All tunable parameters live in `_runtime_config` dict, modifiable via `/config`
6. **Cascading cleanup** — `close_all_terminals()` fires on SIGINT, SIGTERM, `/exit`, and normal shutdown
7. **Depth-based recursion control** — Sub-agents via `--execute`, capped at 3 nesting levels

---

## Known Limitations

- tmux is required for seamless sub-terminal interactive sessions
- No persistent session recovery — PTY state is lost on crash
