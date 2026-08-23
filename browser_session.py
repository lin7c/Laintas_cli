"""Headless-browser session for laintas_cli — live-view stack.

Spawns Xvfb + Chrome (headed-in-virtual-display, --remote-debugging-port) +
x11vnc. The AI side drives Chrome over CDP (http://127.0.0.1:<debug-port>) —
see the browser.* tools in tools.py.

The user-facing live view is peer-to-peer: x11vnc serves RFB on
127.0.0.1:<rfb_port>, and webrtc_channel.py's VNC bridge carries those bytes to
the browser's noVNC over a WebRTC DataChannel, so the framebuffer never touches
the backend. See Helpwo/docs/vnc-p2p-design.md. (An earlier revision relayed RFB
through a backend /vnc WebSocket; that endpoint was never deployed and the code
for it has been removed — don't reintroduce it.)

Unix-only: requires Xvfb for the live view.

Required system packages (probed at start; missing → clear error, no crash):
  Xvfb, x11vnc, google-chrome | chromium | chromium-browser
"""

from __future__ import annotations

import os
import sys
import glob
import signal
import socket
import subprocess
import threading
import time
import json
import base64
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Optional, List

# Chrome binaries tried in order.
_CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd) or shutil.which(f"{cmd}.exe")


def find_chrome() -> Optional[str]:
    for c in _CHROME_CANDIDATES:
        p = _which(c)
        if p:
            return p
    return None


