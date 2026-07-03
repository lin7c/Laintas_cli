import os
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import laintas_cli
import agent_loop
import plan_mode
import task_manager
import workgraph
import workflow_engine
from rich.console import Console
from rich.markdown import Markdown


class _Chdir:
    def __init__(self, path):
        self.path = str(path)
        self.old = None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *exc):
        os.chdir(self.old)


def _plan(task="Build a safe feature", suffix=""):
    return f"""# Plan: {task}

**Status:** drafting
**Approved:** no

## Context
The project needs a transactional implementation with compatibility and recovery.

## Exploration
Inspect entry points, state ownership, persistence, and existing tests.

## Architecture
Use one project database and immutable revisions. Keep compatibility adapters.

## Implementation Steps
1. Create the transactional storage layer{suffix}
2. Connect command and agent-tool adapters
3. Run migration and regression verification

## Risks & Edge Cases
Concurrent writers, stale approvals, interrupted migration, and dependency cycles.

## Test Plan
Run unit, integration, stale-revision, dependency, and recovery tests.
"""


class WorkGraphTests(unittest.TestCase):
    def test_plan_submit_is_not_exposed_outside_plan_mode(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            plan_mode.exit_plan_mode(approve=False)
            allowed = agent_loop._allowed_tool_names_for_state({})
            self.assertNotIn("plan.submit", allowed)
            self.assertNotIn("plan.update", allowed)

    def test_fresh_session_resets_persisted_plan_and_active_work(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp), \
                mock.patch.object(plan_mode, "PLANS_DIR", Path(tmp) / "plans"), \
                mock.patch.object(
                    plan_mode, "_STATE_PATH", Path(tmp) / "plans" / "_state.json"):
            plan_mode._loaded_cwd = None
            plan_mode.arm_plan_mode()
            self.assertTrue(plan_mode.is_plan_mode())

            laintas_cli._reset_fresh_session_context(tmp)

            self.assertFalse(plan_mode.is_plan_mode())
            self.assertIsNone(workgraph.get_active_work(cwd=tmp))

    def test_revision_sha_binding_and_step_projection(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            work = workgraph.create_work("Build it")
            first = workgraph.add_revision(work["id"], _plan())
            review = workgraph.submit_plan(work["id"])
            self.assertEqual(len(review["steps"]), 3)

            # Any revision after review invalidates the old approval token.
            workgraph.add_revision(work["id"], _plan(suffix=" safely"))
            with self.assertRaises(workgraph.WorkGraphConflict):
                workgraph.approve_plan(
                    work["id"], first["revision"], first["content_sha"])

            review = workgraph.submit_plan(work["id"])
            revision = review["revision"]
            self.assertEqual(len(review["steps"]), 3)
            workgraph.approve_plan(
                work["id"], revision["revision"], revision["content_sha"])
            active = workgraph.begin_execution(
                work["id"], revision["revision"], revision["content_sha"])
            self.assertEqual(active["status"], "EXECUTING")
            self.assertIn(revision["content_sha"], workgraph.approved_plan_context())

    def test_dag_validation_and_progress_normalization(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            work = workgraph.ensure_active_work("Tasks")
            one = workgraph.create_step(work["id"], "one")
            two = workgraph.create_step(work["id"], "two")
            workgraph.add_dependency(work["id"], two["id"], one["id"])
            with self.assertRaises(workgraph.WorkGraphConflict):
                workgraph.update_step(work["id"], two["id"], status="completed")
            with self.assertRaises(workgraph.WorkGraphError):
                workgraph.add_dependency(work["id"], one["id"], two["id"])
            with self.assertRaises(workgraph.WorkGraphError):
                workgraph.add_dependency(work["id"], one["id"], "999")
            completed = workgraph.update_step(work["id"], one["id"], progress=100)
            self.assertEqual((completed["status"], completed["progress"]), ("completed", 100))
            reopened = workgraph.update_step(work["id"], one["id"], status="in_progress")
            self.assertEqual(reopened["progress"], 0)

    def test_resume_session_ids_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            work = workgraph.ensure_active_work("Resume")
            count = workgraph.import_session_steps(
                work["id"], [
                    {"id": "s1", "subject": "first"},
                    {"id": "s1", "subject": "second"},
                ])
            ids = [step["id"] for step in workgraph.list_steps(work["id"])]
            self.assertEqual(count, 2)
            self.assertEqual(len(ids), len(set(ids)))

    def test_legacy_tasks_import_once_and_plan_capabilities_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            legacy = Path(tmp) / ".laintas" / "tasks.json"
            legacy.parent.mkdir()
            legacy.write_text(
                '[{"id":"1","subject":"legacy","status":"pending",'
                '"blockedBy":[],"blocks":[],"progress":0}]', encoding="utf-8")
            first = task_manager.list_tasks(cwd=tmp)
            second = task_manager.list_tasks(cwd=tmp)
            self.assertEqual([item["subject"] for item in first], ["legacy"])
            self.assertEqual(len(second), 1)
            with mock.patch.object(plan_mode, "_plan_mode", True), \
                    mock.patch.object(plan_mode, "_loaded_cwd", str(Path.cwd().resolve())):
                self.assertTrue(plan_mode.is_tool_allowed("task.complete"))
                self.assertFalse(plan_mode.is_tool_allowed("skill.load"))
                self.assertFalse(plan_mode.is_tool_allowed("task.create"))

    def test_new_session_detaches_tasks_without_reimporting_legacy_archive(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            legacy = Path(tmp) / ".laintas" / "tasks.json"
            legacy.parent.mkdir()
            legacy.write_text(
                '[{"id":"1","subject":"unfinished legacy task",'
                '"status":"in_progress","blockedBy":[],"blocks":[],'
                '"progress":20}]', encoding="utf-8")

            self.assertEqual(len(task_manager.list_tasks(cwd=tmp)), 1)
            original_work = workgraph.get_active_work(cwd=tmp)

            task_manager.detach_active_tasks(cwd=tmp)

            self.assertIsNone(workgraph.get_active_work(cwd=tmp))
            self.assertEqual(task_manager.list_tasks(cwd=tmp), [])
            self.assertEqual(len(workgraph.list_work(cwd=tmp)), 1)
            self.assertIsNotNone(workgraph.get_work(
                original_work["id"], cwd=tmp))

    def test_workflow_phase_uses_same_active_work(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            workflow_engine._active_workflow = None
            workflow_engine._active_workflow_cwd = None
            workflow_engine.start_workflow("bug-fix", "repair it")
            work = workgraph.get_active_work()
            self.assertEqual(work["workflow_template"], "bug-fix")
            self.assertEqual(work["workflow_phase"], "reproduce")
            self.assertFalse(workflow_engine.is_tool_allowed_in_workflow("fs.write"))
            self.assertTrue(workflow_engine.is_tool_allowed_in_workflow("task.complete"))

    def test_plan_confirmation_fails_closed_and_binds_revision(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            root = Path(tmp)
            plans = root / "plans"
            state = plans / "_state.json"
            with mock.patch.object(plan_mode, "PLANS_DIR", plans), \
                    mock.patch.object(plan_mode, "_STATE_PATH", state):
                plan_mode._current_plan = None
                plan_mode._plan_mode = False
                plan_mode._loaded_cwd = None
                plan_mode.enter_plan_mode("Build a safe feature")
                self.assertTrue(plan_mode.update_plan(_plan()))
                self.assertIsNotNone(plan_mode.submit_current_plan())

                with mock.patch.object(
                        laintas_cli, "_blocking_approval_prompt", return_value="no"):
                    self.assertIsNone(laintas_cli._review_and_approve_current_plan())
                self.assertEqual(workgraph.get_active_work()["status"], "REVIEW_PENDING")

                with mock.patch.object(
                        laintas_cli, "_blocking_approval_prompt", return_value="yes"):
                    approved = laintas_cli._review_and_approve_current_plan()
                self.assertIsNotNone(approved)
                self.assertEqual(workgraph.get_active_work()["status"], "EXECUTING")
                content = Path(approved["file"]).read_text(encoding="utf-8")
                self.assertIn("**Approved:** yes", content)

    def test_plan_submit_tool_is_the_readiness_signal(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            root = Path(tmp)
            plans = root / "plans"
            with mock.patch.object(plan_mode, "PLANS_DIR", plans), \
                    mock.patch.object(plan_mode, "_STATE_PATH", plans / "_state.json"):
                plan_mode._current_plan = None
                plan_mode._plan_mode = False
                plan_mode._loaded_cwd = None
                plan_mode.enter_plan_mode("Build a safe feature")
                plan_mode.update_plan(_plan())

                deps = agent_loop.LoopDeps(
                    read_file=lambda path: None,
                    append_file=lambda *args: None,
                    write_file=lambda *args: None,
                    strip_ansi=lambda value: value,
                    generate_prompt=lambda: "{{planMode}}\n{{tools}}\n{{promptOpt}}",
                    call_backend=lambda **kwargs: {
                        "reply": "",
                        "tool_calls": [{"name": "plan.submit", "arguments": {}}],
                        "done": False,
                        "finish_reason": "tool_calls",
                    },
                    SubTerminalSession=mock.Mock,
                    display_command_output=lambda *args, **kwargs: None,
                    display_sub_terminal_preview=lambda *args, **kwargs: None,
                    display_file_diff=lambda *args, **kwargs: None,
                    console=Console(file=io.StringIO(), force_terminal=False),
                    Markdown=Markdown,
                )
                result = agent_loop.run_agent_loop(
                    deps, "Build a safe feature", {}, {},
                    [{"role": "user", "content": "Build a safe feature"}],
                    max_loops_override=2)
                self.assertEqual(result["completion_source"], "plan_submitted")
                self.assertEqual(workgraph.get_active_work()["status"], "REVIEW_PENDING")


if __name__ == "__main__":
    unittest.main()
