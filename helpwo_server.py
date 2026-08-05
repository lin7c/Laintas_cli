"""Local gateway for /helpwo: serves Helpwo's static dist + a minimal
in-process API that bridges the frontend to the running laintas_cli.

Design:
- stdlib http.server (no Flask dependency).
- Binds 127.0.0.1 by default. Whether a machine should be reachable from the
  network is the operator's decision, not this server's, so `host` is settable
  — but the token is always required, and a non-loopback bind is reported for
  what it costs (browsers only grant File System Access, clipboard, microphone
  and Service Workers on a secure context: HTTPS, or localhost).
- Authentication follows Jupyter: the token arrives once in the URL, is
  exchanged for a cookie, and is not needed again. The frontend needs no
  change — its calls are same-origin with credentials already.
- Three API endpoints mirror Helpwo's gateway contract:
    GET  /api/agents                       - list registered agents
    POST /api/agents/<id>/send             - dispatch a message to the CLI
    GET  /api/agents/<id>/updates?since=N  - poll buffered events
- Static files served from the dist directory with path-traversal guard.
- Events are captured in-process by overriding AgentRegistry._do_post_events,
  so the existing _push_events / _push_final pipeline works unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs, unquote

# Default port: 2913 is the same port _detect_backend() probes, so when the
# local server is up the CLI auto-detects it as the backend.
DEFAULT_PORT = 2913

# ---------------------------------------------------------------------------
# Per-agent event buffer
# ---------------------------------------------------------------------------

_COOKIE_NAME = "laintas_helpwo_token"
_auth_token_value: Optional[str] = None
_bind_host: str = "127.0.0.1"
_scheme: str = "http"
_cert_hostnames: set = set()


def _hostnames_in_cert(cert_path: str) -> set:
    """Subject Alternative Names (and CN) from a PEM certificate."""
    names: set = set()
    try:
        import ssl as _ssl
        decoded = _ssl._ssl._test_decode_cert(cert_path)  # type: ignore[attr-defined]
        for key, value in decoded.get("subjectAltName", ()):
            if key.lower() == "dns" and value:
                names.add(value.lower().lstrip("*."))
        for rdn in decoded.get("subject", ()):
            for key, value in rdn:
                if key == "commonName" and value:
                    names.add(value.lower().lstrip("*."))
    except Exception:
        pass
    return names


def _auth_token() -> str:
    """The token this server requires, or "" when explicitly disabled."""
    return _auth_token_value or ""


def _self_origin() -> str:
    """The origin this server actually answers on.

    These headers used to name http://127.0.0.1:<port> unconditionally, which
    was wrong the moment the server bound anywhere else or spoke TLS — it
    advertised an origin that was not the one the browser had loaded.
    """
    host = _bind_host if _bind_host not in ("0.0.0.0", "::") else "127.0.0.1"
    return f"{_scheme}://{host}:{_server_port()}"


def _allowed_hosts() -> set:
    """Host names a request's Host/Origin header may carry.

    Loopback always, plus whatever the server was actually bound to. Binding
    to a routable address is the user's decision; refusing their own Host
    header afterwards would just look like a broken server.
    """
    hosts = {"127.0.0.1", "localhost", "[::1]", "::1"}
    if _bind_host and _bind_host not in ("0.0.0.0", "::"):
        hosts.add(_bind_host.lower())
    # Names the TLS certificate was issued for. Supplying a certificate for
    # cli.laintas.com is a statement that the server answers to that name, so
    # requiring the operator to repeat it in LAINTAS_HELPWO_HOSTS would just be
    # a second chance to get it wrong — and the failure (421) says nothing
    # about what is missing.
    hosts.update(_cert_hostnames)
    extra = os.environ.get("LAINTAS_HELPWO_HOSTS", "")
    for item in extra.replace(",", " ").split():
        if item.strip():
            hosts.add(item.strip().lower())
    return hosts


# RFC 6455's fixed handshake GUID. Verified against the worked example in the
# spec: key "dGhlIHNhbXBsZSBub25jZQ==" must accept as "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=".
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_MAX_FRAME = 8 * 1024 * 1024


def _ws_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _ws_frame(payload: bytes, opcode: int = 0x2) -> bytes:
    """Encode one unfragmented server frame (server frames are never masked)."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += length.to_bytes(2, "big")
    else:
        header.append(127)
        header += length.to_bytes(8, "big")
    return bytes(header) + payload


def _ws_read_exact(sock, count: int) -> bytes:
    """Read exactly count bytes, or return b"" if the peer went away."""
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            return b""
        chunks += chunk
    return bytes(chunks)


def _ws_read_frame(sock) -> tuple[int, bytes] | None:
    """Read one client frame. Returns (opcode, payload) or None when closed.

    Client frames are always masked; an unmasked one is a protocol violation
    and closes the connection rather than being interpreted.
    """
    head = _ws_read_exact(sock, 2)
    if len(head) < 2:
        return None
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        raw = _ws_read_exact(sock, 2)
        if len(raw) < 2:
            return None
        length = int.from_bytes(raw, "big")
    elif length == 127:
        raw = _ws_read_exact(sock, 8)
        if len(raw) < 8:
            return None
        length = int.from_bytes(raw, "big")
    if length > _WS_MAX_FRAME or not masked:
        return None
    mask = _ws_read_exact(sock, 4)
    if len(mask) < 4:
        return None
    payload = _ws_read_exact(sock, length) if length else b""
    if length and not payload:
        return None
    unmasked = bytearray(payload)
    for i in range(len(unmasked)):
        unmasked[i] ^= mask[i & 3]
    return opcode, bytes(unmasked)


