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

Three other things are neutralised for speed, none of them under test here:
the inter-turn backoff, the per-request tool-schema rendering, and semantic
skill/memory ranking. Left in, the fixture rather than the code is what the
suite spends its time on.
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


def _memoized_tool_schemas():
    """Cache the rendered tool schemas for the duration of one scripted run.

    Rendering them costs about 0.7s per request (the prose rewrite runs once
    per tool per description), and the registry does not change during these
    runs. Without this the fixture, not the code under test, is what the suite
    spends its time on.
    """
    import tools as tools_mod
    registry = tools_mod.get_registry()
    real = registry.to_openai_tools
    cache = {}

    def cached(unified=False, allowed_names=None):
        key = (unified, tuple(sorted(allowed_names)) if allowed_names else None)
        if key not in cache:
            cache[key] = real(unified=unified, allowed_names=allowed_names)
        return cache[key]

    return mock.patch.object(registry, "to_openai_tools", cached)


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
        self.compare_reply = json.dumps({
            "severity": "detail_gap", "aligned": True, "divergences": [],
            "void_steps": [], "missing": [], "next": ""}, ensure_ascii=False)

    def _backend(self, **kw):
        self.calls.append(kw)
        kind = kw.get("task_kind", "")
        if kind == "intent":
            reply = (self.intent_replies.pop(0) if self.intent_replies
                     else SPEC_REPLY)
            return {"reply": reply, "done": True, "error": False}
        if kind == "intent_compare":
            return {"reply": self.compare_reply, "done": True, "error": False}
        if kind == "critic":
            return {"reply": self.critic_reply, "done": True, "error": False}
        if self.working_turns > 0:
            self.working_turns -= 1
            # Vary the argument: an identical call every turn is repetition,
            # and the loop's (correct) backoff then adds a second of delay to
            # every scripted iteration.
            return {
                "reply": "看一下目录",
                "tool_calls": [{"id": f"c{self.working_turns}", "name": "ls",
                                "arguments": {"path": "."
                                              if self.working_turns % 2
                                              else ".laintas"}}],
                "finish_reason": "tool_calls", "done": False, "error": False,
            }
        return dict(self.main_reply)

    def _config(self, key):
        if key in self.config:
            return self.config[key]
        if key == "loop_delay":
            return 0        # no reason to sleep between scripted turns
        if key in ("skill_route_highlight", "mem_recall_highlight"):
            # Semantic ranking over skills and memories costs about a second
            # per iteration and has nothing to do with what is asserted here.
            return False
        return agent_loop._DEFAULT_CONFIG.get(key)

    def run_loop(self, task=TASK, loops=3, state=None, agent_id=None):
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
                        _memoized_tool_schemas(), \
                        mock.patch.object(agent_loop, "_adaptive_loop_delay",
                                          return_value=0), \
                        mock.patch.object(agent_loop, "get_runtime_config",
                                          side_effect=self._config):
                    return agent_loop.run_agent_loop(
                        deps, task, {}, state if state is not None else {},
                        [{"role": "user", "content": task}],
                        agent_id=agent_id,
                        max_loops_override=loops)
        finally:
            os.chdir(cwd)

    # ── helpers ──────────────────────────────────────────────────────────
    def kinds(self):
        return [c.get("task_kind", "") for c in self.calls]

    def main_calls(self):
        return [c for c in self.calls if c.get("task_kind") not in
                ("intent", "intent_compare", "critic")]

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
        self.assertNotEqual(intent.IDLE, state["_intent"]["phase"])
        self.assertTrue(intent.is_usable(state["_intent"]["spec"]))

    def test_spec_is_kept_in_state_for_the_rest_of_the_turn(self):
        # Turn-only by declaration: the reading quotes THIS request, so the
        # next user turn builds its own instead of inheriting this one.
        self.config = {"critic_enabled": False}
        state = self.run_loop(loops=3)["state"]
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


