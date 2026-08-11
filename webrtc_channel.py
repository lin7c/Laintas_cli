"""WebRTC peer-to-peer file channel for laintas_cli — Layer 1 (transport).

Establishes a DataChannel directly with the Helpwo browser so file transfers can
bypass the relay server (only the tiny SDP handshake rides the relay). This file
owns the aiortc peer(s) and a background asyncio loop; the signaling (offer in,
answer out) is handed in/out by laintas_cli.py over the existing agent protocol.

Security model: the offer only reaches us via /api/agents/<id>/poll, which the
Helpwo backend authorizes to the agent's owning user — so an offer arriving here
is already from the authorized user. DTLS (mandatory in WebRTC) encrypts the
channel end to end; the relay/operator never sees file bytes.

Layer 1 just opens the channel and answers a ping with a pong to prove it works.
Layer 2 will add the file read/write RPC in `_on_message`.

Non-trickle ICE: aiortc finishes candidate gathering inside setLocalDescription,
so the answer SDP already carries the candidates — no separate ICE messages.
"""

import asyncio
import base64
import fnmatch
import json
import os
import pty
import shutil
import signal
import socket
import struct
import termios
import threading
import fcntl
from typing import Any, Callable, Optional

# File RPC limits (Layer 2). Bytes flow as raw binary DataChannel messages.
_CHUNK = 16 * 1024            # per binary message (safe under SCTP message size)
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_HTTP_BYTES = 20 * 1024 * 1024   # per tunneled HTTP response (dev bundles can be big)
_AI_EXEC_MAX_OUTPUT = 256 * 1024    # matches the old server-relayed exec's cap

# Live reference to the AgentRegistry instance that owns this WebrtcManager,
# so path checks can also allow the folder the user explicitly shared via
# /connect <folder> or /helpwo (agent_registry.workspace_path) — not just
# policy.py's allowedRoots. allowedRoots gates autonomous AI shell commands;
# workspace_path is a separate, deliberate per-connection consent the user
# already gave when they chose what to share. Conflating the two meant a
# mounted folder outside the (unrelated) default allowedRoots list — the
# common case, since allowedRoots defaults to a handful of fixed dirs that
# don't include an arbitrary shared cwd — silently listed as empty.
_registry_ref: Optional[Any] = None


def set_agent_registry(registry: Any) -> None:
    """Called once from laintas_cli.py's _ensure_webrtc() so path checks stay
    in sync with the live registry (workspace_path can change via a later
    /connect <folder> without needing to reconstruct the WebrtcManager)."""
    global _registry_ref
    _registry_ref = registry


def _extra_allowed_roots() -> list:
    workspace_path = getattr(_registry_ref, "workspace_path", None) if _registry_ref else None
    return [workspace_path] if workspace_path else []

# Whitelist of commands allowed via the exec RPC. The browser RemoteProvider
# uses these for directory listing + metadata; arbitrary shell is NOT allowed
# over the P2P channel (use the agent loop's command execution instead, which
# goes through policy.py approval).
_EXEC_WHITELIST = frozenset({
    "ls", "dir", "find", "mkdir", "rmdir", "mv", "cp", "rm", "stat",
    "cat", "head", "tail", "wc", "du", "df", "file", "pwd", "echo",
    "touch", "chmod", "chown", "ln", "readlink", "realpath", "basename",
    "dirname", "tree", "exa", "bat",
})

try:
    from aiortc import (
        RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer,
    )
    AIORTC_AVAILABLE = True
    _IMPORT_ERROR = ""
except Exception as e:  # aiortc optional — CLI runs without it
    AIORTC_AVAILABLE = False
    _IMPORT_ERROR = str(e)

# Self-hosted STUN for NAT traversal. TURN URLs are intentionally rejected:
# remote workspace payloads must remain peer-to-peer.
_DEFAULT_ICE = ["stun:192.227.215.252:3478"]


def normalize_stun_urls(value) -> list[str]:
    """Flatten an RTC config, URL list, or comma-separated env value.

    Only STUN endpoints are accepted. In particular, a compromised or
    misconfigured Gateway cannot silently turn this P2P channel into TURN
    relay traffic.
    """
    urls: list[str] = []

    def collect(item):
        if isinstance(item, dict):
            if "iceServers" in item:
                collect(item.get("iceServers"))
            elif "urls" in item:
                collect(item.get("urls"))
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                collect(child)
            return
        if isinstance(item, str):
            candidates = item.split(",")
            for candidate in candidates:
                url = candidate.strip()
                if url.startswith("stun:") and len(url) <= 512 and url not in urls:
                    urls.append(url)

    collect(value)
    return urls


def configured_ice_servers(gateway_config=None) -> list[str]:
    env_value = os.environ.get("LAINTAS_ICE_SERVERS", "")
    return (
        normalize_stun_urls(env_value)
        or normalize_stun_urls(gateway_config)
        or list(_DEFAULT_ICE)
    )

# ICE candidate sockets bind inside this fixed UDP window instead of a random
# ephemeral port, so a host firewall can allowlist exactly this range. On the
# hosted CLI the input chain drops all other inbound UDP — random ports made
# every P2P session die at ICE with no error surfaced. Override with
# LAINTAS_RTC_PORTS="lo-hi".
def _rtc_port_range() -> tuple:
    raw = os.environ.get("LAINTAS_RTC_PORTS", "50700-50899")
    try:
        lo, hi = raw.split("-", 1)
        lo, hi = int(lo), int(hi)
        if 1024 <= lo < hi <= 65535:
            return lo, hi
    except (ValueError, AttributeError):
        pass
    return 50700, 50899


