"""E3+D2 (bughunt): crash recovery and the in-flight frontier checkpoint.

E3: resume only accepted "paused", so a run whose process died mid-flight
    stayed "running" forever — unresumable, only cancellable. The fix lets
    resume recover a "running" run once its recorded owner pid is dead
    (a live owner is refused, so a real concurrent run is never doubled).

D2: nodes were popped from the ready queue before the frontier launched,
    but the queue was only checkpointed after the frontier completed. A
    crash in between dropped the in-flight nodes from the resume state —
    the graph then "completed" without ever running them. The fix
    persists ready + in-flight batch (frontier_inflight) before launch.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import hwg_runner
import workflow_state


class _Chdir:
    def __init__(self, path):
        self.path, self.old = path, None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *exc):
        os.chdir(self.old)


_GRAPH = (
    '(a.hwo)#a# [out(verdict: string)]\n(b.hwo)#b#\n(c.hwo)#c#\n'
    '#a# -> #b#\n#b# -> #c#\n'
)


def _ok(path, **kw):
    return {"ok": True, "msg": "PASS", "outputs": {"verdict": "PASS"}}


class InFlightCheckpointTests(unittest.TestCase):
    """D2: the persisted queue must contain the in-flight node."""

    def test_inflight_node_is_in_persisted_ready_during_execution(self):
        seen = {}

        def fake_run(path, **kw):
            nid = Path(path).stem
            if nid == "b":
                # Mid-flight: b has been popped from the in-memory queue.
                # What survives a crash here is what is on disk right now.
                runs = workflow_state.list_runs()
                run = workflow_state.load_run(runs[0]["runId"])
                seen["ready"] = list(run.get("ready") or [])
                seen["labels"] = [c["label"] for c in run.get("checkpoints", [])]
            return _ok(path, **kw)

        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(_GRAPH, encoding="utf-8")
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file",
                                   side_effect=fake_run):
                result = hwg_runner.run_hwg_file(
                    "flow.hwg", deps=object(), session={})
        self.assertTrue(result["ok"], result["msg"])
        self.assertIn("b", seen["ready"],
                      "in-flight node missing from persisted ready — "
                      "a crash here would silently skip it")
        self.assertIn("frontier_inflight", seen["labels"])


class CrashResumeTests(unittest.TestCase):
    """E3: a crashed 'running' run recovers once its owner is dead."""

    def _crashed_run(self, tmp, owner_pid):
        Path("flow.hwg").write_text(_GRAPH, encoding="utf-8")
        with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file",
                               side_effect=_ok):
            result = hwg_runner.run_hwg_file(
                "flow.hwg", deps=object(), session={})
        run_id = result["runId"]
        run = workflow_state.load_run(run_id)
        run["status"] = "running"          # what a crash leaves behind
        run["owner"] = {"pid": owner_pid, "started": time.time()}
        run["ready"] = ["b"]               # frontier_inflight had persisted b
        workflow_state.save_run(run)
        return run_id

    def test_dead_owner_running_run_resumes(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            run_id = self._crashed_run(tmp, owner_pid=999999)  # no such pid
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file",
                                   side_effect=_ok):
                resumed = hwg_runner.resume_hwg_run(
                    run_id, deps=object(), session={})
            self.assertTrue(resumed["ok"], resumed["msg"])
            # Runs are stored per-project (cwd); assert before leaving the
            # temp project, or load_run() looks in the wrong directory.
            stored = workflow_state.load_run(run_id)
            self.assertEqual(stored["status"], "completed")

    def test_live_owner_running_run_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            run_id = self._crashed_run(tmp, owner_pid=os.getpid())  # us: alive
            resumed = hwg_runner.resume_hwg_run(
                run_id, deps=object(), session={})
        self.assertFalse(resumed["ok"])
        self.assertIn("still running", resumed["msg"])

    def test_completed_run_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(_GRAPH, encoding="utf-8")
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file",
                                   side_effect=_ok):
                result = hwg_runner.run_hwg_file(
                    "flow.hwg", deps=object(), session={})
            resumed = hwg_runner.resume_hwg_run(
                result["runId"], deps=object(), session={})
        self.assertFalse(resumed["ok"])
        self.assertIn("not paused", resumed["msg"])


if __name__ == "__main__":
    unittest.main()
