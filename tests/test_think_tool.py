"""`think`: a step that neither acts nor ends the turn, and a question answered
from the session's own full record instead of from memory."""
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console
from rich.markdown import Markdown

import agent_loop
import context_router
import model_route
import step_record
import task_manager
import tools
import transcript_view

from test_session_runtime import _chdir


class _RecordDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        target = Path(self._tmp.name) / "records"
        patcher = mock.patch.object(step_record, "record_dir", lambda: target)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = target


class StepRecordTests(_RecordDir):
    def test_appends_full_text_privately(self):
        long_output = "line\n" * 50000
        self.assertTrue(step_record.append_step(
            "s1", "primary", 3, reasoning="because the config is stale",
            reply="checking", calls=[{"name": "shell.exec",
                                      "arguments": {"command": "cat app.cfg"},
                                      "output": long_output}]))
        path = step_record.record_path("s1", "primary")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        entry = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(entry["step"], 3)
        self.assertEqual(entry["calls"][0]["output"], long_output)  # under the cap: whole
        self.assertEqual(json.loads(entry["calls"][0]["arguments"]), {"command": "cat app.cfg"})

    def test_empty_step_writes_nothing(self):
        self.assertFalse(step_record.append_step("s1", "primary", 1))
        self.assertFalse(step_record.record_path("s1", "primary").exists())
        self.assertFalse(step_record.append_step("", "primary", 1, reply="x"))

    def test_oversized_field_is_capped_with_a_marker(self):
        step_record.append_step("s1", "primary", 1, reasoning="x" * (step_record.FIELD_CAP + 10))
        entry = json.loads(step_record.record_path("s1", "primary").read_text(encoding="utf-8"))
        self.assertIn("[record truncated: 10 more characters]", entry["reasoning"])

    def test_agents_and_sessions_are_separate(self):
        step_record.append_step("s1", "primary", 1, reasoning="alpha migration plan")
        step_record.append_step("s1", "child-1", 1, reasoning="beta migration plan")
        hits = step_record.search("s1", "child-1", "migration plan", 4000)
        self.assertEqual([h["excerpt"] for h in hits], ["beta migration plan"])
        self.assertEqual(step_record.search("s2", "primary", "migration plan", 4000), [])

    def test_search_needs_most_of_the_question(self):
        step_record.append_step("s1", "primary", 1, calls=[
            {"name": "fs.read", "arguments": {"path": "a.py"}, "output": "error in parser"}])
        step_record.append_step("s1", "primary", 2, calls=[
            {"name": "shell.exec", "arguments": {"command": "pytest"},
             "output": "FAILED test_login: token expired at refresh"}])
        hits = step_record.search("s1", "primary", "why did the login token refresh fail", 4000)
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]["step"], hits[0]["label"]), (2, "shell.exec output"))
        # one common word is not a match
        self.assertEqual(step_record.search("s1", "primary", "error in the build pipeline", 4000), [])

    def test_search_cjk_and_paths(self):
        step_record.append_step("s1", "primary", 4, reasoning="决定把超时改成三十秒，因为网关冷启动很慢")
        step_record.append_step("s1", "primary", 5, calls=[
            {"name": "fs.edit", "arguments": {"path": "agent_loop.py"}, "output": "ok"}])
        self.assertEqual(step_record.search("s1", "primary", "为什么改超时", 4000)[0]["step"], 4)
        self.assertEqual(step_record.search("s1", "primary", "agent_loop edit", 4000)[0]["step"], 5)

    def test_excerpt_is_centred_on_the_match_within_budget(self):
        text = "a" * 20000 + " the cache key collides " + "b" * 20000
        step_record.append_step("s1", "primary", 1, reasoning=text)
        hit = step_record.search("s1", "primary", "cache key collides", 1200)[0]
        self.assertIn("the cache key collides", hit["excerpt"])
        self.assertLess(len(hit["excerpt"]), 1300)

    def test_old_records_are_collected(self):
        old = step_record.record_path("old", "primary")
        old.parent.mkdir(parents=True)
        old.write_text("{}\n", encoding="utf-8")
        past = time.time() - step_record.MAX_AGE_SECONDS - 10
        os.utime(old, (past, past))
        with mock.patch.object(step_record, "_gc_done", False):
            step_record.append_step("new", "primary", 1, reply="x")
        self.assertFalse(old.exists())
        self.assertTrue(step_record.record_path("new", "primary").exists())


