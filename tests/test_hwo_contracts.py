import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hwo_runner


class _Chdir:
    def __init__(self, path):
        self.path = path
        self.old = None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *_exc):
        os.chdir(self.old)


class HwoContractTests(unittest.TestCase):
    def test_agent_file_output_is_verified_as_a_real_artifact(self):
        io = {"out": [{"name": "report", "type": "file"}]}
        with tempfile.TemporaryDirectory() as tmp:
            gaps = hwo_runner._io_contract_gaps(
                io, {"report": "report.md"}, tmp)
            self.assertTrue(gaps)
            self.assertIn("not a file that exists", gaps[0])

            Path(tmp, "report.md").write_text("result", encoding="utf-8")
            self.assertEqual([], hwo_runner._io_contract_gaps(
                io, {"report": "report.md"}, tmp))

    def test_file_level_contract_fails_a_silent_success(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwo").write_text(
                "@line [out(report: file)]\n-> finish the report\n",
                encoding="utf-8")
            with mock.patch.object(hwo_runner, "run_sequence", return_value={
                    "ok": True, "msg": "done", "outputs": {}}):
                result = hwo_runner.run_hwo_file(
                    "flow.hwo", deps=object(), session={})

            self.assertFalse(result["ok"])
            self.assertIn("HWO workflow finished without honouring", result["msg"])
            self.assertIn("missing required output 'report'", result["msg"])


class DeclaredTypeTests(unittest.TestCase):
    def test_a_misspelled_type_is_reported_not_silently_downgraded(self):
        """`out(report: fil)` becomes `string`, so the file check the author
        asked for never runs — the contract reads stricter than it is."""
        import agent_contract
        self.assertEqual(
            ["report: fil"],
            agent_contract.unknown_io_types({"out": [{"name": "report",
                                                      "type": "fil"}]}))
        self.assertEqual([], agent_contract.unknown_io_types(
            {"out": [{"name": "report", "type": "file"},
                     {"name": "note", "type": "string"},
                     {"name": "plain", "type": ""}]}))

    def test_a_node_declaring_an_unknown_type_fails_its_contract_check(self):
        import hwg_runner
        node = {"id": "n", "file": "n.hwo",
                "io": {"out": [{"name": "report", "type": "fil"}]}}
        errors = hwg_runner._output_contract_errors(
            node, "", {"report": "anything"}, cwd=".")
        self.assertTrue(any("unknown declared type" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
