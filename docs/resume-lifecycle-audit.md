# /resume session lifecycle audit

Audit date: 2026-09-17. Scenarios are grouped by session identity, tree structure, runtime state, stored copies, concurrency and failure paths.

## Operational semantics

- `/resume`, `--resume`, `--continue` switch to an existing session and keep the session ID.
- `/fork` creates an independent branch. Repeated `/resume` never adds branch levels.
- "Resuming" a history-snapshot row enters the latest saved state of the session it belongs to, avoiding silent rollback; the detail view still shows the selected historical snapshot.
- Deleting a session node includes all of its snapshots and descendants; deleting a history-snapshot row removes only that snapshot's copies.
- If the current terminal or any other terminal still holds a valid lease on any session in the target tree, the whole delete is refused; switch to a tree outside it or close the occupying terminal first.

## Scenario matrix

| Scenario | Result / current behavior |
| --- | --- |
| First-turn autosave of a new session | Fixed: live, in-memory state, autosave and lease could use different IDs; identity is unified at creation |
| A → B → A | Fixed: every resume used to create a branch; now the latest progress of A is saved before switching, and returning is still A |
| Resume the current session | Notice that it is already the current session; no wipe, no overwrite, no new branch |
| Switch from a resumed branch to another branch | Keep the target ID and original parent/child relation; do not re-parent the target under the current session |
| Startup-flag resume | Uses the same switch function as interactive resume |
| Pick an old snapshot | Prefer re-reading that session's current tip; never overwrite newer progress with the old snapshot |
| Target updated while the picker is open | Re-read from disk at switch time |
| Target deleted while the picker is open | Refuse to resume when the tombstone exists or the file is missing; the current session is preserved |
| Empty session / records from another directory | Refuse to resume; the current lease is not released |
| Target held by another terminal | Refuse to switch; no bypassing the occupancy check by creating a branch |
| Lock directory or file write fails | Fixed: a failure was treated as success; now the resume is refused and the error is shown |
| Resume-state error / failure closing the source session | Keep the source in-memory state and lease, release the target lease, try to restore the source current pointer |
| Old-version snapshot without a session ID | Use the stable snapshot ID instead of minting a new identity on every resume |
| Old-version fork sharing the parent's ID | Use a stable, independent ID for the old branch and keep its parent relation |
| Same-name branches / identical lineage | Fixed: records with different IDs were merged by name; ID now wins explicitly |
| Delete the running root session | Refuse the delete; no "deleted then reappears" entries |
| Delete a root session whose descendant is running | Check the whole tree before deleting any file; refuse the entire delete |
| Delete an unused root session | Delete all snapshots, fork copies, live/current pointers and all descendants |
| Delete a middle branch | Delete that branch and its descendants; keep ancestors and sibling branches |
| Child branch past the picker retention window | Compute the delete scope from the full on-disk inventory, not just the picker's visible rows |
| Delete an old snapshot | Keep newer autosaves and branches; delete all copies of the same snapshot |
| Old nested forks | Detect nesting via old lineage and parent identity; deleting a subtree must not delete the parent session |
| Missing parent node | The existing tree rendering promotes the branch to a visible root; unknown ancestors are never invented or deleted |
| Corrupt parent links forming a cycle | Traversal uses a visited set, terminates, and handles the recognizable connected descendants |
| Lease left by a dead process | Does not block deletion; resume reuses the existing stale-lease recovery |
| Late save / exit checkpoint / live sync / fork after deletion | Persistent tombstones stop old identities from coming back |
| Disk error during deletion | Errors are no longer swallowed into a success message; sessions whose tombstone was written are not revived by remaining copies |
| Two processes deleting, resuming or saving at once | A per-directory lifecycle file lock serializes operations; covered by separate subprocess race tests |
| Record `_path` points outside the directory | Deletion only touches the verified inventory of the given directory; it never blindly deletes paths taken from records |
| Old current pointer of the same terminal found in use by another instance at startup | Fixed: the old checkpoint was written before the occupancy check, overwriting the other side's progress; the order is now correct |
| `--execute --session-id` resuming an occupied session | Fixed: execution continued after failing to grab the lease; the lease is now held for the whole run and released even on exceptions |
| `--execute --session-id` first using a new ID | Acquires the lease before executing too; session protection is no longer bypassed |
| Work items / tasks / workflows after a switch | Live work items are queried by session ID; stale non-session-scoped context is dropped on switch |
| Content fingerprint unchanged but the saved file is gone | Skip the write only when the file still exists, avoiding a false "already saved" |
| | |

## Verification and limits

Added `tests/test_resume_lifecycle.py`; also validated the existing fork-tree, picker-delete, lease, resume-summary, peer-coordination, session-store, runtime and shared-save-function tests. Tests use temporary session directories; subprocesses are awaited or terminated and reaped at test end.

No real models were called and no user sessions were modified for verification. Corrupt JSON that cannot be parsed is never attributed to a guessed session and force-deleted; unidentifiable historical fragments are kept. History event logs and WorkGraph audit records are also outside what `/resume` snapshot deletion may touch. The Windows file-lock branch is implemented, but this run was verified on Linux.

Already-running old CLI processes must be restarted to pick up these protections; this change does not kill user terminals or rewrite existing history trees.
