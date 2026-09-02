"""Idle (sleep-time) memory consolidation: when it runs, and off what material.

Consolidation used to hang off compaction, which was wrong twice over. It only
ever fired on a manual `/compact` — the automatic compaction path never called
it — and compaction is the worst available moment to mine a session anyway,
because it fires when the context is FULL and the head has already been
replaced by a ~4k-token summary. Moving it to the idle point after a finished
turn means extraction reads the turns verbatim.

What must hold: it never runs inside the turn, it never runs twice at once, it
is rate-limited on BOTH turns and wall clock, the watermark advances even when
the pass fails (so a bad turn is not retried forever), and a memory flagged
stale earns a pass on its own because an unverified claim is sitting in the
prompt right now.
"""
import threading
import time
import types
import unittest

import agent_loop


def _msgs(user_turns, per_turn=2):
    """A thread with `user_turns` user messages, each followed by a reply."""
    out = []
    for i in range(user_turns):
        out.append({"role": "user", "content": f"request {i}"})
        for j in range(per_turn - 1):
            out.append({"role": "assistant", "content": f"reply {i}.{j}"})
    return out


class IdleConsolidationCase(unittest.TestCase):
    KEYS = ("mem_extract_on_idle", "mem_idle_min_turns",
            "mem_idle_min_seconds", "mem_idle_review_limit")

    def setUp(self):
        self.saved = {k: agent_loop.get_runtime_config(k) for k in self.KEYS}
        self.addCleanup(self._restore)
        agent_loop.set_runtime_config("mem_extract_on_idle", True)
        agent_loop.set_runtime_config("mem_idle_min_turns", 2)
        agent_loop.set_runtime_config("mem_idle_min_seconds", 0)
        # Review needs the real store; these tests are about scheduling only.
        agent_loop.set_runtime_config("mem_idle_review_limit", 0)

        self.started = threading.Event()
        self.extracted = []

        def fake_extract(text, llm_fn, session=None):
            self.extracted.append(text)
            self.started.set()

        self._saved_extract = agent_loop.mem_extract.extract_and_store
        agent_loop.mem_extract.extract_and_store = fake_extract
        self.addCleanup(self._restore_extract)

        self.deps = types.SimpleNamespace(
            call_backend=lambda **kwargs: {"reply": "[]"})

    def _restore(self):
        for key, value in self.saved.items():
            agent_loop.set_runtime_config(key, value)

    def _restore_extract(self):
        agent_loop.mem_extract.extract_and_store = self._saved_extract

    def run_idle(self, state):
        fired = agent_loop._consolidate_memories_when_idle(
            self.deps, {}, state, [])
        if fired:
            self.started.wait(timeout=5)
            # Let the worker clear the single-flight flag before the next call.
            for _ in range(100):
                if not agent_loop._idle_consolidation_running:
                    break
                time.sleep(0.01)
        return fired


