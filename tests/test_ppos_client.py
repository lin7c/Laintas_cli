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


class PPOSAuthAndIdempotencyTests(unittest.TestCase):
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
            "ppos.platform_review.decide",
        }
        registry = tools.get_registry()
        self.assertTrue(expected.issubset({tool.name for tool in registry.list()}))
        exported, name_map = registry.to_openai_tools(unified=True)
        wire_names = {item["function"]["name"] for item in exported}
        self.assertIn("ppos_publish", wire_names)
        self.assertEqual(name_map["ppos_publish"], "ppos.publish_markdown")

    def test_ppos_meta_command_routes_to_handler(self):
        import laintas_cli

        registry = mock.Mock()
        with mock.patch.object(laintas_cli, "_cmd_ppos") as handler:
            result = laintas_cli._handle_meta_command_impl(
                "/ppos status", registry, {"userId": "u"})
        self.assertFalse(result)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0], ["/ppos", "status"])


if __name__ == "__main__":
    unittest.main()