def _bind_ports_in_range(loop, lo: int, hi: int) -> None:
    """Wrap this loop's create_datagram_endpoint so port-0 binds land in
    [lo, hi]. Only this manager's private loop is patched."""
    orig = loop.create_datagram_endpoint

    async def patched(factory, local_addr=None, **kw):
        if local_addr and len(local_addr) == 2 and local_addr[1] == 0:
            last_err = None
            for port in range(lo, hi + 1):
                try:
                    return await orig(factory, local_addr=(local_addr[0], port), **kw)
                except OSError as e:
                    last_err = e
            raise last_err or OSError(f"no free UDP port in RTC range {lo}-{hi}")
        return await orig(factory, local_addr=local_addr, **kw)

    loop.create_datagram_endpoint = patched


# How long a detached terminal session (browser refresh, tab switch, brief
# network blip) keeps its shell alive waiting for the SAME sessionId to
# reattach, before it's killed for good. Mirrors the gateway relay's own
# detach-grace concept for the WS-based terminal, so moving a terminal to P2P
# doesn't regress "refresh doesn't interrupt your shell".
_TERM_DETACH_GRACE_SECONDS = 120
# Output produced while nobody is attached (mid-grace) is buffered for replay
# on reattach, capped so a chatty command (e.g. left running while detached)
# can't grow this unbounded.
_TERM_BUFFER_CAP = 65536


