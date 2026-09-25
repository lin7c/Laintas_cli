"""Regression cover for contract_notify.py.

The module is the CLI→Helpwo doorbell: a dropped notification must never
raise, never retry, and cost at most one round of staleness. These tests pin
that contract — silent-failure semantics, the 1s dedup window, local-bridge
priority — none of which had any coverage before (grep found zero references
in tests/).

Isolation: `_registry()` and the helpwo_server bridge are mocked, so no real
Helpwo/bridge/cloud path is touched.
"""

import unittest
from unittest import mock

import contract_notify


class _FakeRegistry:
    def __init__(self):
        self.pushed = []

    def _push_events(self, events):
        self.pushed.extend(events)
        return True


class ContractNotifyTests(unittest.TestCase):
    def setUp(self):
        self.registry = _FakeRegistry()
        # Restore pre-existing state as well as isolating this test's writes.
        state = mock.patch.multiple(contract_notify, _last_push=0.0, _last_summary="")
        state.start()
        self.addCleanup(state.stop)
        bridge = mock.patch("helpwo_server.is_running", return_value=False)
        bridge.start()
        self.addCleanup(bridge.stop)

    def _push(self, what="propose", result=None):
        with mock.patch.object(contract_notify, "_registry",
                               return_value=self.registry):
            return contract_notify.push(what, result or {})

    def test_no_registry_means_nothing_emitted_and_no_raise(self):
        with mock.patch.object(contract_notify, "_registry", return_value=None):
            self.assertFalse(contract_notify.push("propose", {}))

    def test_event_vocabulary_and_doorbell_action(self):
        self._push("agree", {"operation": "propose"})
        self.assertEqual(len(self.registry.pushed), 1)
        event = self.registry.pushed[0]
        self.assertEqual(event["type"], "peer-request")
        self.assertEqual(event["kind"], "contract-changed")
        # The payload is a nudge, never the contract itself.
        self.assertEqual(event["meta"]["action"], "reread-contract")
        self.assertEqual(event["meta"]["path"], ".laintas/contract/contract.lock.json")

    def test_operation_names_come_from_result_or_results_list(self):
        self._push("implement", {"operation": "propose"})
        self.assertIn("contract implement: propose",
                      self.registry.pushed[0]["content"])
        self._push("implement", {"results": [
            {"operation": "propose"}, {"operation": "agree"}]})
        self.assertIn("contract implement: propose, agree",
                      self.registry.pushed[1]["content"])

    def test_identical_summary_within_window_is_suppressed(self):
        self.assertTrue(self._push("propose", {"operation": "propose"}))
        self.assertFalse(self._push("propose", {"operation": "propose"}))
        self.assertEqual(len(self.registry.pushed), 1)

    def test_different_fact_is_not_suppressed(self):
        self.assertTrue(self._push("propose", {"operation": "propose"}))
        # A proposal followed by an agreement are two different facts.
        self.assertTrue(self._push("agree", {"operation": "agree"}))
        self.assertEqual(len(self.registry.pushed), 2)

    def test_local_bridge_takes_priority_over_cloud_push(self):
        bridge_calls = []
        with mock.patch.object(contract_notify, "_registry",
                               return_value=self.registry), \
             mock.patch("helpwo_server.is_running", return_value=True,
                        create=True), \
             mock.patch("helpwo_server.push_unsolicited",
                        side_effect=lambda events: bridge_calls.append(events) or True,
                        create=True):
            self.assertTrue(contract_notify.push("propose", {}))
        # Went through the bridge only; the cloud _push_events path stayed idle.
        self.assertEqual(len(bridge_calls), 1)
        self.assertEqual(self.registry.pushed, [])

    def test_push_failure_is_silent_not_fatal(self):
        with mock.patch.object(contract_notify, "_registry",
                               return_value=self.registry), \
             mock.patch.object(self.registry, "_push_events",
                               side_effect=OSError("network down")):
            # Neither raise nor retry — the doorbell just fails.
            self.assertFalse(contract_notify.push("propose", {}))


if __name__ == "__main__":
    unittest.main()
