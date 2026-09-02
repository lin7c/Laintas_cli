"""Evidence-backed memories: staleness propagation, review, and the idle pass.

A memory that cites code is a claim about code, and code moves. Before this,
nothing noticed: the claim stayed in the prompt at full confidence long after
the file it described was rewritten, which is the failure mode a persistent
store makes WORSE than no store, because a confident stale memory outranks a
fresh read.

The rules these tests pin down, in the order they matter:

  * a write that leaves the bytes identical flags nothing (content hashing,
    not mtime — otherwise every formatter run and every `git checkout` would
    manufacture staleness);
  * flagging is never deleting, and rewriting a stale memory's prose does not
    clear its flag — only re-attesting evidence does;
  * an edit that gets reverted heals with no model call at all;
  * `update` writes a SUCCESSOR and supersedes the original, so Y → Y' stays
    walkable instead of the old belief being overwritten and lost.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import memory_system
import mem_evidence
import mem_review


class MemoryStoreCase(unittest.TestCase):
    """Redirects the memory store and cwd at a temp dir for each test."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="memassert-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = Path(self.root) / "store"
        self.proj = Path(self.root) / "proj"
        self.store.mkdir()
        self.proj.mkdir()

        self._saved = (memory_system.MEMORY_DIR, memory_system.MEMORY_INDEX)
        memory_system.MEMORY_DIR = self.store
        memory_system.MEMORY_INDEX = self.store / "MEMORY.md"
        self.addCleanup(self._restore)

        self._cwd = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, self._cwd)
        mem_evidence.invalidate_cache()
        self.addCleanup(mem_evidence.invalidate_cache)

        self.src = str(self.proj / "auth.py")
        self.write_src("def login():\n    return 1\n")

    def _restore(self):
        memory_system.MEMORY_DIR, memory_system.MEMORY_INDEX = self._saved

    def write_src(self, text):
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write(text)
        mem_evidence.invalidate_cache()

    def save(self, name="auth-flow", description="login returns 1",
             body="body", evidence=True):
        ev = [mem_evidence.evidence_for(self.src, start=1, end=2)] if evidence else None
        ok, msg = memory_system.write_memory(
            name, "structure", description, body, evidence=ev)
        self.assertTrue(ok, msg)
        mem_evidence.invalidate_cache()

    def status(self, name):
        for entry in memory_system.list_memories(include_superseded=True):
            if entry["name"] == name:
                return entry["status"]
        return None


class EvidenceCodecTests(MemoryStoreCase):
    def test_round_trip_with_and_without_range(self):
        items = [{"path": "/a/b.py", "sha": "abc123abc123", "start": 3, "end": 9},
                 {"path": "/c/d.py", "sha": "0123456789ab"}]
        encoded = memory_system.format_evidence(items)
        self.assertEqual(encoded,
                         "/a/b.py@abc123abc123:3-9;/c/d.py@0123456789ab")
        self.assertEqual(memory_system.parse_evidence(encoded), items)

    def test_unparseable_items_are_dropped_not_raised(self):
        parsed = memory_system.parse_evidence("garbage;;/a/b.py@abc123")
        self.assertEqual(parsed, [{"path": "/a/b.py", "sha": "abc123"}])