class ThinkToolTests(_RecordDir):
    def _think(self, **params):
        ctx = tools.ToolCtx(session_id="s1", agent_id="primary", state={})
        return tools.get_registry().invoke("think", params, ctx)

    def test_thought_alone_is_acknowledged(self):
        result = self._think(thought="the test failed because of the fixture")
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], "Noted.")

    def test_question_is_answered_verbatim_from_the_record(self):
        step_record.append_step("s1", "primary", 2, reasoning="use port 8443 since 443 is taken by nginx")
        result = self._think(thought="which port did I pick?",
                             question="port nginx taken")
        self.assertIn("[step 2 · reasoning]", result["result"])
        self.assertIn("use port 8443 since 443 is taken by nginx", result["result"])
        self.assertEqual(result["count"], 1)

    def test_unanswerable_question_says_so(self):
        result = self._think(thought="?", question="database password rotation schedule")
        self.assertEqual(result["count"], 0)
        self.assertIn("Do not reconstruct it from memory", result["result"])

    def test_empty_call_is_an_error(self):
        self.assertFalse(self._think(thought="  ")["ok"])

    def test_schema_and_routing(self):
        tool = {t.name: t for t in tools.get_registry().list()}["think"]
        self.assertEqual(tool.schema["required"], ["thought"])
        self.assertIn("question", tool.schema["properties"])
        caps = tools.infer_capabilities("think")
        self.assertFalse(caps & {"fs.read", "fs.write", "process.exec", "network"})
        self.assertIn("think", context_router.CORE_TOOLS)


class ThinkAuthorizationTests(unittest.TestCase):
    def setUp(self):
        agent_loop.reset_runtime_config()
        self.addCleanup(agent_loop.reset_runtime_config)

    def _authorize(self, state, allowed):
        return agent_loop._authorize_tool_call(
            "think", "x", state, agent_id=None, allowed_tool_names=allowed,
            is_shell_flavored=False, fail_ledger={}, fail_ledger_err={},
            repeat_block_limit=3)

    def test_survives_built_in_restrictions(self):
        for state in ({"_prompt_lab_branch": True}, {"_evolution_lab_branch": True},
                      {"_role_name": "explorer"}):
            names = agent_loop._allowed_tool_names_for_state(state)
            self.assertIn("think", names, state)
            self.assertIsNone(self._authorize(state, names), state)
        with mock.patch.object(agent_loop.plan_mode, "is_plan_mode", return_value=True), \
                mock.patch.object(agent_loop.plan_mode, "is_tool_allowed",
                                  side_effect=lambda name: name != "think"):
            names = agent_loop._allowed_tool_names_for_state({})
            self.assertIn("think", names)
            self.assertIsNone(self._authorize({}, names))

    def test_written_lists_are_exact(self):
        scoped = {"_tool_allowlist": ["fs.read"]}
        self.assertNotIn("think", agent_loop._allowed_tool_names_for_state(scoped))
        self.assertIn("think", agent_loop._allowed_tool_names_for_state(
            {"_tool_allowlist": ["fs.read", "think"]}))
        with mock.patch.object(agent_loop, "_startup_tool_allow", ["fs.*"]):
            self.assertNotIn("think", agent_loop._allowed_tool_names_for_state({}))

    def test_survives_read_only_mode(self):
        with mock.patch.object(agent_loop.mode_manager, "is_tool_allowed",
                               side_effect=lambda name: name != "think"):
            names = agent_loop._allowed_tool_names_for_state({})
        self.assertIn("think", names)

    def test_explicit_denial_still_wins(self):
        with mock.patch.object(agent_loop, "_startup_tool_deny", ["think"]):
            names = agent_loop._allowed_tool_names_for_state({})
            self.assertNotIn("think", names)
            self.assertIn("BLOCKED", self._authorize({}, names)["error"])

    def test_batches_with_reads_and_never_ledger_blocks(self):
        self.assertTrue(agent_loop._can_batch_read_only(
            [{"name": "think"}, {"name": "fs.read"}], False))
        self.assertIn("think", agent_loop._LEDGER_EXEMPT_TOOLS)


