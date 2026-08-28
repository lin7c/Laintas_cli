---
name: code-reading
description: Reading an unfamiliar codebase to answer a question or prepare a change - what to ask the index for, what to grep for, how wide a window to read, and when a result is too incomplete to conclude from. Load before a repository investigation, review, or "where does X happen" question; not needed to reopen a file you already read this turn.
version: 1.0.0
triggers:
  - read the code
  - understand the codebase
  - where is this defined
  - who calls this
  - how does this work
  - trace the flow
  - explore the repo
  - code review
  - find the implementation
---

# Reading code

The goal is a correct answer for the fewest bytes pulled into context. Bytes
you read stay in the context for the rest of the task, so a whole file read to
confirm one function is paid for on every later turn.

Three questions, in order: *does an index already know this?*, *where is it?*,
*how much of it do I actually need to read?*

## Ask the index first, when there is one

If `atlas.*` tools appear in the native schemas, this workspace has the Code
Atlas extension: a deterministic graph of the tree (modules, classes,
functions, and the import/call/inherit edges between them). It is built by a
parser, not a model, so it cannot invent a definition or an edge that is not
in the source.

Use it in this order:

1. **`atlas.stale`** - first, once per task. It compares the stored file
   hashes against the tree on disk. A stale index is worse than no index: it
   answers confidently and wrongly. If it reports stale, either re-index
   (`/atlas index .`) or fall back to `grep`/`read` for this task and say
   which you did.
2. **`atlas.find(name, kind?)`** - where a symbol is defined, as `file:line`.
   Replaces grepping for `def foo`/`class Foo` and the guessing that follows.
   It distinguishes an exact hit from a substring near-miss; treat the
   near-miss list as candidates, not as the answer.
3. **`atlas.outline(module)`** - every class with its methods and the
   top-level functions, with line numbers, *without reading the file*. This is
   the one that saves the most: it turns "read a 9000-line module to find the
   entry point" into one call plus one narrow `read`.
4. **`atlas.neighbors(node_id)`** - both directions of every edge touching a
   node, with `file:line` evidence. The reverse direction (*who reaches this?*)
   is what grep is worst at: callers spell the call differently than the
   definition spells itself, and grep cannot see an alias or a re-export.
5. **`atlas.lookup(src, dst)`** - exact transitive dependency paths between
   two modules, for "can this layer even reach that one".

No index yet (`no index at ...`)? Building one costs a single
`/atlas index .` run and no model tokens. Worth it for anything larger than a
couple of files; skip it for a one-file question.

The index knows structure, never behavior. Once it has told you *where*, the
answer to *what it does* still comes from reading those lines.

## Locate before reading

Without an index, or for anything the graph does not model (strings, config,
templates, generated code):

- `grep` by content, `glob` by path shape, `ls` for one directory level.
- Narrow each call - a precise pattern, a specific path, a bounded range.
  Narrow the call, never the number of calls: independent greps and reads all
  belong in the same turn.
- Search for the distinctive token: an error string, a route literal, a
  config key. Searching for a common word returns a truncated wall.

## Pages for reading, windows for checking

`read(path)` opens a file as a paged document: page 1 arrives with a page
count, `page="next"` turns the page, and the page you leave is **dropped from
your context** and replaced by a stub carrying its line range and a generated
index of what it defined. One file therefore costs one page of context however
large it is - and a page is sized to the room actually available, so a file
that used to take fifteen windows takes three or four page turns.

- **Reading a file to understand it**: use the pages. Do not hand-roll paging
  with offset/limit - that is the habit this replaced, and it is measurably the
  slowest way to read a file (one model turn per window).
- **Pass `note` when you turn a page**: your summary of the page you are
  leaving, written for someone who cannot see the code, keeping the line
  numbers that matter. The generated index survives without it, but only you
  can record what the page MEANT.
- **`pin: true`** holds a page you are about to edit, so turning elsewhere does
  not take it away.
- **Checking a specific thing** - the lines around an `fs.grep` hit, a
  function you were told about - is what `offset`/`limit` are still for. That
  window does not move the page cursor and is never evicted.
- Read the region you located, with enough margin to see the surrounding
  block - not the whole file, and not three lines with no context.
- `read` states which lines it delivered against the file's real length and
  names the offset that continues it. When it marks a truncation, material was
  dropped: continue from the named offset or narrow the request. **Never draw
  a conclusion from a truncated result** - "the function is not there" is
  exactly the wrong answer to get from a window that stopped early.
- Do not re-read a file you have not changed. Reuse what you read this turn.
- Never edit from memory of a file: read the exact target region first, then
  anchor the edit on it.

## Reading to answer vs. reading to change

- **To answer a question**: follow one path end to end (entry point -> the
  branch that matters -> the effect), and stop when the question is answered.
  Breadth-first reading of a subsystem produces context, not an answer.
- **To make a change**: read the target and its immediate neighbors - the
  callers you will affect and the tests that cover it. `atlas.neighbors` gives
  that set directly; grep gives it approximately.
- **To review**: read the complete diff or the complete target, not a sample.
  A review that read part of the change cannot report on the part it skipped,
  and must say so.

## Scale

When the reading splits into genuinely disjoint subsystems, and only then,
consider parallel readers (see the agent-orchestration skill). Two agents
reading the same files is pure cost. One agent reading two files in one turn
is not delegation - it is just a batched turn, and it is cheaper.
