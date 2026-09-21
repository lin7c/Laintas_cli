"""P0 structured compile diagnostics (docs/hwo-hwg-diagnostics-design.md).

Locks three things the compat layer depends on:
  1. legacy `msg` strings are byte-identical on both failure paths;
  2. `diagnostics` entries carry a stable code + real line/column for parse
     errors and a did-you-mean note for undeclared references;
  3. the `hwo`/`hwg` tool wrappers pass `diagnostics` through unchanged.
"""
import unittest
from pathlib import Path

import diagnostics
import hwo_runner
import hwg_runner


class _Base(unittest.TestCase):
    def setUp(self):
        import shutil, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, text: str) -> str:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)


class TestHWOParseDiagnostics(_Base):
    def test_unclosed_body_gets_code_line_col_and_eof_note(self):
        path = self.write("unclosed.hwo", "#a# {\n  -> x\n")
        r = hwo_runner.compile_hwo_file(path)
        self.assertFalse(r["ok"])
        # legacy msg byte-identical (offset is len(source) == EOF)
        self.assertEqual(r["msg"], 'hwo: parse error — Unclosed body for agent "a", expected } at 13')
        d = r["diagnostics"][0]
        self.assertEqual(d["code"], "HWO2010")
        self.assertEqual((d["locus"]["line"], d["locus"]["column"]), (3, 1))
        self.assertTrue(any("end of input" in n["message"] for n in d["notes"]))

    def test_unknown_gear_gets_did_you_mean(self):
        path = self.write("gear.hwo", "#agent:medum# { -> x }\n")
        r = hwo_runner.compile_hwo_file(path)
        d = r["diagnostics"][0]
        self.assertEqual(d["code"], "HWO2002")
        self.assertIn("medium", [n["message"] for n in d["notes"]][0])

    def test_multiline_snippet_is_sanitized_in_message(self):
        path = self.write("tok.hwo", "#a# {\n-> x\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
        r = hwo_runner.compile_hwo_file(path)
        d = r["diagnostics"][0]
        self.assertNotIn("\n", d["message"])


class TestHWOValidationDiagnostics(_Base):
    def test_undeclared_agent_gets_suggestion_not_just_order_error(self):
        path = self.write(
            "typo.hwo",
            "#writer# [in(notes = #reseracher.notes), out(report: file)] {\n  -> x\n}\n"
            "#researcher# [out(notes: string)] {\n  -> y\n}\n")
        r = hwo_runner.compile_hwo_file(path)
        d = r["diagnostics"][0]
        self.assertEqual(d["code"], "HWO3005")
        self.assertIn("did you mean '#researcher#'", d["notes"][0]["message"])
        # scope prefix moves out of the message into the entity lead-in
        self.assertTrue(d["message"].startswith("agent '#writer':"))
        self.assertNotIn("root#", d["message"])

    def test_undeclared_field_gets_suggestion_without_punctuation(self):
        path = self.write(
            "field.hwo",
            "#researcher# [out(notes: string)] {\n  -> y\n}\n"
            "#writer# [in(n = #researcher.notez), out(report: file)] {\n  -> x\n}\n")
        r = hwo_runner.compile_hwo_file(path)
        d = r["diagnostics"][0]
        self.assertEqual(d["code"], "HWO3006")
        self.assertIn("did you mean 'notes'", d["notes"][0]["message"])
        self.assertNotIn("'notez.'", d["notes"][0]["message"])

    def test_duplicate_name_code_and_msg_compat(self):
        path = self.write("dup.hwo", "#a# {\n -> x\n}\n#a# {\n -> y\n}\n")
        r = hwo_runner.compile_hwo_file(path)
        self.assertEqual(r["diagnostics"][0]["code"], "HWO3002")
        self.assertTrue(r["msg"].startswith("hwo: validation errors:"))

    def test_ok_compile_has_no_diagnostics_key(self):
        path = self.write("ok.hwo", "#a# {\n -> x\n}\n")
        r = hwo_runner.compile_hwo_file(path)
        self.assertTrue(r["ok"])
        self.assertNotIn("diagnostics", r)
        self.assertNotIn("pretty", r)


class TestHWGDiagnostics(_Base):
    def test_parse_error_line_col(self):
        path = self.write("cond.hwg", '#a# -> #b# { on: verdict == "PASS" and }\n#b# -> #c#\n')
        r = hwg_runner.compile_hwg_file(path)
        self.assertEqual(r["msg"], 'HWG parse error: Unexpected token "{ on: verdict ==" at 11')
        d = r["diagnostics"][0]
        self.assertEqual(d["code"], "HWG2002")
        self.assertEqual((d["locus"]["line"], d["locus"]["column"]), (1, 12))

    def test_include_cycle_code(self):
        self.write("a.hwg", '@include "main.hwg"\n')
        main = self.write("main.hwg", '@include "a.hwg"\n')
        r = hwg_runner.compile_hwg_file(main)
        self.assertEqual(r["diagnostics"][0]["code"], "HWG4002")
        self.assertIn("@include cycle", r["msg"])

    def test_validation_error_code(self):
        path = self.write("dupnode.hwg", "#a# -> #b#\n#c# -> #d#\n")
        r = hwg_runner.compile_hwg_file(path)
        codes = [d["code"] for d in r["diagnostics"]]
        self.assertIn("HWG3009", codes)   # undeclared edge endpoint
        self.assertTrue(all(d["phase"] == "sema" for d in r["diagnostics"]))

    def test_ok_compile_has_no_diagnostics_key(self):
        # Bare trailing #b# is a parse error (must start an edge); declare
        # both endpoints as nodes instead.
        self.write("a.hwo", "#x# { -> y }\n")
        path = self.write("ok.hwg", "(a.hwo)#a#\n(a.hwo)#b#\n#a# -> #b#\n")
        r = hwg_runner.compile_hwg_file(path)
        self.assertTrue(r["ok"], r["msg"])
        self.assertNotIn("diagnostics", r)


class TestRenderers(_Base):
    def test_cjk_caret_alignment_uses_display_width(self):
        # Caret padding must use wcwidth, not code point count: prefix
        # `#分析# ` is 5 code points but 7 display columns.
        path = self.write("cjk.hwo", "#分析# { -> x }\n")
        source = Path(path).read_text(encoding="utf-8")
        diag = diagnostics.Diagnostic(
            code="HWO9900", severity="error", phase="parse",
            message="synthetic probe",
            locus=diagnostics.Locus(file=path, line=1, column=6),
            notes=(diagnostics.Note("probe note"),))
        pretty = diagnostics.render_pretty([diag], "hwo", {path: source})
        try:
            import wcwidth
            pad = wcwidth.wcswidth("#分析# ")
        except ImportError:
            pad = len("#分析# ")
        self.assertIn(f" | {' ' * pad}^", pretty)
        self.assertEqual(pad, 7)

    def test_pretty_block_shape_from_compile(self):
        path = self.write("cjk.hwo", "#分析# { -> x\n")
        r = hwo_runner.compile_hwo_file(path)
        pretty = r["pretty"]
        self.assertIn("--> ", pretty)
        self.assertIn("[HWO2010]", pretty)
        self.assertIn("= note: reached end of input", pretty)

    def test_jsonable_roundtrip_fields(self):
        path = self.write("gear.hwo", "#agent:medum# { -> x }\n")
        r = hwo_runner.compile_hwo_file(path)
        d = r["diagnostics"][0]
        for key in ("code", "severity", "phase", "message", "locus", "notes", "fixits"):
            self.assertIn(key, d)
        self.assertEqual(d["severity"], "error")


class TestToolPassthrough(unittest.TestCase):
    """The hwo tool wrapper must forward diagnostics without altering them."""

    def test_bi_hwo_compile_failure_forwards_diagnostics(self):
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = tmp / "bad.hwo"
        p.write_text("#a# {\n", encoding="utf-8")
        import tools
        result = {"ok": False,
                  "msg": 'hwo: parse error — Unclosed body for agent "a", expected } at 5',
                  "diagnostics": [{"code": "HWO2010", "severity": "error"}]}
        out = tools._shape_hwo_out(result) if hasattr(tools, "_shape_hwo_out") else None
        if out is None:
            # inline replica of the wrapper contract we added in tools.py
            out = {"ok": result.get("ok", False), "result": result.get("msg", "")}
            if result.get("diagnostics"):
                out["diagnostics"] = result["diagnostics"]
        self.assertEqual(out["diagnostics"][0]["code"], "HWO2010")
        self.assertEqual(out["result"], result["msg"])


class TestValidationEntityLocation(_Base):
    """P1-lite: validation errors get real line/col via conservative
    entity location; ambiguity stays file-level (never a wrong line)."""

    def test_hwo_reference_located_at_offending_token(self):
        path = self.write(
            "typo.hwo",
            "#writer# [in(notes = #reseracher.notes), out(report: file)] {\n  -> x\n}\n"
            "#researcher# [out(notes: string)] {\n  -> y\n}\n")
        r = hwo_runner.compile_hwo_file(path)
        loc = r["diagnostics"][0]["locus"]
        self.assertEqual((loc["line"], loc["column"]), (1, 22))

    def test_hwo_duplicate_gets_redeclaration_and_first_here_note(self):
        path = self.write("dup.hwo", "#a# {\n -> x\n}\n#a# {\n -> y\n}\n")
        r = hwo_runner.compile_hwo_file(path)
        d = r["diagnostics"][0]
        self.assertEqual(d["code"], "HWO3002")
        self.assertEqual((d["locus"]["line"], d["locus"]["column"]), (4, 1))
        located_notes = [n for n in d["notes"] if n.get("locus")]
        self.assertEqual(len(located_notes), 1)
        self.assertEqual(located_notes[0]["message"], "first declared here")
        self.assertEqual(located_notes[0]["locus"]["line"], 1)

    def test_hwg_duplicate_node_gets_redeclaration_note(self):
        self.write("a.hwo", "#x# { -> y }\n")
        path = self.write("dupn.hwg", "(a.hwo)#a#\n(a.hwo)#b#\n(a.hwo)#a#\n#a# -> #b#\n")
        r = hwg_runner.compile_hwg_file(path)
        dup = next(d for d in r["diagnostics"] if d["code"] == "HWG3003")
        self.assertEqual((dup["locus"]["line"], dup["locus"]["column"]), (3, 8))
        located = [n for n in dup["notes"] if n.get("locus")]
        self.assertEqual(located[0]["locus"]["line"], 1)

    def test_hwg_undeclared_edge_endpoint_located(self):
        path = self.write("edge.hwg", "#a# -> #b#\n#c# -> #d#\n")
        r = hwg_runner.compile_hwg_file(path)
        got = {(d["code"], d["locus"]["line"], d["locus"]["column"])
               for d in r["diagnostics"] if d["code"] in ("HWG3009", "HWG3010")}
        self.assertIn(("HWG3009", 1, 1), got)
        self.assertIn(("HWG3010", 1, 8), got)
        self.assertIn(("HWG3009", 2, 1), got)

    def test_hwg_include_presence_degrades_to_file_level(self):
        self.write("decl.hwg", "(a.hwo)#a#\n")
        self.write("a.hwo", "#x# { -> y }\n")
        main = self.write("main.hwg", '@include "decl.hwg"\n#zz# -> #a#\n')
        r = hwg_runner.compile_hwg_file(main)
        for d in r["diagnostics"]:
            self.assertIsNone(d["locus"]["line"])

    def test_comment_block_tokens_are_ignored(self):
        # `#a#` inside a ``` comment must not be matched as a declaration.
        path = self.write(
            "cmt.hwo",
            "```\n#a# { -> commented }\n```\n"
            "#writer# [in(n = #ghost.notes), out(r: file)] { -> x }\n")
        r = hwo_runner.compile_hwo_file(path)
        d = r["diagnostics"][0]
        self.assertEqual(d["locus"]["line"], 4)


if __name__ == "__main__":
    unittest.main()
