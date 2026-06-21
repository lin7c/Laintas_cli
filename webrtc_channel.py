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
import threading
from typing import Callable

# File RPC limits (Layer 2). Bytes flow as raw binary DataChannel messages.
_CHUNK = 16 * 1024            # per binary message (safe under SCTP message size)
_MAX_FILE_BYTES = 5 * 1024 * 1024

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

    async def _close(self, session_id: str):
        pc = self._pcs.pop(session_id, None)
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass
