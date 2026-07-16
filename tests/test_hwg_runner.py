import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hwg_runner
import workflow_state
from hwg_adapter import parse as parse_hwg


class _Chdir:
    def __init__(self, path):
        self.path = path
        self.old = None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *exc):
        os.chdir(self.old)


class HwgRunnerTests(unittest.TestCase):
    def test_parser_accepts_node_policy(self):
        ast = parse_hwg('(a.hwo)#a# { retry: 2, timeout: "10m", cache: "1h" }')
        self.assertEqual(ast[0]["policy"], {
            "retry": 2,
            "timeout": "10m",
            "cache": "1h",
        })

    def test_compile_summarizes_policy(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                '(a.hwo)#a# { retry: 1, timeout: "5s" }\n',
                encoding="utf-8",
            )

            result = hwg_runner.compile_hwg_file("flow.hwg")

            self.assertTrue(result["ok"], result["msg"])
            self.assertIn("retry", result["msg"])
            self.assertIn("timeout", result["msg"])

    def test_run_pauses_at_manual_node_and_resume_completes(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "!(review.hwo)#review#\n(report.hwo)#report#\n"
                "#review# -> { on: PASS } #report#\n",
                encoding="utf-8",
            )

            first = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})
            self.assertTrue(first["paused"])
            run_id = first["runId"]
            graph_tasks = [
                task for task in __import__("task_manager").list_tasks(cwd=tmp)
                if task.get("metadata", {}).get("scopeType") == "hwg-run"
            ]
            self.assertTrue(graph_tasks)
            self.assertTrue(all(not task["session_only"] for task in graph_tasks))

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", return_value={
                "ok": True,
                "msg": '{"verdict":"PASS","report":"done"}',
                "outputs": {"verdict": "PASS", "report": "done"},
            }):
                resumed = hwg_runner.resume_hwg_run(run_id, deps=object(), session={})

            self.assertTrue(resumed["ok"], resumed["msg"])
            stored = workflow_state.load_run(run_id)
            self.assertEqual(stored["status"], "completed")
            self.assertEqual(stored["history"], ["review", "report"])

    def test_retry_then_success_and_structured_verdict(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                '(test.hwo)#test# [out(verdict: string)] { retry: 1 }\n'
                '(done.hwo)#done#\n'
                '#test# -> { on: verdict == "PASS" } #done#\n',
                encoding="utf-8",
            )
            calls = []

            def fake_run(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return {"ok": False, "msg": "temporary failure", "outputs": {}}
                return {"ok": True, "msg": '{"verdict":"PASS"}', "outputs": {"verdict": "PASS"}}

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})

            self.assertTrue(result["ok"], result["msg"])
            self.assertEqual(len(calls), 3)  # test twice, done once
            run_id = result["runId"]
            self.assertEqual(workflow_state.load_run(run_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
