# Context compaction audit and live measurement (2026-09-17)

Conclusion: the previous changes did reduce review latency, but dropping the review's source evidence caused a quality regression. This fix speeds the pipeline up further on the same synthetic conversation while restoring the retention of error messages and user constraints. This is conversational-summary compaction, not file-archive compression; compaction quality is checked by whether the facts needed to continue working survive.

## Compared versions and method

- Committed baseline: the compaction functions and truncation adapter from `92ae85a`, review effort `auto`.
- Before: the workspace version at the start of this task — review effort `none`, head+tail truncation, but the review carried no source-transcript.
- After: the current workspace version — keeps `none`, restores the review's evidence and fixes chunking.
- Script: `scripts/benchmark_compaction.py`. The older function versions are extracted from git / source snapshots and run against the same environment and synthetic inputs.
- The chunk source-text budget is pinned to 4,000 tokens for easy boundary construction; the production default remains 24,000.
- Live calls use the existing laintas backend with the default Gemma summary model and DeepSeek review model; no real user conversation was sent.

## Live-model measurement

The sample is a service-repair task: one long tool log ending with an ImportError, a missing module and a failing exit code. Each version ran once, in the order baseline → before → after; upstream load, caching and model randomness were not controlled. What is measured is the summarize-plus-review sub-pipeline, not the compaction entry's pruning or other session operations.

| Version | Total time | Model calls | Estimated input tokens | Designated fact markers retained |
| --- | ---: | ---: | ---: | ---: |
| Committed baseline | 86.28 s | 4 | 2,752 | 3/6 |
| Before | 52.77 s | 4 | 2,132 | 3/6 |
| After | 23.40 s | 2 | 1,800 | 6/6 |

"After" is about **55.7%** faster than "before" and about **72.9%** faster than the committed baseline. These ratios describe this one sample, not a stable production average.

The six markers are `7319`, `approval`, `/srv/service/worker.py`, `ImportError`, `fastcodec`, `exit 7`. Manual inspection of the final summaries confirms: the deployment-approval restriction and the failing-test status were retained, and nothing was written up as deployed or fixed when it was not. The baseline kept only the head of the log and lost the error tail; in "before" the summary model could see the error tail, but after the evidence-free review those facts still did not survive. 6/6 is a retention check on the designated facts, not a general semantic quality score, and it does not guarantee that arbitrary facts from the middle of a log survive.

Full live results and the complete synthetic summary are in `compaction-benchmark-live.json`.

## Local long-log comparison

Twelve long tool logs with a deterministic fake backend; this measures mechanism overhead only and cannot estimate production latency or semantic quality.

| Scope | Baseline calls | Before calls | After calls |
| --- | ---: | ---: | ---: |
| Raw log fed straight into the summary sub-pipeline | 48 | 48 | 4 |
| Full forced-compaction entry, prune then summarize | 6 | 6 | 4 |

In the full entry, the estimated input is 13,002 tokens after, 7,344 before, 12,796 baseline. Restoring the evidence raises input relative to the evidence-free review; no claim is made that token cost drops in every case. Full-entry local wall times were 0.801, 0.641 and 0.793 seconds; this single fake-backend run likewise does not support a claim of across-the-board local CPU speedup. The reproducible wins are fewer redundant model calls, a bounded source-text size, and review evidence the reviewer can actually use.

## What the fix changes

1. Restores the review's original chunk evidence; every incremental summary is reviewed against its source before being trusted as input to the next fold.
2. Chunks are cut on the serialized text after truncation, eliminating wasted calls; complete tool calls with their results are packed together whenever they fit.
3. A single oversized message is split losslessly within the budget, covering CJK, instead of becoming "one chunk" that still overflows.
4. Tool-call arguments are kept; summary serialization respects protected tools so skill instructions / user rules are not truncated a second time.
5. The tail ratio is clamped to 0–1 and the character budget to non-negative, so a bad config cannot enlarge the output.
6. A backend failure no longer triggers a pointless review; a failed review keeps the draft; cancellation during the final review does not return a committable summary.

All overlong history is NOT aggregated into one unbounded final review: that would reintroduce the huge input and let unverified intermediate summaries propagate unchecked. The chunk budget is a source-text budget — the system prompt, running summary and model output still need room on top of it; it is not a hard cap on the model's whole context window. Head+tail pruning remains lossy; important facts in the middle of a log are not guaranteed to survive.

## Verification and reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_compaction_evidence.py tests/test_session_runtime.py \
  tests/test_esc_cascade.py tests/test_idle_consolidation.py tests/test_file_pager.py

PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_compaction.py \
  --baseline 92ae85a --pipeline --output /tmp/compaction-pipeline.json

# Calls the actually configured models and consumes normal quota.
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_compaction.py \
  --baseline 92ae85a --live --output /tmp/compaction-live.json
```

- 156 tests and 13 subtests pass, covering evidence, long messages, failure fallback, cancellation and session regression.
- `git diff --check` passes.
- The test and benchmark processes have exited; timers were cancelled and joined; no servers or background services were left running.
- This task's temporary source snapshots and /tmp results were cleaned up; only the code, tests and the measurement artifacts in docs remain.
- Nothing was committed, pushed or released; the workspace's other in-flight changes are untouched.