def _ip_is_blocked(ip_str: str) -> bool:
    """True if an IP is loopback / private / link-local / reserved — i.e. the
    host's own network position should not be reachable from a page we browse
    (blocks SSRF to cloud metadata 169.254.169.254, internal services, etc.)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # un-parseable → refuse
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate_browse_url(raw: str, allow_loopback: bool = False) -> str:
    """Normalize and security-check a URL the remote Chrome will navigate to.
    Returns an absolute http(s) URL or raises ValueError. Refuses non-http(s)
    schemes and any host that resolves to a loopback/private/link-local address
    so a driven page cannot pivot into the host's internal network or metadata.

    allow_loopback=True permits 127.0.0.0/8 ONLY (::1 included) — for the
    user-approved webtest flow, whose whole point is testing the host's own
    dev server. Private/link-local/metadata ranges stay blocked regardless."""
    from urllib.parse import urlparse
    s = (raw or "").strip()
    if not s:
        raise ValueError("a URL is required")
    if "://" not in s:
        s = "https://" + s
    u = urlparse(s)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs may be browsed (got '{u.scheme}')")
    host = u.hostname
    if not host:
        raise ValueError(f"not a valid URL: {raw!r}")
    # Resolve and check every address the host maps to (defeats DNS-based SSRF).
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ValueError(f"cannot resolve host {host!r}: {e}")
    import ipaddress
    for info in infos:
        ip = info[4][0]
        if allow_loopback:
            try:
                if ipaddress.ip_address(ip).is_loopback:
                    continue
            except ValueError:
                pass
        if _ip_is_blocked(ip):
            raise ValueError(
                f"refusing to browse {host!r}: resolves to non-public address {ip} "
                f"(loopback/private/link-local/metadata are blocked)")
    return u.geturl()


def _chown_tree(path: str, uid: int, gid: int) -> None:
    """Recursively chown a directory so a dropped-privilege Chrome can use it."""
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except OSError:
                pass


def _unpriv_user():
    """Pick a non-root user to run Chrome as, so the setuid sandbox can stay on
    even in a root container. Returns (uid, gid, name, home) or None if none is
    suitable / not running as root / not Unix."""
    if os.geteuid() != 0:
        return None
    try:
        import pwd
    except ImportError:
        return None
    # Prefer a dedicated browser user; fall back to common low-priv accounts.
    for name in (os.environ.get("LAINTAS_CHROME_USER", ""), "chrome", "chromium", "nobody"):
        if not name:
            continue
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            continue
        if pw.pw_uid == 0:
            continue
        return (pw.pw_uid, pw.pw_gid, pw.pw_name, pw.pw_dir or "/tmp")
    return None


def _check_host_deps() -> Optional[str]:
    """Return an error message string if a required binary is missing, else None."""
    missing = []
    if not _which("Xvfb"):
        missing.append("Xvfb (apt install xvfb)")
    if not _which("x11vnc"):
        missing.append("x11vnc (apt install x11vnc)")
    if not find_chrome():
        missing.append("google-chrome or chromium")
    if missing:
        return "Missing system packages: " + ", ".join(missing)
    return None


# ── Free port / display allocation ───────────────────────────────────────

def _free_tcp_port(start: int = 9222, end: int = 9322) -> int:
    for port in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"no free TCP port in [{start},{end})")


def _free_display(start: int = 99, end: int = 200) -> int:
    """Find a free X display number by checking the lock file."""
    for n in range(start, end):
        lock = os.path.join(_X_LOCK_DIR, f".X{n}-lock")
        if not os.path.exists(lock):
            # Also confirm no process is listening on the abstract socket.
            return n
    raise RuntimeError(f"no free X display in :{start}..:{end}")


_STALE_TEMP_PREFIXES = ("hwo-chrome-", "hwo-vnc-")
_STALE_TEMP_AGE = 24 * 3600
# An orphan has to be old enough that it cannot be a session still starting up.
_ORPHAN_MIN_AGE = 3600
# Patched in tests; X itself always uses /tmp.
_X_LOCK_DIR = "/tmp"


def _proc_stat(pid: int):
    """Return (ppid, comm) for a live pid, or None. Linux /proc only."""
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            data = fh.read()
        # comm can contain spaces and parens, so split on the LAST ')'.
        close = data.rindex(")")
        comm = data[data.index("(") + 1:close]
        return int(data[close + 2:].split()[1]), comm
    except (OSError, ValueError, IndexError):
        return None


def _reap_stale_displays(start: int = 99, end: int = 200) -> int:
    """Release X displays whose session died without cleaning up.

    Two kinds of debris, and only the first is inert:

      * a `/tmp/.X<n>-lock` naming a pid that no longer exists — harmless in
        itself, but `_free_display` treats any lock as occupied, so every leaked
        lock permanently removes a display number from the pool;
      * an `Xvfb`/`x11vnc` pair that outlived its BrowserSession. Those hold a
        real lock and a real RFB port for as long as the box stays up (measured:
        7.8 days, five displays, on a machine with 200 to give away).

    Killing processes unattended needs a high bar, so an orphan must satisfy
    all of: reparented to init (its creator is gone), older than an hour, and no
    surviving Chrome profile directory for that display. A session whose CLI is
    still alive keeps its real ppid and is never touched.
    """
    if not os.path.isdir("/proc"):
        return 0
    freed = 0
    for n in range(start, end):
        lock = os.path.join(_X_LOCK_DIR, f".X{n}-lock")
        try:
            if not os.path.exists(lock):
                continue
            with open(lock, "r") as fh:
                holder = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            continue

        stat = _proc_stat(holder) if holder else None
        if stat is None:
            # Nothing owns it. Drop the lock and the socket so the number
            # returns to the pool.
            _remove_display_files(n)
            freed += 1
            continue

        ppid, comm = stat
        if comm != "Xvfb" or ppid != 1:
            continue                      # live session, or not ours
        try:
            # The lock is written when Xvfb starts, so its mtime is the
            # session's age — more direct than deriving it from /proc.
            if time.time() - os.path.getmtime(lock) < _ORPHAN_MIN_AGE:
                continue
        except OSError:
            continue
        if glob.glob(os.path.join(tempfile.gettempdir(), f"hwo-chrome-{n}-*")):
            continue                      # profile still there — leave it alone

        _kill_display_stack(n, holder)
        _remove_display_files(n)
        freed += 1
    return freed


def _kill_display_stack(display_n: int, xvfb_pid: int) -> None:
    """SIGTERM the orphaned Xvfb and the x11vnc attached to it."""
    victims = [xvfb_pid]
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            stat = _proc_stat(pid)
            if not stat or stat[1] != "x11vnc":
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
            if b"-display" in argv and f":{display_n}".encode() in argv:
                victims.append(pid)
    except OSError:
        pass
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _remove_display_files(display_n: int) -> None:
    for path in (os.path.join(_X_LOCK_DIR, f".X{display_n}-lock"),
                 os.path.join(_X_LOCK_DIR, ".X11-unix", f"X{display_n}")):
        try:
            os.remove(path)
        except OSError:
            pass


def _owned_temp_child(root: str, candidate: str,
                      prefixes: tuple[str, ...]) -> Optional[str]:
    """Resolve one app-owned temp child, never the shared temp root itself."""
    try:
        resolved_root = os.path.realpath(root)
        resolved = os.path.realpath(candidate)
        if resolved == resolved_root:
            return None
        if os.path.dirname(resolved) != resolved_root:
            return None
        if not os.path.basename(resolved).startswith(prefixes):
            return None
        return resolved
    except (OSError, TypeError, ValueError):
        return None


def _reap_stale_temp_dirs(max_age: float = _STALE_TEMP_AGE) -> int:
    """Delete profile/log dirs left behind by sessions that never shut down.

    `stop()` removes these, but it only runs on a graceful exit — a SIGKILL, an
    OOM kill or a dropped SSH session leaves the whole Chrome profile on disk.
    They accumulate at a few hundred MB each and nothing else ever collects
    them, so a session sweeps for orphans before creating its own.

    Age is the liveness test: a running Chrome writes into its profile
    constantly, so anything untouched for a day owns no process. Best-effort by
    design — this must never be the reason a browser fails to start.
    """
    removed = 0
    try:
        root = tempfile.gettempdir()
        now = time.time()
        for name in os.listdir(root):
            path = _owned_temp_child(
                root, os.path.join(root, name), _STALE_TEMP_PREFIXES)
            if path is None:
                continue
            try:
                if not os.path.isdir(path) or now - os.path.getmtime(path) < max_age:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except OSError:
                continue
    except Exception:
        pass
    return removed


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.1)
    return False


def _wait_for_rfb(host: str, port: int, timeout: float = 15.0) -> bool:
    """Wait until x11vnc will actually greet a viewer, not just accept TCP.

    Its listening socket opens before it can serve the RFB handshake, and a
    viewer that lands in that window gets a socket that never sends the banner
    — the session reports itself connected and stays blank forever. Probing for
    the banner is the only check that distinguishes the two states.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            with socket.create_connection((host, port), timeout=min(2.0, remaining)) as probe:
                # Hold this connection and wait out the rest of the budget: the
                # greeting can lag the accept, and hanging up to reconnect every
                # second only restarts that wait.
                probe.settimeout(max(0.5, deadline - time.time()))
                if probe.recv(4).startswith(b"RFB"):
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def _xserver_ready(display_n: int) -> bool:
    """Xvfb doesn't listen on TCP (we pass -nolisten tcp); the X server is up
    once its lock file + unix socket exist."""
    lock = f"/tmp/.X{display_n}-lock"
    sock = f"/tmp/.X11-unix/X{display_n}"
    return os.path.exists(lock) and os.path.exists(sock)


