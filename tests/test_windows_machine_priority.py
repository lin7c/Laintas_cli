"""A connected Windows kernel has to change what the model reaches for.

Registering `win.*` alone did not: with dynamic context on (the default) the
router only advertised a tool family whose keywords the request contained,
and "打开浏览器" matched `browser.` — the invisible headless Chrome in WSL — so
a user with the kernel running got a browser they could not see. The skill
that explains the machine was ranked lexically on English text and never
surfaced for a Chinese request. Nothing in the prompt said a desktop was in
reach, or how to start a program on it. And a bundled skill, once copied to
the user's directory, was never updated again, so improvements to it reached
new installs only.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import context_router
import skills as skills_mod
import windows_tools
from tools import Tool, get_registry


def _tool(name, description="x"):
    return Tool(name=name, description=description, schema={"type": "object"},
                invoke=lambda params, ctx: {"ok": True})


_WIN = ["win.windows", "win.snapshot", "win.screenshot", "win.invoke",
        "win.set_value", "win.click", "win.type", "win.key"]


class RoutingTests(unittest.TestCase):
    def _tools(self, connected):
        base = [_tool("fs.read"), _tool("shell.exec"),
                _tool("browser.open", "Open a page in the headless browser")]
        return base + ([_tool(n, "Windows kernel control") for n in _WIN]
                       if connected else [])

    def test_win_tools_are_resident_while_the_kernel_is_connected(self):
        for query in ("打开浏览器帮我查天气", "open notepad", "帮我回一下微信",
                      "fix the failing test"):
            with self.subTest(query=query):
                chosen = context_router.select_tool_names(query, self._tools(True))
                self.assertTrue(set(_WIN) <= chosen, chosen)

    def test_nothing_changes_without_a_kernel(self):
        chosen = context_router.select_tool_names("fix the failing test",
                                                  self._tools(False))
        self.assertFalse(any(n.startswith("win.") for n in chosen))


class PromptSectionTests(unittest.TestCase):
    def tearDown(self):
        windows_tools._registered.clear()

    def test_absent_without_a_kernel(self):
        windows_tools._registered.clear()
        self.assertEqual(windows_tools.render_prompt_section(), "")

    def test_names_the_desktop_and_how_to_launch_on_it(self):
        windows_tools._registered[:] = list(_WIN)
        text = windows_tools.render_prompt_section()
        self.assertIn("browser.*", text)          # says what NOT to reach for
        self.assertIn("explorer.exe", text)       # says how to open things
        self.assertIn("win.windows", text)        # and how to find the window
        self.assertIn("read and write", text)

    def test_read_only_tier_says_it_cannot_act(self):
        windows_tools._registered[:] = ["win.windows", "win.snapshot"]
        self.assertIn("read only", windows_tools.render_prompt_section())


class SkillPinTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(skills_mod._skill_metadata)
        skills_mod._skill_metadata.clear()
        skills_mod._skill_metadata.update({
            "windows-machine": skills_mod.SkillMetadata(
                name="windows-machine", description="Driving the Windows machine",
                requires_tool="win.snapshot"),
            "git": skills_mod.SkillMetadata(name="git", description="git work"),
        })
        self.enterContext(mock.patch.object(skills_mod, "_scan_stale",
                                            return_value=False))

    def tearDown(self):
        skills_mod._skill_metadata.clear()
        skills_mod._skill_metadata.update(self._saved)
        get_registry().unregister("win.snapshot")

    def _highlight(self, query):
        with mock.patch.object(agent_loop, "get_runtime_config",
                               side_effect=lambda k: {"dynamic_context": True,
                                                      "dynamic_skill_limit": 3}.get(k)):
            return agent_loop._skill_catalog_parts(query, "", None)[1]

    def test_pinned_while_connected_even_for_a_chinese_request(self):
        get_registry().register(_tool("win.snapshot"))
        self.assertIn("windows-machine", self._highlight("打开微信"))

    def test_never_offered_while_disconnected(self):
        # It used to be ranked lexically regardless of availability.
        self.assertNotIn("windows-machine",
                         self._highlight("what is on my screen, windows machine"))


class BundledUpgradeTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="skill-upgrade-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.bundled = root / "bundled"
        self.user = root / "user"
        (self.bundled / "demo").mkdir(parents=True)
        self.user.mkdir()
        self._write_bundled("v1")
        self.enterContext(mock.patch.object(skills_mod, "BUNDLED_SKILLS_DIR", self.bundled))
        self.enterContext(mock.patch.object(skills_mod, "SKILLS_DIR", self.user))
        self.enterContext(mock.patch.object(
            skills_mod, "_SHIPPED_DIGESTS_FILE", self.bundled / ".shipped-digests.json"))

    def _write_bundled(self, body):
        (self.bundled / "demo" / "SKILL.md").write_text(
            f"---\nname: demo\n---\n{body}\n", encoding="utf-8")

    def _user_body(self):
        return (self.user / "demo" / "SKILL.md").read_text(encoding="utf-8")

    def test_untouched_copy_follows_the_bundled_version(self):
        skills_mod.ensure_bundled_skills_installed()
        self.assertIn("v1", self._user_body())
        self._write_bundled("v2")
        self.assertEqual(skills_mod.ensure_bundled_skills_installed(), ["demo"])
        self.assertIn("v2", self._user_body())

    def test_an_edited_copy_is_the_users_and_is_never_replaced(self):
        skills_mod.ensure_bundled_skills_installed()
        (self.user / "demo" / "SKILL.md").write_text(
            "---\nname: demo\n---\nmy own rules\n", encoding="utf-8")
        self._write_bundled("v2")
        self.assertEqual(skills_mod.ensure_bundled_skills_installed(), [])
        self.assertIn("my own rules", self._user_body())

    def test_a_copy_from_before_the_marker_upgrades_if_it_was_shipped(self):
        # Seeded by an old CLI: no marker, content equal to a shipped version.
        shutil.copytree(self.bundled / "demo", self.user / "demo")
        shipped = skills_mod._skill_dir_digest(self.user / "demo")
        (self.bundled / ".shipped-digests.json").write_text(
            json.dumps({"skills": {"demo": [shipped]}}))
        self._write_bundled("v2")
        self.assertEqual(skills_mod.ensure_bundled_skills_installed(), ["demo"])
        self.assertIn("v2", self._user_body())

    def test_an_interrupted_upgrades_leftovers_are_not_listed_as_skills(self):
        # rmtree() in the upgrade path is best-effort, so a staging or retired
        # copy can survive. Sorting first, a dot-dir would show the same skill
        # twice — and be picked ahead of the real one.
        skills_mod.ensure_bundled_skills_installed()
        for leftover in (".demo.upgrading", ".demo.old"):
            shutil.copytree(self.user / "demo", self.user / leftover)

        self.assertEqual([p.name for p in skills_mod.list_skill_dirs()], ["demo"])

    def test_leftovers_are_cleared_even_when_nothing_needs_upgrading(self):
        skills_mod.ensure_bundled_skills_installed()
        for leftover in (".demo.upgrading", ".demo.old"):
            shutil.copytree(self.user / "demo", self.user / leftover)

        # Digest already matches: the early `continue` used to mean no later
        # run ever looked at the leftovers again.
        self.assertEqual(skills_mod.ensure_bundled_skills_installed(), [])
        self.assertFalse((self.user / ".demo.upgrading").exists())
        self.assertFalse((self.user / ".demo.old").exists())

    def test_a_pre_marker_copy_that_was_edited_stays(self):
        shutil.copytree(self.bundled / "demo", self.user / "demo")
        (self.bundled / ".shipped-digests.json").write_text(
            json.dumps({"skills": {"demo": ["0" * 64]}}))
        (self.user / "demo" / "SKILL.md").write_text(
            "---\nname: demo\n---\nhand edited\n", encoding="utf-8")
        self._write_bundled("v2")
        self.assertEqual(skills_mod.ensure_bundled_skills_installed(), [])
        self.assertIn("hand edited", self._user_body())


class ShippedDigestsTests(unittest.TestCase):
    def test_every_bundled_skill_has_shipped_history(self):
        data = json.loads((skills_mod.BUNDLED_SKILLS_DIR / ".shipped-digests.json")
                          .read_text(encoding="utf-8"))["skills"]
        for d in skills_mod.BUNDLED_SKILLS_DIR.iterdir():
            if d.is_dir() and d.name in data:
                self.assertTrue(all(len(x) == 64 for x in data[d.name]))


if __name__ == "__main__":
    unittest.main()
