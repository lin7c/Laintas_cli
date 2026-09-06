"""Esc must reach everything the turn started, and rename nothing else.

Two bugs these cover:

1. Esc set the foreground Agent's ``abort_event`` and stopped there. A
   sub-agent runs on its own thread watching its OWN event, so children of an
   interrupted turn kept running, kept spending tokens and kept holding their
   PTYs. The crash path had always cascaded; the interrupt path never did.

2. Every auxiliary backend call — compaction, its review pass, intent routing,
   the critic, memory extraction, vision — shares ``call_backend_stream`` and
   wrote ITS model into the REPL status cache. The prompt then named a model
   no request of the user's had gone to.
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_loop
import laintas_cli


class StopRunDescendantsTests(unittest.TestCase):
    """_stop_run_descendants: this turn's children, and only this turn's."""

    def setUp(self):
        self.created = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for agent_id in reversed(self.created):
            try:
                agent_loop.unregister_agent(agent_id)
            except Exception:
                pass

    def _agent(self, name, parent=None, depth=0, role="pool"):
        info = agent_loop.register_agent(
            name=name, depth=depth, role=role,
            parent_id=(parent.id if parent else None))
        self.created.append(info.id)
        return info

    def test_children_created_during_the_turn_are_aborted(self):
        parent = self._agent("esc-parent", role="primary")
        before = agent_loop.agent_descendants(parent.id)
        child = self._agent("esc-child", parent=parent, depth=1)
        child.status = "running"

        stopped = laintas_cli._stop_run_descendants(parent, before)

        self.assertEqual(1, stopped)
        self.assertTrue(child.abort_event.is_set(),
                        "a child spawned by the interrupted turn kept running")

    def test_the_abort_cascades_to_grandchildren(self):
        parent = self._agent("esc-parent2", role="primary")
        before = agent_loop.agent_descendants(parent.id)
        child = self._agent("esc-child2", parent=parent, depth=1)
        grandchild = self._agent("esc-grandchild", parent=child, depth=2)
        child.status = "running"
        grandchild.status = "running"

        # One abort at the root of the new subtree, not one per node: the
        # deeper agent has no direct relationship with the interrupted turn.
        self.assertEqual(1, laintas_cli._stop_run_descendants(parent, before))
        self.assertTrue(grandchild.abort_event.is_set())

    def test_agents_that_predate_the_turn_survive(self):
        parent = self._agent("esc-parent3", role="primary")
        standing = self._agent("esc-standing", parent=parent, depth=1)
        standing.status = "running"
        # Snapshot taken AFTER the standing agent exists: it is independent
        # background work, and Esc on an unrelated turn must not kill it.
        before = agent_loop.agent_descendants(parent.id)

        self.assertEqual(0, laintas_cli._stop_run_descendants(parent, before))
        self.assertFalse(standing.abort_event.is_set(),
                         "Esc killed a pre-existing background agent")

    def test_finished_children_are_not_counted(self):
        parent = self._agent("esc-parent4", role="primary")
        before = agent_loop.agent_descendants(parent.id)
        child = self._agent("esc-child4", parent=parent, depth=1)
        child.status = "done"

        self.assertEqual(0, laintas_cli._stop_run_descendants(parent, before))

    def test_no_agent_is_not_an_error(self):
        self.assertEqual(0, laintas_cli._stop_run_descendants(None, set()))


class ForegroundTurnTests(unittest.TestCase):
    """Only the user's own turn may name the model on the prompt."""

    def setUp(self):
        self._saved = laintas_cli._foreground_run_thread
        self.addCleanup(self._restore)

    def _restore(self):
        laintas_cli._foreground_run_thread = self._saved

    def test_main_loop_on_the_foreground_thread_counts(self):
        laintas_cli._foreground_run_thread = threading.current_thread()
        self.assertTrue(laintas_cli._is_foreground_turn("main_loop"))

    def test_auxiliary_kinds_do_not_count(self):
        laintas_cli._foreground_run_thread = threading.current_thread()
        for kind in ("vision", "compaction", "compaction_review", "critic",
                     "intent", "intent_judge", "intent_compare",
                     "mem_extract", "context_inspector", ""):
            with self.subTest(kind=kind):
                self.assertFalse(
                    laintas_cli._is_foreground_turn(kind),
                    f"{kind or '(untagged)'} would rename the prompt")

    def test_a_sub_agents_main_loop_does_not_count(self):
        laintas_cli._foreground_run_thread = threading.current_thread()
        seen = []

        def _worker():
            seen.append(laintas_cli._is_foreground_turn("main_loop"))

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual([False], seen,
                         "a sub-agent's pinned model renamed the user's prompt")


if __name__ == "__main__":
    unittest.main()
