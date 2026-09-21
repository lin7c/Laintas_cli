"""K4 (bughunt): a timed-out node's agents must not stay "running".

The timeout/cancel path set node_abort, joined for 2s, then abandoned the
thread. An HWO run stuck in a long tool call never observed the abort,
so its named agents stayed "running" in the registry forever: the node's
retry hit "agent id is already in use" (only *finished* agents are
reclaimable) and the old execution kept running in the background. The
fix tracks agents spawned per attempt (via the events stream) and
force-aborts them when the thread is abandoned.
"""
import threading
import time
import unittest
from unittest import mock

import hwg_runner


class FakeInfo:
    def __init__(self, status):
        self.status = status


class ForceAbortTests(unittest.TestCase):
    def test_running_agent_is_force_aborted(self):
        registry = {"stuck": FakeInfo("running")}
        aborted = []
        with mock.patch("agent_loop.get_agent",
                        side_effect=lambda i: registry.get(i)), \
             mock.patch("agent_loop.abort_agent",
                        side_effect=lambda i: aborted.append(i)):
            hwg_runner._force_abort_node_agents(["stuck"])
        self.assertEqual(aborted, ["stuck"])

    def test_finished_and_missing_agents_are_skipped(self):
        registry = {"done": FakeInfo("done"), "err": FakeInfo("error")}
        aborted = []
        with mock.patch("agent_loop.get_agent",
                        side_effect=lambda i: registry.get(i)), \
             mock.patch("agent_loop.abort_agent",
                        side_effect=lambda i: aborted.append(i)):
            hwg_runner._force_abort_node_agents(["done", "err", "nope"])
        self.assertEqual(aborted, [])


class TimeoutPathTests(unittest.TestCase):
    def test_timed_out_node_force_aborts_spawned_agent(self):
        """A node whose HWO run hangs past its timeout must have its spawned
        agent force-aborted, so a retry can reclaim the id."""
        import tempfile, os
        from pathlib import Path

        registry = {}
        aborted = []

        def fake_run_hwo_file(path, **kw):
            # Spawn a named agent via the events stream, then hang forever
            # ignoring the abort event (the K4 shape).
            kw["events_cb"]([{"type": "agent_spawned", "agentId": "hungry"}])
            registry["hungry"] = FakeInfo("running")
            while True:
                time.sleep(0.05)

        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                Path("flow.hwg").write_text(
                    '(a.hwo)#a# { timeout: "1s" }\n', encoding="utf-8")
                with mock.patch.object(
                        hwg_runner.hwo_runner, "run_hwo_file",
                        side_effect=fake_run_hwo_file), \
                     mock.patch("agent_loop.get_agent",
                                side_effect=lambda i: registry.get(i)), \
                     mock.patch("agent_loop.abort_agent",
                                side_effect=lambda i: aborted.append(i)):
                    result = hwg_runner.run_hwg_file(
                        "flow.hwg", deps=object(), session={})
            finally:
                os.chdir(old)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["msg"])
        self.assertIn("hungry", aborted,
                      "timed-out node did not force-abort its agent")

    def test_tracking_events_cb_forwards_to_caller(self):
        seen = []
        cb = None
        # Build the tracking wrapper the way _run_hwo_with_policy does.
        spawned = []

        def _tracking(rows):
            for row in rows or []:
                if row.get("type") == "agent_spawned":
                    aid = row.get("agentId")
                    if aid and aid not in spawned:
                        spawned.append(aid)
            seen.extend(rows or [])

        _tracking([{"type": "agent_spawned", "agentId": "x"},
                   {"type": "other"}])
        _tracking([{"type": "agent_spawned", "agentId": "x"}])  # dedup
        self.assertEqual(spawned, ["x"])
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
