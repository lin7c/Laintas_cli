"""The task decision tree: the walk, its validation, and what it renders.

Three ways this goes wrong, and one test class each: a path the model invented,
a tree the user broke, and a branch reaching an agent nobody enabled.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import branches
import intent
import paths


class _TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patch = mock.patch.object(paths, "LAINTAS_HOME", self.home)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, data):
        path = branches.tree_path()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def _ids(self, path, **kwargs):
        tree = kwargs.pop("tree", None) or branches.load_tree()
        return [node.id for node in branches.walk(tree, path, **kwargs)]


class DefaultTreeTests(_TreeCase):
    def test_the_shipped_tree_is_well_formed(self):
        tree = branches.load_tree()
        self.assertTrue(tree.usable)
        self.assertEqual((), tree.problems)

    def test_every_question_has_exactly_two_live_children(self):
        tree = branches.load_tree()
        for node in tree.nodes.values():
            if node.question:
                self.assertEqual(2, len(node.children), node.id)
                for child in node.children:
                    self.assertIn(child, tree.nodes)

    def test_the_four_paths_all_walk(self):
        for path in (["refactor", "adopt-existing"], ["refactor", "in-place"],
                     ["modify", "general"], ["modify", "one-off"]):
            self.assertEqual(path, self._ids(path))

    def test_every_leaf_carries_guidance(self):
        tree = branches.load_tree()
        for node in tree.nodes.values():
            if node.is_leaf and node.id != tree.root:
                self.assertTrue(node.guidance.strip(), node.id)


class WalkTests(_TreeCase):
    def test_a_partial_path_is_allowed(self):
        # Stopping early is a real answer: the guidance above still applies.
        self.assertEqual(["refactor"], self._ids(["refactor"]))

    def test_a_step_that_is_not_a_child_truncates_the_path(self):
        """An id that is not a child of the previous node is invented. The
        part that walked cleanly still applies; the rest does not speak."""
        self.assertEqual(["refactor"], self._ids(["refactor", "general"]))

    def test_an_unknown_first_step_yields_nothing(self):
        self.assertEqual([], self._ids(["made-up"]))

    def test_an_empty_path_yields_nothing(self):
        self.assertEqual([], self._ids([]))

    def test_a_non_list_yields_nothing(self):
        self.assertEqual([], self._ids("refactor"))
        self.assertEqual([], self._ids(None))

    def test_the_walk_is_depth_bounded(self):
        nodes = {"root": {"question": "?", "children": ["a", "b"]},
                 "b": {"label": "B"}}
        chain = ["a"]
        for index in range(20):
            nodes[chain[-1]] = {"label": chain[-1], "question": "?",
                                "children": [f"n{index}", "b"]}
            chain.append(f"n{index}")
        nodes[chain[-1]] = {"label": "end"}
        self._write({"root": "root", "nodes": nodes})
        reached = self._ids(chain)
        self.assertLessEqual(len(reached), branches.MAX_DEPTH)


class BrokenTreeTests(_TreeCase):
    def test_a_question_with_a_missing_child_becomes_a_leaf(self):
        # Discarding the whole tree over one broken link would throw away the
        # guidance above it too.
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a", "gone"]},
            "a": {"label": "A", "guidance": "keep me"}}})
        tree = branches.load_tree()
        self.assertTrue(tree.usable)
        self.assertTrue(tree.get("root").is_leaf)
        self.assertTrue(any("gone" in p for p in tree.problems))

    def test_a_question_with_one_child_becomes_a_leaf(self):
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a"]},
            "a": {"label": "A"}}})
        self.assertTrue(branches.load_tree().get("root").is_leaf)

    def test_a_cycle_is_reported_and_does_not_hang(self):
        self._write({"root": "a", "nodes": {
            "a": {"question": "?", "children": ["b", "c"]},
            "b": {"question": "?", "children": ["a", "c"]},
            "c": {"label": "C"}}})
        tree = branches.load_tree()
        self.assertTrue(any("cycle" in p for p in tree.problems))
        self.assertEqual(["b"], self._ids(["b"], tree=tree))

    def test_an_unreachable_node_is_reported_but_harmless(self):
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a", "b"]},
            "a": {"label": "A"}, "b": {"label": "B"},
            "orphan": {"label": "O"}}})
        tree = branches.load_tree()
        self.assertTrue(tree.usable)
        self.assertTrue(any("orphan" in p for p in tree.problems))

    def test_a_missing_root_makes_the_tree_unusable(self):
        self._write({"root": "nowhere", "nodes": {"a": {"label": "A"}}})
        self.assertFalse(branches.load_tree().usable)

    def test_invalid_json_does_not_fall_back_to_the_default(self):
        """Falling back would hide the mistake and quietly hand the user
        someone else's workflow."""
        branches.tree_path().write_text("{not json", encoding="utf-8")
        tree = branches.load_tree()
        self.assertFalse(tree.usable)
        self.assertTrue(tree.problems)

    def test_a_node_with_an_unusable_id_is_dropped(self):
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a", "../escape"]},
            "a": {"label": "A"}, "../escape": {"label": "X"}}})
        tree = branches.load_tree()
        self.assertNotIn("../escape", tree.nodes)

    def test_a_non_object_node_is_dropped(self):
        self._write({"root": "root", "nodes": {
            "root": {"label": "R"}, "a": "not an object"}})
        self.assertNotIn("a", branches.load_tree().nodes)

    def test_a_tree_that_is_not_an_object_is_unusable(self):
        branches.tree_path().write_text("[1, 2]", encoding="utf-8")
        self.assertFalse(branches.load_tree().usable)


