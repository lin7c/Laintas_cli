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

## Where you message it

**There is no `laintas-cli` contact.** The CLI is a linked *device* of your
account, like WhatsApp Web — not somebody you message. You talk to it in your
account's chat with itself.

You should not have to hunt for it: on connecting, the gateway sends one message
into that chat, which creates it and puts it at the top of your chat list.
`/whatsapp hello` reopens it, `/whatsapp status` names it.

## What it runs

Messages go to the **agent**, not to a chat model — the same loop `--execute`
uses, with tools. It can read files, run commands, search, edit code.

**What a task may do is not decided here.** It comes from the active mode,
exactly as `--execute` takes it. The mode decides what execution may touch; this
extension only decides who may ask. To widen or narrow it, change the mode —
not this extension.

## Who may ask

Only your own chat. The CLI is a linked device of your account, so that chat is
writable by you alone, and a message there is from the person who owns the
machine.

Every other chat is ignored outright — not answered, not executed. A message
saying "run this" from someone else's phone is not a capability this extension
has.

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
