# Laintas CLI

Laintas CLI is a Linux-native autonomous AI agent for terminal work. It combines a normal interactive shell with an agent loop that can understand natural-language tasks, inspect the current workspace, execute commands through a real PTY, and continue from command output.

It is designed for developers, server operators, and remote workspaces that need an AI assistant close to the filesystem and terminal rather than inside a separate chat window.

## What It Does

- Accepts direct shell commands without routing them through the model.
- Turns natural-language requests into an iterative plan, command execution, and feedback loop.
- Runs commands in PTY-backed sessions so interactive tools, colors, prompts, and long-running processes behave like normal terminal programs.
- Maintains named sub-terminals that can be created, inspected, sent input, and closed by the user or agent.
- Connects a local CLI agent to Laintas and Helpwo for authenticated remote messaging and browser-to-local workflows.
- Stores project memory, prompt rules, plans, debug entries, and workflow state locally.
- Supports project extensions, custom slash commands, prompt experiments, and declarative agent roles.

## How It Works

```text
Terminal input
    |
    +-- PATH command --------------------> direct PTY execution
    |
    +-- natural-language task -----------> agent loop
                                             |
                                             +-- backend request
                                             +-- command execution
                                             +-- output feedback
                                             +-- next iteration
```

The CLI keeps direct commands local. Natural-language tasks use the configured Laintas/Helpwo backend, receive structured responses, execute approved commands locally, and send the results back for the next step. This preserves a normal shell workflow while adding an autonomous execution path.

## Main Components

- `laintas_cli.py`: REPL, command routing, PTY sessions, authentication, remote agent registration, and slash commands.
- `agent_loop.py`: iterative task execution, runtime configuration, debug records, and terminal coordination.
- `tools.py`: local filesystem, process, browser, network, and workflow tools exposed to the agent.
- `workgraph.py`: persistent plans, steps, approvals, dependencies, and event history.
- `skills.py`: project and user skill discovery with explicit capability controls.
- `.cli.prop`: project-level prompt and operating rules.
- `.helpwo`: project memory and working context.

## Common Workflows

Start the agent:

```bash
laintas-cli
```

Useful commands include:

| Command | Purpose |
|---|---|
| `/help` | Show available commands |
| `/login` | Re-authenticate with Laintas |
| `/term` | Manage named sub-terminals |
| `/agents` | Inspect registered agents and employees |
| `/mode` | Switch between plan, review, and act modes |
| `/plan` | Create, revise, submit, or approve a plan |
| `/task` | Inspect and update the active work graph |
| `/debug` | Inspect recent model and command events |
| `/config` | View or change runtime settings |
| `/reload` | Reload project defaults and extensions |

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

Optional integrations such as browser automation, MCP, and WebRTC require their corresponding Python dependencies and system packages.

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

## License

MIT. See `LICENSE` when present in the distribution.

## Contributors

Laintas is maintained by its open-source contributors. Parts of the release packaging and documentation workflow were developed with OpenAI Codex assistance.
