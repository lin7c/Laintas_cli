"""Task branches: which workflow gets pinned, for whom, and when not to.

A refactor and a fix are different jobs. Given one set of instructions for
both, a model does the average — mapping the repository to change three lines,
or renaming a function without looking for the callers. These tests guard the
three ways that can go wrong: the wrong branch, a branch for an agent nobody
enabled, and a branch chosen from a guess.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import branches
import intent
import paths


class _BranchCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patch = mock.patch.object(paths, "LAINTAS_HOME", self.home)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, filename, text):
        directory = branches.branches_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(text, encoding="utf-8")
        return path


class SelectionTests(_BranchCase):
    def test_each_kind_selects_its_own_workflow(self):
        for kind in ("refactor", "modify"):
            branch = branches.select(kind, agent_id="scout", allowed="scout")
            self.assertIsNotNone(branch)
            self.assertEqual(kind, branch.when)

    def test_unclear_selects_nothing(self):
        """A workflow chosen by a coin flip is worse than none — it is
        followed with confidence."""
        self.assertIsNone(
            branches.select("unclear", agent_id="scout", allowed="scout"))
        self.assertIsNone(
            branches.select("", agent_id="scout", allowed="scout"))
        self.assertIsNone(
            branches.select("something-else", agent_id="scout", allowed="scout"))

    def test_an_agent_nobody_enabled_gets_nothing(self):
        self.assertIsNone(
            branches.select("refactor", agent_id="primary", agent_name="primary",
                            allowed="scout"))

    def test_matching_is_by_id_or_name_and_ignores_case(self):
        self.assertIsNotNone(branches.select(
            "modify", agent_id="AI-2", agent_name="Scout", allowed="scout"))
        self.assertIsNotNone(branches.select(
            "modify", agent_id="AI-2", agent_name="x", allowed="ai-2"))

    def test_a_star_enables_everyone(self):
        self.assertIsNotNone(
            branches.select("modify", agent_id="primary", allowed="*"))

    def test_an_empty_setting_disables_everyone(self):
        self.assertIsNone(
            branches.select("modify", agent_id="scout", allowed=""))
        self.assertIsNone(
            branches.select("modify", agent_id="scout", allowed="  , "))


class UserOverrideTests(_BranchCase):
    def test_a_user_file_replaces_the_builtin_of_that_name(self):
        """Replaced, not merged: appending the built-in underneath would leave
        the user arguing with instructions they thought they had deleted."""
        self._write("refactor.md",
                    "---\nname: refactor\nwhen: refactor\n---\n\nMY WORKFLOW\n")
        branch = branches.select("refactor", agent_id="scout", allowed="scout")
        self.assertEqual("MY WORKFLOW", branch.body)
        self.assertNotIn("Map before you move", branch.body)

    def test_a_new_name_adds_a_branch(self):
        self._write("careful.md",
                    "---\nname: careful\nwhen: modify\n---\n\nGO SLOWLY\n")
        names = [b.name for b in branches.load_all()]
        self.assertIn("careful", names)
        self.assertIn("refactor", names)

    def test_applies_to_narrows_but_never_widens(self):
        # Dropping a file into the directory must not switch the behaviour on
        # for an agent the user did not enable.
        self._write("refactor.md",
                    "---\nname: refactor\nwhen: refactor\n"
                    "applies_to: [primary]\n---\n\nX\n")
        self.assertIsNone(branches.select(
            "refactor", agent_id="primary", allowed="scout"))
        self.assertIsNone(branches.select(
            "refactor", agent_id="scout", allowed="scout"))

    def test_applies_to_accepts_a_yaml_list(self):
        self._write("refactor.md",
                    "---\nname: refactor\nwhen: refactor\napplies_to:\n"
                    "  - scout\n  - AI-2\n---\n\nX\n")
        self.assertIsNotNone(branches.select(
            "refactor", agent_id="scout", allowed="scout"))

    def test_an_unknown_when_falls_back_to_matching_anything(self):
        self._write("odd.md", "---\nname: odd\nwhen: sideways\n---\n\nX\n")
        branch = next(b for b in branches.load_all() if b.name == "odd")
        self.assertEqual(branches.ANY, branch.when)

    def test_a_body_less_file_is_skipped(self):
        self._write("empty.md", "---\nname: empty\nwhen: modify\n---\n\n\n")
        self.assertNotIn("empty", [b.name for b in branches.load_all()])

    def test_an_unreadable_file_does_not_break_the_others(self):
        path = self._write("broken.md", "---\nname: broken\n---\n\nX\n")
        with mock.patch.object(Path, "read_text",
                               side_effect=OSError("gone")):
            names = [b.name for b in branches.load_all()]
        self.assertIn("refactor", names)
        self.assertNotIn("broken", names)
        self.assertTrue(path.exists())

    def test_a_hostile_name_is_rejected(self):
        self._write("bad.md", "---\nname: ../../escape\nwhen: modify\n---\n\nX\n")
        self.assertNotIn("../../escape", [b.name for b in branches.load_all()])

    def test_no_directory_at_all_still_gives_the_builtins(self):
        self.assertEqual(["modify", "refactor"],
                         [b.name for b in branches.load_all()])


class DefaultFileTests(_BranchCase):
    def test_the_builtins_are_written_so_they_can_be_edited(self):
        created = branches.write_default_files()
        self.assertEqual({"modify.md", "refactor.md"},
                         {p.name for p in created})
        text = (branches.branches_dir() / "refactor.md").read_text("utf-8")
        self.assertIn("when: refactor", text)
        self.assertIn("Map before you move", text)

    def test_an_edited_file_is_never_overwritten(self):
        # Pushed defaults overwriting user edits is a mistake this codebase
        # has already paid for once.
        branches.write_default_files()
        path = branches.branches_dir() / "refactor.md"
        path.write_text("---\nname: refactor\n---\n\nMINE\n", encoding="utf-8")
        self.assertEqual([], branches.write_default_files())
        self.assertIn("MINE", path.read_text("utf-8"))

    def test_writing_the_defaults_round_trips_through_the_parser(self):
        branches.write_default_files()
        loaded = {b.name: b for b in branches.load_all()}
        self.assertEqual("refactor", loaded["refactor"].when)
        self.assertNotEqual("builtin", loaded["refactor"].source)
        self.assertIn("Map before you move", loaded["refactor"].body)

    def test_an_unwritable_home_is_not_an_error(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            self.assertEqual([], branches.write_default_files())


class RenderTests(_BranchCase):
    def test_the_section_names_the_kind_and_the_branch(self):
        branch = branches.select("refactor", agent_id="scout", allowed="scout")
        out = branches.render(branch)
        self.assertIn('<task_branch kind="refactor" name="refactor">', out)
        self.assertIn("</task_branch>", out)

    def test_nothing_renders_to_nothing(self):
        self.assertEqual("", branches.render(None))
        self.assertEqual("", branches.render(branches.Branch(name="x", body="  ")))


class TaskKindTests(unittest.TestCase):
    """The classification itself lives on the intent spec."""

    TASK = "把这个模块重构一下，行为不要变"

    def test_a_valid_kind_survives_validation(self):
        for kind in ("refactor", "modify", "unclear"):
            spec = intent.validate_spec({"goal": "g", "task_kind": kind}, self.TASK)
            self.assertEqual(kind, spec["task_kind"])

    def test_anything_else_is_unclear(self):
        """A default of refactor or modify would turn a parse slip into a
        confidently wrong workflow."""
        for value in ("rewrite", "", None, 7, "REFACTOR "):
            spec = intent.validate_spec({"goal": "g", "task_kind": value},
                                        self.TASK)
            self.assertIn(spec["task_kind"],
                          ("refactor", "unclear"))     # only a real match wins
        self.assertEqual("unclear", intent.validate_spec(
            {"goal": "g", "task_kind": "rewrite"}, self.TASK)["task_kind"])

    def test_a_missing_field_is_unclear(self):
        spec = intent.validate_spec({"goal": "g"}, self.TASK)
        self.assertEqual("unclear", spec["task_kind"])

    def test_case_and_padding_are_tolerated(self):
        spec = intent.validate_spec({"goal": "g", "task_kind": " Refactor "},
                                    self.TASK)
        self.assertEqual("refactor", spec["task_kind"])

    def test_the_kind_reaches_the_progress_critic(self):
        # The contract the later assessments judge against should carry it:
        # "is this on track" means something different for a refactor.
        spec = intent.validate_spec({
            "goal": "g", "task_kind": "refactor",
            "requirements": [{"id": "R1", "text": "行为不变",
                              "anchor": "行为不要变"}]}, self.TASK)
        self.assertIn("Kind: refactor", intent.to_contract_text(spec))

    def test_an_unclear_kind_is_not_stated_as_a_fact(self):
        spec = intent.validate_spec({
            "goal": "g",
            "requirements": [{"id": "R1", "text": "行为不变",
                              "anchor": "行为不要变"}]}, self.TASK)
        self.assertNotIn("Kind:", intent.to_contract_text(spec))

    def test_the_self_ask_prompt_asks_for_it(self):
        self.assertIn("task_kind", intent.SELF_ASK_SYSTEM)
        self.assertIn("unclear", intent.SELF_ASK_SYSTEM)


if __name__ == "__main__":
    unittest.main()
