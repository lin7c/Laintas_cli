"""Skills the agent writes: project scope, authorship, lifecycle, counters.

A learned skill is the procedural half of what a session leaves behind — "how
to work in THIS repo" — and it reuses the installed-skill container so it gets
the existing catalogue, routing and `skill.load` rather than a second store
competing for the same prompt slot.

Three properties carry the design, and each has a specific failure it prevents:

  * PROJECT SCOPE. Skill selection degrades monotonically with catalogue size
    (-8% at 52 skills, -14% at 102, -21% at 202; arXiv 2605.24050) because a
    distractor description shadows the right one. A repo-specific lesson in the
    global store would charge that to every other repo.
  * AUTHORSHIP. Counters, staleness and retirement all rewrite SKILL.md in
    place. A human-installed skill is usually a file in someone's git repo, so
    nothing here may touch one.
  * DOCUMENTATION ONLY. skill.save writes SKILL.md and never skill.py: an
    agent that could author executable skills would persist arbitrary code into
    every later session, behind a trust prompt the user answered for somebody
    else's code.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import mem_evidence
import memory_system
import skills
import tools


class LearnedSkillCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="learned-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.proj = Path(self.root) / "proj"
        self.user_skills = Path(self.root) / "user_skills"
        self.proj.mkdir()
        self.user_skills.mkdir()

        self._saved_dir = skills.SKILLS_DIR
        self._saved_bundled = skills.BUNDLED_SKILLS_DIR
        skills.SKILLS_DIR = self.user_skills
        # Point the bundled set at an empty dir: these tests are about what the
        # agent writes, not about what ships.
        skills.BUNDLED_SKILLS_DIR = Path(self.root) / "no_bundled"
        self.addCleanup(self._restore)

        self._cwd = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, self._cwd)
        skills.invalidate_scan()
        self.addCleanup(skills.invalidate_scan)
        mem_evidence.invalidate_cache()

        self.src = self.proj / "vite.config.ts"
        self.write_src("hmr: true\n")
        self.ctx = tools.ToolCtx(cwd=str(self.proj))

    def _restore(self):
        skills.SKILLS_DIR = self._saved_dir
        skills.BUNDLED_SKILLS_DIR = self._saved_bundled
        skills._skill_states.clear()

    def write_src(self, text):
        self.src.write_text(text, encoding="utf-8")
        mem_evidence.invalidate_cache()

    def save(self, name="vite-hmr", cite=True, **over):
        args = {"name": name, "description": "when HMR fails here",
                "body": "restart the dev server", **over}
        if cite:
            args.setdefault("evidence", ["vite.config.ts"])
        return tools._bi_skill_save(args, self.ctx)

    def catalogue(self):
        skills.invalidate_scan()
        return skills.get_all_metadata()

    def status(self, name):
        for item in skills.learned_skills(include_retired=True):
            if item["name"] == name:
                return item["status"]
        return None


class ScopeTests(LearnedSkillCase):
    def test_saved_into_the_project_by_default(self):
        result = self.save()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue((self.proj / ".laintas/skills/vite-hmr/SKILL.md").is_file())
        self.assertEqual(self.catalogue()["vite-hmr"].scope, skills.SCOPE_PROJECT)

    def test_not_visible_from_another_project(self):
        self.save()
        self.assertIn("vite-hmr", self.catalogue())
        other = Path(self.root) / "other"
        other.mkdir()
        os.chdir(other)
        self.assertNotIn("vite-hmr", self.catalogue(),
                         "a repo-specific lesson must not follow you to another repo")

    def test_user_scope_is_visible_everywhere(self):
        self.save(name="always-on", cite=False, scope="user")
        other = Path(self.root) / "other2"
        other.mkdir()
        os.chdir(other)
        self.assertIn("always-on", self.catalogue())

    def test_project_shadows_user_of_the_same_name(self):
        self.save(name="shared", cite=False, scope="user")
        self.save(name="shared", cite=False, scope="project",
                  description="the project-specific one")
        meta = self.catalogue()["shared"]
        self.assertEqual(meta.scope, skills.SCOPE_PROJECT)
        self.assertEqual(meta.description, "the project-specific one")

    def test_a_cd_rescans(self):
        self.save()
        self.assertIn("vite-hmr", skills.get_all_metadata())
        other = Path(self.root) / "other3"
        other.mkdir()
        os.chdir(other)
        # No explicit invalidation: get_all_metadata must notice the move by
        # itself, or it serves another project's catalogue.
        self.assertNotIn("vite-hmr", skills.get_all_metadata())


class AuthorshipTests(LearnedSkillCase):
    def _install_human_skill(self, name="handwritten"):
        d = self.user_skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: a person wrote this\n---\n\nbody\n",
            encoding="utf-8")
        skills.invalidate_scan()
        return d

    def test_learned_listing_excludes_installed_skills(self):
        self._install_human_skill()
        self.save()
        names = [item["name"] for item in skills.learned_skills()]
        self.assertEqual(names, ["vite-hmr"])

    def test_will_not_overwrite_a_human_authored_skill(self):
        self._install_human_skill("handwritten")
        result = self.save(name="handwritten", cite=False, scope="user")
        self.assertFalse(result["ok"])
        self.assertIn("written by a person", result["error"])

    def test_counters_skip_installed_skills(self):
        self._install_human_skill("handwritten")
        self.save()
        before = (self.user_skills / "handwritten/SKILL.md").read_text(encoding="utf-8")
        touched = skills.record_outcome(["vite-hmr", "handwritten"], helpful=True)
        self.assertEqual(touched, ["vite-hmr"])
        self.assertEqual(
            before, (self.user_skills / "handwritten/SKILL.md").read_text(encoding="utf-8"),
            "a human-authored SKILL.md must come back byte-identical")

    def test_will_not_touch_an_executable_skill(self):
        d = self.user_skills / "executable"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: executable\n---\nb\n", encoding="utf-8")
        (d / "skill.py").write_text("def get_tools():\n    return []\n", encoding="utf-8")
        skills.invalidate_scan()
        result = self.save(name="executable", cite=False, scope="user")
        self.assertFalse(result["ok"])
        self.assertIn("executable", result["error"])

    def test_writes_documentation_never_code(self):
        self.save()
        entries = os.listdir(self.proj / ".laintas/skills/vite-hmr")
        self.assertEqual(entries, ["SKILL.md"])


class LifecycleTests(LearnedSkillCase):
    def test_source_change_flags_the_skill_stale(self):
        self.save()
        self.write_src("hmr: false\n")
        self.assertEqual(mem_evidence.propagate([str(self.src)]), ["vite-hmr"])
        self.assertEqual(self.status("vite-hmr"), "stale")

    def test_identical_rewrite_flags_nothing(self):
        self.save()
        self.write_src("hmr: true\n")
        self.assertEqual(mem_evidence.propagate([str(self.src)]), [])

    def test_a_stale_skill_stays_in_the_catalogue(self):
        self.save()
        self.write_src("hmr: false\n")
        mem_evidence.propagate([str(self.src)])
        self.assertIn("vite-hmr", self.catalogue(),
                      "hiding it would lose the lesson; it must be flagged instead")

    def test_revert_heals_with_no_model_call(self):
        self.save()
        self.write_src("hmr: false\n")
        mem_evidence.propagate([str(self.src)])
        self.write_src("hmr: true\n")
        self.assertIn("vite-hmr", mem_evidence.reconcile())
        self.assertEqual(self.status("vite-hmr"), "active")

    def test_prose_rewrite_does_not_clear_the_flag(self):
        self.save()
        self.write_src("hmr: false\n")
        mem_evidence.propagate([str(self.src)])
        self.save(cite=False, body="reworded but not re-checked")
        self.assertEqual(self.status("vite-hmr"), "stale")

    def test_re_attesting_evidence_clears_the_flag(self):
        self.save()
        self.write_src("hmr: false\n")
        mem_evidence.propagate([str(self.src)])
        self.save()  # cites the file again, at its current content
        self.assertEqual(self.status("vite-hmr"), "active")

    def test_supersede_retires_the_old_and_keeps_its_file(self):
        self.save()
        result = self.save(name="vite-hmr-2", cite=False,
                           description="newer", supersedes="vite-hmr")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(self.status("vite-hmr"), "superseded")
        self.assertTrue((self.proj / ".laintas/skills/vite-hmr/SKILL.md").is_file())
        self.assertNotIn("vite-hmr", self.catalogue(),
                         "a retired skill must leave the catalogue it shadows")
        self.assertIn("vite-hmr-2", self.catalogue())

    def test_forget_requires_a_reason(self):
        self.save()
        self.assertFalse(tools._bi_skill_forget({"name": "vite-hmr"}, self.ctx)["ok"])
        result = tools._bi_skill_forget(
            {"name": "vite-hmr", "reason": "upstream fixed it"}, self.ctx)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(self.status("vite-hmr"), "superseded")

    def test_missing_evidence_file_is_refused_before_writing(self):
        result = self.save(evidence=["does-not-exist.ts"])
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])
        self.assertEqual(skills.learned_skills(), [])


class CounterTests(LearnedSkillCase):
    def test_counters_accumulate_and_survive_updates(self):
        self.save()
        skills.record_outcome(["vite-hmr"], helpful=True)
        skills.record_outcome(["vite-hmr"], helpful=True)
        skills.record_outcome(["vite-hmr"], helpful=False)
        item = skills.learned_skills()[0]
        self.assertEqual((item["helpful"], item["harmful"]), (2, 1))
        self.save(cite=False, body="revised guidance")
        item = skills.learned_skills()[0]
        self.assertEqual((item["helpful"], item["harmful"]), (2, 1),
                         "rewriting the text must not reset what it is worth")

    def test_unknown_skill_is_a_quiet_no_op(self):
        self.assertEqual(skills.record_outcome(["nope"], helpful=True), [])


class ValidationTests(LearnedSkillCase):
    def test_name_must_be_a_slug(self):
        for bad in ("Bad Name", "../escape", "x", "UPPER"):
            self.assertFalse(self.save(name=bad, cite=False)["ok"], bad)

    def test_description_and_body_are_required(self):
        self.assertFalse(self.save(cite=False, description="")["ok"])
        self.assertFalse(self.save(cite=False, body="")["ok"])

    def test_body_is_bounded(self):
        big = "x" * (skills.LEARNED_SKILL_MAX_BODY + 1)
        self.assertFalse(self.save(cite=False, body=big)["ok"])


if __name__ == "__main__":
    unittest.main()
