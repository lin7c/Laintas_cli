"""Headless-browser session for laintas_cli — live-view stack.

Spawns Xvfb + Chrome (headed-in-virtual-display, --remote-debugging-port) +
x11vnc, and bridges the local RFB socket to the Helpwo backend's /vnc
WebSocket relay so the browser UI can render the page via noVNC. The AI side
drives Chrome over CDP (http://127.0.0.1:<debug-port>) — see the browser.*
tools in tools.py.

Topology mirrors TerminalSession (laintas_cli.py): the host runs a WebSocket
*client* to the backend, and the backend relays bytes to the browser's noVNC
client. Frames are JSON text with base64 payloads — same wire format as the
PTY relay — so the backend can reuse one relay implementation:
  host → browser : {"t":"o","d":<b64>}   RFB bytes from x11vnc
  browser → host : {"t":"i","d":<b64>}   RFB bytes from noVNC
  host → browser : {"t":"exit"}          x11vnc ended

Unix-only: requires Xvfb for the live view.

Optional system packages (probed at start; missing → clear error, no crash):
  Xvfb, x11vnc, google-chrome | chromium | chromium-browser
The websockify package is NOT needed on the host — this module is itself the
WS↔RFB bridge. (Backend may use websockify on its side; that's its concern.)
"""

from __future__ import annotations

