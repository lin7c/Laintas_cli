"""Thinking is replayed to the model that wrote it, and only to that model.

GLM, Kimi, DeepSeek and MiniMax expect a tool-calling assistant turn to come
back with its `reasoning_content`; the thread used to store none of it.
"""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console
from rich.markdown import Markdown

import agent_loop
import laintas_cli
import model_route
import task_manager

from test_session_runtime import _chdir


def _tool_turn(reasoning="plan: read the clock", served="glm-5.3"):
    return {"reply": "checking", "tool_calls": [{"name": "time.now", "arguments": {}}],
            "finish_reason": "tool_calls", "done": False, "error": False,
            "_reasoning": reasoning, "_served_model": served}


_FINAL = {"reply": "finished", "tool_calls": [], "finish_reason": "stop",
          "done": False, "error": False}


class ReasoningHelpersTests(unittest.TestCase):
    def test_tool_turn_stores_tagged_reasoning(self):
        msgs = agent_loop._thread_messages_for_turn(
            "x", [{"id": "c1", "name": "time.now", "arguments": {}, "output": "t"}],
            reasoning="why", reasoning_model="glm-5.3")
        self.assertEqual(msgs[0]["reasoning_content"], "why")
        self.assertEqual(msgs[0][agent_loop._REASONING_MODEL_KEY], "glm-5.3")

    def test_untagged_reasoning_is_stored_for_folding(self):
        msgs = agent_loop._thread_messages_for_turn(
            "x", [{"id": "c1", "name": "time.now", "arguments": {}, "output": "t"}],
            reasoning="why", reasoning_model="")
        self.assertEqual(msgs[0]["reasoning_content"], "why")
        self.assertEqual(msgs[0][agent_loop._REASONING_MODEL_KEY], "")

    def test_tag_rules(self):
        tag = agent_loop._reasoning_tag
        self.assertEqual(tag("glm-5.3", ""), "glm-5.3")
        self.assertEqual(tag("glm-5.3", "GLM-5.3"), "glm-5.3")
        self.assertEqual(tag("gpt-6-sol", "gpt-6-sol-2026-09-01"), "gpt-6-sol")
        self.assertEqual(tag("auto", "glm-5.3"), "")
        self.assertEqual(tag("priority", "deepseek-v4-pro"), "")
        self.assertEqual(tag("glm-5.3", "kimi-k3"), "")

    def test_projection_replays_own_reasoning_and_folds_the_rest(self):
        thread = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [],
             "reasoning_content": "a", "_reasoning_model": "glm-5.3"},
            {"role": "assistant", "content": "reading it", "tool_calls": [],
             "reasoning_content": "b", "_reasoning_model": "kimi-k3"},
        ]
        sent = agent_loop._project_reasoning(thread, "GLM-5.3")
        self.assertEqual(sent[1]["reasoning_content"], "a")
        self.assertIsNone(sent[1]["content"])
        self.assertNotIn("reasoning_content", sent[2])
        self.assertTrue(sent[2]["content"].startswith("<prior_reasoning"))
        self.assertIn("\nb\n</prior_reasoning>\nreading it", sent[2]["content"])
        self.assertFalse(any("_reasoning_model" in m for m in sent))
        # The stored thread is untouched.
        self.assertEqual(thread[2]["reasoning_content"], "b")
        self.assertEqual(thread[2]["content"], "reading it")

    def test_unpinned_target_folds_everything_the_same_way(self):
        thread = [{"role": "assistant", "content": None, "tool_calls": [],
                   "reasoning_content": "a", "_reasoning_model": "glm-5.3"}]
        folded = [agent_loop._project_reasoning(thread, target)
                  for target in ("", "auto", "priority")]
        for sent in folded:
            self.assertNotIn("reasoning_content", sent[0])
            self.assertEqual(sent, folded[0])  # stable prefix whatever auto picks

    def test_fold_quotes_the_whole_text(self):
        text = "plan\n\n" + "x" * 5000 + "\ndecide"
        note = agent_loop._fold_reasoning(text)
        self.assertIn(text, note)  # never cut short, line breaks kept
        self.assertTrue(note.startswith('<prior_reasoning source="another model">'))
        self.assertEqual(agent_loop._fold_reasoning("  "), "")

    def test_reply_only_turn_keeps_its_reasoning(self):
        msgs = agent_loop._thread_messages_for_turn(
            "done", [], reasoning="why", reasoning_model="glm-5.3",
            reasoning_details=[{"type": "reasoning.text", "text": "why"}])
        self.assertEqual(msgs, [{"role": "assistant", "content": "done",
                                 "reasoning_content": "why",
                                 "reasoning_details": [{"type": "reasoning.text", "text": "why"}],
                                 agent_loop._REASONING_MODEL_KEY: "glm-5.3"}])
        self.assertEqual(agent_loop._thread_messages_for_turn("done", []),
                         [{"role": "assistant", "content": "done"}])

    def test_summary_source_reads_reasoning_not_signatures(self):
        text = agent_loop._serialize_thread_msg({
            "role": "assistant", "content": "ok", "reasoning_content": "because",
            "tool_calls": [{"id": "c", "type": "function",
                            "function": {"name": "time.now", "arguments": "{}"},
                            "extra_content": {"google": {"thought_signature": "g=="}}}]})
        self.assertIn("[Assistant reasoning]: because", text)
        self.assertIn("time.now", text)
        self.assertNotIn("thought_signature", text)

    def _compact(self, thread, summary):
        with mock.patch.object(agent_loop, "compaction_budget",
                               return_value={"window": 32000, "usable": 4000}), \
                mock.patch.object(agent_loop, "_compaction_boundaries", return_value=(3, 3)), \
                mock.patch.object(agent_loop, "_summarize_head_in_chunks",
                                  return_value=summary) as summarize:
            changed = agent_loop._compact_thread_messages(
                thread, SimpleNamespace(call_backend=mock.Mock()), {}, "EN", {}, force=True)
        return changed, summarize

    def test_summary_receives_reasoning_before_anything_is_dropped(self):
        old = {"role": "assistant", "content": None, "tool_calls": [],
               "reasoning_content": "old", "_reasoning_model": "glm-5.3"}
        thread = [{"role": "user", "content": "go"}, old,
                  {"role": "user", "content": "next"},
                  {"role": "assistant", "content": "new"}]
        changed, summarize = self._compact(thread, "summary")
        self.assertTrue(changed)
        head = summarize.call_args[0][2]
        self.assertEqual(head[1]["reasoning_content"], "old")  # seen by the summary
        self.assertTrue(thread[0]["content"].startswith("[CONVERSATION SUMMARY"))

    def test_prune_drops_reasoning_outside_the_tail_only(self):
        old = {"role": "assistant", "content": None, "tool_calls": [],
               "reasoning_content": "old", "_reasoning_model": "glm-5.3"}
        new = {"role": "assistant", "content": None, "tool_calls": [],
               "reasoning_content": "new", "_reasoning_model": "glm-5.3"}
        thread = [{"role": "user", "content": "go"}, old,
                  {"role": "user", "content": "next"}, new]
        with mock.patch.object(agent_loop, "compaction_budget",
                               return_value={"window": 32000, "usable": 4000}), \
                mock.patch.object(agent_loop, "_compaction_boundaries", return_value=(3, 3)), \
                mock.patch.object(agent_loop, "_summarize_head_in_chunks", return_value=None):
            changed = agent_loop._compact_thread_messages(
                thread, SimpleNamespace(call_backend=mock.Mock()), {}, "EN", {}, force=True)
        self.assertTrue(changed)
        self.assertNotIn("reasoning_content", old)
        self.assertNotIn("_reasoning_model", old)
        self.assertEqual(new["reasoning_content"], "new")


