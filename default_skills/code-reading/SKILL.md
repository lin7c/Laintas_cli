---
name: code-reading
description: Reading an unfamiliar codebase to answer a question or prepare a change - what to ask the index for, what to grep for, how wide a window to read, and when a result is too incomplete to conclude from. Load before a repository investigation, review, or "where does X happen" question; not needed to reopen a file you already read this turn.
version: 2.1.0
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

Two questions, in order: *where is it?*, and *how much of it do I actually
need to read?*

## Locate before reading

Reading starts by finding the lines that matter, not by opening a file:

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
  callers you will affect and the tests that cover it. A grep for the
  symbol gives that set approximately; widen it when a caller could spell
  the call differently (an alias, a re-export, a dynamic dispatch).
- **To review**: read the complete diff or the complete target, not a sample.
  A review that read part of the change cannot report on the part it skipped,
  and must say so.

## Scale

When the reading splits into genuinely disjoint subsystems, and only then,
consider parallel readers (see the agent-orchestration skill). Two agents
reading the same files is pure cost. One agent reading two files in one turn
is not delegation - it is just a batched turn, and it is cheaper.
