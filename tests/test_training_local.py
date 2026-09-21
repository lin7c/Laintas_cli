"""`/training local` — on-machine capture, independent of cloud sharing."""

import importlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TrainingLocalStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("LAINTAS_HOME")
        os.environ["LAINTAS_HOME"] = self._tmp.name
        import paths
        importlib.reload(paths)
        import training_local
        self.mod = importlib.reload(training_local)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("LAINTAS_HOME", None)
        else:
            os.environ["LAINTAS_HOME"] = self._prev
        self._tmp.cleanup()

    def _sink(self, kind="critic", reply='{"on_track": true}'):
        return {
            "payload": {
                "messages": [{"role": "user", "content": "recent actions"}],
                "systemPrompt": "You are a progress supervisor.",
                "taskKind": kind,
                "trajectoryId": "traj-1",
                "tools": [{"type": "function",
                           "function": {"name": "read", "parameters": {}}}],
                "model": "glm-5.3",
            },
            "model": "glm-5.3",
            "billing": {"promptTokens": 2175, "completionTokens": 756},
        }, {"reply": reply, "error": False}

    def test_record_then_stats_and_export(self):
        sink, result = self._sink()
        self.assertTrue(self.mod.record(sink, result, cwd="/root/x", device="box"))
        self.mod.flush()

        info = self.mod.stats()
        self.assertEqual(info["rows"], 1)
        self.assertEqual(info["trajectories"], 1)
        self.assertEqual(dict(info["by_kind"]), {"critic": 1})

        out = os.path.join(self._tmp.name, "export.jsonl")
        self.assertEqual(self.mod.export_jsonl(out), 1)
        line = json.loads(open(out, encoding="utf-8").read().splitlines()[0])
        # The system prompt is re-attached as the first message and the tool
        # catalog is re-inlined, so the file trains without the store.
        self.assertEqual(line["messages"][0]["role"], "system")
        self.assertEqual(line["messages"][-1]["role"], "assistant")
        self.assertEqual(line["task_kind"], "critic")
        self.assertEqual(line["tools"][0]["function"]["name"], "read")

    def test_identical_call_is_not_stored_twice(self):
        sink, result = self._sink()
        self.mod.record(sink, result)
        self.mod.record(sink, result)
        self.mod.flush()
        self.assertEqual(self.mod.stats()["rows"], 1)

    def test_different_answer_to_same_request_is_a_new_sample(self):
        sink, result = self._sink()
        self.mod.record(sink, result)
        sink2, result2 = self._sink(reply='{"on_track": false}')
        self.mod.record(sink2, result2)
        self.mod.flush()
        self.assertEqual(self.mod.stats()["rows"], 2)

    def test_tool_catalog_is_content_addressed_once(self):
        for kind in ("critic", "intent", "mem_extract"):
            sink, result = self._sink(kind=kind)
            self.mod.record(sink, result)
        self.mod.flush()
        db = self.mod._connect()
        try:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM local_tool_catalogs").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM local_interactions").fetchone()[0], 3)
        finally:
            db.close()

    def test_errors_and_refusals_are_not_training_data(self):
        sink, _ = self._sink()
        # A billing refusal has a reply this CLI wrote, not one a model did.
        self.assertIsNone(self.mod.build_record(
            sink, {"reply": "Payment authorization failed", "error": True}))
        self.assertIsNone(self.mod.build_record(sink, {"reply": "", "error": False}))
        self.assertIsNone(self.mod.build_record({"payload": {}}, {"reply": "x"}))

    def test_tool_calls_are_kept_when_there_is_no_prose(self):
        sink, _ = self._sink()
        row = self.mod.build_record(
            sink, {"reply": "", "tool_calls": [{"name": "read", "args": {}}]})
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["response_json"])["tool_calls"][0]["name"],
                         "read")

    def test_purge_empties_the_store(self):
        sink, result = self._sink()
        self.mod.record(sink, result)
        self.mod.flush()
        self.assertEqual(self.mod.purge(), 1)
        self.assertEqual(self.mod.stats()["rows"], 0)

    def test_prune_drops_oldest_beyond_the_cap(self):
        for i in range(30):
            sink, result = self._sink(reply='{"i": %d}' % i)
            self.mod.record(sink, result)
        self.mod.flush()
        before = self.mod.stats()["rows"]
        self.assertEqual(before, 30)
        self.assertGreater(self.mod.prune(max_bytes=1), 0)
        self.assertLess(self.mod.stats()["rows"], before)

    def test_capture_never_raises(self):
        self.assertFalse(self.mod.record(None, None))
        self.assertFalse(self.mod.record({"payload": "not a dict"}, {}))

    def test_store_is_private(self):
        sink, result = self._sink()
        self.mod.record(sink, result)
        self.mod.flush()
        mode = os.stat(os.path.dirname(self.mod.db_path())).st_mode & 0o777
        self.assertEqual(mode, 0o700)


class TrainingLocalSwitchTest(unittest.TestCase):
    """The local switch must not read, or be read by, the cloud one."""

    def test_local_switch_lives_in_config_and_is_off_by_default(self):
        import laintas_cli
        # Default off: an absent key must not enable capture.
        prev = laintas_cli._local_training_cache
        try:
            laintas_cli._invalidate_local_training_cache()
            laintas_cli.load_config = lambda: {}
            self.assertFalse(laintas_cli.local_training_enabled())
            laintas_cli._invalidate_local_training_cache()
            laintas_cli.load_config = lambda: {"training_local": True}
            self.assertTrue(laintas_cli.local_training_enabled())
        finally:
            laintas_cli._local_training_cache = prev

    def test_training_command_routes_local_without_an_account(self):
        import laintas_cli
        seen = []
        prev = laintas_cli._cmd_training_local
        laintas_cli._cmd_training_local = lambda args: seen.append(list(args))
        try:
            # No session, no backend: a local-only user must still get through.
            laintas_cli._cmd_training(["/training", "local", "status"], {})
        finally:
            laintas_cli._cmd_training_local = prev
        self.assertEqual(seen, [["status"]])


if __name__ == "__main__":
    unittest.main()
