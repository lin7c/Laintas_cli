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
                agents = json.load(urlopen(base + "/api/agents", timeout=2))
                self.assertEqual(len(agents), 1)
                self.assertTrue(agents[0]["localBridge"])
                self.assertTrue(agents[0]["id"].startswith("local-"))
                self.assertEqual(agents[0]["workspacePath"], str(workspace))

                rebound = Request(base + "/api/local-fs/root", headers={"Host": "evil.example"})
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
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                self.assertTrue(json.load(urlopen(delete, timeout=2))["ok"])
                self.assertFalse(link.exists())
                self.assertTrue((target / "keep.txt").exists())

                req_id = "offline-test"
                request = Request(
                    base + f"/api/agents/{agents[0]['id']}/send",
                    data=json.dumps({"kind": "exec", "reqId": req_id, "payload": {"command": "pwd"}}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                self.assertEqual(json.load(urlopen(request, timeout=2))["reqId"], req_id)
                events = []
                deadline = time.time() + 2
                while time.time() < deadline and not events:
                    events = json.load(urlopen(
                        base + f"/api/agents/{agents[0]['id']}/updates?since=0", timeout=2,
                    ))["events"]
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
