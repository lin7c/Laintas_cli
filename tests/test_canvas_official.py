"""The canvas extension as the host sees it: manifest, package, contracts.

Whiteboards used to be four top-level modules, a slash command and four tools
compiled into every copy of the CLI. They are an official extension now, so
what has to hold is no longer "does the command exist" but "does the package
still declare everything the command needs to behave like a built-in" — its
argument contract, its file completions, its tool names, and the keyword group
that makes the model's drawing tools visible when somebody says "whiteboard".
"""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import context_router
import extension_manager
import extension_runtime
import laintas_cli
from scripts import build_official_extensions

from tests.extension_packages import RecordingConsole, extension_package

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "canvas"


class CanvasManifestTests(unittest.TestCase):
    def test_manifest_is_valid_and_official(self):
        manifest = extension_manager.read_manifest(EXTENSION)
        self.assertEqual(
            extension_manager.validate_manifest(manifest, "canvas"), [])
        self.assertIn("canvas", build_official_extensions.OFFICIAL_NAMES)
        # The model must keep seeing `canvas.draw`, not
        # `extension.canvas.draw`: the prefix is what makes the move invisible
        # to every prompt and skill that already names these tools.
        self.assertEqual(manifest.get("toolPrefix"), "canvas.")

    def test_sources_are_english(self):
        for path in EXTENSION.rglob("*"):
            if (not path.is_file() or ".laintas" in path.parts
                    or path.suffix not in {".py", ".json", ".md"}):
                continue
            self.assertNotRegex(path.read_text(encoding="utf-8"),
                                r"[一-鿿]", str(path))

    def test_publication_archive_ships_only_extension_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "canvas.lext"
            extension_manager.create_publication_archive(EXTENSION, archive)
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
        self.assertIn("main.py", names)
        for module in ("canvas.py", "canvas_edit.py", "canvas_view.py",
                       "infinite_canvas.py"):
            self.assertIn(module, names)
        self.assertFalse([n for n in names if n.startswith(".laintas/")])


class CanvasRegistrationTests(unittest.TestCase):
    """What `setup(ctx)` declares, through the real runtime."""

    def setUp(self):
        # Through the real loader, not a hand-built context: the tool prefix,
        # the trust gate and the module search path are all things `load()`
        # does, and a test that skipped it would pass while the shipped
        # package registered `extension.canvas.canvas.list`.
        self.runtime = extension_runtime.ExtensionRuntime()
        self.runtime.configure(console=RecordingConsole())
        patcher = mock.patch.object(
            extension_runtime, "_extension_roots",
            return_value=[ROOT / "extensions"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(context_router.unregister_group, ("canvas.",))
        self.addCleanup(self.runtime.unload, "canvas")
        loaded, message = self.runtime.load("canvas")
        self.assertTrue(loaded, message)
        self.package = self.runtime._loaded["canvas"].module

    def test_the_command_is_registered_with_its_argument_contract(self):
        self.assertIn("/canvas", self.runtime.command_names())
        self.assertEqual(self.runtime.command_arg_rule("/canvas", "new")[0], 2)
        self.assertEqual(self.runtime.command_arg_rule("/canvas", "list")[0], 1)
        # An unknown subcommand falls back to the bare rule, as built-ins do.
        self.assertEqual(self.runtime.command_arg_rule("/canvas", "zzz")[0], 1)

    def test_extra_words_are_rejected_exactly_as_a_builtin_would(self):
        with mock.patch.object(extension_runtime, "get_runtime",
                               return_value=self.runtime):
            for command in ("/canvas list here", "/canvas a.excalidraw b"):
                with self.subTest(command=command):
                    with self.assertRaises(laintas_cli.SlashCommandUsageError):
                        action, args, _raw = laintas_cli._parse_slash_command(command)
                        laintas_cli._validate_slash_args(action, args)

    def test_board_paths_still_complete(self):
        with mock.patch.object(extension_runtime, "get_runtime",
                               return_value=self.runtime):
            self.assertEqual(
                extension_runtime.get_runtime().command_file_suffixes(
                    "/canvas", "open"),
                (".excalidraw",))

    def test_the_drawing_tools_keep_their_names(self):
        names = {tool.name for tool in self.package._canvas_tools()}
        self.assertEqual(names, {"canvas.list", "canvas.read",
                                 "canvas.draw", "canvas.update"})

    def test_a_whiteboard_question_still_routes_the_tools(self):
        """"whiteboard" appears in none of the tool names or descriptions."""
        group = [g for g in context_router._registered_groups
                 if g[1] == ("canvas.",)]
        self.assertTrue(group, "the extension did not register its keywords")
        self.assertIn("whiteboard", group[0][0])


if __name__ == "__main__":
    unittest.main()
