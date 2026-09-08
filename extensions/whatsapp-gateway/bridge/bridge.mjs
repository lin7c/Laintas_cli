/**
 * WhatsApp gateway bridge (Baileys).
 * Runs as a child process of the laintas extension main.py.
 *
 * Responsibilities:
 *  - Maintain a WhatsApp Web connection with persistent auth (pair once).
 *  - Two pairing modes: QR code (default) or 8-char pairing code.
 *  - Host a local HTTP page that shows the QR code for pairing.
 *  - Forward inbound messages to parent via stdout JSON-lines.
 *  - Accept outbound replies via stdin JSON-lines and send them back.
 *
 * stdout is the IPC channel and NOTHING else may write to it. Baileys' default
 * logger is pino, which writes to stdout — it would interleave protocol noise
 * with the frames below. Every socket therefore gets the stderr logger built in
 * `makeLogger()`, and diagnostics go to stderr where the parent logs them.
 *
 * IPC protocol (JSON-lines, one object per line):
 *   Node -> Python (stdout):
 *     {"type":"qr","payload":"<base64 png>","url":"<wa link>"}
 *     {"type":"pairing_code","code":"ABCDEFGH","phone":"8613800138000"}
 *     {"type":"status","state":"listening","httpPort":8765}
 *     {"type":"status","state":"connecting|open|closed|logged_out|needs_rescan|pairing_error","reason":"..."}
 *     {"type":"message","id":"<msg id>","remoteJid":"...","from":"...","name":"...","text":"...","isGroup":bool}
 *     {"type":"send_result","reqId":"...","ok":bool,"error":"..."}
 *     {"type":"error","message":"..."}
 *   Python -> Node (stdin):
 *     {"type":"reply","reqId":"...","jid":"<jid>","text":"..."}
 *     {"type":"send","reqId":"...","to":"<jid>","text":"..."}
 *     {"type":"pairing","phone":"<number>"}
 *     {"type":"logout"}
 *     {"type":"ping"}
 */
import http from 'node:http';
import { URL } from 'node:url';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import QRCode from 'qrcode';
import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';

const AUTH_DIR = process.env.WA_AUTH_DIR || path.resolve(process.cwd(), '.auth');
const HTTP_PORT = parseInt(process.env.WA_HTTP_PORT || '8765', 10);
const HOST = process.env.WA_HOST || '127.0.0.1';
/** How many consecutive ports to try before giving up on the QR page. */
const PORT_ATTEMPTS = 10;
/** Reconnect backoff bounds, in ms. */
const RECONNECT_MIN_MS = 2000;
const RECONNECT_MAX_MS = 60000;

let latestQR = null;        // { png, ts }
let connectionState = 'connecting';
let socket = null;
let httpPort = HTTP_PORT;
let pendingPairingPhone = null;   // set by a 'pairing' inbound command
let pairingRequested = false;     // guard: request the code once per socket
let reconnectDelay = RECONNECT_MIN_MS;
let reconnectTimer = null;
let shuttingDown = false;
/** Monotonic id of the live socket. A late event from a replaced socket is
 *  ignored rather than being allowed to schedule a second reconnect chain. */
let socketGeneration = 0;

/* ---------------- IPC to parent (Python) ---------------- */
function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function note(message) {
  process.stderr.write(String(message) + '\n');
}

/* ---------------- logger ----------------
 * Baileys defaults to a pino instance bound to stdout, which is this process's
 * IPC channel. Handing it an explicit stderr logger is what keeps the channel
 * parseable; it is not a verbosity preference. */
function makeLogger(level = 'error') {
  const rank = { trace: 10, debug: 20, info: 30, warn: 40, error: 50, fatal: 60, silent: 100 };
  const logger = {
    level,
    child() { return logger; },
  };
  for (const name of ['trace', 'debug', 'info', 'warn', 'error', 'fatal']) {
    logger[name] = (...args) => {
      if ((rank[name] || 0) < (rank[logger.level] || 50)) return;
      try {
        note(`[baileys:${name}] ${args.map(
          value => (typeof value === 'string' ? value : JSON.stringify(value))).join(' ')}`);
      } catch { /* a value that will not serialise is not worth a crash */ }
    };
  }
  return logger;
}

