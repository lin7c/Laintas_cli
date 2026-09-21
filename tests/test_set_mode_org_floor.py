"""S5 (bughunt): set_mode must not freeze org policy or weaken past it.

set_mode persisted the ORG-MERGED config back to the local file (the
organisation's rules became indistinguishable from local choices, and a
later org update looked like no change), and accepted any mode — a
local file could vote the org's enforcement away. The fix writes back
the raw local config and refuses a mode weaker than the org floor.

Note: _migrate_config legitimately back-fills DEFAULT deny rules into an
old local file; that is expected and is not org leakage. The assertions
here check for the org contributor's marker rule specifically.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import policy

ORG_MARKER = "ORG-SECRET-RULE-xyzzy"


def _org_enforce(cfg):
    cfg = dict(cfg)
    cfg["needs_approval"] = list(cfg.get("needs_approval") or []) + [ORG_MARKER]
    cfg["mode"] = "enforce"
    return cfg


def _org_passthrough(cfg):
    return dict(cfg)


class _CfgFile:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "policy.json"
        self.path.write_text(json.dumps({"mode": "enforce"}),
                             encoding="utf-8")
        self._patches = [
            mock.patch.object(policy, "CONFIG_PATH", self.path),
            mock.patch.object(policy, "_apply_org_policy",
                              side_effect=_org_enforce),
            mock.patch.object(policy, "_config", None),
            mock.patch.object(policy, "_config_mtime", None),
            mock.patch.object(policy, "_config_org_version",
                              policy._config_org_version),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    def local(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


class SetModeOrgFloorTests(unittest.TestCase):
    def test_weakening_below_org_floor_is_refused(self):
        with _CfgFile() as cfg:
            ok, msg = policy.set_mode("audit")
            self.assertFalse(ok)
            self.assertIn("organisation policy", msg)
            self.assertEqual(cfg.local().get("mode"), "enforce")

    def test_org_rules_not_frozen_into_local_file(self):
        with _CfgFile() as cfg:
            policy.set_mode("audit")       # refused
            policy.set_mode("enforce")     # accepted, same level
            raw = json.dumps(cfg.local())
            self.assertNotIn(ORG_MARKER, raw,
                             "org rule frozen into the local config")

    def test_runtime_config_keeps_org_rules_after_set_mode(self):
        # The file is local-only, but the cached config every evaluate() reads
        # must still be the org-merged one. It used to cache the raw local
        # dict with a matching mtime, dropping org rules until the next edit.
        with _CfgFile():
            ok, _ = policy.set_mode("enforce")
            self.assertTrue(ok)
            self.assertIn(ORG_MARKER, policy._load_config().get(
                "needs_approval", []), "org rule dropped from runtime config")

    def test_same_level_and_stricter_accepted(self):
        with _CfgFile() as cfg:
            ok, _ = policy.set_mode("enforce")
            self.assertTrue(ok)
            self.assertEqual(cfg.local().get("mode"), "enforce")

    def test_free_switching_without_org_constraints(self):
        with _CfgFile() as cfg:
            cfg._patches[1].stop()  # drop the org mock
            passthrough = mock.patch.object(
                policy, "_apply_org_policy", side_effect=_org_passthrough)
            passthrough.start()
            self.addCleanup(passthrough.stop)
            ok, _ = policy.set_mode("audit")
            self.assertTrue(ok)
            self.assertEqual(cfg.local().get("mode"), "audit")
            ok2, _ = policy.set_mode("disabled")
            self.assertTrue(ok2)
            self.assertEqual(cfg.local().get("mode"), "disabled")

    def test_invalid_mode_rejected(self):
        with _CfgFile():
            ok, msg = policy.set_mode("yolo")
            self.assertFalse(ok)
            self.assertIn("Invalid mode", msg)


if __name__ == "__main__":
    unittest.main()
