# Laintas CLI beta v2 (1.32.5b2)

Laintas CLI beta v2 (package version 1.32.5b2) is now available.

- Type and press Enter to append instructions while an Agent is thinking. Consecutive append messages remain visible as branches, with execution messages kept from interrupting the connecting lines.
- Added account workspaces with separate sessions, settings and runtime data. Account selection is saved per terminal. Use /account switch, or run separate CLIs for concurrent A/B account work.
- Added explicit task handoff and legacy-task adoption. /handoff transfers task context into a receiving account's own continuation session while preserving the source task. Use /account legacy and /account adopt to claim old tasks explicitly.
- Extended /term with a two-level controller/executor relationship. An executor must explicitly accept the controller's invitation before its Agents join the existing parent/child scheduling structure under a terminal prefix. A third terminal level is refused.
- Local pairing supports separate accounts with explicit acceptance. Cross-server pairing requires the same account and compatible Helpwo Kernel/Gateway support. Ordinary local terminal creation does not require remote services and can fall back to a shell if pairing is unavailable.
- Fixed concurrent file locking, uninitialized account paths and writes to the wrong profile. Account switches validate the restart command and save tasks before shutdown. Preparation failures retain the current session; restart failures after shutdown restore account selection and provide recovery instructions.
- Fixed extra autosaves during account switching, missing snapshot ownership diagnostics, and retained task/application launch arguments. Restarts immediately after the first login preserve the original launch task.
- Linux standalone builds install the complete declared core dependency set. Linux and macOS builds now check both --version and --help.

Download and install: https://cli.laintas.com/
Existing users can update with /v update.
