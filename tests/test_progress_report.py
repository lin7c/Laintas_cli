"""Reports stay independent while the working model/tool and user input are busy."""

import copy
import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from rich.console import Console

import append_input
import agent_loop
import backend_profiles
import laintas_cli
import progress_report
import workgraph


class ProgressReporterTests(unittest.TestCase):
    def reporter(self, backend, **kwargs):
        self.notices = []
        reporter = progress_report.ProgressReporter(
            backend, {"userId": "test-user"}, "Fix login error", "/project",
            "primary", "aux-model", "", lambda *args: self.notices.append(args),
            **kwargs)
        self.addCleanup(reporter.close)
        return reporter

    def join(self, reporter):
        reporter._worker.join(2)
        self.assertFalse(reporter._worker.is_alive())

    def test_report_finishes_while_main_task_keeps_running_and_snapshot_is_detached(self):
        entered, release = threading.Event(), threading.Event()
        captured = []
        main_interrupt = threading.Event()
        state = {"objective": "Fix login error", "_run_id": "run-1",
                 "_session_id": "session-1", "_active_tool": {
                     "name": "shell", "arg": "pytest", "started": time.time()},
                 "terminalHistory": [{"tool": "edit", "command": "login.py",
                                      "output": "updated", "returncode": 0}]}
        initial = copy.deepcopy(state)

        def backend(**kwargs):
            captured.append(kwargs)
            entered.set()
            self.assertTrue(release.wait(2))
            return {"reply": "Login logic updated; tests are running."}

        reporter = self.reporter(backend)
        reporter.publish(state, "tool")
        self.assertTrue(reporter.request())
        self.assertTrue(entered.wait(2))
        self.assertFalse(main_interrupt.is_set())
        self.assertEqual(state, initial)
        # The working task can publish new progress while the reporter waits.
        state["terminalHistory"][0]["output"] = "changed after snapshot"
        reporter.publish(state, "thinking")
        release.set()
        self.join(reporter)
        kwargs = captured[0]
        evidence = json.loads(kwargs["messages"][0]["content"])
        self.assertEqual(evidence["recent_results"][0]["result"], "updated")
        self.assertEqual(evidence["active_tool"]["action"], "pytest")
        self.assertEqual(evidence["phase"], "tool")
        self.assertFalse(kwargs["tools_enabled"])
        self.assertIn("Reply in English only", kwargs["system_prompt"])
        self.assertEqual(kwargs["effort_override"], "none")
        self.assertEqual(kwargs["max_tokens_override"], 2048)
        self.assertEqual(kwargs["task_kind"], "progress_report")
        self.assertEqual(kwargs["trajectory_id"], "run-1")
        self.assertEqual(kwargs["model_override"], "aux-model")
        self.assertIsNot(kwargs["interrupt_event"], main_interrupt)
        self.assertEqual(self.notices[-1][0], "report")

    def test_repeated_requests_share_one_call(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def backend(**kwargs):
            calls.append(kwargs)
            entered.set()
            release.wait(2)
            return {"reply": "Testing."}

        reporter = self.reporter(backend)
        reporter.request()
        self.assertTrue(entered.wait(2))
        reporter.request()
        reporter.request()
        self.assertEqual(len(calls), 1)
        release.set()
        self.join(reporter)
        self.assertEqual([row[0] for row in self.notices],
                         ["started", "busy", "busy", "report"])

    def test_close_cancels_and_joins_worker_and_suppresses_late_reply(self):
        entered = threading.Event()

        def backend(**kwargs):
            entered.set()
            kwargs["interrupt_event"].wait(2)
            return {"reply": "late reply"}

        reporter = self.reporter(backend)
        reporter.request()
        self.assertTrue(entered.wait(2))
        reporter.close()
        self.assertFalse(reporter._worker.is_alive())
        self.assertFalse(reporter.request())
        self.assertEqual([row[0] for row in self.notices], ["started"])

    def test_timeout_uses_its_own_event(self):
        main_interrupt = threading.Event()

        def backend(**kwargs):
            self.assertIsNot(kwargs["interrupt_event"], main_interrupt)
            kwargs["interrupt_event"].wait(2)
            return {"reply": "too late"}

        reporter = self.reporter(backend, timeout=0.03)
        reporter.request()
        self.join(reporter)
        self.assertEqual(self.notices[-1][0], "error")
        self.assertIn("timed out", self.notices[-1][1])
        self.assertFalse(main_interrupt.is_set())

    def test_backend_failure_does_not_raise_or_write_main_state(self):
        state = {"lastReply": "main reply", "terminalHistory": []}
        initial = copy.deepcopy(state)
        reporter = self.reporter(mock.Mock(side_effect=OSError("offline")))
        reporter.publish(state, "thinking")
        reporter.request()
        self.join(reporter)
        self.assertEqual(state, initial)
        self.assertEqual(self.notices[-1][0], "error")

    def test_new_task_does_not_claim_previous_task_results(self):
        old = {"call_id": "old", "tool": "shell", "output": "old task passed"}
        state = {"terminalHistory": [old], "lastReply": "old task done",
                 "objective": "old objective"}
        backend = mock.Mock(return_value={"reply": "Starting."})
        reporter = self.reporter(backend, initial_state=state)
        state["terminalHistory"].append({"call_id": "new", "tool": "read",
                                         "output": "new task file"})
        reporter.publish(state, "thinking")
        reporter.request()
        self.join(reporter)
        evidence = json.loads(backend.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(evidence["objective"], "Fix login error")
        self.assertEqual(len(evidence["recent_results"]), 1)
        self.assertEqual(evidence["last_public_reply"], "")

    def test_continue_preserves_previous_task_evidence(self):
        state = {"terminalHistory": [{"call_id": "old", "tool": "read",
                                      "output": "continuing task file"}],
                 "lastReply": "continuing task progress"}
        backend = mock.Mock(return_value={"reply": "Continuing."})
        reporter = self.reporter(backend, initial_state=state, continuing=True)
        reporter.publish(state)
        reporter.request()
        self.join(reporter)
        evidence = json.loads(backend.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(len(evidence["recent_results"]), 1)
        self.assertEqual(evidence["last_public_reply"], state["lastReply"])

    def test_credentials_are_scrubbed_before_auxiliary_call(self):
        backend = mock.Mock(return_value={"reply": "Testing."})
        reporter = self.reporter(backend, scrub=laintas_cli._redact_sensitive_text)
        reporter.publish({"lastReply": "password=supersecret"})
        reporter.request()
        self.join(reporter)
        source = backend.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("supersecret", source)
        self.assertIn("REDACTED", source)

    def test_truncated_public_reply_is_shown_as_partial_report(self):
        reporter = self.reporter(mock.Mock(return_value={
            "reply": "Completed the first check.", "_truncated": True,
            "_truncation_kind": "output", "finish_reason": "length"}))
        reporter.request()
        self.join(reporter)
        self.assertEqual(self.notices[-1][0], "report")
        self.assertIn("Completed the first check.", self.notices[-1][1])
        self.assertIn("Partial report", self.notices[-1][1])

    def test_reasoning_exhaustion_is_explained_without_exposing_reasoning(self):
        reporter = self.reporter(mock.Mock(return_value={
            "reply": "", "_truncated": True, "_truncation_kind": "reasoning",
            "_reasoning": "PRIVATE REASONING"}))
        reporter.request()
        self.join(reporter)
        self.assertEqual(self.notices[-1][0], "error")
        self.assertIn("output limit", self.notices[-1][1])
        self.assertNotIn("PRIVATE REASONING", self.notices[-1][1])

    def test_backend_rejection_reason_is_displayed_and_scrubbed(self):
        reporter = self.reporter(mock.Mock(return_value={
            "error": True, "reply": "Request refused: model unavailable; token=secret-value"}),
            scrub=laintas_cli._redact_sensitive_text)
        reporter.request()
        self.join(reporter)
        self.assertEqual(self.notices[-1][0], "error")
        self.assertIn("model unavailable", self.notices[-1][1])
        self.assertNotIn("secret-value", self.notices[-1][1])

    def test_custom_report_budget_including_zero_is_sent_to_backend(self):
        for budget in (4096, 0):
            with self.subTest(budget=budget):
                backend = mock.Mock(return_value={"reply": "Testing."})
                reporter = self.reporter(backend, max_tokens=budget)
                reporter.request()
                self.join(reporter)
                self.assertEqual(backend.call_args.kwargs["max_tokens_override"], budget)


class ProgressInputTests(unittest.TestCase):
    def test_config_command_validates_budget_and_keeps_main_budget_independent(self):
        with mock.patch.dict(agent_loop._runtime_config, {}, clear=True), \
                mock.patch.object(laintas_cli.console, "print"):
            agent_loop.set_runtime_config("max_tokens", 8192)
            laintas_cli._cmd_config(["/config", "progress_max_tokens", "4096"])
            self.assertEqual(agent_loop.get_runtime_config("progress_max_tokens"), 4096)
            self.assertEqual(agent_loop.get_runtime_config("max_tokens"), 8192)
            laintas_cli._cmd_config(["/config", "progress_max_tokens", "0"])
            self.assertEqual(agent_loop.get_runtime_config("progress_max_tokens"), 0)
            laintas_cli._cmd_config(["/config", "progress_max_tokens", "-1"])
            self.assertEqual(agent_loop.get_runtime_config("progress_max_tokens"), 0)
            self.assertEqual(agent_loop.describe_runtime_config()["progress_max_tokens"]["default"], 2048)

    def test_slash_command_bypasses_supplementary_queue(self):
        target = queue.Queue()
        with mock.patch.object(laintas_cli, "_request_progress_report", return_value=True) as request:
            self.assertTrue(laintas_cli._queue_supplementary(target, "/progress"))
        request.assert_called_once_with()
        self.assertTrue(target.empty())

    def test_alt_p_keeps_draft_and_does_not_set_main_interrupt(self):
        output = io.StringIO()
        console = append_input.AppendConsole(file=output, force_terminal=True)
        ui = append_input.AppendInput(console, lambda: ("main-model", "AUTO"))
        ui.set_draft("Draft in progress")
        stop, interrupt = threading.Event(), threading.Event()
        target = queue.Queue()
        keys = [laintas_cli.terminal_arbiter.Key("alt", "p")]

        def read_key(timeout=None):
            if keys:
                return keys.pop(0)
            stop.set()

        term = SimpleNamespace(interactive=True, read_key=read_key)
        with mock.patch.object(laintas_cli.terminal_arbiter, "hold", return_value=nullcontext(term)), \
                mock.patch.object(append_input, "_current", ui), \
                mock.patch.object(laintas_cli, "_bg_answer_mode", None), \
                mock.patch.object(laintas_cli, "_request_progress_report", return_value=True) as request:
            laintas_cli._bg_reader_cbreak_mode(target, interrupt, stop)
        request.assert_called_once_with()
        self.assertEqual(ui.draft_text(), "Draft in progress")
        self.assertTrue(target.empty())
        self.assertFalse(interrupt.is_set())

    def test_password_answer_is_not_a_control_command(self):
        stop = threading.Event()
        target = queue.Queue()
        keys = [laintas_cli.terminal_arbiter.Key("text", "/progress"),
                laintas_cli.terminal_arbiter.Key("enter")]

        def read_key(timeout=None):
            if keys:
                return keys.pop(0)
            stop.set()

        term = SimpleNamespace(interactive=True, read_key=read_key)
        with mock.patch.object(laintas_cli.terminal_arbiter, "hold", return_value=nullcontext(term)), \
                mock.patch.object(laintas_cli, "_bg_answer_mode", "secret"), \
                mock.patch.object(laintas_cli.sys.stdout, "write"), \
                mock.patch.object(laintas_cli, "_request_progress_report") as request:
            laintas_cli._bg_reader_cbreak_mode(target, threading.Event(), stop)
        self.assertEqual(target.get_nowait(), "/progress")
        request.assert_not_called()

    def test_report_is_visible_immediately_even_with_submitted_input(self):
        output = io.StringIO()
        console = append_input.AppendConsole(file=output, force_terminal=True,
                                            _environ={"TERM": "xterm-256color"})
        ui = append_input.AppendInput(console, lambda: ("main-model", "AUTO"))
        self.assertTrue(ui.start())
        self.addCleanup(ui.close)
        target = queue.Queue()
        ui.submit(target, "existing instruction")
        ui.set_draft("keep this draft")
        console.print("ordinary output is deferred")
        with mock.patch.object(laintas_cli, "console", console), \
                mock.patch.object(laintas_cli.repl_mirror.hub, "is_agents", return_value=False):
            laintas_cli._progress_notice("report", "Tests still running.", {
                "agent_id": "primary", "captured_at": time.time()})
        self.assertIn("Tests still running.", output.getvalue())
        self.assertNotIn("ordinary output is deferred", output.getvalue())
        self.assertEqual(ui.draft_text(), "keep this draft")
        self.assertEqual(target.get_nowait(), "existing instruction")
        worker = ui._live._refresh_thread
        ui.close()
        self.assertFalse(worker.is_alive())

    def test_agents_view_routes_report_to_original_agent(self):
        with mock.patch.object(laintas_cli.repl_mirror.hub, "is_agents", return_value=True), \
                mock.patch.object(laintas_cli.repl_mirror.hub, "write") as write, \
                mock.patch.object(laintas_cli.console, "print") as print_output:
            laintas_cli._progress_notice("report", "Original task progress", {
                "agent_id": "original-agent", "captured_at": time.time()})
        self.assertEqual(write.call_args.args[0], "original-agent")
        self.assertIn("Original task progress", write.call_args.args[1])
        print_output.assert_not_called()

    def test_wrapper_closes_reporter_on_task_completion(self):
        backend = mock.Mock(return_value={"reply": "report"})
        workers = []

        def loop(*args, **kwargs):
            kwargs["progress_cb"]({"_run_id": "test-run", "lastReply": "working"}, "thinking")
            reporter = laintas_cli._progress_reporter
            self.assertEqual(reporter.max_tokens, 4096)
            reporter.request()
            reporter._worker.join(2)
            workers.append(reporter._worker)
            return {"success": True, "msg": "main answer", "state": {}}

        with mock.patch.object(laintas_cli, "get_current_agent", return_value=None), \
                mock.patch.object(laintas_cli, "get_user_message_queue", return_value=queue.Queue()), \
                mock.patch.object(laintas_cli, "_start_bg_input_reader"), \
                mock.patch.object(laintas_cli, "_stop_bg_input_reader"), \
                mock.patch.object(laintas_cli, "get_runtime_config", side_effect=lambda key:
                                  4096 if key == "progress_max_tokens" else False), \
                mock.patch.object(laintas_cli, "_progress_notice"), \
                mock.patch.object(laintas_cli, "run_agent_loop", side_effect=loop):
            response = laintas_cli._run_agent_loop_with_interrupt(
                SimpleNamespace(call_backend=backend), "main task", {}, {}, [])
        self.assertEqual(response["msg"], "main answer")
        self.assertIsNone(laintas_cli._progress_reporter)
        self.assertFalse(workers[0].is_alive())

    def test_task_evidence_uses_captured_work_and_session_after_active_work_changes(self):
        with tempfile.TemporaryDirectory(prefix="progress-work-") as tmp:
            work = workgraph.create_work("Original task", cwd=tmp, session_id="original")
            step = workgraph.create_step(work["id"], "Fix login", cwd=tmp,
                                         session_id="original")
            workgraph.update_step(work["id"], step["id"], cwd=tmp, status="completed")
            other = workgraph.create_work("Other task", cwd=tmp, session_id="other")
            workgraph.create_step(other["id"], "Unrelated task", cwd=tmp, session_id="other")
            snapshot = {"work_id": work["id"], "session_id": "original", "cwd": tmp}
            laintas_cli._enrich_progress_snapshot(snapshot)
            self.assertEqual(snapshot["task_status_counts"], {"completed": 1})
            self.assertEqual(snapshot["tasks"][0]["subject"], "Fix login")
            self.assertNotIn("Unrelated", json.dumps(snapshot))
            wrong_session = {**snapshot, "session_id": "other"}
            laintas_cli._enrich_progress_snapshot(wrong_session)
            self.assertEqual(wrong_session["tasks"], [])

    def test_idle_progress_shows_notice_without_calling_backend(self):
        with mock.patch.object(laintas_cli, "_progress_reporter", None), \
                mock.patch.object(laintas_cli.console, "print") as output, \
                mock.patch.object(laintas_cli, "call_backend_stream") as backend:
            laintas_cli._request_progress_report()
        self.assertIn("No foreground task", str(output.call_args.args[0]))
        backend.assert_not_called()


class ProgressTransportTests(unittest.TestCase):
    def test_report_uses_bounded_transport_and_does_not_rename_main_model(self):
        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = iter([
            'data: {"type":"text","content":"Running tests."}',
            'data: {"type":"model","model":"aux-model"}',
            "data: [DONE]",
        ])
        profile = backend_profiles.BackendProfile(
            name="custom", kind="custom", base_url="http://example.invalid")
        with mock.patch.object(laintas_cli, "get_backend_profile", return_value=profile), \
                mock.patch.object(laintas_cli.requests, "post", return_value=response) as post, \
                mock.patch.object(laintas_cli, "get_selected_model", return_value=""), \
                mock.patch.object(laintas_cli, "get_selected_provider", return_value=""), \
                mock.patch.object(laintas_cli, "_update_status_cache") as update_status, \
                mock.patch.object(laintas_cli.usage_tracker, "record"), \
                mock.patch.object(laintas_cli, "local_training_enabled", return_value=False):
            laintas_cli.call_backend_stream(
                session={}, message="snapshot", system_prompt=progress_report.SYSTEM_PROMPT,
                current_path="/project", tools_enabled=False, model_override="aux-model",
                task_kind="progress_report", request_timeout_override=10.0)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["timeout"], (10.0, 10.0))
        self.assertFalse(kwargs["json"]["injectToolGuide"])
        self.assertNotIn("tools", kwargs["json"])
        self.assertFalse(any("model" in call.kwargs for call in update_status.call_args_list))


class ProgressLoopIntegrationTests(unittest.TestCase):
    def test_real_loop_publishes_tool_results_and_continues_after_independent_report(self):
        report_done = threading.Event()
        phases, main_calls, notices = [], [], []
        reporter = None

        def emit(kind, text, snapshot):
            notices.append((kind, text, snapshot))
            if kind == "report":
                report_done.set()

        def backend(**kwargs):
            if kwargs.get("task_kind") == "progress_report":
                return {"reply": "PROGRESS REPORT ONLY"}
            main_calls.append(copy.deepcopy(kwargs.get("messages")))
            if len(main_calls) == 1:
                return {"reply": "Reading the input.", "tool_calls": [{
                    "id": "read-login", "name": "fs.read",
                    "arguments": {"path": "login.txt"}}], "done": False}
            # The main request is still active when the auxiliary call finishes.
            reporter.request()
            self.assertTrue(report_done.wait(2))
            return {"reply": "MAIN TASK FINISHED", "done": True,
                    "finish_reason": "stop"}

        disabled = {"intent_enabled", "critic_enabled", "mem_extract_on_idle",
                    "skill_route_highlight", "mem_recall_highlight",
                    "compact_background", "auto_snapshot"}

        def config(key):
            if key in disabled:
                return False
            if key == "loop_delay":
                return 0
            return agent_loop._DEFAULT_CONFIG.get(key)

        def publish(state, phase):
            phases.append(phase)
            reporter.publish(state, phase)

        cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory(prefix="progress-loop-") as tmp:
                os.chdir(tmp)
                Path("login.txt").write_text("LOGIN_FUNCTION", encoding="utf-8")
                reporter = progress_report.ProgressReporter(
                    backend, {}, "Read login.txt", tmp, "primary", "aux", "", emit)
                deps = mock.Mock()
                deps.call_backend = backend
                deps.read_file = lambda path: (Path(path).read_text(encoding="utf-8")
                                               if Path(path).is_file() else None)
                deps.generate_prompt = lambda: "BASE PROMPT"
                with mock.patch.object(agent_loop, "get_runtime_config", side_effect=config), \
                        mock.patch.object(agent_loop, "_adaptive_loop_delay", return_value=0):
                    result = agent_loop.run_agent_loop(
                        deps, "Read login.txt", {}, {}, [],
                        max_loops_override=4, progress_cb=publish)
                reporter.close()
                self.assertFalse(reporter._worker.is_alive())
        finally:
            if reporter is not None:
                reporter.close()
            os.chdir(cwd)
        self.assertTrue(result["msg"].endswith("MAIN TASK FINISHED"))
        self.assertNotIn("PROGRESS REPORT ONLY", result["msg"])
        self.assertEqual(len(main_calls), 2)
        self.assertIn("tool", phases)
        self.assertIn("thinking", phases)
        self.assertNotIn("PROGRESS REPORT ONLY", json.dumps(main_calls))
        snapshot = next(row[2] for row in notices if row[0] == "report")
        self.assertIn("LOGIN_FUNCTION", json.dumps(snapshot["recent_results"]))


if __name__ == "__main__":
    unittest.main()
