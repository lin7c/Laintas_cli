"""Evidence citations: `path:LINE` is a line reference, not a filename.

seq 30139 — `skill.save` was handed evidence "laintas_cli.py:9190" and
reported a missing file, because only `:\\d+-\\d+$` was recognised as a span
and the single-line form fell through to isfile() with the line number still
glued to the name. That form is what agents actually write and what every
other citation surface here accepts (agent_contract._LINE_REF, the reviewer
contract's "path:line for every finding"), so rejecting it turned a valid
source into a bogus path.

Two properties are pinned here beyond the happy path:

  * A colon is legal in a POSIX filename, so "the spec that actually exists
    wins" — a real file named `weird.py:7` stays a path, and only a spec
    naming nothing has its tail read as a line citation.
  * Every rejection names the expected format. The old message reported a
    path the caller never wrote and never said what form was wanted, which
    is why the model retried the same input instead of fixing it.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools


LINES = 30


class EvidenceLineRefCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="evidence-lineref-")
        self.addCleanup(shutil.rmtree, self.root)
        self.ctx = tools.ToolCtx(cwd=self.root)
        self.path = os.path.join(self.root, "auth.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"line {i}" for i in range(1, LINES + 1)) + "\n")

    def resolve(self, spec):
        built, error = tools._resolve_evidence([spec], self.ctx)
        return built, error

    # ── accepted forms ───────────────────────────────────────────────────
    def test_bare_path_has_no_span(self):
        built, error = self.resolve("auth.py")
        self.assertEqual("", error)
        self.assertNotIn("start", built[0])
        self.assertNotIn("end", built[0])

    def test_single_line_ref_is_the_regression(self):
        """`path:LINE` — the seq 30139 input — resolves to a one-line span."""
        built, error = self.resolve("auth.py:7")
        self.assertEqual("", error)
        self.assertEqual(7, built[0]["start"])
        self.assertEqual(7, built[0]["end"])
        self.assertEqual(self.path, built[0]["path"])

    def test_range_ref(self):
        built, error = self.resolve("auth.py:7-9")
        self.assertEqual("", error)
        self.assertEqual((7, 9), (built[0]["start"], built[0]["end"]))

    def test_absolute_single_line_ref(self):
        built, error = self.resolve(f"{self.path}:12")
        self.assertEqual("", error)
        self.assertEqual(12, built[0]["start"])

    def test_last_line_is_in_range(self):
        built, error = self.resolve(f"auth.py:{LINES}")
        self.assertEqual("", error)
        self.assertEqual(LINES, built[0]["end"])

    def test_fingerprint_is_recorded(self):
        built, _ = self.resolve("auth.py:7")
        self.assertTrue(built[0].get("sha"), "evidence stored without a hash")

    # ── a colon can be part of a real filename ───────────────────────────
    def test_existing_file_with_colon_in_name_stays_a_path(self):
        odd = os.path.join(self.root, "weird.py:7")
        with open(odd, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        built, error = self.resolve("weird.py:7")
        self.assertEqual("", error)
        self.assertEqual(odd, built[0]["path"])
        self.assertNotIn("start", built[0], "existing file misread as a span")

    def test_line_ref_on_a_colon_named_file(self):
        odd = os.path.join(self.root, "notes:2026.md")
        with open(odd, "w", encoding="utf-8") as fh:
            fh.write("a\nb\nc\n")
        built, error = self.resolve("notes:2026.md:2")
        self.assertEqual("", error)
        self.assertEqual(odd, built[0]["path"])
        self.assertEqual(2, built[0]["start"])

    # ── rejections, each naming the contract ─────────────────────────────
    def assertRejected(self, spec, *fragments):
        built, error = self.resolve(spec)
        self.assertIsNone(built, f"{spec!r} should not resolve")
        self.assertTrue(error, f"{spec!r} rejected without an error message")
        for fragment in fragments:
            self.assertIn(fragment, error, f"{spec!r}: {error!r}")
        self.assertIn(tools._EVIDENCE_FORMAT_HINT, error,
                      f"{spec!r} rejection does not state the expected format")

    def test_line_past_eof_is_rejected(self):
        """Recording a citation past EOF would point the memory at nothing."""
        self.assertRejected("auth.py:99", "line 99", f"only {LINES} line")

    def test_reversed_range_is_rejected(self):
        self.assertRejected("auth.py:9-4", "reversed")

    def test_line_zero_is_rejected(self):
        self.assertRejected("auth.py:0", "not a valid line number")

    def test_missing_file_echoes_the_original_spec(self):
        """The old message named a path the caller never wrote."""
        self.assertRejected("missing.py:3", "missing.py", "'missing.py:3'")

    def test_missing_bare_file(self):
        self.assertRejected("missing.py", "missing.py")

    def test_empty_and_blank_specs_are_skipped(self):
        built, error = tools._resolve_evidence(["", "   "], self.ctx)
        self.assertIsNone(built)
        self.assertEqual("", error)

    def test_no_evidence_is_not_an_error(self):
        self.assertEqual((None, ""), tools._resolve_evidence(None, self.ctx))
        self.assertEqual((None, ""), tools._resolve_evidence([], self.ctx))

    # ── the schema is the contract the model reads ───────────────────────
    def test_schemas_advertise_the_single_line_form(self):
        """A schema that omits `path:LINE` is what made the model write it
        into a field documented as `path:START-END` only."""
        registry = tools.get_registry()
        named = {tool.name: tool for tool in registry.list()}
        for tool_name in ("mem.save", "skill.save"):
            description = named[tool_name].schema["properties"]["evidence"]["description"]
            self.assertIn("path:LINE", description,
                          f"{tool_name} evidence schema omits the single-line form")


if __name__ == "__main__":
    unittest.main()
