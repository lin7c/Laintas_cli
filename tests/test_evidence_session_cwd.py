"""L3 (bughunt): relative evidence paths resolve against the session cwd.

_proposal_evidence resolved relative paths with Path.cwd() — the runner
process's directory, not the project the conversation ran in. Extraction
runs on a background thread whose cwd can be anywhere, so staleness
detection watched the wrong source file (or none at all). The fix hands
the session's cwd down the chain: extract_and_store -> parse_proposals ->
_proposal_evidence, falling back to Path.cwd() when no session is given
(previous behaviour).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mem_extract


class _Dirs:
    """A fake project dir (with the evidence file) and a fake runner dir."""

    def __enter__(self):
        self.proj = Path(tempfile.mkdtemp())
        self.other = Path(tempfile.mkdtemp())
        (self.proj / "auth.py").write_text("TOKEN = 'x'\n", encoding="utf-8")
        self._old_cwd = os.getcwd()
        os.chdir(self.other)  # the runner is somewhere else entirely
        return self

    def __exit__(self, *exc):
        os.chdir(self._old_cwd)


class EvidenceCwdTests(unittest.TestCase):
    def test_relative_path_resolves_against_base_cwd(self):
        with _Dirs() as d:
            got = mem_extract._proposal_evidence(
                {"evidence": ["auth.py"]}, base_cwd=str(d.proj))
            self.assertTrue(got, "session-cwd file not recognised")

    def test_fallback_uses_runner_cwd(self):
        with _Dirs() as d:
            got = mem_extract._proposal_evidence({"evidence": ["auth.py"]})
            self.assertEqual(got, [], "fallback behaviour changed")

    def test_parse_proposals_passes_base_cwd(self):
        with _Dirs() as d:
            reply = json.dumps([{
                "type": "project", "name": "l3-probe",
                "description": "probe", "body": "probe fact",
                "importance": 0.5, "evidence": ["auth.py"],
            }])
            parsed = mem_extract.parse_proposals(reply, base_cwd=str(d.proj))
            self.assertTrue(parsed)
            self.assertTrue(parsed[0]["evidence"],
                            "base_cwd did not reach _proposal_evidence")

    def test_absolute_paths_unaffected(self):
        with _Dirs() as d:
            got = mem_extract._proposal_evidence(
                {"evidence": [str(d.proj / "auth.py")]})
            self.assertTrue(got)

    def test_missing_files_are_dropped(self):
        with _Dirs() as d:
            got = mem_extract._proposal_evidence(
                {"evidence": ["nonexistent.py"]}, base_cwd=str(d.proj))
            self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
