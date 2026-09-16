"""Whether a delegation tool blocks must be readable from its first words.

Two tools start a child agent, two wait for one, and three run a command;
which of them holds the parent's loop was, in every case, a detail buried
mid-paragraph or absent. The model picks by name and opening line — `spawn`
is shorter than `agent.spawn` and reads like the default, and it is the
blocking one — so these tests pin the labels rather than the prose.
"""
import unittest

import tools


class ToolLabels(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tools.register_builtin_tools()
        cls.registry = tools.get_registry()

    def describe(self, name):
        tool = self.registry.get(name)
        self.assertIsNotNone(tool, name)
        return tool.description

    def test_blocking_tools_say_so_first(self):
        for name in ("spawn", "await_spawns", "agent.wait"):
            with self.subTest(tool=name):
                self.assertTrue(self.describe(name).startswith("BLOCKING"),
                                self.describe(name)[:60])

    def test_non_blocking_tools_say_so_first(self):
        for name in ("agent.spawn", "spawn_parallel", "terminal.exec"):
            with self.subTest(tool=name):
                self.assertTrue(self.describe(name).startswith("NON-BLOCKING"),
                                self.describe(name)[:60])

    def test_each_blocking_tool_names_its_non_blocking_alternative(self):
        self.assertIn("agent.spawn", self.describe("spawn"))
        self.assertIn("branch_status", self.describe("await_spawns"))
        self.assertIn("terminal.exec", self.describe("shell.exec"))

    def test_await_spawns_admits_what_the_timeout_costs(self):
        """It aborts every unfinished child at 20 minutes. Undocumented, that
        reads as 'stopped waiting', not 'threw the work away'."""
        text = self.describe("await_spawns")
        self.assertIn("20 minutes", text)
        self.assertIn("ABORTED", text)

    def test_await_spawns_says_it_cannot_return_the_first_result(self):
        self.assertIn("no 'first result' mode", self.describe("await_spawns"))

    def test_shell_exec_explains_that_its_timeout_bounds_silence(self):
        text = self.describe("shell.exec")
        self.assertIn("BLOCKS", text)
        self.assertIn("SILENCE", text)

    def test_the_two_terminal_starters_are_told_apart(self):
        self.assertIn("terminal.exec", self.describe("terminal.create"))
        self.assertIn("INTERACTIVE", self.describe("session.start"))

    def test_agent_spawn_flags_that_wait_turns_it_into_a_blocking_call(self):
        self.assertIn("wait=true makes this call BLOCK",
                      self.describe("agent.spawn"))


if __name__ == "__main__":
    unittest.main()