class GuidanceFormatTests(_TreeCase):
    def test_guidance_may_be_a_list_of_lines(self):
        # The awkward part of prose in JSON is the escaping, not the file.
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a", "b"]},
            "a": {"label": "A", "guidance": ["one", "", "two"]},
            "b": {"label": "B"}}})
        node = branches.load_tree().get("a")
        self.assertEqual("one\n\ntwo", node.guidance)

    def test_guidance_may_be_a_plain_string(self):
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a", "b"]},
            "a": {"label": "A", "guidance": "just text"},
            "b": {"label": "B"}}})
        self.assertEqual("just text", branches.load_tree().get("a").guidance)


class EnablementTests(_TreeCase):
    def test_only_named_agents_get_a_branch(self):
        self.assertEqual([], [n.id for n in branches.select(
            ["refactor"], agent_id="primary", allowed="scout")])
        self.assertEqual(["refactor"], [n.id for n in branches.select(
            ["refactor"], agent_id="scout", allowed="scout")])

    def test_star_enables_everyone_and_empty_enables_nobody(self):
        self.assertTrue(branches.agent_enabled("anyone", "", "*"))
        self.assertFalse(branches.agent_enabled("scout", "", ""))
        self.assertFalse(branches.agent_enabled("scout", "", " , "))

    def test_id_or_name_matches_case_insensitively(self):
        self.assertTrue(branches.agent_enabled("AI-2", "Scout", "scout"))
        self.assertTrue(branches.agent_enabled("AI-2", "x", "ai-2"))

    def test_a_subtree_can_narrow_but_never_widen(self):
        # Adding a node must not switch behaviour on for an agent the user
        # did not enable.
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a", "b"]},
            "a": {"label": "A", "guidance": "x", "applies_to": ["scout"]},
            "b": {"label": "B", "guidance": "y"}}})
        self.assertEqual([], [n.id for n in branches.select(
            ["a"], agent_id="primary", allowed="scout")])
        self.assertEqual(["a"], [n.id for n in branches.select(
            ["a"], agent_id="scout", allowed="scout")])
        self.assertEqual([], [n.id for n in branches.select(
            ["a"], agent_id="other", allowed="*")])

    def test_a_narrowed_subtree_is_not_offered_in_the_questions(self):
        self._write({"root": "root", "nodes": {
            "root": {"question": "pick", "children": ["a", "b"]},
            "a": {"label": "A", "choice": "only scout", "applies_to": ["scout"]},
            "b": {"label": "B", "choice": "anyone"}}})
        block = branches.render_questions(branches.load_tree(),
                                          agent_id="primary")
        self.assertIn('"b"', block)
        self.assertNotIn('"a"', block)


