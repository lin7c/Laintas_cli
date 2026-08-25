import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import extension_manager


class ExtensionMarketplaceTests(unittest.TestCase):
    @staticmethod
    def _response(payload):
        encoded = json.dumps(payload).encode("utf-8")

        class Response:
            headers = {"Content-Length": str(len(encoded))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _size):
                yield encoded

        return Response()

    def test_available_combines_and_labels_official_and_community(self):
        official = {"extensions": [{
            "id": "laintas/blindpick", "name": "Blindpick",
            "version": "3.1.0", "description": "Blind model comparison",
        }]}
        community = {"extensions": [{
            "id": "@alice/sample", "author": "alice", "slug": "sample",
            "version": "1.2.0", "manifest": {
                "displayName": "Sample", "summary": "A community extension",
            },
        }]}
        responses = [self._response(official), self._response(community)]
        with mock.patch("requests.get", side_effect=responses) as request:
            entries = extension_manager.ExtensionManager().list_available()
        self.assertEqual([entry["source"] for entry in entries],
                         ["official", "community"])
        self.assertEqual(entries[1]["id"], "@alice/sample")
        self.assertEqual(request.call_count, 2)

    def test_available_source_filter_avoids_unneeded_registry(self):
        community = {"extensions": [{
            "id": "@alice/sample", "slug": "sample", "version": "1.0.0",
            "manifest": {"description": "Searchable ledger helper"},
        }]}
        with mock.patch("requests.get", return_value=self._response(community)) as request:
            entries = extension_manager.ExtensionManager().list_available(
                source="community", query="LEDGER")
        self.assertEqual([entry["id"] for entry in entries], ["@alice/sample"])
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[0].startswith(
            "https://cli.laintas.com/api/extensions/community"))
        self.assertIn("limit=200", request.call_args.args[0])

    def test_available_shows_only_latest_community_publication(self):
        community = {"extensions": [
            {"id": "@alice/sample", "slug": "sample", "version": "1.0.0",
             "publishedAt": 10, "manifest": {}},
            {"id": "@alice/sample", "slug": "sample", "version": "2.0.0",
             "publishedAt": 20, "manifest": {}},
        ]}
        with mock.patch("requests.get", return_value=self._response(community)):
            entries = extension_manager.ExtensionManager().list_available(
                source="community")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], "2.0.0")

    def test_registry_download_is_bounded(self):
        response = self._response({"extensions": []})
        response.headers = {"Content-Length": str(
            extension_manager.MAX_REGISTRY_BYTES + 1)}
        with mock.patch("requests.get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "1 MB limit"):
                extension_manager._fetch_registry_json("https://example.invalid")

    def test_publication_archive_removes_local_install_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sample"
            root.mkdir()
            (root / "extension.json").write_text(json.dumps({
                "schemaVersion": 2, "name": "sample", "version": "1.0.0",
                "entrypoint": "main.py",
                "install": {"trustedBy": "user-confirm"},
            }), encoding="utf-8")
            (root / "main.py").write_text("def setup(ctx):\n    pass\n", encoding="utf-8")
            output = Path(tmp) / "sample.lext"
            extension_manager.create_publication_archive(root, output)
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("extension.json"))
            self.assertNotIn("install", manifest)

    def test_publication_archive_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sample"
            root.mkdir()
            (root / "extension.json").write_text(json.dumps({
                "schemaVersion": 2, "name": "sample", "version": "1.0.0",
                "entrypoint": "main.py",
            }), encoding="utf-8")
            (root / "main.py").write_text("def setup(ctx):\n    pass\n", encoding="utf-8")
            first, second = Path(tmp) / "first.lext", Path(tmp) / "second.lext"
            extension_manager.create_publication_archive(root, first)
            extension_manager.create_publication_archive(root, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_marketplace_identifiers_are_separated_by_namespace(self):
        self.assertEqual(
            extension_manager.ExtensionManager.detect_source("@alice/sample-extension"),
            "marketplace")

    def test_publish_uploads_marker_last_and_commits_storage_folder(self):
        class Storage:
            def __init__(self):
                self.uploads = []
                self.committed = ""

            def push_file(self, local, remote):
                self.uploads.append((Path(local).read_bytes(), remote))

            def publish_extension(self, folder):
                self.committed = folder
                return {"id": "@alice/sample", "version": "1.0.0"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extension = root / "sample"
            extension.mkdir()
            (extension / "extension.json").write_text(json.dumps({
                "schemaVersion": 2, "name": "sample", "version": "1.0.0",
                "entrypoint": "main.py",
            }), encoding="utf-8")
            (extension / "main.py").write_text(
                "def setup(ctx):\n    pass\n", encoding="utf-8")
            storage = Storage()
            manager = extension_manager.ExtensionManager()
            with (
                mock.patch.object(extension_manager.paths, "extensions_dir", return_value=root),
                mock.patch.object(extension_manager.paths, "global_extensions_dir", return_value=root / "global"),
            ):
                item = manager.publish("sample", storage)
            self.assertEqual(item["id"], "@alice/sample")
            self.assertEqual(
                [remote for _data, remote in storage.uploads],
                ["Extensions/sample/extension.lext", "Extensions/sample/publish.json"])
            publication = json.loads(storage.uploads[-1][0])
            self.assertEqual(publication["sha256"],
                             __import__("hashlib").sha256(storage.uploads[0][0]).hexdigest())
            self.assertEqual(storage.committed, "Extensions/sample")
        self.assertEqual(
            extension_manager.ExtensionManager.detect_source("laintas/blindpick"),
            "marketplace")

    def test_safe_extract_rejects_excessive_unpacked_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "large.lext"
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("main.py", b"x" * (extension_manager.MAX_UNPACKED_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, "unpacked size"):
                extension_manager._safe_extract_zip(
                    stream.read_bytes(), Path(tmp) / "out")

    def test_failed_hot_load_restores_previous_version(self):
        class Runtime:
            def unload(self, _name):
                return True, "unloaded"

            def load(self, _name):
                current = json.loads((project / "sample" / "extension.json").read_text())
                return (False, "setup failed") if current["version"] == "2.0.0" \
                    else (True, "loaded")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "installed"
            project.mkdir()
            existing = project / "sample"
            existing.mkdir()
            staging = root / "staging"
            staging.mkdir()
            for directory, version in ((existing, "1.0.0"), (staging, "2.0.0")):
                (directory / "extension.json").write_text(json.dumps({
                    "schemaVersion": 2, "name": "sample", "version": version,
                    "entrypoint": "main.py",
                }), encoding="utf-8")
                (directory / "main.py").write_text(
                    "def setup(ctx):\n    pass\n", encoding="utf-8")
            manager = extension_manager.ExtensionManager(runtime=Runtime())
            with (
                mock.patch.object(extension_manager.paths, "extensions_dir", return_value=project),
                mock.patch.object(extension_manager.paths, "global_extensions_dir", return_value=root / "global"),
                mock.patch.object(extension_manager.trust_store, "extension_status", return_value={"trusted": True}),
                mock.patch.object(extension_manager.trust_store, "revoke_extension"),
                mock.patch.object(manager, "trust", return_value=(True, "trusted")),
            ):
                result = manager._install_staged(
                    staging, global_install=False, force=True,
                    trust_mode="community-ai-confirm", source_label="test")
            self.assertFalse(result.ok)
            restored = json.loads((existing / "extension.json").read_text())
            self.assertEqual(restored["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
