"""windows_host.py — the Windows machine this CLI is running inside of.

On the Windows build, laintas_cli is a Linux program in a private WSL
distribution. Everything it can reach directly is Linux: bash, a Linux
Chromium, and the user's real disk seen through a slow 9P mount. The machine
the user is actually sitting at is on the other side of that boundary, and
nothing in this process can touch it.

`helpwo-kernel.exe` can. It already exists, it already runs on the Windows
side for Helpwo, and it already has the accessibility tree, screen capture and
input synthesis behind a guard. So this module does not reimplement any of
that — it becomes a second client of the same kernel, speaking the same frames
the browser speaks.

Who listens
-----------
The kernel's promise is that it opens no port, so it dials out and *we*
listen. This module binds a loopback socket inside the distribution and
publishes the port and a fresh token in a rendezvous file on the Windows side;
the kernel picks it up within a couple of seconds and connects. Windows
forwards `localhost` into WSL by default, which is what makes this direction
the one that works without depending on the user's networking mode.

Availability, not optimism
--------------------------
Tools appear only once a kernel is actually connected and has said what it can
do. A `win.*` tool that exists but always fails costs the model turns to
discover and often provokes a workaround nobody wanted — so `windows_tools`
registers on connect and unregisters on disconnect, and on a machine with no
kernel running the tool surface is exactly what it was before.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import winbridge

FRAME_JSON = 0
FRAME_BINARY = 1
MAX_FRAME = 32 * 1024 * 1024

#: How long a `win` call may take before we stop waiting. A tree walk against
#: a busy application and a full-desktop capture are both seconds; a hung
#: provider is bounded on the kernel side, and this is the backstop.
CALL_TIMEOUT = 45.0


def rendezvous_dir() -> Optional[Path]:
    """Where each CLI session publishes its own endpoint for the kernel.

    One file per process, so the kernel serves every session on the machine
    (it watches the directory; see kernel/localbridge.py). On the Windows
    build the kernel is a Windows program and looks under LOCALAPPDATA; on
    Linux both run as the same user and share the laintas home.
    """
    override = os.environ.get("LAINTAS_KERNEL_RENDEZVOUS_DIR")
    if override:
        return Path(override)
    if winbridge.in_wsl():
        base = winbridge.localappdata()
        return base / "Laintas" / "kernel-rendezvous" if base else None
    import paths
    return Path(paths.LAINTAS_HOME) / "kernel-rendezvous"


def rendezvous_path() -> Optional[Path]:
    """The single-file endpoint kernels older than the relay look for.

    Under LOCALAPPDATA rather than the workspace: the workspace is a folder
    the user chooses and may delete, and a rendezvous that vanishes with it
    would look like the CLI had gone away.
    """
    override = os.environ.get("LAINTAS_KERNEL_RENDEZVOUS")
    if override:
        return Path(override)
    base = winbridge.localappdata()
    if base is None:
        return None
    return base / "Laintas" / "kernel-rendezvous.json"


class KernelUnavailable(RuntimeError):
    """No kernel is connected, or it went away mid-call."""


class WindowsHost:
    """Listens for the kernel, then speaks frames to it.

    One connection. The kernel serves one local client at a time, and a
    second CLI would need an answer to how two peers share one terminal —
    a question worth answering when somebody has it, not before.
    """

    def __init__(self, path: Optional[Path] = None, *,
                 directory: Optional[Path] = None,
                 publish_legacy: bool = True) -> None:
        # An explicit path means exactly that file (the tests, and the old
        # override). Otherwise: this session's own file in the directory, and
        # — for the primary terminal only — the single file older kernels
        # read, so their Windows tools keep working until they are updated.
        if path is not None:
            self._path = path
            self._dir = directory
        else:
            self._path = rendezvous_path() if publish_legacy else None
            self._dir = directory if directory is not None else rendezvous_dir()
        self._dir_file: Optional[Path] = None
        self._token = secrets.token_hex(24)
        self._listener: Optional[socket.socket] = None
        self._conn: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list]] = {}
        self._pending_lock = threading.Lock()
        self._counter = 0
        #: Serialises the connect and disconnect callbacks against each
        #: other. Without it the greeting thread can decide the connection
        #: is live, be descheduled, and announce it *after* the disconnect
        #: has already been handled — leaving tools registered for a kernel
        #: that is gone. It only reproduced under a loaded test suite.
        self._callback_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.info: dict = {}
        self.probe: dict = {}
        #: Windows tools are ready (the machine tiers were probed).
        self.on_connect: Optional[Callable[["WindowsHost"], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        #: Any kernel connected / went away — the relay's concern, and every
        #: platform's, whether or not the kernel controls Windows.
        self._link_listeners: list[Callable[[bool], None]] = []
        #: Frames the kernel sends unprompted, by type: relayed inputs from
        #: Helpwo, requests for this session's screen.
        self._handlers: dict[str, Callable[[dict], None]] = {}

    # -- lifecycle -------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def features(self) -> frozenset:
        """What the connected kernel says it supports; empty for old ones."""
        raw = self.info.get("features") if isinstance(self.info, dict) else None
        return frozenset(str(f) for f in raw) if isinstance(raw, list) else frozenset()

    def supports(self, feature: str) -> bool:
        return self.connected and feature in self.features()

    def add_link_listener(self, callback: Callable[[bool], None]) -> None:
        """Call `callback(True)` on every kernel connection, `(False)` on loss.

        Called at once with True if a kernel is already connected, so a
        listener added late does not miss the connection it cares about.
        """
        with self._callback_lock:
            self._link_listeners.append(callback)
            already = self._conn is not None and bool(self.info)
        if already:
            try:
                callback(True)
            except Exception:
                pass

    def set_handler(self, kind: str, handler: Callable[[dict], None]) -> None:
        self._handlers[kind] = handler

    def start(self) -> bool:
        """Publish a rendezvous and wait for the kernel. Never raises."""
        if self._path is None and self._dir is None:
            return False
        try:
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
        except OSError:
            return False
        self._listener = listener
        port = listener.getsockname()[1]
        record = {
            "version": 2, "host": "127.0.0.1", "port": port,
            "token": self._token, "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created": int(time.time()),
        }
        published = False
        if self._dir is not None:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self._dir, 0o700)
                except OSError:
                    pass
                _collect_stale(self._dir)
                self._dir_file = self._dir / (
                    f"{socket.gethostname()}-{os.getpid()}-{self._token[:8]}.json")
                _publish(self._dir_file, record)
                published = True
            except OSError:
                self._dir_file = None
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                _publish(self._path, record)
                published = True
            except OSError:
                pass
        if not published:
            listener.close()
            self._listener = None
            return False
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="windows-host", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._conn, self._listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for mine in (self._path, self._dir_file):
            try:
                # Only a file that still names us: a newer session may have
                # taken the single file over, and removing it would cut that
                # session off from the kernel.
                if mine is not None and mine.exists():
                    data = json.loads(mine.read_text(encoding="utf-8"))
                    if data.get("token") == self._token:
                        mine.unlink()
            except (OSError, ValueError):
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._listener is not None:
            try:
                self._listener.settimeout(1.0)
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        """Validate the caller, then read frames until it goes away.

        The probe and the connect callback run on their own thread: they send
        a frame and wait for the answer, and the answer can only arrive
        through the loop below. Doing them inline is a deadlock that presents
        as "the kernel is slow" — it cost this file one debugging round.
        """
        try:
            hello = self._read_frame(conn)
            if not isinstance(hello, dict) or hello.get("t") != "hello":
                return
            if hello.get("token") != self._token:
                # Somebody dialled us who had not read our file. Say nothing
                # and drop it — a wrong-token reply is a probing oracle.
                return
            self.info = hello.get("kernel") or {}
            self._conn = conn
            threading.Thread(target=self._greet, args=(conn,),
                             name="windows-host-greet", daemon=True).start()
            while not self._stop.is_set():
                message = self._read_frame(conn)
                if message is None:
                    break
                if isinstance(message, dict):
                    handler = self._handlers.get(str(message.get("t") or ""))
                    if handler is not None:
                        # Off the reader thread: a handler may itself call
                        # the kernel, and the answer arrives through here.
                        threading.Thread(target=self._run_handler,
                                         args=(handler, message),
                                         name="kernel-frame", daemon=True).start()
                    else:
                        self._resolve(message)
        finally:
            # Taken around the whole teardown so a greeting still in flight
            # either announces before this runs, or sees `_conn` cleared and
            # says nothing. Either order leaves the tool surface correct.
            with self._callback_lock:
                was_connected = self._conn is conn
                if was_connected:
                    # Only clear what was ours. A caller rejected at the
                    # token check never became the connection, and must not
                    # tear down one that did.
                    self._conn = None
                    self.probe = {}
                try:
                    conn.close()
                except OSError:
                    pass
                self._fail_pending("the Windows kernel disconnected")
                if was_connected and self.on_disconnect:
                    try:
                        self.on_disconnect()
                    except Exception:
                        pass
                if was_connected:
                    self._notify_link(False)

    @staticmethod
    def _run_handler(handler: Callable[[dict], None], message: dict) -> None:
        try:
            handler(message)
        except Exception:
            pass

    def _notify_link(self, up: bool) -> None:
        for callback in list(self._link_listeners):
            try:
                callback(up)
            except Exception:
                pass

    def _greet(self, conn: socket.socket) -> None:
        """Announce the link, then probe for Windows tools — only while ours.

        The probe can fail because the kernel just went away, and the
        disconnect path has already run by the time we find out. Announcing
        anyway re-registers tools for a kernel that is gone, which is how a
        dead connection ends up with a live tool surface.
        """
        with self._callback_lock:
            if self._conn is not conn:
                return
        self._notify_link(True)
        info = self.info if isinstance(self.info, dict) else {}
        if "features" in info and not (info.get("machineRead")
                                       or info.get("machineWrite")):
            # A kernel that says what it supports and grants no machine tier
            # has no Windows tools to offer (every Linux kernel, and a
            # Windows one started without the switches). Probing would only
            # be refused.
            return
        try:
            probe = self.call("probe", timeout=15)
        except Exception:
            return
        with self._callback_lock:
            if self._conn is not conn:
                return
            self.probe = probe
            if self.on_connect:
                try:
                    self.on_connect(self)
                except Exception:
                    pass

    # -- framing ---------------------------------------------------------

    def _read_frame(self, conn: socket.socket) -> Optional[Any]:
        header = self._exact(conn, 5)
        if header is None:
            return None
        kind, length = struct.unpack(">BI", header)
        if length > MAX_FRAME:
            return None
        body = self._exact(conn, length) if length else b""
        if body is None:
            return None
        if kind == FRAME_JSON:
            try:
                return json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
        return body

    @staticmethod
    def _exact(conn: socket.socket, count: int) -> Optional[bytes]:
        chunks = []
        while count:
            try:
                chunk = conn.recv(min(count, 65536))
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def send(self, payload: dict) -> None:
        """Send one frame, expecting no answer. Raises if no kernel."""
        self._write(payload)

    def request(self, payload: dict, timeout: float = CALL_TIMEOUT) -> dict:
        """Send a frame and wait for the frame that carries the same id."""
        with self._pending_lock:
            self._counter += 1
            req_id = f"cli-{self._counter}"
            event = threading.Event()
            slot: list = []
            self._pending[req_id] = (event, slot)
        try:
            self._write({**payload, "id": req_id})
        except KernelUnavailable:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise
        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise KernelUnavailable(
                f"the Helpwo kernel did not answer within {int(timeout)}s")
        return slot[0] if slot else {}

    def _write(self, payload: dict) -> None:
        conn = self._conn
        if conn is None:
            raise KernelUnavailable("no Helpwo kernel is connected")
        raw = json.dumps(payload).encode("utf-8")
        with self._send_lock:
            try:
                conn.sendall(struct.pack(">BI", FRAME_JSON, len(raw)) + raw)
            except OSError as exc:
                raise KernelUnavailable(
                    f"the Windows kernel went away: {exc}") from exc

    # -- request / response ----------------------------------------------

    def _resolve(self, message: dict) -> None:
        req_id = str(message.get("id") or "")
        with self._pending_lock:
            waiter = self._pending.pop(req_id, None)
        if waiter is None:
            return
        event, slot = waiter
        slot.append(message)
        event.set()

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for event, slot in waiters:
            slot.append({"ok": False, "error": reason})
            event.set()

    def call(self, op: str, args: Optional[dict] = None,
             timeout: float = CALL_TIMEOUT) -> dict:
        """One `win` op. Raises on refusal so tools report a real error."""
        with self._pending_lock:
            self._counter += 1
            req_id = f"cli-{self._counter}"
            event = threading.Event()
            slot: list = []
            self._pending[req_id] = (event, slot)
        try:
            self._write({"t": "win", "id": req_id, "op": op,
                         "args": args or {}})
        except KernelUnavailable:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise
        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise KernelUnavailable(
                f"the Windows kernel did not answer {op!r} within "
                f"{int(timeout)}s")
        reply = slot[0] if slot else {}
        if not reply.get("ok"):
            raise KernelUnavailable(
                str(reply.get("error") or "the Windows kernel refused"))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    # -- capability description ------------------------------------------

    def tiers(self) -> dict:
        return dict(self.probe.get("tiers") or {})

    def can(self, op: str) -> bool:
        """Whether this kernel would accept an op right now."""
        tiers = self.tiers()
        if op == "probe":
            return True
        from windows_tools import READ_OPS, WRITE_OPS
        if op in WRITE_OPS:
            return bool(tiers.get("machineWrite"))
        if op in READ_OPS:
            return bool(tiers.get("machineRead"))
        return False


def _publish(target: Path, record: dict) -> None:
    """Write whole, then move into place, owner-only.

    The kernel polls this path, and a half-written file is a connection
    attempt against a port that does not exist yet. The token in it is what
    lets the kernel in, so nobody else on the machine gets to read it.
    """
    staging = target.with_suffix(".tmp")
    staging.write_text(json.dumps(record), encoding="utf-8")
    try:
        os.chmod(staging, 0o600)
    except OSError:
        pass
    staging.replace(target)


def _collect_stale(directory: Path) -> None:
    """Remove files left by CLI sessions on this machine that no longer run.

    Only this host's, only by pid: another distribution or machine sharing the
    folder is not ours to judge. A file the kernel would dial and find nobody
    behind costs it a retry every half minute, forever.
    """
    me = socket.gethostname()
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return
    for entry in entries[:256]:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            if data.get("hostname") != me:
                continue
            pid = int(data.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            continue
        if pid <= 0 or pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                entry.unlink()
            except OSError:
                pass
        except OSError:
            pass   # alive, but not ours to signal


#: Process-wide host. Started by the REPL bootstrap on every platform: the
#: Helpwo kernel is how this session reaches Helpwo, not only Windows.
_host: Optional[WindowsHost] = None
_host_lock = threading.Lock()


def get_host() -> Optional[WindowsHost]:
    return _host


def start_host(primary: bool = True) -> Optional[WindowsHost]:
    """Begin waiting for a Helpwo kernel. Safe to call on any platform.

    `primary` is the top-level terminal: only it publishes the single file
    older kernels read, so two sessions never fight over it.
    """
    global _host
    with _host_lock:
        if _host is not None:
            return _host
        host = WindowsHost(publish_legacy=primary and winbridge.in_wsl())

        def connected(h: WindowsHost) -> None:
            import windows_tools
            windows_tools.register(h)

        def gone() -> None:
            import windows_tools
            windows_tools.unregister()

        host.on_connect = connected
        host.on_disconnect = gone
        if not host.start():
            return None
        _host = host
        import atexit
        atexit.register(host.stop)
        return host


def stop_host() -> None:
    global _host
    with _host_lock:
        if _host is not None:
            _host.stop()
            _host = None