class PropagationTests(MemoryStoreCase):
    def test_identical_rewrite_flags_nothing(self):
        self.save()
        self.write_src("def login():\n    return 1\n")  # same bytes
        self.assertEqual(mem_evidence.propagate([self.src]), [])
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_ACTIVE)

    def test_real_edit_flags_the_citing_memory(self):
        self.save()
        self.write_src("def login():\n    return 2\n")
        self.assertEqual(mem_evidence.propagate([self.src]), ["auth-flow"])
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_STALE)

    def test_flagging_is_idempotent(self):
        self.save()
        self.write_src("def login():\n    return 2\n")
        mem_evidence.propagate([self.src])
        self.assertEqual(mem_evidence.propagate([self.src]), [])

    def test_unrelated_file_flags_nothing(self):
        self.save()
        other = str(self.proj / "other.py")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        self.assertEqual(mem_evidence.propagate([other]), [])

    def test_memory_without_evidence_is_never_flagged(self):
        self.save(evidence=False)
        self.write_src("totally different\n")
        self.assertEqual(mem_evidence.propagate([self.src]), [])

    def test_deleted_source_flags_with_a_readable_reason(self):
        self.save()
        os.unlink(self.src)
        mem_evidence.invalidate_cache()
        self.assertEqual(mem_evidence.propagate([self.src]), ["auth-flow"])
        entry = memory_system.list_stale()[0]
        self.assertIn("deleted", entry["stale_reason"])

    def test_revert_heals_without_a_model_call(self):
        self.save()
        self.write_src("def login():\n    return 2\n")
        mem_evidence.propagate([self.src])
        self.write_src("def login():\n    return 1\n")
        self.assertEqual(mem_evidence.reconcile(), ["auth-flow"])
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_ACTIVE)


class LifecycleTests(MemoryStoreCase):
    def test_prose_rewrite_preserves_evidence_and_keeps_the_flag(self):
        self.save()
        self.write_src("def login():\n    return 2\n")
        mem_evidence.propagate([self.src])
        memory_system.write_memory("auth-flow", "structure", "reworded", "new prose")
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_STALE)
        entry = memory_system.list_stale()[0]
        self.assertTrue(entry["evidence"], "evidence must survive a prose rewrite")

    def test_re_attesting_evidence_clears_the_flag(self):
        self.save()
        self.write_src("def login():\n    return 2\n")
        mem_evidence.propagate([self.src])
        self.save(description="login returns 2")  # re-attests current content
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_ACTIVE)

    def test_stale_entries_are_labelled_in_the_prompt_not_hidden(self):
        self.save()
        self.write_src("def login():\n    return 2\n")
        mem_evidence.propagate([self.src])
        prompt = memory_system.load_all_for_prompt()
        self.assertIn("auth-flow", prompt)
        self.assertIn("STALE", prompt)

    def test_supersede_keeps_the_file_and_drops_the_index_entry(self):
        self.save()
        self.save(name="auth-flow-2", description="login returns 2")
        ok, _ = memory_system.supersede_memory("auth-flow", "auth-flow-2")
        self.assertTrue(ok)
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_SUPERSEDED)
        self.assertTrue((self.store / "auth-flow.md").exists())
        visible = [entry["name"] for entry in memory_system.list_memories()]
        self.assertEqual(visible, ["auth-flow-2"])
        index = memory_system.MEMORY_INDEX.read_text(encoding="utf-8")
        self.assertNotIn("(auth-flow.md)", index)
        self.assertIn("(auth-flow-2.md)", index)

    def test_supersede_rejects_self_and_missing_successor(self):
        self.save()
        self.assertFalse(memory_system.supersede_memory("auth-flow", "auth-flow")[0])
        self.assertFalse(memory_system.supersede_memory("auth-flow", "nope")[0])

    def test_retire_keeps_the_file_with_a_reason(self):
        self.save()
        ok, _ = memory_system.retire_memory("auth-flow", "endpoint removed")
        self.assertTrue(ok)
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_SUPERSEDED)
        self.assertTrue((self.store / "auth-flow.md").exists())

    def test_successor_names_increment(self):
        self.save()
        self.assertEqual(memory_system.successor_name("auth-flow"), "auth-flow-2")
        self.save(name="auth-flow-2")
        self.assertEqual(memory_system.successor_name("auth-flow"), "auth-flow-3")
        self.assertEqual(memory_system.successor_name("auth-flow-2"), "auth-flow-3")


