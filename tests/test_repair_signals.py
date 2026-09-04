"""The user's reaction is captured as facts, never as a verdict.

What these lock in is the boundary the module exists to hold: it records what
can be pointed at in a transcript (who said what, how alike two turns are, how
the previous turn ended) and refuses to record what cannot be checked (a
satisfaction score). A later pass may label these rows; nothing in the hot
path may act on them.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paths
import repair_signals


def _thread(*turns):
    """A native message thread from (role, content) pairs."""
    return [{"role": role, "content": text} for role, text in turns]


class SimilarityTests(unittest.TestCase):
    def test_it_is_symmetric_and_bounded(self):
        for left, right in (("hello world", "hello there"),
                            ("别用中文注释", "注释都写英文"),
                            ("", "anything"), ("x", "x")):
            score = repair_signals.similarity(left, right)
            self.assertEqual(score, repair_signals.similarity(right, left))
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_reformatting_is_not_a_difference(self):
        self.assertEqual(1.0, repair_signals.similarity(
            "run  the\ttests", "run the tests"))

    def test_a_paraphrase_scores_low_which_is_the_point(self):
        """The finding that shaped this module: bigram overlap cannot see a
        restatement, so the score is a candidate finder and never a decision."""
        self.assertLess(
            repair_signals.similarity("别用中文注释", "注释都写英文"), 0.3)
        self.assertEqual(1.0, repair_signals.similarity("同一句话", "同一句话"))


class ThreadReadingTests(unittest.TestCase):
    def test_the_last_thing_the_agent_SAID_not_what_it_did(self):
        thread = _thread(("user", "go"), ("assistant", "the answer"),
                         ("assistant", ""))
        self.assertEqual("the answer",
                         repair_signals.last_assistant_text(thread))

    def test_malformed_history_yields_nothing_rather_than_raising(self):
        for junk in (None, "not a list", [None, 3, {"role": "user"}]):
            self.assertEqual([], repair_signals.user_turns(junk))
            self.assertEqual("", repair_signals.last_assistant_text(junk))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        project = Path(self.tmp.name)
        patch = mock.patch.object(paths, "project_dir", lambda: project)
        patch.start()
        self.addCleanup(patch.stop)
        enabled = mock.patch.object(repair_signals, "_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.path = project / repair_signals.SIGNALS_FILENAME

    def _rows(self):
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_the_first_turn_has_nothing_to_be_a_reaction_to(self):
        self.assertIsNone(repair_signals.on_user_turn(
            "do the thing", _thread(("user", "do the thing"))))
        self.assertEqual([], self._rows())

    def test_a_second_turn_is_recorded_with_both_sides(self):
        thread = _thread(("user", "别用中文注释"),
                         ("assistant", "done, comments are in Chinese"))
        row = repair_signals.on_user_turn("我说的是注释都写英文", thread)
        self.assertIsNotNone(row)
        self.assertEqual(1, row["turn_index"])
        self.assertIn("英文", row["user"])
        self.assertIn("Chinese", row["assistant_tail"])
        self.assertEqual([row], [{k: v for k, v in r.items()
                                  if k not in ("v", "kind", "ts")}
                                 for r in self._rows()])

    def test_a_turn_is_never_compared_with_itself(self):
        """The caller may already have appended the new turn to the thread."""
        thread = _thread(("user", "first"), ("assistant", "reply"),
                         ("user", "second"))
        row = repair_signals.on_user_turn("second", thread)
        self.assertEqual(1, row["turn_index"])
        self.assertEqual("first", row["prior_user"])
        self.assertLess(row["closest_prior_similarity"], 1.0)

    def test_no_score_and_no_verdict_is_ever_written(self):
        """Two observers agree on what was said; they do not agree on 41/100."""
        thread = _thread(("user", "a"), ("assistant", "b"))
        row = repair_signals.on_user_turn("c", thread)
        for banned in ("score", "satisfaction", "verdict", "label",
                       "is_dissatisfied", "rating", "lexical_negation"):
            self.assertNotIn(banned, row)

    def test_a_sub_agent_prompt_is_not_a_user_reaction(self):
        """On this machine's own history that would be 2571 of 2600 rows: the
        agent grading itself."""
        thread = _thread(("user", "a"), ("assistant", "b"))
        self.assertIsNone(
            repair_signals.on_user_turn("c", thread, agent_id="child-1"))
        self.assertEqual([], self._rows())

    def test_only_outcomes_that_need_no_judgement_are_recorded(self):
        for reason in ("interrupted", "aborted", "user_denied"):
            self.assertIsNotNone(repair_signals.on_turn_end(reason))
        for reason in ("completed", "end_turn", "max_loops", ""):
            self.assertIsNone(repair_signals.on_turn_end(reason))
        self.assertEqual(3, len(self._rows()))

    def test_capture_is_off_by_default(self):
        """Parity with mem_signals/precheck: opt-in, and silent when off."""
        with mock.patch.object(repair_signals, "_ENABLED", False):
            self.assertIsNotNone(repair_signals.on_user_turn(
                "c", _thread(("user", "a"), ("assistant", "b"))))
        self.assertEqual([], self._rows(),
                         "a returned row must not mean a written row")

    def test_a_broken_redactor_does_not_lose_the_turn(self):
        with mock.patch.object(repair_signals, "redactor") as broken:
            broken.scrub_text.side_effect = RuntimeError("boom")
            row = repair_signals.on_user_turn(
                "c", _thread(("user", "a"), ("assistant", "b")))
        self.assertEqual("c", row["user"])

    def test_an_unwritable_log_never_reaches_the_caller(self):
        with mock.patch.object(repair_signals, "_signals_path",
                               side_effect=OSError("read-only")):
            self.assertIsNotNone(repair_signals.on_user_turn(
                "c", _thread(("user", "a"), ("assistant", "b"))))


class ClassifierTests(unittest.TestCase):
    """One auxiliary call, and it may quote but never author."""

    USER = "我说的是注释都写英文，你还是写了中文"
    PRIOR = ["别用中文注释", "先跑一下测试"]
    ASSISTANT = "已完成，注释使用了中文以便阅读。"

    def _classify(self, reply):
        return repair_signals.classify(
            self.USER, self.ASSISTANT, self.PRIOR, lambda messages: reply)

    def test_a_quoted_verdict_survives(self):
        verdict = self._classify(
            '```json\n{"kind":"restated","later_anchor":"我说的是注释都写英文",'
            '"earlier_anchor":"别用中文注释","about":""}\n```')
        self.assertEqual("restated", verdict["kind"])
        self.assertEqual("别用中文注释", verdict["earlier_anchor"])

    def test_an_invented_quote_is_dropped_and_the_class_demoted(self):
        """The basis for trusting a small model here: it can point at the
        text, it cannot make text up."""
        verdict = self._classify(
            '{"kind":"restated","later_anchor":"我从没说过这句",'
            '"earlier_anchor":"也没说过","about":""}')
        self.assertEqual("unclear", verdict["kind"])
        self.assertEqual("", verdict["later_anchor"])
        self.assertEqual("", verdict["earlier_anchor"])

    def test_a_restatement_without_the_earlier_quote_is_not_a_restatement(self):
        verdict = self._classify(
            '{"kind":"restated","later_anchor":"我说的是注释都写英文",'
            '"earlier_anchor":"","about":""}')
        self.assertEqual("unclear", verdict["kind"])

    def test_an_unknown_class_is_refused_outright(self):
        self.assertIsNone(self._classify('{"kind":"angry"}'))

    def test_junk_a_crash_and_silence_all_yield_nothing(self):
        self.assertIsNone(self._classify("no json here"))
        self.assertIsNone(self._classify(""))
        self.assertIsNone(repair_signals.classify(
            self.USER, self.ASSISTANT, self.PRIOR,
            lambda messages: (_ for _ in ()).throw(RuntimeError("boom"))))
        self.assertIsNone(repair_signals.classify(
            self.USER, self.ASSISTANT, self.PRIOR, None))

    def test_adding_a_constraint_is_not_a_failure(self):
        """`refined` and `redirected` stay outside REPAIR_KINDS on purpose: a
        user who changes their mind is not a user the agent failed."""
        for kind in ("refined", "redirected", "proceeding"):
            self.assertNotIn(kind, repair_signals.REPAIR_KINDS)
        for kind in ("restated", "contradicted", "confused"):
            self.assertIn(kind, repair_signals.REPAIR_KINDS)

    def test_the_prompt_forbids_paraphrase_and_asks_for_no_score(self):
        prompt = repair_signals.SYSTEM_PROMPT
        self.assertIn("character-for-character", prompt)
        for banned in ("score", "0-100", "rate ", "confidence"):
            self.assertNotIn(banned, prompt.lower())

    def test_the_judge_sees_both_sides(self):
        messages = repair_signals.build_messages(
            self.USER, self.ASSISTANT, self.PRIOR)
        content = messages[0]["content"]
        self.assertIn(self.USER, content)
        self.assertIn(self.ASSISTANT, content)
        self.assertIn(self.PRIOR[0], content)


class VerdictRecordingTests(CaptureTests):
    def test_a_verdict_is_written_beside_the_facts(self):
        row = repair_signals.record_verdict(
            {"kind": "restated", "later_anchor": "a", "earlier_anchor": "b",
             "about": ""}, session_id="s", run_id="r", turn_index=3)
        self.assertTrue(row["is_repair"])
        written = self._rows()
        self.assertEqual(["verdict"], [r["kind"] for r in written])
        self.assertNotIn("score", written[0])

    def test_a_non_repair_class_is_still_recorded(self):
        """The negative cases are what a later pass measures precision on."""
        row = repair_signals.record_verdict({"kind": "refined",
                                             "later_anchor": "x"})
        self.assertFalse(row["is_repair"])

    def test_nothing_is_written_without_a_class(self):
        self.assertIsNone(repair_signals.record_verdict({}))
        self.assertIsNone(repair_signals.record_verdict(None))
        self.assertEqual([], self._rows())


class FailureVisibilityTests(CaptureTests):
    """A hook that raises must not vanish.

    `except Exception: pass` around a capture hook is how a capability goes
    missing for a whole release with nothing on screen to say so; this
    repository has the scar in `extension_runtime.register_tool`.
    """

    def test_a_raising_hook_leaves_a_record(self):
        repair_signals.note_failure("on_user_turn", ValueError("boom"))
        rows = self._rows()
        self.assertEqual(["error"], [r["kind"] for r in rows])
        self.assertEqual("on_user_turn", rows[0]["where"])
        self.assertIn("ValueError", rows[0]["error"])

    def test_it_cannot_itself_raise_from_inside_an_except_block(self):
        """It is called FROM an except block: raising there would replace a
        caught error with an uncaught one."""
        with mock.patch.object(repair_signals, "_record",
                               side_effect=RuntimeError("log is broken")):
            repair_signals.note_failure("anywhere", ValueError("boom"))

    def test_it_is_silent_when_capture_is_off(self):
        with mock.patch.object(repair_signals, "_ENABLED", False):
            repair_signals.note_failure("on_user_turn", ValueError("boom"))
        self.assertEqual([], self._rows())


class ReflectionTests(unittest.TestCase):
    """After a correction: state the divergence as facts, judge nobody."""

    USER = "I said comments in English, and they are still Chinese"
    PRIOR = ["no Chinese comments"]
    ASSISTANT = "Done. The comments are written in Chinese for readability."

    def _reflect(self, reply):
        return repair_signals.reflect(
            self.USER, self.ASSISTANT, self.PRIOR, lambda messages: reply)

    def test_both_quotes_must_be_real(self):
        reflection = self._reflect(
            '{"understood":"written in Chinese for readability",'
            '"asked":"no Chinese comments","analysis":"you treated a standing '
            'constraint as optional."}')
        self.assertEqual("written in Chinese for readability",
                         reflection["understood"])
        self.assertEqual("no Chinese comments", reflection["asked"])

    def test_a_reflection_with_no_real_quote_is_discarded(self):
        """With neither quote intact there is nothing factual left in it, and
        injecting the prose alone would be injecting an opinion."""
        self.assertIsNone(self._reflect(
            '{"understood":"never said this","asked":"nor this",'
            '"analysis":"the user was unclear"}'))

    def test_one_real_quote_is_enough_to_keep_it(self):
        reflection = self._reflect(
            '{"understood":"","asked":"no Chinese comments","analysis":"x"}')
        self.assertEqual("no Chinese comments", reflection["asked"])
        self.assertEqual("", reflection["understood"])

    def test_junk_a_crash_and_no_callable_all_yield_nothing(self):
        self.assertIsNone(self._reflect("not json"))
        self.assertIsNone(self._reflect(""))
        self.assertIsNone(repair_signals.reflect(
            self.USER, self.ASSISTANT, self.PRIOR,
            lambda m: (_ for _ in ()).throw(RuntimeError("boom"))))
        self.assertIsNone(repair_signals.reflect(
            self.USER, self.ASSISTANT, self.PRIOR, None))

    def test_the_prompt_forbids_judging_the_user(self):
        """The design this replaced had a component whose job was to argue the
        user might have misunderstood. Injected into the main context that is
        a licence to dismiss feedback, with no arbiter to overrule it."""
        prompt = repair_signals.REFLECTION_PROMPT.lower()
        self.assertIn("do not judge the user", prompt)
        self.assertIn("not about the person", prompt)
        self.assertIn("character-for-character", prompt)

    def test_the_note_carries_the_facts_and_no_score(self):
        note = repair_signals.format_note(
            {"kind": "restated"},
            {"understood": "u", "asked": "a", "analysis": "why"})
        self.assertIn("acted on", note)
        self.assertIn("actually asked", note)
        self.assertIn("why", note)
        for banned in ("score", "satisfaction", "/100"):
            self.assertNotIn(banned, note.lower())

    def test_no_reflection_means_no_note(self):
        self.assertEqual("", repair_signals.format_note({"kind": "restated"}, None))


class NoteDeliveryTests(unittest.TestCase):
    def setUp(self):
        repair_signals.clear_notes()
        self.addCleanup(repair_signals.clear_notes)

    def test_a_note_is_delivered_once(self):
        """Repeated every iteration it stops being guidance and becomes a
        standing accusation the model answers instead of working."""
        repair_signals.publish_note("run-1", "something happened")
        self.assertEqual("something happened", repair_signals.take_note("run-1"))
        self.assertEqual("", repair_signals.take_note("run-1"))

    def test_a_note_reaches_only_its_own_run(self):
        repair_signals.publish_note("run-1", "for one")
        self.assertEqual("", repair_signals.take_note("run-2"))
        self.assertEqual("for one", repair_signals.take_note("run-1"))

    def test_a_newer_correction_supersedes_an_unread_one(self):
        repair_signals.publish_note("run-1", "first")
        repair_signals.publish_note("run-1", "second")
        self.assertEqual("second", repair_signals.take_note("run-1"))

    def test_an_empty_note_is_never_published(self):
        repair_signals.publish_note("run-1", "   ")
        self.assertEqual("", repair_signals.take_note("run-1"))


class LoopWiringTests(unittest.TestCase):
    def test_the_loop_calls_it_and_survives_it_raising(self):
        source = Path(__file__).resolve().parents[1] / "agent_loop.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn("repair_signals.on_user_turn", text)
        self.assertIn("repair_signals.on_turn_end", text)
        self.assertIn("_launch_repair_classifier", text)
        # The one model call runs on a daemon thread: nothing in the turn reads
        # the verdict, so making the user wait for a network round trip on
        # every prompt from the second one onward would buy nothing.
        launcher = text[text.index("def _launch_repair_classifier"):]
        launcher = launcher[:launcher.index("\ndef ", 10)]
        self.assertIn("threading.Thread", launcher)
        self.assertIn("daemon=True", launcher)
        self.assertIn("tools_enabled=False", launcher)
        # It must not fire when capture is off, or an opt-in switch would
        # silently still cost one auxiliary call per turn.
        self.assertIn("repair_signals.enabled()", text)
        # No hook may be swallowed into silence.
        self.assertNotIn("""        repair_signals.on_turn_end(
            _exit_reason, session_id=_session_id or "",
            run_id=_run_id or "", agent_id=agent_id or "")
    except Exception:
        pass""", text)
        # Each hook and each stage of the background call reports its own
        # failure under its own name; a bare count would rot on the next edit.
        for where in ("on_user_turn", "on_turn_end", "launch_classifier",
                      "classify", "reflect"):
            self.assertIn(f'repair_signals.note_failure("{where}"', text)

    def test_the_note_reaches_the_running_turn_and_only_once(self):
        source = Path(__file__).resolve().parents[1] / "agent_loop.py"
        text = source.read_text(encoding="utf-8")
        # Published by the background worker, taken by the live-state tail the
        # turn rebuilds each iteration -- the route sub-agent results already
        # take when they land mid-task.
        self.assertIn("repair_signals.publish_note", text)
        self.assertIn('"repair_note": repair_signals.take_note(', text)
        self.assertIn('("user_correction", vol.get("repair_note"))', text)

    def test_reflection_runs_only_on_a_repair(self):
        """A refinement or a change of mind is not a failure; spending a
        second call on it would tell the model nothing went wrong."""
        source = Path(__file__).resolve().parents[1] / "agent_loop.py"
        text = source.read_text(encoding="utf-8")
        worker = text[text.index("def _launch_repair_classifier"):]
        worker = worker[:worker.index("\ndef ", 10)]
        guard = worker.index("repair_signals.REPAIR_KINDS")
        self.assertLess(guard, worker.index("repair_signals.reflect"))
        # Both hooks sit inside a bare try/except: a capture module must never
        # be able to end a user's turn.
        for hook in ("repair_signals.on_user_turn", "repair_signals.on_turn_end"):
            before = text[:text.index(hook)]
            self.assertTrue(before.rstrip().endswith("try:")
                            or "try:" in before[-400:],
                            f"{hook} is not guarded")


if __name__ == "__main__":
    unittest.main()
