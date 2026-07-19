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


if __name__ == "__main__":
    unittest.main()
