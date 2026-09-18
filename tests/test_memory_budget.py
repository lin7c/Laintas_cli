"""What bounds the memory store, and what decides who leaves.

The store used to grow forever while the prompt kept showing the same handful
of entries: writes were unbounded, the read window was a fixed count, and
nothing recorded whether an entry had ever been used, so there was no evidence
on which anything could have been dropped. These tests pin the three parts of
the fix:

  * usage is recorded where a memory is actually USED, and recording it does
    not rewrite the memory file (its mtime is what recall orders by);
  * eviction prefers entries nobody comes back to, never touches the user's
    own words, and ARCHIVES rather than deletes — an archived entry is still
    readable by name;
  * the prompt block fills a character budget instead of a fixed count, so a
    large store can surface more than the same five summaries forever.
"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import memory_system
import mem_recall


class BudgetCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="membudget-")
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

    def _restore(self):
        memory_system.MEMORY_DIR, memory_system.MEMORY_INDEX = self._saved

    def write(self, name, body="body text", mem_type="project", importance=0.5):
        ok, msg = memory_system.write_memory(
            name, mem_type, f"summary of {name}", body, importance=importance)
        self.assertTrue(ok, msg)

    # ── metadata ────────────────────────────────────────────────────────
    def test_created_at_survives_rewrites(self):
        self.write("alpha")
        first = memory_system.read_memory("alpha")["meta"]["created_at"]
        time.sleep(0.01)
        self.write("alpha", body="rewritten")
        self.assertEqual(memory_system.read_memory("alpha")["meta"]["created_at"],
                         first, "a rewrite must not reset the entry's birthday")

    def test_touch_records_use_without_rewriting_the_file(self):
        self.write("beta")
        path = Path(memory_system.read_memory("beta")["path"])
        before = path.stat().st_mtime_ns
        time.sleep(0.01)
        memory_system.touch("beta")
        memory_system.touch("beta")
        entry = [e for e in memory_system.list_memories() if e["name"] == "beta"][0]
        self.assertEqual(entry["uses"], 2)
        self.assertGreater(entry["last_used"], 0)
        self.assertEqual(path.stat().st_mtime_ns, before,
                         "recording a read must not look like an edit")

    def test_listing_never_writes(self):
        self.write("gamma")
        memory_system.list_memories()          # first call migrates if needed
        before = {p.name: p.stat().st_mtime_ns for p in self.store.glob("*.md")}
        time.sleep(0.01)
        for _ in range(3):
            memory_system.list_memories()
        after = {p.name: p.stat().st_mtime_ns for p in self.store.glob("*.md")}
        self.assertEqual(before, after)

    # ── eviction ────────────────────────────────────────────────────────
    def test_budget_archives_the_unused_and_keeps_the_used(self):
        for i in range(8):
            self.write(f"cold-{i}", importance=0.5)
        self.write("hot", importance=0.5)
        for _ in range(5):
            memory_system.touch("hot")
        archived = memory_system.enforce_budget(limit=5)
        names = {e["name"] for e in archived}
        self.assertEqual(len(memory_system.list_memories()), 5)
        self.assertNotIn("hot", names, "a memory in active use must survive")
        self.assertTrue(names)

    def test_user_and_feedback_are_never_archived(self):
        self.write("pref", mem_type="feedback", importance=0.1)
        self.write("who", mem_type="user", importance=0.1)
        for i in range(6):
            self.write(f"fact-{i}", importance=0.9)
        memory_system.enforce_budget(limit=3)
        live = {e["name"] for e in memory_system.list_memories()}
        self.assertIn("pref", live)
        self.assertIn("who", live)

    def test_archived_entry_is_kept_and_still_readable(self):
        self.write("doomed")
        ok, path = memory_system.archive_memory("doomed", reason="test")
        self.assertTrue(ok)
        self.assertTrue(Path(path).exists(), "archiving must not delete")
        self.assertNotIn("doomed", {e["name"] for e in memory_system.list_memories()})
        self.assertIsNotNone(memory_system.read_memory("doomed"),
                             "an archived entry stays reachable by name")
        index = memory_system.MEMORY_INDEX.read_text(encoding="utf-8")
        self.assertNotIn("doomed", index)

    def test_eviction_score_prefers_used_over_merely_important(self):
        now = time.time()
        stale_important = {"importance": 0.9, "uses": 0,
                           "last_used": now - 200 * 86400, "created_at": now}
        used_ordinary = {"importance": 0.5, "uses": 4,
                         "last_used": now - 86400, "created_at": now}
        self.assertLess(memory_system.eviction_score(stale_important, now),
                        memory_system.eviction_score(used_ordinary, now))

    # ── prompt budget ───────────────────────────────────────────────────
    def test_prompt_block_fills_a_budget_instead_of_a_fixed_count(self):
        for i in range(20):
            self.write(f"router-{i}", body="the router handles retries")
        fixed = mem_recall.relevant_block("router retries", k=5, local_only=True)
        budgeted = mem_recall.relevant_block("router retries", k=5,
                                             local_only=True, budget_chars=1400)
        self.assertGreater(len(budgeted.splitlines()), len(fixed.splitlines()))
        self.assertLessEqual(len(budgeted), 1400 + 200)

    def test_budget_never_starves_the_floor(self):
        for i in range(4):
            self.write(f"long-{i}", body="x " * 200)
        block = mem_recall.relevant_block("long", k=3, local_only=True,
                                          budget_chars=10)
        self.assertGreaterEqual(len(block.splitlines()) - 1, 3,
                                "the floor is honoured before the budget")


if __name__ == "__main__":
    unittest.main()


class GlobalBudgetCase(BudgetCase):
    """The ceiling across every scope, not just the one you are standing in."""

    def test_other_scopes_count_towards_the_global_budget(self):
        self.write("here-1")
        self.write("here-2")
        # Two entries belonging to a project that is not the current one.
        for name in ("elsewhere-1", "elsewhere-2"):
            ok, _ = memory_system.write_memory(
                name, "project", f"summary of {name}", "body",
                scope="project", scope_id="some-other-project")
            self.assertTrue(ok)
        self.assertEqual(len(memory_system.list_memories()), 2,
                         "another project's memories stay invisible here")
        self.assertEqual(len(memory_system.list_memories(all_scopes=True)), 4)
        self.assertFalse(memory_system.enforce_budget(limit=3),
                         "the scope budget only sees its own scope")
        archived = memory_system.enforce_global_budget(limit=3)
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(memory_system.list_memories(all_scopes=True)), 3)

    def test_global_pass_can_be_capped(self):
        for i in range(10):
            ok, _ = memory_system.write_memory(
                f"far-{i}", "project", "summary", "body",
                scope="project", scope_id="far-away")
            self.assertTrue(ok)
        archived = memory_system.enforce_global_budget(limit=2, max_archived=3)
        self.assertEqual(len(archived), 3,
                         "one pass archives at most max_archived entries")


class UseSignalCase(BudgetCase):
    """What counts as "this memory was used"."""

    def test_only_entries_that_reach_the_prompt_are_counted(self):
        for i in range(12):
            self.write(f"router-{i}", body="the router handles retries")
        mem_recall._USE_RECORDED.clear()
        block = mem_recall.relevant_block("router retries", k=2, local_only=True,
                                          budget_chars=160)
        shown = len(block.splitlines()) - 1
        counted = sum(1 for e in memory_system.list_memories()
                      if int(e.get("uses", 0) or 0) > 0)
        self.assertEqual(counted, shown,
                         "ranking an entry is not using it; only what the "
                         "prompt showed may count")
        self.assertLess(shown, 12)

    def test_rebuilding_the_prompt_does_not_inflate_the_count(self):
        self.write("single", body="unique token zzz")
        mem_recall._USE_RECORDED.clear()
        for _ in range(5):
            mem_recall.relevant_block("zzz", k=1, local_only=True)
        entry = [e for e in memory_system.list_memories() if e["name"] == "single"][0]
        self.assertEqual(entry["uses"], 1,
                         "one run that shows an entry five times is one use")


class SelfReviewCase(BudgetCase):
    """Two defects found by reviewing the budget work, pinned so they stay fixed."""

    def test_parallel_writers_do_not_lose_uses(self):
        import threading
        self.write("shared")
        def hammer():
            for _ in range(50):
                memory_system.touch("shared")
        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(memory_system.usage_of("shared")["uses"], 200,
                         "read-modify-write on the usage sidecar must be "
                         "serialised; this CLI runs agents in parallel")

    def test_unmeetable_budget_archives_nothing(self):
        for i in range(6):
            self.write(f"pref-{i}", mem_type="feedback")
        for i in range(4):
            self.write(f"fact-{i}", importance=0.9)
        archived = memory_system.enforce_budget(limit=3)
        self.assertEqual(archived, [],
                         "with more protected entries than the whole budget, "
                         "archiving everything else cannot meet it — so it "
                         "must not empty the store trying")
        self.assertEqual(len(memory_system.list_memories()), 10)
