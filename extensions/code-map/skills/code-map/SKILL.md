---
name: code-map
description: Read a public GitHub repository as a layered map instead of file by file - what the parts are, how they connect, and where the declarations live. Load before investigating a large remote repository; not for code checked out in the working directory.
version: 1.1.0
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

## How to read one: a glance, not a residence

Read it the way a person reads the map at a park entrance. Look once, get
your bearings, take the two or three things you needed, then walk. Nobody
walks the park holding the map open, and nothing here is worth keeping in
context after you have used it: read, summarise what the task needs, move on.
Read it again when you next need bearings.

That is also the honest scope of it. A map answers *where does this live and
what is it made of*. It does not answer *what does it do* - once it has told
you where, that comes from reading those lines.

## What a map will not tell you

**It is a partition, so a feature's callers are somewhere else.** Every file
belongs to exactly one part. Ask the map where the web-search feature lives
and it names the four files that implement it - the five that *call* it are
filed under the tool registry, the agent loop and the bridge. Use the map to
find the home of a thing, then search from there; do not treat a part as the
complete footprint of a feature.

**It is a picture of one commit.** Every read returns `freshness`: the commit
the map was built at, measured against the checkout you are standing in. When
that says the map is many commits behind, its file paths are still roughly
right and its prose may not be - the source is the authority, and say which
one you answered from.

## When the account has no map and the user has not asked for one

Do not build one to answer a question they asked casually. Say what a build
costs and what it would buy, and read the repository the ordinary way if they
would rather not pay for it.
