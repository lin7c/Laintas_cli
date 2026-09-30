"""Commands the user types straight into term0 must be discoverable by the agent.

They land in chat_history but never in the message thread, and the
<sub_terminals> snapshot skips the agent's own terminal. The agent is told
which commands ran and reads their output with terminal.read.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import agent_loop
import laintas_cli  # noqa: F401  (wires the runtime the way the CLI does)
import tools


def _shell(cmd, output=None, rc=0):
    rows = [{"role": "user", "content": cmd, "input_kind": "shell"}]
    if output is not None:
        rows.append({"role": "shell", "content": output, "returncode": rc})
    return rows


class PendingDirectCommandsTests(unittest.TestCase):
    def test_collects_commands_since_the_agent_last_spoke(self):
        history = (
            _shell("ls", "a b")
            + [{"role": "user", "content": "fix it", "input_kind": "prompt"},
               {"role": "assistant", "content": "done"}]
            + _shell("pytest", "1 failed", rc=1)
            + _shell("touch x")                      # no output, no shell row
            + [{"role": "user", "content": "y", "input_kind": "interactive"}]
            + _shell("make", "ok", rc=0))
        self.assertEqual(agent_loop._pending_direct_commands(history), [
            {"command": "pytest", "returncode": 1},
            {"command": "touch x", "returncode": None},
            {"command": "make", "returncode": 0},
        ])

    def test_nothing_pending_after_an_agent_turn(self):
        history = _shell("ls", "a") + [{"role": "assistant", "content": "hi"}]
        self.assertEqual(agent_loop._pending_direct_commands(history), [])

    def test_failed_and_unanswered_turns_do_not_hide_commands(self):
        failed = {"role": "assistant", "message_kind": "turn_failed",
                  "content": "[This turn ended without an answer: backend error.]"}
        history = (
            [{"role": "assistant", "content": "done"}]
            + _shell("echo key >> ~/.ssh/authorized_keys")
            + [{"role": "user", "content": "remove it", "input_kind": "prompt"},
               failed,
               {"role": "user", "content": "remove it", "input_kind": "prompt"},
               failed,
               {"role": "user", "content": "why?", "input_kind": "prompt"}])
        self.assertEqual(agent_loop._pending_direct_commands(history), [
            {"command": "echo key >> ~/.ssh/authorized_keys",
             "returncode": None},
        ])

    def test_keys_are_declared_turn_only(self):
        for key in ("_direct_commands", "_direct_commands_readable"):
            self.assertIn(key, agent_loop.STATE_KEYS_TURN_ONLY)


class LiveStateHintTests(unittest.TestCase):
    def test_hint_names_commands_and_the_tool_in_both_modes(self):
        state = {"_direct_commands": [{"command": "pytest", "returncode": 1}],
                 "_direct_commands_readable": True}
        for thread_mode in (True, False):
            message = agent_loop._build_user_message(
                "fix that error", state, [], [], 0, 30,
                thread_mode=thread_mode, first_turn=True)
            self.assertIn("<user_terminal>", message)
            self.assertIn("$ pytest  (exit 1)", message)
            self.assertIn('terminal.read name="term0"', message)

    def test_hint_does_not_offer_an_unavailable_tool(self):
        state = {"_direct_commands": [{"command": "ls", "returncode": None}],
                 "_direct_commands_readable": False}
        message = agent_loop._build_user_message(
            "x", state, [], [], 0, 30, thread_mode=True)
        self.assertIn("$ ls", message)
        self.assertNotIn("terminal.read", message)

    def test_no_hint_without_direct_commands(self):
        message = agent_loop._build_user_message(
            "x", {}, [], [], 0, 30, thread_mode=True)
        self.assertNotIn("<user_terminal>", message)

    def test_terminal_read_is_routed_when_commands_are_pending(self):
        authorized = {"shell.exec", "terminal.read", "terminal.scroll"}
        with mock.patch.object(agent_loop, "get_runtime_config",
                               side_effect=lambda k: True if k == "dynamic_context"
                               else agent_loop._DEFAULT_CONFIG.get(k)):
            quiet = agent_loop._visible_tool_names_for_task(
                "fix that error", {}, authorized)
            pending = agent_loop._visible_tool_names_for_task(
                "fix that error",
                {"_direct_commands": [{"command": "pytest", "returncode": 1}]},
                authorized)
        self.assertNotIn("terminal.read", quiet)
        self.assertIn("terminal.read", pending)
        self.assertIn("terminal.scroll", pending)


class TerminalReadScrubTests(unittest.TestCase):
    def test_markers_and_colour_codes_are_removed(self):
        raw = ("echo __LAINTAS_SHELL_BEGIN_ab12__; pytest\r\n"
               "__LAINTAS_SHELL_BEGIN_ab12__\r\n"
               "\x1b[31mFAILED\x1b[0m test_x.py::test_a\r\n"
               "__LAINTAS_SHELL_END_ab12__ 1\r\n")
        session = SimpleNamespace(
            raw_output=raw, returncode=-1,
            read_output=lambda timeout=0: "", is_alive=lambda: True)
        term = SimpleNamespace(session=session)
        ctx = tools.ToolCtx(agent_id="primary",
                            get_terminal=lambda name: term)
        result = tools._bi_terminal_read({"name": "term0"}, ctx)
        self.assertEqual(result["new_output"], "FAILED test_x.py::test_a")


if __name__ == "__main__":
    unittest.main()
