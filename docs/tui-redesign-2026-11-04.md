# Terminal interface redesign audit — 2026-11-04

Checkpoint: `b82b7b0` (`chore: checkpoint TUI audit before interface redesign`).
The preceding UI work was committed before this round. Existing backup and
build-scratch directories were excluded.

## Design direction

The interface is an operational workspace: location and state, the working
area, then contextual actions. The selected object leads; supporting metadata
moves into an inspector or a labelled record when space is limited.

| Skill | Applied design decisions |
|---|---|
| tui-design | Stable selection, explicit focus, contextual keys, terminal restoration and responsive structure. |
| ptk-layout | Shared list/preview layouts, single-pane inspector navigation and resize focus repair. |
| rich-terminal-output | Copyable tables, narrow records, semantic output and literal data. |
| tui-test | Real terminal input, resize, screenshots, state waits, streaming and exact restoration checks. |
| impeccable | Operate/Read hierarchy, progressive disclosure, recovery paths, state-first copy and a final rendered review. |
| frontend-design | One coherent visual direction, deliberate density and consistent green/violet identity. |
| ui-ux-pro-max | Keyboard access, visible selection, feedback, semantic colors and content priority across sizes. |

The general UI skills inform product decisions; terminal cells, native keyboard
behavior and the project's Python libraries determine the implementation.
[DESIGN.md](../DESIGN.md) records the reusable rules.

## Resulting behavior

- The command and model selectors separate names from their explanations. Wide
  views show a preview beside the list; narrower views show it below. Very
  short terminals retain inline descriptions and marquee access to long text.
- The command preview includes usage, aliases and contextual actions. Models
  search supply/provider information while returning the original complete ID.
- Pickers use the active prompt_toolkit output dimensions during resize.
  Idle previews repaint on state changes; active marquees and countdowns still
  advance. F1 expands key hints and Ctrl+U clears the filter.
- Multi-column command tables become labelled records when crowded. Semantic
  primary fields such as TASK, Path and Key lead the record. Headerless
  allowance/layout grids retain their established relationship.
- Account, backend, tool, skill, MCP, extension, lab and work-history listings
  use structured output. Identity domains and branch descriptions remain
  available in full. Paths, setting values and tool/hook JSON render literally.
- Resource browsers keep feedback separate from actions, show applied filters,
  preserve focus through resize and keep complete key/action pairs visible.
  Ctrl+R reloads; help suppresses underlying mutation shortcuts.
- Detail scrolling counts actual wrapped rows. End reaches the document tail;
  source-line requests and section/search navigation map to displayed rows.
- Background results publish on the UI loop. Cancellation/close reject late
  results. Changing selection defers only a returned detail; operation failures
  retain their message/style and still refresh the resource state.
- Retask and HWG provide `i` to focus an inspector and Esc to return. Narrow
  layouts give the primary checklist/graph the working width. Their help,
  feedback, action bars, themes and terminal ownership follow the shared rules.
  Manual checklist scrolling is no longer pulled back to the selected title.

## Verification and boundaries

- 74 primary commands and 84 names, including aliases, received a source/flow
  review. The checklist below records every command.
- 900 help renders cover the overview plus every primary command at
  38/50/80/120 columns in dark/light/mono.
- All 74 isolated handler scenarios ran with task-local state and remote I/O
  disabled. A scenario can exercise validation or an empty state; it is not a
  production account operation.
- All six bundled extensions loaded and exposed help/command discovery:
  `/blindpick`, `/canvas`, `/issues`, `/pow`, `/swebench`, `/whatsapp`.
- A real REPL exercised all 84 help names and 15 ordinary command scenarios.
  It also exercised sub-terminal quit/detach versus exit behavior.
- Real TUI sessions cover command/model choices, resource actions and async
  cancellation, Retask, HWG, Agent streaming, HWO, memory, messages, debug and
  skills. Additional theme captures use matching light/dark terminal profiles.
- OAuth, paid inference, remote writes and extension publication were covered
  through validation/help and existing mocked integration tests.
- Final full regression and cleanup results are recorded below after completion.

## Command-by-command checklist

Every row passed registry/help review, the real-REPL help check and its stated
isolated scenario. Specialized flows retain their own interaction model.