const logger = makeLogger(process.env.WA_LOG_LEVEL || 'error');

/* ---------------- HTTP page: shows QR or status ---------------- */
function htmlPage() {
  const title = 'WhatsApp Gateway';
  if (connectionState === 'open') {
    return `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title></head>
      <body style="font-family:sans-serif;text-align:center;padding-top:60px">
        <h1>&#9989; Paired</h1><p>WhatsApp session is active. You can close this page. The Agent is now listening for messages.</p></body></html>`;
  }
  const qrImg = latestQR
    ? `<img src="/qr.png?ts=${latestQR.ts}" style="width:260px;height:260px;image-rendering:pixelated" alt="QR">`
    : '<p>Generating QR code...</p>';
  return `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title>
    <meta http-equiv="refresh" content="2"></head>
    <body style="font-family:sans-serif;text-align:center;padding-top:40px">
      <h1>Scan with WhatsApp to connect</h1>
      <p>Open WhatsApp on your phone &rarr; Settings &rarr; Linked devices &rarr; scan the QR code below</p>
      ${qrImg}
      <p style="color:#888">The QR code refreshes automatically every ~2 seconds. The page will update automatically once scanned.</p></body></html>`;
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${HOST}:${httpPort}`);
  if (url.pathname === '/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(htmlPage());
  } else if (url.pathname === '/qr.png' && latestQR) {
    res.writeHead(200, {
      'Content-Type': 'image/png',
      'Cache-Control': 'no-store',
    });
    res.end(Buffer.from(latestQR.png, 'base64'));
  } else {
    res.writeHead(404); res.end('Not found');
  }
});

/** Bind the QR page, walking forward past ports already in use.
 *
 * `server.listen` reports EADDRINUSE through the server's 'error' EVENT, which
 * no promise rejection handler can see. Left unhandled it is a fatal uncaught
 * exception: a second CLI session, or one leftover bridge, killed the whole
 * gateway at startup with nothing on stdout to say why. */
function listenOnFreePort() {
  return new Promise((resolve, reject) => {
    let attempt = 0;

    const onError = (err) => {
      if (err && err.code === 'EADDRINUSE' && attempt < PORT_ATTEMPTS - 1) {
        attempt += 1;
        httpPort = HTTP_PORT + attempt;
        note(`port ${httpPort - 1} in use, trying ${httpPort}`);
        setTimeout(tryListen, 0);
        return;
      }
      server.removeListener('error', onError);
      reject(err);
    };

    const tryListen = () => server.listen(httpPort, HOST);

    server.on('error', onError);
    server.once('listening', () => {
      server.removeListener('error', onError);
      // Past startup a socket error must not be fatal either.
      server.on('error', (err) => note(`http server error: ${err.message}`));
      resolve(httpPort);
    });
    tryListen();
  });
}

/* ---------------- stdin framing ----------------
 * 'data' arrives in pipe-sized chunks with no regard for line boundaries, so a
 * frame can straddle two chunks. Splitting each chunk independently turned such
 * a frame into two halves that both failed JSON.parse and were both silently
 * dropped — a long reply would simply never be sent. Hold the partial line. */
let stdinBuffer = '';

function onStdinChunk(chunk) {
  stdinBuffer += chunk;
  let index;
  while ((index = stdinBuffer.indexOf('\n')) >= 0) {
    const line = stdinBuffer.slice(0, index);
    stdinBuffer = stdinBuffer.slice(index + 1);
    processInbound(line);
  }
  if (stdinBuffer.length > 8 * 1024 * 1024) {
    note('stdin frame exceeded 8MB without a newline; dropping');
    stdinBuffer = '';
  }
}

function processInbound(line) {
  if (!line.trim()) return;
  let obj;
  try { obj = JSON.parse(line); } catch { return; }

  if (obj.type === 'reply' || obj.type === 'send') {
    const jid = obj.jid || obj.remoteJid || obj.to;
    const reqId = obj.reqId || '';
    if (!socket || connectionState !== 'open') {
      emit({ type: 'send_result', reqId, ok: false,
             error: `not connected (state=${connectionState})` });
      return;
    }
    if (!jid) {
      emit({ type: 'send_result', reqId, ok: false, error: 'no recipient jid' });
      return;
    }
    socket.sendMessage(jid, { text: String(obj.text ?? '') })
      .then(() => emit({ type: 'send_result', reqId, ok: true, jid }))
      .catch(e => emit({ type: 'send_result', reqId, ok: false, error: e.message }));
    return;
  }

  if (obj.type === 'pairing') {
    // Pairing-code mode: remember the phone. Request the code once the WS is
    // ready (handled by the connection.update QR branch, or now if it is up).
    const phone = String(obj.phone || '').replace(/\D/g, '');
    if (!phone) {
      emit({ type: 'status', state: 'pairing_error', reason: 'no phone number provided' });
    } else {
      pendingPairingPhone = phone;
      pairingRequested = false;
      if (socket) requestPairingCode();
    }
    return;
  }

  if (obj.type === 'logout') {
    logout().catch(e => emit({ type: 'error', message: `Logout failed: ${e.message}` }));
    return;
  }

  if (obj.type === 'ping') {
    emit({ type: 'pong', state: connectionState, httpPort });
  }
}

/* Request an 8-char pairing code for pendingPairingPhone (guard: once per socket). */
async function requestPairingCode() {
  if (!socket || !pendingPairingPhone || pairingRequested) return;
  pairingRequested = true;
  try {
    const code = await socket.requestPairingCode(pendingPairingPhone);
    emit({ type: 'pairing_code', code, phone: pendingPairingPhone });
    connectionState = 'awaiting_pairing';
  } catch (e) {
    emit({ type: 'status', state: 'pairing_error', reason: e.message });
    pairingRequested = false;
  }
}

/* ---------------- extract readable text from a message ---------------- */
function msgText(msg) {
  let c = msg.message || {};
  // Disappearing messages and view-once wrap the real payload one level down.
  c = c.ephemeralMessage?.message
    || c.viewOnceMessage?.message
    || c.viewOnceMessageV2?.message
    || c;
  return c.conversation
    || c.extendedTextMessage?.text
    || c.imageMessage?.caption
    || c.videoMessage?.caption
    || c.documentMessage?.caption
    || c.buttonsResponseMessage?.selectedDisplayText
    || c.listResponseMessage?.title
    || null;
}

function cleanJid(jid) {
  if (!jid) return jid;
  const [user, domain] = String(jid).split('@');
  if (!domain) return jid;
  return `${user.split(':')[0]}@${domain}`;   // strip device suffix user:22@…
}

/* ---------------- WhatsApp connection ---------------- */
async function start() {
  await listenOnFreePort();
  emit({ type: 'status', state: 'listening', httpPort });
  await connect();
}

function scheduleReconnect() {
  if (shuttingDown || reconnectTimer) return;
  const delay = reconnectDelay;
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
  note(`reconnecting in ${delay}ms`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect().catch(e => {
      emit({ type: 'error', message: `Reconnect failed: ${e.message}` });
      scheduleReconnect();
    });
  }, delay);
}

/** Drop the previous socket for good before a new one is built.
 *
 * Reassigning the global alone left the old socket's listeners live: it could
 * still fire 'close' and start a second reconnect chain, and every reconnect
 * added another. Detach the listeners and end the websocket. */
function retireSocket() {
  const old = socket;
  socket = null;
  if (!old) return;
  try { old.ev.removeAllListeners('connection.update'); } catch { /* already gone */ }
  try { old.ev.removeAllListeners('creds.update'); } catch { /* already gone */ }
  try { old.ev.removeAllListeners('messages.upsert'); } catch { /* already gone */ }
  try { old.end(undefined); } catch { /* already closed */ }
}

async function logout() {
  shuttingDown = false;
  retireSocket();
  await fs.rm(AUTH_DIR, { recursive: true, force: true });
  latestQR = null;
  pairingRequested = false;
  emit({ type: 'status', state: 'needs_rescan', reason: 'auth cleared' });
  reconnectDelay = RECONNECT_MIN_MS;
  await connect();
}

async function connect() {
  retireSocket();
  // Re-read the auth state on every attempt: after a logout wipe, the previous
  // in-memory state describes credentials that no longer exist on disk.
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const generation = ++socketGeneration;

  connectionState = 'connecting';
  pairingRequested = false;
  socket = makeWASocket({
    auth: state,
    logger,
    browser: ['laintas', 'Chrome', '22'],
  });

  socket.ev.on('creds.update', saveCreds);

  socket.ev.on('connection.update', (update) => {
    if (generation !== socketGeneration) return;   // event from a retired socket
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Pairing-code mode requested: ask for the code instead of showing a QR.
      if (pendingPairingPhone) {
        requestPairingCode();
        return;
      }
      connectionState = 'awaiting_scan';
      QRCode.toDataURL(qr, { width: 260, margin: 1 })
        .then(dataUrl => {
          const base64 = dataUrl.split(',')[1];
          latestQR = { png: base64, ts: Date.now() };
          emit({ type: 'qr', payload: base64, url: qr });
        })
        .catch(e => emit({ type: 'error', message: `QR generation failed: ${e.message}` }));
      return;
    }

    if (connection === 'open') {
      connectionState = 'open';
      latestQR = null;
      reconnectDelay = RECONNECT_MIN_MS;
      emit({ type: 'status', state: 'open', reason: 'connected' });
      return;
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      connectionState = 'closed';
      if (code === DisconnectReason.loggedOut) {
        // Terminal for this session, but not for the bridge: wipe the dead
        // credentials and come back on a fresh QR. Exiting here used to leave
        // a live process that could never pair again, while `/whatsapp status`
        // still reported it as running.
        emit({ type: 'status', state: 'logged_out', reason: 'account logged out, rescan required' });
        logout().catch(e => {
          emit({ type: 'error', message: `Re-pair failed: ${e.message}` });
          scheduleReconnect();
        });
        return;
      }
      emit({ type: 'status', state: 'closed', reason: `code=${code}` });
      scheduleReconnect();
    }
  });

  socket.ev.on('messages.upsert', async ({ messages, type }) => {
    if (generation !== socketGeneration) return;
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (msg.key?.fromMe) continue;                 // ignore our own
      const text = msgText(msg);
      if (!text) continue;                            // ignore non-text for now
      const remoteJid = cleanJid(msg.key.remoteJid);
      if (!remoteJid || remoteJid === 'status@broadcast') continue;
      const isGroup = remoteJid.endsWith('@g.us');
      const id = msg.key.id || `${Date.now()}-${Math.random()}`;
      const from = cleanJid(msg.key.participant || msg.key.remoteJid);
      const name = msg.pushName || String(from).split('@')[0];
      emit({ type: 'message', id, remoteJid, from, name, text, isGroup });
    }
  });
}

process.stdin.setEncoding('utf-8');
process.stdin.on('data', onStdinChunk);
process.stdin.on('end', () => {
  // The parent went away; do not linger as an orphan holding the port.
  shuttingDown = true;
  process.exit(0);
});

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    shuttingDown = true;
    retireSocket();
    try { server.close(); } catch { /* not listening */ }
    process.exit(0);
  });
}

process.on('uncaughtException', (err) => {
  emit({ type: 'error', message: `Uncaught: ${err.message}` });
  note(err.stack || String(err));
});
process.on('unhandledRejection', (err) => {
  emit({ type: 'error', message: `Unhandled rejection: ${err?.message || err}` });
});

start().catch(e => {
  emit({ type: 'error', message: `Bridge failed to start: ${e.message}` });
  note(e.stack || String(e));
  process.exit(1);
});
