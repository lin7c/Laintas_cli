"""Machine-wide extensions and extension-declared tool namespaces.

Both exist for the Enterprise organisation layer: it is installed once per
machine rather than per project, and its tools have to reach the model under a
name a model can read.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import extension_runtime
import paths
from tools import Tool, get_registry

_MAIN = """
from tools import Tool


def setup(ctx):
    ctx.register_command("/probe", lambda parts, raw="": {reply!r})
    ctx.register_tool(Tool(name={tool!r}, description="ping",
                           schema={{"type": "object", "properties": {{}}}},
                           invoke=lambda args, ctx=None: {{"ok": True}}))
"""


def _write_extension(root: Path, name: str, *, reply: str = "local",
                     tool_prefix=None, tool: str = "ping") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"schemaVersion": 1, "name": name, "version": "1.0.0"}
    if tool_prefix is not None:
        manifest["toolPrefix"] = tool_prefix
    (directory / "extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "main.py").write_text(
        _MAIN.format(reply=reply, tool=tool), encoding="utf-8")
    return directory


class ExtensionRootTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.project = self.root / "project"
        self.machine = self.root / "machine"
        self.project.mkdir()
        self.machine.mkdir()
        patch_project = mock.patch.object(
            paths, "extensions_dir", lambda: self.project)
        patch_machine = mock.patch.object(
            paths, "global_extensions_dir", lambda: self.machine)
        patch_project.start()
        patch_machine.start()
        self.addCleanup(patch_project.stop)
        self.addCleanup(patch_machine.stop)
        self.runtime = extension_runtime.ExtensionRuntime()
        self.runtime.configure(reserved_commands=["/help"])
        self.addCleanup(lambda: self.runtime.unload("thing"))

    def test_a_machine_wide_extension_loads(self):
        _write_extension(self.machine, "thing", reply="machine")
        ok, message = self.runtime.load("thing")
        self.assertTrue(ok, message)
        _, result = self.runtime.invoke_command("/probe", ["/probe"], "/probe")
        self.assertEqual(result, "machine")

    def test_a_project_extension_shadows_the_machine_one(self):
        _write_extension(self.machine, "thing", reply="machine")
        _write_extension(self.project, "thing", reply="project")
        self.assertTrue(self.runtime.load("thing")[0])
        _, result = self.runtime.invoke_command("/probe", ["/probe"], "/probe")
        self.assertEqual(result, "project")

    def test_tool_prefix_from_the_manifest_is_used(self):
        _write_extension(self.machine, "thing", tool_prefix="org.")
        self.assertTrue(self.runtime.load("thing")[0])
        names = [tool.name for tool in get_registry().list()
                 if tool.source == "extension:thing"]
        self.assertEqual(names, ["org.ping"])

    def test_without_a_prefix_tools_stay_namespaced_by_owner(self):
        _write_extension(self.machine, "thing")
        self.assertTrue(self.runtime.load("thing")[0])
        names = [tool.name for tool in get_registry().list()
                 if tool.source == "extension:thing"]
        self.assertEqual(names, ["extension.thing.ping"])

    def test_a_malformed_tool_prefix_is_refused(self):
        _write_extension(self.machine, "thing", tool_prefix="Org")
        ok, message = self.runtime.load("thing")
        self.assertFalse(ok)
        self.assertIn("toolPrefix", message)

    def test_a_prefix_cannot_be_used_to_shadow_a_builtin(self):
        # A namespace an extension picks itself would otherwise be a way to
        # replace `fs.read` with its own implementation. The registry refuses to
        # overwrite a builtin, and that refusal must fail the whole load rather
        # than leave the extension half-registered.
        import tools as tools_mod
        tools_mod.register_builtin_tools()
        self.assertIsNotNone(get_registry().get("fs.read"))

        _write_extension(self.machine, "thing", tool_prefix="fs.", tool="read")
        ok, message = self.runtime.load("thing")
        self.assertFalse(ok, message)
        self.assertEqual(get_registry().get("fs.read").source, "builtin")


if __name__ == "__main__":
    unittest.main()