| Group | Command / aliases | UI review or change | Isolated scenario |
|---|---|---|---|
| Basics | `/help` | Generated help and contextual command preview. | `/help cwd` |
| Basics | `/cwd` | Literal paths, including bracketed directory names. | `/cwd` |
| Basics | `/progress` | Reviewed direct flow, status/empty states and wording. | `/progress` |
| Basics | `/fg` | Reviewed direct flow, status/empty states and wording. | `/fg` |
| Basics | `/messages, /msg` | Shared browser, complete actions and reading controls. | `/messages list` |
| Basics | `/scan` | Reviewed direct flow, status/empty states and wording. | `/scan` |
| Basics | `/img` | Reviewed direct flow, status/empty states and wording. | `/img list` |
| Basics | `/video` | Reviewed direct flow, status/empty states and wording. | `/video list` |
| Account & Session | `/login` | Reviewed direct flow, status/empty states and wording. | `/login extra` |
| Account & Session | `/account` | Labelled account records and explicit Current/Saved states. | `/account list` |
| Account & Session | `/private` | Reviewed direct flow, status/empty states and wording. | `/private list` |
| Account & Session | `/password` | Reviewed direct flow, status/empty states and wording. | `/password extra` |
| Account & Session | `/training` | Reviewed direct flow, status/empty states and wording. | `/training status` |
| Account & Session | `/usage` | Responsive metrics; allowance grids retain their layout. | `/usage local` |
| Account & Session | `/resume` | Shared picker, inline fallback, and in-place deletion feedback. | `/resume` |
| Account & Session | `/fork` | Reviewed direct flow, status/empty states and wording. | `/fork --help` |
| Account & Session | `/new, /clear, /new-session, /reset-session` | Reviewed direct flow, status/empty states and wording. | `/new` |
| Account & Session | `/exit` | Reviewed direct flow, status/empty states and wording. | `/exit --help` |
| Account & Session | `/quit, /q` | Reviewed direct flow, status/empty states and wording. | `/quit --help` |
| Account & Session | `/back` | Reviewed direct flow, status/empty states and wording. | `/back` |
| Account & Session | `/version, /v, /update` | Reviewed direct flow, status/empty states and wording. | `/version enterprise invalid` |
| Account & Session | `/extensions` | Structured extension records and contextual command discovery. | `/extensions list` |
| Agents & Terminals | `/name` | Reviewed rename flow and status output. | `/name` |
| Agents & Terminals | `/hire` | Shared model/choice pickers; capability flow reviewed. | `/hire audit-agent` |
| Agents & Terminals | `/agent` | Shared selection; dialog ownership flow reviewed. | `/agent` |
| Agents & Terminals | `/agents` | Specialized live view; streamed replies and resize exercised. | `/agents --plain` |
| Agents & Terminals | `/term, /t` | Shared resource browser; terminal handoff reviewed. | `/term peers` |
| Agents & Terminals | `/app` | Responsive manifest records; lifecycle flow reviewed. | `/app list` |
| Agents & Terminals | `/station, /st` | Command input wording, async results and resource browser. | `/station suggest --task audit` |
| Agents & Terminals | `/handoff` | Responsive handoff records and existing evidence flow. | `/handoff list` |
| Agents & Terminals | `/shared` | Path-first responsive records. | `/shared list` |
| Agents & Terminals | `/terminate` | Shared selection; target/cascade flow reviewed. | `/terminate absent-terminal` |
| Agents & Terminals | `/send` | Shared selection; direct terminal input flow reviewed. | `/send` |
| Agents & Terminals | `/spawn` | Reviewed direct flow, status/empty states and wording. | `/spawn` |
| Agents & Terminals | `/tell` | Shared selection; message-delivery flow reviewed. | `/tell` |
| Agents & Terminals | `/abort` | Shared selection; stopping/completed wording reviewed. | `/abort absent-agent` |
| Planning & Tasks | `/hwo` | Workflow Studio inspected at wide/narrow sizes and help states. | `/hwo status` |
| Planning & Tasks | `/hwg` | Responsive inspector, scrollable help, themes and terminal ownership. | `/hwg status` |
| Planning & Tasks | `/retask` | Responsive inspector, manual reading scroll, help and themes. | `/retask list` |
| Planning & Tasks | `/mode` | Shared choices; mode transitions reviewed. | `/mode list` |
| Planning & Tasks | `/plan` | Shared choices; revision/approval flow reviewed. | `/plan status` |
| Planning & Tasks | `/prompt` | Structured branch/profile records and shared choices. | `/prompt status` |
| Planning & Tasks | `/evolve` | Structured branch/profile records and actionable empty branches. | `/evolve status` |
| Planning & Tasks | `/task` | TASK-first responsive records and full progress fields. | `/task list` |
| Planning & Tasks | `/work` | Structured history and shared browser; literal objectives. | `/work status` |
| Planning & Tasks | `/workflow` | Reviewed direct flow, status/empty states and wording. | `/workflow status` |
| Config & Tools | `/model` | Searchable model/supply choices, previews and full-ID return. | `/model route list` |
| Config & Tools | `/config` | Key-first records; literal setting values and descriptions. | `/config` |
| Config & Tools | `/web, /search` | Responsive engine/diagnostic records. | `/web engines` |
| Config & Tools | `/identity` | Responsive records; complete domain lists. | `/identity list` |
| Config & Tools | `/policy` | Shared choices; approval-state semantics reviewed. | `/policy status` |
| Config & Tools | `/trust` | Reviewed workspace/hash review flow. | `/trust status` |
| Config & Tools | `/hooks` | Literal JSON reload results. | `/hooks status` |
| Config & Tools | `/backend` | Structured profile records and literal backend metadata. | `/backend status` |
| Config & Tools | `/max` | Reviewed direct flow, status/empty states and wording. | `/max` |
| Config & Tools | `/tools` | Structured tool/source groups and wrapping descriptions. | `/tools` |
| Config & Tools | `/tool` | Shared tool choices and literal arbitrary JSON results. | `/tool absent-tool` |
| Config & Tools | `/skill` | Shared browser and structured installed-skill records. | `/skill list` |
| Config & Tools | `/mcp` | Structured server/state/tool/error records. | `/mcp list` |
| Config & Tools | `/bash` | Reviewed direct flow, status/empty states and wording. | `/bash list` |
| Config & Tools | `/memory` | Shared document browser and responsive metadata. | `/memory status` |
| Config & Tools | `/prop` | Reviewed direct flow, status/empty states and wording. | `/prop sys` |
| Config & Tools | `/debug` | Shared evidence browser and wrapped content navigation. | `/debug` |
| Config & Tools | `/why` | Reviewed direct flow, status/empty states and wording. | `/why` |
| Config & Tools | `/stream` | Reviewed direct flow, status/empty states and wording. | `/stream` |
| Config & Tools | `/theme` | Common dark/light/mono styling in additional views. | `/theme` |
| Config & Tools | `/detail` | Wrapped navigation and logical source-line positioning. | `/detail` |
| History | `/undo` | Shared choices; checkpoint restoration flow reviewed. | `/undo absent-checkpoint` |
| History | `/snapshot` | Reviewed direct flow, status/empty states and wording. | `/snapshot audit` |
| History | `/snapshots` | Reviewed direct flow, status/empty states and wording. | `/snapshots` |
| History | `/compact` | Reviewed direct flow, status/empty states and wording. | `/compact status` |
| History | `/continue` | Reviewed direct flow, status/empty states and wording. | `/continue` |
| History | `/told` | Shared history/evidence browser; literal transcript reviewed. | `/told` |
| History | `/reload` | Reviewed direct flow, status/empty states and wording. | `/reload --help` |


## Final validation

- Full regression suite: **4,149 passed, 21 skipped, 1,129 subtests passed**.
  The 16 existing Python multi-threaded fork deprecation warnings remain.
- After the final detail-cache kind/collapsibility correction: **171 related
  tests and 279 subtests passed**, including its two new regression cases.
- **17 final real-terminal sessions passed** with exact termios restoration.
  They include command/model choices, async cancellation, Retask, HWG, streamed
  Agent replies, HWO, populated memory/messages/debug/skills, and additional
  light/mono captures. The light captures use a light terminal profile.
- The real CLI passed **84 help names and 15 ordinary command scenarios**,
  then sub-terminal detach and full exit, with exact terminal restoration.
- `ruff check --select F821 --exclude venv,build .`: passed.
- `git diff --check`: passed.
- The local launcher executes this checkout through its project virtualenv;
  the next CLI launch loads these changes.

## Cleanup

- All test runs, native terminal sessions and their owned children exited.
  A global process/cwd/descendant check found no remaining task processes.
- Removed the exact task temporary directory, including the isolated test
  environment, runtime state, downloads, caches, captures, logs and reports;
  verified that it no longer exists.
- Verified the final application source hashes before removing test artifacts.
  The design guide, audit record and regression tests remain in the repository.
