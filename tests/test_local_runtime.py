"""Tests for the loopback half of the Helpwo ⇄ laintas_cli transports.

These cover the three surfaces that used to be P2P-only — command execution,
the interactive PTY and the HTTP tunnel — plus the cross-writer coordination
that keeps a browser-side agent and this CLI's agent loop from silently
overwriting each other in a shared working directory.

The bug being guarded against in each case is a real one that shipped:

  * a local /helpwo session negotiated ICE with itself to run `ls`, and failed
    outright when the optional aiortc extra was absent — on the very machine
    that was serving the page;
  * Helpwo's writes reached the disk *through* this process, so the peer
    scanner (which counts processes) never saw a second writer and the later
    write won with no error on either side.
"""

import base64
import json
import os
import signal
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
from urllib.request import Request, urlopen

import helpwo_server
import local_runtime
import peer_coordination


class ProcessGroupCleanupTests(unittest.TestCase):
    def test_kill_process_group_reaps_the_leader(self):
        proc = mock.Mock()
        proc.pid = 4242
        with mock.patch.object(local_runtime.os, "getpgid", return_value=4242), \
             mock.patch.object(local_runtime.os, "killpg") as killpg:
            local_runtime._kill_process_group(proc)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        proc.wait.assert_called_once_with(timeout=2)


