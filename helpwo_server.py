"""Local gateway for /helpwo: serves Helpwo's static dist + a minimal
in-process API that bridges the frontend to the running laintas_cli.

Design:
- stdlib http.server (no Flask dependency).
- Binds 127.0.0.1 ONLY - never reachable from the network.
- Three API endpoints mirror Helpwo's gateway contract:
    GET  /api/agents                       - list registered agents
    POST /api/agents/<id>/send             - dispatch a message to the CLI
    GET  /api/agents/<id>/updates?since=N  - poll buffered events
- Static files served from the dist directory with path-traversal guard.
- Events are captured in-process by overriding AgentRegistry._do_post_events,
  so the existing _push_events / _push_final pipeline works unchanged.
"""

from __future__ import annotations

import json
import os
import re
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
    """Monkey-patch _do_post_events so events stay in-process.

    The original _do_post_events POSTs to the remote backend.  In local mode
    the backend *is* us, so we short-circuit: stuff events into _event_buffer
    instead of making an HTTP round-trip to ourselves.
    """
    orig = agent_registry._do_post_events

    def _local_post(events: list) -> None:
        agent_id = agent_registry.agent_id
        if agent_id:
            _event_buffer.append(agent_id, events)

    # Keep a reference to the original for uninstall.
    agent_registry._orig_do_post_events = orig
    agent_registry._do_post_events = _local_post


def uninstall_event_intercept(agent_registry) -> None:
    """Restore the original _do_post_events."""
    orig = getattr(agent_registry, "_orig_do_post_events", None)
    if orig is not None:
        agent_registry._do_post_events = orig
        del agent_registry._orig_do_post_events


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
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:%d" % _server_port())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        self.wfile.write(data)

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
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- route dispatch --

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # API routes
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

        # Static files
        return self._static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        m = re.match(r"^/api/agents/([^/]+)/send$", path)
        if m:
            agent_id = unquote(m.group(1))
            body = self._read_body()
            return self._handle_send(agent_id, body)
        if path == "/api/chat/stream":
            return self._handle_chat_stream()
        if path == "/api/generate-image":
            return self._handle_generate_image()
        if path == "/api/search":
            return self._handle_search()
        if path == "/api/fetch":
            return self._handle_fetch()
        if path == "/api/auth/get-session":
            return self._handle_auth_session()

        self._json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:%d" % _server_port())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # -- API handlers --

    def _handle_list_agents(self) -> None:
        reg = _agent_registry()
        if reg is None or not reg.agent_id:
            # No agent registered yet - return empty list (frontend handles this).
            self._json(200, [])
            return
        import socket as _sock
        import platform
        entry = {
            "id": reg.agent_id,
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
        }
        self._json(200, [entry])

    def _handle_updates(self, agent_id: str, since: int) -> None:
        events, high = _event_buffer.get_since(agent_id, since)
        self._json(200, {"events": events, "seq": high})

    def _handle_send(self, agent_id: str, body: dict) -> None:
        reg = _agent_registry()
        if reg is None or not reg.agent_id:
            self._json(503, {"error": "no agent registered"})
            return
        if agent_id != reg.agent_id:
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

        try:
            executor.submit(
                reg._run_bounded_remote, msg,
                reg._state_cb or (lambda: {}),
                reg._chat_cb or (lambda: []),
                is_control,
            )
        except RuntimeError:
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
        self.send_header("Access-Control-Allow-Origin", f"http://127.0.0.1:{_server_port()}")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        self.wfile.write(resp.content)

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
        self.send_header("Access-Control-Allow-Origin", f"http://127.0.0.1:{_server_port()}")
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

    def _handle_auth_session(self) -> None:
        """Stub better-auth /api/auth/get-session - returns null session (local mode)."""
        self._json(200, None)

    def _handle_balance(self) -> None:
        """Stub /api/balance - returns zero balance (no billing in local mode)."""
        self._json(200, {"balance": 0, "subscription": None})

    def _handle_models(self) -> None:
        """Stub /api/models - returns a minimal model list."""
        self._json(200, {
            "models": ["deepseek-v4-flash", "deepseek-v4", "gpt-4o", "claude-sonnet-4"],
            "providers": [
                {"id": "deepseek", "name": "DeepSeek", "models": ["deepseek-v4-flash", "deepseek-v4"]},
                {"id": "openai", "name": "OpenAI", "models": ["gpt-4o"]},
                {"id": "anthropic", "name": "Anthropic", "models": ["claude-sonnet-4"]},
            ],
            "contextWindows": {},
        })

    def _handle_usage(self) -> None:
        """Stub /api/usage - returns empty usage (no billing in local mode)."""
        self._json(200, {"usage": [], "total": 0})

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

    def _handle_generate_image(self) -> None:
        """Proxy /api/generate-image to the remote backend."""
        body = self._read_body()
        self._proxy_json("POST", "/api/generate-image", body)

    def _handle_search(self) -> None:
        """Proxy /api/search to the remote backend."""
        body = self._read_body()
        self._proxy_json("POST", "/api/search", body)

    def _handle_fetch(self) -> None:
        """Proxy /api/fetch to the remote backend."""
        body = self._read_body()
        self._proxy_json("POST", "/api/fetch", body)


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


def _agent_registry() -> Any:
    return _registry_ref


def _session() -> dict:
    return _session_ref or {}


def _dist_dir() -> Optional[Path]:
    return _dist_dir_ref


def _server_port() -> int:
    return _server_port_val


def _find_dist() -> Optional[Path]:
    """Locate the Helpwo dist directory.

    Search order:
    1. LAINTAS_HELPWO_DIST env var
    2. Sibling /root/Helpwo/dist (development layout)
    3. Package-bundled dist (future: shipped inside laintas_cli)
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
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c.resolve()
    return None


def is_running() -> bool:
    return _server is not None and _server_thread is not None and _server_thread.is_alive()


def start_server(agent_registry: Any, dist_dir: Optional[Path] = None,
                 port: int = DEFAULT_PORT,
                 session: Optional[dict] = None) -> tuple[bool, str]:
    """Start the local Helpwo gateway server.

    Returns (success, message).
    """
    global _server, _server_thread, _registry_ref, _dist_dir_ref, _server_port_val, _session_ref

    if is_running():
        return True, f"already running on http://127.0.0.1:{_server_port_val}"

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

    # Intercept events so they stay in-process instead of HTTP-round-tripping.
    install_event_intercept(agent_registry)

    try:
        srv = ThreadingHTTPServer(
            ("127.0.0.1", port), _HelpwoHandler,
        )
    except OSError as e:
        uninstall_event_intercept(agent_registry)
        return False, f"cannot bind 127.0.0.1:{port}: {e}"

    srv.daemon_threads = True
    _server = srv

    t = threading.Thread(
        target=srv.serve_forever, daemon=True,
        name="helpwo-gateway",
    )
    t.start()
    _server_thread = t

    return True, f"Helpwo gateway running at http://127.0.0.1:{port}"


def stop_server() -> None:
    """Stop the local Helpwo gateway server."""
    global _server, _server_thread, _registry_ref, _dist_dir_ref, _session_ref

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
    _event_buffer.clear()


def get_url() -> str:
    """Return the base URL of the running server (or empty string)."""
    if is_running():
        return f"http://127.0.0.1:{_server_port_val}"
    return ""
