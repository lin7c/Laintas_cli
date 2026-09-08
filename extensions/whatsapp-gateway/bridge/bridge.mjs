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
import fsSync, { promises as fs } from 'node:fs';
import path from 'node:path';
import QRCode from 'qrcode';
import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';

/* A paired session is a credential, not a cache. Anything that can read
 * AUTH_DIR can send as this WhatsApp account and read every message it
 * receives -- no second factor, no re-pairing. `useMultiFileAuthState` writes
 * with the ambient umask, which on a default install means world-readable
 * (0644) keys. Clamp it before anything is written; the CLI holds its own
 * session.json at 0600 for the same reason. */
process.umask(0o077);

const AUTH_DIR = process.env.WA_AUTH_DIR || path.resolve(process.cwd(), '.auth');
const HTTP_PORT = parseInt(process.env.WA_HTTP_PORT || '8765', 10);
const HOST = process.env.WA_HOST || '127.0.0.1';
/** How many consecutive ports to try before giving up on the QR page. */
const PORT_ATTEMPTS = 10;
/** Reconnect backoff bounds, in ms. */
const RECONNECT_MIN_MS = 2000;
const RECONNECT_MAX_MS = 60000;
/** How long each pairing ref stays valid.
 *
 * Baileys walks a fixed list of pairing refs, ending the connection with
 * `timedOut` once they run out -- at the stock 60s + 5x20s that is under three
 * minutes to fetch a phone, find Linked devices and type eight characters. Any
 * code still being typed when the socket dies is already dead. Six refs at
 * three minutes each is a realistic window. */
const PAIRING_WINDOW_MS = parseInt(process.env.WA_PAIRING_WINDOW_MS || '180000', 10);

/** How this client identifies itself to WhatsApp.
 *
 * These exact three values are the only combination observed to complete a
 * pairing against a real account. Two others did not:
 *
 *   ['laintas',     'Chrome',  '22']       -> never paired
 *   ['Ubuntu',      'Chrome',  '22.04.4']  -> PAIRED
 *   ['laintas-cli', 'Desktop', '22.04.4']  -> never paired
 *
 * Only registration carries them (`generateRegistrationNode` puts slot [0] in
 * `os` and slot [1] through `getPlatformType`); a reconnect sends neither, so
 * this affects pairing and nothing else. That is precisely why it is easy to
 * get wrong: changing it looks harmless right up until somebody needs to pair.
 *
 * Slot [0] is also the name shown under Linked devices, so "laintas-cli" there
 * is tempting -- it was tried in 1.2.0 and pairing stopped working. The
 * evidence does not separate the name from the platform type, and settling
 * that needs a real phone for every attempt, so the default stays on the
 * combination known to work and the override is left for anyone willing to
 * risk a failed pairing to get a nicer name:
 *   WA_DEVICE_NAME, WA_PLATFORM, WA_OS_VERSION
 */
const DEVICE_NAME = process.env.WA_DEVICE_NAME || 'Ubuntu';
const PLATFORM = process.env.WA_PLATFORM || 'Chrome';
const OS_VERSION = process.env.WA_OS_VERSION || '22.04.4';
const BROWSER = [DEVICE_NAME, PLATFORM, OS_VERSION];

let latestQR = null;        // { png, ts }
let connectionState = 'connecting';
let socket = null;
let httpPort = HTTP_PORT;
let pendingPairingPhone = null;   // set by a 'pairing' inbound command
let pairingRequested = false;     // guard: request the code once per socket
let lastPairingCode = null;       // so a replacement code can name what it kills
let reconnectDelay = RECONNECT_MIN_MS;
let reconnectTimer = null;
/** Consecutive reconnects that never reached `open`.
 *
 *  An unbounded retry loop is not resilience: each attempt that re-registers
 *  is a new device request to WhatsApp, and enough of them is what the server
 *  answers with `Connection Terminated by Server`. Stop, and say so, rather
 *  than hammering the account. */
let failedConnects = 0;
const MAX_FAILED_CONNECTS = 6;
let shuttingDown = false;
/** Monotonic id of the live socket. A late event from a replaced socket is
 *  ignored rather than being allowed to schedule a second reconnect chain. */
