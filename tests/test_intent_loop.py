"""Intent alignment wired into the agent loop.

These run the real ``run_agent_loop`` against a scripted backend that answers
differently per ``task_kind``, so what is asserted is what the provider would
actually have been sent: the pinned system-prompt section, the questions handed
to the working model, and the contract the progress critic is later judged
against.

Background threads are made synchronous for the duration of each run. The
alternative is asserting against a race — the spec build is launched on turn
zero and harvested on a later turn, and with a scripted backend the loop
outruns any real thread.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import intent


#: The opening tag as it is actually emitted. The hook section mentions the
#: tag by name, so matching the bare name would find the instructions rather
#: than the pinned section.
_UNDERSTANDING = '<task_understanding authoritative='

TASK = ("我想做一个像 ChatGPT 那样的网站，要能多轮对话，"
        "并且左边有会话列表。不要做移动端 App。")

SPEC_REPLY = json.dumps({
    "goal": "做一个对话式网站",
    "requirements": [
        {"id": "R1", "text": "支持多轮对话", "anchor": "要能多轮对话"},
        {"id": "R2", "text": "左侧会话列表", "anchor": "左边有会话列表"},
    ],
    "out_of_scope": ["移动端 App"],
    "deliverables": ["一个可访问的网站"],
    "open_questions": [
        {"id": "Q1", "q": "一个对话式网站的必备界面元素有哪些?",
         "needs": "evidence", "why": "决定首屏布局"},
        {"id": "Q2", "q": "要接入哪个模型?", "needs": "user", "why": "决定后端"},
    ],
    "assumptions": [],
    "task_breakdown": ["搭前端骨架", "接流式接口"],
}, ensure_ascii=False)


class _SyncThread:
    """A Thread that has already finished by the time ``start()`` returns."""

    def __init__(self, target=None, name="", args=(), kwargs=None, daemon=None):
        self._target, self._args = target, args
        self._kwargs = kwargs or {}
        self.name, self.daemon = name, daemon

    def start(self):
        try:
            if self._target is not None:
                self._target(*self._args, **self._kwargs)
        except Exception:
            pass

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


class _Console:
    width = 100

    def print(self, *a, **k):
        pass


class IntentLoopTestCase(unittest.TestCase):
    """Runs the loop in a throwaway cwd with a scripted backend."""

    #: Overridden per test; keys are runtime-config names.
    config = {}

    def setUp(self):
        self.calls = []          # every call_backend kwargs dict, in order
        self.intent_replies = [SPEC_REPLY, SPEC_REPLY]
        # The loop only reaches a second turn if the model asks for one, and
        # the intent spec is harvested at the top of a LATER turn than the one
        # that launched it — so a single-turn script could never see it land.
        self.working_turns = 4
        self.main_reply = {
            "reply": "开始", "tool_calls": [], "finish_reason": "stop",
            "done": True, "error": False,
        }
        self.critic_reply = json.dumps(
            {"on_track": True, "score": 90, "issue": "", "suggestion": ""})

    def _backend(self, **kw):
        self.calls.append(kw)
        kind = kw.get("task_kind", "")
        if kind == "intent":
            reply = (self.intent_replies.pop(0) if self.intent_replies
                     else SPEC_REPLY)
            return {"reply": reply, "done": True, "error": False}
        if kind == "critic":
            return {"reply": self.critic_reply, "done": True, "error": False}
        if self.working_turns > 0:
            self.working_turns -= 1
            return {
                "reply": "看一下目录",
                "tool_calls": [{"id": f"c{self.working_turns}", "name": "ls",
                                "arguments": {"path": "."}}],
                "finish_reason": "tool_calls", "done": False, "error": False,
            }
        return dict(self.main_reply)

    def _config(self, key):
        if key in self.config:
            return self.config[key]
        if key == "loop_delay":
            return 0        # no reason to sleep between scripted turns
        return agent_loop._DEFAULT_CONFIG.get(key)

    def run_loop(self, task=TASK, loops=3, state=None):
        deps = mock.Mock()
        deps.call_backend = self._backend
        deps.console = _Console()
        deps.read_file = lambda p: ""
        deps.generate_prompt = lambda: "BASE PROMPT"
        cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                Path(".laintas").mkdir()
                with mock.patch.object(agent_loop.threading, "Thread",
                                       _SyncThread), \
                        mock.patch.object(agent_loop, "get_runtime_config",
                                          side_effect=self._config):
                    return agent_loop.run_agent_loop(
                        deps, task, {}, state if state is not None else {},
                        [{"role": "user", "content": task}],
                        max_loops_override=loops)
        finally:
            os.chdir(cwd)

    # ── helpers ──────────────────────────────────────────────────────────
    def kinds(self):
        return [c.get("task_kind", "") for c in self.calls]

    def main_calls(self):
        return [c for c in self.calls
                if c.get("task_kind") not in ("intent", "critic")]

    def prompts(self):
        return [c.get("system_prompt", "") for c in self.main_calls()]

    def sent_text(self):
        """Everything the working model was sent, as one string."""
        out = []
        for call in self.main_calls():
            for message in call.get("messages") or []:
                content = message.get("content")
                out.append(content if isinstance(content, str) else str(content))
        return "\n".join(out)


class SpecBuildTests(IntentLoopTestCase):
    def test_self_ask_runs_before_the_work_and_costs_one_call_per_round(self):
        self.config = {"intent_self_ask_rounds": 2, "critic_enabled": False}
        self.main_reply["done"] = False        # keep looping so we see turn 2
        self.run_loop(loops=3)
        self.assertEqual(2, self.kinds().count("intent"))
        # The spec build must not carry the tool catalogue; a judge that
        # cannot call a tool paying for the schema of every tool was the
        # measured 10% of input spend the critic used to waste.
        intent_calls = [c for c in self.calls if c.get("task_kind") == "intent"]
        self.assertTrue(all(c.get("tools_enabled") is False
                            for c in intent_calls))

    def test_agreed_reading_is_pinned_into_the_system_prompt(self):
        self.config = {"critic_enabled": False}
        self.run_loop(loops=3)
        prompts = self.prompts()
        # Turn one cannot have it — the spec did not exist yet.
        self.assertNotIn(_UNDERSTANDING, prompts[0])
        self.assertIn(_UNDERSTANDING, prompts[-1])
        self.assertIn("支持多轮对话", prompts[-1])
        self.assertIn("左侧会话列表", prompts[-1])

    def test_the_prefix_change_is_attributed_not_unexplained(self):
        # A system prompt that changes mid-task re-bills the whole prefix at
        # the cache-miss rate; the tripwire exists so that shows up as a named
        # cause rather than a bigger invoice.
        self.config = {"critic_enabled": False}
        state = self.run_loop(loops=3)["state"]
        self.assertIn("intent", state.get("_sys_prompt_churn_causes") or [])

    def test_invented_requirements_never_reach_the_prompt(self):
        self.config = {"critic_enabled": False}
        self.intent_replies = [json.dumps({
            "goal": "",
            "requirements": [
                {"id": "R1", "text": "支持语音输入", "anchor": "支持语音输入"},
                {"id": "R2", "text": "接入支付", "anchor": "需要支付功能"},
            ],
        }, ensure_ascii=False)] * 2
        state = self.run_loop(loops=3)["state"]
        for prompt in self.prompts():
            self.assertNotIn(_UNDERSTANDING, prompt)
            self.assertNotIn("语音输入", prompt)
        self.assertEqual(intent.DISABLED, state["_intent"]["phase"])

    def test_a_broken_intent_model_does_not_break_the_task(self):
        self.config = {"critic_enabled": False}
        self.intent_replies = ["not json at all", "still not json"]
        result = self.run_loop(loops=3)
        self.assertEqual(intent.DISABLED, result["state"]["_intent"]["phase"])
        self.assertTrue(self.main_calls())      # the work still happened

    def test_a_build_orphaned_by_a_returning_turn_is_retried(self):
        """A worker that outlives its turn wrote into a local that is gone.

        Leaving the phase at "analyzing" would mean the task never gets a
        spec and never tries again — a silent, permanent no-op.
        """
        self.config = {"critic_enabled": False}
        state = {"_intent": dict(intent.new_state(), phase=intent.ANALYZING)}
        self.run_loop(loops=3, state=state)
        self.assertEqual(intent.SPEC_READY, state["_intent"]["phase"])
        self.assertTrue(intent.is_usable(state["_intent"]["spec"]))

    def test_spec_is_kept_in_state_for_the_rest_of_the_turn(self):
        # Turn-only by declaration: the reading quotes THIS request, so the
        # next user turn builds its own instead of inheriting this one.
        self.config = {"critic_enabled": False}
        state = self.run_loop(loops=3)["state"]
        self.assertEqual(intent.SPEC_READY, state["_intent"]["phase"])
        self.assertTrue(intent.is_usable(state["_intent"]["spec"]))


class EvidenceQuestionTests(IntentLoopTestCase):
    def test_evidence_questions_go_to_the_model_that_has_tools(self):
        self.config = {"critic_enabled": False}
        self.run_loop(loops=3)
        sent = self.sent_text()
        self.assertIn("<intent_questions>", sent)
        self.assertIn("必备界面元素", sent)

    def test_questions_only_for_the_requester_are_not_asked_of_the_model(self):
        # A question only the user can answer is not research; handing it to
        # the model invites it to invent an answer.
        self.config = {"critic_enabled": False}
        self.run_loop(loops=3)
        self.assertNotIn("要接入哪个模型", self.sent_text())

    def test_questions_are_asked_once(self):
        """Asked once into the durable thread, not once per request.

        The block is appended to thread_messages, so every later request
        carries it again; what must not happen is a second injection.
        """
        self.config = {"critic_enabled": False}
        self.run_loop(loops=5)
        last = self.main_calls()[-1].get("messages") or []
        blocks = [m for m in last
                  if "<intent_questions>" in str(m.get("content", ""))]
        self.assertEqual(1, len(blocks))

    def test_a_spec_without_evidence_questions_injects_nothing(self):
        self.config = {"critic_enabled": False}
        self.intent_replies = [json.dumps({
            "goal": "做一个对话式网站",
            "requirements": [{"id": "R1", "text": "支持多轮对话",
                              "anchor": "要能多轮对话"}],
            "open_questions": [{"id": "Q2", "q": "要接入哪个模型?",
                                "needs": "user"}],
        }, ensure_ascii=False)] * 2
        self.run_loop(loops=3)
        self.assertNotIn("<intent_questions>", self.sent_text())


class CriticHandoffTests(IntentLoopTestCase):
    def test_the_agreed_reading_becomes_the_progress_critics_contract(self):
        """The point of settling the reading is that later checks use it.

        Without this the progress critic keeps judging against the ambiguous
        sentence the intent layer was built because nobody could interpret.
        """
        self.config = {"critic_enabled": True, "critic_min_loop": 1,
                       "critic_interval": 1}
        self.run_loop(loops=5)
        critic_calls = [c for c in self.calls if c.get("task_kind") == "critic"]
        self.assertTrue(critic_calls)
        contracted = [c for c in critic_calls
                      if "支持多轮对话" in str(c.get("messages"))]
        self.assertTrue(contracted, "no critic call carried the agreed reading")
        self.assertIn("THE CHILD OWES THIS CONTRACT",
                      str(contracted[0].get("messages")))


class GatingTests(IntentLoopTestCase):
    def test_disabled_by_config_changes_nothing(self):
        self.config = {"intent_enabled": False, "critic_enabled": False}
        self.run_loop(loops=3)
        self.assertNotIn("intent", self.kinds())
        for prompt in self.prompts():
            self.assertNotIn("<intent_alignment>", prompt)
            self.assertNotIn(_UNDERSTANDING, prompt)

    def test_a_short_instruction_is_not_worth_a_pass(self):
        self.config = {"critic_enabled": False}
        self.run_loop(task="ls -la", loops=3)
        self.assertNotIn("intent", self.kinds())

    def test_a_contracted_child_keeps_its_own_contract(self):
        # agent_contract already says exactly what a child owes; a second
        # authority would only fight with it.
        self.config = {"critic_enabled": False}
        self.run_loop(loops=3, state={"_contract": {"outputs": ["x"]}})
        self.assertNotIn("intent", self.kinds())

    def test_the_hook_is_present_whenever_the_layer_is_on(self):
        self.config = {"critic_enabled": False}
        self.run_loop(loops=2)
        self.assertIn("<intent_alignment>", self.prompts()[0])


if __name__ == "__main__":
    unittest.main()
