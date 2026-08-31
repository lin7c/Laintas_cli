import tempfile
import unittest
from pathlib import Path
from unittest import mock

import snapshot


class SnapshotSafetyTests(unittest.TestCase):
    def test_create_skips_repository_rooted_at_home_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp).resolve())
            with mock.patch.object(snapshot, "repo_root", return_value=home), \
                    mock.patch.object(
                        snapshot.os.path, "expanduser", return_value=home), \
                    mock.patch.object(snapshot, "_git") as git:
                result = snapshot.create(home, "test")

        self.assertIsNone(result)
        git.assert_not_called()


class ChangedSinceTests(unittest.TestCase):
    """What `restore` cannot undo has to be visible to whoever decides.

    `restore` never deletes files created after a checkpoint, so a correction
    that says "steps 3-7 are void" is misleading unless it also names the files
    those steps created — an undo would leave every one of them on disk.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self._tmp.name).resolve())
        # An isolated repo: no user config, no signing hooks, no home fallout.
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "t"),
                     ("config", "commit.gpgsign", "false")):
            snapshot._git(self.repo, *args)
        (Path(self.repo) / "kept.txt").write_text("one\n")
        snapshot._git(self.repo, "add", "-A")
        snapshot._git(self.repo, "commit", "-q", "-m", "base")
        # The checkpoint store must live outside the repo, or it shows up as
        # an untracked file in every listing this test makes.
        self._store_dir = tempfile.TemporaryDirectory()
        self._store = mock.patch.object(
            snapshot, "_store_path",
            return_value=Path(self._store_dir.name) / "checkpoints.json")
        self._store.start()
        self.checkpoint = snapshot.create(self.repo, "task start")

    def tearDown(self):
        self._store.stop()
        self._store_dir.cleanup()
        self._tmp.cleanup()

    def test_reports_added_modified_and_deleted(self):
        (Path(self.repo) / "kept.txt").write_text("two\n")
        (Path(self.repo) / "brand_new.txt").write_text("new\n")
        snapshot._git(self.repo, "rm", "-q", "--cached", "kept.txt")

        changed = snapshot.changed_since(self.repo, self.checkpoint["sha"])
        self.assertIn("brand_new.txt", changed["added"])
        self.assertIn("kept.txt", changed["modified"] + changed["deleted"])
        self.assertFalse(changed["truncated"])

    def test_untracked_files_are_reported_as_added(self):
        # The category `restore` cannot touch, and the reason this exists.
        (Path(self.repo) / "scratch.py").write_text("x = 1\n")
        changed = snapshot.changed_since(self.repo, self.checkpoint["sha"])
        self.assertEqual(["scratch.py"], changed["added"])

    def test_clean_tree_reports_nothing(self):
        changed = snapshot.changed_since(self.repo, self.checkpoint["sha"])
        self.assertEqual([], changed["added"])
        self.assertEqual([], changed["modified"])

    def test_long_listings_are_truncated_not_dumped(self):
        for index in range(80):
            (Path(self.repo) / f"f{index:03d}.txt").write_text("x\n")
        changed = snapshot.changed_since(self.repo, self.checkpoint["sha"],
                                         max_files=10)
        self.assertEqual(10, len(changed["added"]))
        self.assertTrue(changed["truncated"])

    def test_missing_checkpoint_degrades_to_empty(self):
        changed = snapshot.changed_since(self.repo, "0" * 40)
        self.assertEqual([], changed["added"])

    def test_outside_a_repository_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual([], snapshot.changed_since(plain)["added"])


if __name__ == "__main__":
    unittest.main()
