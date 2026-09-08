"""Extension: whatsapp

Run laintas-cli from WhatsApp, through a secretary.

Messages do not go straight to the agent. They go to an AI that belongs to this
extension, whose job is to decide what the message actually is:

    phone      ->  "check the disk"          -> secretary -> run it on the machine
    phone      ->  "switch to act mode"      -> secretary -> change the mode
    phone      ->  "use opus"                -> secretary -> change the model
    phone      ->  "what mode are you in?"   -> secretary -> answer, run nothing
    phone      ->  "thanks"                  -> secretary -> answer, run nothing

Then it reports back in its own words. That indirection is the design: not
every message is a task, and a channel that assumes otherwise runs "thanks" as
a shell command and cannot be asked to change anything about the CLI itself.

The secretary decides WHAT to do; `ctx.tasks` does the doing when the answer is
"run it", which is the agent loop with tools -- not `ctx.backend`, which only
writes text.

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
            ("start", "Start the gateway (links by QR if no account yet)"),
            ("pairing", "Link by entering an 8-char code: pairing <phone>"),
            ("status", "Connection, linked account, and queue"),
            ("message", "Show the WhatsApp conversation: message [count]"),
            ("restart", "Restart the gateway"),
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
    """The sidecar process exists. Says nothing about whether it works."""
    return _proc is not None and _proc.poll() is None


#: What "connected" means, stated once so every caller agrees.
#:
#:   stopped    nothing running
#:   starting   process up, has not reached WhatsApp yet
#:   unlinked   reached WhatsApp, no account linked -- needs a QR or a code
#:   connected  linked and usable; this is the only state that can carry a task
#:   retrying   was connected, lost it, coming back on its own
#:   failed     gave up; needs a person
def _phase() -> str:
    if not _running():
        return "stopped"
    if _state == "open":
        return "connected"
    if _state in ("awaiting_scan", "awaiting_pairing", "needs_rescan", "logged_out"):
        return "unlinked"
    if _state == "gave_up":
        return "failed"
    if _state in ("closed", "connecting"):
        return "retrying"
    return "starting"


def _bridge_is_stale() -> bool:
    """True when the sidecar on disk is newer than the one running.

    Restarting by hand after an update is a chore invented by the tool, not by
    the task. If the file changed, the running process is the wrong one.
    """
    if not _running() or _proc is None:
        return False
    try:
        started = time.time() - float(
            open(f"/proc/{_proc.pid}/stat").read().split()[21]) / os.sysconf("SC_CLK_TCK")
        # /proc gives ticks since boot; compare against boot time instead.
        with open("/proc/uptime") as handle:
            boot = time.time() - float(handle.read().split()[0])
        started = boot + float(
            open(f"/proc/{_proc.pid}/stat").read().split()[21]) / os.sysconf("SC_CLK_TCK")
        return BRIDGE.stat().st_mtime > started
    except (OSError, ValueError, IndexError):
        return False


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
    _record("in", text)
    _task_queue.put({
        "text": text,
        "jid": msg.get("remoteJid") or "",
        "key": msg.get("key"),
    })


# ----------------------------------------------------------------------
# execution
# ----------------------------------------------------------------------

#: What the user sent and what came back. Kept here rather than printed: the
#: CLI's own screen belongs to whoever is sitting at it, and a phone
#: conversation scrolling through it is noise in someone else's workspace.
#: `/whatsapp message` is where it belongs.
_MESSAGES: list[dict] = []
MESSAGE_HISTORY = 100


def _record(direction: str, text: str, note: str = "") -> None:
    _MESSAGES.append({"at": time.strftime("%H:%M:%S"), "dir": direction,
                      "text": text, "note": note})
    del _MESSAGES[:-MESSAGE_HISTORY]


_SECRETARY_SYSTEM = """You are the WhatsApp secretary for laintas-cli, a coding \
agent running on the user's own machine. The user messages you from their phone. \
You decide what each message means and reply with ONE json object, nothing else.

  {"action":"execute","task":"<instruction for the agent>"}
      The user wants something done on the machine: inspect, build, fix, deploy,
      answer a question about their code or system. Rewrite their message as a
      clear instruction. This is the default for anything that needs the machine.

  {"action":"answer","text":"<your reply>"}
      Nothing needs to run: small talk, a question you can already answer from
      the context below, or a request you need clarified before acting.

  {"action":"mode","value":"<mode name>"}
      They want to change how the agent behaves. Available: %(modes)s

  {"action":"model","value":"<model name or 'auto'>"}
      They want to change the model.

  {"action":"status"}
      They are asking how the CLI is set up or what is going on.

