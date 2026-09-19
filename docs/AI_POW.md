# AI-PoW 0.8

AI-PoW is an official extension. Install it once, then start recording a Git
project and use laintas-cli normally:

```sh
/extensions install ai-pow
/pow init            # the repository containing the current directory
/pow init <path>     # or name it
```

Recording follows the work, not the directory the CLI started in: started in
`~`, an agent that edits `~/projects/app` records into `app`. File tools are
attributed by their paths, shell commands by their directory and any `cd` or
`git -C`; your message, the replies and model usage belong to the turn and are
replayed into each repository the turn touches. A turn that touches none counts
toward the focus repository. See `extensions/ai-pow/router.py`.

```
/pow                       where this session records, and hook health
/pow off | /pow on         pause / resume (the pause is recorded as a gap)
/pow focus [path|auto]     pin the repository chat-only turns count toward
/pow report [commit]       this commit's score and evidence (HTML)
/pow history               cumulative total and iteration history (HTML)
/pow verify [commit]       check a sealed proof against Git
/pow export <file> [commit]
/pow repair hook|boundary|rebuild
```

The pages live at `.git/ai-pow/reports/<commit>.html` and `.git/ai-pow/index.html`
(worktrees use their own Git metadata path). They are offline; nothing is uploaded.

`laintas-cli pow <args>` is the standalone `aipow` command line (`status`,
`report --html`, `index --rebuild --html`, `task`, `reset-boundary`, ...),
served from the extension. The post-commit hook runs
`laintas-cli pow --cwd . seal`, naming the launcher rather than a file inside
the extension, so updating or moving the extension cannot break it. A hook
written by an older version (pointing at the removed `ai_pow.py`) is rewritten
the first time the session works in that repository, or with `/pow repair hook`;
a hook you edited is never touched.

Nothing is offered to the model: no tools, and no score in its context.

## Maintaining

`extensions/ai-pow/ai_pow*.py` are vendored byte-for-byte from the standalone
project. Change them there, then `python scripts/sync_ai_pow.py ../ai-pow` and
bump the extension version; `tests/test_aipow_extension.py` detects drift when
the independent checkout sits alongside this repository.

The host side is the `ctx.on` lifecycle-event API (`extension_runtime.OBSERVABLE_EVENTS`);
the core has no knowledge of AI-PoW.

## Trust contract

Disabled projects create no AI-PoW data. Data stays in the worktree Git metadata
directory, default database cap 64 MiB, metadata only: prompts and replies are
stored as sizes and hashes, tool calls by name. The recorder never blocks the
agent on an error; `/pow` shows a persistent recording-error marker.

Verification proves local consistency, not honest execution or complete capture.
The retention-v2 score weights input retention 40%, artifact survival 40% and task
fulfillment 20%; resource use is recorded but not scored. Human attribution is
temporal, artifact fingerprints are structural proxies, and code quality is not
scored. Rebase/amend/missed commit boundaries need `/pow repair boundary`.

Known gaps: a shell command that changes a repository without naming it (no
path, no `cd`) is sampled at the next turn instead of linked to this one;
sub-agent worktrees must be initialized separately.