class ReviewTests(MemoryStoreCase):
    def _stale(self):
        self.save()
        self.write_src("def login():\n    return 2\n")
        mem_evidence.propagate([self.src])

    def test_valid_verdict_re_pins_evidence_so_it_does_not_re_flag(self):
        self._stale()
        results = mem_review.review_stale(
            lambda msgs: '{"verdict": "valid", "reason": "unrelated change"}')
        self.assertEqual(results[0]["verdict"], "valid")
        self.assertTrue(results[0]["applied"])
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_ACTIVE)
        # Re-pinned to the CURRENT bytes: another propagate must be quiet.
        self.assertEqual(mem_evidence.propagate([self.src]), [])

    def test_update_verdict_writes_a_successor_and_supersedes(self):
        self._stale()
        reply = ('{"verdict": "update", "description": "login returns 2", '
                 '"body": "it now returns 2", "reason": "value changed"}')
        results = mem_review.review_stale(lambda msgs: reply)
        self.assertEqual(results[0]["verdict"], "update")
        self.assertTrue(results[0]["applied"])
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_SUPERSEDED)
        self.assertEqual(self.status("auth-flow-2"), memory_system.STATUS_ACTIVE)
        # The chain is walkable: the old entry points forward, and its text
        # survives so what the agent used to believe is still recoverable.
        old = memory_system.read_memory("auth-flow")
        self.assertEqual(old["meta"].get("superseded_by"), "auth-flow-2")
        self.assertIn("body", old["body"])

    def test_update_without_new_text_changes_nothing(self):
        self._stale()
        results = mem_review.review_stale(lambda msgs: '{"verdict": "update"}')
        self.assertFalse(results[0]["applied"])
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_STALE)

    def test_invalid_verdict_retires_without_deleting(self):
        self._stale()
        results = mem_review.review_stale(
            lambda msgs: '{"verdict": "invalid", "reason": "function removed"}')
        self.assertTrue(results[0]["applied"])
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_SUPERSEDED)
        self.assertTrue((self.store / "auth-flow.md").exists())

    def test_unparseable_reply_leaves_the_entry_stale(self):
        self._stale()
        results = mem_review.review_stale(lambda msgs: "I think it's probably fine")
        self.assertEqual(results[0]["verdict"], "error")
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_STALE)

    def test_raising_reviewer_leaves_the_entry_stale(self):
        self._stale()
        def boom(msgs):
            raise RuntimeError("backend down")
        results = mem_review.review_stale(boom)
        self.assertEqual(results[0]["verdict"], "error")
        self.assertEqual(self.status("auth-flow"), memory_system.STATUS_STALE)

    def test_fenced_json_is_accepted(self):
        self._stale()
        reply = '```json\n{"verdict": "valid", "reason": "ok"}\n```'
        results = mem_review.review_stale(lambda msgs: reply)
        self.assertEqual(results[0]["verdict"], "valid")

    def test_reverted_edits_are_healed_before_any_model_call(self):
        self._stale()
        self.write_src("def login():\n    return 1\n")
        calls = []

        def counting(msgs):
            calls.append(msgs)
            return '{"verdict": "valid", "reason": "x"}'

        results = mem_review.review_stale(counting)
        self.assertEqual(results, [])
        self.assertEqual(calls, [], "reconcile must run before the reviewer")

    def test_review_is_capped_per_pass(self):
        for i in range(4):
            path = str(self.proj / f"m{i}.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("a = 1\n")
            memory_system.write_memory(
                f"mem-{i}", "structure", f"fact {i}", "body",
                evidence=[mem_evidence.evidence_for(path)])
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("a = 2\n")
            mem_evidence.invalidate_cache()
            mem_evidence.propagate([path])
        self.assertEqual(len(memory_system.list_stale()), 4)
        results = mem_review.review_stale(
            lambda msgs: '{"verdict": "valid", "reason": "x"}', limit=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(memory_system.list_stale()), 2)


if __name__ == "__main__":
    unittest.main()
