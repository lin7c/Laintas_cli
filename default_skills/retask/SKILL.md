---
name: retask
description: Reverse tasks — hand the person a checklist (.retask) for work only they can do (dashboards, payments, credentials, accounts, exercises), then check each task when they say it is done. Load when a goal needs the person's own hands, or when a <retask> block is present.
version: 1.0.0
triggers:
  - retask
  - reverse task
  - walk me through
  - guide me
  - step by step
  - integrate
  - exercise
  - quiz
  - homework
  - practice
---

# Reverse tasks (.retask)

A `.retask` file is a checklist of work YOU hand to the PERSON. They see it with
`/retask` or Alt+R. Every turn you get a `<retask>` block with every title and
the current task in full, rebuilt from the file — so the list, not the
conversation, is where progress lives.

## When to make one

Only when the goal needs steps that only the person can do:

- **Integrations** — register a merchant account, pass verification, create API
  keys, set a callback URL in a dashboard, make a sandbox payment.
- **Accounts and credentials** — sign in, approve access, rotate a key.
- **Exercises and learning** — questions they must answer themselves.

Never for work you can do yourself, and never for one instruction that fits in a
sentence. Your own steps (writing the code, running tests) are NOT tasks in the
list: you do them between the person's tasks.

## Writing tasks

`retask.create({title, goal, tasks:[{title, description, checks, after}]})`

Each task is ONE action. Its description must let them act without asking:

1. **Where** — the URL and the menu path (`Product Center → Dev Settings`).
2. **What** — exactly what to click or type.
3. **Where the result goes** — a text file in the workspace (`.env`,
   `answers/q1.md`, `notes/merchant.md`).
4. **Done when** — in plain words, matching the checks.

Descriptions are text for a terminal: no images or videos. Write the menu path
and the exact labels instead of pointing at a picture.

Checks (paths relative to the list file):

| check | use |
|---|---|
| `file_exists notes/merchant.md` | they wrote the result down |
| `contains .env "WECHAT_MCH_ID="` | a value is in place — name the KEY, never the secret |
| `matches answers/q2.md "\b42\b"` | an answer with a known form |
| `min_length answers/q3.md 200` | a written answer of real length |
| `review "the answer explains why the index is needed"` | a judgement you make by reading |

Rules:
- Never ask the person to paste a secret into the chat. Secrets go into `.env`;
  check the variable name with `contains`.
- Evidence is a text FILE in the workspace or a command you can run yourself
  (`curl` the callback URL, query the sandbox order).
- Use `after` for real order only.
- Exercises: the questions go in the description; never put the answer
  anywhere the person reads.

## Checking

When the person says a task is done (or its status is `submitted`):

1. Look at the evidence yourself: `fs.read`, or run the command that proves it.
2. `retask.update({id, status:"done", note:"<what you saw>"})`. The file checks
   run then; a failure makes the task `rejected` and returns the gaps.
3. Rejected: tell them exactly what is missing and what to do — one or two
   sentences. The list already shows the note.

A `review` check needs a note naming the evidence. Do not pass something you
could not verify — say what you need instead.

## When the person is stuck and asks

The question is about the current task in `<retask>` unless they say otherwise.

- Ask where exactly they are in the description, not "what happened".
- Ask them to save the error text or log INTO A FILE in the workspace and read
  it, instead of having them paste it.
- Dashboards change: `web.search` the provider's current docs before describing
  a menu from memory.
- Exercises: hints in steps (concept → approach → partial), never the answer.
- Same task stuck twice: the description is the problem. Rewrite it
  (`retask.update {id, description}`) or split it (`add_tasks` + `after`).
- Long troubleshooting that is not the person's step (logs, server config):
  delegate it (`agent.spawn`) so this thread stays on the task.

## Keeping the conversation small

- Do not repeat the list in the chat; they have `/retask`. Mention the current
  task and what changed.
- One list per goal. Skip tasks that stopped mattering (`status:"skipped"`)
  rather than leaving a list that never finishes.