class ThinkRenderingTests(unittest.TestCase):
    def _render(self, result):
        event = transcript_view.build_tool_event(
            "think", "think", "short", str(result.get("result") or ""), result)
        out = io.StringIO()
        console = Console(file=out, force_terminal=False, width=100)
        view = transcript_view.TranscriptRenderer(console)
        view.event(event)
        view.flush()
        return out.getvalue(), event

    def test_shown_as_thinking_and_replays_from_the_event(self):
        text, event = self._render({"ok": True, "thought": "first\nsecond line",
                                    "question": "where is the key", "count": 0,
                                    "result": "Nothing…"})
        self.assertIn("first", text)
        self.assertIn("second line", text)
        self.assertIn("where is the key", text)
        self.assertIn("not in the session record", text)
        self.assertNotIn("Nothing…", text)
        # what the replay needs is on the event itself
        self.assertEqual(event["extra"]["thought"], "first\nsecond line")
        self.assertEqual(event["extra"]["count"], 0)


_FINAL = {"reply": "done", "tool_calls": [], "finish_reason": "stop",
          "done": False, "error": False}


class ThinkLoopTests(_RecordDir):
    def setUp(self):
        super().setUp()
        task_manager.clear_session_tasks()
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("auto_snapshot", False)
        agent_loop.set_runtime_config("loop_delay", 0)
        agent_loop.set_runtime_config("use_message_thread", True)
        agent_loop.set_runtime_config("mem_extract_on_compact", False)
        agent_loop.set_runtime_config("critic_enabled", False)
        patcher = mock.patch.object(model_route, "active_model", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(agent_loop.reset_runtime_config)
        self.addCleanup(task_manager.clear_session_tasks)

    def _run(self, responses, max_loops=8):
        calls = []

        def backend(**kwargs):
            calls.append(kwargs)
            return dict(responses[min(len(calls), len(responses)) - 1])

        deps = agent_loop.LoopDeps(
            read_file=lambda path: None,
            append_file=lambda path, content: None,
            write_file=lambda path, content: None,
            strip_ansi=lambda text: text,
            generate_prompt=lambda: "You are a test agent.",
            call_backend=backend,
            SubTerminalSession=mock.Mock,
            display_command_output=lambda *args, **kwargs: None,
            display_sub_terminal_preview=lambda *args, **kwargs: None,
            display_file_diff=lambda *args, **kwargs: None,
            console=Console(file=io.StringIO(), force_terminal=False),
            Markdown=Markdown,
        )
        state = {"_model_override": "glm-5.3"}
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "check the clock", {}, state,
                [{"role": "user", "content": "check the clock"}],
                max_loops_override=max_loops)
        return calls, result, state

    @staticmethod
    def _step(name, arguments, reasoning=""):
        return {"reply": "", "tool_calls": [{"name": name, "arguments": arguments}],
                "finish_reason": "tool_calls", "done": False, "error": False,
                "_reasoning": reasoning, "_served_model": "glm-5.3"}

    def test_thinking_repeatedly_neither_ends_nor_trips_the_loop_breakers(self):
        thoughts = [self._step("think", {"thought": "still weighing it"}) for _ in range(5)]
        calls, result, _ = self._run(thoughts + [_FINAL], max_loops=10)
        self.assertEqual(len(calls), 6)
        self.assertEqual(result.get("exit_reason"), agent_loop.TRANSITION_END_TURN)

    def test_question_finds_what_an_earlier_step_produced(self):
        responses = [
            self._step("time.now", {}, reasoning="the deploy window opens at the quarter hour"),
            self._step("think", {"thought": "when was the window?",
                                 "question": "deploy window quarter hour"}),
            _FINAL,
        ]
        calls, _, state = self._run(responses)
        tool_msgs = [m for m in calls[2]["messages"] if m.get("role") == "tool"]
        self.assertIn("the deploy window opens at the quarter hour", tool_msgs[-1]["content"])
        records = list(self.dir.glob("*__primary.jsonl"))
        self.assertEqual(len(records), 1)
        record = records[0]
        steps = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([bool(s.get("calls")) for s in steps], [True, True, False])
        self.assertEqual(steps[-1]["reply"], "done")


if __name__ == "__main__":
    unittest.main()