class _EventBuffer:
    """Thread-safe per-agent event store with monotonic sequence numbers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # agent_id -> list of (seq, event_dict)
        self._buffers: dict[str, list[tuple[int, dict]]] = {}
        # agent_id -> next seq
        self._seqs: dict[str, int] = {}
        # agent_id -> high-water mark (last seq returned to any poller)
        self._cursors: dict[str, int] = {}

    def append(self, agent_id: str, events: list[dict]) -> None:
        if not agent_id or not events:
            return
        with self._lock:
            buf = self._buffers.setdefault(agent_id, [])
            seq = self._seqs.get(agent_id, 0)
            for ev in events:
                seq += 1
                buf.append((seq, dict(ev)))
            self._seqs[agent_id] = seq

    def get_since(self, agent_id: str, since: int) -> tuple[list[dict], int]:
        """Return (events_with_seq_injected, current_high_seq)."""
        with self._lock:
            buf = self._buffers.get(agent_id, [])
            high = self._seqs.get(agent_id, 0)
            out: list[dict] = []
            for seq, ev in buf:
                if seq > since:
                    e = dict(ev)
                    e["seq"] = seq
                    out.append(e)
            return out, high

    def clear(self, agent_id: Optional[str] = None) -> None:
        with self._lock:
            if agent_id:
                self._buffers.pop(agent_id, None)
                self._seqs.pop(agent_id, None)
                self._cursors.pop(agent_id, None)
            else:
                self._buffers.clear()
                self._seqs.clear()
                self._cursors.clear()


# Module-level singleton (the HTTP handler is stateless across requests).
_event_buffer = _EventBuffer()


def install_event_intercept(agent_registry) -> None:
    """Route local-bridge request events into the in-process buffer.

    Patch ``_push_events`` rather than ``_do_post_events`` so offline mode
    works even when the CLI has no cloud ``agent_id`` (the normal method
    intentionally drops events before they reach ``_do_post_events`` in that
    state). Requests received from the local bridge are tracked explicitly;
    unrelated cloud requests keep using the original sender. This prevents a
    local /helpwo session from hijacking or duplicating an already-connected
    cloud agent's event stream.
    """
    if hasattr(agent_registry, "_orig_push_events_for_helpwo"):
        return
    orig = agent_registry._push_events

    def _local_push(events: list, req_id: str = None) -> None:
        if not events:
            return
        with _local_requests_lock:
            is_local = bool(req_id and req_id in _local_request_ids)
        if not is_local:
            orig(events, req_id=req_id)
            return

        prepared = []
        saw_final = False
        for raw in events:
            event = dict(raw)
            event.setdefault("reqId", req_id)
            event.setdefault("meta", {})
            prepared.append(event)
            saw_final = saw_final or event.get("type") == "final"
        _event_buffer.append(_effective_agent_id(agent_registry), prepared)
        if saw_final:
            with _local_requests_lock:
                _local_request_ids.discard(req_id)

    agent_registry._orig_push_events_for_helpwo = orig
    agent_registry._push_events = _local_push


def uninstall_event_intercept(agent_registry) -> None:
    """Restore the original event dispatcher."""
    orig = getattr(agent_registry, "_orig_push_events_for_helpwo", None)
    if orig is not None:
        agent_registry._push_events = orig
        del agent_registry._orig_push_events_for_helpwo


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

# Paths that look like files but should serve index.html (SPA routing).
_SPA_ROUTES = re.compile(r"^/(?!api/|assets/|favicon\.|icons\.|oauth-)")


def _resolve_dist_path(dist_dir: Path, path: str) -> Optional[Path]:
    """Resolve a URL path to a real file under dist_dir, safely.

    Returns None if the path escapes dist_dir or doesn't exist.
    """
    # Strip query, decode
    path = unquote(path.split("?")[0])
    if not path or path == "/":
        path = "/index.html"
    # Normalise: prevent path traversal
    candidate = (dist_dir / path.lstrip("/")).resolve()
    try:
        candidate.relative_to(dist_dir.resolve())
    except ValueError:
        return None  # escaped dist_dir
    if candidate.is_file():
        return candidate
    # SPA fallback: non-asset, non-api paths serve index.html
    if _SPA_ROUTES.match(path) and (dist_dir / "index.html").is_file():
        return (dist_dir / "index.html").resolve()
    return None


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _HelpwoHandler(BaseHTTPRequestHandler):
    """Handles API + static file requests for /helpwo.

    The handler accesses module-level globals (_event_buffer, _agent_registry,
    _dist_dir) set by start_server().  This avoids per-instance state since
    BaseHTTPRequestHandler instantiates a new object per request.
    """

    # Quieter logging - the default writes every request to stderr.
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # -- helpers --

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # CORS headers so the browser's same-origin policy is satisfied
        # (all requests are localhost, but be explicit).
        self.send_header("Access-Control-Allow-Origin", _self_origin())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self._emit_auth_cookie()
        self.end_headers()
        self.wfile.write(data)

    def _emit_auth_cookie(self) -> None:
        """Emit the Set-Cookie queued by a successful ?token= exchange."""
        pending = getattr(self, "_pending_cookie", "")
        if pending:
            self.send_header("Set-Cookie", pending)
            self._pending_cookie = ""

    def _trusted_host(self) -> bool:
        """Reject DNS-rebinding requests before they reach loopback APIs."""
        host = (self.headers.get("Host") or "").split(":", 1)[0].strip().lower()
        if host in _allowed_hosts():
            return True
        self._json(421, {"error": "local Helpwo bridge: host not allowed"})
        return False

    # -- authentication (Jupyter's model) --
    #
    # The token arrives once in the URL, is exchanged for a cookie, and is not
    # needed again. The browser carries the cookie on its own, so the Helpwo
    # frontend needs no change: every one of its /api calls is already
    # same-origin with credentials: 'include'.
    #
    # Before this there was no authentication at all — any process on the
    # machine could drive the bridge, and any web page could POST to it.

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _authenticated(self, query: dict) -> bool:
        token = _auth_token()
        if not token:
            return True  # token disabled explicitly
        if hmac.compare_digest(self._cookie(_COOKIE_NAME), token):
            return True
        supplied = (query.get("token") or [""])[0]
        if supplied and hmac.compare_digest(supplied, token):
            self._set_auth_cookie(token)
            return True
        header = (self.headers.get("Authorization") or "")
        if header.startswith("token ") and hmac.compare_digest(header[6:].strip(), token):
            return True
        return False

    def _set_auth_cookie(self, token: str) -> None:
        # Marked for the rest of this response cycle; _json/_static emit it.
        self._pending_cookie = (
            f"{_COOKIE_NAME}={token}; Path=/; SameSite=Strict; HttpOnly; Max-Age=31536000"
        )

    def _require_auth(self, parsed) -> bool:
        if self._authenticated(parse_qs(parsed.query)):
            return True
        self._json(403, {
            "error": "authentication required",
            "detail": "open the URL printed by /helpwo, which carries ?token=",
        })
        return False

    def _same_origin(self) -> bool:
        """Reject cross-site state changes without needing frontend cooperation.

        Two independent checks, either of which is enough:
          - Sec-Fetch-Site, which the browser sets and a page cannot forge;
          - Origin, matched against this server's own origins.

        This replaces Jupyter's _xsrf header, which would require the frontend
        to send something it does not send today.
        """
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in ("same-origin", "none"):
            return True
        if site in ("cross-site", "same-site"):
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True  # non-browser client (curl, the CLI itself)
        try:
            host = urlparse(origin).hostname or ""
        except ValueError:
            return False
        return host.lower() in _allowed_hosts()

    def _guard_write(self, parsed) -> bool:
        """Everything a state-changing request must satisfy.

        The Content-Type check is load-bearing, not cosmetic: without it a
        cross-site form post of text/plain is a "simple request", skips the
        CORS preflight entirely, and reaches /api/local-fs/write. Requiring
        JSON forces a preflight, which the origin check then refuses.
        """
        if not self._require_auth(parsed):
            return False
        if not self._same_origin():
            self._json(403, {"error": "cross-site request refused"})
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype and ctype != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return False
        return True

    def _static(self, path: str) -> None:
        dist_dir = _dist_dir()
        if dist_dir is None:
            self._json(503, {"error": "dist directory not configured"})
            return
        fp = _resolve_dist_path(dist_dir, path)
        if fp is None:
            self.send_error(404)
            return
        ext = fp.suffix.lower()
        mime = _MIME_TYPES.get(ext, "application/octet-stream")
        try:
            data = fp.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        # The very first page load is what carries ?token=, so the static
        # response is where the cookie has to be set.
        self._emit_auth_cookie()
        self.end_headers()
        self.wfile.write(data)

    def _content_length_ok(self) -> bool:
        """Reject an oversized body before reading any of it into memory.

        Local-fs writes are base64-encoded (~33% inflation), so the raw JSON
        body can legitimately run larger than _MAX_LOCAL_FILE_BYTES; cap it
        generously above that instead of trusting a client-declared
        Content-Length unbounded. Loopback-only binding already limits who
        can reach this at all, but a buggy or compromised same-machine
        caller shouldn't be able to make this process buffer an arbitrary
        multi-GB body into memory on a whim. Checked once in do_POST before
        any handler runs, so there's exactly one response written either way
        (no risk of a handler also writing a response after this already did).
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return True  # malformed header — let _read_body's own parsing fail closed
        if length > _MAX_BODY_BYTES:
            self._json(413, {"error": "request body too large"})
            return False
        return True

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- route dispatch --

    def do_GET(self) -> None:
        if not self._trusted_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        # A GET is not a state change, so it needs the token but not the
        # cross-site checks — and it is the request that exchanges ?token=
        # for the cookie every later request rides on.
        if not self._require_auth(parsed):
            return

        # API routes
        if path == "/api/vnc":
            return self._handle_vnc(parsed)
        if path == "/api/agents":
            return self._handle_list_agents()
        m = re.match(r"^/api/agents/([^/]+)/updates$", path)
        if m:
            qs = parse_qs(parsed.query)
            since = int(qs.get("since", ["0"])[0])
            return self._handle_updates(unquote(m.group(1)), since)
        if path == "/api/auth/get-session":
            return self._handle_auth_session()
        if path == "/api/balance":
            return self._handle_balance()
        if path == "/api/models":
            return self._handle_models()
        if path == "/api/usage":
            return self._handle_usage()
        if path == "/api/sso/login":
            return self._handle_sso_login(parsed.query)
        if path == "/api/local-fs/root":
            return self._handle_local_fs_root()
        if path == "/api/local-fs/list":
            qs = parse_qs(parsed.query)
            return self._handle_local_fs_list(qs.get("path", [""])[0])
        if path == "/api/local-fs/read":
            qs = parse_qs(parsed.query)
            return self._handle_local_fs_read(qs.get("path", [""])[0])
        if path == "/api/local-fs/stat":
            qs = parse_qs(parsed.query)
            return self._handle_local_fs_stat(qs.get("path", [""])[0])

        # Static files
        return self._static(path)

    def do_POST(self) -> None:
        if not self._trusted_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._guard_write(parsed):
            return  # response already written

        if not self._content_length_ok():
            return  # 413 already written; nothing else may write a response

        m = re.match(r"^/api/agents/([^/]+)/send$", path)
        if m:
            agent_id = unquote(m.group(1))
            body = self._read_body()
            return self._handle_send(agent_id, body)
        if path == "/api/chat/stream":
            return self._handle_chat_stream()
        if path == "/api/chat":
            return self._handle_chat()
        if path == "/api/generate-image":
            return self._handle_generate_image()
        # The frontend calls /api/helpwo/search; /api/search is the older name
        # the cloud backend also answers. Both land here.
        if path in ("/api/helpwo/search", "/api/search"):
            return self._handle_search()
        if path == "/api/fetch":
            return self._handle_fetch()
        if path == "/api/auth/get-session":
            return self._handle_auth_session()
        if path == "/api/local-fs/write":
            return self._handle_local_fs_write(self._read_body())
        if path == "/api/local-fs/mkdir":
            return self._handle_local_fs_mkdir(self._read_body())
        if path == "/api/local-fs/delete":
            return self._handle_local_fs_delete(self._read_body())
        if path == "/api/local-fs/rename":
            return self._handle_local_fs_rename(self._read_body())
        if path == "/api/local-fs/move":
            return self._handle_local_fs_move(self._read_body())

        self._json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        if not self._trusted_host():
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _self_origin())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # -- API handlers --

    def _handle_list_agents(self) -> None:
        reg = _agent_registry()
        if reg is None:
            # No agent registered yet - return empty list (frontend handles this).
            self._json(200, [])
            return
        import socket as _sock
        import platform
        entry = {
            "id": _effective_agent_id(reg),
            "name": reg.agent_name or "cli",
            "hostname": _sock.gethostname(),
            "os": platform.system(),
            "shell": os.environ.get("SHELL", ""),
            "cwd": os.getcwd(),
            "status": "running",
            "online": True,
            "parentId": reg.parent_remote_id or None,
            "childIds": [],
            "terminal": reg.terminal_meta or None,
            "goal": f"CLI agent '{reg.agent_name}' on {_sock.gethostname()}",
            "workspacePath": str(_local_root() or ""),
            "localBridge": True,
        }
        self._json(200, [entry])

    def _handle_updates(self, agent_id: str, since: int) -> None:
        reg = _agent_registry()
        if reg is None or agent_id != _effective_agent_id(reg):
            self._json(404, {"error": "unknown agent"})
            return
        events, high = _event_buffer.get_since(agent_id, since)
        self._json(200, {"events": events, "seq": high})

    def _handle_send(self, agent_id: str, body: dict) -> None:
        reg = _agent_registry()
        if reg is None:
            self._json(503, {"error": "local runtime unavailable"})
            return
        if agent_id != _effective_agent_id(reg):
            self._json(404, {"error": "unknown agent"})
            return

        kind = body.get("kind", "chat")
        req_id = body.get("reqId") or body.get("id") or ""
        payload = body.get("payload") or {}

        # If the message has no reqId, generate one (the frontend always
        # sends one, but be defensive).
        if not req_id:
            import time
            req_id = f"local-{int(time.time() * 1000)}"

        msg = {
            "reqId": req_id,
            "kind": kind,
            "payload": payload,
        }

        # Dispatch via the registry's bounded executor pool, exactly like
        # _poll_loop does for remote messages.
        is_control = kind in reg.REMOTE_CONTROL_KINDS
        executor = (reg._remote_control_executor if is_control
                    else reg._remote_executor)
        if executor is None:
            self._json(503, {"error": "executor not ready"})
            return

        if not reg._reserve_remote_capacity(is_control):
            self._json(429, {"error": "local runtime is busy"})
            return

        with _local_requests_lock:
            _local_request_ids.add(req_id)
        try:
            executor.submit(
                reg._run_bounded_remote, msg,
                reg._state_cb or (lambda: {}),
                reg._chat_cb or (lambda: []),
                is_control,
            )
        except RuntimeError:
            with _local_requests_lock:
                _local_request_ids.discard(req_id)
            group = "control" if is_control else "task"
            with reg._remote_capacity_lock:
                reg._remote_accepted[group] = max(0, reg._remote_accepted[group] - 1)
                reg._remote_capacity_lock.notify_all()
            self._json(503, {"error": "executor is shutting down"})
            return

        # Acknowledge - the frontend polls /updates for the actual results.
        self._json(200, {"ok": True, "reqId": req_id})

    # -- Proxy helpers --

    def _proxy_json(self, method: str, path: str, body: dict | None = None) -> None:
        """Forward a JSON request to the remote backend and relay the response."""
        import backend_profiles
        import requests as _requests
        profile = backend_profiles.resolve(
            os.environ.get("LAINTAS_BACKEND") or "https://laintas.com")
        url = f"{profile.base_url}{path}"
        headers, cookies = backend_profiles.request_auth(profile, _session())
        headers["Content-Type"] = "application/json"
        try:
            resp = _requests.request(method, url, json=body, headers=headers,
                                     cookies=cookies, timeout=60, stream=False,
                                     allow_redirects=False)
        except Exception as e:
            self._json(502, {"error": f"backend unreachable: {e}"})
            return
        self.send_response(resp.status_code)
        ct = resp.headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(resp.content)))
        self.send_header("Access-Control-Allow-Origin", _self_origin())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        self.wfile.write(resp.content)

    def _proxy_json_get(self, path: str):
        """GET `path` from the backend with the CLI's credentials.

        Returns the decoded JSON, or None when the backend cannot answer — the
        caller decides what a local-mode fallback should look like rather than
        surfacing a 502 for something the UI only needs advisory data from.
        """
        import backend_profiles
        import requests as _requests
        try:
            profile = backend_profiles.resolve(
                os.environ.get("LAINTAS_BACKEND") or "https://laintas.com")
            headers, cookies = backend_profiles.request_auth(profile, _session())
            # Follow redirects here, unlike the streaming proxy: these are
            # read-only metadata endpoints and /api/balance answers behind a
            # 301, which a no-redirect fetch reports as failure and silently
            # degrades to the zero-balance fallback.
            resp = _requests.get(f"{profile.base_url}{path}", headers=headers,
                                 cookies=cookies, timeout=20, allow_redirects=True)
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    def _proxy_json(self, path: str, body: dict) -> None:
        """Forward a request to the remote backend and return its JSON reply."""
        import backend_profiles
        import requests as _requests
        profile = backend_profiles.resolve(
            os.environ.get("LAINTAS_BACKEND") or "https://laintas.com")
        url = f"{profile.base_url}{path}"
        headers, cookies = backend_profiles.request_auth(profile, _session())
        headers["Content-Type"] = "application/json"
        try:
            resp = _requests.post(url, json=body, headers=headers, cookies=cookies,
                                  timeout=120, allow_redirects=False)
        except Exception as e:
            self._json(502, {"error": f"backend unreachable: {e}"})
            return
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text[:500] or "backend returned no JSON"}
        self._json(resp.status_code, payload)

    def _proxy_stream(self, path: str, body: dict) -> None:
        """Forward a request to the remote backend and stream SSE response back."""
        import backend_profiles
        import requests as _requests
        profile = backend_profiles.resolve(
            os.environ.get("LAINTAS_BACKEND") or "https://laintas.com")
        url = f"{profile.base_url}{path}"
        headers, cookies = backend_profiles.request_auth(profile, _session())
        headers["Content-Type"] = "application/json"
        try:
            resp = _requests.post(url, json=body, headers=headers, cookies=cookies,
                                  stream=True, timeout=120, allow_redirects=False)
        except Exception as e:
            self._json(502, {"error": f"backend unreachable: {e}"})
            return
        self.send_response(resp.status_code)
        ct = resp.headers.get("Content-Type", "text/event-stream")
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", _self_origin())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- Stub handlers --

    # These four used to be stubs, which made local mode lie about itself.
    # Chat has always been proxied with the CLI's own credentials, so calls
    # were really billed to the signed-in account — while the UI was told
    # there was no session, no balance, no usage, and offered a made-up model
    # list. Serving the truth means proxying them like everything else.

    def _handle_auth_session(self) -> None:
        """Who the CLI is signed in as.

        Returned null before, so the UI showed a signed-out state even though
        every AI call it made was billed to this account.
        """
        session = _session() or {}
        payload = self._proxy_json_get("/api/auth/get-session")
        if payload is None:
            # Backend unreachable or unauthenticated: fall back to what the CLI
            # already knows, so the UI still names the account paying for the
            # calls rather than claiming nobody is signed in.
            if not session.get("token"):
                self._json(200, None)
                return
            payload = {"user": {
                "id": session.get("userId"),
                "email": session.get("userEmail"),
                "name": session.get("userName"),
            }}
        self._json(200, payload)

    def _handle_balance(self) -> None:
        """The signed-in account's real balance (was hardcoded to zero)."""
        payload = self._proxy_json_get("/api/balance")
        self._json(200, payload if payload is not None
                   else {"balance": 0, "subscription": None})

    def _handle_models(self) -> None:
        """The models the backend actually serves.

        This was a hardcoded list naming gpt-4o and claude-sonnet-4 under
        "OpenAI" and "Anthropic" providers. None of them exist on this gateway:
        picking one sent an unknown model upstream, which resolves to the
        default provider with the wrong key and bills at the unlisted-model
        tier. The real catalog comes from the backend.
        """
        payload = self._proxy_json_get("/api/models")
        self._json(200, payload if payload is not None else {
            "models": [], "providers": [], "contextWindows": {},
            "error": "model catalog unavailable — backend unreachable",
        })

    def _handle_usage(self) -> None:
        """Real usage for the signed-in account (was always empty)."""
        payload = self._proxy_json_get("/api/usage")
        self._json(200, payload if payload is not None
                   else {"usage": [], "total": 0})

    def _handle_sso_login(self, query: str) -> None:
        """Stub /api/sso/login - redirect to the main page (no SSO in local mode)."""
        qs = parse_qs(query)
        return_to = qs.get("return_to", [f"http://127.0.0.1:{_server_port()}/"])[0]
        self.send_response(302)
        self.send_header("Location", return_to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_chat_stream(self) -> None:
        """Proxy /api/chat/stream to the remote backend."""
        body = self._read_body()
        self._proxy_stream("/api/chat/stream", body)

    def _handle_chat(self) -> None:
        """Proxy the NON-streaming /api/chat to the remote backend.

        The cloud backend serves both /api/chat and /api/chat/stream and the
        frontend uses both — streaming for conversation, the plain endpoint for
        one-shot answers it just wants a value from. Only the streaming one was
        proxied here, so against a locally served Helpwo every non-streaming
        call 404'd: language detection fell back with "AI analysis failed", and
        HelpwoBridge.query (which previewed pages use to ask the model
        something) failed outright. Observed as `POST /api/chat -> 404` while
        driving the local UI in a browser.
        """
        body = self._read_body()
        self._proxy_json("/api/chat", body)

    def _handle_generate_image(self) -> None:
        """Proxy /api/generate-image to the remote backend."""
        body = self._read_body()
        self._proxy_json("POST", "/api/generate-image", body)

    # -- Live view: RFB over a WebSocket on this same port --
    #
    # The screen of the browser this machine drives, so a human can clear a
    # CAPTCHA or sign in by hand. It shares the port (and therefore the auth
    # cookie and the origin) with everything else here, and the bytes go
    # straight from x11vnc to the viewer — no relay, no signalling server.

    def _resolve_vnc_session(self, name: str):
        """Pick which browser session the viewer should see.

        A session waiting on the user wins over everything else. The viewer
        asks for "default" because its button has no session picker, so
        without this preference a fetch stuck on a challenge is invisible
        whenever any other session happens to exist.
        """
        try:
            import browser_session as _bs
        except ImportError:
            return None, "browser_session module unavailable"

        with _bs._browser_lock:
            sessions = list(_bs._browser_sessions.items())
        for _key, session in sessions:
            if getattr(session, "_needs_attention", False) and session.is_alive():
                return session, ""
        if name:
            session = _bs.get_browser_session(name)
            if session is not None and session.is_alive():
                return session, ""
        session = _bs.get_latest_browser_session()
        if session is None:
            return None, "no browser session is open"
        return session, ""

    def _handle_vnc(self, parsed) -> None:
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        upgrade = (self.headers.get("Upgrade") or "").strip().lower()
        if upgrade != "websocket" or not key:
            self._json(400, {"error": "expected a WebSocket upgrade"})
            return

        name = (parse_qs(parsed.query).get("session") or [""])[0].strip()
        session, err = self._resolve_vnc_session(name)
        rfb_port = getattr(session, "rfb_port", 0) if session is not None else 0
        if not rfb_port:
            self._json(503, {"error": err or "that session has no live view"})
            return

        try:
            rfb = socket.create_connection(("127.0.0.1", rfb_port), timeout=5)
        except OSError as e:
            self._json(502, {"error": f"cannot reach the live view: {e}"})
            return

        # Written straight to the socket rather than through send_response():
        # this handler speaks HTTP/1.0, where the base class marks the
        # connection for closing and the upgrade never survives the response.
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {_ws_accept_key(key)}\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            self.wfile.write(handshake)
            self.wfile.flush()
        except OSError:
            rfb.close()
            return

        self._pump_vnc(rfb)

    def _pump_vnc(self, rfb) -> None:
        """Shuttle RFB bytes between the viewer and x11vnc until either ends."""
        import select
        client = self.connection
        client.settimeout(None)
        rfb.settimeout(None)
        try:
            while True:
                ready, _w, _x = select.select([client, rfb], [], [], 60)
                if not ready:
                    # Keep middleboxes from dropping an idle viewer.
                    try:
                        client.sendall(_ws_frame(b"", opcode=0x9))  # ping
                    except OSError:
                        return
                    continue
                if rfb in ready:
                    data = rfb.recv(65536)
                    if not data:
                        return
                    try:
                        client.sendall(_ws_frame(data))
                    except OSError:
                        return
                if client in ready:
                    frame = _ws_read_frame(client)
                    if frame is None:
                        return
                    opcode, payload = frame
                    if opcode == 0x8:          # close
                        return
                    if opcode == 0x9:          # ping → pong
                        try:
                            client.sendall(_ws_frame(payload, opcode=0xA))
                        except OSError:
                            return
                        continue
                    if opcode == 0xA:          # pong
                        continue
                    if payload:
                        try:
                            rfb.sendall(payload)
                        except OSError:
                            return
        finally:
            try:
                rfb.close()
            except OSError:
                pass
            self.close_connection = True

    # -- Web search / fetch: served here, not proxied --
    #
    # These used to forward to the cloud backend. Running them in-process means
    # the browser inherits *this machine's* view of the web: its engine list
    # (including cn.bing, which is what works from inside China), its per-host
    # proxy routing, the challenge clearance it has already earned, and its
    # saved logins. A page the CLI can read is now a page Helpwo can read.
    #
    # It also fixes a plain break: the frontend calls /api/helpwo/search, which
    # this server had no route for at all, so search 404'd in local mode.

    def _web_search_module(self):
        try:
            import web_search
            return web_search
        except ImportError:
            return None

    def _handle_search(self) -> None:
        body = self._read_body()
        ws = self._web_search_module()
        if ws is None:
            self._json(503, {"error": "web_search module unavailable"})
            return
        query = str(body.get("query") or "").strip()
        if not query:
            self._json(400, {"error": "a query is required"})
            return
        try:
            max_results = min(max(int(body.get("maxResults", 8)), 1), 20)
        except (TypeError, ValueError):
            max_results = 8
        timelimit = body.get("timelimit")
        if timelimit not in ("d", "w", "m", "y"):
            timelimit = None
        engines = body.get("engines")
        if isinstance(engines, str):
            engines = [engines]
        elif not isinstance(engines, list):
            engines = None

        try:
            out = ws.search(query, max_results=max_results,
                            region=body.get("region"), timelimit=timelimit,
                            engines=engines)
        except Exception as e:
            self._json(502, {"error": f"search failed: {type(e).__name__}"})
            return

        if not out.get("ok"):
            self._json(502, {
                "error": out.get("error", "search failed"),
                "engines": out.get("engines_available"),
            })
            return
        # Shape matches what the frontend already expects from the cloud route,
        # including the "domain" field it renders as a tag next to each result.
        from urllib.parse import urlparse
        results = []
        for item in out.get("result") or []:
            entry = dict(item)
            if not entry.get("domain"):
                try:
                    entry["domain"] = urlparse(entry.get("url", "")).netloc
                except ValueError:
                    entry["domain"] = ""
            results.append(entry)
        self._json(200, {
            "query": query,
            "results": results,
            "engine": out.get("engine"),
        })

    def _handle_fetch(self) -> None:
        body = self._read_body()
        ws = self._web_search_module()
        if ws is None:
            self._json(503, {"error": "web_search module unavailable"})
            return
        url = str(body.get("url") or "").strip()
        if not url:
            self._json(400, {"error": "a URL is required"})
            return
        try:
            max_chars = min(max(int(body.get("maxChars", 16000)), 500), 200_000)
        except (TypeError, ValueError):
            max_chars = 16000
        try:
            timeout = min(max(int(body.get("timeout", 15)), 3), 60)
        except (TypeError, ValueError):
            timeout = 15
        # A saved login is only ever used when named, and identity_store still
        # checks the URL against that identity's own domains.
        identity = str(body.get("identity") or "").strip() or None

        try:
            out = ws.fetch(url, max_bytes=max_chars, timeout=timeout,
                           identity=identity)
        except Exception as e:
            self._json(502, {"error": f"fetch failed: {type(e).__name__}"})
            return

        if not out.get("ok"):
            self._json(502, {
                "error": out.get("error", "fetch failed"),
                "blocked": out.get("blocked"),
                "attempts": out.get("attempts"),
            })
            return
        self._json(200, {
            "url": out.get("final_url", url),
            "title": out.get("title", ""),
            "text": out.get("result", ""),
            "truncated": bool(out.get("truncated")),
            "transport": out.get("transport"),
            "note": out.get("note", ""),
        })

    # -- Local filesystem API --
    # Scoped to _local_root() (this process's cwd when /helpwo started).
    # Same-machine + loopback-only, so plain HTTP is enough — no P2P/WebRTC
    # handshake needed the way a genuine remote host requires. Every handler
    # resolves through _resolve_local_path(), which refuses anything outside
    # the root (symlink escapes, `..`) the same way RemoteProvider.pathOf
    # and webrtc_channel.py's _is_path_allowed do for the remote-mount path.

    def _entry_meta(self, p: Path) -> dict:
        st = p.stat()
        is_dir = p.is_dir()
        mime, _ = (None, None) if is_dir else mimetypes.guess_type(p.name)
        return {
            "name": p.name,
            "type": "folder" if is_dir else "file",
            "size": 0 if is_dir else st.st_size,
            "modifiedAt": st.st_mtime,
            "mimeType": mime,
        }

    def _handle_local_fs_root(self) -> None:
        root = _local_root()
        if root is None:
            self._json(200, {"available": False})
            return
        self._json(200, {"available": True, "root": str(root)})

    def _handle_local_fs_list(self, path: str) -> None:
        p = _resolve_local_path(path)
        if p is None:
            self._json(400, {"error": "path outside local workspace root"})
            return
        if not p.is_dir():
            self._json(404, {"error": "not a directory"})
            return
        try:
            entries = [self._entry_meta(child) for child in p.iterdir()]
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"entries": entries})

    def _handle_local_fs_stat(self, path: str) -> None:
        p = _resolve_local_path(path)
        if p is None or not p.exists():
            self._json(404, {"error": "not found"})
            return
        self._json(200, self._entry_meta(p))

    def _handle_local_fs_read(self, path: str) -> None:
        p = _resolve_local_path(path)
        if p is None or not p.is_file():
            self._json(404, {"error": "not found"})
            return
        try:
            size = p.stat().st_size
            if size > _MAX_LOCAL_FILE_BYTES:
                self._json(413, {"error": f"file too large ({size} bytes, cap {_MAX_LOCAL_FILE_BYTES})"})
                return
            data = p.read_bytes()
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        mime, _ = mimetypes.guess_type(p.name)
        self._json(200, {
            "contentBase64": base64.b64encode(data).decode("ascii"),
            "mimeType": mime,
            "size": len(data),
        })

    def _handle_local_fs_write(self, body: dict) -> None:
        p = _resolve_local_path(str(body.get("path") or ""))
        if p is None:
            self._json(400, {"error": "path outside local workspace root"})
            return
        if not p.parent.is_dir():
            self._json(400, {"error": "parent directory does not exist"})
            return
        try:
            raw_b64 = body.get("contentBase64")
            data = (base64.b64decode(raw_b64, validate=True) if raw_b64 is not None
                   else str(body.get("content") or "").encode("utf-8"))
            if len(data) > _MAX_LOCAL_FILE_BYTES:
                self._json(413, {"error": f"content too large (cap {_MAX_LOCAL_FILE_BYTES})"})
                return
            p.write_bytes(data)
        except (OSError, ValueError) as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ok": True})

    def _handle_local_fs_mkdir(self, body: dict) -> None:
        p = _resolve_local_path(str(body.get("path") or ""), follow_leaf=False)
        if p is None:
            self._json(400, {"error": "path outside local workspace root"})
            return
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ok": True})

    def _handle_local_fs_delete(self, body: dict) -> None:
        p = _resolve_local_path(str(body.get("path") or ""), follow_leaf=False)
        root = _local_root()
        if p is None or root is None:
            self._json(400, {"error": "path outside local workspace root"})
            return
        if p == root:
            self._json(400, {"error": "cannot delete the workspace root"})
            return
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
            else:
                self._json(404, {"error": "not found"})
                return
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ok": True})

    def _handle_local_fs_rename(self, body: dict) -> None:
        p = _resolve_local_path(str(body.get("path") or ""), follow_leaf=False)
        new_name = str(body.get("newName") or "").strip()
        if p is None or not new_name or "/" in new_name or new_name in (".", ".."):
            self._json(400, {"error": "invalid path or name"})
            return
        target = _resolve_local_path(str(Path(body.get("path") or "").parent / new_name))
        if target is None:
            self._json(400, {"error": "target path outside local workspace root"})
            return
        if target.exists():
            self._json(409, {"error": "target already exists"})
            return
        try:
            p.rename(target)
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ok": True})

    def _handle_local_fs_move(self, body: dict) -> None:
        p = _resolve_local_path(str(body.get("path") or ""), follow_leaf=False)
        new_parent = _resolve_local_path(str(body.get("newParentPath") or ""))
        if p is None or new_parent is None or not new_parent.is_dir():
            self._json(400, {"error": "invalid source or destination"})
            return
        target = new_parent / p.name
        if target.exists():
            self._json(409, {"error": "target already exists"})
            return
        try:
            shutil.move(str(p), str(target))
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ok": True})


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