SIGNED = {"type": "reasoning.text", "text": "plan", "signature": "sig==",
          "format": "anthropic-claude-v1", "index": 0}
GEMINI = {"google": {"thought_signature": "g=="}}


class ProviderStateTests(unittest.TestCase):
    def test_streamed_fragments_merge_by_index(self):
        acc = []
        laintas_cli._merge_reasoning_details(acc, [{"type": "reasoning.text", "text": "pl",
                                                    "format": "anthropic-claude-v1", "index": 0}])
        laintas_cli._merge_reasoning_details(acc, [{"text": "an", "index": 0},
                                                   {"signature": "sig==", "index": 0}])
        laintas_cli._merge_reasoning_details(acc, [{"type": "reasoning.encrypted", "data": "x"}])
        self.assertEqual(acc[0], SIGNED)
        self.assertEqual(acc[1], {"type": "reasoning.encrypted", "data": "x"})

    def test_native_tool_call_keeps_its_signature(self):
        calls = laintas_cli._native_to_tool_calls({0: {
            "id": "c", "name": "time.now", "arguments": "{}", "extra_content": GEMINI}})
        self.assertEqual(calls[0]["_extra_content"], GEMINI)

    def _thread(self, model):
        return [{"role": "user", "content": "go"}] + agent_loop._thread_messages_for_turn(
            "", [{"id": "c1", "name": "time.now", "arguments": {}, "output": "t",
                  "extra_content": GEMINI}],
            reasoning="plan", reasoning_model=model, reasoning_details=[SIGNED])

    def test_state_goes_back_only_to_its_own_model(self):
        thread = self._thread("glm-5.3")
        self.assertEqual(thread[1]["reasoning_details"], [SIGNED])
        self.assertEqual(thread[1]["tool_calls"][0]["extra_content"], GEMINI)
        own = agent_loop._project_reasoning(thread, "glm-5.3")[1]
        self.assertEqual(own["reasoning_details"], [SIGNED])
        self.assertEqual(own["tool_calls"][0]["extra_content"], GEMINI)
        for target in ("kimi-k3", "auto"):
            other = agent_loop._project_reasoning(thread, target)[1]
            self.assertNotIn("reasoning_details", other)
            self.assertNotIn("extra_content", other["tool_calls"][0])
            self.assertIn("<prior_reasoning", other["content"])
        self.assertEqual(thread[1]["tool_calls"][0]["extra_content"], GEMINI)  # untouched

    def test_state_alone_is_still_tagged(self):
        msgs = agent_loop._thread_messages_for_turn(
            "", [{"id": "c1", "name": "time.now", "arguments": {}, "output": "t"}],
            reasoning_model="glm-5.3", reasoning_details=[SIGNED])
        self.assertEqual(msgs[0][agent_loop._REASONING_MODEL_KEY], "glm-5.3")
        self.assertNotIn("reasoning_content", msgs[0])

    def test_prune_drops_provider_state(self):
        message = self._thread("glm-5.3")[1]
        self.assertTrue(agent_loop._drop_reasoning(message))
        self.assertNotIn("reasoning_details", message)
        self.assertNotIn("extra_content", message["tool_calls"][0])


