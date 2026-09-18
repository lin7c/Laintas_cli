---
name: app-hosting
description: Create, configure, start, update and stop hosted applications with dedicated agents and terminals through /app tools.
version: 2.0.0
triggers:
  - hosted app
  - host the application
  - app manifest
  - /app
  - hosted application
---

# App Hosting (/app)

Complete the user's hosting task with tools. Write the manifest yourself with
`app.manifest.put`; do not hand file placement back to the user. Each app runs
in a nested CLI with its own primary agent. Helpwo uses `/helpwo`, not a manifest.

## Workflow

1. Inspect the application and `app.list`; use `app.manifest.get` before updating
   an existing app so unrelated settings survive.
2. Write a valid manifest using `app.manifest.put` (project scope by default,
   user scope when requested). Fix validation errors before launching.
3. Call `app.start`. If the manifest is new or changed, this requests trust via
   the normal approval policy. Existing trust covers later starts and stops.
   `app.trust.request` is also available for a separate review. Do not edit the
   trust store or bypass a refusal.
4. Verify the returned runtime status and application behavior, including a
   bridge chat and any configured agents/terminals. A successful manifest write
   alone is not a running app. Inspect the returned log when startup fails.
5. For updates, write the manifest, then `app.stop` and `app.start` to apply it.
   Stop only apps within the user's request. `app.trust.revoke` revokes future
   starts; it does not stop a running app.

## Manifest

```json
{
  "name": "notes",
  "description": "Notes application",
  "command": "node server.js",
  "app_url": "http://127.0.0.1:3000",
  "prompt": "Maintain and operate the notes application; verify completed work.",
  "persistence": "workspace",
  "auto_approve": true,
  "session_tools": ["*"],
  "agent": {"capability_tags": ["coordination"]},
  "terminals": [{"name": "worker-shell", "cwd": "."}],
  "agents": [{
    "name": "worker",
    "parent": "primary",
    "terminal": "worker-shell",
    "prompt": "Implement and test assigned application changes.",
    "capability_tags": ["coding", "testing"],
    "tools": ["shell.exec", "fs.read", "task.complete"]
  }]
}
```

- `name`: lowercase letters, digits, `.`, `_`, `-`, up to 32 characters;
  `helpwo` and `term0` are reserved.
- `description`, `command`, `prompt`: strings; command and prompt are optional.
- `persistence`: `none` (default) or `workspace`; optional `port`: the bridge
  port (1–65535), not the project's web server port.
- `app_url`: the project's absolute HTTP(S) address, including its actual port
  and optional base path. Read the server configuration/startup log and set it
  for web apps, then verify it responds. This field displays the address; it
  does not configure the server's listener or prove readiness. Startup prints
  Project URL separately from Bridge API and Bridge login. Omit for non-web apps.
- `agent`: primary agent configuration. `agents`: named children, with optional
  `parent` (primary or an earlier entry) and `terminal` (a declared terminal).
  Both accept `prompt`, `profile` (an existing employee role), `title`,
  `description`, `capability_tags`, `model`, `provider`, `tools`, `denied_tools`.
  Omitted model/provider inherit runtime selection; use an available model ID.
  `tools: ["*"]` enables the normal tool catalogue. Explicit arrays restrict
  visibility; denied tools still apply. Omitted child tools inherit their parent's allowed tools unless a role provides its own tool list. Use actual registered names.
- `terminals`: `{name, command?, cwd?}`. Default command is the normal shell;
  cwd is resolved relative to the app workspace (the user's folder in a session).
  Only one agent may occupy a terminal. Unstationed children use temporary
  terminals on assignment. These definitions create resources, not tasks;
  the primary assigns work through the normal agent tools.
- `session_tools`: session primary's fallback tool list; defaults to
  `["shell.exec"]`. Accepts registered tool names or `["*"]`. An explicit
  `agent.tools` overrides it. Each session also instantiates agents/terminals.
- `auto_approve`: true completes approval requests automatically, including
  destructive operations; false (default) emits `needs-approval` and accepts
  `approval-response`. Choose true when the user wants autonomous operation.
  Other runtime policy denials still apply.
- `max_sessions`: 1–200, default 10. `session_idle_minutes`: 0–10080,
  default 30; 0 disables idle shutdown.

## Bridge contract

The app command receives `LAINTAS_APP_BRIDGE_URL`, `LAINTAS_APP_TOKEN`,
`LAINTAS_APP_AGENT_ID`, and `LAINTAS_APP_NAME`. Send
`Authorization: token <token>`; never expose the operator token to untrusted
end users. The bridge has the normal authenticated file, shell, terminal,
proxy and agent APIs. Tool visibility is not an OS sandbox.

`app.start` returns `runtime.url` (the API base), `runtime.token`, and
`runtime.open_url` (a browser login link). The configured project URL is
`runtime.app_url`. The command environment includes
`LAINTAS_APP_BRIDGE_OPEN_URL`. Open that link to establish the browser cookie;
it displays bridge runtime information, not the project's frontend. API clients
must send the token header on every request. Do not use the bare bridge URL as
a login link or append API paths to the login URL containing `?token=`.

POST `/api/agents/<id>/send` with `{kind, reqId, payload}`; poll
GET `/api/agents/<id>/updates`. Supported kinds include `chat`, `abort`,
`approval-response`, `exec`, `query`, `delegate`, and terminal operations.
In interactive mode, answer `needs-approval` with `approval-response` and
`payload.targetReqId` plus `payload.decision` (`approve` or `reject`).

The main app bridge also accepts `session-open`, `session-close`, and
`session-list` with `payload.user`. Session-open returns the user's own bridge
URL, token and agentId. Sessions have separate homes, working directories and
conversations, but run as the same OS user. Session bridges do not open nested
user sessions. Closing the app closes its session processes too.

User REPL equivalents: `/app list`, `/app trust <name>`, `/app start <name>`,
`/app stop <name>`, `/app revoke <name>`. Use tools during agent work.
