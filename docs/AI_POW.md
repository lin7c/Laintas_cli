# AI-PoW 0.8

Run `laintas-cli pow init` once inside a Git project, then use laintas-cli normally.
After a commit, use these two commands:

```sh
# Current version: cumulative total score and iteration history
laintas-cli pow index --html

# Current iteration: this commit's score and supporting evidence
laintas-cli pow report --html

# A specific previously sealed iteration
laintas-cli pow report --commit <commit-hash> --html

# Recorder status and proof verification
laintas-cli pow status
laintas-cli pow verify
```

Each HTML command prints its output path. Open that file in a browser.
The overview lives at `.git/ai-pow/index.html`; iteration reports live at
`.git/ai-pow/reports/<commit>.html` (worktrees use their own Git metadata path).
Both pages are offline, use English interface text, and group supporting data
into expandable sections. The overview leads with Total score; an iteration
leads with Iteration score. Nothing is uploaded.

`--view latest`, `--view iteration`, and `report-config` are obsolete; use the
separate `report` and `index` commands. After upgrading, run both HTML commands
to refresh existing pages. This does not change sealed scores or count an
iteration twice. Use `laintas-cli pow index --rebuild --html` to rebuild the
iteration chain from locally available sealed proofs when needed.
Export a proof with `laintas-cli pow export /path/to/proof.jsonl`.

The default post-commit hook seals proofs automatically if no existing/custom hook conflicts.
Already initialized projects also work with the standalone `aipow` command and Claude hooks.

Disabled projects create no AI-PoW data. Data stays in the worktree Git metadata directory,
default database cap 64 MiB, metadata only. SQL journal and exported bundles require extra space.
The recorder never blocks chat on an error; `pow status` exposes a persistent coverage error marker.

The bundled `ai_pow.py`, `ai_pow_scoring.py`, and `ai_pow_report.py` are identical to the standalone project modules.
`aipow_bridge.py` connects native prompt, visible output, usage, tool and Skill context events.
Package manifest includes the core, scoring, report, and bridge modules.
When changing the core, update the standalone source and vendor using apply_patch, then run
`tests/test_aipow.py`; it detects drift when the independent source is alongside this repository.

Verification proves local consistency, not honest execution or complete capture.
The current retention-v2 score weights input retention 40%, artifact survival 40%,
and task fulfillment 20%. Resource usage is recorded but is not scored.
Older sealed proofs retain their original scoring algorithm. Each iteration adds
its existing score once to the cumulative total, starting at zero.
Missing evidence is neutral and low confidence is provisional. Human attribution is temporal,
artifact fingerprints are structural/content proxies, and code quality is not scored.
Task evidence is explicit: `pow task checkout --status completed --evidence src/checkout.py`.
Reference prices require explicit price snapshots; missing usage fields remain unknown.
Rebase/amend/missed commit boundaries require `pow reset-boundary`; old evidence is preserved.

Native coverage includes interactive top-level inputs/visible outputs, usage_tracker records, tool dispatch
and result events, Skill context injection, child creation, and file snapshots after tool batches.
Auxiliary threads without explicit repository context, noninteractive rendering and other workflow paths
can have gaps. Child worktrees must be initialized separately; their costs are not automatically copied
into the parent proof. Use the standalone protocol/readme for the complete measurement and trust contract.
