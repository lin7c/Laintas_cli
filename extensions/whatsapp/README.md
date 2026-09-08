# whatsapp

Run laintas-cli from WhatsApp. You message it a task, it **executes** the task,
and it reports back.

```
you   ->  check the disk and tell me what is eating it
         ⏳                                   (reaction: received)
cli   ->  Still working on: check the disk...  (only if it runs long)
         ✅
cli   ->  /dev/vda2 is at 57% (34G of 63G). Biggest: /root/laintas_cli
          node_modules 1.2G, /root/db-backup-2026-08-18 890M.
          ⏱ 34s
```

## How it works

Messages do not go straight to the agent. They go to a **secretary** — an AI
that belongs to this extension — whose job is to work out what the message
actually is:

| you send | secretary does |
|---|---|
| "check the disk" | runs it on the machine, reports back |
| "switch to act mode" | changes the CLI's mode |
| "use opus" | changes the model |
| "what mode are you in?" | answers; runs nothing |
| "thanks" | answers; runs nothing |

That indirection is the design. Not every message is a task — a channel that
assumes otherwise runs "thanks" as a shell command, and can never be asked to
change anything *about* the CLI, because a mode or a model is not work for the
agent, it is work on the agent.

When the answer is "run it", it goes to the agent loop with tools. The
secretary then reports back in its own words: the agent writes for a terminal,
which is unreadable on a phone.

If the secretary cannot be reached, the message is executed rather than
dropped — you asked for something, and doing it is closer to your intent than
silence.

## Where you message it

**There is no `laintas-cli` contact.** The CLI is a linked *device* of your
account, like WhatsApp Web. You talk to it in your account's chat with itself —
the chat titled with **your own name**, not with the CLI's.

On connecting it sends one message there, which creates that chat and puts it at
the top of your list. Reply to it.

## Watching the conversation

Messages are **not** printed into the CLI. That screen belongs to whoever is
sitting at it.

```
/whatsapp            # status, and the last thing said
/whatsapp message    # the conversation, most recent last
/whatsapp message 50 # more of it
```

## Lifecycle

`/whatsapp` reports one word for what is going on:

| phase | meaning | next step |
|---|---|---|
| `stopped` | nothing running | `/whatsapp start` |
| `starting` | up, not at WhatsApp yet | wait |
| `unlinked` | at WhatsApp, no account linked | `/whatsapp pairing <number>` |
| `connected` | linked and usable | message yourself |
| `retrying` | lost the connection, coming back | wait |
| `failed` | gave up | `/whatsapp restart` |

Note that `connected` means *usable*, not "a process exists" — those came apart
often enough to be worth separate words.

`/whatsapp stop` ends it. `/whatsapp restart` restarts it. And if the sidecar is
updated underneath a running one, `/whatsapp start` notices and restarts it for
you rather than telling you to stop and start.

## Setup

Node.js 18+ on `PATH`. First `/whatsapp start` runs `npm install`.

```
/whatsapp start              # link by QR
/whatsapp pairing <number>   # or link by 8-char code
/whatsapp status             # connection, linked account, queue depth
/whatsapp hello              # reopen the chat you message it in
/whatsapp send <num> <text>  # send someone else a message
/whatsapp logout             # forget the linked account
```

## Behaviour worth knowing

- **One task at a time.** Each is a full agent loop that may hold a PTY and run
  commands; extra messages queue rather than interleave over one directory.
- **A long reply is summarised** before sending — the agent writes for a
  terminal, and that is unreadable on a phone. Long messages are chunked at
  4000 characters.
- **Credentials live in `~/.laintas/credentials/whatsapp/`** (0700/0600), *not*
  in the extension directory. Updating or reinstalling the extension does not
  cost you the pairing — it used to, repeatedly.
- **The device shows as `Ubuntu` with a Chrome icon.** That is the only identity
  observed to complete a pairing; `WA_DEVICE_NAME` / `WA_PLATFORM` override it,
  at the risk of a pairing that will not complete. Only registration reads them,
  so a change appears harmless until the next time you link.
- **The sidecar log** is `~/.laintas/credentials/whatsapp.log` — it holds
  WhatsApp's own account of a link attempt, which is what separates a code that
  never arrived from one that was refused.