class ComparisonTests(IntentLoopTestCase):
    """The three verdicts, and what each is allowed to do to the run."""

    def test_a_scope_error_names_the_void_steps_and_the_files(self):
        self.config = {"critic_enabled": False}
        self.compare_reply = json.dumps({
            "severity": "scope_error", "aligned": False,
            "divergences": [{"req_id": "R1", "steps": [3],
                             "what": "建成了单页工具", "why": "没有会话"}],
            "void_steps": [3], "missing": ["R2"],
            "next": "改成会话式布局"}, ensure_ascii=False)
        state = self.run_loop(loops=6)["state"]
        sent = self.sent_text()
        self.assertIn("<intent_correction", sent)
        self.assertIn("Steps considered void: 3", sent)
        self.assertIn("改成会话式布局", sent)
        self.assertEqual(intent.CORRECTING, state["_intent"]["phase"])

    def test_a_detail_gap_notes_without_declaring_anything_void(self):
        # Undoing correct work over a missing detail is the expensive
        # mistake; a detail gap may point, never void.
        self.config = {"critic_enabled": False}
        self.compare_reply = json.dumps({
            "severity": "detail_gap", "aligned": False,
            "divergences": [{"req_id": "R2", "steps": [3],
                             "what": "会话列表还没做", "why": ""}],
            "void_steps": [3], "missing": ["R2"]}, ensure_ascii=False)
        state = self.run_loop(loops=6)["state"]
        sent = self.sent_text()
        self.assertIn("<intent_note>", sent)
        self.assertNotIn("<intent_correction", sent)
        self.assertNotIn("void", sent)
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])

    def test_alignment_interrupts_nothing(self):
        self.config = {"critic_enabled": False}
        state = self.run_loop(loops=6)["state"]
        sent = self.sent_text()
        self.assertNotIn("<intent_correction", sent)
        self.assertNotIn("<intent_note>", sent)
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])

    def test_an_unsure_judge_does_not_get_to_interrupt(self):
        self.config = {"critic_enabled": False}
        self.compare_reply = json.dumps({
            "severity": "critic_unsure", "aligned": False,
            "divergences": [{"req_id": "R1", "steps": [3], "what": "说不好"}],
        }, ensure_ascii=False)
        state = self.run_loop(loops=6)["state"]
        self.assertNotIn("<intent_correction", self.sent_text())
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])

    def test_the_comparison_is_run_once_and_without_tools(self):
        self.config = {"critic_enabled": False}
        self.run_loop(loops=6)
        compares = [c for c in self.calls
                    if c.get("task_kind") == "intent_compare"]
        self.assertEqual(1, len(compares))
        self.assertIs(False, compares[0].get("tools_enabled"))

    def test_the_comparison_sees_thread_step_numbers(self):
        # A correction that cites "step 3" is worthless if the judge was
        # shown an unnumbered tail.
        self.config = {"critic_enabled": False}
        self.run_loop(loops=6)
        compares = [c for c in self.calls
                    if c.get("task_kind") == "intent_compare"]
        self.assertIn("[step 0]", str(compares[0].get("messages")))

    def test_a_failed_comparison_leaves_the_spec_in_charge(self):
        self.config = {"critic_enabled": False}
        self.compare_reply = "not json"
        state = self.run_loop(loops=6)["state"]
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])
        self.assertNotIn("<intent_correction", self.sent_text())
        self.assertIn(_UNDERSTANDING, self.prompts()[-1])

    def test_no_comparison_before_the_agent_has_acted(self):
        self.config = {"critic_enabled": False, "intent_compare_loop": 99}
        self.run_loop(loops=4)
        self.assertNotIn("intent_compare", self.kinds())


