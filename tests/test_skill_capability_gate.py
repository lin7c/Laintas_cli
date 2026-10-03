"""A skill about tools that are not here should not be in the prompt.

Optional service capabilities may come and go. Describing unavailable tools
in every prompt invites the model to plan around work it cannot execute.

`requires_tool` is the gate. It is deliberately narrow: absent means
unconditional, which is what almost every skill is.
"""

import unittest

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
        get_registry().unregister("optional.snapshot")

    def test_a_skill_with_no_requirement_is_always_available(self):
        self.assertTrue(skills_mod.skill_is_available(_meta("git")))

    def test_a_skill_is_hidden_while_its_tool_is_absent(self):
        self.assertFalse(
            skills_mod.skill_is_available(_meta("optional-service",
                                                "optional.snapshot")))

    def test_a_skill_appears_once_its_tool_registers(self):
        get_registry().register(_tool("optional.snapshot"))
        self.assertTrue(
            skills_mod.skill_is_available(_meta("optional-service",
                                                "optional.snapshot")))

    def test_it_disappears_again_when_the_tool_goes(self):
        get_registry().register(_tool("optional.snapshot"))
        get_registry().unregister("optional.snapshot")
        self.assertFalse(
            skills_mod.skill_is_available(_meta("optional-service",
                                                "optional.snapshot")))


class CatalogTests(unittest.TestCase):
    """The rendered catalog, which is what actually reaches the prompt."""

    def setUp(self):
        self._saved = dict(skills_mod._skill_metadata)
        skills_mod._skill_metadata.clear()
        skills_mod._skill_metadata.update({
            "git": _meta("git"),
            "optional-service": _meta("optional-service", "optional.snapshot"),
        })
        skills_mod._scan_done = True
        skills_mod._scan_project = str(skills_mod.project_skills_dir())

    def tearDown(self):
        skills_mod._skill_metadata.clear()
        skills_mod._skill_metadata.update(self._saved)
        skills_mod.invalidate_scan()
        get_registry().unregister("optional.snapshot")

    def test_the_gated_skill_is_absent_without_a_service(self):
        catalog = skills_mod.describe_skills_for_prompt()
        self.assertIn("git", catalog)
        self.assertNotIn("optional-service", catalog)

    def test_the_gated_skill_is_listed_with_a_service(self):
        get_registry().register(_tool("optional.snapshot"))
        catalog = skills_mod.describe_skills_for_prompt()
        self.assertIn("optional-service", catalog)


if __name__ == "__main__":
    unittest.main()
