"""Evidence capture and separator-blind dedup on the memory write path.

These pin the two fixes that close the memory lifecycle:

  * a proposal citing a real file stores evidence whose ``sha`` is the same
    fingerprint ``mem_evidence.content_hash`` produces — a different-length
    hand-rolled hash would flag every cited file as changed on the first
    re-check, and the whole stale machinery would be noise;
  * the name-based dedup gate (the offline fallback when embeddings are down)
    is separator-blind: ``aipow-x`` and ``ai-pow-x`` are the same topic, and
    writing both was one way the store filled with near-duplicates.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import memory_system
import mem_extract
import mem_evidence
import mem_review


class EvidenceCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="memev-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        store = self.root / "store"
        self.proj = self.root / "proj"
        store.mkdir()
        self.proj.mkdir()
        self._saved = (memory_system.MEMORY_DIR, memory_system.MEMORY_INDEX)
        memory_system.MEMORY_DIR = store
        memory_system.MEMORY_INDEX = store / "MEMORY.md"
        self.addCleanup(self._restore)
        self._cwd = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, self._cwd)
        # Force the offline path: the name gate is what these tests exercise.
        self._saved_emb = mem_extract.embeddings
        mem_extract.embeddings = None
        self.addCleanup(setattr, mem_extract, "embeddings", self._saved_emb)

    def _restore(self):
        memory_system.MEMORY_DIR, memory_system.MEMORY_INDEX = self._saved

    def _reply(self, *items):
        return lambda messages, **kwargs: json.dumps(items)

    def test_evidence_sha_matches_drift_fingerprint(self):
        (self.proj / "src.py").write_text("x = 1\n", encoding="utf-8")
        written = mem_extract.extract_and_store(
            "convo", self._reply({
                "type": "structure", "name": "claim",
                "description": "d", "body": "from src",
                "importance": 0.6, "evidence": ["src.py"],
            }))
        self.assertIn("claim", written)
        meta = memory_system.read_memory("claim")["meta"]
        evidence = memory_system.parse_evidence(meta.get("evidence"))
        self.assertTrue(evidence)
        self.assertEqual(
            evidence[0]["sha"],
            mem_evidence.content_hash(str((self.proj / "src.py").resolve())))

    def test_missing_evidence_paths_are_not_attested(self):
        written = mem_extract.extract_and_store(
            "convo", self._reply({
                "type": "reference", "name": "no-file",
                "description": "d", "body": "b",
                "importance": 0.5, "evidence": ["does-not-exist.c"],
            }))
        self.assertIn("no-file", written)
        meta = memory_system.read_memory("no-file")["meta"]
        self.assertEqual(memory_system.parse_evidence(meta.get("evidence")), [])

    def test_full_stale_lifecycle_from_extracted_evidence(self):
        (self.proj / "src.py").write_text("x = 1\n", encoding="utf-8")
        mem_extract.extract_and_store("convo", self._reply({
            "type": "structure", "name": "claim",
            "description": "d", "body": "from src",
            "importance": 0.6, "evidence": ["src.py"],
        }))
        meta = memory_system.read_memory("claim")["meta"]
        evidence = memory_system.parse_evidence(meta.get("evidence"))

        # Source unchanged: no drift.
        self.assertEqual(mem_evidence._drift({"evidence": evidence}), [])

        # Source drifts: stale is raised, then a valid verdict re-pins and
        # clears — the cycle that never ran while evidence coverage was zero.
        (self.proj / "src.py").write_text("x = 2\n", encoding="utf-8")
        self.assertTrue(mem_evidence._drift({"evidence": evidence}))
        ok, _ = memory_system.mark_stale("claim", "source changed")
        self.assertTrue(ok)
        result = mem_review.review_stale(
            lambda msgs: json.dumps({"verdict": "valid", "reason": "still true"}))
        self.assertEqual(result[0]["verdict"], "valid")
        self.assertTrue(result[0]["applied"])
        entries = {e["name"]: e["status"] for e in memory_system.list_memories()}
        self.assertEqual(entries.get("claim"), "active")

    def test_separator_blind_name_dedup(self):
        first = mem_extract.extract_and_store("convo", self._reply({
            "type": "reference", "name": "aipow-ocr-integration",
            "description": "d", "body": "b", "importance": 0.5,
        }))
        self.assertEqual(first, ["aipow-ocr-integration"])

        # Same topic, different hyphenation: previously a second entry.
        second = mem_extract.extract_and_store("convo", self._reply({
            "type": "reference", "name": "ai-pow-ocr-integration",
            "description": "d", "body": "b2", "importance": 0.5,
        }))
        self.assertEqual(second, [])
        names = {e["name"] for e in memory_system.list_memories()}
        self.assertEqual(names, {"aipow-ocr-integration"})

    def test_distinct_slugs_still_pass(self):
        written = mem_extract.extract_and_store("convo", self._reply(
            {"type": "reference", "name": "alpha-layout",
             "description": "d", "body": "b", "importance": 0.5},
            {"type": "reference", "name": "beta-layout",
             "description": "d", "body": "b", "importance": 0.5},
        ))
        self.assertEqual(sorted(written), ["alpha-layout", "beta-layout"])


if __name__ == "__main__":
    unittest.main()
