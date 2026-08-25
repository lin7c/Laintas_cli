import json
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
EXTENSION = ROOT / "extensions" / "code-atlas"


class CodeAtlasOfficialTests(unittest.TestCase):
    def test_manifest_is_valid_and_official(self):
        manifest = extension_manager.read_manifest(EXTENSION)
        self.assertEqual(extension_manager.validate_manifest(
            manifest, "code-atlas"), [])
        self.assertIn("code-atlas", build_official_extensions.OFFICIAL_NAMES)

    def test_package_is_english_and_has_no_transient_files(self):
        for path in EXTENSION.rglob("*"):
            if not path.is_file():
                continue
            self.assertNotIn("__pycache__", path.parts)
            self.assertNotIn(path.suffix, {".pyc", ".pyo"})
            if path.suffix in {".py", ".json", ".md", ".html", ".js", ".css"}:
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"[\u4e00-\u9fff]", str(path))

    def test_vendored_indexer_runs_without_external_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample"
            package = source / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "module.py").write_text(
                "def answer():\n    return 42\n", encoding="utf-8")
            output = root / "atlas"
            index = subprocess.run(
                [sys.executable, str(EXTENSION / "atlas_cli.py"), "index",
                 str(source), "--out", str(output)],
                capture_output=True, text=True, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(index.returncode, 0, index.stderr)
            self.assertTrue((output / "graph.db").is_file())
            self.assertTrue((output / "graph.json").is_file())
            verify = subprocess.run(
                [sys.executable, str(EXTENSION / "atlas_cli.py"), "verify",
                 str(source), "--out", str(output)],
                capture_output=True, text=True, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_publication_archive_contains_self_contained_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "code-atlas.lext"
            extension_manager.create_publication_archive(
                EXTENSION, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
            self.assertIn("code_atlas_core/indexer.py", names)
            self.assertIn("workflows/run.py", names)
            self.assertIn("viewer/index.html", names)
            self.assertFalse(any("__pycache__" in name for name in names))


if __name__ == "__main__":
    unittest.main()
