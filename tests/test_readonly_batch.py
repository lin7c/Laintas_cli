"""Concurrent dispatch of an all-read-only turn.

Tool calls were dispatched strictly one after another, so a turn cost the sum
of its calls rather than the slowest one. The gate mirrors Helpwo's: a turn goes
parallel only when EVERY call in it is read-only, which is what keeps the
agent's single persistent PTY unshared and every write ordered.
"""
import time
import unittest

import agent_loop
import tools


def _call(name, **args):
    return {"name": name, "arguments": args}


class _SleepyRegistry:
    """Stands in for the real registry so timing is deterministic."""

    def invoke(self, name, args, ctx):
        time.sleep(args.get("delay", 0))
        return {"ok": True, "tool": name, "result": args.get("tag")}


class _ExplodingRegistry:
    def invoke(self, name, args, ctx):
        raise RuntimeError("executor failure")


class ReadOnlyGateTests(unittest.TestCase):
    def test_a_turn_of_only_read_only_calls_batches(self):
        self.assertTrue(agent_loop._can_batch_read_only(
            [_call("fs.read"), _call("fs.grep"), _call("fs.ls")], False))

    def test_the_wire_taxonomy_is_recognised_too(self):
        """The set is consulted with whichever naming is active."""
        self.assertTrue(agent_loop._can_batch_read_only(
            [_call("read"), _call("grep")], False))

    def test_one_non_read_only_call_serializes_the_whole_turn(self):
        for intruder in ("shell.exec", "fs.write", "fs.edit", "terminal.send",
                         "skill.load", "tool.search", "some.unknown.tool"):
            with self.subTest(intruder=intruder):
                self.assertFalse(agent_loop._can_batch_read_only(
                    [_call("fs.read"), _call(intruder)], False),
                    f"{intruder} must force sequential dispatch")

    def test_a_single_call_and_an_empty_turn_stay_sequential(self):
        self.assertFalse(agent_loop._can_batch_read_only([_call("fs.read")], False))
        self.assertFalse(agent_loop._can_batch_read_only([], False))

    def test_an_interrupted_turn_starts_nothing(self):
        self.assertFalse(agent_loop._can_batch_read_only(
            [_call("fs.read"), _call("fs.grep")], True))

    def test_the_shell_and_every_write_tool_are_excluded_by_name(self):
        for excluded in ("shell", "shell.exec", "terminal.send", "terminal.exec",
                         "fs.write", "fs.edit", "fs.multi_edit", "write", "edit",
                         "skill.load", "skill_load", "tool.search", "tool_search"):
            self.assertNotIn(excluded, agent_loop._READ_ONLY_TOOLS)


class ReadOnlyBatchTests(unittest.TestCase):
    def setUp(self):
        self._real = tools.get_registry
        tools.get_registry = lambda: _SleepyRegistry()

    def tearDown(self):
        tools.get_registry = self._real

    def test_results_come_back_in_call_order_not_completion_order(self):
        calls = [_call("fs.read", delay=0.30, tag="A"),
                 _call("fs.grep", delay=0.05, tag="B"),
                 _call("fs.ls", delay=0.15, tag="C")]
        out = agent_loop._dispatch_read_only_batch(calls, object())
        self.assertEqual(["A", "B", "C"], [r["result"] for r in out])

    def test_the_turn_costs_its_slowest_call_not_their_sum(self):
        calls = [_call("fs.read", delay=0.3), _call("fs.grep", delay=0.3),
                 _call("fs.ls", delay=0.3)]
        started = time.monotonic()
        agent_loop._dispatch_read_only_batch(calls, object())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.65, "0.9s of sleeps should overlap into ~0.3s")

    def test_a_non_dict_argument_is_normalized_like_the_sequential_path(self):
        out = agent_loop._dispatch_read_only_batch(
            [{"name": "fs.read", "arguments": "malformed"},
             _call("fs.ls", tag="X")], object())
        self.assertEqual(2, len(out))
        self.assertEqual("X", out[1]["result"])

    def test_every_slot_is_filled_even_when_the_executor_fails(self):
        tools.get_registry = lambda: _ExplodingRegistry()
        out = agent_loop._dispatch_read_only_batch(
            [_call("fs.read"), _call("fs.ls")], object())
        self.assertEqual(2, len(out))
        for entry in out:
            self.assertIsInstance(entry, dict)
            self.assertFalse(entry["ok"])


if __name__ == "__main__":
    unittest.main()
