# Context budget

Every request is a tree, and every node takes a **share of its parent**,
floored at a minimum. There are no ceilings.

```
window (reported by the gateway, per model)
├─ output      share of the window, never above the model's output ceiling
└─ input       window − output
   ├─ system   level 1 — the system prompt
   │  ├─ <role> <capabilities> <execution> …        level 2 — tags kept
   │  │  └─ persistentMemory, tools, rolePrompt …   level 3 — markers removed
   │  └─ gateway                                    the gateway's additions (hidden)
   ├─ tools    level 1 — tool schemas
   ├─ live     level 1 — the per-iteration tail (<task>, <progress>, <now> …)
   └─ thread   the rest — compaction thresholds, pages, results are shares of it
```

## Rules

* **A level only acts when its parent overflows.** While `system` fits its
  share of the input, nothing inside it is touched, whatever the children's
  shares say. Once it is over, its children are compressed in proportion to
  their shares; a child already under its share keeps its full text and hands
  the surplus to the others. `shrink: none` blocks (safety, authority, durable
  rules, product protocol) are never cut.
* **Floors only.** `min` holds on any window; nothing is capped. A block that
  wants more than its share on a large window simply gets it.
* **The gateway goes first.** The gateway's additions (language rule, tool
  guide, model pins, experience) are a level-2 block of `system` with a 0%
  share: the moment `system` is over, the request carries
  `promptBudget: {"gateway": 0}` and the gateway adds nothing. Only their size
  ever reaches the CLI (`_budget.injectableTokens`); the block is never shown.
* **Tags.** Level-2 tags stay in the prompt. Level-3 and deeper markers only
  partition a block and are removed from what the model receives — the runtime
  wraps template slots in them, and you can write your own in `cli.prop`, e.g.
  `<capabilities>… <tools>…</tools> …</capabilities>`, once `budget.json` (or a
  `/config budget system capabilities tools share`) declares the node. Tags that
  no node declares are ordinary text.
* **Stable.** Compression is deterministic and a released block needs to fall
  below `budget hysteresis` of its allotment, so the prompt does not change from
  turn to turn and the provider's prefix cache survives.

## Calibration

The thread shares are sized on a **1M-token model** (thread budget ≈ 860K
tokens) and scale with every other window:

| share | on 1M | on 128K (thread ≈ 80K) |
| --- | ---: | ---: |
| `tool_result` 0.02 | ≈17K tokens per result | ≈1.6K |
| `read_page` 0.04 | ≈34K tokens per page | ≈3.2K (floor 2.5K) |
| `web_fetch` 0.08 | ≈69K tokens of page text kept | ≈6.4K (floor 5K) |
| `grep_line` 0.0005 | ≈430 tokens per matching line | 60 (floor) |

Only what enters the context is a share. Limits that never reach the model
keep their fixed values: the raw download cap of `web.fetch`, the directory
walk's entry and time limits, `fs.grep` skipping files over 8MB, the capture's
16MB memory guard, and image size limits for vision. Every value this budget
introduced is a `/config budget …` setting — shares, floors, `hysteresis`,
`assumed_window`, `assumed_summary_window` and `chars_per_token` — and all of
them appear on the tuning page and in `.config` files.

## Where the rest of the constants went

| Was | Now |
| --- | --- |
| `model_context_window`, `context_trigger_share`, `context_window_adopt_cap` | the real window; `budget thread …` |
| `compact_background_ratio` / `compact_auto_ratio` / `compact_target_ratio` | `budget thread compact_background` / `compact_foreground` / `compact_target` |
| `output_truncate`, the per-tool multiples | `budget thread tool_result` (reads: `budget thread read_page`) |
| pager 8K–120K characters, 35% of headroom; `fs.read` 200KB / 2000 lines | `budget thread read_page` |
| `web.fetch` 64KB of text kept | `budget thread web_fetch` (the raw download cap stays: it never enters the context) |
| `fs.grep` 100 / `fs.glob` 200 / `fs.ls` 100 results | `budget thread tool_result`, continued with `offset` |
| grep lines cut at 500 characters | `budget thread grep_line` |
| window assumed before a model reports (64000; was `model_context_window`) | `budget assumed_window` |
| summarizer window assumed before it reports (32768) | `budget assumed_summary_window` |
| 3.5 characters per token when a token budget becomes a character cut | `budget chars_per_token` |
| `terminal.read` 4000 (max 20000), `browser.snapshot` 5000 characters | `budget thread tool_result` (the cursor / saved text covers the rest) |
| a PTY command's output read back from the 512KB session buffer | captured whole per command (a 16MB memory guard keeps both ends and states the gap) |
| `tool_output_max_chars` (2000), `keep_recent_min/max` | `budget thread pruned_output`, `budget thread recent_tail` |
| `buffer_tokens` output reserve | `budget output` |
| `compact_chunk_tokens`, summary output 4096, margins 1024/512 | `budget aux summary output` / `margin`; slices are whatever both windows leave |
| critic 4000 / memory extraction 8000–12000 characters | `budget aux critic source`, `budget aux mem_extract source` |

A result too large for its share keeps both ends, and the whole output is
saved under `.laintas/outputs/` for `read(path, page=…)`.

## Seeing it

* `/prop budget [N]` — every block, original → sent tokens; a compressed
  block shows what the model was sent, then the original below it.
* `.laintas/budget/<agent>.md` — the same report for the agent, written only
  while something is compressed; `read` it and page through.
* `/prop budget [N] output [path]` — writes a self-contained page (default
  `.laintas/budget/budget-N.html`, no network needed) built from that request:
  the model, its window and every block and tool. Drag the shares and the page
  re-runs the budget live — which blocks would be compressed, which tools
  kept, what the thread is left with — then **Download .config** saves only
  what changed. `/config budget reset` returns the budget to the shipped defaults.
* `/config budget` lists every knob; `/config budget system` one level; `/config budget system share 0.2` sets one — a level per word, like every other multi-level command.

Defaults live in `agent_gateway/context/budget.json` and are vendored to
`context_policy/budget.json` without the gateway's own subtree.

## `.config` files

`/config import <file>` applies any settings — budget or not — written one per
line exactly as they would follow `/config`:

```
# comments are fine; a leading /config is tolerated
budget system share 0.05
budget thread read_page share 0.2
compact_background false
search_engine 'cn-bing duckduckgo'
```

All or nothing: one bad line (named by number) changes nothing. Values persist
as they would when typed. `/config export <file>` writes every setting changed
from its default in the same format, so a tuned setup moves between machines.
