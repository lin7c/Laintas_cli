"""S4 (bughunt): structured tool parameters must be scrubbed, not just
_TEXT_KEYS strings.

_scrub_content's dict branch only scanned strings under whitelisted keys
(content/text/output/command/input/value). A Bearer token under
input.headers.Authorization — or any secret in a key the whitelist never
heard of — sailed through unredacted while the identical serialized-JSON
arguments form was caught. The fix scrubs every string value in dict
branches; the patterns are high-confidence secret shapes, so ordinary
values match nothing.
"""
import json
import unittest

import redactor


class StructuredParamScrubTests(unittest.TestCase):
    def test_dict_tool_input_secret_is_redacted(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "tool_use", "name": "http_request",
             "input": {"headers": {
                 "Authorization": "Bearer sk-zzzzzzzz12345678"}}}
        ]}]
        out, n = redactor.scrub_messages(msgs, enforce=True)
        raw = json.dumps(out)
        self.assertGreaterEqual(n, 1)
        self.assertNotIn("sk-zzzzzzzz12345678", raw)

    def test_arbitrary_key_names_covered(self):
        # The whole point of the fix: any key, not a whitelist.
        msgs = [{"role": "assistant", "content": [
            {"type": "tool_use", "name": "deploy",
             "input": {"meta_config": {"api_key": "KGAT_abcdefgh12345678"}}}
        ]}]
        out, n = redactor.scrub_messages(msgs, enforce=True)
        self.assertNotIn("KGAT_abcdefgh12345678", json.dumps(out))

    def test_serialized_arguments_still_redacted(self):
        msgs = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "http",
                         "arguments": json.dumps({
                             "headers": {"token": "sk-abcdefgh12345678"}})}
        }]}]
        out, n = redactor.scrub_messages(msgs, enforce=True)
        self.assertNotIn("sk-abcdefgh12345678", json.dumps(out))

    def test_ordinary_values_untouched(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "正常"},
            {"type": "tool_use", "name": "fs_read",
             "input": {"path": "/tmp/x.py", "mode": "r"}}
        ]}]
        out, n = redactor.scrub_messages(msgs, enforce=True)
        self.assertEqual(n, 0)
        self.assertEqual(out[0]["content"][1]["input"]["path"], "/tmp/x.py")

    def test_scrub_text_direct_on_structured_string(self):
        out, spans = redactor.scrub_text(
            "Bearer sk-abcdefgh12345678", enforce=True, capture=False)
        self.assertTrue(spans)
        self.assertNotIn("sk-abcdefgh12345678", out)



class StructuredDataNotMangledTests(unittest.TestCase):
    def test_non_secret_values_survive(self):
        # Arbitrary structured values are scanned for credentials only; the
        # shape-guess types (CARD/IPV4/...) rewrote timestamps and hosts in
        # tool calls the model later reads back under redact_enforce.
        import redactor
        value = {"arguments": {"cmd": "curl http://192.168.1.10:8080/x"},
                 "params": {"ts": "1758300000000", "sha": "a" * 40,
                            "auth": "Bearer sk-abcdefghijklmnop"}}
        out, _n = redactor._scrub_content(
            value, enforce=True, capture=False, source="t")
        self.assertEqual(out["arguments"]["cmd"], value["arguments"]["cmd"])
        self.assertEqual(out["params"]["ts"], "1758300000000")
        self.assertEqual(out["params"]["sha"], "a" * 40)
        self.assertNotIn("sk-abcdefghijklmnop", out["params"]["auth"])

if __name__ == "__main__":
    unittest.main()
