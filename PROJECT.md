# Laintas CLI — Project Documentation

## Overview

Laintas CLI is an autonomous AI agent for the terminal. Users type natural-language tasks at a prompt; the AI agent loop calls a backend API (Laintas/Helpwo), receives a reply with a shell command, executes that command in a pseudo-terminal, feeds the output back to the AI, and iterates until the task is done. System commands typed directly are executed via PTY passthrough with no AI involvement.

**Version:** 0.1.0
**Python:** >= 3.10
**Files:** `laintas_cli.py` (~3118 lines) + `agent_loop.py` (~755 lines)

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

Uses `os.fork()`, `pty.openpty()`, `os.execve()`, non-blocking I/O via `fcntl`, and `termios` preservation. Windows fallback uses `subprocess.run()`.

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

### 6. Backend API Client (`call_backend_stream`)

Calls the Laintas backend `/api/chat/stream` via SSE: sends `message`, `history`, `currentPath`, `systemPrompt`, `lang`, `maxTokens`. Authenticates via cookies or `Authorization` header. Returns parsed JSON with `reply`, `command`, `memory`, `done`.

### 7. Extensible Command System

Two auto-generated files provide pluggable command handling:

- **`.extra_command.py`** — Defines `handle_extra_command(action, parts, ctx)` for custom slash commands (`/config`, `/reload`). Loaded with mtime-based caching so edits take effect without restart.
- **`.loop_command.py`** — Defines `handle_loop_command(command, ctx)` for custom loop-level commands (`wait(N)`). If the handler returns a string, it becomes `lastOutput`; if None, the command executes normally.

Both files are created empty (or from templates) alongside `.cli` and `.cli.prop` on first run.

### 8. Agent Registry (`AgentRegistry`)

Optional remote registration with a Laintas backend: registers with hostname/OS/shell/CWD, sends periodic heartbeats, polls for remote messages from the Laintas web UI, streams events (user input, AI replies, command output) in real time. Works standalone without registration.

### 9. Authentication

Three methods: cached session (`~/.laintas_cli_session.json`, chmod 600), browser-based OAuth login (local HTTP callback server + laintas.com), or terminal username/password login.

### 10. Prompt System (`.cli.prop`)

The AI system prompt is templated from `.cli.prop` (auto-generated if missing). Template variables: `{{currentPath}}`, `{{activeFile}}`, `{{globalMemory}}`, `{{lastOutput}}`, `{{conversationHistory}}`, `{{depth}}`, `{{nextDepth}}`.

### 11. Debug System

`DebugEntry` dataclass captures full state of each agent interaction. In-memory ring buffer (configurable size). Interactive `prompt_toolkit` TUI browser with arrow-key navigation, detail view (user input, request payload, AI response, raw JSON, command output).

### 12. Meta Commands (Slash Commands)

| Command | Description |
|---|---|
| `/help` | Show available commands |
| `/login` | Re-authenticate |
| `/name [n]` | Set/view agent name; `/name term<N> <new>` renames a terminal |
| `/memory` | View `.helpwo` contents |
| `/prop` | View `.cli.prop` template |
| `/scan` | Rescan PATH for executables |
| `/debug` | Browse AI interaction debug logs (TUI) |
| `/cwd` | Show current working directory |
| `/t, /term` | Sub-terminal manager: `new <n> <cmd>`, `send <n> <k>`, `close <n>`, `details <n>` |
| `/config [key] [value]` | View/set runtime config (from `.extra_command.py`) |
| `/reload` | Delete all default files and restart laintas-cli (from `.extra_command.py`) |
| `/clear` | Clear screen |
| `/exit, /quit` | Exit (cascading cleanup of all terminals) |

---

## File System Artifacts

| File | Location | Purpose |
|---|---|---|
| `~/.laintas_cli_session.json` | Home directory | Cached auth session (chmod 600) |
| `~/.laintas_cli_config.json` | Home directory | CLI config (agent name, preferences) |
| `~/.laintas_cli_history` | Home directory | prompt_toolkit command history |
| `.cli` | Working directory | Discovered PATH executables (auto-generated) |
| `.cli.prop` | Working directory | AI system prompt template (auto-generated) |
| `.helpwo` | Working directory | AI memory / project rules (auto-created empty) |
| `.extra_command.py` | Working directory | Custom slash command handlers (auto-created) |
| `.loop_command.py` | Working directory | Custom loop command handlers (auto-created) |

---

## Data Flow

### System Command
```
User: "ls -la"
  → is_system_command() → True ("ls" in .cli)
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
requests>=2.28.0      # HTTP client for backend API
rich>=13.0.0          # Terminal UI (Panel, Markdown, Table, Live, Spinner)
prompt_toolkit>=3.0.0 # Interactive prompt with completion, history, keybindings
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

- Windows support is partial — interactive programs fall back to `subprocess.run()`
- tmux is required for seamless sub-terminal interactive sessions
- `.cli` command list is a static PATH snapshot; new commands need `/scan` or restart
- No persistent session recovery — PTY state is lost on crash