class SchedulingTests(IdleConsolidationCase):
    def test_disabled_does_nothing(self):
        agent_loop.set_runtime_config("mem_extract_on_idle", False)
        state = {"_thread_messages": _msgs(5)}
        self.assertFalse(self.run_idle(state))
        self.assertEqual(self.extracted, [])

    def test_too_few_new_turns_does_not_run(self):
        state = {"_thread_messages": _msgs(1)}
        self.assertFalse(self.run_idle(state))

    def test_enough_turns_runs_and_advances_the_watermark(self):
        state = {"_thread_messages": _msgs(3)}
        self.assertTrue(self.run_idle(state))
        self.assertEqual(state["_mem_idle_mark"], len(state["_thread_messages"]))
        self.assertTrue(self.extracted)

    def test_second_pass_only_sees_new_turns(self):
        state = {"_thread_messages": _msgs(2)}
        self.assertTrue(self.run_idle(state))
        first = self.extracted[-1]
        self.assertIn("request 0", first)

        state["_thread_messages"] = _msgs(2) + [
            {"role": "user", "content": "request NEW"},
            {"role": "assistant", "content": "reply NEW"},
            {"role": "user", "content": "request NEWER"},
            {"role": "assistant", "content": "reply NEWER"},
        ]
        self.assertTrue(self.run_idle(state))
        second = self.extracted[-1]
        self.assertIn("request NEW", second)
        self.assertNotIn("request 0", second,
                         "already-consolidated turns must not be re-sent")

    def test_wall_clock_floor_blocks_a_burst(self):
        agent_loop.set_runtime_config("mem_idle_min_seconds", 3600)
        state = {"_thread_messages": _msgs(3)}
        self.assertTrue(self.run_idle(state))
        state["_thread_messages"] = _msgs(6)
        self.assertFalse(self.run_idle(state),
                         "the clock floor must survive plenty of new turns")

    def test_a_stale_memory_earns_a_pass_on_its_own(self):
        # One turn is below min_turns, but a flagged claim is standing in the
        # prompt unverified, which is reason enough to run.
        state = {"_thread_messages": _msgs(1), "_stale_memories": ["auth-flow"]}
        self.assertTrue(self.run_idle(state))
        self.assertNotIn("_stale_memories", state)

    def test_watermark_advances_even_when_extraction_raises(self):
        def boom(text, llm_fn, session=None):
            self.started.set()
            raise RuntimeError("extractor down")

        agent_loop.mem_extract.extract_and_store = boom
        state = {"_thread_messages": _msgs(3)}
        self.assertTrue(self.run_idle(state))
        self.assertEqual(state["_mem_idle_mark"], len(state["_thread_messages"]))
        self.assertFalse(agent_loop._idle_consolidation_running,
                         "a failed pass must release the single-flight flag")

    def test_single_flight(self):
        release = threading.Event()

        def blocking(text, llm_fn, session=None):
            self.started.set()
            release.wait(timeout=5)

        agent_loop.mem_extract.extract_and_store = blocking
        state = {"_thread_messages": _msgs(3)}
        self.assertTrue(agent_loop._consolidate_memories_when_idle(
            self.deps, {}, state, []))
        self.started.wait(timeout=5)
        other = {"_thread_messages": _msgs(3)}
        self.assertFalse(agent_loop._consolidate_memories_when_idle(
            self.deps, {}, other, []),
            "a second pass must not start while one is running")
        release.set()
        for _ in range(200):
            if not agent_loop._idle_consolidation_running:
                break
            time.sleep(0.01)
        self.assertFalse(agent_loop._idle_consolidation_running)


class MaterialTests(IdleConsolidationCase):
    def test_material_is_the_verbatim_tail_since_the_mark(self):
        state = {"_thread_messages": _msgs(3), "_mem_idle_mark": 2}
        text, mark = agent_loop._idle_consolidation_material(state, [])
        self.assertEqual(mark, 6)
        self.assertNotIn("request 0", text)
        self.assertIn("request 1", text)
        self.assertIn("request 2", text)

    def test_a_shrunken_thread_resets_the_mark(self):
        # Compaction replaced the thread; the old index points nowhere.
        state = {"_thread_messages": _msgs(1), "_mem_idle_mark": 999}
        text, mark = agent_loop._idle_consolidation_material(state, [])
        self.assertEqual(mark, 2)
        self.assertIn("request 0", text)

    def test_nothing_new_yields_no_material(self):
        state = {"_thread_messages": _msgs(2), "_mem_idle_mark": 4}
        text, mark = agent_loop._idle_consolidation_material(state, [])
        self.assertEqual(text, "")
        self.assertEqual(mark, 4)

    def test_budget_keeps_the_end_of_the_window(self):
        saved = agent_loop.get_runtime_config("compact_chunk_tokens")
        agent_loop.set_runtime_config("compact_chunk_tokens", 4000)
        self.addCleanup(agent_loop.set_runtime_config,
                        "compact_chunk_tokens", saved)
        big = [{"role": "user", "content": "x" * 40000},
               {"role": "assistant", "content": "y" * 40000},
               {"role": "user", "content": "the conclusion"}]
        state = {"_thread_messages": big}
        text, _ = agent_loop._idle_consolidation_material(state, [])
        self.assertIn("the conclusion", text)
        self.assertNotIn("x" * 100, text,
                         "when the budget binds, the oldest turns are dropped")


if __name__ == "__main__":
    unittest.main()
