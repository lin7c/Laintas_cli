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

## Inbound messages

Every inbound text message is handed to the Agent through the extension
backend gateway, and the generated reply is sent back to the same chat. Group
messages are included and tagged `isGroup`.

## When pairing does not work

`/whatsapp status` prints the sidecar's log path. That file holds WhatsApp's
own account of the attempt -- `not logged in, attempting registration`,
`logging in...`, `pair success recv`, `error in pairing` -- which is what
distinguishes a code that never reached WhatsApp from one that was rejected.
Set `WA_LOG_LEVEL=trace` for the full protocol exchange.

If the phone *rejects* the code, the pairing reached WhatsApp and the companion
registration was refused. Which client identity WhatsApp accepts is its
decision, so that identity is configurable -- try another before assuming the
gateway is broken:

```
WA_BROWSER=macos WA_BROWSER_CLIENT=Safari laintas-cli
WA_BROWSER=windows laintas-cli
```

`/whatsapp status` shows the identity in use. Default is Ubuntu / Chrome.

## Notes

- `bridge/.auth/` holds live WhatsApp credentials. It is excluded from the
  published archive and from the trust hash; never copy it between machines.
- The gateway never starts on its own. Loading the extension registers the
  command and the tools, nothing more.
