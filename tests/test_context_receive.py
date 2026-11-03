"""Opening and sending inbox context hands (context_receive.py)."""
import json
import unittest
from unittest import mock

import paths
if not paths._ACCOUNT_CONFIGURED:
    paths.configure_account("")

import agent_loop
import context_receive as cr


class WireHistoryTest(unittest.TestCase):
    def test_carried_thinking_is_folded_not_replayed(self):
        history = cr.wire_history({"origin": "Laintas AI", "items": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "reasoning": "my plan"}]})
        msg = history[-1]
        self.assertNotIn("reasoning", msg)
        projected = agent_loop._project_reasoning(history, "some-model")[-1]
        self.assertNotIn("reasoning_content", projected)
        self.assertIn('<prior_reasoning source="another model">', projected["content"])
        self.assertIn("my plan", projected["content"])

    def test_over_budget_record_keeps_a_fitted_tail_without_orphans(self):
        big = "x" * 300_000
        items = [{"role": "user", "content": "q"}]
        for i in range(6):
            items.append({"role": "assistant", "content": "",
                          "tool_calls": [{"id": f"c{i}", "name": "read", "input": {}}]})
            items.append({"role": "tool", "tool_call_id": f"c{i}",
                          "content": big if i >= 3 else "r"})
        history = cr.wire_history({"origin": "X", "items": items})
        self.assertTrue(history)
        self.assertNotEqual(history[0]["role"], "tool")
        self.assertLessEqual(len(json.dumps(history, separators=(",", ":"))),
                             cr._FIT_CHARS + 1000)


class SendContextTest(unittest.TestCase):
    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def json(self):
            return self._body

    def _send(self, status, body, history=None):
        posts = []
        with mock.patch.object(cr.official_messages, "auth_args",
                               return_value={"headers": {}}), \
                mock.patch("requests.post", side_effect=lambda url, **kw:
                           posts.append((url, kw["json"])) or self._Resp(status, body)):
            result = cr.send_context({}, "https://x", "helpwo", history or [
                {"role": "user", "content": "go"}])
        return result, posts

    def test_the_gateway_seals_what_the_cli_sends(self):
        env_id, posts = self._send(200, {"id": "ctx-1"})
        self.assertEqual(env_id, "ctx-1")
        url, body = posts[0]
        self.assertEqual(url, "https://x/api/context/envelopes")
        self.assertEqual((body["origin"], body["target"]), ("laintas-cli", "helpwo"))

    def test_a_refusal_is_reported_in_words(self):
        with self.assertRaises(cr.SendError) as caught:
            self._send(413, {"detail": "Delete something."})
        self.assertIn("storage is full", str(caught.exception))
        self.assertIn("Delete something.", str(caught.exception))

    def test_exporter_id_is_encoded_in_the_broker_query(self):
        seen = []

        class Resp:
            status_code = 404

        with mock.patch.object(cr.official_messages, "auth_args",
                               return_value={"headers": {}}), \
                mock.patch("requests.get", side_effect=lambda url, **kw: seen.append(url) or Resp()):
            cr.fetch_envelope({}, "https://x", "issues", "ctx-1", "a&b=c")
        self.assertTrue(seen[0].endswith("?from=a%26b%3Dc"))


class LandTest(unittest.TestCase):
    ENVELOPE = {"kernel": 1, "kind": "conversation", "origin": "Helpwow", "items": [
        {"role": "user", "content": "fix the build"},
        {"role": "assistant", "content": "done"}]}

    def test_land_saves_the_imported_session(self):
        import account_tasks
        saved = []
        with mock.patch.object(cr, "fetch_envelope", return_value=self.ENVELOPE), \
                mock.patch.object(cr.paths, "ACCOUNT_USER_ID", "u1"), \
                mock.patch.object(account_tasks, "save_received",
                                  side_effect=lambda blob: saved.append(blob) or blob), \
                mock.patch.object(account_tasks, "activate", return_value=True):
            out = cr.land({}, "https://x", ("helpwo", "ctx-1", ""), "/tmp")
        self.assertEqual(out["title"], "fix the build")
        self.assertTrue(out["ok"] and out["activated"])
        history = saved[0]["chat_history"]
        self.assertEqual(history[-1]["role"], "knowledge")
        self.assertTrue(history[-1]["content"].startswith(cr._IMPORTED_NOTE))

    def test_missing_envelope_is_not_found(self):
        with mock.patch.object(cr, "fetch_envelope", return_value=None):
            out = cr.land({}, "https://x", ("helpwo", "ctx-1", ""), "/tmp")
        self.assertEqual(out, {"ok": False, "reason": "not_found"})

    def test_an_imported_session_can_be_sent_on(self):
        history = cr.session_history(self.ENVELOPE, "helpwo")
        history.insert(0, {"role": "knowledge", "content": "summary of older turns"})
        history.append({"role": "assistant", "content": "x", "reasoning_content": "t",
                        "_reasoning_model": "m", "reasoning_details": [{"sig": 1}]})
        items = cr.context_items(history)
        self.assertEqual(items[0], {"role": "system", "content": "summary of older turns"},
                         "a summary travels as a system note")
        self.assertNotIn(cr._IMPORTED_NOTE.strip(), json.dumps(items),
                         "the import note stays with the session it describes")
        self.assertEqual(items[-1], {"role": "assistant", "content": "x", "reasoning_content": "t"},
                         "model-only reasoning state stays home")

    def test_handoff_notices_are_not_offered_for_import(self):
        self.assertIsNone(cr.parse_context_ref(
            "https://helpwow.laintas.com/?context=handoff%3Arepo%2Fh1"))


if __name__ == "__main__":
    unittest.main()