class TaskBreakdownTests(IntentLoopTestCase):
    def test_the_breakdown_becomes_real_session_tasks(self):
        """Advice in a prompt competes with everything else in the context.

        <active_tasks> is rebuilt into every turn, so a task written there is
        both visible and durable.
        """
        self.config = {"critic_enabled": False}
        import task_manager
        created = []
        with mock.patch.object(task_manager, "create_session_task",
                               side_effect=lambda s, d="", **k: created.append(s)), \
                mock.patch.object(task_manager, "get_active_tasks_snapshot",
                                  return_value=""):
            self.run_loop(loops=4)
        self.assertEqual(["搭前端骨架", "接流式接口"], created)

    def test_an_agent_that_planned_for_itself_is_left_alone(self):
        # Two task lists for one job is worse than either.
        self.config = {"critic_enabled": False}
        import task_manager
        created = []
        with mock.patch.object(task_manager, "create_session_task",
                               side_effect=lambda s, d="", **k: created.append(s)), \
                mock.patch.object(task_manager, "get_active_tasks_snapshot",
                                  return_value="1. 已有任务"):
            self.run_loop(loops=4)
        self.assertEqual([], created)

    def test_the_breakdown_is_written_once(self):
        self.config = {"critic_enabled": False}
        import task_manager
        created = []
        with mock.patch.object(task_manager, "create_session_task",
                               side_effect=lambda s, d="", **k: created.append(s)), \
                mock.patch.object(task_manager, "get_active_tasks_snapshot",
                                  return_value=""):
            self.run_loop(loops=6)
        self.assertEqual(2, len(created))

    def test_disabled_by_config(self):
        self.config = {"critic_enabled": False, "intent_inject_tasks": False}
        import task_manager
        created = []
        with mock.patch.object(task_manager, "create_session_task",
                               side_effect=lambda s, d="", **k: created.append(s)), \
                mock.patch.object(task_manager, "get_active_tasks_snapshot",
                                  return_value=""):
            self.run_loop(loops=4)
        self.assertEqual([], created)


SCOPE_ERROR_REPLY = json.dumps({
    "severity": "scope_error", "aligned": False,
    "divergences": [{"req_id": "R1", "steps": [3], "what": "建成了单页工具",
                     "why": "没有会话"}],
    "void_steps": [3], "missing": ["R2"], "next": "改成会话式布局",
}, ensure_ascii=False)


