---
name: git
description: Working with git on the user's real repository — inspecting state, staging and committing atomically, branching, and staying away from destructive or history-rewriting commands. Load this before any git command that writes.
version: 1.0.0
triggers:
  - commit
  - git
  - stage changes
  - branch
  - revert
  - pull request
---

# Git

There are no git tools. Git runs through `shell.exec` as real commands against
the user's real repository — a repository that usually holds uncommitted work
that is not yours, and whose history other people depend on.

## Before you write anything

- Read first: `git --no-pager status`, `git --no-pager diff`,
  `git --no-pager log --oneline -10`. These are free and always worth running.
- Always use `--no-pager` (or pipe to `cat`). A pager holds the PTY open and the
  command never returns.
- Always use non-interactive git. Never `rebase -i`, `add -i`, `add -p`, or
  anything that opens an editor — pass `-m`, `--no-edit`, or set
  `GIT_EDITOR=true`. An editor in the PTY is a hang, not a prompt.

## You are in someone else's worktree

The worktree may be dirty, and the changes you did not make may belong to the
user or to another agent working concurrently.

- NEVER revert, undo, or modify changes you did not make unless the user
  explicitly asks you to.
- If you are asked to commit and there are unrelated changes in the files you
  touched, do not revert them. Read them carefully and work *with* them.
- If the unexpected changes are in unrelated files, ignore them.
- If they directly conflict with the task you were given, stop and ask the user
  how to proceed. Do not resolve it by discarding their work.

## Committing

- NEVER commit unless the user explicitly asks. The same goes for `push` and
  `tag`. Finishing a code change is not permission to record it.
- **One commit, one logical change.** This is the whole reason a later revert
  is safe: `git revert` pulls out exactly one commit. If that commit also
  carried a rename, a formatting sweep, and an unrelated fix, reverting the bug
  drags all three out with it. Separate behaviour change from refactor from
  formatting, even when you made them in one sitting.
- Stage by explicit path — `git add src/parser.py tests/test_parser.py`. Never
  `git add -A`, `git add .`, or `git add -u`: they sweep in the user's
  unrelated work and entangle it with yours in the same commit.
- Run `git --no-pager diff --staged` and read it before every commit. If it
  contains something that is not part of this change, unstage it
  (`git restore --staged <path>`) rather than committing it.
- Write the message about the change and why it was needed, not about the
  activity. "Fix off-by-one in range slicing" beats "update parser.py".
- Do not amend a commit unless explicitly asked. Do not rebase, squash, or
  otherwise rewrite existing history unless explicitly asked.
- Never commit secrets, credentials, build output, or vendored directories.
  Check the staged diff for them; `.gitignore` is not a guarantee.
- If a pre-commit hook fails, read what it reported and fix it. Do not reach
  for `--no-verify`.

## Destructive commands

Run these ONLY when the user asked for that specific action, in that specific
place — never as a way to get to a clean state:

`git reset --hard` · `git checkout -- <path>` / `git restore <path>` ·
`git clean` · `git push --force` / `-f` · `git branch -D` ·
bare `git stash` (it moves the entire working tree away) ·
`git stash drop` / `clear` · rebase of already-pushed commits.

- `git clean -x` / `-fdx` also deletes gitignored files: `.env`, local config,
  caches. Nothing brings those back — not even the session checkpoint below,
  which excludes gitignored files by design.
- The commands listed above always stop for a fresh confirmation, in every
  policy mode. Everything *else* — `commit`, `push`, `rebase`, `merge`,
  `switch`, `cherry-pick` — is advisory in the default mode and simply runs.
  The discipline in this skill is what protects the user there, not a prompt.
- Reach for the reversible form instead: `git stash push -m "<why>" -- <paths>`
  rather than `reset --hard`; a new branch rather than a force-push;
  `git revert <sha>` rather than rewriting history.

## Checkpoints are not commits

laintas-cli keeps its own working-tree undo, backed by dangling commits that
never touch the index, the stash, or any branch. The user drives it with
`/snapshot [label]`, `/snapshots` and `/undo [sha]`; one is taken automatically
before the first workspace-mutating tool call of a top-level task; and you have
`snapshot.create`, `snapshot.list`, `snapshot.restore`.

- A checkpoint answers "undo what this session did to my files". A commit
  answers "record this change in the project's history". Neither substitutes
  for the other, and a checkpoint is not a reason to skip a commit the user
  asked for.
- Call `snapshot.create({label})` yourself before anything wide or hard to
  unpick — a rename across many files, a codemod, a dependency migration, a
  destructive command the user just approved. It is cheap and non-destructive,
  so take one rather than deliberating; the automatic checkpoint only fires
  once per top-level task and will be stale by then.
- Checkpoints cover tracked and untracked files, but not gitignored ones. That
  is why `git clean -x` is unrecoverable even with a checkpoint in place.
- `snapshot.restore` rewrites the WHOLE tree, so it also reverts edits made
  outside this session. Propose it only when your own changes are the problem,
  name the checkpoint from `snapshot.list`, and let the user approve — it
  always asks and cannot be pre-approved in bulk.

## Branches and worktrees

- Do not switch branches on your own initiative. `git checkout` carries
  modified files across, or fails outright on a dirty tree, and either outcome
  surprises the user.
- Sub-agents you spawn run in their own isolated git worktree, whose edits are
  merged back file-by-file when they finish. A sub-agent's `git status`
  therefore describes its private copy, not the user's tree. Do not commit,
  branch, or stash from a sub-agent — do the git work yourself, in the primary
  agent, after the merge-back.
- Do not run git commands from several agents at once. Concurrent git across
  worktrees can corrupt the shared `.git` metadata; serialize it.

## Remotes

- `fetch`, `pull`, and `push` touch state shared with other people: only on an
  explicit request. Prefer `git fetch` and report what is upstream over
  `git pull`, which can merge or rebase and leave conflicts behind.
- Never push to `main`/`master` unless the user named that branch.
- Use `gh` for pull requests when it is installed and authenticated. Put nothing
  in a PR body or commit message that you have not actually verified — no
  "tested" or "fixes" claims you did not run.
