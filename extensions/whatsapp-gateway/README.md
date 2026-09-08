# whatsapp-gateway

Pair a WhatsApp account with laintas-cli and let the Agent read and answer
messages.

A Python entry point (`main.py`) owns the slash command, the tools and the
Agent hand-off; a Node sidecar (`bridge/bridge.mjs`) speaks the WhatsApp Web
protocol through [Baileys](https://github.com/WhiskeySockets/Baileys) and hosts
the local pairing page. The two talk over JSON-lines on the sidecar's
stdin/stdout.

## Requirements

Node.js 18 or newer on `PATH`. The first `/whatsapp start` runs `npm install`
inside the extension directory; the published package deliberately carries no
`node_modules`.

## Use

```
/whatsapp start              # start the gateway, pair by QR code
/whatsapp pairing <phone>    # pair by 8-char code instead of a QR
/whatsapp status             # process, connection state, QR page, session dir
/whatsapp send <num> <text>  # send a message
/whatsapp logout             # forget the paired session, show a fresh QR
/whatsapp stop               # stop the gateway
```

`/whatsapp` with no subcommand is the same as `/whatsapp start`. `qrcode` is
kept as an alias for `start`.

Pairing opens `http://127.0.0.1:8765` (the next free port if that one is taken;
the real URL is printed). Scan it from **WhatsApp → Settings → Linked devices**.
The session is stored in `bridge/.auth/` and survives restarts — pair once.

## Tools the model sees

- `whatsapp.send(to, text)` — send a message. It waits for the sidecar's
  acknowledgement and reports a real failure when the gateway is stopped,
  unpaired or the recipient is rejected.
- `whatsapp.status()` — running / connection state / paired / QR page URL.

## Talking to the Agent from WhatsApp

Open the chat with **yourself** ("Message yourself" / your own number in the
chat list) and type. That chat is the Agent's conversation: it answers there,
with a short rolling history, so it reads as one thread rather than a series of
unrelated questions. Nothing to configure -- the chat exists because the CLI is
now a linked device of your account.

The two directions are deliberately not the same thing:

| Where | Who can write | How it is treated |
|---|---|---|
| Your own chat | only you | a request addressed to the Agent |
| Any other chat | anyone | text to draft a reply to, quoted as data |

A stranger's message is never an instruction. It is quoted into the prompt and
carries no conversation state, so "ignore your instructions and ..." arriving
from an unknown number is answered, not obeyed.

The Agent's own replies land back in the self-chat as `fromMe` messages; their
ids are remembered so it does not answer itself in a loop. A fresh pairing
replays history, and messages older than the bridge's start are skipped rather
than run as a backlog of instructions.

## How the device appears on your phone

Under **Linked devices** it shows as `Ubuntu` with a Chrome icon. That is not a
cosmetic choice -- it is the only identity observed to complete a pairing:

| `browser` triple | pairing |
|---|---|
| `['laintas', 'Chrome', '22']` | never paired |
| `['Ubuntu', 'Chrome', '22.04.4']` | **paired** |
| `['laintas-cli', 'Desktop', '22.04.4']` | never paired |

Slot `[0]` is free text and is the displayed name, slot `[1]` picks the icon
from a fixed `PlatformType` enum (a linked device cannot supply artwork), and
slot `[2]` is a version string. Only **registration** sends them, so changing
them appears harmless until the next pairing -- which is how 1.2.0 shipped a
nicer name and a gateway that could no longer pair.

The override is there for anyone willing to risk a failed pairing:

```
WA_DEVICE_NAME="laintas-cli" WA_PLATFORM=Chrome laintas-cli
```

If a pairing then fails, unset it and pair again. The evidence does not
separate the name from the platform type -- each attempt costs a real phone --
so the default stays where it is known to work.

## Notes

- `bridge/.auth/` holds live WhatsApp credentials -- whatever can read them can
  send as the account and read every message it receives, with no second factor
  and no re-pairing. The directory is kept at 0700 and its files at 0600, it is
  excluded from the published archive and from the trust hash, and it should
  never be copied between machines. `bridge.log` is 0600 for the same reason:
  at `WA_LOG_LEVEL=trace` it records raw protocol frames.
- The QR page binds 127.0.0.1 only. It is a pairing aid, not a service, and
  nothing about it should be exposed or proxied.
- The gateway never starts on its own. Loading the extension registers the
  command and the tools, nothing more.