class DebateTests(IntentLoopTestCase):
    """A correction the agent disagrees with is not yet a correction.

    The judge runs on the cheap auxiliary model; letting it overrule a
    frontier model on what a sentence meant, unchallenged, is precisely how
    this feature would make things worse than no feature.
    """

    def setUp(self):
        super().setUp()
        self.compare_reply = SCOPE_ERROR_REPLY
        self.judge_replies = []
        self.working_turns = 10

    def _backend(self, **kw):
        if kw.get("task_kind") == "intent_judge":
            self.calls.append(kw)
            reply = (self.judge_replies.pop(0) if self.judge_replies
                     else json.dumps({"verdict": "critic_right"}))
            return {"reply": reply, "done": True, "error": False}
        return super()._backend(**kw)

    def main_calls(self):
        return [c for c in self.calls if c.get("task_kind") not in
                ("intent", "intent_compare", "intent_judge", "critic")]

    def test_the_agent_can_win_and_the_spec_is_rewritten(self):
        self.config = {"critic_enabled": False}
        self.judge_replies = [json.dumps({
            "verdict": "main_right", "reason": "请求里并没有要求会话列表在左侧",
            "spec_fix": {"drop": ["R2"], "add": [
                {"id": "R9", "text": "不做移动端", "anchor": "不要做移动端 App"}]},
        }, ensure_ascii=False)]
        state = self.run_loop(loops=9)["state"]
        spec = state["_intent"]["spec"]
        ids = [r["id"] for r in spec["requirements"]]
        self.assertNotIn("R2", ids)
        self.assertIn("R9", ids)
        self.assertEqual(2, spec["spec_version"])
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])
        # A changed system prompt is silent to the model; losing the argument
        # has to be said in the thread.
        self.assertIn("<intent_revision>", self.sent_text())
        self.assertIn('version="2"', self.prompts()[-1])

    def test_the_review_can_win_and_nothing_more_is_injected(self):
        self.config = {"critic_enabled": False}
        self.judge_replies = [json.dumps({
            "verdict": "critic_right", "reason": "请求写着左边有会话列表"})]
        state = self.run_loop(loops=9)["state"]
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])
        self.assertNotIn("<intent_challenge", self.sent_text())
        self.assertNotIn("<intent_revision>", self.sent_text())

    def test_an_unsettled_point_gets_another_round_then_the_user(self):
        self.config = {"critic_enabled": False, "intent_debate_rounds": 2}
        self.judge_replies = [json.dumps({"verdict": "unresolved",
                                          "user_question": "要接入哪个模型?"})] * 4
        state = self.run_loop(loops=12)["state"]
        sent = self.sent_text()
        self.assertIn('<intent_challenge round="2"', sent)
        self.assertIn("<intent_unresolved>", sent)
        self.assertIn("要接入哪个模型?", sent)
        self.assertEqual(intent.ESCALATED, state["_intent"]["phase"])

    def test_neither_side_overwrites_the_other_when_unresolved(self):
        # An ambiguity in the request is not something either model can
        # resolve by arguing; the spec must come out unchanged.
        self.config = {"critic_enabled": False, "intent_debate_rounds": 1}
        self.judge_replies = [json.dumps({
            "verdict": "unresolved", "user_question": "要接入哪个模型?",
            "spec_fix": {"drop": ["R1", "R2"]}})] * 3
        state = self.run_loop(loops=10)["state"]
        ids = [r["id"] for r in state["_intent"]["spec"]["requirements"]]
        self.assertEqual(["R1", "R2"], ids)
        self.assertEqual(1, state["_intent"]["spec"]["spec_version"])

    def test_unattended_runs_continue_on_a_stated_assumption(self):
        # There is nobody to ask; stalling helps no one, but the assumption
        # has to be said out loud.
        self.config = {"critic_enabled": False, "intent_debate_rounds": 1}
        self.judge_replies = [json.dumps({"verdict": "unresolved",
                                          "user_question": "要接入哪个模型?"})] * 3
        import mode_manager
        with mock.patch.object(mode_manager, "get_auto_approve",
                               return_value="all"):
            self.run_loop(loops=10)
        sent = self.sent_text()
        self.assertIn("<intent_unresolved>", sent)
        self.assertIn("nobody to ask", sent)
        self.assertNotIn("Stop and ask the user", sent)

    def test_the_judge_only_reads_what_came_after_the_correction(self):
        """Judging the whole thread would read pre-correction work as a
        rebuttal the agent never made."""
        self.config = {"critic_enabled": False}
        self.run_loop(loops=9)
        judged = [c for c in self.calls
                  if c.get("task_kind") == "intent_judge"]
        self.assertTrue(judged)
        body = "".join(str(m.get("content") or "")
                       for m in judged[0].get("messages") or [])
        self.assertIn("THE AGENT'S REBUTTAL", body)
        self.assertNotIn("<intent_questions>", body)

    def test_debate_can_be_switched_off(self):
        self.config = {"critic_enabled": False, "intent_debate_rounds": 0}
        state = self.run_loop(loops=9)["state"]
        self.assertNotIn("intent_judge", self.kinds())
        self.assertIn("<intent_correction", self.sent_text())
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])

    def test_a_broken_judge_settles_rather_than_stalls(self):
        self.config = {"critic_enabled": False}
        self.judge_replies = ["not json", "still not json"]
        state = self.run_loop(loops=9)["state"]
        self.assertEqual(intent.ALIGNED, state["_intent"]["phase"])

    def test_the_judgement_is_tool_less(self):
        self.config = {"critic_enabled": False}
        self.run_loop(loops=9)
        judged = [c for c in self.calls
                  if c.get("task_kind") == "intent_judge"]
        self.assertTrue(all(c.get("tools_enabled") is False for c in judged))