def _kill_process_group(proc) -> None:
    """Kill the whole process group `proc` leads, not just its own pid.

    A plain proc.kill() only signals the immediate child (e.g. the /bin/sh
    that create_subprocess_shell spawned). If that shell forked a real child
    for the actual command instead of exec-replacing itself, killing just the
    shell leaves the real command running and still holding the stdout pipe
    open — the reader never sees EOF and abort silently does nothing.
    Requires the process was started with start_new_session=True.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _is_path_allowed(path: str) -> bool:
    """Check if `path` is within any of policy.py's allowedRoots, OR within
    the folder the user explicitly shared for this connection
    (agent_registry.workspace_path).

    Prevents the WebRTC channel from reading/writing arbitrary files
    (e.g. ~/.ssh/id_rsa, /etc/shadow) even if the connected browser is
    compromised.
    """
    if not path:
        return False
    try:
        import policy
        cfg = policy._load_config()
        roots = list(cfg.get("allowedRoots", []))
    except Exception:
        roots = []
    roots.extend(_extra_allowed_roots())
    if not roots:
        # No allowedRoots configured - deny by default for safety.
        return False
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        try:
            root_resolved = os.path.realpath(root)
        except OSError:
            continue
        if resolved == root_resolved or resolved.startswith(root_resolved + os.sep):
            return True
    return False


def _validate_exec_cmd(cmd: str) -> str | None:
    """Return an error message if `cmd` is not allowed, None if it is.

    Only commands in _EXEC_WHITELIST are permitted, with simple arguments.
    No shell metacharacters (|, ;, &, >, <, `, $(), etc.) are allowed to
    prevent command chaining/injection.
    """
    if not cmd or not cmd.strip():
        return "empty command"
    # Reject shell metacharacters that enable chaining/injection.
    dangerous = set("|;&`$()<>\n\r")
    if any(c in cmd for c in dangerous):
        return "shell metacharacters not allowed"
    import shlex
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return "malformed command"
    if not parts:
        return "empty command"
    # Resolve the binary name (handle leading env assignments like FOO=bar cmd).
    bin_idx = 0
    while bin_idx < len(parts) and "=" in parts[bin_idx] and not os.path.isfile(parts[bin_idx]):
        bin_idx += 1
    if bin_idx >= len(parts):
        return "no command found"
    binary = os.path.basename(parts[bin_idx])
    if binary not in _EXEC_WHITELIST:
        return f"command '{binary}' not in whitelist"
    return None


def _validate_exec_paths(cmd: str) -> str | None:
    """Return an error message if any path argument in `cmd` is outside
    the policy's allowedRoots, None if all paths are allowed (or no paths).

    Extracts non-flag arguments and checks each one that looks like a path
    (contains '/' or starts with '.'/'~') against _is_path_allowed().
    """
    import shlex
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None  # _validate_exec_cmd already catches malformed
    for part in parts:
        if part.startswith("-"):
            continue  # flag
        if "/" in part or part.startswith((".", "~")):
            if not _is_path_allowed(part):
                return f"path '{part}' outside allowed roots"
    return None


def _rpc_path(value, *, follow_leaf=True) -> str:
    """Resolve and contain one structured-RPC path.

    Entry mutations preserve the final symlink so deleting or moving a link
    cannot delete its target. Reads follow it and must still land in an
    allowed root.
    """
    raw = str(value or "")
    if not raw or not os.path.isabs(raw):
        raise ValueError("absolute path required")
    if follow_leaf:
        resolved = os.path.realpath(raw)
    else:
        resolved = os.path.join(os.path.realpath(os.path.dirname(raw)), os.path.basename(raw))
    try:
        import policy
        roots = list(policy._load_config().get("allowedRoots", []))
    except Exception:
        roots = []
    roots.extend(_extra_allowed_roots())
    contained = False
    for root in roots:
        try:
            root_resolved = os.path.realpath(root)
        except OSError:
            continue
        if resolved == root_resolved or resolved.startswith(root_resolved + os.sep):
            contained = True
            break
    if not contained:
        raise PermissionError("path outside allowed roots")
    return resolved


def _validate_copied_symlinks(source: str) -> None:
    """Reject copies that would introduce a link escaping allowed roots."""
    candidates = [source]
    if os.path.isdir(source) and not os.path.islink(source):
        candidates = []
        for current, dirs, files in os.walk(source, followlinks=False):
            candidates.extend(os.path.join(current, name) for name in dirs + files)
    for candidate in candidates:
        if os.path.islink(candidate):
            _rpc_path(candidate)


def _fs_meta(path: str) -> dict:
    st = os.lstat(path)
    if os.path.islink(path):
        kind = "symlink"
    elif os.path.isdir(path):
        kind = "folder"
    else:
        kind = "file"
    return {
        "name": os.path.basename(path.rstrip(os.sep)) or os.sep,
        "type": kind,
        "size": int(st.st_size),
        "inode": str(st.st_ino),
        "createdAt": float(st.st_ctime),
        "modifiedAt": float(st.st_mtime),
        "accessedAt": float(st.st_atime),
        "symlinkTarget": os.readlink(path) if kind == "symlink" else None,
    }


def _run_fs_operation(op: str, args: dict):
    if op == "probe":
        return {"ready": True}
    if op == "list":
        path = _rpc_path(args.get("path"))
        if not os.path.isdir(path):
            raise NotADirectoryError(path)
        return [_fs_meta(entry.path) for entry in os.scandir(path)]
    if op == "stat":
        path = _rpc_path(args.get("path"), follow_leaf=False)
        return _fs_meta(path) if os.path.lexists(path) else None
    if op == "mkdir":
        path = _rpc_path(args.get("path"), follow_leaf=False)
        os.makedirs(path, exist_ok=True)
        return {"ok": True}
    if op == "remove":
        path = _rpc_path(args.get("path"), follow_leaf=False)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.unlink(path)
        else:
            raise FileNotFoundError(path)
        return {"ok": True}
    if op == "move":
        source = _rpc_path(args.get("source"), follow_leaf=False)
        destination = _rpc_path(args.get("destination"), follow_leaf=False)
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        shutil.move(source, destination)
        return _fs_meta(destination)
    if op == "copy":
        source = _rpc_path(args.get("source"), follow_leaf=False)
        destination = _rpc_path(args.get("destination"), follow_leaf=False)
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        _validate_copied_symlinks(source)
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
        return _fs_meta(destination)
    if op in ("search", "walk"):
        root = _rpc_path(args.get("root"))
        limit = min(max(int(args.get("limit") or 500), 1), 5000)
        recursive = bool(args.get("recursive", True))
        pattern = str(args.get("pattern") or "*")
        rows = []
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name != ".git"]
            names = dirs + files
            for name in names:
                full = os.path.join(current, name)
                rel = os.path.relpath(full, root)
                if op == "walk" or fnmatch.fnmatch(name, pattern):
                    meta = _fs_meta(full)
                    meta["path"] = rel
                    meta["absolutePath"] = full
                    rows.append(meta)
                    if len(rows) >= limit:
                        return rows
            if not recursive:
                break
        return rows
    if op == "quota":
        path = _rpc_path(args.get("path"))
        usage = shutil.disk_usage(path)
        return {"quota": int(usage.total), "usage": int(usage.used)}
    if op == "symlink":
        path = _rpc_path(args.get("path"), follow_leaf=False)
        target = str(args.get("target") or "")
        resolved_target = target if os.path.isabs(target) else os.path.join(os.path.dirname(path), target)
        _rpc_path(resolved_target)
        if os.path.lexists(path):
            raise FileExistsError(path)
        os.symlink(target, path)
        return _fs_meta(path)
    if op == "readlink":
        path = _rpc_path(args.get("path"), follow_leaf=False)
        if not os.path.islink(path):
            raise OSError("not a symbolic link")
        return {"target": os.readlink(path)}
    raise ValueError(f"unsupported filesystem operation: {op}")


