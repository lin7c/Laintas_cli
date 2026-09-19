# ai-pow

[AI-PoW](https://github.com/lin7c/ai-pow) for laintas-cli: a process score and
a verifiable work journal for every Git commit, recorded from your sessions.

```
/pow                       where this session records, and hook health
/pow init [path]           start recording the repository containing path
/pow off | /pow on         pause / resume recording for this session
/pow focus [path|auto]     the repository chat-only turns count toward
/pow report [commit]       the commit proof page (HTML)
/pow history               the repository summary page (HTML)
/pow verify [commit]       check a sealed proof against Git
/pow export <file> [commit]
/pow repair hook|boundary|rebuild
```

`laintas pow <args>` is the standalone `aipow` command line, served from this
package. The post-commit hook calls it.

## Recording follows the work

Start the CLI in `~` and ask it to change `~/projects/app`: the work is
recorded in `app`, not in `~`. Each event is attributed by its own target —
a file tool by its paths, a shell command by its directory and any `cd` or
`git -C` in it. Events without a location (your message, the reply, model
usage) belong to the turn, and are replayed into each repository when the
turn first touches it, before that repository's baseline sample. A turn that
touches no repository counts toward the focus repository.

Only repositories where `/pow init` has run are recorded. `/pow` lists the
ones the session worked in that are not.

## Nothing is offered to the model

No tools are registered and no score reaches the model's context. The score
describes a person's work; an agent that can read it can optimise for it.

## Files

```
main.py            /pow, `laintas pow`, setup
router.py          which repository each event belongs to
ai_pow*.py         vendored from lin7c/ai-pow -- do not edit here
```

Update the vendored files with `python scripts/sync_ai_pow.py <ai-pow checkout>`.
