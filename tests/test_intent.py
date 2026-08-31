"""Intent alignment: anchoring, parsing, rendering, and the phase machine.

The gate that matters here is ``validate_spec``. Everything downstream — an
authoritative system-prompt section, a correction that declares work void —
is only safe because a requirement cannot enter the spec without quoting the
user. These tests exist to keep that gate mechanical rather than aspirational.
"""
import unittest

import intent


TASK = ("我想做一个像 ChatGPT 那样的网站，要能多轮对话，"
        "并且左边有会话列表。不要做移动端 App。")


def _spec(**over):
    base = {
        "goal": "做一个对话网站",
        "requirements": [
            {"id": "R1", "text": "多轮对话", "anchor": "要能多轮对话"},
            {"id": "R2", "text": "会话列表在左侧", "anchor": "左边有会话列表"},
        ],
        "out_of_scope": ["移动端 App"],
        "deliverables": ["一个可访问的网站"],
        "open_questions": [
            {"id": "Q1", "q": "ChatGPT 的界面有哪些必备元素?",
             "needs": "evidence", "why": "决定首页布局"},
            {"id": "Q2", "q": "要接哪个模型?", "needs": "user", "why": "决定后端"},
        ],
        "assumptions": [{"id": "A1", "text": "面向桌面浏览器", "risk": "high"}],
        "task_breakdown": ["搭前端骨架", "接流式接口"],
    }
    base.update(over)
    return base


class AnchoringTests(unittest.TestCase):
    def test_verbatim_anchor_is_accepted(self):
        self.assertTrue(intent.is_anchored("要能多轮对话", TASK))

    def test_whitespace_and_case_differences_still_count_as_quoting(self):
        task = "Build a  DASHBOARD\nwith live charts"
        self.assertTrue(intent.is_anchored("dashboard with live charts", task))

    def test_invented_anchor_is_rejected(self):
        self.assertFalse(intent.is_anchored("要支持语音输入", TASK))

    def test_too_short_anchor_is_rejected(self):
        # A one-character "quote" matches almost anything; treating it as
        # evidence would defeat the whole gate.
        self.assertFalse(intent.is_anchored("网", TASK))

    def test_empty_anchor_is_rejected(self):
        self.assertFalse(intent.is_anchored("", TASK))


