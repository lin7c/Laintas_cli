---
name: api-contract
description: Building the backend half of a product whose frontend is written by a Helpwo agent — the shared API contract, what each state means, and why "I implemented it" is not the same claim as "it works". Load when a frontend agent is on the other end, or when the contract tools appear in a task.
version: 1.0.0
triggers:
  - contract
  - api contract
  - helpwo
  - frontend agent
  - endpoint
  - openapi
  - drift
  - implement the api
---

# Working against a frontend agent

When a Helpwo agent writes the frontend and you write the backend, you are two
processes with no shared conversation. The frontend cannot see your code, you
cannot see its components, and neither of you remembers the other's last run.

Everything you agree on has to survive that. One thing does: the contract in
`.laintas/contract/`.

## The two files

```
.laintas/contract/openapi.json         the interface (OpenAPI 3.1)
.laintas/contract/contract.lock.json   who agreed to what, and its state
```

Both are checked into the repository — the contract tools carve them out of
the `.laintas/` ignore rule the first time they run, because an interface
nobody can review in a diff is not an agreement.

Read them with `contract.read` and `contract.status`. Do not parse the files
by hand; the lock file's fingerprints are how drift is detected and editing
them directly is how you make every endpoint look permanently broken.

## The states, and what each one costs to claim

```
proposed ──agree──> agreed ──implement──> implemented ──verify──> verified
    ^                  ^                       │                     │
    └───── counter ────┘                       └──── drift ──────────┘
```

**proposed** — the frontend said what it needs. It is already building against
a generated mock, so it is not waiting for you, but it is committed to that
shape. Read the definition before you touch it.

**agreed** (`contract.agree`) — you accepted. This is a promise, and the
frontend will ship code that depends on it. If the shape is wrong for the
backend — it needs a parameter you cannot supply, or a response you cannot
produce cheaply — do not agree and then build something adjacent. Counter-offer
with `contract.propose`, which returns it to `proposed` so the frontend has to
look at your change instead of discovering it at runtime.

**implemented** (`contract.implement`) — you built it, and you name the files
that build it. Name the files that actually implement the behaviour. Their hash
is what drift compares against later, so listing a whole directory makes every
unrelated edit look like drift, and listing a file you merely touched makes
real drift invisible.

**verified** (`contract.verify`) — the service was asked and answered
correctly. This is the only state that is evidence rather than a claim.
`implemented` is you saying you built the thing; `verified` is the thing
proving it. Do not report a task complete at `implemented`.

## Do this, in this order

1. `contract.read` the operation. If it is `proposed`, decide: agree or
   counter-offer.
2. `contract.agree` once the shape is one you can actually deliver.
3. Build it.
4. `contract.implement`, naming the implementing files and the base URL where
   it answers.
5. `contract.verify`. If it fails, the operation goes to `drift` and you are
   not done — read the problems, they name the field and the expected type.

## Drift

`contract.drift` finds two things, and they are different failures:

- **the declared shape changed after it was agreed** — someone edited the
  contract. Code written against the old shape is now wrong.
- **an implementing file changed after it was declared done** — someone edited
  the code. It may no longer do what the contract says.

Run it before you claim a batch of work is finished, and after you change
anything you previously implemented. `mark=true` writes the finding into the
lock file, which is what a pre-commit or CI check wants; the default is
read-only so you can ask without changing anything.

## What not to put in the contract

The contract is the interface, not the plan. Implementation notes, TODOs, and
reasoning belong in your own files. Every byte in the contract is read by an
agent on the other side that has no context for it.

## What not to put in a message instead

The mirror-image mistake, and the more common one: agreeing to something in
conversation and never writing it down. A delegated run starts with an empty
history — the next backend run, including yours after a restart, knows only
what the contract says. If it is not in the contract, it did not happen.
