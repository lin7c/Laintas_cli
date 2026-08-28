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

    def test_query_tools_are_registered_and_answer_from_the_vendored_core(self):
        """The tools the code-reading skill sends the model to must exist.

        The extension shipped for a while with only `atlas.lookup` while the
        standalone repo had grown the four query tools, so a skill telling the
        model to ask the index would have named tools with no schema.
        """
        spec = _load_extension_module().TOOL_SPEC
        self.assertEqual(
            {"atlas.lookup", "atlas.find", "atlas.outline", "atlas.neighbors",
             "atlas.stale"},
            set(spec))
        registered = []

        class _Ctx:
            cwd = "."

            def register_command(self, *a, **k):
                pass

            def register_tool(self, name, fn, schema):
                registered.append(name)

        _load_extension_module().setup(_Ctx())
        self.assertEqual(set(spec), set(registered))

    def test_vendored_core_answers_find_outline_and_stale(self):
        # The published package must stay free of .pyc files, and this test
        # imports the vendored core (which imports the indexer lazily, inside
        # store.stale) -- so bytecode stays off for the whole test.
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        self.addCleanup(setattr, sys, "dont_write_bytecode", previous)
        module = _load_extension_module()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample"
            package = source / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "module.py").write_text(
                "def answer():\n    return 42\n", encoding="utf-8")
            output = Path(tmp) / "atlas"
            index = subprocess.run(
                [sys.executable, str(EXTENSION / "atlas_cli.py"), "index",
                 str(source), "--out", str(output)],
                capture_output=True, text=True, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(index.returncode, 0, index.stderr)
            sys.path.insert(0, str(EXTENSION))
            try:
                from code_atlas_core import store
            finally:
                sys.path.remove(str(EXTENSION))
            db = output / "graph.db"
            found = store.find_symbol(db, "answer")
            self.assertEqual("exact", found["match"])
            self.assertEqual("pkg/module.py", found["matches"][0]["file"])
            outline = store.outline(db, "pkg.module")
            self.assertEqual(["answer"],
                             [f["name"] for f in outline["functions"]])
            # stale is the tool that keeps the other four honest.
            self.assertFalse(store.stale(db, str(source))["stale"])
            (package / "module.py").write_text(
                "def answer():\n    return 43\n", encoding="utf-8")
            self.assertTrue(store.stale(db, str(source))["stale"])
        del module


def _load_extension_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "code_atlas_extension_under_test", EXTENSION / "main.py")
    module = importlib.util.module_from_spec(spec)
    # Importing the extension must not leave a __pycache__ inside the package
    # that gets published -- the sibling test asserts the package is clean.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module



if __name__ == "__main__":
    unittest.main()