class _Registry:
    """The minimum AgentRegistry surface the bridge touches."""

    REMOTE_CONTROL_KINDS = frozenset({"abort"})

    def __init__(self):
        self.agent_id = None
        self.agent_name = "test-cli"
        self.parent_remote_id = None
        self.terminal_meta = None
        self._state_cb = None
        self._chat_cb = None
        self._remote_executor = ThreadPoolExecutor(max_workers=2)
        self._remote_control_executor = ThreadPoolExecutor(max_workers=1)
        self._remote_capacity_lock = threading.Condition(threading.RLock())
        self._remote_accepted = {"task": 0, "control": 0}
        self._push_events = lambda events, req_id=None: None

    def _reserve_remote_capacity(self, control):
        return True

    def close(self):
        self._remote_executor.shutdown(wait=False)
        self._remote_control_executor.shutdown(wait=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _mask_frame(payload: bytes, opcode: int = 0x2) -> bytes:
    """A client-side WebSocket frame. Clients must mask; the server must not."""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        header = bytes([0x80 | opcode, 0x80 | n])
    else:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", n)
    return header + mask + masked


class LoopbackRuntimeTests(unittest.TestCase):
    TOKEN = "test-token"

    def setUp(self):
        self.registry = _Registry()
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.dist = root / "dist"
        self.workspace = root / "workspace"
        self.dist.mkdir()
        self.workspace.mkdir()
        (self.dist / "index.html").write_text("ok", encoding="utf-8")
        os.chdir(self.workspace)

        self.port = _free_port()
        ok, message = helpwo_server.start_server(
            self.registry, dist_dir=self.dist, port=self.port, token=self.TOKEN)
        self.assertTrue(ok, message)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        helpwo_server.stop_server()
        self.registry.close()
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _request(self, path, body=None, method=None, raw=False):
        import urllib.error
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data,
                          method=method or ("POST" if data else "GET"))
        request.add_header("Cookie", f"laintas_helpwo_token={self.TOKEN}")
        request.add_header("Sec-Fetch-Site", "same-origin")
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=20) as response:
                payload = response.read()
                return response.status, (payload if raw else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as e:
            # An error status is an answer here, not a transport failure: the
            # tunnel reporting 502 for a dead port is the behaviour under test.
            payload = e.read()
            return e.code, (payload if raw else json.loads(payload or b"{}"))

    def _exec(self, req_id, cmd, timeout=15, approve=True):
        """Run one command over SSE, answering the approval prompt if asked."""
        request = Request(
            self.base + "/api/local-exec",
            data=json.dumps({"reqId": req_id, "cmd": cmd,
                             "cwd": str(self.workspace), "timeout": timeout}).encode(),
            method="POST")
        request.add_header("Cookie", f"laintas_helpwo_token={self.TOKEN}")
        request.add_header("Sec-Fetch-Site", "same-origin")
        request.add_header("Content-Type", "application/json")
        events = []
        with urlopen(request, timeout=timeout + 20) as response:
            for line in response:
                text = line.decode().strip()
                if not text.startswith("data: "):
                    continue
                event = json.loads(text[6:])
                events.append(event)
                if event.get("t") == "approval":
                    decision = "approve" if approve else "reject"
                    threading.Thread(
                        target=self._request,
                        args=("/api/local-exec/approval",
                              {"reqId": req_id, "decision": decision}),
                        daemon=True).start()
                if event.get("t") == "final":
                    break
        return events

    # -- topology negotiation --------------------------------------------

    def test_local_runtime_probe_names_every_surface(self):
        """The probe is how the frontend decides loopback vs p2p — once."""
        status, body = self._request("/api/local-runtime")
        self.assertEqual(status, 200)
        self.assertEqual(body["topology"], "loopback")
        self.assertTrue(body["agentId"])
        self.assertEqual(body["root"], str(self.workspace))
        for surface in ("fs", "exec", "pty", "proxy", "screen"):
            self.assertIn(surface, body["surfaces"])

    # -- exec ------------------------------------------------------------

    def test_exec_streams_output_and_exit_code(self):
        events = self._exec("r-ok", "echo hello-loopback")
        kinds = [e["t"] for e in events]
        self.assertIn("start", kinds)
        final = events[-1]
        self.assertEqual(final["t"], "final")
        self.assertEqual(final["status"], "success")
        self.assertEqual(final["exitCode"], 0)
        output = "".join(e.get("data", "") for e in events if e["t"] == "out")
        self.assertIn("hello-loopback", output)

    def test_exec_reports_a_failing_command_as_fail(self):
        events = self._exec("r-fail", "exit 3")
        final = events[-1]
        self.assertEqual(final["status"], "fail")
        self.assertEqual(final["exitCode"], 3)

    def test_exec_rejection_never_runs_the_command(self):
        """A denied approval must not leave the side effect behind."""
        marker = self.workspace / "should-not-exist"
        events = self._exec("r-deny", f"touch {marker}", approve=False)
        final = events[-1]
        if any(e["t"] == "approval" for e in events):
            self.assertEqual(final["status"], "aborted")
            self.assertFalse(marker.exists())
        else:
            # Approval is off in this environment's runtime config; then the
            # command legitimately ran and there is nothing to assert about a
            # rejection that was never requested.
            self.assertEqual(final["status"], "success")

    def test_a_silent_command_is_stopped_and_killed(self):
        """`timeout` is an idle budget: a command that says nothing for that
        long is presumed wedged. A command that keeps printing is not — see
        test_shell_idle_budget for the other half of that contract."""
        events = self._exec("r-slow", "sleep 30", timeout=2)
        final = events[-1]
        self.assertEqual(final["status"], "fail")
        self.assertIn("no output", final.get("error", ""))

    def test_abort_stops_a_running_command(self):
        req_id = "r-abort"
        result = {}

        def run():
            result["events"] = self._exec(req_id, "sleep 30", timeout=60)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if local_runtime.status()["execRunning"]:
                break
            time.sleep(0.05)
        self._request("/api/local-exec/abort", {"reqId": req_id})
        worker.join(timeout=20)
        self.assertIn("events", result)
        self.assertEqual(result["events"][-1]["status"], "aborted")

    # -- PTY -------------------------------------------------------------

    def _open_pty(self, session_id="t1"):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (f"GET /api/local-pty?id={session_id}&cols=80&rows=24 HTTP/1.1\r\n"
             f"Host: 127.0.0.1:{self.port}\r\n"
             f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
             f"Cookie: laintas_helpwo_token={self.TOKEN}\r\n\r\n").encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(1)
            if not chunk:
                break
            header += chunk
        return sock, header

    def _read_until(self, sock, needle, seconds=8):
        sock.settimeout(1.0)
        seen = b""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            seen += data
            if needle in seen:
                return True, seen
        return needle in seen, seen

    def test_pty_runs_a_real_shell(self):
        sock, header = self._open_pty("pty-real")
        try:
            self.assertIn(b"101 Switching Protocols", header)
            sock.sendall(_mask_frame(b"echo pty-works\n"))
            found, _seen = self._read_until(sock, b"pty-works")
            self.assertTrue(found, "the shell never echoed the command back")
        finally:
            sock.close()
            self._request("/api/local-pty/close", {"id": "pty-real"})

    def test_pty_reattaches_instead_of_starting_a_second_shell(self):
        """A refresh must find the same shell, with what it missed replayed."""
        sock, _header = self._open_pty("pty-resume")
        sock.sendall(_mask_frame(b"export MARKER=survived\n"))
        self._read_until(sock, b"MARKER", seconds=4)
        sock.close()                       # the "refresh"
        time.sleep(0.3)

        sock2, header2 = self._open_pty("pty-resume")
        try:
            self.assertIn(b"101 Switching Protocols", header2)
            sock2.sendall(_mask_frame(b"echo $MARKER\n"))
            found, _seen = self._read_until(sock2, b"survived")
            self.assertTrue(found, "reattaching started a fresh shell")
        finally:
            sock2.close()
            self._request("/api/local-pty/close", {"id": "pty-resume"})

    # -- HTTP tunnel ------------------------------------------------------

    def test_proxy_reaches_a_loopback_port(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b"tunneled:" + self.path.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: A003
                pass

        port = _free_port()
        server = TCPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, body = self._request(f"/api/local-proxy/{port}/page?x=1", raw=True)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"tunneled:/page?x=1")
        finally:
            server.shutdown()
            server.server_close()

    def test_proxy_reports_a_dead_port_rather_than_hanging(self):
        status, body = self._request(f"/api/local-proxy/{_free_port()}/", raw=False)
        self.assertEqual(status, 502)
        self.assertIn("error", body)


class CrossWriterCoordinationTests(unittest.TestCase):
    """Helpwo and the CLI's agent loop must not silently clobber each other."""

    def setUp(self):
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chdir(self.root)
        peer_coordination._coord = None          # a fresh coordinator per test
        self.coord = peer_coordination.get_coord()
        self.coord.register(str(self.root))

    def tearDown(self):
        peer_coordination.detach_external_actor(peer_coordination.HELPWO_P2P_ACTOR)
        peer_coordination._coord = None
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def test_coordination_is_inert_with_a_single_writer(self):
        """One writer pays nothing: no tracking, no checks, no false blocks."""
        target = self.root / "solo.txt"
        target.write_text("v1", encoding="utf-8")
        self.coord.note_read(str(target))
        target.write_text("v2", encoding="utf-8")
        self.assertIsNone(self.coord.assert_unchanged(str(target)))

    def test_attaching_helpwo_activates_coordination(self):
        self.assertFalse(self.coord.enabled())
        peer_coordination.attach_external_actor(peer_coordination.HELPWO_P2P_ACTOR)
        self.assertTrue(self.coord.enabled())
        peer_coordination.detach_external_actor(peer_coordination.HELPWO_P2P_ACTOR)
        self.assertFalse(self.coord.enabled())

    def test_lost_update_is_blocked_in_both_directions(self):
        actor = peer_coordination.HELPWO_P2P_ACTOR
        peer_coordination.attach_external_actor(actor)
        target = self.root / "shared.ts"
        target.write_text("v1", encoding="utf-8")

        # Helpwo reads, the CLI writes underneath, Helpwo must not overwrite.
        peer_coordination.note_external_read(str(target), actor)
        self.coord.note_read(str(target))
        target.write_text("v2 by cli", encoding="utf-8")
        self.coord.note_write(str(target))
        self.assertIsNotNone(peer_coordination.guard_external_write(str(target), actor))

        # Re-reading is what clears it — the point is to force a fresh look.
        peer_coordination.note_external_read(str(target), actor)
        self.assertIsNone(peer_coordination.guard_external_write(str(target), actor))

        # And the mirror image: Helpwo writes, the CLI is the one blocked.
        target.write_text("v3 by helpwo", encoding="utf-8")
        peer_coordination.note_external_write(str(target), "put", actor)
        self.assertIsNotNone(self.coord.assert_unchanged(str(target)))

    def test_each_writer_is_unaffected_by_its_own_writes(self):
        actor = peer_coordination.HELPWO_P2P_ACTOR
        peer_coordination.attach_external_actor(actor)
        target = self.root / "mine.ts"
        target.write_text("v1", encoding="utf-8")
        for index in range(3):
            peer_coordination.note_external_read(str(target), actor)
            self.assertIsNone(peer_coordination.guard_external_write(str(target), actor))
            target.write_text(f"v{index + 2}", encoding="utf-8")
            peer_coordination.note_external_write(str(target), "put", actor)

    def test_the_two_transports_hold_independent_attachments(self):
        """The bridge and a peer can be up at once; either leaving must not
        revoke the other's protection."""
        peer_coordination.attach_external_actor(peer_coordination.HELPWO_BRIDGE_ACTOR)
        peer_coordination.attach_external_actor(peer_coordination.HELPWO_P2P_ACTOR)
        peer_coordination.detach_external_actor(peer_coordination.HELPWO_P2P_ACTOR)
        self.assertTrue(self.coord.enabled())
        peer_coordination.detach_external_actor(peer_coordination.HELPWO_BRIDGE_ACTOR)
        self.assertFalse(self.coord.enabled())


class BridgeWritePathTests(unittest.TestCase):
    """The loopback filesystem endpoints must enforce the same CAS as P2P."""

    TOKEN = "test-token"

    def setUp(self):
        self.registry = _Registry()
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.dist = root / "dist"
        self.workspace = root / "workspace"
        self.dist.mkdir()
        self.workspace.mkdir()
        (self.dist / "index.html").write_text("ok", encoding="utf-8")
        os.chdir(self.workspace)
        peer_coordination._coord = None
        self.port = _free_port()
        ok, message = helpwo_server.start_server(
            self.registry, dist_dir=self.dist, port=self.port, token=self.TOKEN)
        self.assertTrue(ok, message)
        self.base = f"http://127.0.0.1:{self.port}"
        self.coord = peer_coordination.get_coord()
        self.coord.register(str(self.workspace))

    def tearDown(self):
        helpwo_server.stop_server()
        self.registry.close()
        peer_coordination._coord = None
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def _call(self, path, body=None, method=None):
        import urllib.error
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data,
                          method=method or ("POST" if data else "GET"))
        request.add_header("Cookie", f"laintas_helpwo_token={self.TOKEN}")
        request.add_header("Sec-Fetch-Site", "same-origin")
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_bridge_write_refuses_to_clobber_a_change_it_did_not_see(self):
        target = self.workspace / "shared.ts"
        target.write_text("v1", encoding="utf-8")

        status, _body = self._call(f"/api/local-fs/read?path={target}")
        self.assertEqual(status, 200)

        # The CLI's own agent loop edits it while the browser holds v1.
        self.coord.note_read(str(target))
        target.write_text("v2 by cli", encoding="utf-8")
        self.coord.note_write(str(target))

        status, body = self._call("/api/local-fs/write", {
            "path": str(target),
            "contentBase64": base64.b64encode(b"v2 by helpwo").decode(),
        })
        self.assertEqual(status, 409, body)
        self.assertIn("coordination", body.get("error", ""))
        self.assertEqual(target.read_text(encoding="utf-8"), "v2 by cli")

        # Re-reading clears it, and the write then lands.
        self._call(f"/api/local-fs/read?path={target}")
        status, _body = self._call("/api/local-fs/write", {
            "path": str(target),
            "contentBase64": base64.b64encode(b"v3 by helpwo").decode(),
        })
        self.assertEqual(status, 200)
        self.assertEqual(target.read_text(encoding="utf-8"), "v3 by helpwo")


if __name__ == "__main__":
    unittest.main()
