---
name: app-hosting
description: Host an application in its own sub-terminal with its own dedicated agent (/app), from a manifest the user places and trusts.
version: 1.0.0
triggers:
  - hosted app
  - host the application
  - app manifest
  - /app
  - hosted application
---

# App Hosting (/app)

A hosted application runs the way Helpwo does: in its own named sub-terminal,
with its own dedicated agent whose only job is that application. A loopback
bridge exposes nothing but the conversation (send chat / abort / approval
response / poll updates) — never the CLI's disk or shell. Helpwo itself is the
built-in instance of this mechanism (`/helpwo`), not a manifest app.

## The workflow you guide

1. **Draft the manifest** with the user. Fields:
   - `name` — must match `^[a-z0-9][a-z0-9._-]{0,31}$`; `helpwo` and `term0` are reserved.
   - `description` — short human-readable purpose.
   - `command` — how the application is started (run with `LAINTAS_APP_BRIDGE_URL`).
   - `prompt` — the dedicated agent's role.
   - `persistence` — `"none"` (fresh every start) or `"workspace"` (login, data and conversation persist per folder).
   - `port` — optional fixed port; otherwise one is assigned.
   - `session_tools` — tools the app's session agents may use. Only a whitelist
     is allowed (`shell.exec`, `web.search`, `web.fetch`, `image.describe`,
     `image.to_text`, `media.generate_image`, `media.generate_video`, `sleep`);
     default `["shell.exec"]`. Do not widen this without a reason.
   - `auto_approve` (default false), `max_sessions` (default 10, max 200),
     `session_idle_minutes` (default 30).
2. **The user places the file** at `~/.laintas/apps/<name>.json` (global) or
   `./.laintas/apps/<name>.json` (project; shadows a user manifest of the same
   name). Never write this file yourself: the file's human placement is the root
   of the trust chain.
3. **Discover**: `app.list` shows registered apps, trust state, running state,
   and any broken manifests (`problems`) you can help fix.
4. **Trust**: read the manifest with `app.manifest.get`, summarise for the user
   what its session agents may run, then call `app.trust.request` with that
   summary as `note`. Nothing is recorded unless the user approves. Trust
   follows the manifest digest: after any manifest change, trust must be
   requested again.
5. **Run**: `app.start` (asks approval) starts the sub-terminal and its agent;
   `app.list` shows running state. `app.stop` (asks approval) closes the
   sub-terminal, agent, and process together.

## Hard rules

- Manifest files are placed by the user, never by you.
- Trust is a human decision: `app.trust.request` asks; it never records on its own.
- Do not revoke trust for the user — that is the user's `/app revoke`.
- Keep `session_tools` minimal; explain the implication of each addition.

## User-side commands (mention them, do not run them)

`/app list`, `/app trust <name>`, `/app start <name>`, `/app stop <name>`,
`/app revoke <name>` — the same operations from the keyboard, without an agent
in the middle.
