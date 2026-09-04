---
name: code-map
description: Read a public GitHub repository as a layered map instead of file by file - what the parts are, how they connect, and where the declarations live. Load before investigating a large remote repository; not for code checked out in the working directory.
version: 1.0.0
triggers:
  - code map
  - codemap
  - architecture map
  - map the repository
  - understand this repo
  - github repository
  - how is this project structured
---

# Reading a repository as a map

Code Map is a layered map of a **public GitHub repository**, built on the
server: what the system does, what each part is made of, and the real
declarations with their `file:line`. A parser produces the structure; a model
names the layers.

It maps a remote repository, never the working directory. For code already
checked out here, this skill does not apply - read the files, and load
`code-reading` for how.

## Read before you build

1. **`code_map.list`** - which repositories the account already has a finished
   map for, and how many slots remain. A map that exists costs nothing to
   read; a build costs the user money and runs for minutes to hours.
2. **`code_map.read(map_id)`** - the whole system in about 15 KB: what it is,
   every part with its summary, and the arrows between them. Start here, not
   with a file.
3. **`code_map.read(map_id, node='l1:<id>')`** - that part's components. Then
   `node='l2:<part>:<component>'` for its declarations with `file:line`, which
   is where reading actual source begins.
4. **`code_map.build(repo_url)`** - only when no map exists **and** the user
   has agreed to the cost. It returns an id immediately; poll
   `code_map.status` occasionally and do other work in between, never in a
   tight loop.
5. **`code_map.delete`** - frees a slot and is not reversible without another
   paid build. Ask first.

## What a map is not

It knows structure, never behavior. Once it has told you *where*, the answer
to *what it does* still comes from reading those lines.

It is also a snapshot of one ref. If the question is about work in progress,
the map is stale by construction and the source is the authority - say which
one you answered from.

## When the account has no map and the user has not asked for one

Do not build one to answer a question they asked casually. Say what a build
costs and what it would buy, and read the repository the ordinary way if they
would rather not pay for it.
