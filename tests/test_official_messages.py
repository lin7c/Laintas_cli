"""Official laintas.com announcements arriving in the L> message list.

The contract worth protecting is the read receipt: the same announcement
re-posted on every start must stay read, and an announcement edited on the site
must come back unread. Everything else here is about staying quiet — a signed
out session, an unreachable site or a malformed payload must produce no items
and no exception, because this runs behind the first prompt of every session.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import official_messages
import startup_mail


def _message(**overrides):
    base = {
        "id": 1,
        "slug": "maintenance",
        "level": "notice",
        "title": "计划维护",
        "body": "周日 02:00 UTC 起约三十分钟。",
        "actionUrl": "/settings#messages",
        "digest": "abc123",
        "read": False,
    }
    base.update(overrides)
    return base


class PostTests(unittest.TestCase):
    def setUp(self):
        startup_mail.clear()
        self.addCleanup(startup_mail.clear)

    def test_a_message_becomes_one_item_keyed_by_slug(self):
        self.assertEqual(official_messages.post([_message()]), 1)
        item = startup_mail.get("official:maintenance")
        self.assertIsNotNone(item)
        self.assertEqual(item.title, "计划维护")
        # Site levels map onto the four styles the mail list knows.
        self.assertEqual(item.level, "warn")
        # A site path is shown with the host, since a terminal cannot resolve it.
        self.assertEqual(item.action, "laintas.com/settings#messages")

    def test_reposting_the_same_message_updates_in_place(self):
        official_messages.post([_message()])
        official_messages.post([_message()])
        self.assertEqual(len(startup_mail.items()), 1)

    def test_the_receipt_follows_the_server_digest(self):
        # Read state only survives a restart when receipts are on, which is
        # what the CLI turns on at startup — so this is the real path.
        store = tempfile.TemporaryDirectory()
        self.addCleanup(store.cleanup)
        self.addCleanup(startup_mail.disable_persistence)
        startup_mail.enable_persistence(Path(store.name) / "read.json")

        key = "official:maintenance"
        official_messages.post([_message()])
        startup_mail.mark_read(key)
        self.assertTrue(startup_mail.get(key).read)

        # Next session, same announcement: still read, so the mark stays quiet.
        startup_mail.clear()
        official_messages.post([_message()])
        self.assertTrue(startup_mail.get(key).read)

        # Edited on the site: a new digest, so it is new information again.
        startup_mail.clear()
        official_messages.post([_message(digest="def456", body="改期到周一。")])
        self.assertFalse(startup_mail.get(key).read)

    def test_malformed_entries_are_skipped_rather_than_raising(self):
        posted = official_messages.post([
            {"slug": "", "title": "no slug"},
            {"slug": "no-title"},
            _message(slug="good"),
            "not a dict" if False else {},
        ])
        self.assertEqual(posted, 1)
        self.assertEqual([i.key for i in startup_mail.items()], ["official:good"])

    def test_absolute_links_are_left_alone(self):
        official_messages.post([_message(actionUrl="https://laintas.com/pricing")])
        self.assertEqual(startup_mail.get("official:maintenance").action,
                         "https://laintas.com/pricing")


class AuthTests(unittest.TestCase):
    def test_the_newest_cookie_wins_and_a_bearer_is_the_fallback(self):
        session = {"cookies": {"laintas-v2.session_token": "old",
                               "__Secure-laintas-v2.session_token": "new"}}
        self.assertEqual(official_messages.auth_args(session),
                         {"cookies": {"__Secure-laintas-v2.session_token": "new"}})

        bearer = {"headers": {"Authorization": "Bearer t"}}
        self.assertEqual(official_messages.auth_args(bearer), bearer)

    def test_a_signed_out_session_is_not_a_request(self):
        self.assertIsNone(official_messages.auth_args({}))
        self.assertIsNone(official_messages.auth_args({"cookies": {"unrelated": "x"}}))
        # fetch must not reach the network without a credential.
        with mock.patch("requests.get", side_effect=AssertionError("called")):
            self.assertIsNone(official_messages.fetch({}, "https://laintas.com"))


class SyncTests(unittest.TestCase):
    def setUp(self):
        startup_mail.clear()
        self.addCleanup(startup_mail.clear)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(
            official_messages, "CACHE_FILE", Path(self._dir.name) / "messages.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _response(status=200, payload=None):
        return mock.Mock(status_code=status, json=mock.Mock(return_value=payload or {}))

    def test_a_successful_fetch_is_cached_and_posted(self):
        session = {"cookies": {"laintas-v2.session_token": "t"}}
        with mock.patch("requests.get",
                        return_value=self._response(payload={"messages": [_message()]})):
            self.assertEqual(official_messages.sync(session, "https://laintas.com"), 1)
        cached = json.loads(official_messages.CACHE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(cached[0]["slug"], "maintenance")

    def test_an_unreachable_site_falls_back_to_the_last_fetch(self):
        official_messages.CACHE_FILE.write_text(
            json.dumps([_message()]), encoding="utf-8")
        session = {"cookies": {"laintas-v2.session_token": "t"}}
        with mock.patch("requests.get", side_effect=OSError("no route")):
            self.assertEqual(official_messages.sync(session, "https://laintas.com"), 1)
        self.assertIsNotNone(startup_mail.get("official:maintenance"))

    def test_an_error_response_does_not_overwrite_the_cache(self):
        official_messages.CACHE_FILE.write_text(
            json.dumps([_message()]), encoding="utf-8")
        session = {"cookies": {"laintas-v2.session_token": "t"}}
        with mock.patch("requests.get", return_value=self._response(status=503)):
            official_messages.sync(session, "https://laintas.com")
        cached = json.loads(official_messages.CACHE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(cached), 1)

    def test_an_empty_published_set_clears_the_cache(self):
        session = {"cookies": {"laintas-v2.session_token": "t"}}
        with mock.patch("requests.get",
                        return_value=self._response(payload={"messages": []})):
            self.assertEqual(official_messages.sync(session, "https://laintas.com"), 0)
        self.assertEqual(
            json.loads(official_messages.CACHE_FILE.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