class RenderTests(_TreeCase):
    def test_guidance_accumulates_down_the_path(self):
        tree = branches.load_tree()
        out = branches.render(branches.walk(tree, ["refactor", "in-place"]))
        self.assertIn("Behaviour does not change", out)   # from the parent
        self.assertIn("Map before you move", out)         # from the leaf

    def test_the_path_is_stated_even_when_guidance_is_thin(self):
        """"This is a refactor, we are not adopting a library" are decisions.
        Stating them stops the model reopening them every turn."""
        self._write({"root": "root", "nodes": {
            "root": {"question": "?", "children": ["a", "b"]},
            "a": {"label": "Chosen"}, "b": {"label": "B"}}})
        out = branches.render(branches.walk(branches.load_tree(), ["a"]))
        self.assertIn('path="Chosen"', out)
        self.assertIn("No further guidance", out)

    def test_nothing_renders_to_nothing(self):
        self.assertEqual("", branches.render([]))
        self.assertEqual("", branches.render(None))

    def test_the_question_block_names_ids_not_yes_and_no(self):
        # An id that is not a child of the previous node is caught; a
        # mis-ordered "yes" is not.
        block = branches.render_questions(branches.load_tree())
        self.assertIn('"refactor"', block)
        self.assertIn('"in-place"', block)
        self.assertIn("branch_path", block)

    def test_an_unusable_tree_offers_no_questions(self):
        branches.tree_path().write_text("{bad", encoding="utf-8")
        self.assertEqual("", branches.render_questions(branches.load_tree()))


class DefaultFileTests(_TreeCase):
    def test_the_tree_is_written_so_it_can_be_edited(self):
        path = branches.write_default_tree()
        self.assertIsNotNone(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("kind", data["root"])
        self.assertIn("refactor", data["nodes"])

    def test_an_edited_tree_is_never_overwritten(self):
        # Pushed defaults overwriting user edits is a mistake this codebase
        # has already paid for once.
        branches.tree_path().write_text(
            json.dumps({"root": "mine", "nodes": {"mine": {"label": "M"}}}),
            encoding="utf-8")
        self.assertIsNone(branches.write_default_tree())
        self.assertEqual("mine", branches.load_tree().root)

    def test_the_written_tree_round_trips(self):
        branches.write_default_tree()
        tree = branches.load_tree()
        self.assertTrue(tree.usable)
        self.assertEqual((), tree.problems)
        self.assertEqual(["modify", "general"],
                         self._ids(["modify", "general"], tree=tree))

    def test_an_unwritable_home_is_not_an_error(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            self.assertIsNone(branches.write_default_tree())


class SpecFieldTests(unittest.TestCase):
    """The path travels on the intent spec; the tree validates it later."""

    TASK = "Refactor this module; the behaviour must not change"

    def test_a_plausible_path_survives(self):
        spec = intent.validate_spec(
            {"goal": "g", "branch_path": ["refactor", "in-place"]}, self.TASK)
        self.assertEqual(["refactor", "in-place"], spec["branch_path"])

    def test_a_bare_string_is_accepted_as_one_step(self):
        spec = intent.validate_spec(
            {"goal": "g", "branch_path": "refactor"}, self.TASK)
        self.assertEqual(["refactor"], spec["branch_path"])

    def test_ids_that_are_not_ids_are_dropped(self):
        spec = intent.validate_spec(
            {"goal": "g", "branch_path": ["../escape", "ok", ""]}, self.TASK)
        self.assertEqual(["ok"], spec["branch_path"])

    def test_a_missing_field_is_an_empty_path(self):
        self.assertEqual([], intent.validate_spec(
            {"goal": "g"}, self.TASK)["branch_path"])
        self.assertEqual([], intent.validate_spec(
            {"goal": "g", "branch_path": 7}, self.TASK)["branch_path"])

    def test_the_path_is_length_bounded(self):
        spec = intent.validate_spec(
            {"goal": "g", "branch_path": [f"n{i}" for i in range(40)]},
            self.TASK)
        self.assertLessEqual(len(spec["branch_path"]), intent.MAX_BRANCH_DEPTH)

    def test_the_decided_approach_reaches_the_progress_critic(self):
        spec = intent.validate_spec({
            "goal": "g",
            "requirements": [{"id": "R1", "text": "behaviour unchanged",
                              "anchor": "the behaviour must not change"}]}, self.TASK)
        spec["branch_label"] = "Refactor → Restructure in place"
        self.assertIn("Approach: Refactor → Restructure in place",
                      intent.to_contract_text(spec))

    def test_no_approach_is_not_stated_as_a_fact(self):
        spec = intent.validate_spec({
            "goal": "g",
            "requirements": [{"id": "R1", "text": "behaviour unchanged",
                              "anchor": "the behaviour must not change"}]}, self.TASK)
        self.assertNotIn("Approach:", intent.to_contract_text(spec))

    def test_the_self_ask_prompt_asks_for_the_path(self):
        self.assertIn("branch_path", intent.SELF_ASK_SYSTEM)
        self.assertIn("empty list", intent.SELF_ASK_SYSTEM)


if __name__ == "__main__":
    unittest.main()
