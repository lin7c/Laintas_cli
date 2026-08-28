"""The three untyped carriers in the runtime, and the checks that keep them honest.

`state`, a tool result, and an event record are all bare dicts. That is a
reasonable choice in Python right up to the point where a key is added in one
place and read in another, because nothing then connects the two: no import, no
signature, no type. Each of these tests exists because that gap has already
cost something.

Nothing here changes behaviour. What it buys is that the next gap is a test
failure, at the moment the author still knows which answer is right, instead of
a post-mortem months later.
"""
import re
import unittest
from pathlib import Path

import agent_loop
import event_log
import tools

_SOURCES = ("agent_loop.py", "tools.py", "laintas_cli.py", "hwo_runner.py",
            "hwg_runner.py", "branch.py", "agent_contract.py", "file_pager.py")


def _source() -> str:
    root = Path(__file__).resolve().parents[1]
    return "".join((root / name).read_text(encoding="utf-8") for name in _SOURCES)


class StateKeyTests(unittest.TestCase):
    """`prepare_state_for_repl` rebuilds state from a hand-written list.

    A key written anywhere else is silently dropped when the turn ends — no
    error at the write site, nothing in review, and a symptom ("this resets by
    itself") that points nowhere near the cause.
    """

    def test_every_state_key_in_the_code_is_declared(self):
        pattern = r'state(?:\[|\.get\(|\.setdefault\(|\.pop\()"(_[a-z_]+)"'
        used = set(re.findall(pattern, _source()))
        undeclared = sorted(used - agent_loop.declared_state_keys())
        self.assertEqual(
            [], undeclared,
            "add each of these to STATE_KEYS_CARRIED (the next turn needs it) "
            "or STATE_KEYS_TURN_ONLY (it must not leak) in agent_loop")

    def test_the_carried_set_matches_what_the_copy_actually_carries(self):
        """The declaration describes the code, or it is decoration."""
        carried = {k for k in agent_loop.prepare_state_for_repl({}) if k.startswith("_")}
        self.assertEqual(set(agent_loop.STATE_KEYS_CARRIED), carried)

    def test_the_two_sets_never_overlap(self):
        self.assertEqual(
            set(), agent_loop.STATE_KEYS_CARRIED & agent_loop.STATE_KEYS_TURN_ONLY)

    def test_a_turn_only_key_does_not_survive_the_turn(self):
        carried = agent_loop.prepare_state_for_repl(
            {"_contract": {"x": 1}, "_help_request": {"id": "r"},
             "_pager": {"a.py": {}}})
        self.assertNotIn("_contract", carried)
        self.assertNotIn("_help_request", carried)
        self.assertIn("_pager", carried)          # …and a carried one does


class ToolResultFlagTests(unittest.TestCase):
    """A tool result's underscore keys are its only out-of-band contract.

    `_page_ref` was renamed to `_read_ref` during the paging work while the
    consumer kept reading the old name. Nothing caught it: not the import
    system, not a signature, not a test — the feature simply stopped working
    and the tests still passed.
    """

    def test_every_underscore_key_a_tool_returns_is_registered(self):
        produced = set(re.findall(r'result\["(_[a-z_]+)"\]\s*=', _source()))
        produced |= set(re.findall(r'"(_[a-z_]+)":\s*(?:True|False)', _source()))
        unknown = sorted(produced - set(tools.RESULT_FLAGS))
        self.assertEqual([], unknown,
                         "document these in tools.RESULT_FLAGS, or fix the typo")

    def test_the_helper_names_an_unregistered_key(self):
        self.assertEqual(["_page_ref"],
                         tools.unknown_result_flags({"ok": True, "_page_ref": {}}))
        self.assertEqual([], tools.unknown_result_flags({"ok": True, "_read_ref": {}}))

    def test_every_registered_flag_is_actually_read_somewhere(self):
        """A registry that accumulates dead entries stops being a description."""
        source = _source()
        for flag in tools.RESULT_FLAGS:
            self.assertIn(flag, source, f"{flag} is documented but never used")


class EventSchemaTests(unittest.TestCase):
    """An event nobody can attribute answers no question.

    Nineteen `critic_assessment` records were written during one six-agent
    batch with no agent id on any of them, so "was this child supervised?" —
    the question the log exists for — could not be answered from the log.
    """

    def test_a_supervision_event_must_name_the_agent_it_judged(self):
        for name in ("critic_assessment", "contract_checked", "member_settled"):
            self.assertIn("agent_id", event_log.REQUIRED_FIELDS[name], name)

    def test_a_missing_field_is_reported_rather_than_silently_accepted(self):
        self.assertEqual(["agent_id", "run_id"],
                         event_log.schema_gaps("critic_assessment", {"score": 90}))
        self.assertEqual([], event_log.schema_gaps(
            "critic_assessment", {"agent_id": "a", "run_id": "r", "score": 90}))

    def test_an_incomplete_event_is_still_written_but_marked(self):
        """Losing the event is worse than logging an incomplete one."""
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                event_log.append("critic_assessment", score=90)
                written = [json.loads(line) for line in
                           open(Path(tmp) / ".laintas" / "events.jsonl")]
            finally:
                os.chdir(old)
        self.assertEqual(1, len(written))
        self.assertEqual(["agent_id", "run_id"], written[0]["_schema_gap"])
        self.assertIsNone(written[0]["agent_id"])

    def test_the_events_this_runtime_writes_are_all_described(self):
        emitted = set(re.findall(r'event_log\.append\(\s*"([a-z_]+)"', _source()))
        undescribed = sorted(
            name for name in emitted if name not in event_log.REQUIRED_FIELDS)
        # Not every event needs required fields, but every one should have been
        # considered. Listing the exceptions here is the consideration.
        allowed_without_requirements = {
            # Pure signals: the event type IS the information.
            "context_snapshot_failed", "critic_disabled", "critic_escalation",
            "critic_nudge_suppressed", "system_prompt_changed",
            "output_contract_violated", "tool_repeat_blocked",
        }
        self.assertEqual([], sorted(set(undescribed) - allowed_without_requirements))


if __name__ == "__main__":
    unittest.main()
