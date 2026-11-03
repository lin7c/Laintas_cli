# Consistency fixes, 2026-11-03

This change addresses the ten reproduced findings from the code consistency
audit. It preserves existing CLI commands and on-disk contract filenames.

## File access

CLI reads, writes, edits, batch edits and deletes share a canonical per-file
process lock with the local Helpwow filesystem bridge. Reads hold it through
fingerprint recording; writes hold it from the initial read through approval,
commit and fingerprint refresh. Rename and move operations acquire source and
destination locks in a consistent order. Lock failures refuse the operation.
Stable hash slots limit the shared lock metadata to 1,024 sidecars, even when
the tools access many different files; collisions only serialize those accesses.

File writes use a flushed temporary file and atomic replacement, preserve the
existing mode, and follow an existing symlink without replacing the symlink.
Failed replacements remove the temporary file and retain the original bytes.
Formatting finishes before the writer records its new fingerprint.

These locks coordinate participating file tools. External editors and shell
commands do not acquire them; fingerprint checks continue to detect observed
external changes. Persistent lock sidecars must not be deleted while other CLI
processes may hold them.

## Contract transactions

Contract updates hold a per-project process lock across the entire
read/modify/commit operation. A pending commit journal makes replacement of
`openapi.json` and `contract.lock.json` recoverable as a pair. Readers complete
an interrupted commit before exposing the files. The lock and journal live
outside the versioned contract directory.

HTTP verification runs outside the store lock. Its results are merged into the
latest state only when the checked operation, implementation and relevant
schema/server definitions still match the snapshot. Unrelated proposals are
preserved; a changed operation requires verification again.

## Tasks and WorkGraph

WorkGraph transactions reuse the connection within one thread and database,
with savepoints for nested operations. Reads inside a transaction see its
uncommitted changes without committing them. Task updates include child
creation, dependency changes and status changes in one transaction.

Status validation runs after deriving the final status from progress. Setting
progress to 100 cannot bypass the deleted, skipped or blocked transition rules.
Finding or creating an active work item is a single transaction. Schema
initialization is tracked by the database's `user_version`, so replacing a
database at the same path does not reuse stale process-local schema state.
Read connections and failed initialization connections are explicitly closed.

## Execution and deadlines

Concurrent duplicate execution request IDs are rejected before starting a
process. Cleanup matches the request object as well as its ID, preventing a
stale cleanup from removing a newer request. The local proxy retains compressed
payloads together with their `Content-Encoding` header. Detached terminals
check their grace deadline even while output remains continuously readable.

Auto-Pilot passes its configured timeout to the outer timer and passes a
deadline event plus bounded transport timeouts to inference. Expired inference
results are ignored and the ordinary heuristic fallback remains available.

## Shared persistence and validation

Workflow state now uses the existing `file_lock` and `json_store` helpers,
including fsync and temporary-file cleanup, instead of a separate lock registry
and write implementation. Existing string serialization of optional objects is
retained. Redundant `webbrowser` and `Panel` imports were removed.

`tests/test_review_regressions.py` covers concurrent thread and process writers,
bridge/CLI contention, failed commits, task rollback, status invariants,
database replacement, duplicate requests, gzip forwarding, terminal expiry and
inference deadlines. Run these alongside the existing filesystem, contract,
WorkGraph, runtime and workflow-state tests. The full existing suite was also
run; exact results are recorded with the task's verification artifacts.

Large-module decomposition and further host-interface extraction remain
separate architectural work; this change keeps the consistency fixes reviewable.
