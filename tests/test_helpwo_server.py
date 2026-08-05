import json
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import helpwo_server


class _OfflineRegistry:
    REMOTE_CONTROL_KINDS = frozenset({"abort", "approval-response", "disconnect", "term-close"})

    def __init__(self):
        self.agent_id = None
        self.agent_name = "offline-cli"
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
        group = "control" if control else "task"
        with self._remote_capacity_lock:
            self._remote_accepted[group] += 1
        return True

    def _run_bounded_remote(self, message, *_args):
        req_id = message["reqId"]
        self._push_events([{
            "type": "final", "content": "offline-ok",
            "meta": {"status": "success", "summary": "offline-ok"},
        }], req_id=req_id)
        group = "control" if message.get("kind") in self.REMOTE_CONTROL_KINDS else "task"
        with self._remote_capacity_lock:
            self._remote_accepted[group] = max(0, self._remote_accepted[group] - 1)

    def close(self):
        self._remote_executor.shutdown(wait=True)
        self._remote_control_executor.shutdown(wait=True)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HelpwoOfflineBridgeTests(unittest.TestCase):
    def test_offline_bridge_exposes_runtime_and_routes_events_without_cloud_id(self):
        registry = _OfflineRegistry()
        previous_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dist = root / "dist"
            workspace = root / "workspace"
            dist.mkdir()
            workspace.mkdir()
            (dist / "index.html").write_text("ok", encoding="utf-8")
            os.chdir(workspace)
            port = _free_port()
            try:
                ok, _ = helpwo_server.start_server(registry, dist_dir=dist, port=port, session={})
                self.assertTrue(ok)
                base = f"http://127.0.0.1:{port}"
                # The bridge authenticates now; a caller that is not a browser
                # presents the token as a header instead of exchanging it for
                # a cookie.
                token = helpwo_server.auth_token()
                self.assertTrue(token)
                auth = {"Authorization": f"token {token}"}
                agents = json.load(urlopen(Request(base + "/api/agents", headers=auth), timeout=2))
                self.assertEqual(len(agents), 1)
                self.assertTrue(agents[0]["localBridge"])
                self.assertTrue(agents[0]["id"].startswith("local-"))
                self.assertEqual(agents[0]["workspacePath"], str(workspace))

                rebound = Request(base + "/api/local-fs/root",
                                  headers={**auth, "Host": "evil.example"})
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(rebound, timeout=2)
                self.assertEqual(rejected.exception.code, 421)

                target = workspace / "target"
                target.mkdir()
                (target / "keep.txt").write_text("keep", encoding="utf-8")
                link = workspace / "link"
                link.symlink_to(target, target_is_directory=True)
                delete = Request(
                    base + "/api/local-fs/delete",
                    data=json.dumps({"path": str(link)}).encode(),
                    headers={**auth, "Content-Type": "application/json"}, method="POST",
                )
                self.assertTrue(json.load(urlopen(delete, timeout=2))["ok"])
                self.assertFalse(link.exists())
                self.assertTrue((target / "keep.txt").exists())

                req_id = "offline-test"
                request = Request(
                    base + f"/api/agents/{agents[0]['id']}/send",
                    data=json.dumps({"kind": "exec", "reqId": req_id, "payload": {"command": "pwd"}}).encode(),
                    headers={**auth, "Content-Type": "application/json"}, method="POST",
                )
                self.assertEqual(json.load(urlopen(request, timeout=2))["reqId"], req_id)
                events = []
                deadline = time.time() + 2
                while time.time() < deadline and not events:
                    events = json.load(urlopen(Request(
                        base + f"/api/agents/{agents[0]['id']}/updates?since=0",
                        headers=auth), timeout=2))["events"]
                    if not events:
                        time.sleep(0.02)
                self.assertEqual(events[-1]["type"], "final")
                self.assertEqual(events[-1]["reqId"], req_id)
            finally:
                helpwo_server.stop_server()
                os.chdir(previous_cwd)
                registry.close()


