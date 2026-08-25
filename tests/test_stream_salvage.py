"""A stream that dies mid-answer must become a truncated turn, not a lost one.

Reasoning turns stream for a long time before the transport gives up — the read
timeout, a key whose upstream cuts long thinking streams, a dropped connection.
Everything received was paid for; throwing it away is the most expensive and
least useful possible outcome.
"""

import unittest
from unittest import mock

import backend_profiles
import laintas_cli
import requests


def _sse(*events):
    """Render events as SSE lines the way the gateway does."""
    import json
    out = []
    for e in events:
        out.append("data: " + json.dumps(e))
    return out


class _FakeResponse:
    """Streams the given lines, then raises `boom` if one was supplied."""

    def __init__(self, lines, boom=None):
        self.status_code = 200
        self.headers = {}
        self._lines = lines
        self._boom = boom

    def iter_lines(self, *a, **kw):
        for line in self._lines:
            yield line
        if self._boom is not None:
            raise self._boom

    def close(self):
        pass


def _run(lines, boom=None):
    profile = backend_profiles.BackendProfile(
        "custom", "custom", "https://ai.example.com")
    with mock.patch.object(laintas_cli, "get_backend_profile", return_value=profile), \
            mock.patch.object(laintas_cli.requests, "post",
                              return_value=_FakeResponse(lines, boom)), \
            mock.patch.object(laintas_cli, "get_selected_model", return_value=""), \
            mock.patch.object(laintas_cli, "get_selected_provider", return_value=""):
        return laintas_cli.call_backend_stream(
            {}, "hello", "system", "/tmp", tools_enabled=False)


class StreamSalvageTests(unittest.TestCase):
    def test_context_receipt_is_requested_and_captured_out_of_band(self):
        profile = backend_profiles.BackendProfile(
            "custom", "custom", "https://ai.example.com")
        capture = {}
        lines = _sse(
            {"_context": {
                "verified": True, "effective_system_prompt": "final system",
                "messages": [{"role": "system", "content": "final system"}],
                "tools": [],
            }},
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        )
        with mock.patch.object(laintas_cli, "get_backend_profile", return_value=profile), \
                mock.patch.object(laintas_cli.requests, "post",
                                  return_value=_FakeResponse(lines)) as post, \
                mock.patch.object(laintas_cli, "get_selected_model", return_value=""), \
                mock.patch.object(laintas_cli, "get_selected_provider", return_value=""):
            result = laintas_cli.call_backend_stream(
                {}, "hello", "local system", "/tmp", tools_enabled=False,
                context_capture=capture)

        self.assertEqual(result["reply"], "ok")
        self.assertTrue(post.call_args.kwargs["json"]["contextReceipt"])
        self.assertEqual(capture["client_payload"]["systemPrompt"], "local system")
        self.assertEqual(
            capture["gateway_receipt"]["effective_system_prompt"], "final system")

    def test_timeout_after_content_keeps_the_partial_answer(self):
        lines = _sse(
            {"choices": [{"delta": {"content": "I checked the first module and "}}]},
            {"choices": [{"delta": {"content": "found one leak in the reader"}}]},
        )
        result = _run(lines, boom=requests.Timeout("read timed out"))

        self.assertFalse(result["error"])
        self.assertFalse(result["done"])
        self.assertTrue(result["_truncated"])
        self.assertEqual(result["_truncation_kind"], "stream_timeout")
        self.assertIn("found one leak in the reader", result["reply"])

    def test_timeout_before_any_content_is_still_an_error(self):
        result = _run([], boom=requests.Timeout("read timed out"))

        self.assertTrue(result["error"])
        self.assertTrue(result["done"])
        self.assertNotIn("_truncated", result)

    def test_half_written_tool_call_is_dropped_not_executed(self):
        lines = _sse(
            {"choices": [{"delta": {"content": "writing the file now"}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "fs.write",
                             "arguments": '{"path": "/etc/hosts", "content": "half'},
            }]}}]},
        )
        result = _run(lines, boom=requests.ConnectionError("connection reset"))

        self.assertTrue(result["_truncated"])
        self.assertEqual(result["tool_calls"], [])   # never run a half-written write
        self.assertIn("writing the file now", result["reply"])

    def test_upstream_error_after_content_is_marked_truncated(self):
        """The gateway streamed content, then reported the upstream died. That
        partial must not pass as a finished answer."""
        lines = _sse(
            {"choices": [{"delta": {"content": "Here is the first half"}}]},
            {"error": "Upstream ended the response early",
             "details": "The model provider closed the stream."},
        )
        result = _run(lines)

        self.assertFalse(result["error"])
        self.assertTrue(result["_truncated"])
        self.assertEqual(result["_truncation_kind"], "stream_dropped")
        self.assertIn("Here is the first half", result["reply"])
        self.assertIn("closed the stream", result["_truncation_detail"])


if __name__ == "__main__":
    unittest.main()