class WebrtcManager:
    """One per agent connection. Holds a dedicated asyncio loop in a daemon
    thread and a peer connection per signaling session (keyed by reqId)."""

    def __init__(self, push_signal: Callable[[str, str, dict], None],
                 ice_servers=None):
        # push_signal(session_id, event_type, meta) → send an event back to the
        # browser via the agent event stream (e.g. "rtc-answer").
        self._push = push_signal
        self._ice = configured_ice_servers(ice_servers)
        self._pcs: dict = {}
        self._puts: dict = {}  # channel -> in-progress upload {f, path, written}
        self._vnc: dict = {}   # channel -> live VNC bridge {sock, task, name}
        # sessionId -> {channel (or None if detached), master_fd, pid, buffer,
        # grace_handle}. Keyed by sessionId (not channel) since a session must
        # survive its channel closing (refresh) and reattach on a new one.
        self._terms: dict = {}
        # reqId -> asyncio.subprocess.Process, for the AI-exec channel (below).
        self._ai_exec_procs: dict = {}
        # reqId -> asyncio.Future[str], resolved with "approve"/"deny" when the
        # matching ai-exec-approval-response frame arrives.
        self._ai_exec_approvals: dict = {}
        # Structured metadata calls are cheap but may recurse. Keep a hostile
        # or buggy peer from filling the process-wide thread pool.
        self._fs_slots = threading.BoundedSemaphore(4)
        self._loop = asyncio.new_event_loop()
        _bind_ports_in_range(self._loop, *_rtc_port_range())
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="laintas-webrtc",
        )
        self._thread.start()

    @staticmethod
    def available() -> bool:
        return AIORTC_AVAILABLE

    @staticmethod
    def import_error() -> str:
        return _IMPORT_ERROR

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _config(self):
        return RTCConfiguration(iceServers=[RTCIceServer(urls=u) for u in self._ice])

    # ── Signaling entry points (called from the sync poll thread) ────────
    def handle_offer(self, session_id: str, sdp: str):
        if not AIORTC_AVAILABLE:
            return
        asyncio.run_coroutine_threadsafe(self._handle_offer(session_id, sdp), self._loop)

    def handle_close(self, session_id: str):
        if not AIORTC_AVAILABLE:
            return
        asyncio.run_coroutine_threadsafe(self._close(session_id), self._loop)

    async def _handle_offer(self, session_id: str, sdp: str):
        pc = RTCPeerConnection(self._config())
        self._pcs[session_id] = pc

        @pc.on("datachannel")
        def on_datachannel(channel):
            @channel.on("message")
            def on_message(message):
                try:
                    self._on_message(channel, message)
                except Exception:
                    pass

            @channel.on("close")
            def on_close():
                self._close_vnc(channel)
                self._detach_terms_for_channel(channel)

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self._close(session_id)

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            await pc.setLocalDescription(await pc.createAnswer())  # gathers ICE
            # Non-trickle: localDescription.sdp now carries the candidates.
            self._push(session_id, "rtc-answer", {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
            })
        except Exception as e:
            self._push(session_id, "rtc-error", {"error": str(e)})
            await self._close(session_id)

    def _on_message(self, channel, message):
        # Binary frames on a VNC channel are RFB input bytes (noVNC → x11vnc).
        if isinstance(message, (bytes, bytearray)):
            v = self._vnc.get(channel)
            if v is not None:
                sock = v.get("sock")
                if sock is not None:
                    try:
                        sock.sendall(message)
                    except OSError:
                        pass
                return
        # Binary frames are file-upload chunks for an in-progress 'put'.
        if isinstance(message, (bytes, bytearray)):
            st = self._puts.get(channel)
            if st is not None:
                try:
                    st["f"].write(message)
                    st["written"] += len(message)
                except Exception as e:
                    st["error"] = str(e)
            return

        try:
            data = json.loads(message)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        t = data.get("t")

        if t == "ping":
            channel.send(json.dumps({"t": "pong", "id": data.get("id")}))
        elif t == "get":
            asyncio.ensure_future(self._serve_get(channel, data))
        elif t == "put":
            self._begin_put(channel, data)
        elif t == "put-end":
            self._finish_put(channel, data)
        elif t == "exec":
            asyncio.ensure_future(self._serve_exec(channel, data))
        elif t == "fs":
            asyncio.ensure_future(self._serve_fs(channel, data))
        elif t == "http":
            asyncio.ensure_future(self._serve_http(channel, data))
        elif t == "vnc-open":
            asyncio.ensure_future(self._open_vnc(channel, data))
        elif t == "vnc-close":
            self._close_vnc(channel)
        elif t == "term-open":
            asyncio.ensure_future(self._open_term(channel, data))
        elif t == "term-i":
            self._term_input(data)
        elif t == "term-resize":
            self._term_resize(data)
        elif t == "term-detach":
            self._term_client_detach(data)
        elif t == "term-close":
            self._terminate_term(str(data.get("id") or ""), notify=False)
        elif t == "ai-exec":
            asyncio.ensure_future(self._serve_ai_exec(channel, data))
        elif t == "ai-exec-approval-response":
            self._handle_ai_exec_approval_response(data)
        elif t == "ai-exec-abort":
            self._abort_ai_exec(data)

    # ── Metadata RPC: exec a short shell command (ls/find/mkdir/mv/rm/stat) ──
    # Used by the browser RemoteProvider so directory listing + metadata also
    # flow off-server over the P2P channel, not just file bytes.
    async def _serve_exec(self, channel, msg: dict):
        rid = msg.get("id")
        cmd = msg.get("cmd") or ""
        err = _validate_exec_cmd(cmd)
        if err:
            channel.send(json.dumps({"t": "exec-res", "id": rid, "ok": False,
                                     "code": -1, "out": f"rejected: {err}"}))
            return
        err = _validate_exec_paths(cmd)
        if err:
            channel.send(json.dumps({"t": "exec-res", "id": rid, "ok": False,
                                     "code": -1, "out": f"rejected: {err}"}))
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            out = (out_b or b"").decode("utf-8", "replace")
            if len(out) > 256 * 1024:
                out = out[:256 * 1024]
            channel.send(json.dumps({"t": "exec-res", "id": rid, "ok": proc.returncode == 0,
                                     "code": proc.returncode, "out": out}))
        except asyncio.TimeoutError:
            channel.send(json.dumps({"t": "exec-res", "id": rid, "ok": False, "code": -1,
                                     "out": "command timed out"}))
        except Exception as e:
            channel.send(json.dumps({"t": "exec-res", "id": rid, "ok": False, "code": -1, "out": str(e)}))

    # ── Structured filesystem metadata RPC ─────────────────────────────
    # RemoteProvider used to send shell snippets containing pipes, redirects
    # and `if; then`, while the security validator correctly rejected those
    # metacharacters. Keep arbitrary commands on the approval-gated exec path;
    # filesystem metadata uses explicit operations and native Python APIs.
    async def _serve_fs(self, channel, msg: dict):
        rid = msg.get("id")
        op = str(msg.get("op") or "")
        args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
        if not self._fs_slots.acquire(blocking=False):
            channel.send(json.dumps({"t": "fs-res", "id": rid, "ok": False,
                                     "error": "filesystem operation limit reached"}))
            return
        try:
            result = await asyncio.to_thread(_run_fs_operation, op, args)
            channel.send(json.dumps({"t": "fs-res", "id": rid, "ok": True, "result": result}))
        except Exception as e:
            channel.send(json.dumps({"t": "fs-res", "id": rid, "ok": False,
                                     "error": str(e)}))
        finally:
            self._fs_slots.release()

    # ── Interactive terminal: real PTY multiplexed over the DataChannel ──
    # Deliberately separate from `exec` (whitelisted, one-shot, machine-driven
    # metadata calls) — this spawns an actual shell reacting to a human
    # typing in real time, exactly like the existing WS-relay terminal
    # (gateway.py's `/api/agents/<id>/term`) already does with no whitelist.
    # The trust boundary is the same one documented at the top of this file:
    # only a session the browser opened via the authenticated agent-send API
    # ever reaches this channel. shell_exec (the AI tool) must keep using the
    # policy.py-gated exec path — never route AI-driven commands through here.
    async def _open_term(self, channel, msg: dict):
        session_id = str(msg.get("id") or "")
        if not session_id:
            return
        cols = int(msg.get("cols") or 80)
        rows = int(msg.get("rows") or 24)

        existing = self._terms.get(session_id)
        if existing is not None:
            # Reattach: the previous channel for this session detached
            # (refresh/tab-switch) within the grace window — rebind to the
            # new channel and replay whatever the shell printed while nobody
            # was listening, instead of silently starting a fresh shell.
            handle = existing.get("grace_handle")
            if handle is not None:
                handle.cancel()
                existing["grace_handle"] = None
            existing["channel"] = channel
            _set_winsize(existing["master_fd"], rows, cols)
            buffered = bytes(existing["buffer"])
            existing["buffer"].clear()
            if buffered:
                channel.send(json.dumps({
                    "t": "term-o", "id": session_id,
                    "d": base64.b64encode(buffered).decode("ascii"),
                }))
            channel.send(json.dumps({"t": "term-open-ack", "id": session_id, "resumed": True}))
            return

        roots = _extra_allowed_roots()
        cwd = roots[0] if roots else None
        try:
            pid, master_fd = pty.fork()
        except OSError as e:
            channel.send(json.dumps({"t": "term-exit", "id": session_id, "error": str(e)}))
            return
        if pid == 0:
            # Child: must do nothing but exec immediately — the parent's
            # asyncio loop / aiortc state is meaningless post-fork in a
            # multi-threaded process (same fork+exec discipline as
            # InteractiveSession in laintas_cli.py).
            try:
                if cwd:
                    os.chdir(cwd)
            except Exception:
                pass
            os.environ["TERM"] = "xterm-256color"
            shell = os.environ.get("SHELL") or "/bin/bash"
            try:
                os.execvp(shell, [shell])
            except Exception:
                pass
            os._exit(1)

        # Parent
        _set_winsize(master_fd, rows, cols)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        term = {"channel": channel, "master_fd": master_fd, "pid": pid,
                "buffer": bytearray(), "grace_handle": None}
        self._terms[session_id] = term

        def _on_readable():
            t = self._terms.get(session_id)
            if t is None:
                return
            try:
                data = os.read(master_fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                self._terminate_term(session_id)
                return
            ch = t["channel"]
            if ch is None:
                # Detached mid-grace — buffer for replay on reattach.
                t["buffer"].extend(data)
                overflow = len(t["buffer"]) - _TERM_BUFFER_CAP
                if overflow > 0:
                    del t["buffer"][:overflow]
                return
            try:
                ch.send(json.dumps({
                    "t": "term-o", "id": session_id,
                    "d": base64.b64encode(data).decode("ascii"),
                }))
            except Exception:
                pass

        self._loop.add_reader(master_fd, _on_readable)
        channel.send(json.dumps({"t": "term-open-ack", "id": session_id}))

    def _term_input(self, msg: dict) -> None:
        term = self._terms.get(str(msg.get("id") or ""))
        if not term:
            return
        try:
            data = base64.b64decode(msg.get("d") or "")
            os.write(term["master_fd"], data)
        except OSError:
            self._terminate_term(str(msg.get("id") or ""), notify=False)
        except Exception:
            pass

    def _term_resize(self, msg: dict) -> None:
        term = self._terms.get(str(msg.get("id") or ""))
        if not term:
            return
        cols = int(msg.get("cols") or 80)
        rows = int(msg.get("rows") or 24)
        _set_winsize(term["master_fd"], rows, cols)
        try:
            os.kill(term["pid"], signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def _term_client_detach(self, msg: dict) -> None:
        """Quiet detach requested in-app (not a real channel close) — e.g. the
        terminal pane was closed but the DataChannel (shared with fs ops) stays
        open. Arms the same grace timer a real channel close would."""
        session_id = str(msg.get("id") or "")
        term = self._terms.get(session_id)
        if not term or term.get("channel") is None:
            return
        term["channel"] = None
        term["grace_handle"] = self._loop.call_later(
            _TERM_DETACH_GRACE_SECONDS, self._terminate_term, session_id)

    def _detach_terms_for_channel(self, channel) -> None:
        """A DataChannel actually closed (refresh/navigate away/connection
        drop) — detach every session still attached to it with the same
        grace window, rather than killing shells the instant a tab reloads."""
        for session_id, term in list(self._terms.items()):
            if term.get("channel") is channel:
                term["channel"] = None
                term["grace_handle"] = self._loop.call_later(
                    _TERM_DETACH_GRACE_SECONDS, self._terminate_term, session_id)

    def _terminate_term(self, session_id: str, notify: bool = True) -> None:
        term = self._terms.pop(session_id, None)
        if term is None:
            return
        handle = term.get("grace_handle")
        if handle is not None:
            handle.cancel()
        master_fd = term["master_fd"]
        try:
            self._loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            os.kill(term["pid"], signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(term["pid"], os.WNOHANG)
        except ChildProcessError:
            pass
        channel = term.get("channel")
        if notify and channel is not None:
            try:
                channel.send(json.dumps({"t": "term-exit", "id": session_id}))
            except Exception:
                pass

    # ── AI-driven remote exec, fully over the P2P channel ───────────────
    # Same policy.py evaluate()/approval gate as the server-relayed exec
    # protocol (HELPWO_INTEGRATION_PLAN's kind:'exec') — that safety check is
    # a local decision the CLI makes regardless of transport and stays
    # intact. What moves is *where the bytes travel*: command text, output,
    # the approval prompt and the user's answer now all ride this
    # DataChannel instead of /api/agents/<id>/send + /updates, so the
    # gateway never sees (or logs) any of it. shell_exec must always create
    # its reqId-tagged request through this path when a P2P channel is
    # available — the browser side no longer has a server-relay fallback
    # for this either (see remoteHostExec.ts).
    async def _request_p2p_approval(self, channel, req_id: str, cmd: str, cwd: str, destructive: bool) -> bool:
        fut = self._loop.create_future()
        self._ai_exec_approvals[req_id] = fut
        try:
            channel.send(json.dumps({
                "t": "ai-exec-approval", "id": req_id, "cmd": cmd, "cwd": cwd,
                "destructive": destructive,
            }))
            try:
                decision = await asyncio.wait_for(fut, timeout=300)
            except asyncio.TimeoutError:
                decision = "deny"
        finally:
            self._ai_exec_approvals.pop(req_id, None)
        return decision == "approve"

    def _handle_ai_exec_approval_response(self, msg: dict) -> None:
        req_id = str(msg.get("id") or "")
        fut = self._ai_exec_approvals.get(req_id)
        if fut is not None and not fut.done():
            fut.set_result(str(msg.get("decision") or "deny"))

    def _abort_ai_exec(self, msg: dict) -> None:
        req_id = str(msg.get("id") or "")
        proc = self._ai_exec_procs.get(req_id)
        if proc is not None:
            _kill_process_group(proc)
        fut = self._ai_exec_approvals.get(req_id)
        if fut is not None and not fut.done():
            fut.set_result("deny")

    async def _serve_ai_exec(self, channel, msg: dict):
        req_id = str(msg.get("id") or "")
        cmd = str(msg.get("cmd") or "")
        roots = _extra_allowed_roots()
        cwd = str(msg.get("cwd") or (roots[0] if roots else os.getcwd()))
        try:
            timeout = int(msg.get("timeout") or 30)
        except (TypeError, ValueError):
            timeout = 30
        if not req_id or not cmd:
            channel.send(json.dumps({"t": "ai-exec-final", "id": req_id,
                                     "status": "fail", "error": "missing id/cmd"}))
            return
        try:
            cwd = _rpc_path(cwd)
        except (ValueError, PermissionError, OSError) as e:
            channel.send(json.dumps({"t": "ai-exec-final", "id": req_id,
                                     "status": "fail", "error": str(e)}))
            return
        if not os.path.isdir(cwd):
            channel.send(json.dumps({"t": "ai-exec-final", "id": req_id,
                                     "status": "fail", "error": f"invalid cwd: {cwd}"}))
            return

        import policy as _policy
        from agent_loop import get_runtime_config
        agent_id = getattr(_registry_ref, "agent_id", None)
        decision = _policy.evaluate(cmd, cwd, req_id=req_id, agent_id=agent_id)
        if decision.action == "deny":
            channel.send(json.dumps({"t": "ai-exec-final", "id": req_id, "status": "fail",
                                     "error": f"Blocked by policy: {decision.reason}"}))
            return
        if (decision.action == "needs_approval"
                or not get_runtime_config("allow_remote_exec_without_approval")):
            approved = await self._request_p2p_approval(
                channel, req_id, cmd, cwd,
                destructive=(_policy.is_delete_command(cmd)
                             or _policy.is_destructive_git_command(cmd)))
            if not approved:
                channel.send(json.dumps({"t": "ai-exec-final", "id": req_id, "status": "aborted",
                                         "error": f"User denied: {cmd[:100]}"}))
                return

        channel.send(json.dumps({"t": "ai-exec-start", "id": req_id, "cwd": cwd}))
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            channel.send(json.dumps({"t": "ai-exec-final", "id": req_id, "status": "fail", "error": str(e)}))
            return

        self._ai_exec_procs[req_id] = proc
        total = 0

        async def _pump():
            nonlocal total
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                total += len(chunk)
                if total <= _AI_EXEC_MAX_OUTPUT:
                    channel.send(json.dumps({
                        "t": "ai-exec-out", "id": req_id,
                        "data": chunk.decode("utf-8", "replace"),
                    }))

        try:
            await asyncio.wait_for(_pump(), timeout=timeout)
            code = await asyncio.wait_for(proc.wait(), timeout=5)
            channel.send(json.dumps({
                "t": "ai-exec-final", "id": req_id,
                "status": "success" if code == 0 else "fail", "exitCode": code,
            }))
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            channel.send(json.dumps({"t": "ai-exec-final", "id": req_id, "status": "fail",
                                     "error": f"timeout after {timeout}s"}))
        except asyncio.CancelledError:
            _kill_process_group(proc)
            raise
        finally:
            self._ai_exec_procs.pop(req_id, None)

    # ── HTTP tunnel RPC: proxy one request to a loopback port ───────────
    # Lets Helpwo render a dev server running on this host inside its preview
    # iframe (service worker → P2P → here → 127.0.0.1:<port>). Loopback-only
    # by design: this must never become an open proxy into the host's network.
    async def _serve_http(self, channel, msg: dict):
        rid = msg.get("id")

        def _head(ok, status=0, headers=None, error=None):
            payload = {"t": "http-head", "id": rid, "ok": ok, "status": status,
                       "headers": headers or {}}
            if error:
                payload["error"] = error
            channel.send(json.dumps(payload))

        try:
            port = int(msg.get("port") or 0)
            method = str(msg.get("method") or "GET").upper()
            path = str(msg.get("path") or "/")
            if not (1 <= port <= 65535):
                _head(False, error="bad port")
                return
            if not path.startswith("/"):
                path = "/" + path
            req_headers = msg.get("headers") or {}
            body_b64 = msg.get("body")

            def _do_request():
                import base64
                import http.client
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                try:
                    headers = {}
                    for k, v in req_headers.items():
                        # Hop-by-hop / auto-computed headers break the replayed request.
                        if k.lower() in ("host", "connection", "content-length",
                                         "accept-encoding", "transfer-encoding", "upgrade"):
                            continue
                        headers[str(k)] = str(v)
                    headers["Host"] = f"127.0.0.1:{port}"
                    headers["Accept-Encoding"] = "identity"
                    body = base64.b64decode(body_b64) if body_b64 else None
                    conn.request(method, path, body=body, headers=headers)
                    resp = conn.getresponse()
                    resp_headers = {}
                    for k, v in resp.getheaders():
                        if k.lower() in ("connection", "transfer-encoding", "keep-alive",
                                         "content-length"):
                            continue
                        resp_headers[k] = v
                    data = resp.read(_MAX_HTTP_BYTES + 1)
                    return resp.status, resp_headers, data
                finally:
                    conn.close()

            loop = asyncio.get_event_loop()
            status, resp_headers, body = await asyncio.wait_for(
                loop.run_in_executor(None, _do_request), timeout=25)
            if len(body) > _MAX_HTTP_BYTES:
                _head(False, error=f"response too large (>{_MAX_HTTP_BYTES} bytes)")
                return
            _head(True, status=status, headers=resp_headers)
            for i in range(0, len(body), _CHUNK):
                while channel.bufferedAmount > 1_000_000:
                    await asyncio.sleep(0.02)
                channel.send(body[i:i + _CHUNK])
            channel.send(json.dumps({"t": "http-end", "id": rid}))
        except asyncio.TimeoutError:
            _head(False, error="request timed out")
        except ConnectionRefusedError:
            _head(False, error="connection refused (is the dev server running on that port?)")
        except Exception as e:
            try:
                _head(False, error=str(e))
            except Exception:
                pass

    # ── File RPC: get (host → browser) ───────────────────────────────────
    async def _serve_get(self, channel, msg: dict):
        rid = msg.get("id")
        path = msg.get("path") or ""
        try:
            if not path or not os.path.isfile(path):
                channel.send(json.dumps({"t": "get-head", "id": rid, "ok": False, "error": "not a file"}))
                return
            if not _is_path_allowed(path):
                channel.send(json.dumps({"t": "get-head", "id": rid, "ok": False,
                                         "error": "path outside allowed roots"}))
                return
            size = os.path.getsize(path)
            if size > _MAX_FILE_BYTES:
                channel.send(json.dumps({"t": "get-head", "id": rid, "ok": False,
                                         "error": f"file too large ({size} bytes)"}))
                return
            channel.send(json.dumps({"t": "get-head", "id": rid, "ok": True, "size": size}))
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    # Backpressure: let the SCTP buffer drain so we don't OOM.
                    while channel.bufferedAmount > 1_000_000:
                        await asyncio.sleep(0.02)
                    channel.send(chunk)
            channel.send(json.dumps({"t": "get-end", "id": rid}))
        except Exception as e:
            try:
                channel.send(json.dumps({"t": "get-head", "id": rid, "ok": False, "error": str(e)}))
            except Exception:
                pass

    # ── File RPC: put (browser → host) ───────────────────────────────────
    def _begin_put(self, channel, msg: dict):
        rid = msg.get("id")
        path = msg.get("path") or ""
        size = int(msg.get("size") or 0)
        if not path or size < 0 or size > _MAX_FILE_BYTES:
            channel.send(json.dumps({"t": "put-ack", "id": rid, "ok": False, "error": "bad path/size"}))
            return
        if not _is_path_allowed(path):
            channel.send(json.dumps({"t": "put-ack", "id": rid, "ok": False,
                                     "error": "path outside allowed roots"}))
            return
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            f = open(path, "wb")
            self._puts[channel] = {"f": f, "path": path, "written": 0, "id": rid, "error": None}
        except Exception as e:
            channel.send(json.dumps({"t": "put-ack", "id": rid, "ok": False, "error": str(e)}))

    def _finish_put(self, channel, msg: dict):
        rid = msg.get("id")
        st = self._puts.pop(channel, None)
        if st is None:
            channel.send(json.dumps({"t": "put-ack", "id": rid, "ok": False, "error": "no upload in progress"}))
            return
        try:
            st["f"].close()
        except Exception:
            pass
        if st.get("error"):
            channel.send(json.dumps({"t": "put-ack", "id": rid, "ok": False, "error": st["error"]}))
        else:
            channel.send(json.dumps({"t": "put-ack", "id": rid, "ok": True, "written": st["written"]}))

    # ── VNC bridge: x11vnc RFB socket ⇄ DataChannel ─────────────────────
    # Renders a remote browser session's screen P2P, bypassing the relay.
    # Control frames are JSON strings; framebuffer/input bytes are raw binary.
    async def _open_vnc(self, channel, msg: dict):
        name = msg.get("name") or "default"
        try:
            import browser_session as _bs
            sess = _bs.get_browser_session(name)
            if sess is None:
                # The live-view button has no session picker — it always asks
                # for "default" — while browser.open auto-names sessions
                # browser1, browser2, ... Rather than make the button depend on
                # how a session happened to be named, show the newest one.
                sess = _bs.get_latest_browser_session()
        except Exception as e:
            channel.send(json.dumps({"t": "vnc-error", "error": f"registry: {e}"}))
            return
        rfb_port = getattr(sess, "rfb_port", 0) if sess is not None else 0
        if not rfb_port:
            channel.send(json.dumps({"t": "vnc-error", "error": f"no session '{name}'"}))
            return
        try:
            sock = socket.create_connection(("127.0.0.1", rfb_port), timeout=5)
            sock.setblocking(False)
        except OSError as e:
            channel.send(json.dumps({"t": "vnc-error", "error": f"rfb connect: {e}"}))
            return
        task = asyncio.ensure_future(self._pump_rfb_to_channel(channel, sock))
        self._vnc[channel] = {"sock": sock, "task": task, "name": name}
        channel.send(json.dumps({"t": "vnc-ready"}))

    async def _pump_rfb_to_channel(self, channel, sock):
        """x11vnc → noVNC. Raw RFB bytes as binary frames, with backpressure."""
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.sock_recv(sock, 65536)
                if not data:
                    break
                # Let the SCTP buffer drain so a slow viewer doesn't OOM us.
                while channel.bufferedAmount > 1_000_000:
                    await asyncio.sleep(0.02)
                channel.send(data)
        except (OSError, asyncio.CancelledError):
            pass
        except Exception:
            pass
        finally:
            try:
                channel.send(json.dumps({"t": "vnc-exit"}))
            except Exception:
                pass

    def _close_vnc(self, channel):
        v = self._vnc.pop(channel, None)
        if not v:
            return
        task = v.get("task")
        if task is not None:
            task.cancel()
        sock = v.get("sock")
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    async def _close(self, session_id: str):
        pc = self._pcs.pop(session_id, None)
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass

    def close(self):
        """Shut down all peer connections, VNC bridges, and the event loop.

        Best-effort, idempotent. Called from the CLI shutdown cascade.
        """
        if self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._close_all(), self._loop)
            fut.result(timeout=5.0)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        self._thread.join(timeout=3.0)
        self._loop = None

    async def _close_all(self):
        """Close every peer connection and VNC bridge."""
        for sid in list(self._pcs):
            await self._close(sid)
        for channel in list(self._vnc):
            self._close_vnc(channel)
        for session_id in list(self._terms):
            self._terminate_term(session_id, notify=False)
        for proc in list(self._ai_exec_procs.values()):
            _kill_process_group(proc)
        self._ai_exec_procs.clear()
        for fut in list(self._ai_exec_approvals.values()):
            if not fut.done():
                fut.set_result("deny")
        self._ai_exec_approvals.clear()
        for channel in list(self._puts):
            st = self._puts.pop(channel, None)
            if st and st.get("f"):
                try:
                    st["f"].close()
                except Exception:
                    pass