if __name__ == "__main__":
    unittest.main()


class HelpwoAuthAndSearchTests(unittest.TestCase):
    """The local bridge had no authentication at all, and no route for the
    path the frontend actually calls for search. Both are asserted here."""

    def setUp(self):
        self.registry = _OfflineRegistry()
        self.previous_cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        dist = root / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("ok", encoding="utf-8")
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, self.previous_cwd)
        self.port = _free_port()
        ok, _ = helpwo_server.start_server(
            self.registry, dist_dir=dist, port=self.port, session={})
        self.assertTrue(ok)
        self.addCleanup(helpwo_server.stop_server)
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = helpwo_server.auth_token()

    def _status(self, path, *, data=None, headers=None, method=None):
        request = Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers or {},
            method=method or ("POST" if data is not None else "GET"),
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as e:
            return e.code, e.read()

    def test_a_token_is_generated_by_default(self):
        self.assertGreaterEqual(len(self.token), 20)
        self.assertIn("token=", helpwo_server.get_url(with_token=True))

    def test_requests_without_the_token_are_refused(self):
        status, _ = self._status("/api/agents")
        self.assertEqual(status, 403)

    def test_the_url_token_is_exchanged_for_a_cookie(self):
        # Jupyter's model: the token appears once in the URL, and the cookie
        # carries it from then on.
        request = Request(self.base + f"/api/agents?token={self.token}")
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            cookie = response.headers.get("Set-Cookie", "")
        self.assertIn(helpwo_server._COOKIE_NAME, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

        status, _ = self._status(
            "/api/agents",
            headers={"Cookie": f"{helpwo_server._COOKIE_NAME}={self.token}"})
        self.assertEqual(status, 200)

    def test_a_wrong_token_does_not_authenticate(self):
        status, _ = self._status(f"/api/agents?token={'x' * 40}")
        self.assertEqual(status, 403)

    def test_non_json_post_is_refused_even_when_authenticated(self):
        # The CSRF hole: a cross-site form post of text/plain is a "simple
        # request", skips the CORS preflight entirely, and used to reach
        # /api/local-fs/write.
        status, _ = self._status(
            "/api/local-fs/write",
            data={"path": str(self.workspace / "x.txt"), "content": "hi"},
            headers={"Authorization": f"token {self.token}",
                     "Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertFalse((self.workspace / "x.txt").exists())

    def test_cross_site_post_is_refused(self):
        for headers in ({"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "https://evil.example"}):
            with self.subTest(headers=headers):
                status, _ = self._status(
                    "/api/local-fs/write",
                    data={"path": str(self.workspace / "y.txt"), "content": "hi"},
                    headers={"Authorization": f"token {self.token}",
                             "Content-Type": "application/json", **headers})
                self.assertEqual(status, 403)
        self.assertFalse((self.workspace / "y.txt").exists())

    def test_same_origin_json_post_is_allowed(self):
        status, _ = self._status(
            "/api/local-fs/write",
            data={"path": str(self.workspace / "z.txt"), "content": "hi"},
            headers={"Authorization": f"token {self.token}",
                     "Content-Type": "application/json",
                     "Origin": self.base,
                     "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertEqual((self.workspace / "z.txt").read_text(), "hi")

    def test_the_search_path_the_frontend_calls_exists(self):
        # /api/helpwo/search is what src/tools/web.ts posts to; this server
        # had no route for it, so local-mode search 404'd.
        captured = {}

        import web_search
        original = web_search.search
        self.addCleanup(setattr, web_search, "search", original)

        def fake_search(query, **kwargs):
            captured["query"] = query
            captured["kwargs"] = kwargs
            return {"ok": True, "engine": "cn-bing", "count": 1,
                    "result": [{"title": "T", "url": "https://e.example/a",
                                "snippet": "S"}]}

        web_search.search = fake_search
        status, body = self._status(
            "/api/helpwo/search",
            data={"query": "hello", "maxResults": 3, "region": "cn-zh",
                  "timelimit": "w"},
            headers={"Authorization": f"token {self.token}",
                     "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["engine"], "cn-bing")
        # The frontend renders this as a tag beside each result.
        self.assertEqual(payload["results"][0]["domain"], "e.example")
        self.assertEqual(captured["query"], "hello")
        self.assertEqual(captured["kwargs"]["region"], "cn-zh")
        self.assertEqual(captured["kwargs"]["timelimit"], "w")

    def test_fetch_is_served_in_process_and_can_name_an_identity(self):
        import web_search
        original = web_search.fetch
        self.addCleanup(setattr, web_search, "fetch", original)
        captured = {}

        def fake_fetch(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return {"ok": True, "result": "body text", "title": "A Title",
                    "final_url": url, "truncated": False, "transport": "http"}

        web_search.fetch = fake_fetch
        status, body = self._status(
            "/api/fetch",
            data={"url": "https://e.example/p", "maxChars": 5000,
                  "identity": "acct"},
            headers={"Authorization": f"token {self.token}",
                     "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["title"], "A Title")
        self.assertEqual(payload["text"], "body text")
        self.assertEqual(captured["kwargs"]["identity"], "acct")


class HelpwoVncSocketTests(unittest.TestCase):
    """The live view over the local bridge: RFB bytes on the same origin that
    serves the page, so the auth cookie applies and nothing relays them."""

    def test_accept_key_matches_the_rfc_example(self):
        # RFC 6455 §1.3. Getting the handshake GUID wrong yields a plausible
        # looking key that every client rejects.
        self.assertEqual(
            helpwo_server._ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_frames_use_the_right_length_form(self):
        for size, expect_len_byte in ((10, 10), (200, 126), (70000, 127)):
            with self.subTest(size=size):
                frame = helpwo_server._ws_frame(b"x" * size)
                self.assertEqual(frame[0], 0x82)          # FIN + binary
                self.assertEqual(frame[1] & 0x7F, expect_len_byte)
                self.assertEqual(frame[0] & 0x80, 0x80)

    def test_unmasked_client_frames_are_refused(self):
        # Client frames must be masked; accepting one would mean reading a
        # length the peer never masked and desynchronising the stream.
        import socket as _s
        a, b = _s.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(bytes([0x82, 0x04]) + b"abcd")   # binary, unmasked
        self.assertIsNone(helpwo_server._ws_read_frame(a))

    def test_masked_client_frames_round_trip(self):
        import socket as _s
        a, b = _s.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        payload = b"\x03\x00\x00\x01"
        mask = b"\x11\x22\x33\x44"
        masked = bytes(p ^ mask[i & 3] for i, p in enumerate(payload))
        b.sendall(bytes([0x82, 0x80 | len(payload)]) + mask + masked)
        opcode, out = helpwo_server._ws_read_frame(a)
        self.assertEqual(opcode, 0x2)
        self.assertEqual(out, payload)

    def test_a_session_waiting_on_the_user_wins_over_the_named_one(self):
        # The viewer's button always asks for "default"; without this a fetch
        # stuck on a challenge is invisible whenever any other session exists.
        import browser_session as bs

        class Session:
            def __init__(self, attention=False):
                self._needs_attention = attention
                self.rfb_port = 1
            def is_alive(self):
                return True

        waiting = Session(attention=True)
        with bs._browser_lock:
            saved = dict(bs._browser_sessions)
            bs._browser_sessions.clear()
            bs._browser_sessions["default"] = Session()
            bs._browser_sessions["web-fetch"] = waiting
        try:
            handler = helpwo_server._HelpwoHandler.__new__(helpwo_server._HelpwoHandler)
            resolved, err = handler._resolve_vnc_session("default")
            self.assertEqual(err, "")
            self.assertIs(resolved, waiting)
        finally:
            with bs._browser_lock:
                bs._browser_sessions.clear()
                bs._browser_sessions.update(saved)
