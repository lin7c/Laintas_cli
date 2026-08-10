import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend_profiles
import ppos_client


class _Response:
    def __init__(self, data=None, status=200):
        self._data = data if data is not None else {}
        self.status_code = status
        self.content = json.dumps(self._data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._data


class _Requests:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/token"):
            return _Response({"token": "signed-token", "expires_at": int(__import__('time').time()) + 300})
        return _Response({"ok": True})


class _PublishRequests(_Requests):
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/token"):
            return _Response({"token": "signed-token", "expires_at": int(__import__('time').time()) + 300})
        if url.endswith("/publish/preflight"):
            return _Response({"valid": True, "review_fee_cents": 0})
        if url.endswith("/drafts/preflight"):
            return _Response({"valid": True, "review_fee_cents": 0})
        if url.endswith("/storage/presign"):
            return _Response({"upload_url": "https://r2.example/presigned",
                              "public_url": "https://cdn.example/pic.png", "key": "pic"})
        return _Response({"id": "work-1"})

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return _Response({})


class PPOSParsingTests(unittest.TestCase):
    def test_utf16_limit_counts_as_javascript(self):
        self.assertEqual(ppos_client.utf16_units("a😀"), 3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "too-long.md"
            path.write_text("😀" * 50_001, encoding="utf-8")
            with self.assertRaisesRegex(ppos_client.PPOSClientError, "UTF-16"):
                ppos_client.read_markdown(path)

    def test_local_image_parsing_and_traversal_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc_dir = root / "docs"
            doc_dir.mkdir()
            (doc_dir / "image.png").write_bytes(b"png")
            doc = doc_dir / "work.md"
            doc.write_text("![safe](image.png)", encoding="utf-8")
            _, _, assets = ppos_client.read_markdown(doc)
            self.assertEqual([(a.reference, a.size) for a in assets], [("image.png", 3)])

            (root / "secret.png").write_bytes(b"secret")
            doc.write_text("![escape](../secret.png)", encoding="utf-8")
            with self.assertRaisesRegex(ppos_client.PPOSClientError, "escapes"):
                ppos_client.read_markdown(doc)

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc_dir = root / "docs"
            doc_dir.mkdir()
            outside = root / "outside.png"
            outside.write_bytes(b"secret")
            try:
                (doc_dir / "link.png").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            doc = doc_dir / "work.md"
            doc.write_text("![escape](link.png)", encoding="utf-8")
            with self.assertRaisesRegex(ppos_client.PPOSClientError, "escapes"):
                ppos_client.read_markdown(doc)


class PPOSMediaTests(unittest.TestCase):
    def test_ppos_app_html_images_are_uploaded_and_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "option-a.png").write_bytes(b"a")
            doc = root / "post.md"
            doc.write_text(
                '```ppos-app\n<!-- ppos-widget id="choice" height="480" -->\n'
                '<img class="choice" src="option-a.png" alt="Option A">\n```',
                encoding="utf-8",
            )
            _, markdown, assets = ppos_client.read_markdown(doc)
        self.assertEqual([asset.reference for asset in assets], ["option-a.png"])
        rewritten = ppos_client.rewrite_media_references(
            markdown, {"option-a.png": "https://cdn.example/option-a.png"})
        self.assertIn('src="https://cdn.example/option-a.png"', rewritten)

    def test_video_and_image_share_one_upload_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clip.mp4").write_bytes(b"video-bytes")
            (root / "shot.png").write_bytes(b"png")
            doc = root / "post.md"
            doc.write_text("![video](clip.mp4)\n![shot](shot.png)", encoding="utf-8")
            _, _, assets = ppos_client.read_markdown(doc)
        by_name = {asset.path.name: asset for asset in assets}
        self.assertTrue(by_name["clip.mp4"].is_video)
        self.assertEqual(by_name["clip.mp4"].media_type, "video/mp4")
        self.assertFalse(by_name["shot.png"].is_video)

    def test_unsupported_media_type_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sheet.pdf").write_bytes(b"%PDF")
            doc = root / "post.md"
            doc.write_text("![doc](sheet.pdf)", encoding="utf-8")
            with self.assertRaisesRegex(ppos_client.PPOSClientError, "does not accept"):
                ppos_client.read_markdown(doc)

    def test_one_file_written_two_ways_is_uploaded_once_and_rewritten_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.png").write_bytes(b"one")
            doc = root / "post.md"
            doc.write_text("![x](a.png) and ![y](./a.png)", encoding="utf-8")
            _, markdown, assets = ppos_client.read_markdown(doc)
        self.assertEqual(len(assets), 1)
        self.assertEqual(set(assets[0].references), {"a.png", "./a.png"})
        rewritten = ppos_client.rewrite_media_references(
            markdown, {ref: "https://cdn.example/a.png" for ref in assets[0].references})
        self.assertEqual(rewritten,
                         "![x](https://cdn.example/a.png) and ![y](https://cdn.example/a.png)")

    def test_rewrite_only_touches_link_targets(self):
        # "a.png" is both a prose word and a prefix of another file's name.
        markdown = "a.png is shown here: ![one](a.png) ![two](a.png.backup.png)"
        rewritten = ppos_client.rewrite_media_references(
            markdown, {"a.png": "https://cdn.example/1.png",
                       "a.png.backup.png": "https://cdn.example/2.png"})
        self.assertEqual(
            rewritten,
            "a.png is shown here: ![one](https://cdn.example/1.png) "
            "![two](https://cdn.example/2.png)")


class PPOSAuthAndIdempotencyTests(unittest.TestCase):
    def test_localized_server_errors_are_rendered_in_english(self):
        fake = _Requests()
        fake.request = mock.Mock(return_value=_Response(
            {"detail": "\u5b58\u50a8\u7a7a\u95f4\u5df2\u6ee1"}, status=413))
        client = ppos_client.PPOSClient({"userId": "u"}, requests_module=fake)
        with self.assertRaisesRegex(ppos_client.PPOSClientError, "storage allowance") as raised:
            client._request("GET", "/api/ppos/agent/storage")
        self.assertFalse(any("\u3400" <= char <= "\u9fff" for char in str(raised.exception)))

    def test_custom_active_backend_never_receives_laintas_credentials(self):
        fake = _Requests()
        session = {"userId": "u-auth-test", "headers": {"Authorization": "Bearer secret"},
                   "cookies": {"session": "secret-cookie"}}
        with mock.patch.dict(os.environ, {"LAINTAS_BACKEND": "https://evil.example"}):
            client = ppos_client.PPOSClient(session, requests_module=fake)
            client.read("account")
        _, url, kwargs = fake.calls[-1]
        self.assertTrue(url.startswith("https://laintas.com/api/ppos/agent/"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["cookies"], {"session": "secret-cookie"})
        self.assertNotIn("evil.example", url)

    def test_idempotency_is_stable_and_content_sensitive(self):
        one = ppos_client.stable_idempotency_key("user", "comment", {"b": 2, "a": 1})
        two = ppos_client.stable_idempotency_key("user", "comment", {"a": 1, "b": 2})
        three = ppos_client.stable_idempotency_key("user", "comment", {"a": 2, "b": 2})
        self.assertEqual(one, two)
        self.assertNotEqual(one, three)

    def test_publish_uses_exact_size_presigned_upload_without_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pic.png").write_bytes(b"asset-bytes")
            doc = root / "post.md"
            doc.write_text("hello ![pic](pic.png)", encoding="utf-8")
            fake = _PublishRequests()
            client = ppos_client.PPOSClient(
                {"userId": "publish-user", "headers": {"Authorization": "Bearer secret"}},
                requests_module=fake)
            result = client.publish_markdown(
                doc, community="demo", self_score=72, autonomous=False)

        self.assertEqual(result["id"], "work-1")
        upload = next(call for call in fake.calls if call[0] == "PUT")
        self.assertEqual(upload[2]["data"], b"asset-bytes")
        self.assertEqual(upload[2]["headers"], {"Content-Type": "image/png", "Content-Length": "11"})
        api_writes = [call for call in fake.calls if call[0] == "POST" and not call[1].endswith('/token')]
        self.assertEqual(len(api_writes), 3)
        for _, _, kwargs in api_writes:
            self.assertTrue(kwargs["headers"]["Idempotency-Key"])
            self.assertEqual(kwargs["headers"]["X-PPOS-Agent-Token"], "signed-token")

    def test_draft_save_uses_private_routes_and_publish_can_submit_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "draft.md"
            doc.write_text("# Private draft\n\nNot reviewed yet.", encoding="utf-8")
            fake = _PublishRequests()
            client = ppos_client.PPOSClient(
                {"userId": "draft-user"}, requests_module=fake)
            client.save_draft(doc, autonomous=False)
            client.publish_markdown(
                doc, community="community-1", self_score=64,
                draft_id="draft-1", autonomous=False)

        posts = [(url, kwargs.get("json") or {}) for method, url, kwargs in fake.calls
                 if method == "POST" and not url.endswith("/token")]
        self.assertTrue(any(url.endswith("/drafts/preflight") for url, _ in posts))
        self.assertTrue(any(url.endswith("/drafts/save") for url, _ in posts))
        publish_quote = next(body for url, body in posts if url.endswith("/publish/preflight"))
        publish_commit = next(body for url, body in posts if url.endswith("/publish/commit"))
        self.assertEqual(publish_quote["draft_id"], "draft-1")
        self.assertEqual(publish_commit["draft_id"], "draft-1")


class PPOSReadTests(unittest.TestCase):
    def test_pagination_reaches_the_server_as_an_offset(self):
        fake = _Requests()
        client = ppos_client.PPOSClient({"userId": "u-page"}, requests_module=fake)
        client.read("works", page=3, page_size=10)
        _, url, kwargs = fake.calls[-1]
        self.assertTrue(url.endswith("/works"))
        self.assertEqual(kwargs["params"]["limit"], 10)
        self.assertEqual(kwargs["params"]["offset"], 20)

    def test_different_pages_are_not_served_from_one_cache_entry(self):
        fake = _Requests()
        client = ppos_client.PPOSClient({"userId": "u-cache"}, requests_module=fake)
        client._invalidate_reads()
        client.read("works", page=1)
        client.read("works", page=2)
        offsets = [call[2]["params"]["offset"] for call in fake.calls
                   if call[0] == "GET" and call[1].endswith("/works")]
        self.assertEqual(offsets, [0, 20])


class PPOSManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(
            ppos_client.paths, "PPOS_POLICY_FILE", Path(self.temp.name) / "policy.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_delete_and_cleanup_need_the_manage_opt_in(self):
        fake = _Requests()
        client = ppos_client.PPOSClient({"userId": "u-manage"}, requests_module=fake)
        with self.assertRaisesRegex(ppos_client.PPOSPolicyError, "disabled"):
            client.delete_work("work-1", autonomous=True)
        with self.assertRaisesRegex(ppos_client.PPOSPolicyError, "disabled"):
            client.cleanup_storage(dry_run=False, autonomous=True)
        # A dry run inspects without deleting, so it is not gated.
        client.cleanup_storage(dry_run=True, autonomous=True)

        ppos_client.update_policy(enabled=True, scope="manage", scope_enabled=True)
        client.delete_work("work-1", autonomous=True)
        _, url, kwargs = fake.calls[-1]
        self.assertTrue(url.endswith("/works/work-1/delete"))
        self.assertTrue(kwargs["headers"]["Idempotency-Key"])

    def test_update_uploads_replacement_media_and_patches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "new.png").write_bytes(b"new-bytes")
            doc = root / "edit.md"
            doc.write_text("![new](new.png)", encoding="utf-8")
            fake = _PublishRequests()
            client = ppos_client.PPOSClient({"userId": "u-edit"}, requests_module=fake)
            client.update_work("work-9", markdown_path=doc, autonomous=False)
        patch_call = next(call for call in fake.calls if call[0] == "PATCH")
        self.assertTrue(patch_call[1].endswith("/works/work-9"))
        self.assertEqual(patch_call[2]["json"]["description"],
                         "![new](https://cdn.example/pic.png)")
        self.assertTrue(any(call[0] == "PUT" for call in fake.calls))


class PPOSPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.policy_path = Path(self.temp.name) / "ppos_policy.json"
        self.patch = mock.patch.object(ppos_client.paths, "PPOS_POLICY_FILE", self.policy_path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_writes_default_to_disabled_then_enforce_scope_allowlist_and_cap(self):
        with self.assertRaisesRegex(ppos_client.PPOSPolicyError, "disabled"):
            ppos_client.enforce_policy("publish", community="safe")

        ppos_client.update_policy(enabled=True)
        with self.assertRaisesRegex(ppos_client.PPOSPolicyError, "not enabled"):
            ppos_client.enforce_policy("publish", community="safe")

        ppos_client.update_policy(scope="publish", scope_enabled=True,
                                  community_allowlist=["safe"], daily_cap=("publish", 1))
        with self.assertRaisesRegex(ppos_client.PPOSPolicyError, "allowlist"):
            ppos_client.enforce_policy("publish", community="other")
        ppos_client.enforce_policy("publish", community="safe")
        ppos_client.record_policy_use("publish")
        with self.assertRaisesRegex(ppos_client.PPOSPolicyError, "cap reached"):
            ppos_client.enforce_policy("publish", community="safe")

    def test_review_confidence_and_platform_default_cap(self):
        ppos_client.update_policy(enabled=True, scope="community_review", scope_enabled=True)
        with self.assertRaisesRegex(ppos_client.PPOSPolicyError, "confidence"):
            ppos_client.enforce_policy("community_review", confidence=0.79)
        ppos_client.enforce_policy("community_review", confidence=0.8)


class PPOSCommandRoutingTests(unittest.TestCase):
    def test_ppos_tools_are_registered_and_provider_safe(self):
        import tools

        expected = {
            "ppos.account.get", "ppos.storage.get", "ppos.communities.list",
            "ppos.works.list", "ppos.status.get", "ppos.publish_markdown",
            "ppos.comment", "ppos.community_review.queue",
            "ppos.community_review.decide", "ppos.platform_review.queue",
            "ppos.platform_review.decide", "ppos.work.get", "ppos.work.update",
            "ppos.work.delete", "ppos.storage.cleanup",
            "ppos.draft.save",
        }
        registry = tools.get_registry()
        self.assertTrue(expected.issubset({tool.name for tool in registry.list()}))
        exported, name_map = registry.to_openai_tools(unified=True)
        wire_names = {item["function"]["name"] for item in exported}
        self.assertIn("ppos_publish", wire_names)
        self.assertIn("ppos_draft_save", wire_names)
        self.assertEqual(name_map["ppos_publish"], "ppos.publish_markdown")
        self.assertEqual(name_map["ppos_draft_save"], "ppos.draft.save")

    def test_ppos_meta_command_routes_to_handler(self):
        import laintas_cli

        registry = mock.Mock()
        with mock.patch.object(laintas_cli, "_cmd_ppos") as handler:
            result = laintas_cli._handle_meta_command_impl(
                "/ppos status", registry, {"userId": "u"})
        self.assertFalse(result)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0], ["/ppos", "status"])

    def test_ppos_second_level_choices_have_specific_descriptions(self):
        import laintas_cli

        spec = next(item for item in laintas_cli.COMMAND_SPECS if item.name == "/ppos")
        descriptions = {item.value: item.description for item in spec.contextual_completions}
        self.assertIn("without review", descriptions["draft"])
        self.assertIn("review workflow", descriptions["publish"])
        self.assertIn("Admin-only", descriptions["platform-review"])
        self.assertEqual(len(descriptions), len(set(descriptions.values())))


if __name__ == "__main__":
    unittest.main()