class ValidateSpecTests(unittest.TestCase):
    def test_anchored_requirements_survive(self):
        spec = intent.validate_spec(_spec(), TASK)
        self.assertEqual(["R1", "R2"], [r["id"] for r in spec["requirements"]])
        self.assertEqual(0, spec["dropped_anchors"])

    def test_unanchored_requirement_is_dropped_and_counted(self):
        raw = _spec(requirements=[
            {"id": "R1", "text": "多轮对话", "anchor": "要能多轮对话"},
            {"id": "R2", "text": "语音输入", "anchor": "支持语音输入"},
        ])
        spec = intent.validate_spec(raw, TASK)
        self.assertEqual(["R1"], [r["id"] for r in spec["requirements"]])
        self.assertEqual(1, spec["dropped_anchors"])

    def test_spec_with_no_surviving_requirement_is_not_usable(self):
        raw = _spec(goal="", requirements=[
            {"id": "R1", "text": "语音输入", "anchor": "支持语音输入"}])
        spec = intent.validate_spec(raw, TASK)
        self.assertFalse(intent.is_usable(spec))
        self.assertEqual("", intent.render_understanding(spec))

    def test_garbage_input_yields_an_empty_spec_not_an_exception(self):
        for bad in (None, [], "text", 3):
            spec = intent.validate_spec(bad, TASK)
            self.assertEqual([], spec["requirements"])
            self.assertFalse(intent.is_usable(spec))

    def test_duplicate_ids_are_disambiguated(self):
        raw = _spec(requirements=[
            {"id": "R1", "text": "多轮对话", "anchor": "要能多轮对话"},
            {"id": "R1", "text": "会话列表", "anchor": "左边有会话列表"},
        ])
        spec = intent.validate_spec(raw, TASK)
        ids = [r["id"] for r in spec["requirements"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_requirements_are_capped(self):
        raw = _spec(requirements=[
            {"id": f"R{i}", "text": f"需求{i}", "anchor": "要能多轮对话"}
            for i in range(40)])
        spec = intent.validate_spec(raw, TASK)
        self.assertEqual(intent.MAX_REQUIREMENTS, len(spec["requirements"]))

    def test_unknown_needs_value_falls_back_to_asking_the_user(self):
        raw = _spec(open_questions=[{"id": "Q1", "q": "?", "needs": "vibes"}])
        spec = intent.validate_spec(raw, TASK)
        self.assertEqual(intent.NEEDS_USER, spec["open_questions"][0]["needs"])


class SelfAskTests(unittest.TestCase):
    def test_round_parses_and_validates(self):
        import json
        spec, fail = intent.self_ask_round(
            TASK, None, lambda m: json.dumps(_spec(), ensure_ascii=False))
        self.assertIsNone(fail)
        self.assertEqual(2, len(spec["requirements"]))
        self.assertEqual(1, spec["round"])

    def test_fenced_json_is_accepted(self):
        import json
        reply = "```json\n" + json.dumps(_spec(), ensure_ascii=False) + "\n```"
        spec, fail = intent.self_ask_round(TASK, None, lambda m: reply)
        self.assertIsNone(fail)
        self.assertTrue(intent.is_usable(spec))

    def test_llm_error_is_reported_not_raised(self):
        def boom(_messages):
            raise RuntimeError("upstream down")
        spec, fail = intent.self_ask_round(TASK, None, boom)
        self.assertIsNone(spec)
        self.assertEqual(intent.FAIL_LLM, fail)

    def test_unparseable_reply_is_reported_as_parse_failure(self):
        spec, fail = intent.self_ask_round(TASK, None, lambda m: "I think...")
        self.assertIsNone(spec)
        self.assertEqual(intent.FAIL_PARSE, fail)

    def test_empty_task_is_refused(self):
        spec, fail = intent.self_ask_round("   ", None, lambda m: "{}")
        self.assertIsNone(spec)
        self.assertEqual(intent.FAIL_EMPTY, fail)

    def test_later_round_sees_the_earlier_spec(self):
        import json
        seen = []

        def fn(messages):
            seen.append(messages[0]["content"])
            return json.dumps(_spec(), ensure_ascii=False)

        intent.build_spec(TASK, fn, rounds=2)
        self.assertEqual(2, len(seen))
        self.assertNotIn("SPEC SO FAR", seen[0])
        self.assertIn("SPEC SO FAR", seen[1])

    def test_a_failing_later_round_keeps_the_earlier_spec(self):
        import json
        calls = {"n": 0}

        def fn(_messages):
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps(_spec(), ensure_ascii=False)
            return "garbage"

        spec, fail = intent.build_spec(TASK, fn, rounds=3)
        self.assertIsNone(fail)
        self.assertEqual(1, spec["round"])
        self.assertTrue(intent.is_usable(spec))

    def test_all_rounds_failing_reports_the_failure(self):
        spec, fail = intent.build_spec(TASK, lambda m: "nope", rounds=2)
        self.assertIsNone(spec)
        self.assertEqual(intent.FAIL_PARSE, fail)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.spec = intent.validate_spec(_spec(), TASK)
        self.actions = "[step 3] assistant: [calls: write(path=src/tool.tsx)]"

    def _run(self, payload):
        import json
        return intent.compare(self.spec, self.actions,
                              lambda m: json.dumps(payload, ensure_ascii=False))

    def test_scope_error_keeps_void_steps(self):
        result, fail = self._run({
            "severity": "scope_error", "aligned": False,
            "divergences": [{"req_id": "R1", "steps": [3],
                             "what": "building a single-page tool", "why": "no chat"}],
            "void_steps": [3], "missing": ["R2"], "next": "重新按会话式界面开始"})
        self.assertIsNone(fail)
        self.assertEqual([3], result["void_steps"])
        self.assertFalse(result["aligned"])

    def test_scope_error_cannot_claim_alignment(self):
        result, _ = self._run({"severity": "scope_error", "aligned": True,
                               "divergences": [], "void_steps": [2]})
        self.assertFalse(result["aligned"])

    def test_detail_gap_never_voids_work(self):
        # Undoing correct work over a missing detail is the expensive mistake
        # this branch exists to prevent.
        result, _ = self._run({"severity": "detail_gap", "aligned": True,
                               "divergences": [], "void_steps": [1, 2, 3]})
        self.assertEqual([], result["void_steps"])

    def test_unknown_severity_degrades_to_detail_gap(self):
        result, _ = self._run({"severity": "catastrophe", "aligned": False})
        self.assertEqual(intent.DETAIL_GAP, result["severity"])

    def test_unusable_spec_is_refused(self):
        result, fail = intent.compare({}, self.actions, lambda m: "{}")
        self.assertIsNone(result)
        self.assertEqual(intent.FAIL_EMPTY, fail)

    def test_empty_actions_are_refused(self):
        result, fail = intent.compare(self.spec, "  ", lambda m: "{}")
        self.assertIsNone(result)
        self.assertEqual(intent.FAIL_EMPTY, fail)

    def test_llm_error_is_reported(self):
        def boom(_m):
            raise OSError("no route to host")
        result, fail = intent.compare(self.spec, self.actions, boom)
        self.assertIsNone(result)
        self.assertEqual(intent.FAIL_LLM, fail)


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.spec = intent.validate_spec(_spec(), TASK)
        self.comparison = {"severity": "scope_error", "divergences": [
            {"req_id": "R1", "steps": [3], "what": "x", "why": "y"}]}

    def _judge(self, payload):
        import json
        return intent.judge_rebuttal(
            TASK, self.spec, self.comparison, "我认为规格误读了",
            lambda m: json.dumps(payload, ensure_ascii=False))

    def test_main_right_carries_a_spec_fix(self):
        verdict, fail = self._judge({
            "verdict": "main_right", "reason": "请求里没有这句",
            "spec_fix": {"drop": ["R2"], "add": [
                {"id": "R9", "text": "不做移动端", "anchor": "不要做移动端 App"}]}})
        self.assertIsNone(fail)
        self.assertEqual(["R2"], verdict["drop"])
        self.assertEqual(1, len(verdict["add"]))

    def test_unknown_verdict_degrades_to_unresolved(self):
        verdict, _ = self._judge({"verdict": "obviously_me"})
        self.assertEqual("unresolved", verdict["verdict"])

    def test_only_unresolved_produces_a_user_question(self):
        verdict, _ = self._judge({"verdict": "critic_right",
                                  "user_question": "要接哪个模型?"})
        self.assertEqual("", verdict["user_question"])
        verdict, _ = self._judge({"verdict": "unresolved",
                                  "user_question": "要接哪个模型?"})
        self.assertEqual("要接哪个模型?", verdict["user_question"])

    def test_empty_rebuttal_is_refused(self):
        verdict, fail = intent.judge_rebuttal(
            TASK, self.spec, self.comparison, "", lambda m: "{}")
        self.assertIsNone(verdict)
        self.assertEqual(intent.FAIL_EMPTY, fail)


class ApplyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.spec = intent.validate_spec(_spec(), TASK)

    def test_losing_the_argument_rewrites_the_spec(self):
        updated = intent.apply_resolution(self.spec, {
            "verdict": "main_right", "drop": ["R1"],
            "add": [{"id": "R9", "text": "不做移动端", "anchor": "不要做移动端 App"}],
        }, TASK)
        ids = [r["id"] for r in updated["requirements"]]
        self.assertNotIn("R1", ids)
        self.assertIn("R9", ids)
        self.assertEqual(self.spec["spec_version"] + 1, updated["spec_version"])

    def test_added_requirement_must_still_quote_the_request(self):
        updated = intent.apply_resolution(self.spec, {
            "verdict": "main_right", "drop": [],
            "add": [{"id": "R9", "text": "语音输入", "anchor": "支持语音输入"}],
        }, TASK)
        self.assertNotIn("R9", [r["id"] for r in updated["requirements"]])
        self.assertEqual(1, updated["dropped_anchors"])

    def test_other_verdicts_leave_the_spec_alone(self):
        for verdict in ("critic_right", "unresolved"):
            updated = intent.apply_resolution(
                self.spec, {"verdict": verdict, "drop": ["R1"]}, TASK)
            self.assertEqual(self.spec, updated)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.spec = intent.validate_spec(_spec(), TASK)

    def test_understanding_is_marked_authoritative_and_versioned(self):
        out = intent.render_understanding(self.spec)
        self.assertIn('<task_understanding authoritative="true" version="1">', out)
        self.assertIn("多轮对话", out)
        self.assertIn("</task_understanding>", out)

    def test_understanding_is_empty_for_an_empty_spec(self):
        self.assertEqual("", intent.render_understanding(None))
        self.assertEqual("", intent.render_understanding({}))

    def test_questions_block_only_carries_evidence_questions(self):
        out = intent.render_questions(self.spec)
        self.assertIn("Q1", out)
        self.assertNotIn("Q2", out)      # needs=user goes to the user, not here

    def test_questions_block_is_empty_without_evidence_questions(self):
        spec = intent.validate_spec(_spec(open_questions=[
            {"id": "Q2", "q": "要接哪个模型?", "needs": "user"}]), TASK)
        self.assertEqual("", intent.render_questions(spec))

    def test_correction_names_void_steps_and_new_files(self):
        out = intent.render_correction(
            {"severity": "scope_error",
             "divergences": [{"req_id": "R1", "steps": [3, 4],
                              "what": "建成了单页工具", "why": "没有会话"}],
             "void_steps": [3, 4], "next": "改成会话式布局"},
            self.spec,
            {"added": ["src/tool.tsx"], "modified": ["index.html"],
             "deleted": []})
        self.assertIn("steps 3, 4", out)
        self.assertIn("Steps considered void: 3, 4", out)
        self.assertIn("src/tool.tsx", out)
        # The surprising half of undo has to be said out loud.
        self.assertIn("would not remove newly created files", out)
        self.assertIn("改成会话式布局", out)

    def test_correction_without_file_data_still_renders(self):
        out = intent.render_correction(
            {"severity": "scope_error", "divergences": [], "void_steps": []},
            self.spec, None)
        self.assertIn("<intent_correction", out)
        self.assertNotIn("Files touched", out)

    def test_challenge_invites_a_quoted_rebuttal(self):
        out = intent.render_challenge(
            {"divergences": [{"req_id": "R1", "steps": [3], "what": "x"}]}, 2)
        self.assertIn('round="2"', out)
        self.assertIn("quote the part of the request", out)

    def test_challenge_is_empty_with_nothing_to_dispute(self):
        self.assertEqual("", intent.render_challenge({"divergences": []}))

    def test_escalation_falls_back_to_a_pending_user_question(self):
        self.assertEqual("要接哪个模型?",
                         intent.render_escalation(self.spec, {"verdict": "unresolved"}))

    def test_contract_text_feeds_the_progress_critic(self):
        import critic
        text = intent.to_contract_text(self.spec)
        messages = critic.build_messages("原始那句话", "[step 1] assistant: hi", text)
        self.assertIn("多轮对话", messages[0]["content"])
        self.assertIn("THE CHILD OWES THIS CONTRACT", messages[0]["content"])


class ShouldStartTests(unittest.TestCase):
    def _call(self, **over):
        kwargs = {"thread_mode": True, "enabled": True, "has_contract": False,
                  "plan_mode": False, "min_chars": 40}
        kwargs.update(over)
        return intent.should_start(TASK, **kwargs)

    def test_runs_on_a_long_thread_mode_task(self):
        self.assertTrue(self._call())

    def test_skipped_when_disabled_or_not_thread_mode(self):
        self.assertFalse(self._call(enabled=False))
        self.assertFalse(self._call(thread_mode=False))

    def test_skipped_for_a_contracted_child(self):
        # The contract already is the spec; two authorities would fight.
        self.assertFalse(self._call(has_contract=True))

    def test_skipped_in_plan_mode(self):
        self.assertFalse(self._call(plan_mode=True))

    def test_skipped_for_a_short_instruction(self):
        self.assertFalse(intent.should_start(
            "ls", thread_mode=True, enabled=True, has_contract=False,
            plan_mode=False, min_chars=40))


class PhaseMachineTests(unittest.TestCase):
    def test_happy_path_reaches_aligned(self):
        state = intent.new_state()
        self.assertEqual(intent.IDLE, state["phase"])
        state["phase"] = intent.next_phase(state, "spec_ready")
        self.assertEqual(intent.SPEC_READY, state["phase"])
        state["phase"] = intent.next_phase(state, "aligned")
        self.assertEqual(intent.ALIGNED, state["phase"])
        self.assertTrue(intent.is_settled(state))

    def test_divergence_then_concession(self):
        state = intent.new_state()
        state["phase"] = intent.next_phase(state, "diverged")
        self.assertEqual(intent.CORRECTING, state["phase"])
        state["phase"] = intent.next_phase(state, "conceded")
        self.assertEqual(intent.ALIGNED, state["phase"])

    def test_dispute_enters_debate_then_escalates_at_the_budget(self):
        state = intent.new_state()
        state["phase"] = intent.next_phase(state, "disputed", max_debate_rounds=2)
        self.assertEqual(intent.DEBATING, state["phase"])
        state["debate_round"] = 2
        state["phase"] = intent.next_phase(state, "disputed", max_debate_rounds=2)
        self.assertEqual(intent.ESCALATED, state["phase"])
        self.assertTrue(intent.is_settled(state))

    def test_unresolved_cannot_loop_forever(self):
        state = intent.new_state()
        for round_index in range(6):
            state["debate_round"] = round_index
            state["phase"] = intent.next_phase(
                state, "unresolved", max_debate_rounds=2)
        self.assertEqual(intent.ESCALATED, state["phase"])

    def test_zero_debate_budget_escalates_immediately(self):
        state = intent.new_state()
        state["phase"] = intent.next_phase(state, "disputed", max_debate_rounds=0)
        self.assertEqual(intent.ESCALATED, state["phase"])

    def test_failures_disable_rather_than_stall(self):
        state = intent.new_state()
        state["phase"] = intent.next_phase(state, "spec_failed")
        self.assertEqual(intent.DISABLED, state["phase"])
        self.assertTrue(intent.is_settled(state))

    def test_unknown_event_is_a_no_op(self):
        state = {"phase": intent.SPEC_READY}
        self.assertEqual(intent.SPEC_READY,
                         intent.next_phase(state, "sneeze"))


class HookSectionTests(unittest.TestCase):
    def test_names_every_block_the_loop_can_inject(self):
        for tag in ("<intent_questions>", "<intent_correction>",
                    "<intent_challenge>", "<task_understanding>"):
            self.assertIn(tag, intent.HOOK_SECTION)

    def test_does_not_ask_the_model_to_hide_anything(self):
        # Same reasoning as critic.HOOK_SECTION: a "never mention this"
        # instruction is indistinguishable from prompt injection.
        lowered = intent.HOOK_SECTION.lower()
        for phrase in ("do not tell", "never mention", "don't tell",
                       "without telling"):
            self.assertNotIn(phrase, lowered)


if __name__ == "__main__":
    unittest.main()
