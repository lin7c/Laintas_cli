# Customization security model

## Customization inventory

| Surface | User-controlled content | Execution risk | Practical role |
| --- | --- | --- | --- |
| Backend profile | URL and separate auth reference | Network/data disclosure | Use official, self-hosted, or local model gateways |
| `cli.prop` | Prompt text | Prompt injection, no direct code execution | Project-specific agent behavior |
| `modes.json` | Prompt text and tool allowlist | Prompt injection, no direct code execution | Switchable project behavior modes |
| Project memory | JSON/Markdown data | Prompt/data poisoning | Persistent project context |
| `commands.py`, `loop.py` | Python | Full local-user code execution | Project commands and loop interception |
| Skill | Python, manifest, references | Full local-user code execution | Reusable tools and instructions |
| MCP | Process command, arguments, environment | Child process plus declared tool effects | External tool/server integration |
| Hooks | Python or process argv | Full local-user/process execution | Policy, logging, and automation |

Declarative customization remains frictionless. Executable customization is
hash-approved so that cloning a repository or editing an approved file cannot
silently introduce code execution. This is intentionally more usable than a
global on/off switch: approvals are scoped to a project or extension and are
invalidated only when executable content changes.

Custom mode tool lists are restrictive only: they are intersected with Plan
Mode, workflow, role and global policy decisions and cannot grant a capability
that another layer denies. The built-in `/policy` setting remains independent
from agent behavior modes.

## Trust domains

Laintas CLI has three backend trust domains:

- `official`: exact HTTPS origins owned by Laintas. The verified Laintas
  session may be sent and usage is expected to be metered by the official
  service.
- `custom`: an arbitrary remote compatible backend. It receives only its own
  configured credential and is labelled external/unmetered.
- `local`: a loopback compatible backend. It never receives the Laintas
  session and does not require Laintas login.

Setting `LAINTAS_BACKEND` or `--backend` selects a legacy custom/local profile
unless the URL has an exact official origin. A config entry cannot promote an
arbitrary origin to `official`. Redirects are not followed for authenticated
backend requests.

Profiles live in `~/.laintas/backends.json`. Custom credentials should use an
`env:VARIABLE` or `keyring:service/user` auth reference; they must never be put
in a backend URL.

## Project trust

`cli.prop` and project memory are declarative and remain available in
restricted mode. `.laintas/commands.py` and `.laintas/loop.py` are executable
Python and run only when either:

1. they are byte-identical generated defaults recorded by the CLI, or
2. `/trust allow` approved the exact current hashes.

Any executable-file change invalidates project trust. Symlink entrypoints are
always rejected. Use `/trust status` and `/trust revoke` to inspect or remove
approval.

## Skills, MCP, and hooks

Executable skills require:

- `/skill trust <name>` approval for the exact `skill.py` and
  `extension.json` hashes;
- an `extension.json` manifest declaring capabilities;
- the `skill.<name>.*` namespace.

Skill registration is transactional: every tool name and capability is
validated before any tool becomes visible. A failed load leaves no partial
registration. Capability declarations support review, collision prevention,
and auditing; they cannot sandbox arbitrary trusted Python, which can call OS
APIs directly.

MCP servers require `/mcp trust <name>` for the current config hash. Their
tools use `mcp.<server>.*`, cannot replace builtins, and receive a minimal
environment plus only explicitly configured variables.

Python hooks require `/hooks trust`. Configured process hooks execute argv
directly with `shell=False`; event context is JSON on stdin. Pre-hook failures
configured with `block_on_failure` fail closed.

## Evolution Lab extensions

`/evolve` is the executable counterpart to `/prompt`: it supports CREATE,
IMPROVE and REPAIR branches, candidate review, baseline/candidate subprocess
tests, profiles, explicit activation, hot reload, disable and rollback. Its
temporary test environment is a reliability boundary rather than a hostile-code
sandbox; activated extensions are intentionally expressive local Python.

ExtensionContext does not contain the Laintas session or cookies. Integrated
model calls use `ctx.backend.chat()`, which follows the selected backend profile
and the normal authentication/billing path. Arbitrary local Python can still
use a user's own external model service. Protection of official paid inference
is therefore enforced by server-side authentication, model authorization and
the billing ledger, never by trusting extension code.

## Tool registry invariants

- Builtin tool names are reserved permanently.
- Extension registration cannot overwrite a builtin or another tool.
- Untrusted extension tools cannot be invoked.
- Tools carry source, trust level, and capability metadata in the audit log.
- Prompt text is not a security boundary; runtime policy remains authoritative.

Extension invocation does not receive the authenticated session or agent
control callbacks through `ToolCtx`. This reduces accidental credential and
control-plane exposure, but it does not stop trusted Python from reading files
available to the current OS account.

## Operational recommendations

1. Keep normal projects declarative; add executable files only when required.
2. Review the displayed path, SHA-256, and capabilities before trusting.
3. Revoke trust when an extension or workspace is no longer in use.
4. Put custom-backend secrets in a dedicated environment variable or keyring,
   never in a URL, project file, prompt, or Laintas session field.
5. Run third-party Skills, MCP servers, and hooks under a restricted account or
   container when their provenance is uncertain.
6. Treat `external/unmetered` as a trust-domain label, not proof that the
   external provider is free or private.

## Billing boundary

The client cannot enforce payment against a user who replaces the client or
uses their own model. It can and does prevent official credentials from being
forwarded to that alternative service. Preventing unpaid use of official model
resources is exclusively a server responsibility.

The official backend must:

1. derive identity from the authenticated session, never a client `userId`;
2. authorize model/provider selection server-side;
3. calculate cost from provider-confirmed usage;
4. reserve and settle balance in an idempotent server ledger;
5. associate retries and stream interruption with one billing request;
6. never trust client prices, usage, balance, or billing events;
7. return a server receipt if the UI needs verifiable billing display.

This repository does not contain that backend ledger. Client tests therefore
verify credential isolation and billing-domain labelling, not server charging.

## Remaining operating-system boundary

Code explicitly trusted by the local user still executes with that user's OS
permissions. Workspace and extension trust prevents silent execution; it is
not a kernel sandbox. Run third-party executable extensions in a container or
restricted OS account when stronger isolation is required.
