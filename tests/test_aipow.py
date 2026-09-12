import json
import os
import re
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import ai_pow
import ai_pow_report
import aipow_bridge
import event_log
import usage_tracker


class NativePowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="laintas-aipow-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ai_pow.git(self.root, "init", "-q")
        patcher = mock.patch.object(aipow_bridge, "_local", threading.local())
        patcher.start()
        self.addCleanup(patcher.stop)
        aipow_bridge.bind(self.root)
        self.rec = ai_pow.Recorder(self.root)

    def test_disabled_does_not_create_storage(self):
        aipow_bridge.message("human.message", "hello")
        aipow_bridge.emit("tool.call", {"name": "shell"})
        self.assertFalse(self.rec.directory.exists())

    def test_native_journal_usage_and_privacy(self):
        self.rec.init()
        aipow_bridge.message("human.message", "SECRET PROMPT")
        aipow_bridge.message("assistant.visible", "visible", channel="commentary")
        with mock.patch.object(event_log, "_log_path", side_effect=OSError("disabled legacy log")):
            event_log.append("tool_call", name="shell", call_id="x", arguments={"secret": "DO NOT STORE"})
            event_log.append("tool_result", name="shell", call_id="x", ok=True, output="SECRET OUTPUT")
        with mock.patch.object(usage_tracker, "_usage_dir", return_value=self.root), mock.patch.object(usage_tracker, "_SESSION", []):
            usage_tracker.record(model="example", prompt_tokens=100, completion_tokens=20,
                                 official=True, cost_cents=2, cached_prompt_tokens=10)
        totals = self.rec.status()["active"]
        self.assertEqual(totals["human"]["messages"], 1)
        self.assertEqual(totals["event_counts"]["tool.call"], 1)
        self.assertEqual(totals["models"]["example/provider_reported"]["input_tokens"], 100)
        self.assertEqual(totals["visible_ai"]["events"], 1)
        data = self.rec.database.read_bytes()
        self.assertNotIn(b"SECRET PROMPT", data)
        self.assertNotIn(b"DO NOT STORE", data)
        self.assertNotIn(b"SECRET OUTPUT", data)

    def test_failure_is_visible_but_does_not_break_agent(self):
        self.rec.init()
        with mock.patch.object(ai_pow.Recorder, "record", side_effect=OSError("full")):
            aipow_bridge.emit("tool.call", {"name": "shell"})
        self.assertTrue(self.rec.status()["recording_error"])

    def test_thread_binding_isolated(self):
        self.rec.init()
        errors = []
        def worker():
            try:
                with tempfile.TemporaryDirectory(prefix="laintas-aipow-child-") as other:
                    ai_pow.git(other, "init", "-q")
                    child = ai_pow.Recorder(other)
                    child.init()
                    aipow_bridge.bind(other)
                    aipow_bridge.emit("tool.call", {"name": "child"})
                    self.assertEqual(child.status()["active"]["tools_used"], ["child"])
            except Exception as exc:
                errors.append(exc)
        worker_thread = threading.Thread(target=worker)
        worker_thread.start()
        worker_thread.join()
        self.assertFalse(errors, errors)
        aipow_bridge.emit("tool.call", {"name": "parent"})
        self.assertEqual(self.rec.status()["active"]["tools_used"], ["parent"])

    def test_vendor_matches_independent_source_when_available(self):
        for name in ("ai_pow.py", "ai_pow_scoring.py", "ai_pow_report.py"):
            source = Path(__file__).resolve().parents[2] / "ai-pow" / name
            if source.exists():
                self.assertEqual(source.read_bytes(), Path(ai_pow.__file__).with_name(name).read_bytes())

    def test_native_recording_seals_two_iterations_and_renders_both_pages(self):
        ai_pow.git(self.root, "config", "user.name", "AI-PoW Test")
        ai_pow.git(self.root, "config", "user.email", "test@example.invalid")
        self.rec.init()
        proofs = []
        for value in (1, 2):
            aipow_bridge.sample()
            aipow_bridge.message("human.message", "Update the example")
            (self.root / "example.py").write_text(f"value = {value}\n")
            aipow_bridge.sample()
            aipow_bridge.message("assistant.visible", "Updated the example", channel="final")
            ai_pow.git(self.root, "add", "example.py")
            ai_pow.git(self.root, "commit", "-qm", f"Update {value}")
            proofs.append(self.rec.seal())
        before = self.rec.index_data()["iteration"]["value"]
        self.assertAlmostEqual(float(before), sum(float(p["score"]["value"]) for p in proofs))
        report = self.rec.html_report()
        overview = self.rec.html_index()
        payload_pattern = r'<script id="report-data" type="application/json">(.*?)</script>'
        report_data = json.loads(re.search(payload_pattern, report.read_text()).group(1))
        overview_data = json.loads(re.search(payload_pattern, overview.read_text()).group(1))
        self.assertEqual(report.read_text(), ai_pow_report.render(report_data))
        self.assertEqual(overview.read_text(), ai_pow_report.render_index(overview_data))
        self.assertEqual(report_data["current"]["score"], proofs[-1]["score"])
        self.assertEqual(overview_data["iteration"]["value"], before)
        self.assertIn("scoreHero('Iteration score'", report.read_text())
        self.assertIn("scoreHero('Total score'", overview.read_text())
        self.assertNotRegex(report.read_text() + overview.read_text(), r"[\u4e00-\u9fff]")
        self.rec.rebuild_iterations()
        self.assertEqual(self.rec.index_data()["iteration"]["value"], before)
