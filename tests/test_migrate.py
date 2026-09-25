"""Regression cover for migrate.py.

The module moves USER DATA (session credentials, memory, the Helpwo-era
`.helpwo` project file) on every startup; a silent bug here loses data or
re-migrates forever. The old layout is pre-2026, so these tests fabricate it
in temp directories instead of relying on leftovers.

Isolation: `_home_migrations()` is patched to [] for the end-to-end runs so
a developer's real home directory is never touched; cwd migrations run inside
a TemporaryDirectory via os.chdir.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import migrate
import paths


class MigrateOneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_missing_old_is_skipped(self):
        self.assertIsNone(migrate._migrate_one(
            self.root / "gone", self.root / "new", "nothing"))

    def test_existing_new_is_skipped(self):
        old = self.root / "old.json"
        old.write_text("{}")
        new = self.root / "new.json"
        new.write_text('{"kept": true}')
        self.assertIsNone(migrate._migrate_one(old, new, "occupied"))
        # The new file is never overwritten by a migration.
        self.assertEqual(json.loads(new.read_text()), {"kept": True})
        self.assertTrue(old.exists())

    def test_file_is_copied_and_old_renamed_bak(self):
        old = self.root / "old.json"
        old.write_text('{"v": 1}')
        new = self.root / "nested" / "new.json"
        self.assertEqual(
            migrate._migrate_one(old, new, "a file"), "migrated")
        self.assertEqual(json.loads(new.read_text()), {"v": 1})
        # Safety net: the original is renamed, never deleted.
        self.assertFalse(old.exists())
        self.assertTrue(old.with_name("old.json.bak").exists())

    def test_directory_is_copied_recursively(self):
        old = self.root / "old_dir"
        (old / "sub").mkdir(parents=True)
        (old / "sub" / "memory.db").write_text("data")
        new = self.root / "new_dir"
        self.assertEqual(
            migrate._migrate_one(old, new, "a dir"), "migrated")
        self.assertEqual((new / "sub" / "memory.db").read_text(), "data")
        self.assertTrue(old.with_name("old_dir.bak").exists())

    def test_empty_new_directory_is_replaced(self):
        # ensure_home() pre-creates the target tree; an empty target must not
        # block the copy (it is removed and refilled, not merged).
        old = self.root / "old_dir"
        old.mkdir()
        (old / "x").write_text("x")
        new = self.root / "new_dir"
        new.mkdir()
        self.assertEqual(
            migrate._migrate_one(old, new, "empty target"), "migrated")
        self.assertEqual((new / "x").read_text(), "x")

    def test_copy_failure_is_reported_not_raised(self):
        old = self.root / "old.json"
        old.write_text("{}")
        with mock.patch.object(migrate.shutil, "copy2",
                               side_effect=OSError("disk full")):
            outcome = migrate._migrate_one(old, self.root / "new.json", "boom")
        self.assertTrue(outcome.startswith("error:"))
        # A failed migration must not have renamed the original away.
        self.assertTrue(old.exists())


class MigrateAllTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)

    def _run(self):
        # Real home migrations would move the developer's own files: pin the
        # list to empty and test the cwd half, which is the Helpwo linkage.
        with mock.patch.object(migrate, "_home_migrations", return_value=[]):
            return migrate.migrate_all(verbose=False)

    def test_helpwo_legacy_file_moves_into_laintas(self):
        legacy = {"user": {"name": "tester"}}
        Path(".helpwo").write_text(json.dumps(legacy))
        results = self._run()
        self.assertIn("project file .helpwo", results["migrated"])
        moved = Path(paths.project_dir()) / "memory.json"
        self.assertEqual(json.loads(moved.read_text()), legacy)
        self.assertTrue(Path(".helpwo.bak").exists())

    def test_second_run_is_idempotent(self):
        Path(".helpwo").write_text("{}")
        self._run()
        results = self._run()
        self.assertIn("project file .helpwo", results["skipped"])
        self.assertEqual(results["migrated"], [])

    def test_needs_migration_reflects_old_files(self):
        self.assertFalse(migrate.needs_migration())
        Path(".loop_command.py").write_text("def handle_loop_command(c, ctx): pass\n")
        self.assertTrue(migrate.needs_migration())

    def test_error_is_collected_not_fatal(self):
        Path(".helpwo").write_text("{}")
        with mock.patch.object(migrate, "_home_migrations", return_value=[]), \
                mock.patch.object(migrate, "_migrate_one",
                                  side_effect=[None, "error: boom", None, None]):
            results = migrate.migrate_all(verbose=False)
        self.assertEqual(results["errors"], ["project file .helpwo: error: boom"])


if __name__ == "__main__":
    unittest.main()
