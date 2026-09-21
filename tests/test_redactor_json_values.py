"""S3 (bughunt): JSON-quoted secret values must be redacted.

The KEY pattern excluded quotes from the value (`[^\s"']{4,}`), so a JSON
string value — `"api_key": "sk-..."` — starts with a quote and never
matched: the secret reached logs unredacted while the identical bare
form (`api_key: sk-...`) was caught. The fix adds a quoted-value pattern
that swallows the quote pair with the value, keeping surrounding JSON
shape intact. The keyword vocabulary itself is unchanged (bare `token`
was never a keyword; that is a separate policy decision).
"""
import unittest

import redactor


class JsonQuotedValueTests(unittest.TestCase):
    def test_json_quoted_values_are_redacted(self):
        for text in ('{"api_key": "mysecretpassword123"}',
                     '{"password": "hunter2secret42"}',
                     '{"access_token": "xyz999token888"}',
                     '{"secret": "s3cr3tvalue99"}'):
            spans = redactor.scan_text(text)
            self.assertTrue(spans, f"quoted value leaked: {text!r}")
            out = redactor.apply_spans(text, spans)
            self.assertNotIn("secretpassword", out)
            self.assertNotIn("hunter2", out)
            self.assertNotIn("xyz999", out)
            self.assertNotIn("s3cr3t", out)

    def test_bare_values_still_redacted(self):
        spans = redactor.scan_text("api_key: mysecretpassword123")
        self.assertTrue(spans)

    def test_single_quoted_values_redacted(self):
        spans = redactor.scan_text("secret: 'quoted_secret_value'")
        self.assertTrue(spans)

    def test_placeholder_keeps_json_shape(self):
        text = '{"api_key": "mysecretpassword123", "n": 1}'
        spans = redactor.scan_text(text)
        out = redactor.apply_spans(text, spans)
        # The quote pair is swallowed with the value: no dangling quotes.
        self.assertNotIn('""', out)
        self.assertIn('"n": 1', out)

    def test_normal_json_values_not_flagged(self):
        for text in ('{"name": "张三", "city": "北京"}',
                     '{"title": "The Password Manager Guide"}',
                     '正常的中文文本不应误伤'):
            spans = redactor.scan_text(text)
            self.assertFalse(spans, f"false positive on {text!r}")

    def test_scrub_text_enforce_redacts_json_secret(self):
        text = '{"api_key": "mysecretpassword123"}'
        out, spans = redactor.scrub_text(text, enforce=True, capture=False)
        self.assertTrue(spans)
        self.assertNotIn("mysecretpassword123", out)


if __name__ == "__main__":
    unittest.main()
