import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import policy
import agent_loop


class TerminalOpenPolicyTests(unittest.TestCase):
    def test_remote_terminal_is_available_after_connect_by_default(self):
        self.assertFalse(agent_loop._DEFAULT_CONFIG["disable_remote_terminal"])

    def test_terminal_open_never_creates_connection_approval(self):
        for mode in ("disabled", "audit", "enforce"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "policy.json"
                audit_path = Path(tmp) / "audit.log"
                config = copy.deepcopy(policy._DEFAULT_CONFIG)
                config["mode"] = mode
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with mock.patch.object(policy, "CONFIG_PATH", config_path), \
                        mock.patch.object(policy, "AUDIT_PATH", audit_path):
                    policy._config = None
                    policy._config_mtime = 0.0
                    decision = policy.evaluate_terminal_open(tmp, "req-1", "agent-1")
                    self.assertEqual(decision.action, "allow")
                    self.assertIn('"action": "allow"', audit_path.read_text(encoding="utf-8"))
                policy._config = None
                policy._config_mtime = 0.0


if __name__ == "__main__":
    unittest.main()
