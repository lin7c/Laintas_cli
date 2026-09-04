"""What `capabilities` is for, now that it is not decorative.

It was read once, to print on a list, and never validated or shown at the
moment it matters. An extension is Python loaded into this process, so nothing
in a manifest can stop `import os` -- the declaration cannot be a sandbox and
should not read like one. What it can be is informed consent: the person who
runs `/extensions trust` is told what the package puts in front of them and of
the model before their approval lets it load.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import extension_manager
import paths

REPO = Path(__file__).resolve().parents[1]


class CapabilityVocabularyTests(unittest.TestCase):
    def _manifest(self, **overrides):
        base = {"schemaVersion": 2, "name": "thing", "version": "1.0.0",
                "description": "d", "entrypoint": "main.py"}
        base.update(overrides)
        return base

    def test_a_typo_is_not_a_silent_declaration_of_nothing(self):
        errors = extension_manager.validate_manifest(
            self._manifest(capabilities=["fs.read", "fs.wrte"]), "thing")
        self.assertTrue(any("unknown capabilities" in e for e in errors))

    def test_known_values_pass(self):
        self.assertEqual([], extension_manager.validate_manifest(
            self._manifest(capabilities=["fs.read", "fs.write", "network"]),
            "thing"))

    def test_declaring_nothing_stays_legal(self):
        """A package that claims nothing is different from one with a typo."""
        self.assertEqual([], extension_manager.validate_manifest(
            self._manifest(capabilities=[]), "thing"))
        self.assertEqual([], extension_manager.validate_manifest(
            self._manifest(), "thing"))

    def test_capabilities_must_be_a_list(self):
        errors = extension_manager.validate_manifest(
            self._manifest(capabilities="fs.read"), "thing")
        self.assertIn("capabilities must be a list", errors)


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copytree(REPO / "extensions" / "code-map", self.root / "code-map")
        for patch in (mock.patch.object(paths, "extensions_dir",
                                        lambda: self.root),
                      mock.patch.object(paths, "global_extensions_dir",
                                        lambda: self.root / "absent")):
            patch.start()
            self.addCleanup(patch.stop)
        self.manager = extension_manager.ExtensionManager()

    def test_it_names_what_reaches_the_model(self):
        """Tool schemas and skill prose, which is the half a sandbox would
        not have bounded even if there were one."""
        facts = self.manager.disclosure("code-map")
        self.assertEqual("code-map", facts["name"])
        self.assertEqual("code_map.", facts["tool_namespace"])
        self.assertEqual(["code-map"], [s["name"] for s in facts["skills"]])
        self.assertIn("public GitHub repository",
                      facts["skills"][0]["description"])
        self.assertGreater(facts["files"], 1)

    def test_an_absent_extension_discloses_nothing_rather_than_guessing(self):
        self.assertIsNone(self.manager.disclosure("not-installed"))

    def test_every_bundled_extension_declares_a_known_vocabulary(self):
        """The rule has to hold for what we ship, or it is advice."""
        for directory in sorted((REPO / "extensions").iterdir()):
            manifest_path = directory / "extension.json"
            if not manifest_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [], extension_manager.validate_manifest(manifest, directory.name),
                f"{directory.name} manifest is invalid")


if __name__ == "__main__":
    unittest.main()
