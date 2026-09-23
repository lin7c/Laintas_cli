"""Paths that do the same job must share one implementation.

Each case here was two or more hand-written copies that had already drifted:
the prompt preview missed placeholders the agent loop fills, and /compact
persisted two of the three records every REPL exit path writes.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_loop
import laintas_cli


class RenderPropEffectiveTests(unittest.TestCase):

    def test_preview_fills_the_placeholders_the_loop_fills(self):
        out = laintas_cli._render_prop_effective(
            "rules={{durableRules}} inbox={{inbox}}", redact=False)
        self.assertNotIn("{{durableRules}}", out)
        self.assertIn(agent_loop._INBOX_POINTER, out)


class LabEffectivePromptTests(unittest.TestCase):

    def test_prefers_the_prompt_the_model_actually_received(self):
        conversation = {"calls": [{"system_prompt": "older"},
                                  {"system_prompt": "REAL PROMPT"}]}
        with mock.patch("context_snapshot.load_conversation",
                        return_value=conversation):
            self.assertEqual(agent_loop.captured_system_prompt("s1"), "REAL PROMPT")

    def test_no_capture_yields_empty_so_callers_fall_back(self):
        with mock.patch("context_snapshot.load_conversation",
                        side_effect=RuntimeError("no snapshot")):
            self.assertEqual(agent_loop.captured_system_prompt("s1"), "")

    def test_fallback_applies_the_lab_section_either_way(self):
        self.assertEqual(
            agent_loop.lab_prompt_fallback("A {{promptOpt}} B", "LAB"), "A LAB B")
        self.assertEqual(
            agent_loop.lab_prompt_fallback("A B", "LAB"), "A B\n\nLAB")
        self.assertEqual(agent_loop.lab_prompt_fallback("A B", ""), "A B")


class PersistSessionStateTests(unittest.TestCase):

    def test_writes_autosave_resume_state_and_live_record_together(self):
        with mock.patch.object(laintas_cli, "save_session_snapshot") as snapshot, \
                mock.patch.object(laintas_cli, "save_resume_state") as resume, \
                mock.patch.object(laintas_cli.session_store, "sync_runtime",
                                  return_value={"id": "synced"}) as sync:
            live = laintas_cli._persist_session_state(
                {"k": 1}, [], "/work", {"id": "live"}, tasks=[])
        snapshot.assert_called_once_with({"k": 1}, [], "/work")
        resume.assert_called_once_with(
            {"k": 1}, [], "/work",
            agent_id=laintas_cli.get_current_agent_id())
        sync.assert_called_once_with(
            {"id": "live"}, {"k": 1}, [], cwd="/work", tasks=[])
        self.assertEqual(live, {"id": "synced"})

    def test_without_a_live_session_only_the_files_are_written(self):
        with mock.patch.object(laintas_cli, "save_session_snapshot") as snapshot, \
                mock.patch.object(laintas_cli, "save_resume_state") as resume, \
                mock.patch.object(laintas_cli.session_store, "sync_runtime") as sync:
            live = laintas_cli._persist_session_state({}, [], "/work", None)
        snapshot.assert_called_once()
        resume.assert_called_once()
        sync.assert_not_called()
        self.assertIsNone(live)


if __name__ == "__main__":
    unittest.main()
