"""Grounding repair on the compaction summary.

The summary replaces the turns it summarizes, so this is the one model output
that can never be checked against its source again afterwards. The tests pin
the ways this feature could do more harm than the fabrications it catches:
losing the summary when the checker misfires, and failing a compaction because
the checker is unreachable.
"""
import unittest
from unittest import mock

import grounding


SRC = "cron 的 MemoryMax 从 1G 收紧到 256M 并通过实测。实测峰值 RSS 从 572MB 降到 42MB。"
SUMMARY = ("分块上限从 1200 调整为 1600。cron 内存上限收紧到 512M。"
           "另外还顺手把 Redis 缓存层接了进来。")
REPAIRED = "分块上限从 1200 调整为 1600。cron 的 MemoryMax 从 1G 收紧到 256M 并通过实测。"

BODY = {
    "method": "nli",
    "sanitized": REPAIRED,
    "checked": 3,
    "hallucinations": 2,
    "threshold": 0.9,
    "changes": [
        {"text": "cron 内存上限收紧到 512M。", "action": "replaced",
         "replacement": "cron 的 MemoryMax 从 1G 收紧到 256M 并通过实测。",
         "score": 1.0, "type": "contradiction"},
        {"text": "另外还顺手把 Redis 缓存层接了进来。", "action": "removed",
         "replacement": None, "score": 0.999, "type": "unsupported"},
    ],
}


class RepairSummary(unittest.TestCase):
    def _repair(self, body):
        with mock.patch.object(grounding, "repair", return_value=body):
            return grounding.repair_summary(SRC, SUMMARY)

    def test_returns_the_repaired_text(self):
        out = self._repair(BODY)
        self.assertEqual(out, REPAIRED)
        # The distorted number is not merely deleted — it is restored from the
        # source, which is the reason to repair rather than drop.
        self.assertIn("256M", out)
        self.assertNotIn("512M", out)
        self.assertNotIn("Redis", out)

    def test_check_unavailable_leaves_the_summary_byte_identical(self):
        self.assertEqual(self._repair(None), SUMMARY)

    def test_empty_repair_keeps_the_summary(self):
        # A misfiring checker (empty source, threshold drifted off the model)
        # flags every sentence and hands back "". Taking that literally would
        # delete the session's memory. Losing the check is recoverable.
        self.assertEqual(self._repair({**BODY, "sanitized": "   "}), SUMMARY)

    def test_logs_each_repair_and_each_drop(self):
        lines = []
        with mock.patch.object(grounding, "repair", return_value=BODY):
            grounding.repair_summary(SRC, SUMMARY, log=lines.append)
        self.assertEqual(len(lines), 2)
        self.assertIn("repaired", lines[0])
        self.assertIn("256M", lines[0])       # the restoring sentence is shown
        self.assertIn("dropped", lines[1])

    def test_flags_a_partial_pass(self):
        lines = []
        with mock.patch.object(grounding, "repair",
                               return_value={**BODY, "source_truncated": True}):
            grounding.repair_summary(SRC, SUMMARY, log=lines.append)
        self.assertIn("partly readable", lines[-1])


class RepairTransport(unittest.TestCase):
    def setUp(self):
        grounding._endpoint_disabled = False
        self.addCleanup(setattr, grounding, "_endpoint_disabled", False)
        self.enterContext(mock.patch.object(
            grounding, "_load_session", return_value={"token": "t"}))
        self.enterContext(mock.patch.object(
            grounding.backend_profiles, "request_auth", return_value=({}, {})))

    def _resp(self, status=200, body=None):
        r = mock.Mock(status_code=status)
        r.json.return_value = body if body is not None else {}
        return r

    def _call(self, resp):
        with mock.patch.dict("sys.modules", {"requests": mock.Mock(post=mock.Mock(return_value=resp))}):
            return grounding.repair(SRC, SUMMARY)

    def test_returns_the_body_on_a_good_response(self):
        self.assertEqual(self._call(self._resp(body=BODY)), BODY)

    def test_checker_outage_is_not_a_clean_bill(self):
        # method:"unavailable" means the gateway answered but the NLI service
        # did not. Its echoed-back `sanitized` must not be mistaken for a pass.
        self.assertIsNone(self._call(self._resp(
            body={"method": "unavailable", "sanitized": SUMMARY, "changes": []})))

    def test_nothing_checkable_is_not_a_clean_bill_either(self):
        self.assertIsNone(self._call(self._resp(
            body={"method": "none", "sanitized": SUMMARY, "changes": []})))

    def test_missing_sanitized_field_is_rejected(self):
        self.assertIsNone(self._call(self._resp(body={"method": "nli", "changes": []})))

    def test_missing_endpoint_disables_further_attempts(self):
        self.assertIsNone(self._call(self._resp(status=404)))
        self.assertTrue(grounding._endpoint_disabled)
        with mock.patch.dict("sys.modules", {"requests": mock.Mock(
                post=mock.Mock(side_effect=AssertionError("should not be called")))}):
            self.assertIsNone(grounding.repair(SRC, SUMMARY))

    def test_empty_input_never_calls_out(self):
        with mock.patch.dict("sys.modules", {"requests": mock.Mock(
                post=mock.Mock(side_effect=AssertionError("should not be called")))}):
            self.assertIsNone(grounding.repair("", SUMMARY))
            self.assertIsNone(grounding.repair(SRC, ""))


if __name__ == "__main__":
    unittest.main()
