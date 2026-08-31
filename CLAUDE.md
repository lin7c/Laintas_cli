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

There IS a test suite — 39 files under `tests/`, 664 tests. Activate the venv first (`source venv/bin/activate`); two modules import `pytest`, which the system python lacks.

```bash
python3 -m unittest discover -s tests          # full suite, ~70s
python3 -m unittest tests.test_git_policy      # targeted, sub-second — use this while iterating
python3 -m pytest tests/                       # also works, ~85s
```

The suite is green on a clean checkout — no expected failures, and CI runs it with no deselect list. Note `-t .` fails — there is no `tests/__init__.py`.

There is no linter config and no Makefile. Beyond tests, iterate by running the CLI directly. After editing files, force-reload the dev session by typing `/reload` inside the REPL (deletes `.laintas/` project files and restarts).

## Package Builds

```bash
./build/linux/build_deb.sh [VERSION]     # Requires fpm (gem install fpm); outputs build/linux/laintas-cli_X.Y.Z_amd64.deb
```

The .deb launcher (`/usr/bin/laintas-cli`) lazy-installs `requirements.txt` via pip on first run and `cd`s into `$LAINTAS_WORKSPACE` (default `~/laintas_workspace`).

A companion React/Vite download site lives in `laintas_cli_download/` (separate build, not part of the Python package).

**To publish a release, use `.github/workflows/release.yml`** — it builds Linux amd64/arm64 binaries and a source package, then publishes checksums and update manifests to GitHub Releases.

## Architecture (read PROJECT.md for full detail)

The project has grown from two core modules to ten, organized in layers:

**Core (original two):**
- **`laintas_cli.py`** (~19,600 lines) — Entry point, REPL, PTY execution (`InteractiveSession`, `SubTerminalSession`), OAuth/session auth, `AgentRegistry` (remote heartbeat + polling), meta-command dispatch, prompt template generation, debug TUI.
- **`agent_loop.py`** (~8,600 lines) — Library: `run_agent_loop()`, runtime config, debug ring buffer, terminal registry (named persistent sub-terminals), agent registry (tree hierarchy, spawning, inboxes, abort), context compression, memory formatting, policy integration.