Current state:  mode=%(mode)s  model=%(model)s  directory=%(cwd)s

Judge by intent, not keywords. Prefer "execute" when they clearly want work \
done; prefer "answer" when acting on a guess could do the wrong thing."""

_REPORT_SYSTEM = (
    "You are the WhatsApp secretary for laintas-cli, reporting back to the user "
    "on their phone. Lead with the answer or the outcome. Keep it a few short "
    "lines. Include the concrete results -- numbers, paths, names -- and say "
    "plainly if it failed or did nothing. No preamble, no markdown headings. "
    "Reply in the language the user wrote in.")


def _cli_state() -> dict:
    """What the secretary needs to know to make a sensible decision."""
    state = {"mode": "unknown", "model": "auto", "cwd": os.getcwd(),
             "modes": "act, plan"}
    try:
        import mode_manager
        active = mode_manager.get_active_mode()
        state["mode"] = (active or {}).get("name", "unknown") if isinstance(
            active, dict) else str(active or "unknown")
        names = [m.get("name", "") for m in mode_manager.list_modes()
                 if isinstance(m, dict)]
        if names:
            state["modes"] = ", ".join(n for n in names if n)
    except Exception:
        pass
    try:
        import agent_loop
        state["model"] = agent_loop.get_runtime_config("model") or "auto"
    except Exception:
        pass
    return state


def _decide(text: str) -> dict:
    """Ask the secretary what this message is. Falls back to executing it.

    A secretary that cannot be reached must not swallow the message: the user
    asked for something, and running it is closer to their intent than silence.
    """
    state = _cli_state()
    try:
        res = _backend.chat(text, system_prompt=_SECRETARY_SYSTEM % state)
        raw = (res.get("reply") or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            decision = json.loads(raw[start:end + 1])
            if isinstance(decision, dict) and decision.get("action"):
                return decision
    except Exception as exc:
        _log(f"[whatsapp] secretary unavailable ({type(exc).__name__}); "
             "treating the message as a task")
    return {"action": "execute", "task": text}


def _set_mode(value: str) -> str:
    try:
        import mode_manager
        ok, message = mode_manager.activate(str(value))
        return message if message else ("mode changed" if ok else "could not change mode")
    except Exception as exc:
        return f"could not change mode: {exc}"


def _set_model(value: str) -> str:
    try:
        import sys as _sys
        cli = _sys.modules.get("laintas_cli")
        if cli is None or not hasattr(cli, "_rprompt_apply_model_choice"):
            return "model switching is not available in this session"
        ok, message = cli._rprompt_apply_model_choice(str(value))
        return message if message else ("model changed" if ok else "could not change model")
    except Exception as exc:
        return f"could not change model: {exc}"


def _task_worker() -> None:
    while True:
        job = _task_queue.get()
        try:
            _handle_job(job)
        except Exception as exc:
            try:
                _reply(job, f"(failed: {type(exc).__name__}: {exc})", "❌")
            except Exception:
                pass
        finally:
            _task_queue.task_done()


def _handle_job(job: dict) -> None:
    text = job["text"]
    _react(job.get("jid", ""), job.get("key"), "⏳")

    decision = _decide(text)
    action = str(decision.get("action") or "execute")

    if action == "answer":
        _reply(job, str(decision.get("text") or "").strip() or "(no reply)", "✅")
        return

    if action == "mode":
        _reply(job, _set_mode(decision.get("value") or ""), "✅")
        return

    if action == "model":
        _reply(job, _set_model(decision.get("value") or ""), "✅")
        return

    if action == "status":
        state = _cli_state()
        _reply(job, (f"mode: {state['mode']}\nmodel: {state['model']}\n"
                     f"directory: {state['cwd']}\n"
                     f"queued: {_task_queue.qsize()}"), "✅")
        return

    # execute
    task = str(decision.get("task") or text)
    done = threading.Event()
    threading.Thread(target=_progress_ping, args=(job, done, task),
                     daemon=True).start()
    started = time.time()
    try:
        result = _tasks.run(task, conversation=f"whatsapp:{job.get('jid','')}")
    finally:
        done.set()
    elapsed = int(time.time() - started)

    if not result.get("ok"):
        _reply(job, f"Task failed: {result.get('error') or 'unknown error'}", "❌")
        return
    output = (result.get("reply") or "").strip()
    report = _report(text, task, output) if output else "(the task produced no output)"
    _reply(job, f"{report}\n\n⏱ {elapsed}s", "✅")


def _report(asked: str, task: str, output: str) -> str:
    """Let the secretary say what happened, in its own words.

    The agent writes for a terminal -- long, and often a transcript of its
    reasoning. Relaying that to a phone verbatim is unreadable.
    """
    try:
        res = _backend.chat(
            f"They asked:\n{asked}\n\nI ran:\n{task}\n\nResult:\n{output}",
            system_prompt=_REPORT_SYSTEM)
        summary = (res.get("reply") or "").strip()
        if summary:
            return summary
    except Exception:
        pass
    return output[:CHUNK_LIMIT]


def _reply(job: dict, text: str, emoji: str) -> None:
    _react(job.get("jid", ""), job.get("key"), emoji)
    ok, error = _send(job.get("jid", ""), text)
    _record("out", text, "" if ok else f"delivery failed: {error}")
    if not ok:
        _log(f"[whatsapp] could not reply: {error}")


def _progress_ping(job: dict, done: threading.Event, task: str) -> None:
    if done.wait(PROGRESS_AFTER):
        return
    _send(job.get("jid", ""), f"Still working on: {task[:60]}...")


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
    if not ok:
        _greeted = False
        _log(f"Could not open your chat: {error}")
        return
    # Saying "message yourself" is not directions. The chat is titled with the
    # user's OWN name, which does not read as a place to send a message, and if
    # they have never used it the reflex is to look for a contact that is not
    # there.
    _log(f"WhatsApp connected (+{_me_jid.split('@')[0]}). I sent you a message; "
         "it is the chat titled with your own name.")


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

_USAGE = ("Usage: /whatsapp [status] | message [n] | start | restart | stop | "
          "pairing <phone> | send <number> <text> | logout")


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
    sub = args[0].lower() if args else "status"
    rest = args[1:]

    if sub in ("start", "restart"):
        if sub == "restart" and _running():
            _stop_bridge()
            _log("Stopped; starting again.")
        elif _running() and _bridge_is_stale():
            # The sidecar was updated under a running process. Nobody should
            # have to know that, or be told to stop and start.
            _stop_bridge()
            _log("The sidecar was updated; restarting it.")
        ok, message = _ensure_bridge()
        if not ok:
            _log(message)
        elif message == "already running":
            _log(f"WhatsApp is already {_phase()}.")
        else:
            _log("WhatsApp starting. If no account is linked yet, a QR page or "
                 "a pairing code will follow.")

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
        phase = _phase()
        hint = {
            "stopped": "   -> /whatsapp start",
            "unlinked": "   -> /whatsapp pairing <your number>",
            "failed": "   -> /whatsapp restart",
        }.get(phase, "")
        _log(f"WhatsApp:  {phase}{hint}")
        if phase == "connected":
            _log(f"Account:   +{_me_jid.split('@')[0]}   "
                 "(message yourself on WhatsApp to give it work)")
        if _task_queue.qsize():
            _log(f"Running:   {_task_queue.qsize()} queued")
        if _MESSAGES:
            last = _MESSAGES[-1]
            arrow = "->" if last["dir"] == "in" else "<-"
            _log(f"Last:      {last['at']} {arrow} "
                 f"{last['text'].replace(chr(10), ' ')[:60]}"
                 "    (/whatsapp message for more)")
        if _install_error:
            _log(f"Error:     {_install_error}")
        _log(f"Log:       {LOG_FILE}")

    elif sub in ("message", "messages"):
        if not _MESSAGES:
            _log("No WhatsApp messages yet." if _phase() == "connected"
                 else f"No messages -- WhatsApp is {_phase()}.")
            return
        count = 20
        if rest and rest[0].isdigit():
            count = max(1, min(int(rest[0]), MESSAGE_HISTORY))
        for item in _MESSAGES[-count:]:
            arrow = "->" if item["dir"] == "in" else "<-"
            body = item["text"].replace("\n", " ")[:110]
            _log(f"  {item['at']}  {arrow}  {body}"
                 + (f"   [{item['note']}]" if item["note"] else ""))

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
