# Code Map

Laintas Code Map builds a layered architecture map of a **public GitHub
repository** on the server: what the system does, what each part is made of,
and the real declarations with their `file:line`. A build runs for minutes to
hours and is billed to your account; reading a finished map is free.

This maps a remote repository, not your working directory. For code already
checked out locally, read the files.

## Install

```text
/extensions install extensions/code-map --global
```

Installed means enabled: the `code_map.*` tools are in front of the model from
the next session on, so a "how is this repository put together" question
starts by asking the map instead of reading files to find out. Uninstall and
they are simply absent — there is nothing for the model to probe and nothing
to explain, and code reading falls back to `grep`/`read`.

## Commands

```text
/codemap                                   The account's maps and free slots
/codemap build <url> [ref] [--model <id>]  Queue a build, print its id
/codemap status <id>                       Progress of one build
/codemap read <id> [node]                  The finished map as text
/codemap delete <id>                       Free the slot a map holds
```

`build` never waits: a terminal that sat for an hour on a server-side build is
a terminal you cannot use. It queues the job and prints the id; `status` and
`read` collect it later.

## What ships with it

Everything Code Map needs lives in this directory — nothing about it remains
in the CLI core:

| File | What it is |
|---|---|
| `main.py` | The five tools and the `/codemap` command |
| `client.py` | The HTTP client for `/api/agent/code-map` |
| `skills/code-map/SKILL.md` | The method, registered with `ctx.register_skills()` |

The skill matters as much as the tools. Guidance about `code_map.*` sitting in
a bundled skill would keep describing them after the extension was removed —
prose the model cannot tell apart from a capability it actually has. Here, the
method appears when the extension loads and goes when it unloads, and the
trust hash that approves the code covers it too.

## Tools

| Tool | What it answers |
|---|---|
| `code_map.list` | Which repositories already have a finished map, and how many slots remain |
| `code_map.read` | The whole system in ~15 KB, or one part (`node='l1:<id>'`), or one component's declarations (`node='l2:<part>:<component>'`) |
| `code_map.build` | Queue a map of a repository that has none |
| `code_map.status` | How far a queued build has got |
| `code_map.delete` | Free a slot; a map costs model calls to rebuild |

Read before you build: an existing map costs nothing, a build costs money and
hours.

## Quotas

Free accounts keep 2 maps, memberships 4, and more at $1 a month each.
`/codemap` prints the current usage under the list.
