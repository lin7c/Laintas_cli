"""Code Map ships as an extension, and its absence is the normal case.

It used to be five built-in tools registered on every request for every user,
whether or not the account had ever built a map. That is what an extension is
for: installed, the `code_map.*` schemas are in front of the model and a
repository question starts by asking the map; not installed, they are absent
and code reading falls back to grep/read with nothing to probe and nothing to
explain.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import extension_runtime
import paths
from tools import get_registry

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "extensions" / "code-map"


class CodeMapExtensionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.machine = self.root / "machine"
        self.machine.mkdir()
        shutil.copytree(SOURCE, self.machine / "code-map")
        for patch in (mock.patch.object(paths, "extensions_dir",
                                        lambda: self.root / "absent"),
                      mock.patch.object(paths, "global_extensions_dir",
                                        lambda: self.machine)):
            patch.start()
            self.addCleanup(patch.stop)
        self.runtime = extension_runtime.ExtensionRuntime()
        self.runtime.configure(reserved_commands=["/help"])
        self.addCleanup(lambda: self.runtime.unload("code-map"))

    def test_the_shipped_extension_loads_and_reaches_the_model(self):
        """A command only a human can type is not a capability the model has."""
        ok, message = self.runtime.load("code-map")
        self.assertTrue(ok, message)
        names = {tool.name for tool in get_registry().list()}
        for tool in ("code_map.build", "code_map.status", "code_map.list",
                     "code_map.read", "code_map.delete"):
            self.assertIn(tool, names)
        self.assertIn("/codemap", self.runtime.command_names())

    def test_unloading_takes_the_tools_back_out_of_every_request(self):
        self.runtime.load("code-map")
        self.runtime.unload("code-map")
        names = {tool.name for tool in get_registry().list()}
        self.assertEqual([], [n for n in names if n.startswith("code_map.")])

    def test_the_manifest_claims_the_namespace_the_tools_already_used(self):
        """`code_map.` and not `extension.code-map.`: the names are unchanged
        from when these were built in, so a saved prompt or policy rule that
        mentions one still matches."""
        manifest = json.loads((SOURCE / "extension.json").read_text(encoding="utf-8"))
        self.assertEqual("code_map.", manifest["toolPrefix"])
        self.assertEqual("code-map", manifest["name"])

    def test_a_refusal_reaches_the_model_in_the_server_s_own_words(self):
        """Code Map says why it refused; a status code would throw that away.

        The plain (name, handler, spec) registration form would nest this
        envelope inside a second one, so the tool is registered as a `Tool`.
        """
        self.runtime.load("code-map")
        tool = next(t for t in get_registry().list()
                    if t.name == "code_map.status")
        module = self.runtime._loaded["code-map"].module
        client = module._cm()
        with mock.patch.object(client, "status",
                               side_effect=client.CodeMapError("quota full")):
            result = tool.invoke({"map_id": "0" * 32}, None)
        self.assertFalse(result["ok"])
        self.assertEqual("quota full", result["error"])

    def test_the_command_keeps_a_url_intact(self):
        """The host hands over the whole line, `/codemap` included; the
        built-in dispatcher used to hand over the arguments alone."""
        self.runtime.load("code-map")
        module = self.runtime._loaded["code-map"].module
        client = module._cm()
        url = "https://github.com/owner/repo"
        with mock.patch.object(client, "build",
                               return_value={"id": "x", "title": "repo"}) as build:
            module._cmd_codemap(["/codemap", "build", url, "main"],
                                f"/codemap build {url} main")
        build.assert_called_once()
        self.assertEqual(url, build.call_args[0][0])
        self.assertEqual("main", build.call_args[0][1])

    def test_the_skill_arrives_and_leaves_with_the_tools(self):
        """Method for a tool the model may not have is worse than no method.

        It reads as an instruction, and nothing in the catalog says the
        capability behind it is absent -- so the prompt ships with the
        extension rather than in the bundled set.
        """
        import skills

        self.assertNotIn("code-map", skills.extension_skill_roots())
        self.runtime.load("code-map")
        roots = skills.extension_skill_roots()
        self.assertIn("code-map", roots)
        self.assertTrue((roots["code-map"] / "code-map" / "SKILL.md").is_file())

        catalog = skills.scan_metadata()
        self.assertIn("code-map", catalog)
        self.assertEqual(skills.SCOPE_EXTENSION, catalog["code-map"].scope)

        self.runtime.unload("code-map")
        self.assertNotIn("code-map", skills.extension_skill_roots())
        self.assertNotIn("code-map", skills.scan_metadata())

    def test_the_prompts_are_covered_by_the_same_approval_as_the_code(self):
        """A SKILL.md is instructions injected into the model's context.

        The sandboxing story bounds what extension CODE can do and nothing
        about what extension PROSE can do, so the trust hash has to cover the
        prose too -- otherwise the text an extension tells the model could
        change without the approval that installed it becoming invalid.
        """
        import extension_runtime

        covered = {
            str(path.relative_to(SOURCE))
            for path in extension_runtime.related_trust_paths(SOURCE)
        }
        self.assertIn("skills/code-map/SKILL.md", covered)
        self.assertIn("client.py", covered)
        self.assertNotIn("main.py", covered)   # hashed as the entrypoint

    def test_the_core_no_longer_carries_code_map(self):
        """The point of the move: nothing in the always-loaded surface —
        neither the tools nor a word of prose about them."""
        self.assertFalse((REPO / "code_map.py").exists())
        for module in ("tools.py", "laintas_cli.py"):
            text = (REPO / module).read_text(encoding="utf-8")
            self.assertNotIn("code_map.build", text)
            self.assertNotIn("_cmd_codemap", text)
        # The generated system prompt and the bundled skills are the two
        # surfaces every session pays for whether or not this is installed.
        self.assertNotIn("code_map", (REPO / "laintas_cli.py").read_text(
            encoding="utf-8"))
        for skill in (REPO / "default_skills").rglob("SKILL.md"):
            self.assertNotIn("code_map", skill.read_text(encoding="utf-8"),
                             f"{skill} describes an extension's tools")
        manifest = json.loads(
            (REPO / "package_manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("code_map", manifest["modules"])

    def test_a_read_says_how_stale_the_picture_is(self):
        """A map is glanced at for weeks; the tree moves under it.

        The failure mode of a park map is not being coarse — it is showing a
        path that is no longer there, because an orientation error is
        trusted. So a read carries the commit it was built at, measured
        against the checkout the agent is standing in.
        """
        import importlib.util
        import subprocess
        spec = importlib.util.spec_from_file_location(
            "code_map_main", SOURCE / "main.py")
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)

        head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        here = "https://github.com/lin7c/laintas_cli"
        self.assertIn("same commit",
                      main._age_against_working_tree(here, head))
        older = subprocess.run(("git", "rev-parse", "HEAD~3"), cwd=REPO,
                               capture_output=True, text=True).stdout.strip()
        self.assertIn("3 commit(s) ahead",
                      main._age_against_working_tree(here, older))
        # A map of a different repository is background, not description.
        self.assertIn("not the repository checked out here",
                      main._age_against_working_tree(
                          "https://github.com/openai/codex", head))
        # And nothing is claimed when nothing is known.
        self.assertEqual(main._age_against_working_tree(here, ""), "")
        self.assertIn("not in this checkout",
                      main._age_against_working_tree(here, "deadbee"))


if __name__ == "__main__":
    unittest.main()