import os
import sys
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
        lock = f"/tmp/.X{n}-lock"
        if not os.path.exists(lock):
            # Also confirm no process is listening on the abstract socket.
            return n
    raise RuntimeError(f"no free X display in :{start}..:{end}")


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
    """One headless Chrome + live-view relay, bridged to Helpwo over WS.

    Lifecycle:
      start()  → spawn Xvfb, Chrome, x11vnc; connect WS to backend (retried);
                 start the two bridge threads.
      close()  → kill subprocesses, close WS, remove user-data-dir.

    The WS connection is retried in the background so the host stack comes up
    immediately even if the backend /vnc endpoint isn't deployed yet — useful
    for debugging and for surviving transient backend outages.
    """

    def __init__(self, backend_url: str, agent_id: str, agent_secret: str,
                 session_id: str, url: str = "about:blank",
                 width: int = 1280, height: int = 800):
        self.backend_url = backend_url.rstrip("/")
        self.agent_id = agent_id
        self.agent_secret = agent_secret
        self.session_id = session_id
        # Security-check the opening URL (SSRF / scheme guard). about:blank and
        # other non-navigational defaults pass through untouched.
        if url and url not in ("about:blank",) and not url.startswith("about:"):
            self.url = validate_browse_url(url)
        else:
            self.url = url
        self.width = width
        self.height = height

        self.display_n: int = 0
        self.cdp_port: int = 0
        self.rfb_port: int = 0
        self.user_data_dir: Optional[str] = None

        self._xvfb: Optional[subprocess.Popen] = None
        self._chrome: Optional[subprocess.Popen] = None
        self._x11vnc: Optional[subprocess.Popen] = None

        self._rfb_sock: Optional[socket.socket] = None
        self._ws = None
        self._ws_thread: Optional[threading.Thread] = None
        self._rfb_reader: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._ws_connected = threading.Event()
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

    def ws_connected(self) -> bool:
        return self._ws_connected.is_set()

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

        self.display_n = _free_display()
        self.cdp_port = _free_tcp_port(9222, 9322)
        self.rfb_port = _free_tcp_port(5900, 6000)
        self.user_data_dir = tempfile.mkdtemp(prefix=f"hwo-chrome-{self.display_n}-")

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
            except OSError:
                pass
            self._start_chrome(no_sandbox=True)
            if not _wait_for_port("127.0.0.1", self.cdp_port, timeout=15):
                raise RuntimeError(f"Chrome CDP port {self.cdp_port} did not open "
                                   f"(see {self._log('chrome')})")
        self._start_x11vnc()
        if not _wait_for_port("127.0.0.1", self.rfb_port, timeout=5):
            raise RuntimeError(f"x11vnc RFB port {self.rfb_port} did not open")

        # The screen is now served by x11vnc on 127.0.0.1:rfb_port. The browser
        # attaches to it peer-to-peer over WebRTC (webrtc_channel.py's VNC
        # bridge) — RFB bytes never touch the backend. No relay WS is started;
        # the legacy _ws_bridge_loop is retained only for optional fallback and
        # is intentionally not spawned here.

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()

        # Disconnect Playwright before killing Chrome so it doesn't hang.
        self._close_playwright()

        # Close the RFB socket first so the reader thread exits.
        if self._rfb_sock is not None:
            try:
                self._rfb_sock.close()
            except OSError:
                pass
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

        for proc in (self._x11vnc, self._chrome, self._xvfb):
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
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

    # ── WS ↔ RFB bridge ────────────────────────────────────────────────

    def _ws_url(self) -> str:
        base = self.backend_url
        base = base.replace("https://", "wss://").replace("http://", "ws://")
        return (f"{base}/api/agents/{self.agent_id}/vnc"
                f"?sessionId={self.session_id}&role=host"
                f"&agentSecret={self.agent_secret}")

    def _ws_bridge_loop(self) -> None:
        """Connect to backend /vnc, then shuttle RFB bytes both ways.

        Retries the WS connect every 3s until close() — so a missing or
        temporarily-down backend /vnc endpoint never takes down the host
        stack. Once connected, runs until either side closes.
        """
        try:
            from websockets.sync.client import connect as _ws_connect
        except ImportError:
            return  # websockets not installed → no relay; host stack still up

        while not self._closed.is_set():
            try:
                self._ws = _ws_connect(self._ws_url(), open_timeout=10,
                                       max_size=None)
            except Exception:
                # Backend may not have deployed /vnc yet. Retry.
                if self._closed.wait(timeout=3):
                    return
                continue

            self._ws_connected.set()
            # Connect to x11vnc's RFB port on the host side.
            try:
                self._rfb_sock = socket.create_connection(
                    ("127.0.0.1", self.rfb_port), timeout=5)
            except OSError:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
                self._ws_connected.clear()
                if self._closed.wait(timeout=3):
                    return
                continue

            # Reader: RFB → WS (sole WS writer per the one-writer pattern).
            self._rfb_reader = threading.Thread(
                target=self._pump_rfb_to_ws, daemon=True,
                name=f"hwo-vnc-rfb-{self.session_id}")
            self._rfb_reader.start()

            # Main: WS → RFB (sole RFB writer).
            try:
                self._pump_ws_to_rfb()
            finally:
                self._ws_connected.clear()
                if self._rfb_sock is not None:
                    try:
                        self._rfb_sock.close()
                    except OSError:
                        pass
                self._rfb_sock = None
                if self._ws is not None:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                self._ws = None

            if self._closed.wait(timeout=3):
                return

    def _pump_rfb_to_ws(self) -> None:
        """RFB socket → WS. Sole writer of the WS so frames stay ordered."""
        while not self._closed.is_set() and self._ws is not None:
            try:
                data = self._rfb_sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            try:
                self._ws.send(json.dumps({
                    "t": "o",
                    "d": base64.b64encode(data).decode("ascii"),
                }))
            except Exception:
                break
        # Tell the browser the VNC server ended.
        try:
            if self._ws is not None:
                self._ws.send(json.dumps({"t": "exit"}))
        except Exception:
            pass

    def _pump_ws_to_rfb(self) -> None:
        """WS → RFB socket."""
        import base64 as _b64
        if self._ws is None:
            return
        try:
            for message in self._ws:
                try:
                    msg = json.loads(message)
                except (ValueError, TypeError):
                    continue
                t = msg.get("t")
                if t == "i":
                    try:
                        self._rfb_sock.sendall(
                            _b64.b64decode(msg.get("d", "")))
                    except OSError:
                        break
                # no resize/exit handling — VNC has no resize semantic.
        except Exception:
            pass

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
        agent_id="selftest", agent_secret="selftest",
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
    print(f"    ws relay    : {sess.ws_connected()} (retries until backend /vnc is up)")
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