let socketGeneration = 0;
/** Ids of messages this bridge sent itself.
 *
 *  In the self-chat the Agent's own reply comes straight back through
 *  `messages.upsert` as another `fromMe` message in the same conversation.
 *  Without remembering what we sent, answering it would answer ourselves, for
 *  ever. Bounded so a long-running session does not grow without limit. */
const ownMessageIds = new Set();
const OWN_ID_MEMORY = 500;
/** Only messages newer than this are acted on. A fresh pairing replays
 *  history, and a self-chat full of old notes must not be re-run as a queue of
 *  instructions. */
const startedAt = Math.floor(Date.now() / 1000);

function rememberOwnMessage(id) {
  if (!id) return;
  ownMessageIds.add(id);
  if (ownMessageIds.size > OWN_ID_MEMORY) {
    ownMessageIds.delete(ownMessageIds.values().next().value);
  }
}

/** The account's own chat -- "Message yourself" in WhatsApp.
 *
 *  This is the conversation the user talks to the CLI in, so it is the one
 *  place where a `fromMe` message is an instruction rather than an echo of
 *  something they said to somebody else. */
function isSelfChat(remoteJid) {
  const own = socket?.user?.id;
  if (!own || !remoteJid) return false;
  return String(remoteJid).split('@')[0] === String(own).split(':')[0].split('@')[0];
}

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

const logger = makeLogger(process.env.WA_LOG_LEVEL || 'info');

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
      .then(sent => {
        rememberOwnMessage(sent?.key?.id);
        emit({ type: 'send_result', reqId, ok: true, jid });
      })
      .catch(e => emit({ type: 'send_result', reqId, ok: false, error: e.message }));
    return;
  }

  if (obj.type === 'pairing') {
    // Pairing-code mode: remember the phone. Request the code once the WS is
    // ready (handled by the connection.update QR branch, or now if it is up).
    const phone = String(obj.phone || '').replace(/\D/g, '');
    if (!phone) {
      emit({ type: 'status', state: 'pairing_error', reason: 'no phone number provided' });
      return;
    }
    const switchingMode = !pendingPairingPhone || pendingPairingPhone !== phone;
    pendingPairingPhone = phone;
    pairingRequested = false;
    if (socket && switchingMode) {
      // The live socket was built for QR mode, with the short ref window that
      // implies. Rebuild it so the code gets the full pairing window rather
      // than whatever is left of a QR rotation.
      connect({ allowReset: true })         // a new pairing starts clean
        .catch(e => emit({ type: 'status', state: 'pairing_error',
                           reason: e.message }));
    } else if (socket) {
      requestPairingCode();
    }
    return;
  }

  if (obj.type === 'logout') {
    logout().catch(e => emit({ type: 'error', message: `Logout failed: ${e.message}` }));
    return;
  }

  if (obj.type === 'ping') {
    emit({ type: 'pong', state: connectionState, httpPort, browser: BROWSER });
  }
}