def _wait_for_xserver(display_n: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _xserver_ready(display_n):
            return True
        time.sleep(0.1)
    return False


def _xserver_port(display_n: int) -> int:
    """Legacy: X over TCP used 6000+N. Kept only for API symmetry; Xvfb here
    is -nolisten tcp so prefer _wait_for_xserver."""
    return 6000 + display_n


# ── upstream proxy with credentials ─────────────────────────────────────

class ProxyAuthRelay:
    """Loopback proxy that adds Proxy-Authorization on the way upstream.

    Chrome has no command-line flag for proxy credentials, and argv would be
    the wrong place for them anyway — every local user can read it out of
    ``ps``. Chrome is pointed at this relay over loopback instead, so the
    credentials only ever exist in this process's memory.

    Handles both forms a browser sends to a proxy: ``CONNECT host:port`` for
    HTTPS (the tunnel is opaque once established) and an absolute-URI request
    line for plain HTTP.
    """

    _HEAD_LIMIT = 64 * 1024      # a request head larger than this is not a browser
    _CONNECT_TIMEOUT = 20.0

    def __init__(self, upstream: str, credentials: Optional[str] = None):
        host, _, port = upstream.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"upstream proxy must be host:port (got {upstream!r})")
        self.upstream_host = host
        self.upstream_port = int(port)
        self._auth = b""
        if credentials:
            token = base64.b64encode(credentials.encode()).decode()
            self._auth = f"Proxy-Authorization: Basic {token}\r\n".encode()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self.port = 0

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Loopback only. This host is on the public internet; an open proxy on
        # it would be found by scanners and abused within hours.
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, args=(sock,),
                                        daemon=True, name="proxy-auth-relay")
        self._thread.start()
        return self.port

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._sock = None
        # The listening socket is closed by the accept loop itself: closing an
        # fd out from under a blocked accept() leaves the port bound until that
        # call returns, so the owning thread has to be the one to do it. Wake it
        # with a throwaway connection and wait for it to finish.
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ── plumbing ────────────────────────────────────────────────────────
    def _accept_loop(self, sock: socket.socket) -> None:
        try:
            while not self._closed.is_set():
                try:
                    client, _ = sock.accept()
                except OSError:
                    return
                if self._closed.is_set():
                    try:
                        client.close()      # the wake-up connection from close()
                    except OSError:
                        pass
                    return
                threading.Thread(target=self._serve, args=(client,), daemon=True).start()
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _read_head(sock: socket.socket, limit: int) -> bytes:
        """Read up to and including the blank line ending the request head."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > limit:
                raise ValueError("request head too large")
            chunk = sock.recv(8192)
            if not chunk:
                raise ConnectionError("client closed before sending a full request")
            buf += chunk
        return buf

    def _serve(self, client: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        try:
            client.settimeout(self._CONNECT_TIMEOUT)
            head = self._read_head(client, self._HEAD_LIMIT)
            line, _, rest = head.partition(b"\r\n")

            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=self._CONNECT_TIMEOUT)
            # Drop any Proxy-Authorization the client supplied; ours is the one
            # that counts, and duplicating the header confuses some proxies.
            headers = b"".join(
                h + b"\r\n" for h in rest.split(b"\r\n")
                if h and not h.lower().startswith(b"proxy-authorization:")
            )
            upstream.sendall(line + b"\r\n" + self._auth + headers + b"\r\n")

            if line.upper().startswith(b"CONNECT "):
                # Relay the upstream's answer verbatim: a 407 or a refusal is
                # something the browser should see, not something to mask.
                answer = self._read_head(upstream, self._HEAD_LIMIT)
                client.sendall(answer)
                if b" 200" not in answer.split(b"\r\n", 1)[0]:
                    return

            # From here the connection is opaque in both directions.
            client.settimeout(None)
            upstream.settimeout(None)
            done = threading.Event()
            forward = threading.Thread(target=self._pipe, args=(client, upstream, done), daemon=True)
            forward.start()
            self._pipe(upstream, client, done)
            forward.join(timeout=5)
        except (OSError, ValueError, ConnectionError):
            pass                            # a dead tab is not an error worth raising
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket, done: threading.Event) -> None:
        try:
            while not done.is_set():
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            done.set()
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def egress_from_env() -> dict:
    """Browser egress settings for this host, taken from the environment.

    Deliberately not a tool parameter: pointing the browser at an arbitrary
    proxy is a capability the agent must not be able to grant itself on the
    say-so of a page it is reading. Only whoever starts the process can set it.

      LAINTAS_BROWSER_PROXY              host:port of an upstream HTTP proxy
      LAINTAS_BROWSER_PROXY_CREDENTIALS  user:pass for it, when it needs auth
      LAINTAS_BROWSER_USER_AGENT         User-Agent to pin for every page
    """
    out: dict = {}
    proxy = (os.environ.get("LAINTAS_BROWSER_PROXY") or "").strip()
    if not proxy:
        # Fall back to the one proxy setting shared with web.search/web.fetch.
        # Without this a page that escalates from an HTTP fetch to a browser
        # render goes direct, and fails for exactly the users who configured a
        # proxy because they cannot reach the site any other way.
        try:
            import web_search as _ws
            proxy = str(_ws.browser_egress_overrides().get("proxy") or "").strip()
        except Exception:
            proxy = ""
    if proxy:
        out["proxy"] = proxy
        creds = (os.environ.get("LAINTAS_BROWSER_PROXY_CREDENTIALS") or "").strip()
        if creds:
            out["proxy_credentials"] = creds
    user_agent = (os.environ.get("LAINTAS_BROWSER_USER_AGENT") or "").strip()
    if user_agent:
        out["user_agent"] = user_agent
    return out


# ── BrowserSession ──────────────────────────────────────────────────────

@dataclass
class BrowserSessionInfo:
    name: str
    url: str
    cdp_endpoint: str
    display: str
    rfb_port: int
    created_at: float
    alive: bool


class BrowserSession:
    """One headless Chrome with a screen that can be viewed and driven remotely.

    Lifecycle:
      start()  → spawn Xvfb, Chrome, x11vnc.
      close()  → kill subprocesses, drop the proxy relay, remove user-data-dir.

    Nothing here reaches out to the backend: the live view is attached
    separately by webrtc_channel.py, which connects to rfb_port on demand.
    """

    def __init__(self, backend_url: str, agent_id: str,
                 session_id: str, url: str = "about:blank",
                 width: int = 1280, height: int = 800,
                 proxy: Optional[str] = None,
                 proxy_credentials: Optional[str] = None,
                 user_agent: Optional[str] = None):
        self.backend_url = backend_url.rstrip("/")
        self.agent_id = agent_id
        self.session_id = session_id
        # Security-check the opening URL (SSRF / scheme guard). about:blank and
        # other non-navigational defaults pass through untouched.
        if url and url not in ("about:blank",) and not url.startswith("about:"):
            self.url = validate_browse_url(url)
        else:
            self.url = url
        self.width = width
        self.height = height
        # Egress via an upstream proxy, so the page sees that exit IP rather
        # than this host's. Credentials go to a loopback relay instead of
        # Chrome's argv — see ProxyAuthRelay.
        self.proxy = proxy
        self.proxy_credentials = proxy_credentials
        self.user_agent = user_agent
        self._relay: Optional[ProxyAuthRelay] = None

        self.display_n: int = 0
        self.cdp_port: int = 0
        self.rfb_port: int = 0
        self.user_data_dir: Optional[str] = None

        self._xvfb: Optional[subprocess.Popen] = None
        self._chrome: Optional[subprocess.Popen] = None
        self._x11vnc: Optional[subprocess.Popen] = None

        self._closed = threading.Event()
        self._log_dir: Optional[str] = None

        # Playwright CDP connection (lazily connected by get_page()).
        self._pw = None          # sync_playwright().start() instance
        self._pw_browser = None  # chromium.connect_over_cdp() browser
        self._pw_lock = threading.Lock()

        # ── runtime capture (for website testing) ───────────────────────────
        # console messages, uncaught JS errors, and failed/4xx-5xx network
        # requests — what turns "operate a browser" into "test a site". Listeners
        # are attached once per page in _instrument(); handlers just append.
        import collections as _collections
        self._console_log = _collections.deque(maxlen=1000)   # {type, text, location}
        self._page_errors = _collections.deque(maxlen=300)    # {message}
        self._network_errors = _collections.deque(maxlen=300) # {url, method, status?/failure?}
        self._instrumented = set()   # id(page) already wired up

        # ── full XHR/fetch capture (for site analysis) ──────────────────────
        # OFF by default — only site analysis turns it on, so ordinary browsing
        # and testing pay nothing. When enabled, _on_response records every
        # XHR/fetch request+response (headers redacted, body size-capped) so the
        # observed API surface can be reconstructed. Set via set_api_capture().
        self._api_capture_on = False
        self._api_log = _collections.deque(maxlen=500)        # {url, method, status, req_body?, res_body?, content_type}
        self._API_BODY_CAP = 20000   # bytes per body kept

    # ── public ─────────────────────────────────────────────────────────

    def inject_refs(self) -> list:
        """Inject data-laintas-ref attributes on visible interactive elements
        and return a list of descriptor dicts for each.

        Each descriptor has: ref, tag, text, role, href, placeholder, type,
        value, aria_label, x, y, w, h.

        Refs are stable within a single page state. Any state-changing action
        (navigate, click, type, etc.) may invalidate them — call snapshot or
        inject_refs again to get fresh refs.
        """
        page = self.get_page()
        return page.evaluate("""
            () => {
                const selectors = [
                    'a[href]', 'button', 'input', 'select', 'textarea',
                    '[role="button"]', '[role="link"]', '[role="checkbox"]',
                    '[role="tab"]', '[role="menuitem"]', '[role="option"]',
                    '[role="textbox"]', '[onclick]', '[contenteditable]',
                    'summary', 'label[for]',
                ];
                const seen = new Set();
                const elements = [];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (seen.has(el)) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        if (style.opacity === '0') continue;
                        seen.add(el);
                        elements.push(el);
                    }
                }
                elements.sort((a, b) => {
                    const ra = a.getBoundingClientRect();
                    const rb = b.getBoundingClientRect();
                    return (ra.top - rb.top) || (ra.left - rb.left);
                });
                const refs = [];
                elements.forEach((el, i) => {
                    const ref = i + 1;
                    el.setAttribute('data-laintas-ref', String(ref));
                    const rect = el.getBoundingClientRect();
                    refs.push({
                        ref: ref,
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 200),
                        role: el.getAttribute('role') || '',
                        href: el.getAttribute('href') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        type: el.getAttribute('type') || '',
                        value: el.value || '',
                        aria_label: el.getAttribute('aria-label') || '',
                        name: el.getAttribute('name') || '',
                        id: el.getAttribute('id') || '',
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                    });
                });
                return refs;
            }
        """)

    def _log(self, name: str) -> str:
        """Path to a per-component stderr log file (lazily created)."""
        if self._log_dir is None:
            self._log_dir = tempfile.mkdtemp(prefix=f"hwo-vnc-{self.session_id}-")
        return os.path.join(self._log_dir, f"{name}.log")

    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.cdp_port}"

    def is_alive(self) -> bool:
        if self._closed.is_set():
            return False
        return bool(self._chrome and self._chrome.poll() is None)


    def get_page(self):
        """Lazily connect to Chrome via Playwright CDP and return the active
        Page.  All browser.* tools call this to get a handle to the page.

        The connection is created on first call and reused.  Playwright's sync
        API requires all calls from the same thread — the agent loop is
        single-threaded so this is safe.  If Chrome was restarted, the
        connection is re-established automatically.
        """
        if self._closed.is_set():
            raise RuntimeError("browser session is closed")

        with self._pw_lock:
            if self._pw is None:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._pw_browser = self._pw.chromium.connect_over_cdp(
                    self.cdp_endpoint())

            # Get the first context's first page, or create one.
            if self._pw_browser is None:
                raise RuntimeError("Playwright CDP connection failed")
            contexts = self._pw_browser.contexts
            if contexts:
                ctx = contexts[0]
            else:
                ctx = self._pw_browser.new_context()
            if ctx.pages:
                page = ctx.pages[0]
            else:
                page = ctx.new_page()
            self._instrument(page)
            return page

    # ── runtime capture (console / errors / network) ────────────────────────
    def _instrument(self, page) -> None:
        """Attach console/pageerror/network listeners to a page exactly once.
        Cheap: handlers only append to capped deques."""
        try:
            if id(page) in self._instrumented:
                return
            self._instrumented.add(id(page))

            def _on_console(msg):
                try:
                    loc = msg.location or {}
                    self._console_log.append({
                        "type": msg.type,
                        "text": (msg.text or "")[:2000],
                        "location": f"{loc.get('url','')}:{loc.get('lineNumber','')}" if loc else "",
                    })
                except Exception:
                    pass

            def _on_pageerror(err):
                try:
                    self._page_errors.append({"message": str(err)[:2000]})
                except Exception:
                    pass

            def _on_requestfailed(req):
                try:
                    self._network_errors.append({
                        "url": req.url, "method": req.method,
                        "failure": (req.failure or "request failed"),
                    })
                except Exception:
                    pass

            def _on_response(resp):
                try:
                    if resp.status >= 400:
                        self._network_errors.append({
                            "url": resp.url, "method": resp.request.method, "status": resp.status,
                        })
                except Exception:
                    pass
                # Full XHR/fetch capture (site analysis only). Kept fully
                # separate from the error path above so a body-read failure
                # never affects error capture.
                if not self._api_capture_on:
                    return
                try:
                    req = resp.request
                    if req.resource_type not in ("xhr", "fetch"):
                        return
                    ctype = ""
                    try:
                        ctype = (resp.headers or {}).get("content-type", "")
                    except Exception:
                        ctype = ""
                    entry = {
                        "url": resp.url,
                        "method": req.method,
                        "status": resp.status,
                        "content_type": ctype,
                        "req_body": None,
                        "res_body": None,
                    }
                    try:
                        pd = req.post_data
                        if pd:
                            entry["req_body"] = pd[: self._API_BODY_CAP]
                    except Exception:
                        pass
                    # Only read text-ish bodies; skip binary. body-read can throw
                    # (cached/redirected/no-body) — swallow and keep the metadata.
                    if any(t in ctype.lower() for t in ("json", "text", "javascript", "xml", "urlencoded")):
                        try:
                            txt = resp.text()
                            if txt:
                                entry["res_body"] = txt[: self._API_BODY_CAP]
                        except Exception:
                            pass
                    self._api_log.append(entry)
                except Exception:
                    pass

            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)
            page.on("requestfailed", _on_requestfailed)
            page.on("response", _on_response)
        except Exception:
            pass

    def get_console(self, level: Optional[str] = None) -> list:
        items = list(self._console_log)
        if level and level != "all":
            items = [m for m in items if m.get("type") == level]
        return items

    def get_page_errors(self) -> list:
        return list(self._page_errors)

    def get_network_errors(self) -> list:
        return list(self._network_errors)

    def set_api_capture(self, on: bool) -> None:
        """Enable/disable full XHR/fetch body capture (site analysis)."""
        self._api_capture_on = bool(on)

    def get_api_log(self) -> list:
        return list(self._api_log)

    def clear_captures(self) -> None:
        self._console_log.clear()
        self._page_errors.clear()
        self._network_errors.clear()
        self._api_log.clear()

    def _close_playwright(self) -> None:
        """Disconnect Playwright from Chrome (called by close())."""
        with self._pw_lock:
            if self._pw_browser is not None:
                try:
                    self._pw_browser.close()
                except Exception:
                    pass
                self._pw_browser = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    def start(self) -> None:
        """Spawn the full host stack. Raises RuntimeError on missing deps."""
        err = _check_host_deps()
        if err:
            raise RuntimeError(err)

        # Collect orphans from earlier sessions before adding one of our own —
        # displays first, so _free_display sees the numbers they release.
        _reap_stale_temp_dirs()
        _reap_stale_displays()

        self.display_n = _free_display()
        self.cdp_port = _free_tcp_port(9222, 9322)
        self.rfb_port = _free_tcp_port(5900, 6000)
        self.user_data_dir = tempfile.mkdtemp(prefix=f"hwo-chrome-{self.display_n}-")

        # The relay exists only to keep credentials out of Chrome's argv, so an
        # upstream that needs no credentials is pointed at directly. Callers
        # running their own relay (one that must outlive this process) pass its
        # address with no credentials and get the same effect.
        #
        # Must be listening before Chrome starts — Chrome resolves its proxy at
        # launch and a refused connection surfaces as an unhelpful page error.
        if self.proxy and self.proxy_credentials:
            self._relay = ProxyAuthRelay(self.proxy, self.proxy_credentials)
            self._relay.start()

        self._start_xvfb()
        # Chrome needs the display up to attach; Xvfb is -nolisten tcp so we
        # poll the lock file + unix socket, not a TCP port.
        if not _wait_for_xserver(self.display_n, timeout=5):
            raise RuntimeError(f"Xvfb :{self.display_n} did not start "
                               f"(see {self._log('xvfb')})")
        self._start_chrome()
        if not _wait_for_port("127.0.0.1", self.cdp_port, timeout=15):
            # Sandboxed launch can die at boot on hosts that block unprivileged
            # user namespaces (Ubuntu 24's apparmor_restrict_unprivileged_userns=1
            # → FATAL in sandbox/linux/services/credentials.cc). Retry once
            # without the sandbox — still as the dropped user, Site Isolation on.
            try:
                if self._chrome is not None:
                    self._chrome.kill()
                    try:
                        self._chrome.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            except OSError:
                pass
            self._start_chrome(no_sandbox=True)
            if not _wait_for_port("127.0.0.1", self.cdp_port, timeout=15):
                raise RuntimeError(f"Chrome CDP port {self.cdp_port} did not open "
                                   f"(see {self._log('chrome')})")
        self._start_x11vnc()
        if not _wait_for_rfb("127.0.0.1", self.rfb_port, timeout=15):
            raise RuntimeError(f"x11vnc on RFB port {self.rfb_port} never sent its "
                               f"banner (see {self._log('x11vnc')})")

        # The screen is now served by x11vnc on 127.0.0.1:rfb_port; the live
        # view attaches to it peer-to-peer when a viewer asks (webrtc_channel.py's
        # VNC bridge). Nothing is pushed to the backend from here.

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()

        # Disconnect Playwright before killing Chrome so it doesn't hang.
        self._close_playwright()

        if self._relay is not None:
            self._relay.close()
            self._relay = None

        for proc in (self._x11vnc, self._chrome, self._xvfb):
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                except Exception:
                    pass

        # Clean the X lock file Xvfb may have left.
        lock = f"/tmp/.X{self.display_n}-lock"
        for p in (lock, f"/tmp/.X11-unix/X{self.display_n}"):
            try:
                os.remove(p)
            except OSError:
                pass

        if self.user_data_dir:
            try:
                shutil.rmtree(self.user_data_dir, ignore_errors=True)
            except Exception:
                pass

        if self._log_dir:
            try:
                shutil.rmtree(self._log_dir, ignore_errors=True)
            except Exception:
                pass

    # ── subprocess starters ────────────────────────────────────────────

    def _start_xvfb(self) -> None:
        screen = f"{self.width}x{self.height}x24"
        cmd = ["Xvfb", f":{self.display_n}", "-screen", "0", screen,
               "-nolisten", "tcp", "-ac"]
        self._xvfb = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL,
            stderr=open(self._log("xvfb"), "wb"),
            start_new_session=True,
        )

    def _start_chrome(self, no_sandbox: bool = False) -> None:
        chrome = find_chrome()
        if not chrome:
            raise RuntimeError("no Chrome/Chromium binary on PATH")
        args = [
            chrome,
            f"--remote-debugging-port={self.cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # Keep Site Isolation ON (do NOT disable IsolateOrigins/site-per-process)
            # — it is a key cross-site / Spectre defense for untrusted pages.
            "--disable-features=TranslateUI",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            f"--window-size={self.width},{self.height}",
            "--start-maximized",
            # Prefer pages to start at a real size even without a WM.
            f"--window-position=0,0",
        ]

        if self._relay is not None or self.proxy:
            # Point at the loopback relay when there is one: credentials in argv
            # are readable by every local user through ps. Without credentials
            # there is nothing to hide and the upstream is used directly.
            # The relay always speaks HTTP. A directly-used upstream keeps its
            # own scheme: prefixing "http://" onto a "socks5://host:port" value
            # yields a proxy URL Chrome cannot parse, and it silently falls back
            # to a direct connection — the failure mode is a page that loads
            # from the wrong exit IP rather than an error.
            if self._relay is not None:
                args.append(f"--proxy-server=http://127.0.0.1:{self._relay.port}")
            else:
                upstream = self.proxy
                if "://" not in upstream:
                    upstream = f"http://{upstream}"
                args.append(f"--proxy-server={upstream}")
            # Chrome otherwise bypasses the proxy for loopback, which would let
            # a page reach services bound to this host.
            args.append("--proxy-bypass-list=<-loopback>")
        if self.user_agent:
            args.append(f"--user-agent={self.user_agent}")

        # Privilege handling: a page we browse is untrusted, so we want Chrome's
        # setuid sandbox ON. The sandbox refuses to run as root, so when we are
        # root we drop to a non-root user (preexec setuid) and keep the sandbox.
        # Only if no such user exists do we fall back to --no-sandbox.
        unpriv = _unpriv_user()
        preexec = None
        run_env = dict(os.environ, DISPLAY=f":{self.display_n}")
        if os.geteuid() == 0:
            if unpriv is not None:
                uid, gid, uname, uhome = unpriv
                # The dropped user must own the profile dir and reach the X socket.
                try:
                    _chown_tree(self.user_data_dir, uid, gid)
                except OSError:
                    pass
                # System accounts like `nobody` have HOME=/nonexistent; Chrome's
                # crashpad handler then dies on startup ("--database is required")
                # and takes Chrome with it. Point HOME (and crash dumps) at the
                # session's profile dir, which the dropped user owns.
                if not uhome or not os.path.isdir(uhome):
                    uhome = self.user_data_dir
                crash_dir = os.path.join(self.user_data_dir, "crash-dumps")
                try:
                    os.makedirs(crash_dir, exist_ok=True)
                    os.chown(crash_dir, uid, gid)
                except OSError:
                    pass
                args.append(f"--crash-dumps-dir={crash_dir}")
                run_env["HOME"] = uhome
                run_env["USER"] = uname

                def _drop():
                    os.setgid(gid)
                    try:
                        os.setgroups([gid])
                    except OSError:
                        pass
                    os.setuid(uid)
                preexec = _drop
            else:
                # No unprivileged user available — sandbox cannot run as root.
                args.append("--no-sandbox")

        if no_sandbox and "--no-sandbox" not in args:
            args.append("--no-sandbox")

        args.append(self.url)

        self._chrome = subprocess.Popen(
            args, stdout=subprocess.DEVNULL,
            stderr=open(self._log("chrome"), "wb"),
            env=run_env, start_new_session=True, preexec_fn=preexec,
        )

    def _start_x11vnc(self) -> None:
        cmd = [
            "x11vnc",
            "-display", f":{self.display_n}",
            "-rfbport", str(self.rfb_port),
            "-nopw",
            "-forever",
            "-shared",
            "-localhost",
            "-cursor", "arrow",
            "-quiet",
        ]
        self._x11vnc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL,
            stderr=open(self._log("x11vnc"), "wb"),
            start_new_session=True,
        )

    def info(self, name: str) -> BrowserSessionInfo:
        return BrowserSessionInfo(
            name=name,
            url=self.url,
            cdp_endpoint=self.cdp_endpoint(),
            display=f":{self.display_n}",
            rfb_port=self.rfb_port,
            created_at=time.time(),
            alive=self.is_alive(),
        )


# ── Session Registry (mirrors terminal registry in agent_loop.py) ───────

_browser_sessions: dict[str, BrowserSession] = {}
_browser_counter = 0
_browser_lock = threading.RLock()


def register_browser_session(session: BrowserSession, name: str = None) -> str:
    """Register a browser session. Auto-names 'browser<N>' if none given.
    If the name exists, closes the old session and replaces it. Returns name."""
    global _browser_counter
    with _browser_lock:
        _browser_counter += 1
        if name is None:
            name = f"browser{_browser_counter}"
        if name in _browser_sessions:
            try:
                _browser_sessions[name].close()
            except Exception:
                pass
        _browser_sessions[name] = session
        return name


def unregister_browser_session(name: str) -> bool:
    with _browser_lock:
        sess = _browser_sessions.pop(name, None)
        if sess is None:
            return False
        try:
            sess.close()
        except Exception:
            pass
        return True


def get_browser_session(name: str) -> Optional[BrowserSession]:
    with _browser_lock:
        return _browser_sessions.get(name)


def get_all_browser_sessions() -> List[BrowserSession]:
    with _browser_lock:
        return list(_browser_sessions.values())


def get_latest_browser_session() -> Optional[BrowserSession]:
    """The most recently registered session, or None.

    For viewers that have no way to name one: Helpwo's live-view button is a
    single icon per agent, so "this agent's browser" is the only thing it can
    mean, and it should not depend on how the session happened to be named.
    """
    with _browser_lock:
        for session in reversed(list(_browser_sessions.values())):
            if session.is_alive():
                return session
        return None


def close_all_browser_sessions() -> None:
    """Cascading cleanup — wired into the same shutdown hooks as
    close_all_terminals / close_all_agents."""
    with _browser_lock:
        sessions = list(_browser_sessions.values())
        _browser_sessions.clear()
    for sess in sessions:
        try:
            sess.close()
        except Exception:
            pass


# ── Self-test (P1 verification) ─────────────────────────────────────────

def _self_test(url: str = "https://www.google.com") -> int:
    """Start a session on `url`, print status, block until Ctrl-C."""
    err = _check_host_deps()
    if err:
        print(f"[!] {err}")
        return 1
    print(f"[*] starting headless-browser stack for {url}")
    sess = BrowserSession(
        backend_url=os.environ.get("LAINTAS_BACKEND", "http://localhost:8000"),
        agent_id="selftest",
        session_id=f"selftest-{int(time.time())}",
        url=url,
    )
    try:
        sess.start()
    except Exception as e:
        print(f"[!] start failed: {e}")
        sess.close()
        return 1

    name = register_browser_session(sess, name="selftest")
    print(f"[+] session '{name}' up:")
    print(f"    url         : {sess.url}")
    print(f"    display     :{sess.display_n}")
    print(f"    cdp         : {sess.cdp_endpoint()}")
    print(f"    rfb (VNC)   : 127.0.0.1:{sess.rfb_port}")
    print(f"    chrome pid  : {sess._chrome.pid if sess._chrome else '-'}")
    print("[*] Ctrl-C to tear down")

    # Prove the CDP endpoint is real (json/version), independent of Playwright.
    try:
        import urllib.request
        with urllib.request.urlopen(f"{sess.cdp_endpoint()}/json/version", timeout=3) as r:
            ver = json.loads(r.read().decode("utf-8", "replace"))
        print(f"    cdp/version : {ver.get('Browser', '?')} (protocol {ver.get('Protocol-Version', '?')})")
    except Exception as e:
        print(f"    cdp/version : probe failed: {e}")

    try:
        while not sess._closed.wait(timeout=1):
            if not sess.is_alive():
                print("[!] chrome exited")
                break
    except KeyboardInterrupt:
        print("\n[*] tearing down")
    sess.close()
    unregister_browser_session(name)
    print("[+] done")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="headless-browser session self-test")
    ap.add_argument("url", nargs="?", default="https://www.google.com")
    sys.exit(_self_test(ap.parse_args().url))
