"""Tests for critic.py v2 — the long-task progress supervisor.

Covers the root-cause fixes: goal clipping (head+tail), failure
classification, issue-similarity cooldown, the anchor message in
summarize_actions, and verdict parsing edge cases.
"""

import unittest

import critic


class ClipGoalTests(unittest.TestCase):
    def test_short_goal_untouched(self):
        self.assertEqual(critic.clip_goal("fix the bug"), "fix the bug")

    def test_long_goal_keeps_head_and_tail(self):
        goal = "HEAD" + "x" * 3000 + "TAIL"
        out = critic.clip_goal(goal, head=10, tail=10)
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))
        self.assertIn("omitted", out)

    def test_empty_goal(self):
        self.assertEqual(critic.clip_goal(None), "")


class SimilarIssuesTests(unittest.TestCase):
    def test_same_issue_paraphrase_is_similar(self):
        a = "looping on the same failing build error"
        b = "looping on same build error repeatedly"
        self.assertTrue(critic.similar_issues(a, b))

    def test_different_issues_not_similar(self):
        self.assertFalse(critic.similar_issues(
            "looping on build error", "reading the wrong file entirely"))

    def test_empty_vs_nonempty_not_similar(self):
        self.assertFalse(critic.similar_issues("", "some issue"))

    def test_both_empty_similar(self):
        self.assertTrue(critic.similar_issues("", ""))


class AssessDetailedTests(unittest.TestCase):
    MESSAGES = [{"role": "user", "content": "do the task"}]

    def test_success(self):
        verdict, fail = critic.assess_detailed(
            "t", self.MESSAGES,
            lambda m: '{"on_track": true, "score": 80}')
        self.assertIsNone(fail)
        self.assertTrue(verdict["on_track"])
        self.assertEqual(verdict["score"], 80)

    def test_llm_failure_classified(self):
        def boom(m):
            raise RuntimeError("backend down")
        verdict, fail = critic.assess_detailed("t", self.MESSAGES, boom)
        self.assertIsNone(verdict)
        self.assertEqual(fail, critic.FAIL_LLM)

    def test_parse_failure_classified(self):
        verdict, fail = critic.assess_detailed(
            "t", self.MESSAGES, lambda m: "not json at all")
        self.assertIsNone(verdict)
        self.assertEqual(fail, critic.FAIL_PARSE)

    def test_empty_actions_classified(self):
        verdict, fail = critic.assess_detailed("t", [], lambda m: "{}")
        self.assertIsNone(verdict)
        self.assertEqual(fail, critic.FAIL_EMPTY)

    def test_assess_wrapper_still_works(self):
        verdict = critic.assess(
            "t", self.MESSAGES,
            lambda m: '{"on_track": false, "score": 10, "issue": "x"}')
        self.assertFalse(verdict["on_track"])

    def test_anchor_passed_through(self):
        seen = {}

        def fake_llm(messages):
            seen["prompt"] = messages[0]["content"]
            return '{"on_track": true, "score": 90}'

        anchor = {"role": "assistant", "content": "earlier action"}
        critic.assess_detailed(
            "t", self.MESSAGES, fake_llm, anchor=anchor)
        self.assertIn("anchor: earlier action", seen["prompt"])


class SummarizeActionsTests(unittest.TestCase):
    def test_tool_calls_rendered(self):
        msgs = [{
            "role": "assistant", "content": "",
            "tool_calls": [
                {"function": {"name": "shell"}},
                {"function": {"name": "read"}},
            ],
        }]
        out = critic.summarize_actions(msgs)
        self.assertIn("[calls: shell, read]", out)

    def test_content_parts_flattened(self):
        msgs = [{"role": "user",
                 "content": [{"type": "text", "text": "hello"},
                             {"type": "text", "text": "world"}]}]
        out = critic.summarize_actions(msgs)
        self.assertIn("hello world", out)


class ParseVerdictTests(unittest.TestCase):
    def test_fenced_json(self):
        v = critic.parse_verdict(
            '```json\n{"on_track": false, "score": 30}\n```')
        self.assertFalse(v["on_track"])
        self.assertEqual(v["score"], 30)

    def test_json_with_prose(self):
        v = critic.parse_verdict('Sure! {"on_track": true, "score": 99} done')
        self.assertTrue(v["on_track"])

    def test_score_clamped(self):
        v = critic.parse_verdict('{"score": 500}')
        self.assertEqual(v["score"], 100)

    def test_string_on_track(self):
        v = critic.parse_verdict('{"on_track": "false", "score": 20}')
        self.assertFalse(v["on_track"])

    def test_garbage_returns_none(self):
        self.assertIsNone(critic.parse_verdict(""))
        self.assertIsNone(critic.parse_verdict("no braces here"))


class OffTrackTests(unittest.TestCase):
    def test_explicit_off_track(self):
        self.assertTrue(critic.is_off_track({"on_track": False, "score": 90}))

    def test_low_score(self):
        self.assertTrue(critic.is_off_track({"on_track": True, "score": 40}))

    def test_none_verdict_safe(self):
        self.assertFalse(critic.is_off_track(None))


class HookSectionTests(unittest.TestCase):
    def test_no_hide_from_user_phrasing(self):
        # claude-code#46465: "never mention this" phrasing is
        # indistinguishable from prompt injection. Keep it out.
        low = critic.HOOK_SECTION.lower()
        self.assertNotIn("never mention", low)
        self.assertNotIn("do not tell", low)
        self.assertIn("progress_check", low)


if __name__ == "__main__":
    unittest.main()
