"""local_runtime.py — the loopback half of the Helpwo ⇄ laintas_cli transports.

Helpwo reaches a laintas_cli host over one of two topologies:

  p2p       a genuinely different machine. Everything (filesystem RPC, AI
            command execution, the PTY, the HTTP tunnel, the VNC framebuffer)
            rides a WebRTC DataChannel so the bytes stay off the relay —
            webrtc_channel.py owns that side.

  loopback  the page is being served by THIS process's own /helpwo bridge.
            There is no NAT to traverse and no relay to avoid: the browser and
            the shell are one machine, one origin, already authenticated by the
            bridge's own cookie.

Until now only two of the five surfaces knew about that distinction. The
filesystem had a proper abstraction (LocalWorkspaceProvider vs RemoteProvider)
and VNC had two hand-written modules (vncOverWs vs vncOverRtc), but command
execution, the interactive PTY and the HTTP tunnel were P2P-only. A local
`/helpwo` session therefore negotiated ICE with itself to run `ls`, and — since
aiortc is an optional extra guarded by a try/except — a CLI installed without
it could read and write files locally while `shell_exec` and the terminal
failed outright, on the very machine serving the page.

This module supplies the missing loopback transports so the frontend's
CliSession can pick a topology once and use the same five surfaces either way:

    POST /api/local-exec            SSE: approval → start → out* → final
    POST /api/local-exec/approval   answer a pending approval prompt
    POST /api/local-exec/abort      kill a running command
    GET  /api/local-pty             WebSocket: real PTY, reattachable
    ANY  /api/local-proxy/<port>/*  one request proxied to 127.0.0.1:<port>

The wire vocabulary deliberately mirrors webrtc_channel.py's frames (`start`,
`out`, `final`, `approval`) so the two transports collapse into one interface
on the browser side rather than one interface plus a pile of special cases.

Trust boundary: identical to the rest of the bridge. The server binds loopback
only, every request carries the bridge token, and state-changing requests pass
the same-origin guard. The policy.py gate that decides whether an AI-driven
command may run at all is unchanged and still evaluated here — it is a local
safety decision, independent of which transport carried the request.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
from typing import Any, Optional

# Bounds. Deliberately the same order as the P2P path's so a command does not
# behave differently depending on which transport carried it.
MAX_EXEC_OUTPUT = 256 * 1024
DEFAULT_EXEC_TIMEOUT = 30
MAX_EXEC_TIMEOUT = 5 * 60
APPROVAL_TIMEOUT = 300
MAX_CONCURRENT_EXEC = 8
MAX_TERMINALS = 8
TERM_GRACE_SECONDS = 120
TERM_BUFFER_BYTES = 256 * 1024
PROXY_TIMEOUT = 30
MAX_PROXY_BODY = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Pending AI-exec requests
# ---------------------------------------------------------------------------

class _ExecRequest:
    """One in-flight AI command: its approval gate and its abort switch."""

    def __init__(self, req_id: str) -> None:
        self.req_id = req_id
        self.approval = threading.Event()
        self.decision = "reject"
        self.abort = threading.Event()
        self.proc: Optional[subprocess.Popen] = None


_exec_lock = threading.RLock()
_exec_requests: dict[str, _ExecRequest] = {}


def _register_exec(req_id: str) -> Optional[_ExecRequest]:
    with _exec_lock:
        if len(_exec_requests) >= MAX_CONCURRENT_EXEC:
            return None
        req = _ExecRequest(req_id)
        _exec_requests[req_id] = req
        return req


def _release_exec(req_id: str) -> None:
    with _exec_lock:
        _exec_requests.pop(req_id, None)


def respond_approval(req_id: str, decision: str) -> bool:
    """Answer a pending approval prompt. Returns False if nothing was waiting."""
    with _exec_lock:
        req = _exec_requests.get(req_id)
    if req is None:
        return False
    req.decision = "approve" if decision == "approve" else "reject"
    req.approval.set()
    return True


def abort_exec(req_id: str) -> bool:
    """Abort a running (or approval-pending) command."""
    with _exec_lock:
        req = _exec_requests.get(req_id)
    if req is None:
        return False
    req.abort.set()
    # An abort landing during the approval wait must also release that wait,
    # otherwise the command sits for the full APPROVAL_TIMEOUT before noticing.
    req.decision = "reject"
    req.approval.set()
    proc = req.proc
    if proc is not None:
        _kill_process_group(proc)
    return True


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the whole group — a shell command usually spawns children."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def shutdown() -> None:
    """Tear down everything this module owns (called from stop_server)."""
    with _exec_lock:
        requests = list(_exec_requests.values())
        _exec_requests.clear()
    for req in requests:
        req.abort.set()
        req.approval.set()
        if req.proc is not None:
            _kill_process_group(req.proc)
    with _term_lock:
        terminals = list(_terminals.values())
        _terminals.clear()
    for term in terminals:
        term.terminate()


# ---------------------------------------------------------------------------
# SSE command execution
# ---------------------------------------------------------------------------

class SseWriter:
    """Minimal server-sent-events writer over a raw socket file object."""

    def __init__(self, wfile) -> None:
        self._wfile = wfile
        self._lock = threading.Lock()
        self.broken = False

    def event(self, payload: dict) -> bool:
        if self.broken:
            return False
        blob = json.dumps(payload, default=str)
        with self._lock:
            try:
                self._wfile.write(f"data: {blob}\n\n".encode("utf-8"))
                self._wfile.flush()
                return True
            except (OSError, ValueError):
                self.broken = True
                return False


def _policy_modules():
    """Import the policy gate lazily — this module is imported at startup."""
    import policy as _policy
    from agent_loop import get_runtime_config
    return _policy, get_runtime_config


def run_exec(body: dict, sse: SseWriter, resolve_cwd, agent_id: Optional[str] = None) -> None:
    """Run one AI-driven command, streaming the same frames the P2P path sends.

    `resolve_cwd` is injected rather than imported so the containment rule stays
    the bridge's (helpwo_server._resolve_local_path), not a second copy of it
    that could drift out of agreement with the filesystem endpoints.
    """
    req_id = str(body.get("reqId") or body.get("id") or "").strip()
    cmd = str(body.get("cmd") or body.get("command") or "")
    if not req_id or not cmd:
        sse.event({"t": "final", "status": "fail", "error": "missing reqId/cmd"})
        return

    try:
        timeout = int(body.get("timeout") or DEFAULT_EXEC_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_EXEC_TIMEOUT
    timeout = max(1, min(timeout, MAX_EXEC_TIMEOUT))

    resolved = resolve_cwd(str(body.get("cwd") or ""))
    if resolved is None or not resolved.is_dir():
        sse.event({"t": "final", "status": "fail",
                   "error": f"invalid cwd: {body.get('cwd') or '(root)'}"})
        return
    cwd = str(resolved)

    req = _register_exec(req_id)
    if req is None:
        sse.event({"t": "final", "status": "fail",
                   "error": "too many commands are already running"})
        return

    try:
        _policy, get_runtime_config = _policy_modules()
        decision = _policy.evaluate(cmd, cwd, req_id=req_id, agent_id=agent_id)
        if decision.action == "deny":
            sse.event({"t": "final", "status": "fail",
                       "error": f"Blocked by policy: {decision.reason}"})
            return

        needs_approval = (decision.action == "needs_approval"
                          or not get_runtime_config("allow_remote_exec_without_approval"))
        if needs_approval:
            destructive = bool(_policy.is_delete_command(cmd)
                               or _policy.is_destructive_git_command(cmd))
            if not sse.event({"t": "approval", "reqId": req_id, "cmd": cmd,
                              "cwd": cwd, "destructive": destructive}):
                return
            if not req.approval.wait(timeout=APPROVAL_TIMEOUT):
                sse.event({"t": "final", "status": "aborted",
                           "error": "approval timed out"})
                return
            if req.decision != "approve" or req.abort.is_set():
                sse.event({"t": "final", "status": "aborted",
                           "error": f"User denied: {cmd[:100]}"})
                return

        if req.abort.is_set():
            sse.event({"t": "final", "status": "aborted", "error": "aborted"})
            return

        sse.event({"t": "start", "reqId": req_id, "cwd": cwd})
        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            sse.event({"t": "final", "status": "fail", "error": str(e)})
            return
        req.proc = proc
        _stream_process(proc, req, sse, timeout)
    finally:
        _release_exec(req_id)


def _stream_process(proc: subprocess.Popen, req: _ExecRequest,
                    sse: SseWriter, timeout: int) -> None:
    """Pump stdout to the client until exit, abort, timeout or a dead client."""
    deadline = time.monotonic() + timeout
    total = 0
    truncated = False
    stdout = proc.stdout
    assert stdout is not None
    fd = stdout.fileno()
    os.set_blocking(fd, False)

    while True:
        if req.abort.is_set():
            _kill_process_group(proc)
            sse.event({"t": "final", "status": "aborted", "error": "aborted"})
            return
        if sse.broken:
            # Nobody is listening any more; killing beats leaking the process.
            _kill_process_group(proc)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(proc)
            sse.event({"t": "final", "status": "fail",
                       "error": f"timeout after {timeout}s"})
            return
        try:
            ready, _w, _x = select.select([fd], [], [], min(remaining, 1.0))
        except (OSError, ValueError):
            break
        if not ready:
            if proc.poll() is not None:
                break
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError as e:
            if e.errno in (errno.EIO,):  # pty-style EOF; plain pipes just return b""
                break
            break
        if not chunk:
            break
        if total < MAX_EXEC_OUTPUT:
            room = MAX_EXEC_OUTPUT - total
            sse.event({"t": "out", "data": chunk[:room].decode("utf-8", "replace")})
        elif not truncated:
            truncated = True
            sse.event({"t": "out", "data": "\n…[output truncated]…\n"})
        total += len(chunk)

    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        code = -1
    if req.abort.is_set():
        sse.event({"t": "final", "status": "aborted", "error": "aborted"})
        return
    sse.event({"t": "final", "status": "success" if code == 0 else "fail",
               "exitCode": code})


# ---------------------------------------------------------------------------
# PTY over WebSocket
# ---------------------------------------------------------------------------

def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
    except OSError:
        pass


class LocalTerminal:
    """A real PTY whose output survives the viewer disconnecting.

    The reader thread owns master_fd for the terminal's whole life and never
    stops draining it, so a detached shell keeps making progress and its output
    is replayed on reattach. That mirrors the P2P terminal's grace window
    (webrtc_channel._open_term) — a browser refresh must not silently start a
    second shell, and it must not lose whatever the first one printed while
    nobody was listening.
    """

    def __init__(self, session_id: str, cwd: Optional[str], cols: int, rows: int) -> None:
        self.session_id = session_id
        self.buffer = bytearray()
        self.lock = threading.RLock()
        self.sock: Optional[socket.socket] = None
        self.closed = threading.Event()
        self.exit_code: Optional[int] = None
        self._detached_at: Optional[float] = time.monotonic()

        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                if cwd:
                    os.chdir(cwd)
            except OSError:
                pass
            os.environ["TERM"] = "xterm-256color"
            shell = os.environ.get("SHELL") or "/bin/bash"
            try:
                os.execvp(shell, [shell])
            except OSError:
                pass
            os._exit(1)

        self.pid = pid
        self.master_fd = master_fd
        _set_winsize(master_fd, rows, cols)
        self._reader = threading.Thread(
            target=self._pump, daemon=True, name=f"local-pty-{session_id[:8]}")
        self._reader.start()

    # -- output side ------------------------------------------------------

    def _pump(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    ready, _w, _x = select.select([self.master_fd], [], [], 1.0)
                except (OSError, ValueError):
                    break
                if not ready:
                    self._reap_if_expired()
                    continue
                try:
                    data = os.read(self.master_fd, 65536)
                except OSError:
                    break  # EIO — the child exited and closed the slave side
                if not data:
                    break
                self._emit(data)
        finally:
            self._finish()

    def _emit(self, data: bytes) -> None:
        import helpwo_server
        with self.lock:
            sock = self.sock
            if sock is None:
                self.buffer.extend(data)
                if len(self.buffer) > TERM_BUFFER_BYTES:
                    del self.buffer[:len(self.buffer) - TERM_BUFFER_BYTES]
                return
            try:
                sock.sendall(helpwo_server._ws_frame(data))
            except OSError:
                # The viewer vanished mid-write: fall back to buffering so the
                # shell keeps running and the output is there on reattach.
                self.sock = None
                self._detached_at = time.monotonic()
                self.buffer.extend(data)

    def _reap_if_expired(self) -> None:
        with self.lock:
            detached_at = self._detached_at if self.sock is None else None
        if detached_at is not None and time.monotonic() - detached_at > TERM_GRACE_SECONDS:
            self.terminate()

    def _finish(self) -> None:
        import helpwo_server
        code = -1
        try:
            _pid, status = os.waitpid(self.pid, os.WNOHANG)
            if _pid:
                code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        except ChildProcessError:
            pass
        except OSError:
            pass
        self.exit_code = code
        self.closed.set()
        with self.lock:
            sock = self.sock
            self.sock = None
        if sock is not None:
            try:
                sock.sendall(helpwo_server._ws_frame(
                    json.dumps({"t": "exit", "code": code}).encode("utf-8"), opcode=0x1))
            except OSError:
                pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        with _term_lock:
            if _terminals.get(self.session_id) is self:
                del _terminals[self.session_id]

    # -- input side -------------------------------------------------------

    def attach(self, sock: socket.socket) -> bytes:
        """Bind a viewer and hand back whatever it missed."""
        with self.lock:
            self.sock = sock
            self._detached_at = None
            missed = bytes(self.buffer)
            self.buffer.clear()
        return missed

    def detach(self, sock: socket.socket) -> None:
        with self.lock:
            if self.sock is sock:
                self.sock = None
                self._detached_at = time.monotonic()

    def write(self, data: bytes) -> None:
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        _set_winsize(self.master_fd, rows, cols)

    def terminate(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        with _term_lock:
            if _terminals.get(self.session_id) is self:
                del _terminals[self.session_id]


_term_lock = threading.RLock()
_terminals: dict[str, LocalTerminal] = {}


def open_terminal(session_id: str, cwd: Optional[str],
                  cols: int, rows: int) -> tuple[Optional[LocalTerminal], bool, str]:
    """Get or create a terminal. Returns (terminal, resumed, error)."""
    with _term_lock:
        existing = _terminals.get(session_id)
        if existing is not None and not existing.closed.is_set():
            existing.resize(cols, rows)
            return existing, True, ""
        if len(_terminals) >= MAX_TERMINALS:
            return None, False, "too many terminals are open"
        try:
            term = LocalTerminal(session_id, cwd, cols, rows)
        except OSError as e:
            return None, False, str(e)
        _terminals[session_id] = term
        return term, False, ""


def close_terminal(session_id: str) -> bool:
    with _term_lock:
        term = _terminals.get(session_id)
    if term is None:
        return False
    term.terminate()
    return True


def serve_terminal(term: LocalTerminal, sock: socket.socket, resumed: bool) -> None:
    """Shuttle bytes between an attached viewer and the PTY.

    Runs on the request thread and returns when the viewer goes away; the PTY
    itself outlives this call (see LocalTerminal's docstring).
    """
    import helpwo_server

    missed = term.attach(sock)
    try:
        sock.sendall(helpwo_server._ws_frame(
            json.dumps({"t": "open", "id": term.session_id, "resumed": resumed})
            .encode("utf-8"), opcode=0x1))
        if missed:
            sock.sendall(helpwo_server._ws_frame(missed))
    except OSError:
        term.detach(sock)
        return

    sock.settimeout(None)
    try:
        while not term.closed.is_set():
            try:
                ready, _w, _x = select.select([sock], [], [], 30)
            except (OSError, ValueError):
                return
            if not ready:
                try:
                    sock.sendall(helpwo_server._ws_frame(b"", opcode=0x9))  # ping
                except OSError:
                    return
                continue
            frame = helpwo_server._ws_read_frame(sock)
            if frame is None:
                return
            opcode, payload = frame
            if opcode == 0x8:            # close
                return
            if opcode == 0x9:            # ping → pong
                try:
                    sock.sendall(helpwo_server._ws_frame(payload, opcode=0xA))
                except OSError:
                    return
                continue
            if opcode == 0xA:            # pong
                continue
            if opcode == 0x1:            # text → control
                try:
                    msg = json.loads(payload.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("t")
                if kind == "resize":
                    try:
                        term.resize(int(msg.get("cols") or 80), int(msg.get("rows") or 24))
                    except (TypeError, ValueError):
                        pass
                elif kind == "terminate":
                    term.terminate()
                    return
                elif kind == "detach":
                    return
                continue
            if payload:                  # binary → keystrokes
                term.write(payload)
    finally:
        term.detach(sock)


# ---------------------------------------------------------------------------
# Loopback HTTP proxy
# ---------------------------------------------------------------------------

# Hop-by-hop headers must not be forwarded in either direction; passing
# Transfer-Encoding through in particular produces a body the caller decodes
# twice. Same list the P2P tunnel (_serve_http) filters on.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding",
})


def proxy_request(port: int, method: str, path: str, headers: dict,
                  body: Optional[bytes]) -> dict:
    """Proxy one request to 127.0.0.1:<port>.

    Loopback-only by construction: the host is hard-coded, so this can never
    become an open proxy into the machine's network the way a caller-supplied
    host would. Returns {ok, status, headers, body} or {ok: False, error}.
    """
    import http.client

    if not (1 <= port <= 65535):
        return {"ok": False, "error": "bad port"}
    if not path.startswith("/"):
        path = "/" + path

    forwarded = {k: v for k, v in (headers or {}).items()
                 if k.lower() not in _HOP_BY_HOP and k.lower() != "host"}
    forwarded["Host"] = f"127.0.0.1:{port}"
    # The dev server must not try to satisfy this from a cache we can't see.
    forwarded.pop("If-None-Match", None)
    forwarded.pop("If-Modified-Since", None)

    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=PROXY_TIMEOUT)
        conn.request(method.upper(), path, body=body or None, headers=forwarded)
        response = conn.getresponse()
        payload = response.read(MAX_PROXY_BODY + 1)
        if len(payload) > MAX_PROXY_BODY:
            return {"ok": False, "error": "response too large to tunnel"}
        out_headers = {k: v for k, v in response.getheaders()
                       if k.lower() not in _HOP_BY_HOP}
        return {"ok": True, "status": response.status,
                "headers": out_headers, "body": payload}
    except (OSError, http.client.HTTPException) as e:
        return {"ok": False, "error": f"cannot reach 127.0.0.1:{port}: {e}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def status() -> dict[str, Any]:
    """What this module currently holds — surfaced by /api/local-runtime."""
    with _exec_lock:
        running = len(_exec_requests)
    with _term_lock:
        terminals = [
            {"id": t.session_id, "attached": t.sock is not None,
             "closed": t.closed.is_set()}
            for t in _terminals.values()
        ]
    return {"execRunning": running, "terminals": terminals}