# Module-level references set by start_server().
_server: Optional[ThreadingHTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_registry_ref: Optional[Any] = None  # AgentRegistry instance
_dist_dir_ref: Optional[Path] = None
_server_port_val: int = DEFAULT_PORT
_session_ref: Optional[dict] = None  # CLI session dict (for backend auth)
# Root directory the local-fs API is scoped to (this process's cwd at the
# time /helpwo started the server). Same-origin, loopback-only, no P2P
# handshake needed — unlike a genuine remote host, the browser and this
# server are on the same machine, so a plain HTTP file API is sufficient.
_local_root_ref: Optional[Path] = None
_local_agent_id_ref: str = ""
_local_requests_lock = threading.Lock()
_local_request_ids: set[str] = set()


def _agent_registry() -> Any:
    return _registry_ref


def _session() -> dict:
    return _session_ref or {}


def _dist_dir() -> Optional[Path]:
    return _dist_dir_ref


def _server_port() -> int:
    return _server_port_val


def _local_root() -> Optional[Path]:
    return _local_root_ref


def _effective_agent_id(registry: Any = None) -> str:
    reg = registry if registry is not None else _registry_ref
    remote_id = str(getattr(reg, "agent_id", "") or "")
    return remote_id or _local_agent_id_ref


_MAX_LOCAL_FILE_BYTES = 10 * 1024 * 1024  # 10MB, same order as the P2P get/put cap
_MAX_BODY_BYTES = 2 * _MAX_LOCAL_FILE_BYTES  # raw request body cap (base64 inflates ~33%)


def _resolve_local_path(path_str: str, *, follow_leaf: bool = True) -> Optional[Path]:
    """Resolve `path_str` against the local-fs root, refusing any escape.

    `path_str` may be an absolute path (the frontend's entry ids are full
    host paths, mirroring RemoteProvider's convention) or empty (meaning the
    root itself); a relative string is joined under the root as a fallback.
    Mirrors RemoteProvider.pathOf's containment check on the frontend and
    webrtc_channel.py's _is_path_allowed: resolve symlinks/`..` fully, then
    require the result to BE the root or a real descendant of it. Returns
    None (not an exception) for anything invalid so callers can respond
    with a clean 400 instead of leaking a stack trace.
    """
    root = _local_root_ref
    if root is None:
        return None
    path_str = (path_str or "").strip()
    try:
        candidate = Path(path_str) if path_str else root
        if not candidate.is_absolute():
            candidate = root / candidate
        if follow_leaf:
            resolved = candidate.resolve(strict=False)
        else:
            # Mutations of a directory entry (delete/rename/move) must act on
            # the symlink itself, not on the target produced by Path.resolve().
            # Resolve and contain the parent, then append only the leaf name.
            resolved_parent = candidate.parent.resolve(strict=False)
            resolved = resolved_parent / candidate.name
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _find_dist() -> Optional[Path]:
    """Locate the Helpwo dist directory.

    Search order:
    1. LAINTAS_HELPWO_DIST env var
    2. A sibling checkout (development layout) — a developer's own build wins
       over the downloaded one, so editing Helpwo shows up immediately
    3. The dist downloaded by the updater into ~/.laintas/helpwo/dist
    """
    env = os.environ.get("LAINTAS_HELPWO_DIST")
    if env:
        p = Path(env)
        if p.is_dir() and (p / "index.html").is_file():
            return p.resolve()
    # Development layout: /root/Helpwo is a sibling of /root/laintas_cli
    candidates = [
        Path("/root/Helpwo/dist"),
        Path(__file__).resolve().parent.parent / "Helpwo" / "dist",
    ]
    try:
        import updater
        candidates.append(Path(updater.helpwo_dist_dir()))
    except Exception:
        pass
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c.resolve()
    return None


def is_running() -> bool:
    return _server is not None and _server_thread is not None and _server_thread.is_alive()


def start_server(agent_registry: Any, dist_dir: Optional[Path] = None,
                 port: int = DEFAULT_PORT,
                 session: Optional[dict] = None,
                 host: str = "127.0.0.1",
                 token: Optional[str] = None,
                 tls_cert: Optional[str] = None,
                 tls_key: Optional[str] = None) -> tuple[bool, str]:
    """Start the local Helpwo gateway server.

    host defaults to loopback. Binding anywhere else is the operator's call —
    this server does not decide whether a machine should be reachable — but it
    always requires the token, and it says what a non-loopback address costs.

    token defaults to a fresh random one. Pass "" to disable authentication;
    only sensible for a throwaway sandbox.

    tls_cert/tls_key serve HTTPS instead of HTTP. This is not decoration: a
    browser grants Web Crypto, Service Workers, File System Access and the
    clipboard only in a SECURE CONTEXT, which means HTTPS or localhost. Reached
    over plain HTTP at a routable address, crypto.randomUUID and crypto.subtle
    are simply absent, and the terminal, the agent loop and the browser runtime
    all fail on startup because each generates an id. Serving a real
    certificate is what makes this usable as a network service at all — the
    alternative is not "slightly degraded", it is broken.

    Returns (success, message).
    """
    global _server, _server_thread, _registry_ref, _dist_dir_ref, _server_port_val, _session_ref, _local_root_ref, _local_agent_id_ref
    global _auth_token_value, _bind_host, _scheme, _cert_hostnames

    if is_running():
        return True, f"already running on {get_url()}"

    if session is not None:
        _session_ref = session

    # Resolve dist directory
    resolved_dist = dist_dir or _find_dist()
    if resolved_dist is None:
        return False, ("Helpwo dist directory not found. Set LAINTAS_HELPWO_DIST "
                       "or ensure /root/Helpwo/dist exists.")

    _registry_ref = agent_registry
    _dist_dir_ref = resolved_dist.resolve()
    _server_port_val = port
    # Local mode never requires cloud registration. The stable-for-process
    # alias lets the same Helpwo frontend use the existing agent request
    # contract while all traffic remains on loopback.
    _local_agent_id_ref = f"local-{os.getpid():x}"
    _local_root_ref = Path(os.getcwd()).resolve()
    _bind_host = host or "127.0.0.1"
    _auth_token_value = secrets.token_urlsafe(32) if token is None else token

    # Intercept events so they stay in-process instead of HTTP-round-tripping.
    install_event_intercept(agent_registry)

    # Prepare TLS before binding, so a bad certificate fails loudly here
    # rather than as an unexplained handshake error in the browser later.
    ssl_context = None
    if tls_cert or tls_key:
        if not (tls_cert and tls_key):
            uninstall_event_intercept(agent_registry)
            _registry_ref = None
            return False, "TLS needs both a certificate and a key"
        try:
            import ssl as _ssl
            ssl_context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_context.minimum_version = _ssl.TLSVersion.TLSv1_2
            ssl_context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
            _cert_hostnames = _hostnames_in_cert(tls_cert)
        except Exception as e:
            uninstall_event_intercept(agent_registry)
            _registry_ref = None
            return False, f"cannot load TLS certificate: {e}"

    try:
        srv = ThreadingHTTPServer(
            (_bind_host, port), _HelpwoHandler,
        )
        if ssl_context is not None:
            srv.socket = ssl_context.wrap_socket(srv.socket, server_side=True)
    except OSError as e:
        uninstall_event_intercept(agent_registry)
        _registry_ref = None
        _dist_dir_ref = None
        _local_root_ref = None
        _local_agent_id_ref = ""
        return False, f"cannot bind {_bind_host}:{port}: {e}"

    srv.daemon_threads = True
    _scheme = "https" if ssl_context is not None else "http"
    _server = srv

    t = threading.Thread(
        target=srv.serve_forever, daemon=True,
        name="helpwo-gateway",
    )
    t.start()
    _server_thread = t

    # The actual bound port, so port=0 (ephemeral) reports something usable.
    try:
        _server_port_val = srv.server_address[1]
    except Exception:
        pass

    return True, f"Helpwo gateway running at {get_url()}"


def stop_server() -> None:
    """Stop the local Helpwo gateway server."""
    global _server, _server_thread, _registry_ref, _dist_dir_ref, _session_ref, _local_root_ref, _local_agent_id_ref

    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    if _server_thread is not None:
        _server_thread.join(timeout=3)
        _server_thread = None

    if _registry_ref is not None:
        uninstall_event_intercept(_registry_ref)
        _registry_ref = None

    _dist_dir_ref = None
    _session_ref = None
    _local_root_ref = None
    _local_agent_id_ref = ""
    with _local_requests_lock:
        _local_request_ids.clear()
    _event_buffer.clear()


def get_url(with_token: bool = False) -> str:
    """Base URL of the running server (or empty string).

    with_token appends the one the browser needs on its first visit; after
    that the cookie carries it.
    """
    if not is_running():
        return ""
    host = _bind_host if _bind_host not in ("0.0.0.0", "::") else "127.0.0.1"
    url = f"{_scheme}://{host}:{_server_port_val}"
    if with_token and _auth_token():
        url += f"/?token={_auth_token()}"
    return url


def auth_token() -> str:
    """The token this server requires ("" when authentication is disabled)."""
    return _auth_token()


def bind_host() -> str:
    return _bind_host


def scheme() -> str:
    """"http" or "https" — what the running server actually speaks."""
    return _scheme


def is_secure_context() -> bool:
    """Whether a browser will treat this origin as a secure context.

    HTTPS anywhere, or plain HTTP on loopback. Everything the frontend needs
    from Web Crypto and Service Workers hangs on this being true.
    """
    return _scheme == "https" or _bind_host in ("127.0.0.1", "localhost", "::1")
