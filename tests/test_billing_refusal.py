"""Billing refusals must say which thing ran out, and where to look.

An empty wallet, a spent membership allowance and two requests meeting at the
billing lock all used to reach the terminal as "Payment authorization failed:
Unable to reserve this request before provider access". Only the first is a
payment problem, and only the busy lock is fixed by trying again.
"""

import unittest
from unittest import mock

import backend_profiles
import laintas_cli


class _Refusal:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = {}
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body

    def close(self):
        pass


def _run(response, prompt="hello"):
    profile = backend_profiles.BackendProfile(
        "custom", "custom", "https://ai.example.com")
    with mock.patch.object(laintas_cli, "get_backend_profile", return_value=profile), \
            mock.patch.object(laintas_cli.requests, "post", return_value=response) as post, \
            mock.patch.object(laintas_cli.time, "sleep"), \
            mock.patch.object(laintas_cli, "get_selected_model", return_value=""), \
            mock.patch.object(laintas_cli, "get_selected_provider", return_value=""):
        lang = "CN" if any("一" <= ch <= "鿿" for ch in prompt) else "EN"
        result = laintas_cli.call_backend_stream(
            {}, prompt, "system", "/tmp", tools_enabled=False, lang=lang)
    return result, post.call_count


BALANCE = {"code": "insufficient_balance", "title": "Balance exhausted",
           "detail": "Your balance ($0.00) does not cover this request.",
           "remedy": "Top up at https://laintas.com/settings."}
ALLOWANCE = {"code": "quota_exceeded", "title": "Membership allowance used up",
             "detail": "7000 of 7000 monthly calls used for cli."}


class BillingRefusalTests(unittest.TestCase):
    def test_empty_wallet_says_balance_exhausted_and_names_the_page(self):
        result, _ = _run(_Refusal(402, BALANCE))
        self.assertTrue(result["error"])
        self.assertEqual(result["error_code"], "insufficient_balance")
        self.assertTrue(result["reply"].startswith("Balance exhausted"))
        self.assertIn("laintas.com/settings", result["reply"])
        self.assertNotIn("Payment authorization", result["reply"])

    def test_spent_allowance_is_not_retried_and_points_at_the_console(self):
        result, calls = _run(_Refusal(429, ALLOWANCE))
        self.assertEqual(calls, 1)
        self.assertTrue(result["reply"].startswith("Membership allowance used up"))
        self.assertIn("laintas.com/dashboard", result["reply"])

    def test_busy_billing_is_retried(self):
        busy = {"code": "billing_busy", "title": "Billing is busy",
                "detail": "Nothing was charged.", "remedy": "Retry in a moment."}
        result, calls = _run(_Refusal(503, busy))
        self.assertGreater(calls, 1)
        self.assertTrue(result["reply"].startswith("Billing busy"))

    def test_chinese_prompt_gets_the_same_english_refusal(self):
        # Product-authored refusal copy is English-only: a CN turn gets the
        # same coded refusal as an EN one. Functional CJK (intent regexes,
        # test input) stays; product prompts do not.
        result, _ = _run(_Refusal(402, BALANCE), prompt="帮我看看这个报错")
        self.assertTrue(result["reply"].startswith("Balance exhausted"))
        self.assertIn("https://laintas.com/settings", result["reply"])
        result, _ = _run(_Refusal(429, ALLOWANCE), prompt="帮我看看这个报错")
        self.assertTrue(result["reply"].startswith("Membership allowance used up"))


if __name__ == "__main__":
    unittest.main()
