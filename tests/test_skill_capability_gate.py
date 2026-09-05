"""A skill about tools that are not here should not be in the prompt.

The Windows machine tiers come and go with a second process on another
operating system. Describing them in every prompt on every machine costs
context on a capability the model cannot reach — and, worse than the cost,
invites it to plan around one and then discover mid-task that it cannot.

`requires_tool` is the gate. It is deliberately narrow: absent means
unconditional, which is what almost every skill is.
"""

import unittest
from pathlib import Path

import skills as skills_mod
from tools import Tool, get_registry


def _meta(name: str, requires: str = "") -> skills_mod.SkillMetadata:
    return skills_mod.SkillMetadata(name=name, description=f"{name} desc",
                                    requires_tool=requires)


def _tool(name: str) -> Tool:
    return Tool(name=name, description="x", schema={"type": "object"},
                invoke=lambda params, ctx: {"ok": True})


class AvailabilityTests(unittest.TestCase):
    def tearDown(self):
        get_registry().unregister("win.snapshot")

    def test_a_skill_with_no_requirement_is_always_available(self):
        self.assertTrue(skills_mod.skill_is_available(_meta("git")))

    def test_a_skill_is_hidden_while_its_tool_is_absent(self):
        self.assertFalse(
            skills_mod.skill_is_available(_meta("windows-machine",
                                                "win.snapshot")))

    def test_a_skill_appears_once_its_tool_registers(self):
        get_registry().register(_tool("win.snapshot"))
        self.assertTrue(
            skills_mod.skill_is_available(_meta("windows-machine",
                                                "win.snapshot")))

    def test_it_disappears_again_when_the_tool_goes(self):
        get_registry().register(_tool("win.snapshot"))
        get_registry().unregister("win.snapshot")
        self.assertFalse(
            skills_mod.skill_is_available(_meta("windows-machine",
                                                "win.snapshot")))


class CatalogTests(unittest.TestCase):
    """The rendered catalog, which is what actually reaches the prompt."""

    def setUp(self):
        self._saved = dict(skills_mod._skill_metadata)
        skills_mod._skill_metadata.clear()
        skills_mod._skill_metadata.update({
            "git": _meta("git"),
            "windows-machine": _meta("windows-machine", "win.snapshot"),
        })
        skills_mod._scan_done = True
        skills_mod._scan_project = str(skills_mod.project_skills_dir())

    def tearDown(self):
        skills_mod._skill_metadata.clear()
        skills_mod._skill_metadata.update(self._saved)
        skills_mod.invalidate_scan()
        get_registry().unregister("win.snapshot")

    def test_the_gated_skill_is_absent_without_a_kernel(self):
        catalog = skills_mod.describe_skills_for_prompt()
        self.assertIn("git", catalog)
        self.assertNotIn("windows-machine", catalog)

    def test_the_gated_skill_is_listed_with_a_kernel(self):
        get_registry().register(_tool("win.snapshot"))
        catalog = skills_mod.describe_skills_for_prompt()
        self.assertIn("windows-machine", catalog)


class ShippedSkillTests(unittest.TestCase):
    """The bundled files themselves, so the gate is actually wired to one."""

    def _read(self, name: str) -> str:
        return (skills_mod.BUNDLED_SKILLS_DIR / name / "SKILL.md").read_text(
            encoding="utf-8")

    def test_the_windows_machine_skill_declares_its_requirement(self):
        meta, _ = skills_mod._parse_frontmatter(self._read("windows-machine"))
        self.assertEqual("win.snapshot", meta.get("requires_tool"))

    def test_the_wsl_skill_is_not_gated(self):
        """It is about the filesystem, which is there with or without a kernel."""
        meta, _ = skills_mod._parse_frontmatter(self._read("wsl-windows"))
        self.assertEqual("", str(meta.get("requires_tool", "")))

    def test_the_machine_skill_carries_the_privacy_rules(self):
        """The capability with no technical control needs the written one."""
        body = self._read("windows-machine")
        for phrase in ("clipboard", "2FA", "Narrow the read",
                       "Never authenticate as the user"):
            self.assertIn(phrase, body)

    def test_the_moved_section_is_not_duplicated(self):
        """Two copies drift, and the stale one is the one that gets read."""
        wsl = self._read("wsl-windows")
        self.assertNotIn("win.screenshot", wsl)
        self.assertIn("windows-machine", wsl)


if __name__ == "__main__":
    unittest.main()