/* Request an 8-char pairing code for pendingPairingPhone (guard: once per socket). */
async function requestPairingCode() {
  if (!socket || !pendingPairingPhone || pairingRequested) return;
  pairingRequested = true;
  try {
    const code = await socket.requestPairingCode(pendingPairingPhone);
    // A code only lives as long as the socket that issued it. Say so, and say
    // whether it replaces one the user may still be holding -- a silently
    // superseded code is indistinguishable from a code that does not work.
    emit({
      type: 'pairing_code',
      code,
      phone: pendingPairingPhone,
      expiresInSeconds: Math.round((PAIRING_WINDOW_MS * 6) / 1000),
      supersedes: lastPairingCode,
    });
    lastPairingCode = code;
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
  note(`identifying to WhatsApp as ${BROWSER.join(' / ')}`);
  emit({ type: 'status', state: 'listening', httpPort, browser: BROWSER });
  await connect({ allowReset: true });      // clear anything left by last run
}

function scheduleReconnect() {
  if (shuttingDown || reconnectTimer) return;
  failedConnects += 1;
  if (failedConnects > MAX_FAILED_CONNECTS) {
    emit({
      type: 'status',
      state: 'gave_up',
      reason: `${MAX_FAILED_CONNECTS} connection attempts in a row did not `
        + 'complete; not retrying so the account is not hammered',
      needsPairing: true,
    });
    note('giving up after repeated failed connections');
    return;
  }
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

/** Set aside the credentials of a session the server has revoked.
 *
 * Deleting them outright was wrong twice over. It is unrecoverable if the 401
 * was ever a misjudgement, and it destroys the only evidence of what happened
 * -- which is what a revoked session looks like from the user's side: the
 * device silently stops working and the gateway is quietly back at the pairing
 * screen. One generation is kept; it is a credential, so 0700. */
async function archiveAuthDir() {
  const archive = `${AUTH_DIR}.revoked`;
  try {
    await fs.rm(archive, { recursive: true, force: true });
    await fs.rename(AUTH_DIR, archive);
    await fs.chmod(archive, 0o700).catch(() => {});
    note(`revoked session moved to ${archive}`);
  } catch {
    // Renaming is best effort; the session is unusable either way.
    await fs.rm(AUTH_DIR, { recursive: true, force: true }).catch(() => {});
  }
}

async function logout() {
  shuttingDown = false;
  retireSocket();
  await archiveAuthDir();
  latestQR = null;
  pairingRequested = false;
  pendingPairingPhone = null;
  lastPairingCode = null;
  emit({ type: 'status', state: 'needs_rescan', reason: 'auth cleared' });
  reconnectDelay = RECONNECT_MIN_MS;
  failedConnects = 0;
  await connect();
}

/** True when the stored credentials are from a pairing that never finished.
 *
 * `requestPairingCode` writes `creds.me` immediately, before the user has
 * typed anything. Baileys then branches on that field alone -- `creds.me` set
 * means "log in as this account" -- so the next connection tries to log in
 * with a pairing that was never completed, WhatsApp answers 401, and the
 * session is torn down as `loggedOut`. Requesting a fresh code re-arms the
 * same trap, which is why pairing appeared to fail every time: each attempt
 * poisoned the next one.
 *
 * The marker is `account`, not `registered`. Only a pairing the server
 * confirmed carries `account` and `signalIdentities`, written together by
 * `configureSuccessfulPairing` for BOTH pairing routes. `registered` is set
 * only along the link-code path (Socket/messages-recv.js), so testing it
 * would classify a perfectly good QR-paired session as abandoned and delete
 * it on the next start. */
function isAbandonedPairing(creds) {
  return Boolean(creds && creds.me && !creds.account);
}

/** Bring an existing auth directory up to 0700/0600.
 *
 * The umask above only governs files created from now on. A session paired by
 * an earlier build still has its keys at 0644 on disk, and it is exactly the
 * session worth protecting -- it is the one that works. */
async function secureAuthDir() {
  try {
    await fs.chmod(AUTH_DIR, 0o700);
  } catch {
    return;                      // not created yet; the umask covers it
  }
  try {
    for (const name of await fs.readdir(AUTH_DIR)) {
      await fs.chmod(path.join(AUTH_DIR, name), 0o600).catch(() => {});
    }
  } catch { /* nothing readable to tighten */ }
}

/** @param allowReset only true when a NEW pairing is being started.
 *
 *  Discarding an unfinished pairing on every connect was a self-destroying
 *  loop, and the reason pairing kept failing. `requestPairingCode` writes
 *  `creds.me` the moment a code is issued, so from then until the user has
 *  typed it the credentials legitimately look "unfinished". Any reconnect in
 *  that window -- and WhatsApp was closing the socket every 30-90s -- deleted
 *  the pairing the user was in the middle of, registered a brand new device,
 *  and issued a different code. The code in their hand was dead before they
 *  could type it, every time, and the stream of fresh registrations is what
 *  drew `Connection Terminated by Server`.
 *
 *  So the reset belongs to starting a pairing, not to reconnecting. A
 *  reconnect that finds genuinely dead credentials still recovers: the login
 *  is refused, and the loggedOut branch archives them once, with a message. */
async function connect({ allowReset = false } = {}) {
  retireSocket();
  // Re-read the auth state on every attempt: after a logout wipe, the previous
  // in-memory state describes credentials that no longer exist on disk.
  let { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  await secureAuthDir();
  if (allowReset && isAbandonedPairing(state.creds)) {
    note('discarding credentials from an unfinished pairing');
    await fs.rm(AUTH_DIR, { recursive: true, force: true });
    ({ state, saveCreds } = await useMultiFileAuthState(AUTH_DIR));
  }
  const generation = ++socketGeneration;

  connectionState = 'connecting';
  pairingRequested = false;
  socket = makeWASocket({
    auth: state,
    logger,
    // Only stretch the window in pairing-code mode. `qrTimeout` is how long
    // each ref is held, and in QR mode that is how long one image stays on the
    // page -- stretching it there would leave a QR on screen long after
    // WhatsApp stopped honouring it. In pairing mode nothing is displayed and
    // the refs only bound how long the socket survives, which is exactly the
    // time the user needs to type the code.
    ...(pendingPairingPhone ? { qrTimeout: PAIRING_WINDOW_MS } : {}),
    browser: BROWSER,
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
      pendingPairingPhone = null;
      lastPairingCode = null;
      reconnectDelay = RECONNECT_MIN_MS;
      failedConnects = 0;
      emit({ type: 'status', state: 'open', reason: 'connected' });
      return;
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      connectionState = 'closed';
      if (code === DisconnectReason.loggedOut) {
        // The device was unlinked on the phone (or the server rejected the
        // login for good). Terminal for this session, not for the bridge:
        // archive the dead credentials and come back on a fresh QR. Say WHY --
        // the previous wording read like a transient state, so an unlinked
        // device just looked like the gateway had stopped working.
        emit({
          type: 'status',
          state: 'logged_out',
          reason: 'this device is no longer linked to the WhatsApp account',
          statusCode: code,
          needsPairing: true,
        });
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
      const text = msgText(msg);
      if (!text) continue;                            // ignore non-text for now
      const remoteJid = cleanJid(msg.key.remoteJid);
      if (!remoteJid || remoteJid === 'status@broadcast') continue;

      const id = msg.key.id || `${Date.now()}-${Math.random()}`;
      const selfChat = isSelfChat(remoteJid);

      // `fromMe` is normally an echo of something the user typed to someone
      // else and must be ignored -- except in their own chat, which is where
      // they talk to the CLI. There, our own replies come back too, so skip
      // the ones we just sent or the Agent answers itself for ever.
      if (msg.key.fromMe) {
        if (!selfChat || ownMessageIds.has(id)) continue;
      }

      // A new pairing replays history; old notes are not new instructions.
      const ts = Number(msg.messageTimestamp || 0);
      if (ts && ts < startedAt - 60) continue;

      const isGroup = remoteJid.endsWith('@g.us');
      const from = cleanJid(msg.key.participant || msg.key.remoteJid);
      const name = msg.pushName || String(from).split('@')[0];
      emit({ type: 'message', id, remoteJid, from, name, text, isGroup, selfChat });
    }
  });
}

process.stdin.setEncoding('utf-8');
process.stdin.on('data', onStdinChunk);

/** Treat end-of-stdin as "the parent is gone" ONLY when stdin is the pipe the
 *  parent gave us.
 *
 *  Exiting on a bare 'end' looked right and was not: run the sidecar by hand,
 *  or under anything that wires stdin to /dev/null, and EOF arrives
 *  immediately -- racing the connection and killing the process before it
 *  reaches WhatsApp. The symptom was a bridge that printed `listening` and
 *  then nothing at all, intermittently, depending on which won the race. */
let parentPipe = false;
try {
  parentPipe = fsSync.fstatSync(0).isFIFO();
} catch {
  parentPipe = false;      // no stdin to speak of; nothing to watch
}
if (parentPipe) {
  process.stdin.on('end', () => {
    shuttingDown = true;
    process.exit(0);
  });
} else {
  note('stdin is not a parent pipe; running without the orphan guard');
}

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
