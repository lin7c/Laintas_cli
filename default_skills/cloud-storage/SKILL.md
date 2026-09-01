---
name: cloud-storage
description: Laintas Storage - the cloud folder shared with the user's Helpwo sessions. How to list, read back, upload and hand over files that must outlive this process or reach the user on another machine, and how the allowance is spent. Load when a task involves the cloud folder, an upload or download, a file handed to Helpwo, or a storage quota question; not needed for ordinary local files.
version: 1.0.0
triggers:
  - cloud storage
  - laintas storage
  - upload the file
  - download the file
  - shared folder
  - send it to helpwo
  - storage quota
  - how much space
---

# Laintas Storage

One folder per Laintas account, on the account — not on this machine. The user
sees the same folder three ways: `/shared` in this CLI, the "Laintas Storage"
mount inside Helpwo, and the web account page. Anything put there is visible to
the user immediately and survives this process, this workspace, and this
machine.

It is not a filesystem you work in. Files are edited locally with `fs.*` and
copied over when they are finished.

## Which tool

| You want | Use |
|---|---|
| see what is there | `storage.list({ path? })` |
| read a stored file | `storage.get({ path, local_path? })`, then `fs.read` |
| publish a finished file | `storage.put({ local_path, path? })` |
| hand files to a Helpwo agent | `file_push({ paths, target_agent_id })` |
| room left / per-file limit | `storage.usage()` |
| organise | `storage.mkdir`, `storage.move` |
| remove | `storage.delete` — asks the user every time |

`storage.put` publishes to the folder. `file_push` uploads *and* notifies one
Helpwo agent so it pulls the files into its workspace — use it only when a
specific agent is waiting for them; it needs that agent's id.

## Paths

Remote paths are relative, `/`-separated, no `.` or `..` segments:
`reports/2026-09/summary.md`. There is no working directory on the remote side,
so every call names a full path. `storage.get` writes to the working directory
under the remote file's own name unless you say otherwise; pointing
`local_path` at a directory keeps that name.

## What it costs

`storage.usage()` reports the tier, what is already used, the per-file
ceiling, and the free allowance and cap **as the account's plan reports them**
— some plans report no fixed cap (`0`), which means pay-per-use, not unlimited.
Overage is billed monthly. So:

- **Ask before a large upload.** Check `storage.usage()` first when a file is
  big or when you are about to push several: `max_file_bytes` is a hard ceiling
  and an upload past the allowance is rejected by the gateway, not silently
  truncated.
- **Do not use it as scratch space.** Build artifacts, logs and intermediate
  files belong in the workspace. Upload the thing the user asked for.
- **Do not mirror a repository into it.** That is what git is for.

A download is a local write: `storage.get` goes through the same approval and
contract-scope gate as `fs.write`, so in enforce mode the user is asked before
the file lands, and a contract-scoped child cannot drop it outside its scope.

## Deleting

`storage.delete` asks the user in every policy mode, including modes where
ordinary writes are auto-approved. That is deliberate: this store is shared
with Helpwo, the local checkpoint the loop takes before a mutation does not
cover it, and there is no undo. Expect the call to block, and never loop on a
denial — a refusal is an answer.

## When not to reach for it

- A file the user will read in this terminal: leave it local and say where it is.
- Passing data between agents in this process: use the agent tools, not a
  round trip through the network.
- Secrets: this folder is shared with every session on the account, and
  anything with a credential in it should not be here at all.
