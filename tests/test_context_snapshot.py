import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import context_snapshot as snapshots


class ContextSnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "contexts"
        self.store = snapshots.ContextSnapshotStore(
            self.root, max_conversations=20, max_bytes=10 * 1024 * 1024,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def append(self, conversation_id, marker="one", **kwargs):
        return self.store.append_call(
            "session", conversation_id,
            system_prompt="system prompt",
            messages=[{"role": "user", "content": marker}],
            tool_schemas=[{"name": "read", "input": {"type": "object"}}],
            metadata={"model": "test"},
            system_sections=[{"name": "policy", "text": "local"}],
            gateway_context_receipt={"id": marker},
            **kwargs,
        )

    def test_large_request_values_are_deduplicated_by_content_hash(self):
        self.append("turn-a")
        self.append("turn-b")
        blobs = list((self.root / "blobs").glob("*.json"))
        self.assertEqual(len(blobs), 3)
        manifests = list((self.root / "sessions").glob("*/*.json"))
        first = json.loads(manifests[0].read_text(encoding="utf-8"))
        second = json.loads(manifests[1].read_text(encoding="utf-8"))
        self.assertEqual(first["calls"][0]["system_prompt_blob"],
                         second["calls"][0]["system_prompt_blob"])

    def test_conversations_are_indexed_newest_first(self):
        self.append("old", marker="old")
        self.append("new", marker="new")
        self.assertEqual(
            [item["conversation_id"] for item in self.store.list_conversations("session")],
            ["new", "old"],
        )
        self.assertEqual(self.store.load_conversation("session", 1)["conversation_id"], "new")
        self.assertEqual(self.store.load_conversation("session", 2)["conversation_id"], "old")

    def test_a_conversation_holds_multiple_model_calls(self):
        first = self.append("turn", marker="first", call_id="call-1")
        second = self.append("turn", marker="second", call_id="call-2")
        conversation = self.store.load_conversation("session")
        self.assertEqual([call["call_id"] for call in conversation["calls"]],
                         ["call-1", "call-2"])
        self.assertEqual(first["messages"][0]["content"], "first")
        self.assertEqual(second["gateway_context_receipt"], {"id": "second"})
        self.assertEqual(self.store.load_call("session")["call_id"], "call-2")
        self.assertEqual(self.store.load_call("session", call_index=2)["call_id"], "call-1")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not available")
    def test_directories_and_files_are_private(self):
        self.append("turn")
        for directory, _, filenames in os.walk(self.root):
            path = Path(directory)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)
            for filename in filenames:
                file_path = path / filename
                self.assertEqual(stat.S_IMODE(file_path.stat().st_mode), 0o600, file_path)

    def test_missing_and_corrupt_data_fail_safely(self):
        self.assertEqual(self.store.list_conversations("missing"), [])
        with self.assertRaises(snapshots.ContextSnapshotNotFound):
            self.store.load_conversation("missing")

        self.append("good")
        session_dir = next((self.root / "sessions").iterdir())
        (session_dir / ("f" * 64 + ".json")).write_text("not json", encoding="utf-8")
        self.assertEqual(len(self.store.list_conversations("session")), 1)

        raw = self.store.load_conversation("session", expand=False)
        blob = self.root / "blobs" / (raw["calls"][0]["messages_blob"] + ".json")
        blob.write_text("{}", encoding="utf-8")
        with self.assertRaises(snapshots.ContextSnapshotCorrupt):
            self.store.load_conversation("session")

    def test_symlinks_are_not_followed_or_replaced(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        self.root.mkdir()
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (self.root / "blobs").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(snapshots.ContextSnapshotSecurityError):
            self.append("turn")
        self.assertEqual(list(outside.iterdir()), [])

    def test_retention_bounds_conversations_and_removes_orphan_blobs(self):
        store = snapshots.ContextSnapshotStore(
            self.root, max_conversations=2, max_bytes=10 * 1024 * 1024,
        )
        for number in range(3):
            store.append_call(
                "session", f"turn-{number}", system_prompt=f"system-{number}",
                messages=[{"content": str(number)}], tool_schemas=[],
            )
        self.assertEqual(
            [item["conversation_id"] for item in store.list_conversations("session")],
            ["turn-2", "turn-1"],
        )
        self.assertEqual(len(list((self.root / "blobs").glob("*.json"))), 5)

        tiny = snapshots.ContextSnapshotStore(
            self.root, max_conversations=20, max_bytes=1,
        )
        tiny.append_call("session", "too-large", "prompt", [], [])
        self.assertEqual(tiny.list_conversations("session"), [])
        self.assertLessEqual(tiny._store_bytes(), 1)

    def test_append_and_load_never_mutate_or_alias_caller_values(self):
        messages = [{"role": "user", "content": ["before"]}]
        tools = [{"name": "tool", "schema": {"required": ["x"]}}]
        metadata = {"nested": [1]}
        sections = [{"text": "section"}]
        receipt = {"tokens": [7]}
        originals = json.loads(json.dumps([messages, tools, metadata, sections, receipt]))

        returned = self.store.append_call(
            "session", "turn", "prompt", messages, tools, metadata, sections, receipt,
        )
        self.assertEqual([messages, tools, metadata, sections, receipt], originals)
        returned["messages"][0]["content"].append("changed")
        returned["metadata"]["nested"].append(2)
        loaded = self.store.load_conversation("session")
        self.assertEqual(loaded["calls"][0]["messages"], messages)
        self.assertEqual(loaded["calls"][0]["metadata"], metadata)

    def test_explicit_null_optional_material_is_preserved_exactly(self):
        self.store.append_call(
            "session", "turn", "prompt", [], [], None, None, None,
        )
        raw = self.store.load_conversation("session", expand=False)
        expanded = self.store.expand_conversation(raw)
        call = expanded["calls"][0]
        self.assertIsNone(call["metadata"])
        self.assertIsNone(call["system_sections"])
        self.assertIsNone(call["gateway_context_receipt"])

    def test_unexpanded_append_avoids_blob_reads_and_remains_expandable(self):
        with mock.patch.object(self.store, "_load_blob", side_effect=AssertionError("readback")):
            raw = self.append("turn", expand=False)
        self.assertNotIn("messages", raw)
        self.assertEqual(self.store.expand_call(raw)["messages"],
                         [{"role": "user", "content": "one"}])

    def test_invalid_late_field_does_not_create_storage(self):
        with self.assertRaises(snapshots.ContextSnapshotSerializationError):
            self.store.append_call("session", "turn", "prompt", [], [],
                                   gateway_context_receipt={"bad": object()}, expand=False)
        self.assertFalse(self.root.exists())

    def test_retention_cache_is_shared_by_short_lived_stores(self):
        self.append("turn")
        other = snapshots.ContextSnapshotStore(self.root)
        with mock.patch.object(other, "_read_json", side_effect=AssertionError("reparse")):
            other._apply_retention()

    def test_retention_reloads_same_length_rewrite_even_with_restored_mtime(self):
        self.append("turn")
        path = self.store._manifest_path("session", "turn")
        before = path.stat()
        manifest = self.store._read_json(path)
        old_digest = manifest["calls"][0]["messages_blob"]
        replacement = self.store._put_blob([{"role": "user", "content": "new"}])
        text = path.read_text()
        path.write_text(text.replace(old_digest, replacement))
        self.assertEqual(path.stat().st_size, before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.store._apply_retention()
        self.assertEqual(self.store.load_call("session")["messages"][0]["content"], "new")
        self.assertFalse((self.root / "blobs" / f"{old_digest}.json").exists())

    def test_failed_manifest_deletion_preserves_its_blobs(self):
        self.append("turn")
        path = self.store._manifest_path("session", "turn")
        self.store.max_conversations = 0
        unlink = self.store._safe_unlink
        with mock.patch.object(self.store, "_safe_unlink",
                               side_effect=lambda p: False if p == path else unlink(p)):
            self.store._apply_retention()
        self.assertEqual(self.store.load_call("session")["messages"][0]["content"], "one")

    def test_corrupt_cached_manifest_prevents_destructive_blob_cleanup(self):
        self.append("old")
        raw = self.store.load_conversation("session", expand=False)
        digest = raw["calls"][0]["messages_blob"]
        self.store._manifest_path("session", "old").write_text("not JSON")
        self.append("new", marker="different")
        assert (self.root / "blobs" / f"{digest}.json").is_file()

    def test_byte_eviction_walks_store_once_and_preserves_shared_blobs(self):
        for number in range(5):
            self.append(f"turn-{number}", marker=str(number))
        self.store.max_bytes = 1
        with mock.patch.object(self.store, "_store_bytes", wraps=self.store._store_bytes) as walk:
            self.store._apply_retention()
        self.assertEqual(walk.call_count, 1)
        self.assertEqual(self.store.list_conversations("session"), [])
        self.assertLessEqual(self.store._store_bytes(), 1)

    def test_concurrent_stores_keep_every_call_in_same_conversation(self):
        def append(number):
            store = snapshots.ContextSnapshotStore(self.root)
            return store.append_call("session", "turn", "prompt",
                                     [{"content": str(number)}], [],
                                     call_id=f"call-{number}", expand=False)

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(append, range(16)))
        calls = self.store.load_conversation("session")["calls"]
        self.assertEqual({call["call_id"] for call in calls},
                         {f"call-{number}" for number in range(16)})

    def test_symlink_write_lock_is_rejected_without_touching_target(self):
        self.root.mkdir()
        outside = Path(self._tmp.name) / "outside"
        outside.write_text("preserve")
        (self.root / ".write.lock").symlink_to(outside)
        with self.assertRaises(snapshots.ContextSnapshotSecurityError):
            self.append("turn")
        self.assertEqual(outside.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
