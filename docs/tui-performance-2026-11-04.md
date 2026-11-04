# Terminal UI performance audit — 2026-11-04

The redesign checkpoint for this audit is `b501c02`. The implementation changes
are limited to shared picker sizing/filtering, detail layout/cache handling, and
the styled wrapping and graph viewport helpers.

## Findings and corrections

| Area | Expensive work | Correction |
| --- | --- | --- |
| Shared choices | Each layout predicate and visible row reads the terminal size. A 120-column picker made about 80 output-size reads per frame. | Read once per application render; the next resize redraw refreshes it. |
| Shared choices | List, footer, preview, navigation and animation checks repeatedly rebuild/filter the same list. | Cache the current query's rows and original-index positions. Invalidate when choices change. Cache unchanged preview fragments. |
| Resource details | Opening an item wraps the entire document before showing its first screen, including a single source line containing megabytes. | Generate and cache physical rows incrementally, stopping at the visible viewport. Scrolling extends the index. Source-line navigation and End retain access to all content. |
| Resource details | Every cached frame copies and compares all source lines. | Track mutations of `UIDetail.lines` with a version token. Item edits, slices, append/remove, sorting and full replacement invalidate the row cache without a document scan. |
| End navigation | A large jump indexes the entire tail in one input callback, blocking further keys for seconds. | Extend the index in batches of 128 rows and yield to the application loop. New input, a changed detail/width, or close cancels the pending jump. Preserve newer operation feedback. |
| Timeline selection | Moving the selected section invalidates wrapped rows even though only its highlight changes. | Cache heading offsets and apply selection styling to visible rows without rebuilding layout. |
| Detail search | Every displayed line scans the complete match list to find its highlights. | Index matches by source line. Stop laying out the current hit as soon as it is visible. |
| Styled text | Wrapping repeatedly concatenates individual characters and measures printable ASCII through the Unicode width API. | Join style runs once per row; use the single-cell printable ASCII path and retain Unicode measurement for other characters. |
| HWG canvas | A horizontal viewport continues scanning the invisible right-hand tail of a style run. | Stop once the right edge is reached, preserving trailing combining characters. |

Retask and HWG use the same styled wrapping helper. Their data loading, model
execution, backend concurrency and refresh rates are unchanged by this patch.

## Measurement

The comparison uses the checkpoint source and the modified source, the same
Python environment, a 120×30 native terminal, and isolated anonymous runtime
state. No account or production backend operation is needed.

- Picker: 10,000 searchable choices, 15 separately observed downward moves,
  filtering, clear, 120→50→120-column resize, then cancellation.
- Detail browser: 10,000 lines of approximately 2 MB, first paint, separately
  observed page scrolls, End/tail visibility and close.
- Direct layout benchmark: 10,000 lines / 1,910,000 characters and one
  950,000-character line. A cold sample reloads the detail and discards the
  row cache; a cached sample repaints its existing viewport.
- Capture output-size read counts and exact terminal attributes before/after
  the native sessions. Timing is diagnostic and depends on host load; automated
  regressions assert bounded work rather than wall-clock thresholds.

Sequential native-terminal samples, with the full regression run stopped:

| Native scenario | Checkpoint | Modified |
| --- | ---: | ---: |
| Picker: median key receipt to completed frame | 41.057 ms | 21.429 ms |
| Picker: median frame drawing | 31.650 ms | 18.894 ms |
| Picker: output-size reads per frame, including the framework | 80 | 2 |
| Large document: first completed frame | 3,088.452 ms | 136.649 ms |
| Large document: median key receipt to completed frame | 37.753 ms | 33.321–40.194 ms |

The steady-state document scrolling sample does **not** demonstrate an input
latency improvement. The clear gains are first paint, picker work and cached
layout. Driver round-trip timings also include terminal-test IPC/polling and
are not used as application input latency.

On the final code, a new Home key interrupted an in-progress End operation and
displayed the first lines in **63.190 ms**, including test-driver overhead. A
subsequent End reached the actual tail; all sessions restored exact termios.

Sequential direct-layout samples (medians):

| Layout scenario | Checkpoint | Modified |
| --- | ---: | ---: |
| 1,910,000 characters / 10,000 lines: cold first viewport | 2,160.010 ms | 59.183 ms |
| Same document: cached viewport | 7.963 ms | 0.054 ms |
| 950,000 characters in one line: cold first viewport | 1,314.554 ms | 24.355 ms |
| Explicitly wrap all 1,000,000 ASCII characters | 1,379.303 ms | 653.970 ms |

Explicit End or navigation to a distant source line can still require indexing
the preceding content. End now shares the event loop in small batches and can
be interrupted by newer input. The viewport path no longer pays that cost on
open or each frame; this is not a constant-time guarantee for every document
operation.

## Regression guards

`tests/test_tui_performance.py` checks viewport-bounded initial work and scrolling,
cached frames without source scans, all supported list mutations, content
replacement, huge-line laziness, search near the start of a huge line, indexed
highlights, hidden diagram-tail scanning, once-per-render terminal sizing and
filter reuse during navigation. Existing layout, browser, picker, Retask, HWG,
station and command audit tests cover retained behavior.

Four additional End regressions cover event-loop progress, interruption without
a late scroll jump, replacement details with newer feedback, and collapsed
timeline selection without repeated wrapping.

## Final validation

- Related presentation, resource, picker, Retask, HWG, station, command-audit and
  performance tests: **186 passed, 291 subtests passed**, including 15 new
  performance/navigation tests.
- Full suite on the final application source: **4,165 passed, 21 skipped,
  1,141 subtests passed**; one real-CLI screen assertion failed. Its raw stream
  contained the expected command output, but the emulator did not retain it on
  screen. The 16 Python multi-threaded fork deprecation warnings remain.
- The PTY fixture had replied to every cursor query with row 1, column 1. It now
  connects pyte's `write_process_input` callback to the PTY so cursor/device
  replies are emitted at the correct position in the parsed stream. The existing
  assertions remain. The corrected complete attachment file: **13 passed**.
  The full suite was not repeated after this test-fixture-only correction.
- **Seven real native-terminal sessions on the final application source passed**:
  large picker, large document with interruptible End, dark/light/mono resource
  interactions, wide HWG, and long Retask. Exact termios restoration was verified
  in every session. They include search, clear, help, source edits, reload,
  selection changes, narrow/wide resize, cancellation and exit.
- Static resource idle sampling showed no continuous repaint after terminal
  initialization. The final captures were visually inspected.
- `ruff check --no-cache --select F821 --exclude venv,build .`: passed.
- `git diff --check`: passed.
- The local launcher runs this source checkout. Restart the CLI to load it.

## Cleanup

- All benchmark, native-terminal and pytest processes exited, including the
  deliberately stopped intermediate run. A global task-path/recorded-descendant
  process scan returned no remaining owned processes.
- Removed the verified unique temporary directory containing the test package,
  download cache, baseline source copies, isolated runtime/test state, captures,
  profiles, XML and JSON reports. Verified that the directory no longer exists.
- Rechecked final application source hashes and all seven exact terminal-state
  restoration results before removing artifacts. The design, performance record
  and regression tests remain in the repository. Preexisting services and
  unrelated workspace files were preserved.
