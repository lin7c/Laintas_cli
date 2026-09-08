"""Extension: whatsapp

Run laintas-cli from WhatsApp. You message the CLI a task, it EXECUTES the
task, and it reports the result back to the same chat.

    phone                  ->  "check the disk and tell me what is eating it"
    laintas-cli            ->  reacts, runs a full agent loop (tools included)
    laintas-cli            ->  sends back a summary of what it did

The distinction that matters: this runs `ctx.tasks`, the agent, not
`ctx.backend`, the model. A version built on the latter can hold a conversation
and cannot do a single thing you ask of it.

Access control is one rule: only the account's own chat is acted on. The CLI is
a linked device of your account, so that chat is writable by you alone, and a
message arriving there is from the person who owns the machine. Everything else
-- other people's chats, groups -- is ignored outright, because "execute this"
arriving from a stranger's phone is not a thing this extension should be able
to do.

What a task is ALLOWED to do is not decided here. It comes from the active mode,
exactly as `--execute` takes it: the mode decides what execution may touch, this
extension only decides who may ask.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
BRIDGE = BASE / "bridge" / "bridge.mjs"
NODE_MODULES = BASE / "node_modules"
BAILEYS = NODE_MODULES / "@whiskeysockets" / "baileys"

#: Credentials live OUTSIDE the extension, under the CLI's own home.
#:
#: They used to sit in the extension directory, where updating or reinstalling
#: the extension destroyed the pairing -- which happened repeatedly and cost a
#: re-pair every time. A paired session outlives any one version of the code
#: that uses it, so it is stored like one.
AUTH_DIR = Path(os.environ.get("WA_AUTH_DIR") or
                (Path.home() / ".laintas" / "credentials" / "whatsapp"))
LOG_FILE = AUTH_DIR.parent / "whatsapp.log"
LOG_MAX_BYTES = 512 * 1024

DEFAULT_HTTP_PORT = int(os.environ.get("WA_HTTP_PORT", "8765"))
NODE_BIN = os.environ.get("WA_NODE", "node")
NPM_BIN = os.environ.get("WA_NPM", "npm")

SEND_TIMEOUT = 20.0
INSTALL_TIMEOUT = 600.0
#: WhatsApp rejects very long messages; chunk below its limit.
CHUNK_LIMIT = 4000
#: Tell the user it is still working if a task outlives this.
PROGRESS_AFTER = 60.0

_proc: subprocess.Popen | None = None
_backend = None
_tasks = None
_console = None
_lock = threading.RLock()

_http_port = DEFAULT_HTTP_PORT
_state = "stopped"
_install_error = ""
_me_jid = ""
_greeted = False
_qr_hinted = False
_last_status = None
_pending_sends: dict[str, dict] = {}

#: Tasks run one at a time. Each is a full agent loop that may hold a PTY and
#: run commands; two interleaving over one working directory is not something
#: a phone message should be able to cause. Extra messages queue rather than
#: race, and the user is told where they are in line.
_task_queue: "queue.Queue[dict]" = queue.Queue()
_worker: threading.Thread | None = None


def setup(ctx) -> None:
    global _backend, _tasks, _console, _worker
    _backend = ctx.backend
    _tasks = ctx.tasks
    _console = ctx.console

    ctx.register_command(
        "/whatsapp",
        _handle_whatsapp,
        description="Run laintas-cli from WhatsApp: message it a task, it executes",
        subcommands=[
            ("start", "Start the gateway (pairs by QR if not linked yet)"),
            ("pairing", "Link by entering an 8-char code: pairing <phone>"),
            ("status", "Connection, linked account, and where to message it"),
            ("hello", "Open the chat you message it in"),
            ("send", "Send a message to someone: send <number> <text>"),
            ("stop", "Stop the gateway"),
            ("logout", "Forget the linked account"),
        ],
    )
    ctx.register_tool(_make_send_tool())
    ctx.register_tool(_make_status_tool())

    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_task_worker, daemon=True)
        _worker.start()

    atexit.register(_stop_bridge)


def teardown() -> None:
    _stop_bridge()


# ----------------------------------------------------------------------
# sidecar lifecycle
# ----------------------------------------------------------------------

def _running() -> bool:
    return _proc is not None and _proc.poll() is None


def _ensure_dependencies() -> tuple[bool, str]:
    """`node` plus the sidecar's npm tree; installed on first use.

    The published package carries no node_modules -- Baileys is thousands of
    files, past the archive limits, and vendoring a dependency tree nobody
    reviewed is not a thing to ship.
    """
    global _install_error
    if shutil.which(NODE_BIN) is None:
        _install_error = (f"Node.js is required but {NODE_BIN!r} is not on PATH. "
                          "Install Node 18+ and run /whatsapp start again.")
        return False, _install_error
    if BAILEYS.is_dir():
        return True, ""
    if shutil.which(NPM_BIN) is None:
        _install_error = (f"Dependencies are missing and {NPM_BIN!r} is not on "
                          f"PATH. Run `npm install` in {BASE}.")
        return False, _install_error
    _log(f"Installing the WhatsApp sidecar's dependencies in {BASE} (first run only)...")
    try:
        result = subprocess.run(
            [NPM_BIN, "install", "--omit=dev", "--no-audit", "--no-fund",
             "--loglevel=error"],
            cwd=str(BASE), capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        _install_error = f"`npm install` timed out after {int(INSTALL_TIMEOUT)}s."
        return False, _install_error
    except OSError as exc:
        _install_error = f"`npm install` could not be run: {exc}"
        return False, _install_error
    if result.returncode != 0 or not BAILEYS.is_dir():
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        _install_error = "`npm install` failed: " + (tail[-1] if tail else
                                                     f"exit {result.returncode}")
        return False, _install_error
    _install_error = ""
    _log("Dependencies installed.")
    return True, ""


def _ensure_bridge() -> tuple[bool, str]:
    global _proc, _qr_hinted, _state, _http_port
    with _lock:
        if _running():
            return True, "already running"
        ok, message = _ensure_dependencies()
        if not ok:
            return False, message

        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(AUTH_DIR, 0o700)
        env = dict(os.environ)
        env["WA_AUTH_DIR"] = str(AUTH_DIR)
        env["WA_HTTP_PORT"] = str(DEFAULT_HTTP_PORT)
        try:
            _proc = subprocess.Popen(
                [NODE_BIN, str(BRIDGE)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE, text=True, env=env,
                cwd=str(BRIDGE.parent))
        except OSError as exc:
            _proc = None
            return False, f"Could not start the sidecar: {exc}"

        _qr_hinted = False
        _http_port = DEFAULT_HTTP_PORT
        _state = "starting"
        _log(f"WhatsApp sidecar started (pid {_proc.pid})")
        threading.Thread(target=_read_stdout, args=(_proc,), daemon=True).start()
        threading.Thread(target=_read_stderr, args=(_proc,), daemon=True).start()
        return True, "started"


def _stop_bridge() -> None:
    global _proc, _state
    with _lock:
        proc, _proc = _proc, None
        _state = "stopped"
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _read_stdout(proc: subprocess.Popen) -> None:
    if not proc.stdout:
        return
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            # stdout is the IPC channel; anything unparseable is a sidecar bug
            # worth seeing rather than dropping.
            _log(f"[whatsapp] {line[:200]}")
            continue
        try:
            _handle_frame(frame)
        except Exception as exc:
            _log(f"[whatsapp] frame error: {type(exc).__name__}: {exc}")
    _on_bridge_exit(proc)


def _read_stderr(proc: subprocess.Popen) -> None:
    """Keep WhatsApp's own account of a session on disk.

    Its narration -- `attempting registration`, `pair success recv`, `error in
    pairing` -- is what distinguishes a code that never arrived from one that
    was refused, and it is unreadable if it only ever scrolls past in a REPL.
    """
    if not proc.stderr:
        return
    loud = ("error", "fatal", "warn", "pair", "logged", "registration",
            "port ", "discarding", "giving up", "revoked")
    # Errors that are real, logged, and require nothing of the user. WhatsApp
    # answers some optional startup queries slowly or not at all; the session
    # is already open by then and works. Shouting about it trains people to
    # ignore the channel that carries the messages that do matter.
    benign = ("init queries", "error in sending keep alive",
              "failed to send keep alive")
    handle = None
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.unlink()
        handle = LOG_FILE.open("a", encoding="utf-8")
        os.chmod(LOG_FILE, 0o600)      # at trace level this holds raw frames
    except OSError:
        handle = None
    try:
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            if handle is not None:
                try:
                    handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
                    handle.flush()
                except OSError:
                    handle = None
            lowered = line.lower()
            if (any(word in lowered for word in loud)
                    and not any(word in lowered for word in benign)):
                _log(f"[whatsapp] {line[:300]}")
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def _on_bridge_exit(proc: subprocess.Popen) -> None:
    global _state
    with _lock:
        if _proc is not proc:
            return
        code = proc.poll()
        _state = "stopped"
    for entry in list(_pending_sends.values()):
        entry["result"] = {"ok": False, "error": "the sidecar exited"}
        entry["event"].set()
    if code not in (0, None):
        _log(f"WhatsApp sidecar exited with code {code}. /whatsapp start to restart.")


def _to_bridge(frame: dict) -> tuple[bool, str]:
    with _lock:
        if not _running() or not _proc or not _proc.stdin:
            return False, "the gateway is not running"
        try:
            _proc.stdin.write(json.dumps(frame) + "\n")
            _proc.stdin.flush()
        except OSError as exc:
            return False, f"could not reach the sidecar: {exc}"
    return True, ""


# ----------------------------------------------------------------------
# inbound frames
# ----------------------------------------------------------------------

def _handle_frame(frame: dict) -> None:
    global _qr_hinted, _last_status, _state, _http_port, _me_jid
    kind = frame.get("type")

    if kind == "status":
        state = frame.get("state")
        if state == "listening":
            port = frame.get("httpPort")
            if isinstance(port, int):
                _http_port = port
                if port != DEFAULT_HTTP_PORT:
                    _log(f"Port {DEFAULT_HTTP_PORT} was busy; pairing page is on "
                         f"http://127.0.0.1:{port}")
        else:
            _state = str(state or "")
        if state == "open":
            me = frame.get("me")
            if isinstance(me, str) and me:
                _me_jid = me
                threading.Thread(target=_open_own_chat, daemon=True).start()
        if state == "gave_up":
            _announce("WhatsApp: stopped reconnecting.",
                      frame.get("reason") or "", "Run /whatsapp start to retry.")
        elif frame.get("needsPairing"):
            _announce("WhatsApp: this device is no longer linked to your account.",
                      "It was removed under Settings -> Linked devices.",
                      "Link again with:  /whatsapp pairing <your number>")
        if state == "needs_rescan":
            _qr_hinted = False
        if state != _last_status:
            _last_status = state
            _log(f"WhatsApp: {state} {frame.get('reason') or ''}".strip())

    elif kind == "qr":
        _state = "awaiting_scan"
        if not _qr_hinted:
            _qr_hinted = True
            _log(f"Scan to link: http://127.0.0.1:{_http_port}")

    elif kind == "pairing_code":
        _state = "awaiting_pairing"
        superseded = frame.get("supersedes")
        if superseded and superseded != frame.get("code"):
            _log(f"The previous code ({superseded}) has expired -- use this one.")
        _log(f"Pairing code for +{frame.get('phone')}: {frame.get('code')}")
        _log("On your phone: Settings -> Linked devices -> Link with phone number.")

    elif kind == "send_result":
        entry = _pending_sends.get(str(frame.get("reqId") or ""))
        if entry is not None:
            entry["result"] = frame
            entry["event"].set()
        elif not frame.get("ok"):
            _log(f"[whatsapp] send failed: {frame.get('error')}")

    elif kind == "message":
        _on_message(frame)

    elif kind == "error":
        _log(f"[whatsapp] {frame.get('message')}")


def _announce(*lines: str) -> None:
    """Print something the user must not scroll past."""
    _log("")
    for line in lines:
        if line:
            _log(f"  {line}" if line != lines[0] else line)
    _log("")


# ----------------------------------------------------------------------
# access control  --  one rule
# ----------------------------------------------------------------------

def _accepted(msg: dict) -> bool:
    """Only the account's own chat is acted on.

    The CLI is a linked device of this account, so that chat is writable by its
    owner alone -- a message there is from the person who owns this machine.
    Any other chat is writable by whoever has the number, and "execute this"
    from a stranger's phone is not a capability this extension should have.
    """
    return bool(msg.get("selfChat"))


def _on_message(msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    if not text or not _accepted(msg):
        return
    _task_queue.put({
        "text": text,
        "jid": msg.get("remoteJid") or "",
        "key": msg.get("key"),
    })
    waiting = _task_queue.qsize()
    _log(f"WhatsApp task queued: {text[:70]}"
         + (f"   ({waiting} waiting)" if waiting > 1 else ""))


# ----------------------------------------------------------------------
# execution
# ----------------------------------------------------------------------

_SUMMARY_SYSTEM = (
    "You are summarising what laintas-cli just did, for someone reading it on a "
    "phone. Lead with the answer or the outcome. Keep it a few short lines. "
    "Include concrete results -- numbers, paths, names -- and say plainly if it "
    "failed or did nothing. No preamble, no markdown headings. Reply in the "
    "language of the request.")


def _task_worker() -> None:
    """Run queued tasks one at a time, reporting each back to its chat."""
    while True:
        job = _task_queue.get()
        try:
            _run_one(job)
        except Exception as exc:
            try:
                _send(job.get("jid", ""), f"(task failed: {type(exc).__name__}: {exc})")
            except Exception:
                pass
        finally:
            _task_queue.task_done()


def _run_one(job: dict) -> None:
    jid, key, text = job.get("jid", ""), job.get("key"), job["text"]

    # A reaction is the cheapest acknowledgement there is: instant, no
    # chunking, no clutter. Without it a long task looks like nothing happened.
    _react(jid, key, "⏳")

    done = threading.Event()
    threading.Thread(target=_progress_ping, args=(jid, done, text),
                     daemon=True).start()

    started = time.time()
    try:
        result = _tasks.run(text, conversation=f"whatsapp:{jid}")
    finally:
        done.set()
    elapsed = time.time() - started

    if not result.get("ok"):
        _react(jid, key, "❌")
        _send(jid, f"Task failed: {result.get('error') or 'unknown error'}")
        return

    reply = (result.get("reply") or "").strip()
    summary = _summarise(text, reply) if reply else "(the task produced no output)"
    _react(jid, key, "✅")
    _send(jid, f"{summary}\n\n⏱ {int(elapsed)}s")


def _progress_ping(jid: str, done: threading.Event, text: str) -> None:
    """Say it is still working, once, if the task outlives PROGRESS_AFTER."""
    if done.wait(PROGRESS_AFTER):
        return
    _send(jid, f"Still working on: {text[:60]}...")


def _summarise(request: str, reply: str) -> str:
    """Compress the agent's output into something readable on a phone.

    The agent's own reply is written for a terminal -- long, and often a
    transcript of its reasoning. Sending that to a phone is unreadable.
    """
    if len(reply) <= 600:
        return reply
    try:
        res = _backend.chat(
            f"Request:\n{request}\n\nWhat laintas-cli did and found:\n{reply}",
            system_prompt=_SUMMARY_SYSTEM)
        summary = (res.get("reply") or "").strip()
        if summary:
            return summary
    except Exception:
        pass
    return reply[:CHUNK_LIMIT]     # summarising is a nicety, not a gate


# ----------------------------------------------------------------------
# outbound
# ----------------------------------------------------------------------

def _react(jid: str, key, emoji: str) -> None:
    if jid and key:
        _to_bridge({"type": "react", "jid": jid, "key": key, "emoji": emoji})


def _chunks(text: str) -> list[str]:
    """Split on paragraph, then line, then hard -- WhatsApp caps message size."""
    out, rest = [], text
    while len(rest) > CHUNK_LIMIT:
        window = rest[:CHUNK_LIMIT]
        cut = window.rfind("\n\n")
        if cut < CHUNK_LIMIT // 2:
            cut = window.rfind("\n")
        if cut < CHUNK_LIMIT // 2:
            cut = CHUNK_LIMIT
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def _send(jid: str, text: str) -> tuple[bool, str]:
    """Send, waiting for the sidecar's verdict on each chunk.

    Every send is acknowledged; reporting delivery without one means a stopped
    gateway, an unlinked account and a rejected recipient all read as success.
    """
    if not _running():
        return False, "the gateway is not running -- /whatsapp start"
    if _state != "open":
        return False, f"WhatsApp is not connected (state={_state or 'unknown'})"
    for part in _chunks(text):
        req = os.urandom(8).hex()
        entry = {"event": threading.Event(), "result": {}}
        _pending_sends[req] = entry
        try:
            ok, error = _to_bridge({"type": "send", "reqId": req,
                                    "to": jid, "text": part})
            if not ok:
                return False, error
            if not entry["event"].wait(SEND_TIMEOUT):
                return False, f"no acknowledgement within {int(SEND_TIMEOUT)}s"
            if not entry["result"].get("ok"):
                return False, str(entry["result"].get("error") or "send failed")
        finally:
            _pending_sends.pop(req, None)
    return True, ""


_GREETING = (
    "laintas-cli is connected.\n\n"
    "Message me here and I will run it, then tell you what happened. "
    "There is no separate laintas-cli contact -- the CLI is a linked device of "
    "this account, so this chat is where we talk.")


def _open_own_chat() -> None:
    """Create the chat you message it in, once per session.

    An empty self-chat does not appear in WhatsApp's chat list at all, so
    without this there is nothing to tap even after linking succeeds.
    """
    global _greeted
    with _lock:
        if _greeted or not _me_jid:
            return
        _greeted = True
    ok, error = _send(_me_jid, _GREETING)
    if ok:
        _log(f"WhatsApp ready. Message yourself ({_me_jid.split('@')[0]}) to run tasks.")
    else:
        _greeted = False
        _log(f"Could not open your chat: {error}")


# ----------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------

def _make_send_tool():
    from tools import Tool
    return Tool(
        name="whatsapp.send",
        description=("Send a WhatsApp message to a number or group. 'to' is the "
                     "recipient with country code and no + sign, e.g. "
                     "8613800138000. Fails if the gateway is not linked."),
        schema={"type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient number or group JID"},
                    "text": {"type": "string", "description": "Message to send"}},
                "required": ["to", "text"]},
        invoke=_tool_send)


def _make_status_tool():
    from tools import Tool
    return Tool(
        name="whatsapp.status",
        description="WhatsApp connection status, linked account and pairing page.",
        schema={"type": "object", "properties": {}},
        invoke=_tool_status)


def _tool_send(args: dict, ctx=None) -> dict:
    to = str(args.get("to") or "").strip()
    text = str(args.get("text") or "").strip()
    if not to or not text:
        return {"ok": False, "error": "'to' and 'text' are both required"}
    jid = to if "@" in to else f"{to}@s.whatsapp.net"
    ok, error = _send(jid, text)
    if not ok:
        return {"ok": False, "error": f"Could not send to {jid}: {error}"}
    return {"ok": True, "result": f"Delivered to {jid}"}


def _tool_status(args: dict, ctx=None) -> dict:
    return {"ok": True, "result": {
        "running": _running(),
        "connection": _state,
        "linked": _state == "open",
        "account": _me_jid,
        "your_chat": _me_jid,
        "tasks_queued": _task_queue.qsize(),
        "pairing_page": f"http://127.0.0.1:{_http_port}",
        "credentials": str(AUTH_DIR),
        "log": str(LOG_FILE),
        "dependencies_installed": BAILEYS.is_dir(),
        "last_error": _install_error,
    }}


# ----------------------------------------------------------------------
# /whatsapp
# ----------------------------------------------------------------------

_USAGE = ("Usage: /whatsapp start | pairing <phone> | status | hello | "
          "send <number> <text> | logout | stop")


def _log(msg: str) -> None:
    if _console is not None:
        try:
            # These carry bracketed tags and arbitrary error text; Rich would
            # read those as markup.
            _console.print(msg, markup=False, highlight=False)
            return
        except TypeError:
            try:
                _console.print(msg)
                return
            except Exception:
                pass
        except Exception:
            pass
    print(msg)


def _handle_whatsapp(parts: list) -> None:
    global _greeted
    args = [str(p) for p in parts[1:]]
    sub = args[0].lower() if args else "start"
    rest = args[1:]

    if sub in ("start", "qrcode"):
        ok, message = _ensure_bridge()
        if not ok:
            _log(message)
        elif message == "already running":
            _log(f"WhatsApp is already running (state={_state}).")
        else:
            _log("WhatsApp starting. If it is not linked yet, a QR page or "
                 "pairing code will appear shortly.")

    elif sub == "pairing":
        if not rest:
            _log("Usage: /whatsapp pairing <phone>, e.g. /whatsapp pairing 8613800138000")
            return
        phone = "".join(ch for ch in " ".join(rest) if ch.isdigit())
        if len(phone) < 8:
            _log(f"{' '.join(rest)!r} is not a phone number with a country code.")
            return
        _log(f"Requesting a pairing code for +{phone}")
        ok, message = _ensure_bridge()
        if not ok:
            _log(message)
            return
        sent, error = _to_bridge({"type": "pairing", "phone": phone})
        _log("Waiting for WhatsApp to issue the code..." if sent
             else f"Could not request a code: {error}")

    elif sub == "status":
        _log(f"Gateway:      {'running' if _running() else 'not running'}")
        _log(f"Connection:   {_state}"
             + ("   <- not linked; run /whatsapp pairing <number>"
                if _state in ("logged_out", "needs_rescan", "closed") else ""))
        if _me_jid:
            _log(f"Your chat:    {_me_jid}   (message yourself there to run tasks)")
        if _task_queue.qsize():
            _log(f"Tasks queued: {_task_queue.qsize()}")
        _log(f"Credentials:  {AUTH_DIR}")
        _log(f"Log:          {LOG_FILE}")
        if _install_error:
            _log(f"Last error:   {_install_error}")

    elif sub == "hello":
        if not _me_jid:
            _log("Not linked yet -- run /whatsapp start first.")
            return
        _greeted = False
        _open_own_chat()

    elif sub == "send":
        if len(rest) < 2:
            _log("Usage: /whatsapp send <number> <text>")
            return
        result = _tool_send({"to": rest[0], "text": " ".join(rest[1:])})
        _log(result["result"] if result["ok"] else result["error"])

    elif sub == "stop":
        if not _running():
            _log("WhatsApp is not running.")
            return
        _stop_bridge()
        _log("WhatsApp stopped.")

    elif sub == "logout":
        if not _running():
            shutil.rmtree(AUTH_DIR, ignore_errors=True)
            _log("Linked account forgotten. /whatsapp start to link again.")
            return
        sent, error = _to_bridge({"type": "logout"})
        _log("Linked account forgotten; a fresh QR will appear shortly."
             if sent else f"Could not log out: {error}")

    else:
        _log(f"Unknown subcommand {sub!r}. {_USAGE}")
