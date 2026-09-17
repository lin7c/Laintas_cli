# Background silent compaction

By default, background summarization and evidence review start at **70%** of the
usable message budget while the main agent keeps reasoning and running tools.
At **90%**, the main loop prefers to wait for the existing summary; if the
candidate failed, became stale, or does not exist, it falls back to the original
foreground pruning and compaction.
Manual `/compact`, `/compact --force`, context-overflow recovery, and
consecutive output-truncation recovery are all preserved.

## Budget and configuration

These percentages use the usable message budget from `compaction_budget()` as
the denominator — after subtracting the system prompt, tool catalog, dynamic
state messages, and output/safety reserves; they are not percentages of the
model's nominal window.
`/compact status` shows background triggers, foreground triggers, the target
size, and the current background task state.

| Runtime config | Default | Purpose |
| --- | ---: | --- |
| `compact_background` | `true` | Enable background pre-compaction |
| `compact_background_ratio` | `0.70` | Background trigger point |
| `compact_auto_ratio` | `0.90` | Foreground wait / auto-compaction trigger point |
| `compact_target_ratio` | `0.50` | Target occupancy after pruning; leaves room for next-turn growth |
| `compact_background_cooldown` | `60` | Cooldown seconds between background attempts, kept across user turns |
| `compact_background_min_tokens` | `2000` | Minimum estimated tokens a background summary must reclaim to be committed |
| `compact_background_timeout` | `180` | Total time budget (seconds) for background generation and review |

The thresholds must satisfy `target < background < auto <= 1`; invalid
combinations are rejected.
The target size is an optimization goal — recent messages are never dropped
outright just to hit a fixed ratio.
One huge tool result can skip the background band entirely and go straight to
the foreground fallback.

```text
/config compact
/config compact_background false
/config compact_auto_ratio 1.0
```

The last two lines turn pre-compaction off and restore the behavior where
foreground compaction triggers only at 100% of the usable budget.
These configs follow the existing runtime-config mechanism; no new global
persistent settings are added.
A shared context policy with `auto: false` disables both background and normal
automatic compaction; explicit manual compaction and overflow recovery are
unaffected.

## Commit and lifecycle

1. At a request boundary the main loop picks the compactable old prefix and
   keeps the recent context; complete tool calls and their results are never
   split across the commit boundary.
2. The background task reads only a deep copy of the prefix and the old
   summary, and runs the same chunking, summarization, and evidence review; it
   never mutates in-flight messages or task state.
3. Before committing, it re-checks the session, agent, working directory, run
   number, summary config/policy, old summary, and prefix content.
4. The main loop only replaces the matching prefix with the summary; the
   suffix and any messages added while the background task ran are kept intact.
5. A reviewed result is committed before the next request; if the result
   finished during the final request, it may also be committed when the session
   is saved at the end.
   The end-of-session save never starts a new task and never waits for a
   summary that is still running.
6. At most one background task per owner; at most two speculative tasks in the
   whole process, so multiple agents cannot monopolize model concurrency.
7. Ctrl+C, run end, or abnormal exit cancels unfinished tasks; manual
   compaction first cancels the existing background candidate.
   Config, session, or prefix changes invalidate a result; merely appending new
   messages does not.

If the background task times out or fails, the original messages are kept. At
the foreground threshold a synchronous recovery is attempted.
If a cancelled model call does not respond to cancellation, the current turn is
ended after the cleanup grace period, so over-budget requests are not sent and
summarization is not re-launched repeatedly.
Closing a run scope waits at most one second for cleanup; a worker that still
ignores cancellation keeps occupying its task slot until it exits and cannot
write state back.
An underlying HTTP request that has not yet received response headers cannot
force-revoke inference already sent to the server; once the response arrives it
is closed explicitly rather than relying on garbage collection to release the
unread response. A background timeout also does not mean server-side billing
stops immediately.

## Quality, silence, and verification

The background path reuses the same summary structure, protected-output rules,
head+tail pruning, and raw-evidence review; the review bar is not lowered.
Candidates below the minimum reclaim, invalid, cancelled, or stale ones are not
committed; the same failing prefix is not re-launched at every checkpoint.
A normal background run and commit show no spinner and never steal the main
agent's output; only when the foreground genuinely has to wait is a cancellable
status shown.
Auxiliary requests keep billing under the existing `compaction` /
`compaction_review` labels and do not replace the main model's status display.

Tests use controllable thread events and a fake backend, covering:

- A summary still running when the main loop issues its first model request of
  a turn; the second request then uses the new summary.
- New user/assistant/tool messages are preserved; old-prefix mutation, session
  switches, and config changes reject stale results.
- The 90% wait reuses the same task; forced overflow reuses the candidate;
  failure falls back to synchronous compaction.
- Manual takeover, Ctrl+C, timeout, abnormal exit, cooldown, minimum-reclaim,
  and concurrency caps.
- Budget/status queries, cross-turn state, and the existing compaction /
  paging / cancellation / session-resume logic.

What this verifies is scheduling and data integrity, not a new live-model
latency benchmark. The real cost and duration of background work still depend
on the model and its concurrency limits.

## Oversized turns and model budgets

A known provider window below the configured window is a hard ceiling. Window
memory is reloaded when the selected model changes. Fixed request overhead can
leave zero usable message tokens; the CLI reports that condition and does not
invent a minimum budget or send an oversized request after failed compaction.

If a complete recent turn alone exceeds the foreground threshold, foreground
compaction can summarize the whole conversation, including a single large user
message or completed tool exchange. Incomplete tool exchanges are retained.
Small short conversations still make manual compaction a no-op.

Generation and review explicitly request the policy's summary output limit
(default 4096 tokens, reduced for small windows). Source chunks reserve space for
prompts, the previous summary, the review draft and output. Auxiliary calls use
a conservative 32000-token ceiling, lowered by a remembered or observed smaller
model window, and validate the assembled input before sending. An unknown model
can still reject its first request; a reported smaller window informs subsequent
attempts. An oversized legacy summary fails safely instead of being sliced.

Truncated, filtered, tool-calling or oversized summary responses cannot replace
source history. A failed review retains the complete generated draft, preserving
the existing review fallback. These checks bound requests and reject incomplete
results; they do not prove semantic fidelity of a model-generated summary.
