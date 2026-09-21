"""E1 (bughunt): a torn `_deleted.json` must not brick session saves.

deleted_ids only caught FileNotFoundError, so a half-written registry
(power loss / crash mid-save) made every subsequent mark_deleted /
is_deleted call raise JSONDecodeError. The fix quarantines the corrupt
file and treats deletion memory as empty; a dict payload is also not a
key set and must not be silently parsed as one.
"""
import json
import tempfile
import unittest
from pathlib import Path

import paths
import session_lifecycle


class DeletedRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: self._rmtree())
        self._old_dir = paths.SESSIONS_DIR
        paths.SESSIONS_DIR = self.tmp / "sessions"
        paths.SESSIONS_DIR.mkdir(parents=True)
        self.cwd = "/proj/bughunt-e1"

    def _rmtree(self):
        paths.SESSIONS_DIR = self._old_dir

    def _registry(self) -> Path:
        return paths.SESSIONS_DIR / f"{session_lifecycle._key(self.cwd)}_deleted.json"

    def test_torn_json_is_quarantined_and_self_heals(self):
        self._registry().write_text('{"broken": [1,2', encoding="utf-8")
        self.assertEqual(session_lifecycle.deleted_ids(self.cwd), set())
        with session_lifecycle.guard(self.cwd):
            session_lifecycle.mark_deleted(self.cwd, ["abc"])
        # corrupt file preserved for inspection, registry rebuilt clean
        quarantined = [p for p in paths.SESSIONS_DIR.iterdir()
                       if ".corrupt-" in p.name]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(json.loads(self._registry().read_text()), ["abc"])
        self.assertEqual(session_lifecycle.deleted_ids(self.cwd), {"abc"})

    def test_torn_json_is_deleted_then_save_roundtrip(self):
        self._registry().write_text("[oops", encoding="utf-8")
        with session_lifecycle.guard(self.cwd):
            session_lifecycle.mark_deleted(self.cwd, ["x", "y"])
        self.assertEqual(session_lifecycle.deleted_ids(self.cwd), {"x", "y"})

    def test_dict_payload_is_not_a_key_set(self):
        self._registry().write_text(json.dumps({"id1": 1, "id2": 2}),
                                    encoding="utf-8")
        self.assertEqual(session_lifecycle.deleted_ids(self.cwd), set())

    def test_non_string_entries_are_dropped(self):
        self._registry().write_text(json.dumps(["ok", 3, None]),
                                    encoding="utf-8")
        self.assertEqual(session_lifecycle.deleted_ids(self.cwd), {"ok"})

    def test_valid_registry_still_reads(self):
        with session_lifecycle.guard(self.cwd):
            session_lifecycle.mark_deleted(self.cwd, ["a"])
        self.assertEqual(session_lifecycle.deleted_ids(self.cwd), {"a"})
        self.assertTrue(session_lifecycle.is_deleted(
            self.cwd, {"session_id": "a"}))


if __name__ == "__main__":
    unittest.main()