class BranchTests(IntentLoopTestCase):
    """The judged task kind selects a workflow, and it reaches the prompt."""

    def setUp(self):
        super().setUp()
        self._tmp_home = tempfile.TemporaryDirectory()
        self._home_patch = mock.patch.object(
            agent_loop.paths, "LAINTAS_HOME", Path(self._tmp_home.name))
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        self._tmp_home.cleanup()
        super().tearDown()

    def _spec_reply(self, path):
        payload = json.loads(SPEC_REPLY)
        payload["branch_path"] = list(path)
        return json.dumps(payload, ensure_ascii=False)

    def test_the_path_pins_guidance_from_every_node_it_passed(self):
        self.config = {"critic_enabled": False, "branch_agents": "primary"}
        self.intent_replies = [self._spec_reply(["refactor", "in-place"])] * 2
        self.run_loop(loops=3, agent_id="primary")
        prompt = self.prompts()[-1]
        self.assertIn('path="Refactor → Restructure in place"', prompt)
        self.assertIn("Behaviour does not change", prompt)   # parent node
        self.assertIn("Map before you move", prompt)         # leaf

    def test_a_different_path_pins_different_guidance(self):
        self.config = {"critic_enabled": False, "branch_agents": "primary"}
        self.intent_replies = [self._spec_reply(["modify", "one-off"])] * 2
        self.run_loop(loops=3, agent_id="primary")
        prompt = self.prompts()[-1]
        self.assertIn('path="Change behaviour → One-off"', prompt)
        self.assertIn("Keep it where it is used", prompt)
        self.assertNotIn("Map before you move", prompt)

    def test_an_invented_step_is_truncated_not_followed(self):
        self.config = {"critic_enabled": False, "branch_agents": "primary"}
        self.intent_replies = [self._spec_reply(["refactor", "general"])] * 2
        self.run_loop(loops=3, agent_id="primary")
        prompt = self.prompts()[-1]
        self.assertIn('path="Refactor"', prompt)
        self.assertNotIn("General capability", prompt)
        self.assertNotIn("rule of three", prompt.lower())

    def test_an_empty_path_pins_nothing(self):
        self.config = {"critic_enabled": False, "branch_agents": "primary"}
        self.intent_replies = [self._spec_reply([])] * 2
        self.run_loop(loops=3, agent_id="primary")
        for prompt in self.prompts():
            self.assertNotIn("<task_branch", prompt)

    def test_an_agent_nobody_enabled_gets_no_workflow(self):
        # The default setting names scout, not the primary: a prescriptive
        # workflow suits a specialist and not the agent you talk to all day.
        self.config = {"critic_enabled": False}     # default: scout only
        self.intent_replies = [self._spec_reply(["refactor", "in-place"])] * 2
        self.run_loop(loops=3, agent_id="primary")
        for prompt in self.prompts():
            self.assertNotIn("<task_branch", prompt)

    def test_a_disabled_agent_is_not_asked_to_walk_the_tree(self):
        # Asking a model to place a request whose answer is then discarded is
        # a paragraph of prompt for nothing.
        self.config = {"critic_enabled": False}
        self.run_loop(loops=3, agent_id="primary")
        asked = [c for c in self.calls if c.get("task_kind") == "intent"]
        self.assertTrue(asked)
        self.assertNotIn("DECISION TREE", str(asked[0].get("messages")))

    def test_an_enabled_agent_is_given_the_tree(self):
        self.config = {"critic_enabled": False, "branch_agents": "*"}
        self.run_loop(loops=3, agent_id="primary")
        asked = [c for c in self.calls if c.get("task_kind") == "intent"]
        body = "".join(str(m.get("content") or "")
                       for m in asked[0].get("messages") or [])
        self.assertIn("DECISION TREE", body)
        self.assertIn('"refactor"', body)

    def test_a_user_tree_replaces_the_shipped_one(self):
        self.config = {"critic_enabled": False, "branch_agents": "*"}
        self.intent_replies = [self._spec_reply(["mine"])] * 2
        (Path(self._tmp_home.name) / "branches.json").write_text(json.dumps({
            "root": "root",
            "nodes": {"root": {"question": "?", "children": ["mine", "other"]},
                      "mine": {"label": "Mine", "guidance": "DO IT MY WAY"},
                      "other": {"label": "Other", "guidance": "x"}}}),
            encoding="utf-8")
        self.run_loop(loops=3, agent_id="primary")
        prompt = self.prompts()[-1]
        self.assertIn("DO IT MY WAY", prompt)
        self.assertNotIn("Map before you move", prompt)

    def test_the_branch_change_is_attributed_to_the_branch(self):
        # The prefix tripwire must be able to say which component moved, or
        # the one cache-invalidating change of the task is unexplained.
        self.config = {"critic_enabled": False, "branch_agents": "*"}
        self.intent_replies = [self._spec_reply(["refactor", "in-place"])] * 2
        state = self.run_loop(loops=3, agent_id="primary")["state"]
        self.assertIn("branch", state.get("_sys_prompt_churn_causes") or [])

    def test_the_decided_approach_reaches_the_progress_critic(self):
        self.config = {"critic_enabled": True, "critic_min_loop": 1,
                       "critic_interval": 1, "branch_agents": "*"}
        self.intent_replies = [self._spec_reply(["refactor", "in-place"])] * 2
        self.run_loop(loops=6, agent_id="primary")
        critic_calls = [c for c in self.calls if c.get("task_kind") == "critic"]
        self.assertTrue(any("Approach: Refactor" in str(c.get("messages"))
                            for c in critic_calls))
