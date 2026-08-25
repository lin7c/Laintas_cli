# Blindpick

Blindpick runs the same task with the current model and a challenger in separate Git worktrees. You compare the completed results without seeing model identities, then apply only the selected side. Decisions accumulate into ratings and exportable preference data.

## Commands

```text
/blindpick                         Open the full-screen arena in a TTY
/blindpick challenger              Choose a challenger
/blindpick run <task>              Start a background round
/blindpick show [id]               Show both pending results
/blindpick pick a|b|tie|bad [id]   Apply a side, record a tie, or reject both
/blindpick discard [id]            Discard a pending round
/blindpick delete <id>             Delete a round and its resources
/blindpick prune [N|--all]         Apply retention cleanup
/blindpick status                  Show text status
/blindpick ratings                 Show ratings and position bias
/blindpick export [path]           Export DPO preference pairs
/blindpick reset                   Clear round records when no round is running
```

When several rounds await a decision, commands that omit an id list the candidates instead of silently selecting one.

## Arena

The full-screen arena shows both competitors running side by side with live tool events. When they finish, each pane switches to the response and change summary. Use `a`, `b`, `t`, or `x` to decide; identities are revealed only afterward. Use `d` to switch between response and diff, `n` to start another round, `c` to change the challenger, `v` for ratings, and press `D` twice to delete a round.

Approval requests appear inside the arena and are answered with `y` or `n`. The UI reads the same per-agent event stream used by `/agents`; it does not execute work itself.

## Round lifecycle

1. A/B placement is randomized and persisted before execution so live labels cannot reveal identity.
2. The extension creates two isolated worktrees, copies uncommitted work, and records a baseline commit.
3. It spawns two agents with model and provider overrides applied before their threads start.
4. Each result is committed independently. The candidate diff is exactly `baseline..result`; `.laintas/` runtime state is excluded.
5. A selected result is applied with `git apply`. A conflict fails cleanly without partially modifying the workspace.
6. Selected worktree resources are removed. The losing branch is preserved for inspection after an applied result.
7. The append-only match and vote ledgers record the decision for ratings and export.

## Ratings and export

`ratings` uses a Bradley-Terry model displayed on an Elo scale. It also reports first-position win rate to reveal layout bias. `export` writes judged decisive rounds as DPO preference pairs with run identifiers that can reconnect them to gateway training records.

Data defaults to `<cwd>/.laintas/blindpick/`. Set `data_dir` in `~/.laintas/blindpick.json` to aggregate decisions elsewhere.

## Retention

Finished rounds outside the configured window are cleaned automatically with their worktrees and branches. Defaults are 20 rounds and 14 days; set `keep_rounds` or `keep_days` to `0` for no limit. Running and pending rounds are never pruned. Unreferenced `laintas/blindpick-*` branches older than 24 hours are eligible for cleanup unless a worktree still uses them.

Cleanup reclaims worktrees and branches before deleting the record so interrupted cleanup remains recoverable. The append-only decision ledger is not pruned.

## State

- `<cwd>/.laintas/blindpick_state.json`: challenger and retention settings.
- `<cwd>/.laintas/blindpick_rounds.jsonl`: operational round state.
- `<cwd>/.laintas/blindpick/matches.jsonl` and `votes.jsonl`: append-only decision ledger.

One round performs two complete agent runs and therefore costs approximately twice as much as one run.