**Tools & extensibility layer (Phase 3):**
- **`tools.py`** (~7,200 lines) — `ToolRegistry` singleton: structured `Tool` dataclass (name, JSONSchema params, invoke callable) + `ToolCtx`. Built-in tools registered at import time. All modules share one registry.
- **`skills.py`** (~720 lines) — Loads user-installed skill directories from `~/.laintas/skills/`. Each skill is a `skill.py` that exposes `get_tools() -> list[Tool]`. Tags tools with `source="skill:<name>"`.
- **`mcp_client.py`** (~430 lines) — Bridges async `mcp` SDK to the sync Tool registry. Runs a dedicated asyncio thread, one child subprocess per configured MCP server, registers tools as `source="mcp:<server>"`. Config lives in `~/.laintas/mcp.json`.
- **`web_search.py`** (~2,900 lines) - Backs `web.search` and `web.fetch`.
  - **Search**: engine chain (Google -> DuckDuckGo -> laintas_search), fast-fail cooldown, structured error types, `region`/`timelimit` filters.
  - **Fetch**: escalates instead of failing — plain HTTP → curl_cffi TLS fingerprint → rendered browser → hand the challenge to the user in the live view → Wayback snapshot. Each result names its rung in `transport`; failures list every rung tried. Every URL and **every redirect hop** is SSRF-checked (`_guard_url`) — this runs on the user's machine, so an unguarded fetch reaches their LAN and cloud metadata.
  - The render tier owns its browser on one dedicated thread (`_RenderWorker`) because Playwright's sync API is thread-affine; `browser.*` tools skip sessions marked `_owned_by_web_fetch`.
  - Proxy config is shared with the headless browser via `browser_egress_overrides()` (`egress_from_env` falls back to it), with per-host "auto" routing.
  - **Engines are a registry, not a chain of branches.** Built-ins: `google`, `duckduckgo`, `cn-bing` (works from inside China), `laintas_search` (user's key), `laintas_gateway` (signed-in account via `/api/agent/search`, no key needed, bills balance). Default order is by **cost, not quality** — the metered tiers are last and are usually the best results. Users add JSON APIs (serper, extra keys) in `~/.laintas/search_engines.json`; `web.search(engines=[...])` lets the model pick, and failures report per-engine health. Scraped-HTML engines are deliberately **not** user-definable.
- **`cookie_store.py`** (~330 lines) - Anti-bot **clearance** store in `~/.laintas/cookies.json` (chmod 600), shared by search, fetch and the browser; what makes a manually solved CAPTCHA keep working. Three rules:
  1. **Clearance only.** `cf_clearance`, `__cf_bm`, `GOOGLE_ABUSE_EXEMPTION`, DataDome, Imperva… (extend with `search_cookie_names`). A session cookie the browser picked up while the user was signing in is a *credential*, not clearance — it is reported and dropped, never made ambient. Promote it deliberately with `/identity capture`.
  2. **Bound to its exit.** Clearance names the address it was issued to (Google's exemption literally contains `IP=<addr>`), so every record is stamped with `web_search.current_egress()` and filtered on load. `load(all_egress=True)` for listing/clearing — and `merge()` uses it, since `save()` rewrites the whole file and a filtered merge would silently delete another exit's cookies.
  3. **Flows both ways.** `_seed_browser_cookies()` injects the store into each new render session via `context.add_cookies`; without it the browser closes after 5 idle minutes and the next one faces a wall this machine had already cleared.
- **`identity_store.py`** (~300 lines) - Named signed-in sessions in `~/.laintas/identities/<name>.json` (0700 dir, 0600 files), the basis for automation that runs *as the user*. Four rules it exists to enforce:
  1. Stores Playwright `storage_state` (cookies **and** localStorage) — cookie-only export logs you out of sites that keep the session in localStorage.
  2. Pins the **egress**: a session replayed from a different exit is refused (Google's abuse exemption literally embeds the IP it was issued for).
  3. Credentials are **never ambient** — `web.fetch` attaches an identity only when the caller names one *and* the URL is inside that identity's own domains. This is the prompt-injection boundary.
  4. Values are **never returned to the model**; `identity.list` / `describe()` expose names, domains and freshness only.

  Created by `remote_browser/rb.py save <name>` after a human signs in through the temporary live-view route; `identity.check` / `web_search.probe_identity()` verify a session is still alive before a task depends on it. Manual sign-in pays off for **login walls** (sessions last weeks); it does not for anti-bot walls (exemptions are short-lived and IP-bound — measured dead within 3 days).
  - Optional deps (`pip install .[web]`): `trafilatura` for article extraction — without it the fallback keeps nav bars and footers in what the model reads — and `curl_cffi` for the fingerprint rung.

**Cross-cutting subsystems:**
- **`policy.py`** (~370 lines) — Security policy engine: evaluates every command as allow/needs_approval/deny via regex rules. Config in `~/.laintas/policy.json` (mtime-cached, zero-restart updates). Audit log in `~/.laintas/audit.log`. Three modes: audit, enforce, disabled.
- **`memory_system.py`** (~290 lines) — Cross-session persistent memory: 4 types (user/feedback/project/reference), stored as markdown files with frontmatter in `~/.laintas/memory/`, indexed by `MEMORY.md`.
- **`hooks.py`** (~270 lines) — Event-driven hook system: pre_command, post_command, pre_tool, post_tool, on_session_start/end, on_error, on_memory_change. Config in `~/.laintas/hooks.json`; Python hooks in `~/.laintas/hooks.py` (mtime-cached).
- **`plan_mode.py`** (~260 lines) — Structured planning: `/plan enter` → AI explores & designs → writes plan to `~/.laintas/plans/<name>.md` → `/plan approve` → execution.
- **`task_manager.py`** (~180 lines) — Persistent task tracking in `~/.laintas/tasks.json` with status workflow (pending→in_progress→completed) and dependency links.

### Config vs. subsystem commands

`/config` holds **scalars** (76 keys, flat). Anything with *state* — a file, a
registry, credentials — gets its own slash command, as `/policy`, `/trust`,
`/hooks`, `/backend` and `/skill` already do. The web stack follows that split:

| Surface | Command |
|---|---|
| 14 scalar knobs (`search_*`, `fetch_*`, `browser_*`) | `/config search`, `/config fetch`, … — an unmatched word is treated as a **prefix filter** before it is treated as a mistake |
| Engine registry, proxy routing, cookie jar, diagnostics | `/web` (alias `/search`) — `status`, `engines [init]`, `test [engine]`, `try <query>`, `cookies [clear [domain]]` |
| Saved logins | `/identity` — `list`, `check`, `capture`, `delete`. Never prints a cookie or token value |

`/web test` is not a nicety: scraped engines fail *without erroring* — a full
result list with every snippet empty (the DDG bug), or a 200 carrying an empty
result frame (cn.bing without fetch-metadata headers). Only running one and
counting snippet coverage reveals it, so the command reports `degraded`
separately from `ok` and `fail`. Its probe query is deliberately operator-free:
`site:` makes cn.bing look broken when it is merely unsupported.

### Input Routing (REPL classifies every line)

1. Starts with `/` → meta command. Built-ins first (`/help`, `/login`, `/term`, `/debug`, `/name`, `/memory`, `/prop`, `/scan`, `/cwd`, `/clear`, `/exit`); unhandled `/` commands fall through to `.laintas/commands.py:handle_extra_command`.
2. First whitespace token resolves via `shutil.which(...)` or matches a shell/cmd builtin → direct PTY passthrough, no AI. `cd` is special-cased at the REPL to mutate parent CWD (PTY subshell can't).
3. Otherwise → `run_agent_loop()` (natural language).

Routing uses live `shutil.which()` lookups plus a fixed builtin set (`_POSIX_SHELL_BUILTINS`) — newly-installed binaries are picked up immediately, no snapshot to refresh. `/scan` is a display-only enumerator.

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

`InteractiveSession` is the workhorse: `os.fork()` + `pty.openpty()` + non-blocking `fcntl` reads. Unix stdlib imports (`pty`, `fcntl`, `termios`, `tty`, `select`) are unconditional top-level imports in `laintas_cli.py`.

`SubTerminalSession` inside tmux spawns a new tmux window (`tmux new-window -d`) so interactive programs (`vim`, `claude`, REPLs) get native passthrough while the AI loop keeps running in the main pane. Outside tmux it degrades to a backgrounded `InteractiveSession`.

### Terminal ownership — one hard rule

**Nothing outside `terminal_arbiter.py` may call `termios.tcsetattr` on stdin, or read fd 0 directly.** Take the terminal with `terminal_arbiter.hold(owner, Mode.X)` and read parsed keys off the returned session. Read `terminal_arbiter.py`'s module docstring before touching any input code — it explains the two bug classes the rule exists to prevent (stale mode snapshots, and two readers tearing one escape sequence in half), both of which shipped and both of which presented as intermittent freezes that no amount of local locking fixed.

| Mode | Use for | ISIG |
|---|---|---|
| `COOKED` | canonical line input | on |
| `CBREAK` | per-key prompts, approvals, the run's background reader | **on** |
| `RAW` | full byte passthrough only (sub-terminal takeover) | off |
| `EXTERNAL` | prompt_toolkit, or a forked child on the inherited terminal — arbiter stops reading, still restores afterwards | n/a |

Two consequences worth knowing before you "fix" something:

- **A prompt must never run in `RAW`.** Raw disables ISIG, so a prompt that stops consuming input becomes a prompt the user cannot Ctrl+C out of. In `CBREAK` the kernel turns Ctrl+C into SIGINT before any Python code is involved, which is why `_read_single_key_choice` reads Esc but never `\x03`.
- **Modes are computed from `PRISTINE`, never from `tcgetattr` at the call site.** `PRISTINE` is captured at import in `laintas_cli.py`, before anything can dirty it. Restoring from a local snapshot is what let a mode taken during someone else's cbreak get written back minutes later.

The exception, and it is the only one: `InteractiveSession` configures its *own pty slave's* attrs in the forked child. That is the child's terminal, not ours.

### Interrupting a run

Esc soft-interrupts; only a double Ctrl+C force-exits. Esc sets the loop's interrupt event directly and never raises a signal, so it cannot kill the CLI.

For Esc to be honoured promptly the blocking work must have a checkpoint. Backend I/O gets one from `_post_with_interrupt` and `_iter_lines_interruptible`, which move the socket read onto a worker thread — without them a provider that buffers its reasoning gave Esc nothing to land on for the entire thinking phase. **Any new blocking call on the run's critical path needs the same treatment**, or it silently reintroduces a window where Esc does nothing.

### Terminal & Agent Registries

Named sub-terminals (`/term new <name> <cmd>`) and sub-agents survive across loop iterations. `close_all_terminals()` / `close_all_agents()` are wired to SIGINT, SIGTERM, `/exit`, and normal shutdown — when adding new resources that own subprocesses, hook them into the same cascade.

The agent registry now supports tree hierarchies: parent/child links via `spawn_subagent()`, per-agent inbox queues (thread-safe, `queue.Queue`), `abort_agent()` with threading events, and `wait_for_agent()`. Registry mutations are protected by `_registry_lock` (`threading.RLock`).

### Runtime Configuration

All tunable parameters are accessible via `get_runtime_config()`/`set_runtime_config()` and modifiable at runtime via `/config`:

| Key | Default | Description |
|---|---|---|
| `max_loops` | 30 | Max AI loop iterations per task |
| `max_tokens` | 0 | Output-token cap to REQUEST. 0 = take the model's full budget (the gateway grants `min(provider ceiling, context window - prompt)`). A positive value only lowers that, never raises it |
| `max_debug_entries` | 50 | Debug ring buffer size |
| `loop_delay` | 0.2 | Seconds between loop iterations |
| `output_truncate` | 3000 | Char limit for `lastOutput` tail |
| `terminal_tail_lines` | 20 | Lines shown in sub-terminal snapshot |
| `heartbeat_interval` | 30 | Seconds between agent heartbeats |
| `staleness_limit` | 3 | Consecutive no-command steps before auto-exit |
| `search_engine` | `auto` | Engine chain: `auto`, or an ordered list (`"cn-bing duckduckgo"`). Validated against the live registry, not a fixed enum |
| `search_laintas_api_key` | _(none)_ | API key for `search.laintas.com` (required for laintas_search engine) |
| `search_laintas_api_url` | `https://search.laintas.com` | Base URL for laintas_search API |
| `search_proxy` | _(none)_ | Proxy URL shared by web.search, web.fetch **and the headless browser** (`socks5://`, `http://`); env `LAINTAS_HTTP_PROXY` |
| `search_proxy_mode` | `auto` | `off` / `auto` (direct first, proxy only for hosts that proved unreachable) / `always` |
| `search_cookie_enabled` | `false` | Persist challenge cookies across search, fetch and the browser (`~/.laintas/cookies.json`) |
| `identity_enabled` | `false` | Allow `web.fetch` to browse as a saved login. **Separate switch on purpose** — remembering a CAPTCHA is not the same decision as letting fetches act as your signed-in account |
| `search_cookie_domains` | _(all)_ | Allowlist of domains whose cookies may be stored |
| `fetch_render` | `auto` | When web.fetch may render in the browser: `off` / `auto` (blocked or client-rendered) / `always` |
| `fetch_unlock` | `true` | Leave the browser open on a surviving challenge so the user can solve it in the live view |
| `fetch_wayback` | `true` | Fall back to a Wayback Machine snapshot when the live page can't be read |

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
| `~/.laintas/agents/` | `agent_persistence.py` | Employee profiles, tool policies, assignment history, state, and chat history |
| `~/.laintas/skills/` | `skills.py` | User-installed skill directories |

All paths are centralized in `paths.py`. On first launch, `migrate.py` auto-migrates any old `~/.laintas_cli_*` files to the new layout.

## Conventions

- Working directory commits should never include the `.laintas/` project directory — `.gitignore` already excludes it.
- Runtime-tunable knobs live in `agent_loop._runtime_config`. To add one: extend the `_DEFAULT_CONFIG` dict, and it becomes settable via `/config <key> <value>` automatically.
- Debug captures: wrap any agent-touching change with a `DebugEntry` write so it shows up in `/debug` TUI.
- When adding remote-protocol features, follow `HELPWO_INTEGRATION_PLAN.md`: every event must carry `reqId`, and each request has **exactly one** `final` event.
- New tools should register via `tools.ToolRegistry.register()` with a proper JSONSchema — they become automatically available to the AI agent. Use `source=` tags to enable bulk unregistration (`"builtin"`, `"skill:<name>"`, `"mcp:<server>"`).
- User-config files in `~/` that are mtime-cached should follow the pattern in `hooks.py` or `policy.py`: load on demand, cache mtime, re-read when stale.
