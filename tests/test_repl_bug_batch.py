"""Regressions for a batch of REPL command bugs found by hands-on testing."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import handoff
import hwg_runner
import hwo_runner
import laintas_cli
import workflow_engine
import workflow_state


class PrivateArgContractTests(unittest.TestCase):
    def test_documented_forms_pass_the_contract(self):
        for args in (
            [],
            ["list"],
            ["set", "/tmp/x", "hidden"],
            ["set", "/tmp/x", "hidden", "no-read", "no-write"],
            ["remove", "/tmp/x"],
        ):
            laintas_cli._validate_slash_args("/private", args)

    def test_extra_words_are_still_refused(self):
        for args in (["list", "extra"], ["remove", "/tmp/x", "extra"]):
            with self.assertRaises(laintas_cli.SlashCommandUsageError):
                laintas_cli._validate_slash_args("/private", args)


class HandoffLockTests(unittest.TestCase):
    def test_unknown_id_leaves_no_lock_file(self):
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaises(handoff.HandoffError):
                handoff.append("no-such-id", "close", "tester", cwd=cwd)
            directory = handoff.handoff_dir(cwd)
            leftovers = list(directory.glob(".*.lock")) if directory.exists() else []
            self.assertEqual(leftovers, [])


class WorkflowEndTests(unittest.TestCase):
    def _instance(self, current_phase, completed):
        phases = workflow_engine._bug_fix_phases()
        return workflow_engine.WorkflowInstance(
            name="bug-fix", description="d", phases=phases,
            current_phase=current_phase, completed=completed)

    def test_ended_early_reports_the_phase_it_stopped_in(self):
        total = len(workflow_engine._bug_fix_phases())
        wf = self._instance(1, True)
        self.assertTrue(wf.ended_early)
        self.assertIn(f"Phase 2/{total} (ended early)", wf.progress_str)

    def test_finished_workflow_reports_every_phase(self):
        total = len(workflow_engine._bug_fix_phases())
        wf = self._instance(total, True)
        self.assertFalse(wf.ended_early)
        self.assertIn(f"Phase {total}/{total}:", wf.progress_str)

    def test_running_workflow_is_unchanged(self):
        total = len(workflow_engine._bug_fix_phases())
        wf = self._instance(0, False)
        self.assertFalse(wf.ended_early)
        self.assertIn(f"Phase 1/{total}:", wf.progress_str)


class HwgNodePathTests(unittest.TestCase):
    def test_binding_resolves_beside_the_graph(self):
        with tempfile.TemporaryDirectory() as graph_dir, \
                tempfile.TemporaryDirectory() as elsewhere:
            Path(graph_dir, "step1.hwo").write_text("x", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(elsewhere)
            try:
                resolved = hwg_runner._resolve_node_file(
                    os.path.join(graph_dir, "min.hwg"), "step1.hwo")
            finally:
                os.chdir(previous)
            self.assertEqual(os.path.realpath(resolved),
                             os.path.realpath(os.path.join(graph_dir, "step1.hwo")))

    def test_cwd_relative_binding_still_works(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "graphs"))
            Path(root, "step1.hwo").write_text("x", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(root)
            try:
                resolved = hwg_runner._resolve_node_file("graphs/min.hwg", "step1.hwo")
            finally:
                os.chdir(previous)
            self.assertEqual(resolved, "step1.hwo")

    def test_absolute_binding_is_untouched(self):
        self.assertEqual(
            hwg_runner._resolve_node_file("/a/b.hwg", "/c/d.hwo"), "/c/d.hwo")


class HwoFinalStatusTests(unittest.TestCase):
    def test_completed_run_is_stored_as_completed(self):
        with tempfile.TemporaryDirectory() as cwd:
            source = Path(cwd, "min.hwo")
            source.write_text("x", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                def fake_sequence(steps, ctx):
                    # A step reports progress, which replaces ctx.run_state.
                    hwo_runner._emit(ctx, "step_started", {})
                    return {"ok": True, "msg": "", "outputs": {}}

                with mock.patch.object(hwo_runner, "parse_ast", return_value=[]), \
                        mock.patch.object(hwo_runner, "validate_ast", return_value=[]), \
                        mock.patch.object(hwo_runner, "run_sequence", fake_sequence):
                    result = hwo_runner.run_hwo_file(
                        str(source), deps=None, session={},
                        abort_event=threading.Event())
                self.assertTrue(result["ok"])
                stored = workflow_state.load_run(result["runId"])
            finally:
                os.chdir(previous)
            self.assertEqual(stored["status"], "completed")
            self.assertEqual(
                [e["type"] for e in stored["events"]],
                ["workflow_started", "step_started", "workflow_completed"])


if __name__ == "__main__":
    unittest.main()
