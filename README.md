# Laintas CLI

**[Download](https://cli.laintas.com)** · [Docs](https://laintas.com/docs) · [Laintas](https://laintas.com)

Laintas CLI is a Linux-native autonomous AI agent for terminal work. It combines a normal interactive shell with an agent loop that can understand natural-language tasks, inspect the current workspace, execute commands through a real PTY, and continue from command output.

It is designed for developers, server operators, and remote workspaces that need an AI assistant close to the filesystem and terminal rather than inside a separate chat window.

## What It Does

- Accepts direct shell commands without routing them through the model.
- Turns natural-language requests into an iterative plan, command execution, and feedback loop.
- Runs commands in PTY-backed sessions so interactive tools, colors, prompts, and long-running processes behave like normal terminal programs.
- Maintains named sub-terminals that can be created, inspected, sent input, and closed by the user or agent.
- Spawns sub-agents for parallel work, sequential pipelines, and multi-agent orchestration (HWO/HWG workflows).
- Connects a local CLI agent to Laintas, Helpwo, or a custom backend (including self-hosted Enterprise Gateway) for authenticated remote messaging and browser-to-local workflows.
- Manages extensions: official signed extensions via `/v`, community/custom extensions via `/extensions` with a trust gate.
- Integrates MCP (Model Context Protocol) servers as external tool sources.
- Provides browser automation with headless Chrome, live-view relay, and CDP.
- Stores project memory, prompt rules, plans, debug entries, and workflow state locally across sessions.
- Supports skills (in-process Python plugins), custom slash commands, prompt experiments, and declarative agent roles.

## How It Works

```text
Terminal input
    |
    +-- PATH command --------------------> direct PTY execution
    |
    +-- natural-language task -----------> agent loop
                                             |
                                             +-- backend request (Laintas / custom / enterprise gateway)
                                             +-- tool execution (built-in / skill / MCP / extension)
                                             +-- command execution
                                             +-- output feedback
                                             +-- next iteration
```

The CLI keeps direct commands local. Natural-language tasks use the configured backend, receive structured responses, execute approved commands locally, and send the results back for the next step. This preserves a normal shell workflow while adding an autonomous execution path.

## Main Components

- `laintas_cli.py`: REPL, command routing, PTY sessions, authentication, remote agent registration, and slash commands.
- `agent_loop.py`: iterative task execution, runtime configuration, debug records, and terminal coordination.
- `tools.py`: local filesystem, process, browser, network, and workflow tools exposed to the agent.
- `symbols.py`: centralized UI symbol constants for consistent terminal display.
- `skills.py`: progressive skill discovery, loading, and unloading with capability controls.
- `mcp_client.py`: MCP server management, bridging async MCP SDK to the synchronous Tool registry.
- `extension_runtime.py`: extension loading, trust gate, command metadata, and lifecycle management.
- `extension_manager.py`: generic extension lifecycle (install, uninstall, list, trust, create, pack).
- `enterprise_installer.py`: signed Enterprise extension download, verification, and installation.
- `backend_profiles.py`: backend trust domains and credential isolation (official / custom / local).
- `browser_session.py`: headless Chrome automation with CDP, live-view VNC relay, and snapshot tools.
- `memory_system.py`: persistent typed memory (user, feedback, project, reference) across sessions.
- `workgraph.py`: persistent plans, steps, approvals, dependencies, and event history.
- `hwo_runner.py` / `hwg_runner.py`: multi-agent workflow orchestration (HWO) and durable graphs (HWG).
- `policy.py`: layered security policy with org-policy contributors and config cache.
- `trust_store.py`: extension hash trust store for community extensions.
- `.cli.prop`: project-level prompt and operating rules.
- `.helpwo`: project memory and working context.

## Common Workflows

Start the agent:

```bash
laintas-cli
```

Useful commands:

| Command | Purpose |
|---|---|
| `/help` | Show available commands (including extension commands) |
| `/login` | Re-authenticate with Laintas |
| `/mode` | Switch between plan, review, act, study, and auto modes |
| `/plan` | Create, revise, submit, or approve a versioned plan |
| `/task` | Track project tasks and dependencies |
| `/term` | Manage named sub-terminals |
| `/spawn` | Spawn a sub-agent for a delegated task |
| `/hwo` | Open or run a multi-agent orchestration workflow |
| `/hwg` | Compile, run, or resume a durable HWO graph workflow |
| `/skill` | Manage skills (list, load, unload) |
| `/mcp` | Manage MCP servers |
| `/extensions` | Install, manage, and publish community extensions |
| `/v` | Check for updates; install Enterprise extension |
| `/backend` | Manage backend trust profiles (custom gateways) |
| `/org` | Organisation commands (provided by Enterprise extension) |
| `/helpwo` | Connect to Helpwo as a runtime environment |
| `/memory` | Manage persistent memory across sessions |
| `/rule` | Persist recurring user rules and constraints |
| `/model` | List or select a deployed terminal model override |
| `/config` | View or set runtime configuration |
| `/policy` | Show or set security policy |
| `/web` | Inspect web search and fetch engines, proxy, cookies |
| `/identity` | Manage saved logins the agent may browse as |
| `/reload` | Reload project defaults and extensions |

## Backends

The CLI sends inference requests to a configured backend. Three trust domains are supported:

- **Official** (`https://laintas.com`): uses the Laintas session for authentication and billing.
- **Custom** (any HTTPS URL): uses a separate token (`LAINTAS_CUSTOM_BACKEND_TOKEN`), unmetered by Laintas. Configure with `/backend config` or `LAINTAS_BACKEND`.
- **Local** (`127.0.0.1` / `localhost`): no authentication, for development.

Credentials never cross domains: Laintas session cookies are sent only to official origins, and custom tokens are never sent to laintas.com.

## Extensions

Extensions are runtime modules loaded into the CLI process. Two installation paths exist:

- **`/v`** — official signed extensions (Ed25519). Enterprise is installed this way via `/v enterprise`.
- **`/extensions`** — community and custom extensions. Supports local directories, `.lext` packages, and URLs. Requires explicit trust approval (hash-based) before loading.

The trust model has three tiers:

| Tier | Mechanism | Auto-trusted |
|---|---|---|
| Ed25519 signature | `/v` official installer | Yes |
| Hash approval | `/extensions trust` | No (manual) |
| Evolution Lab | `/evolve` activation | Yes |

Legacy extensions without an `install` block in their manifest are allowed through for backward compatibility.

## Platform Scope

The standalone release targets Linux 64-bit systems:

- `x86_64` / `amd64`: Intel and AMD 64-bit systems
- `aarch64` / `arm64`: 64-bit ARM servers and development boards
- glibc 2.28 or newer

The current standalone release does not support `i686`/`i386` 32-bit x86 systems, 32-bit ARM, or musl-based Alpine binaries. Those environments should use the source package if a compatible Python runtime and dependencies are available. See the [download page](https://cli.laintas.com) for the compatibility matrix and installation checks.

## Installation

The installer detects `x86_64` or `aarch64` and downloads the matching package:

```bash
curl -fsSL https://cli.laintas.com/install.sh | bash
```

Check a machine before installing:

```bash
uname -m
getconf LONG_BIT
ldd --version
```

For manual downloads, verify the binary with `file` and compare the archive against `SHA256SUMS.txt` from the release.

## Source Installation

Source installation is useful for unsupported native targets, development, and auditing:

```bash
unzip laintas-cli_source.zip
cd laintas-cli-source
python3 -m pip install -r requirements.txt
python3 laintas_cli.py
```

Optional dependencies for browser automation (`playwright`, `websockets`), MCP (`mcp`), WebRTC (`aiortc`), and web search (`trafilatura`, `curl_cffi`) are listed in `requirements.txt` under extras.

## Development

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
venv/bin/python -m pytest
```

The download page is a separate Vite application:

```bash
cd laintas_cli_download
npm install
npm run dev
```

## Releases

`.github/workflows/release.yml` builds and publishes:

- Linux amd64 standalone binary
- Linux arm64 standalone binary
- Linux amd64 Debian package
- Linux-compatible source package
- SHA256 checksums and source update manifests

Release assets use architecture-specific names. Do not rename them to a generic Linux archive, because an ELF binary can only run on a compatible CPU architecture.

Self-hosted update assets (for `/v update`) are mirrored to `cli.laintas.com/releases/latest/` via `scripts/build_release_assets.py`.

## License

MIT. See `LICENSE` when present in the distribution.

## Contributors

Laintas is maintained by its open-source contributors. Parts of the release packaging and documentation workflow were developed with OpenAI Codex assistance.
