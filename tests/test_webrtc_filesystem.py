import os
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import webrtc_channel


class StructuredFilesystemRpcTests(unittest.TestCase):
    def test_explicitly_shared_workspace_is_an_allowed_p2p_root(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(raw).resolve()
            outside = Path(outside_raw).resolve()
            (root / "visible.txt").write_text("visible", encoding="utf-8")
            registry = SimpleNamespace(workspace_path=str(root))

            with (
                patch("policy._load_config", return_value={"allowedRoots": []}),
                patch.object(webrtc_channel, "_registry_ref", registry),
            ):
                rows = webrtc_channel._run_fs_operation("list", {"path": str(root)})
                self.assertEqual([row["name"] for row in rows], ["visible.txt"])
                with self.assertRaises(PermissionError):
                    webrtc_channel._run_fs_operation("list", {"path": str(outside)})

    def test_operations_stay_inside_allowed_root(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(raw).resolve()
            outside = Path(outside_raw).resolve()
            (root / "source.txt").write_text("hello", encoding="utf-8")
            (outside / "secret.txt").write_text("secret", encoding="utf-8")

            with patch("policy._load_config", return_value={"allowedRoots": [str(root)]}):
                self.assertTrue(webrtc_channel._run_fs_operation("probe", {})["ready"])
                rows = webrtc_channel._run_fs_operation("list", {"path": str(root)})
                self.assertEqual([row["name"] for row in rows], ["source.txt"])

                folder = root / "folder"
                webrtc_channel._run_fs_operation("mkdir", {"path": str(folder)})
                copied = folder / "copy.txt"
                webrtc_channel._run_fs_operation("copy", {
                    "source": str(root / "source.txt"), "destination": str(copied),
                })
                moved = folder / "moved.txt"
                webrtc_channel._run_fs_operation("move", {
                    "source": str(copied), "destination": str(moved),
                })
                self.assertEqual(moved.read_text(encoding="utf-8"), "hello")
                self.assertEqual(
                    webrtc_channel._run_fs_operation("search", {
                        "root": str(root), "pattern": "*.txt", "recursive": True,
                    })[0]["name"],
                    "source.txt",
                )
                self.assertGreater(
                    webrtc_channel._run_fs_operation("quota", {"path": str(root)})["quota"], 0,
                )

                with self.assertRaises(PermissionError):
                    webrtc_channel._run_fs_operation("stat", {"path": str(outside / "secret.txt")})

    def test_symlink_mutations_do_not_follow_or_copy_escape(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(raw).resolve()
            outside = Path(outside_raw).resolve()
            target = outside / "keep.txt"
            target.write_text("keep", encoding="utf-8")
            link = root / "external-link"
            link.symlink_to(target)

            with patch("policy._load_config", return_value={"allowedRoots": [str(root)]}):
                meta = webrtc_channel._run_fs_operation("stat", {"path": str(link)})
                self.assertEqual(meta["type"], "symlink")
                with self.assertRaises(PermissionError):
                    webrtc_channel._run_fs_operation("copy", {
                        "source": str(link), "destination": str(root / "copied-link"),
                    })
                webrtc_channel._run_fs_operation("remove", {"path": str(link)})

            self.assertFalse(os.path.lexists(link))
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
