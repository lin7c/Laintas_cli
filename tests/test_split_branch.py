"""Does this request divide? — asked in the pass already reading it.

Three steps, and every one of them already existed: `branches` walks a
validated binary tree, the "split" path's guidance says to write the .hwo/.hwg,
and the `hwg` tool runs it. Nothing here orchestrates anything; the decision is
a tree and the instruction is prose attached to one of its nodes.
"""

import unittest
from unittest import mock

import agent_loop
import branches
import intent
import laintas_cli


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tree = branches.decomposition_tree()

    def test_the_tree_is_usable_and_binary(self):
        self.assertTrue(self.tree.usable, self.tree.problems)
        self.assertEqual(
            set(self.tree.get(self.tree.root).children), {"split", "single"})

    def test_each_answer_walks_and_nothing_else_does(self):
        for answer in ("split", "single"):
            with self.subTest(answer=answer):
                self.assertEqual(
                    [n.id for n in branches.walk(self.tree, [answer])], [answer])
        for bad in (["maybe"], [], ["split", "split"], "split", None, 7):
            with self.subTest(path=bad):
                reached = branches.walk(self.tree, bad)
                self.assertNotEqual(
                    [n.id for n in reached], ["maybe"],
                    "an id that is not a child of the root is not an answer")

    def test_it_is_parsed_once_and_not_read_from_the_users_file(self):
        """Scout's tree is the user's. This one names the tools that carry it
        out, so a node id the runtime does not expect is unusable guidance."""
        with mock.patch.object(branches, "load_tree",
                               side_effect=AssertionError("read the user file")):
            self.assertIs(branches.decomposition_tree(),
                          branches.decomposition_tree())

    def test_it_is_separate_from_scouts_tree(self):
        """Different question, so a different root — 'does this divide' cannot
        hang under 'is this a refactor' without inheriting its meaning."""
        self.assertNotEqual(self.tree.root, branches.load_tree().root)


class GuidanceTests(unittest.TestCase):
    def _render(self, answer):
        return branches.render(
            branches.walk(branches.decomposition_tree(), [answer]),
            tag="task_split")

    def test_split_says_to_write_the_workflow_and_run_it(self):
        guidance = self._render("split")
        self.assertIn(".hwo", guidance)
        self.assertIn(".hwg", guidance)
        self.assertIn("hwg", guidance)
        self.assertIn("join", guidance)

    def test_split_forbids_two_parts_owning_one_file(self):
        self.assertIn("same file are one part", self._render("split"))

    def test_a_request_that_does_not_divide_gets_no_advice_at_all(self):
        """The whole cost of asking should be the question. A turn that does
        not divide must read exactly like any other turn."""
        nodes = branches.walk(branches.decomposition_tree(), ["single"])
        self.assertTrue(nodes)
        self.assertFalse(any(node.guidance for node in nodes))


class SpecTests(unittest.TestCase):
    def test_the_question_names_the_field_it_is_reported_in(self):
        rendered = branches.render_questions(
            branches.decomposition_tree(), field="split_path")
        self.assertIn("split_path", rendered)
        self.assertNotIn("branch_path", rendered)

    def test_scouts_tree_still_asks_for_branch_path(self):
        self.assertIn(
            "branch_path", branches.render_questions(branches.load_tree()))

    def test_the_spec_carries_both_paths_independently(self):
        spec = intent.validate_spec(
            {"goal": "g", "branch_path": ["refactor"], "split_path": ["split"]},
            "g")
        self.assertEqual(spec["branch_path"], ["refactor"])
        self.assertEqual(spec["split_path"], ["split"])

    def test_a_missing_or_malformed_path_is_an_empty_one(self):
        for raw in ({}, {"split_path": None}, {"split_path": [{"a": 1}]},
                    {"split_path": 7}):
            with self.subTest(raw=raw):
                spec = intent.validate_spec({"goal": "g", **raw}, "g")
                self.assertEqual(spec["split_path"], [])

    def test_a_bare_string_is_read_as_a_one_step_path(self):
        spec = intent.validate_spec({"goal": "g", "split_path": "split"}, "g")
        self.assertEqual(spec["split_path"], ["split"])

    def test_a_later_round_sees_the_placement_the_first_one_made(self):
        """The final spec is the last round's. Without carrying the placement
        forward, round two re-decides from nothing and may flip it."""
        messages = intent.build_self_ask_messages(
            "t", prior=intent.validate_spec(
                {"goal": "g", "requirements": [
                    {"id": "R1", "text": "t", "anchor": "t"}],
                 "split_path": ["split"]}, "t"),
            round_index=2)
        self.assertIn("split_path", str(messages))
        self.assertIn("split", str(messages))

    def test_the_self_ask_prompt_asks_for_it(self):
        self.assertIn("split_path", intent.SELF_ASK_SYSTEM)
        self.assertIn("names the field", intent.SELF_ASK_SYSTEM)


class ForemanTests(unittest.TestCase):
    """The third default colleague: the one this question is put to."""

    def test_it_is_registered_by_default_and_named_by_the_setting(self):
        self.assertEqual(laintas_cli.DECOMPOSER_SUB_AGENT_NAME, "foreman")
        self.assertEqual(
            agent_loop._DEFAULT_CONFIG["split_agents"],
            laintas_cli.DECOMPOSER_SUB_AGENT_NAME)

    def test_only_it_is_asked_by_default(self):
        allowed = agent_loop._DEFAULT_CONFIG["split_agents"]
        self.assertTrue(branches.agent_enabled("foreman", "foreman", allowed))
        for other in ("primary", "scout"):
            with self.subTest(agent=other):
                self.assertFalse(branches.agent_enabled(other, other, allowed))

    def test_scouts_tree_and_this_one_go_to_different_agents(self):
        """Two specialities, two agents: one works a job carefully, the other
        decides whether it is one job."""
        self.assertNotEqual(agent_loop._DEFAULT_CONFIG["branch_agents"],
                            agent_loop._DEFAULT_CONFIG["split_agents"])

    def test_its_prompt_does_not_restate_the_branch_guidance(self):
        """Two copies of 'write a .hwo, bind them in a .hwg' would be two
        things to keep in agreement, and the model loses that argument."""
        prompt = laintas_cli.FOREMAN_PROFILE_PROMPT
        for mechanic in (".hwo", ".hwg", "hwg tool", "join:"):
            with self.subTest(mechanic=mechanic):
                self.assertNotIn(mechanic, prompt)

    def test_its_prompt_owns_the_judgement_and_the_joined_result(self):
        prompt = laintas_cli.FOREMAN_PROFILE_PROMPT.lower()
        self.assertIn("usually", prompt)          # most work does not divide
        self.assertIn("joined result", prompt)
        self.assertIn("claim", prompt)            # unchecked part is a claim

    def test_the_setting_is_documented_like_its_sibling(self):
        self.assertIn("split_agents", agent_loop._RUNTIME_CONFIG_DESCRIPTIONS)


class RenderTests(unittest.TestCase):
    def test_the_two_sections_do_not_collide(self):
        """Both are rendered into the same prompt; each must say which it is."""
        task = branches.render(branches.walk(branches.load_tree(), ["refactor"]))
        split = branches.render(
            branches.walk(branches.decomposition_tree(), ["split"]),
            tag="task_split")
        self.assertIn("<task_branch ", task)
        self.assertIn("<task_split ", split)
        self.assertNotIn("<task_split", task)

    def test_an_empty_path_renders_nothing(self):
        self.assertEqual(branches.render([], tag="task_split"), "")


if __name__ == "__main__":
    unittest.main()
