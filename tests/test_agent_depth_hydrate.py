"""L5 (bughunt): persisted agent depth must be restored on hydrate.

save_agent_state persisted `depth`, but apply_persisted_state never
restored it, so a rehydrated deep agent forgot its nesting level —
can_spawn/depth-limit checks then treated it as a root agent. The fix
restores depth when the file carries a sane non-negative int; a fresh
registration keeps its own depth when the file does not.
"""
import unittest

import agent_persistence as ap


class FakeAgent:
    def __init__(self, depth=0):
        self.name = "fresh"
        self.role = "pool"
        self.depth = depth
        self.state = {}
        self.chat_history = []
        # apply_persisted_state also touches these; give every fake the
        # attributes so hydration works without the full AgentInfo class.
        self.parent_id = None
        self.parent_terminal = None
        self.deployment_terminal = None
        self.home_terminal = None
        self.stationed_terminal = None
        self.base_model = ""
        self.base_provider = ""
        self.profile = None
        self.assignment_history = []
        self.active_assignment = None
        self.created_at = 0.0


class DepthHydrateTests(unittest.TestCase):
    def test_persisted_depth_is_restored(self):
        a = FakeAgent()
        ap.apply_persisted_state(a, {"name": "deep-agent", "depth": 3})
        self.assertEqual(a.depth, 3)

    def test_zero_depth_is_restored(self):
        a = FakeAgent(depth=2)
        ap.apply_persisted_state(a, {"depth": 0})
        self.assertEqual(a.depth, 0)

    def test_missing_depth_keeps_registration_depth(self):
        a = FakeAgent(depth=2)
        ap.apply_persisted_state(a, {"name": "x"})
        self.assertEqual(a.depth, 2)

    def test_bad_depth_value_ignored(self):
        a = FakeAgent()
        ap.apply_persisted_state(a, {"depth": "bad"})
        self.assertEqual(a.depth, 0)
        b = FakeAgent()
        ap.apply_persisted_state(b, {"depth": -1})
        self.assertEqual(b.depth, 0)

    def test_round_trip_via_save_and_apply(self):
        class SaveableAgent(FakeAgent):
            id = "rt-agent"
            parent_id = None
            parent_terminal = None
            deployment_terminal = None
            home_terminal = None
            stationed_terminal = None
            base_model = ""
            base_provider = ""
            profile = None
            assignment_history = []
            active_assignment = None
            created_at = 0.0

        import tempfile
        from pathlib import Path
        from unittest import mock
        agent = SaveableAgent(depth=4)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ap, "_agent_file",
                                  lambda _id: Path(tmp) / "agent.json"):
            self.assertTrue(ap.save_agent_state(agent))
            data = ap.load_agent_state("rt-agent")
        fresh = FakeAgent()
        ap.apply_persisted_state(fresh, data)
        self.assertEqual(fresh.depth, 4)


if __name__ == "__main__":
    unittest.main()
