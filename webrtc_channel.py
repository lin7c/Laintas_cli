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
import json
import os
import socket
import threading
from typing import Callable

# File RPC limits (Layer 2). Bytes flow as raw binary DataChannel messages.
_CHUNK = 16 * 1024            # per binary message (safe under SCTP message size)
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_HTTP_BYTES = 20 * 1024 * 1024   # per tunneled HTTP response (dev bundles can be big)

try:
    from aiortc import (
        RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer,
    )
    AIORTC_AVAILABLE = True
    _IMPORT_ERROR = ""
except Exception as e:  # aiortc optional — CLI runs without it
    AIORTC_AVAILABLE = False
    _IMPORT_ERROR = str(e)

# Public STUN for NAT traversal. Swap for a self-hosted STUN/TURN to avoid
# leaking IPs to a third party (see the security notes in the design).
_DEFAULT_ICE = ["stun:stun.l.google.com:19302"]


class WebrtcManager:
    """One per agent connection. Holds a dedicated asyncio loop in a daemon
    thread and a peer connection per signaling session (keyed by reqId)."""

    def __init__(self, push_signal: Callable[[str, str, dict], None],
                 ice_servers=None):
        # push_signal(session_id, event_type, meta) → send an event back to the
        # browser via the agent event stream (e.g. "rtc-answer").
        self._push = push_signal
        self._ice = ice_servers or _DEFAULT_ICE
        self._pcs: dict = {}
        self._puts: dict = {}  # channel -> in-progress upload {f, path, written}
        self._vnc: dict = {}   # channel -> live VNC bridge {sock, task, name}
        self._loop = asyncio.new_event_loop()
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
        elif t == "http":
            asyncio.ensure_future(self._serve_http(channel, data))
        elif t == "vnc-open":
            asyncio.ensure_future(self._open_vnc(channel, data))
        elif t == "vnc-close":
            self._close_vnc(channel)

    # ── Metadata RPC: exec a short shell command (ls/find/mkdir/mv/rm/stat) ──
    # Used by the browser RemoteProvider so directory listing + metadata also
    # flow off-server over the P2P channel, not just file bytes.
    async def _serve_exec(self, channel, msg: dict):
        rid = msg.get("id")
        cmd = msg.get("cmd") or ""
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
