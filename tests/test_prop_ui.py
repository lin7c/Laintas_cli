import unittest
import threading
from unittest import mock

import laintas_cli
import prop_ui
import resource_ui


class PropUITests(unittest.TestCase):
    def test_target_uses_newest_first_numbering(self):
        self.assertEqual(prop_ui.parse_target(""), (False, 1))
        self.assertEqual(prop_ui.parse_target("2"), (False, 2))
        self.assertEqual(prop_ui.parse_target("sys"), (True, 1))
        self.assertEqual(prop_ui.parse_target("sys 3"), (True, 3))
        with self.assertRaises(ValueError):
            prop_ui.parse_target("sys 0")
        with self.assertRaises(ValueError):
            prop_ui.parse_target("wat")

    def test_full_context_is_grouped_around_latest_call(self):
        conversation = {"calls": [{
            "system_prompt": "system exact",
            "messages": [
                {"role": "system", "content": "system exact"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            "tool_schemas": [{"type": "function", "function": {"name": "read"}}],
            "metadata": {"loop": 7, "verified_gateway_context": True},
            "system_sections": [{
                "id": "policy", "title": "Policy", "origin": "runtime",
                "editable": False, "content": "locked",
            }],
            "gateway_context_receipt": {
                "verified": True, "system_sha256": "abc",
                "effective_system_prompt": "system exact",
                "messages": [], "tools": [],
            },
        }]}
        items = prop_ui.context_items(conversation)
        self.assertEqual([item.key for item in items], [
            "effective-system-prompt", "system-sections", "messages", "tools",
            "capture-metadata", "model-calls"])
        self.assertIn('"role": "user"', items[2].payload["content"])
        self.assertIn('"name": "read"', items[3].payload["content"])
        self.assertIn("gateway verified", items[0].subtitle)
        self.assertIn("latest/final default", items[0].subtitle)

    def test_six_large_calls_remain_bounded_and_lossless(self):
        calls = []
        for call_number in range(1, 7):
            calls.append({
                "system_prompt": f"system-{call_number}",
                "system_sections": [{
                    "id": f"section-{call_number}",
                    "content": f"section body {call_number}",
                }],
                "messages": [{
                    "role": "user", "content": f"message-{call_number}",
                }],
                "tool_schemas": [{
                    "type": "function",
                    "function": {"name": f"call_{call_number}_tool_{tool_number}"},
                } for tool_number in range(132)],
                "metadata": {"loop": call_number},
                "gateway_context_receipt": None,
            })

        items = prop_ui.context_items({"calls": calls})

        self.assertEqual(len(items), 6)
        self.assertEqual(items[0].payload["content"], "system-6")
        self.assertIn("message-6", items[2].search_text)
        self.assertNotIn("message-5", items[2].search_text)
        self.assertIn("call_6_tool_131", items[3].search_text)
        self.assertNotIn("call_5_tool_131", items[3].search_text)
        self.assertIn("132", items[3].title)
        model_calls = next(item for item in items if item.key == "model-calls")
        self.assertIn("message-1", model_calls.search_text)
        self.assertIn("call_1_tool_131", model_calls.search_text)
        self.assertIn("message-6", model_calls.search_text)
        self.assertIn("latest/final call is the default", model_calls.subtitle)

    def test_sys_view_has_only_two_latest_system_groups(self):
        conversation = {"calls": [{
            "system_prompt": "old", "messages": [], "tool_schemas": [],
            "metadata": {}, "system_sections": [], "gateway_context_receipt": None,
        }, {
            "system_prompt": "new", "messages": [{"role": "user", "content": "u"}],
            "tool_schemas": [{"name": "x"}], "metadata": {},
            "system_sections": [{"title": "Policy", "content": "latest policy"}],
            "gateway_context_receipt": None,
        }]}
        items = prop_ui.context_items(conversation, system_only=True)
        self.assertEqual([item.key for item in items], [
            "effective-system-prompt", "system-sections"])
        self.assertEqual(items[0].payload["content"], "new")
        self.assertIn("latest policy", items[1].search_text)
        self.assertNotIn("old", " ".join(item.search_text for item in items))

    def test_command_loads_requested_session_index_and_ai_has_no_tools(self):
        conversation = {"calls": [{
            "system_prompt": "system", "messages": [], "tool_schemas": [],
            "metadata": {"verified_gateway_context": True},
            "system_sections": [], "gateway_context_receipt": None,
        }]}
        captured = {}

        def open_browser(value, **kwargs):
            captured.update(kwargs)
            captured["conversation"] = value
            return resource_ui.UIOutcome("cancel")

        previous = getattr(laintas_cli.handle_meta_command, "_last_agent_state", None)
        laintas_cli.handle_meta_command._last_agent_state = {"_session_id": "session-x"}
        try:
            with mock.patch("context_snapshot.load_conversation",
                            return_value=conversation) as load, \
                    mock.patch("prop_ui.open_browser", side_effect=open_browser), \
                    mock.patch.object(laintas_cli, "call_backend_stream",
                                      return_value={"reply": "translated", "error": False}) as backend:
                laintas_cli._cmd_prop("sys 2", {"token": "x"})
                item = prop_ui.context_items(conversation, system_only=True)[0]
                result = captured["assistant_handler"](
                    item, prop_ui.item_detail(item), "翻译成中文", threading.Event())
        finally:
            if previous is None:
                delattr(laintas_cli.handle_meta_command, "_last_agent_state")
            else:
                laintas_cli.handle_meta_command._last_agent_state = previous

        load.assert_called_once_with("session-x", 2)
        self.assertTrue(captured["system_only"])
        self.assertEqual(captured["newest_index"], 2)
        self.assertEqual(result.detail.lines[0].text, "translated")
        kwargs = backend.call_args.kwargs
        self.assertFalse(kwargs["tools_enabled"])
        self.assertEqual(kwargs["task_kind"], "context_inspector")
        self.assertNotIn("context_capture", kwargs)


if __name__ == "__main__":
    unittest.main()