class ReasoningReplayLoopTests(unittest.TestCase):
    def setUp(self):
        task_manager.clear_session_tasks()
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("auto_snapshot", False)
        agent_loop.set_runtime_config("loop_delay", 0)
        agent_loop.set_runtime_config("use_message_thread", True)
        agent_loop.set_runtime_config("mem_extract_on_compact", False)
        patcher = mock.patch.object(model_route, "active_model", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        agent_loop.reset_runtime_config()
        task_manager.clear_session_tasks()

    def _second_request(self, model, first):
        calls = []

        def backend(**kwargs):
            calls.append(kwargs)
            return dict(first if len(calls) == 1 else _FINAL)

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
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            agent_loop.run_agent_loop(
                deps, "what time is it", {}, {"_model_override": model},
                [{"role": "user", "content": "what time is it"}], max_loops_override=3)
        self.assertGreaterEqual(len(calls), 2)
        sent = calls[1]["messages"]
        self.assertFalse(any("_reasoning_model" in m for m in sent))
        return [m for m in sent if m.get("role") == "assistant" and m.get("tool_calls")]

    def test_same_model_gets_its_reasoning_back(self):
        turns = self._second_request("glm-5.3", _tool_turn())
        self.assertEqual(turns[-1].get("reasoning_content"), "plan: read the clock")

    def test_provider_state_comes_back_on_the_same_model(self):
        first = dict(_tool_turn(), _reasoning_details=[SIGNED])
        first["tool_calls"] = [{"name": "time.now", "arguments": {}, "_extra_content": GEMINI}]
        turns = self._second_request("glm-5.3", first)
        self.assertEqual(turns[-1]["reasoning_details"], [SIGNED])
        self.assertEqual(turns[-1]["tool_calls"][0]["extra_content"], GEMINI)
        other = self._second_request("glm-5.3", dict(first, _served_model="kimi-k3"))
        self.assertNotIn("reasoning_details", other[-1])
        self.assertNotIn("extra_content", other[-1]["tool_calls"][0])

    def test_substituted_model_gets_it_folded(self):
        turns = self._second_request("glm-5.3", _tool_turn(served="kimi-k3"))
        self.assertNotIn("reasoning_content", turns[-1])
        self.assertIn("plan: read the clock\n</prior_reasoning>", turns[-1]["content"])

    def test_auto_routing_gets_it_folded(self):
        turns = self._second_request("auto", _tool_turn())
        self.assertNotIn("reasoning_content", turns[-1])
        self.assertIn("plan: read the clock\n</prior_reasoning>", turns[-1]["content"])


class HarnessStateTests(unittest.TestCase):
    def test_appended_to_the_latest_tool_result(self):
        thread = [{"role": "user", "content": "go"},
                  {"role": "assistant", "content": None, "tool_calls": []},
                  {"role": "tool", "tool_call_id": "c", "content": "out"}]
        sent = agent_loop._attach_harness_state(thread, "<progress>1</progress>")
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[-1]["content"],
                         "out\n\n<harness_state>\n<progress>1</progress>\n</harness_state>")
        self.assertEqual(thread[-1]["content"], "out")  # stored thread untouched

    def test_task_message_text_and_parts(self):
        sent = agent_loop._attach_harness_state([{"role": "user", "content": "go"}], "s")
        self.assertEqual(sent, [{"role": "user", "content": "go\n\n<harness_state>\ns\n</harness_state>"}])
        parts = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}}]
        sent = agent_loop._attach_harness_state([{"role": "user", "content": parts}], "s")
        self.assertEqual(sent[0]["content"][:2], parts)
        self.assertEqual(sent[0]["content"][2]["text"], "<harness_state>\ns\n</harness_state>")
        self.assertEqual(len(parts), 2)

    def test_falls_back_to_its_own_message(self):
        reply = [{"role": "assistant", "content": "hi"}]
        sent = agent_loop._attach_harness_state(reply, "s")
        self.assertEqual(sent[-1], {"role": "user", "content": "<harness_state>\ns\n</harness_state>"})
        self.assertIs(agent_loop._attach_harness_state(reply, "  "), reply)


class HarnessStateLoopTests(ReasoningReplayLoopTests):
    def test_no_user_message_after_the_tool_result(self):
        calls = []

        def backend(**kwargs):
            calls.append(kwargs)
            return dict(_tool_turn() if len(calls) == 1 else _FINAL)

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
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            agent_loop.run_agent_loop(
                deps, "what time is it", {}, {"_model_override": "glm-5.3"},
                [{"role": "user", "content": "what time is it"}], max_loops_override=3)
        first, second = calls[0]["messages"], calls[1]["messages"]
        self.assertEqual(first[-1]["role"], "user")
        self.assertTrue(first[-1]["content"].startswith("what time is it\n\n<harness_state>"))
        self.assertEqual(second[-1]["role"], "tool")
        self.assertTrue(second[-1]["content"].rstrip().endswith("</harness_state>"))
        self.assertEqual(sum(m.get("role") == "user" for m in second), 1)
        self.assertNotIn("<harness_state>", second[0]["content"] if second[0]["role"] == "user" else "")
        self.assertIn("<harness_state_notes>", calls[1]["system_prompt"])


if __name__ == "__main__":
    unittest.main()
