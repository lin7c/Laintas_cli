import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import extension_manager
from scripts import build_official_extensions


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "swebench"


class SwebenchOfficialTests(unittest.TestCase):
    def test_manifest_is_valid_and_official(self):
        manifest = extension_manager.read_manifest(EXTENSION)
        self.assertEqual(extension_manager.validate_manifest(
            manifest, "swebench"), [])
        self.assertIn("swebench", build_official_extensions.OFFICIAL_NAMES)

    def test_sources_are_english(self):
        for path in EXTENSION.rglob("*"):
            if (not path.is_file() or ".laintas" in path.parts
                    or path.suffix not in {".py", ".json", ".md"}):
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"[\u4e00-\u9fff]", str(path))

    def test_publication_archive_ships_only_extension_source(self):
        # The source checkout may carry local CLI state (`.laintas/`);
        # the published package must not.
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "swebench.lext"
            extension_manager.create_publication_archive(
                EXTENSION, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
        for required in ("main.py", "runner.py", "README.md",
                         "extension.json"):
            self.assertIn(required, names)
        self.assertFalse(
            any(name.startswith(".laintas/") for name in names))
        self.assertFalse(any("__pycache__" in name for name in names))

    def test_runner_self_test_passes_from_the_checkout(self):
        # `runner.py` is the standalone entry point CI and an external
        # harness call; it must pass with no dataset and no network.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(EXTENSION / "runner.py"), "--self-test"],
                capture_output=True, text=True, check=False, timeout=300,
                cwd=tmp,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("self-test: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
