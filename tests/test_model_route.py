"""`/model route` (and the /model picker): the chat model served by the backend or by the user's key.

The direct route reuses the gateway stream parser, so the end-to-end test drives
the real `_call_backend_stream_impl` against a local OpenAI-compatible server
and checks what the provider received as well as what the CLI made of the
answer.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import laintas_cli
import model_route
import paths


class _FakeProvider(BaseHTTPRequestHandler):
    received: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer sk-test-1234567890":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"bad key"}}')
            return
        body = json.dumps({"data": [{"id": "m-small"},
                                    {"id": "m-big", "context_length": 200000}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length))
        type(self).received.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": request,
        })
        if request.get("model") == "m-small" and "reasoning_effort" in request:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Unsupported parameter: reasoning_effort"}}')
            return
        events = [
            {"model": "m-big", "choices": [{"delta": {"content": "Checking."}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
              "function": {"name": "fs_read", "arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
              "function": {"arguments": '"a.txt"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 9,
                                      "prompt_tokens_details": {"cached_tokens": 100}}},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class ModelRouteTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(paths, "MODEL_PROVIDERS_FILE", home / "model_providers.json"),
            mock.patch.object(paths, "MODEL_KEYS_FILE", home / "model_keys.json"),
            mock.patch.object(model_route.terminal_preferences, "set_value"),
            mock.patch.object(model_route.terminal_preferences, "delete"),
        ]
        for patcher in self.patches:
            patcher.start()
        self.policy = {}
        policy_patch = mock.patch("policy.get_config", side_effect=lambda: self.policy)
        policy_patch.start()
        self.patches.append(policy_patch)

    def tearDown(self):
        # Reverse order: a later patch of the same attribute must be undone
        # first, or the attribute is "restored" to the earlier mock for good.
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def _select(self, name):
        return mock.patch.dict(os.environ, {model_route.ENV_OVERRIDE: name})

    # ── storage and selection ──────────────────────────────────────────────

    def test_default_route_is_the_backend(self):
        with mock.patch.object(model_route.terminal_preferences, "get", return_value=""):
            self.assertEqual(model_route.selected_name(), "laintas")
            self.assertEqual(model_route.resolve(), (None, ""))

    def test_key_is_kept_out_of_the_route_list(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "deepseek-chat", key="sk-secret-abcdef")
        self.assertNotIn("sk-secret", paths.MODEL_PROVIDERS_FILE.read_text())
        self.assertIn("sk-secret", paths.MODEL_KEYS_FILE.read_text())
        self.assertEqual(paths.MODEL_KEYS_FILE.stat().st_mode & 0o777, 0o600)
        route = model_route.get_route("ds")
        self.assertEqual(route.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(model_route.key_preview(route), "sk-…cdef")

    def test_reserved_and_plaintext_urls_are_refused(self):
        for name in ("laintas", "add", "list"):
            with self.assertRaises(ValueError):
                model_route.save_route(name, "openai", "", "m", key="k")
        with self.assertRaises(ValueError):
            model_route.save_route("x", "custom", "http://example.com/v1", "m", key="k")
        model_route.save_route("local", "custom", "http://127.0.0.1:8000/v1", "m", key="k")

    def test_env_key_reference(self):
        model_route.save_route("oa", "openai", "", "gpt-x", key_env="MY_OA_KEY")
        route = model_route.get_route("oa")
        with mock.patch.dict(os.environ, {"MY_OA_KEY": "sk-from-env"}):
            self.assertEqual(model_route.api_key(route), "sk-from-env")
        self.assertEqual(model_route.key_preview(route), "$MY_OA_KEY")

    def test_remove_drops_key(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "m", key="sk-secret-abcdef")
        with mock.patch.object(model_route.terminal_preferences, "get", return_value="ds"):
            self.assertTrue(model_route.remove_route("ds"))
        self.assertNotIn("sk-secret", paths.MODEL_KEYS_FILE.read_text())
        model_route.terminal_preferences.delete.assert_called_with(model_route.PREF_KEY)

    def test_unusable_selection_is_reported_not_silently_rerouted(self):
        with self._select("gone"):
            route, note = model_route.resolve()
        self.assertIsNone(route)
        self.assertIn("not configured", note)

    # ── policy ─────────────────────────────────────────────────────────────

    def test_policy_can_forbid_direct_routes(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "m", key="sk-secret-abcdef")
        self.policy = {"directModel": "deny", "_org_policy": True}
        with self._select("ds"):
            route, note = model_route.resolve()
        self.assertIsNone(route)
        self.assertIn("organisation policy", note)

    def test_policy_host_allowlist(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "m", key="sk-secret-abcdef")
        route = model_route.get_route("ds")
        self.assertIn("does not allow", model_route.policy_refusal(
            route, {"directModelHosts": ["api.openai.com"]}))
        self.assertEqual(model_route.policy_refusal(
            route, {"directModelHosts": ["*.deepseek.com"]}), "")

    # ── wire ───────────────────────────────────────────────────────────────

    def test_build_request_translates_gateway_payload(self):
        route = model_route.save_route("oa", "openai", "", "gpt-x", key="sk-test-1234567890")
        url, body, headers = model_route.build_request(route, {
            "systemPrompt": "SYS", "maxTokens": 500, "model": "priority",
            "provider": "ark", "injectToolGuide": True, "source": "cli",
            "messages": [{"role": "user", "content": "hi", "_ts": 1}],
            "tools": [{"type": "function", "function": {"name": "t"}}],
        })
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(body["model"], "gpt-x")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(body["max_completion_tokens"], 500)
        self.assertNotIn("provider", body)
        self.assertNotIn("source", body)
        self.assertEqual(headers["Authorization"], "Bearer sk-test-1234567890")
        self.assertNotIn("Cookie", headers)

    def test_end_to_end_stream_through_direct_route(self):
        _FakeProvider.received = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}/v1"
        try:
            self.assertEqual(model_route.fetch_models(base, "sk-test-1234567890"),
                             ["m-big", "m-small"])
            with self.assertRaises(RuntimeError):
                model_route.fetch_models(base, "wrong")
            model_route.save_route("local", "custom", base, "m-big", key="sk-test-1234567890")
            session = {"headers": {"Authorization": "Bearer laintas-session"},
                       "cookies": {"sid": "laintas-cookie"}}
            with self._select("local"), \
                    mock.patch.object(laintas_cli.usage_tracker, "record") as record:
                result = laintas_cli._call_backend_stream_impl(
                    session, "read a.txt", "SYS", "/tmp",
                    messages=[{"role": "user", "content": "read a.txt"}],
                    tools_enabled=False)
        finally:
            server.shutdown()
            server.server_close()

        sent = _FakeProvider.received[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-test-1234567890")
        self.assertNotIn("Cookie", sent["headers"])
        self.assertNotIn("laintas-session", json.dumps(sent))
        self.assertEqual(sent["body"]["model"], "m-big")

        self.assertFalse(result.get("error"), result)
        calls = result.get("tool_calls") or []
        self.assertEqual(len(calls), 1, result)
        billing = result.get("_billing") or {}
        self.assertEqual(billing.get("promptTokens"), 120)
        self.assertEqual(billing.get("billingDomain"), "direct")
        self.assertFalse(record.call_args.kwargs["official"])

    # ── thinking gear and context window ──────────────────────────────────

    def test_gear_maps_to_reasoning_effort(self):
        route = model_route.save_route("oa", "openai", "", "gpt-x", key="sk-test-1234567890")
        def effort(gear, r=route):
            return model_route.build_request(r, {"reasoningEffort": gear})[1].get("reasoning_effort")
        self.assertEqual(effort("max"), "high")
        self.assertEqual(effort("none"), "minimal")
        self.assertIsNone(effort("auto"))
        model_route.mark_reasoning_unsupported("oa", "gpt-x")
        self.assertIsNone(effort("high", model_route.get_route("oa")))

    def test_window_is_per_model_and_user_settable(self):
        model_route.save_route("or", "custom", "https://openrouter.ai/api/v1", "a", key="k", windows={"a": 200000, "b": 0})
        self.assertEqual(model_route.get_route("or").context_window, 200000)
        model_route.set_route_model("or", "b")
        self.assertEqual(model_route.get_route("or").context_window, 0)
        model_route.set_route_model("or", "", 64000)
        self.assertEqual(model_route.get_route("or").context_window, 64000)

    def test_live_model_is_the_direct_one(self):
        import agent_loop
        model_route.save_route("or", "custom", "https://openrouter.ai/api/v1", "vendor/model-x", key="k")
        with self._select("or"):
            self.assertEqual(agent_loop._live_status_model(), "vendor/model-x")

    def _serve(self):
        _FakeProvider.received = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def test_refused_reasoning_is_dropped_and_remembered(self):
        base = self._serve()
        catalog = model_route.fetch_catalog(base, "sk-test-1234567890")
        self.assertEqual(catalog, {"m-small": 0, "m-big": 200000})
        model_route.save_route("local", "custom", base, "m-small",
                               key="sk-test-1234567890", windows=catalog)
        with self._select("local"), \
                mock.patch.object(laintas_cli, "get_runtime_config",
                                  side_effect=lambda k: "high" if k == "reasoning_effort" else 0), \
                mock.patch.object(laintas_cli.usage_tracker, "record"):
            result = laintas_cli._call_backend_stream_impl(
                {}, "hi", "SYS", "/tmp", tools_enabled=False)
        self.assertFalse(result.get("error"), result)
        sent = [r["body"] for r in _FakeProvider.received]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["reasoning_effort"], "high")
        self.assertNotIn("reasoning_effort", sent[1])
        self.assertTrue(model_route.get_route("local").reasoning_unsupported)

    def test_window_reaches_the_loop(self):
        base = self._serve()
        model_route.save_route("local", "custom", base, "m-big",
                               key="sk-test-1234567890", windows={"m-big": 200000})
        with self._select("local"), mock.patch.object(laintas_cli.usage_tracker, "record"):
            result = laintas_cli._call_backend_stream_impl(
                {}, "hi", "SYS", "/tmp", tools_enabled=False)
        self.assertEqual(result["_budget"]["contextWindow"], 200000)

    def test_blocked_route_refuses_the_turn(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "m", key="sk-secret-abcdef")
        self.policy = {"directModel": "deny"}
        with self._select("ds"), mock.patch.object(laintas_cli.requests, "post") as post:
            result = laintas_cli._call_backend_stream_impl(
                {}, "hi", "SYS", "/tmp", tools_enabled=False)
        post.assert_not_called()
        self.assertTrue(result["error"])
        self.assertEqual(result["error_code"], "model_route")


    # ── /model: the route lives in the model command and its picker ────────

    def _model_command(self, *, fetched=None, fetch_error=None):
        """Patch just enough of the REPL for `_cmd_model` on term0."""
        store = {}
        prefs = model_route.terminal_preferences
        terminal = mock.Mock(stationed_agent_id="primary", model_override="glm-5.3",
                             provider_override="")
        stack = [
            mock.patch.object(prefs, "get", side_effect=lambda k, d=None: store.get(k, d)),
            mock.patch.object(prefs, "set_value", side_effect=store.__setitem__),
            mock.patch.object(prefs, "delete", side_effect=lambda k: store.pop(k, None)),
            mock.patch.object(laintas_cli, "get_current_agent", return_value=None),
            mock.patch.object(laintas_cli, "get_terminal",
                              side_effect=lambda n: terminal if n == "term0" else None),
            mock.patch.object(laintas_cli, "set_terminal_model_selection"),
            mock.patch.object(laintas_cli, "set_model_selection"),
            mock.patch.object(laintas_cli, "_update_status_cache"),
            mock.patch.object(laintas_cli, "_rprompt_refill_model_cache"),
            mock.patch.object(laintas_cli, "run_cancellable_blocking",
                              side_effect=fetch_error,
                              return_value=(fetched or [], "test")),
        ]
        for patcher in stack:
            patcher.start()
            self.patches.append(patcher)
        return store

    def _run_model(self, *args):
        laintas_cli._cmd_model(["/model", *args], " ".join(args), {})

    def test_backend_no_longer_owns_the_route(self):
        with mock.patch.object(laintas_cli, "_cmd_model_route") as route:
            laintas_cli._cmd_backend(["/backend", "model", "ds"])
        route.assert_not_called()

    def test_model_route_selects_and_returns(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "deepseek-chat", key="sk-secret-abcdef")
        store = self._model_command()
        self._run_model("route", "ds")
        self.assertEqual(store.get(model_route.PREF_KEY), "ds")
        self._run_model("route", "laintas")
        self.assertNotIn(model_route.PREF_KEY, store)

    def test_model_remove_is_a_top_level_word(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "deepseek-chat", key="sk-secret-abcdef")
        self._model_command()
        self._run_model("remove", "ds")
        self.assertIsNone(model_route.get_route("ds"))

    def test_choosing_a_backend_model_leaves_the_direct_route(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "deepseek-chat", key="sk-secret-abcdef")
        store = self._model_command()
        store[model_route.PREF_KEY] = "ds"
        self._run_model("kimi-k2.7-code")
        self.assertNotIn(model_route.PREF_KEY, store)
        laintas_cli.set_terminal_model_selection.assert_called_with(
            "term0", "kimi-k2.7-code", "")

    def test_picker_lists_routes_after_backend_models(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "deepseek-chat", key="sk-secret-abcdef")
        store = self._model_command(fetched=[{"id": "kimi-k2.7-code", "provider": ""}])
        store[model_route.PREF_KEY] = "ds"
        seen = {}

        def pick(labels, **kwargs):
            seen["labels"], seen["index"] = labels, kwargs.get("selected_index")
            return labels[-1]

        with mock.patch.object(laintas_cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(laintas_cli, "select_dialog", side_effect=pick):
            self._run_model()
        self.assertEqual(len(seen["labels"]), 3)          # auto, backend model, route
        self.assertIn("deepseek-chat", seen["labels"][2])
        self.assertEqual(seen["index"], 2)                # the active route is current
        self.assertTrue(seen["labels"][2].startswith(" *"))
        self.assertFalse(seen["labels"][0].startswith(" *"))
        self.assertEqual(store.get(model_route.PREF_KEY), "ds")

    def test_picker_offers_routes_when_the_backend_list_fails(self):
        model_route.save_route("ds", "custom", "https://api.deepseek.com/v1", "deepseek-chat", key="sk-secret-abcdef")
        store = self._model_command(fetch_error=RuntimeError("offline"))
        with mock.patch.object(laintas_cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(laintas_cli, "select_dialog",
                                  side_effect=lambda labels, **_: labels[-1]):
            self._run_model()
        self.assertEqual(store.get(model_route.PREF_KEY), "ds")

    # ── /model add: positional arguments, hints, and keeping the key private ─

    def _serve_catalog(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def _add(self, *args, tty=False):
        with mock.patch.object(laintas_cli.sys.stdin, "isatty", return_value=tty):
            laintas_cli._cmd_model(["/model", "add", *args], "add " + " ".join(args), {})

    def test_add_takes_everything_inline(self):
        base = self._serve_catalog()
        self._model_command()
        self._add("custom", base, "sk-test-1234567890", "m-big", "mine")
        route = model_route.get_route("mine")
        self.assertEqual((route.model, route.base_url), ("m-big", base))
        self.assertEqual(route.context_window, 200000)
        self.assertEqual(model_route.api_key(route), "sk-test-1234567890")

    def test_add_defaults_the_name_to_the_provider(self):
        base = self._serve_catalog()
        self._model_command()
        self._add("custom", base, "sk-test-1234567890", "m-small")
        self.assertEqual(model_route.get_route("custom").model, "m-small")

    def test_add_refuses_a_bad_key_and_saves_nothing(self):
        base = self._serve_catalog()
        self._model_command()
        self._add("custom", base, "wrong-key", "m-big", "mine")
        self.assertIsNone(model_route.get_route("mine"))

    def test_add_without_a_terminal_names_what_is_missing(self):
        base = self._serve_catalog()
        self._model_command()
        with mock.patch.object(laintas_cli.console, "print") as out:
            self._add("custom", base, "sk-test-1234567890")
        said = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("Missing the model", said)
        self.assertIn("m-big", said)
        self.assertEqual(model_route.list_routes(), [])
        with mock.patch.object(laintas_cli.console, "print") as out:
            self._add("nosuch")
        self.assertIn("Unknown provider", str(out.call_args_list[0].args[0]))

    def test_add_env_reference_stores_no_key(self):
        base = self._serve_catalog()
        self._model_command()
        with mock.patch.dict(os.environ, {"FAKE_PROVIDER_KEY": "sk-test-1234567890"}):
            self._add("custom", base, "$FAKE_PROVIDER_KEY", "m-big", "envy")
        self.assertEqual(model_route.key_preview(model_route.get_route("envy")),
                         "$FAKE_PROVIDER_KEY")
        self.assertNotIn("envy", json.loads(paths.MODEL_KEYS_FILE.read_text()))

    def test_add_line_with_a_key_stays_out_of_history(self):
        private = laintas_cli._is_private_command
        self.assertTrue(private("/model add openai sk-live-123 gpt-5"))
        self.assertTrue(private("/model add custom https://h/v1 sk-live-123"))
        self.assertFalse(private("/model add custom https://h/v1"))
        self.assertFalse(private("/model add openai $OPENAI_API_KEY gpt-5"))
        self.assertFalse(private("/model add openai"))
        self.assertTrue(private("/password"))

    def _rows(self, *prior):
        return dict(laintas_cli._dynamic_arg_candidates("/model", list(prior), ""))

    def test_every_add_position_shows_a_hint_row(self):
        self.assertEqual(sorted(self._rows("add")), ["claude", "custom", "openai"])
        self.assertIn("<key>", self._rows("add", "openai"))
        self.assertIn("$", self._rows("add", "openai"))
        self.assertIn("<base-url>", self._rows("add", "custom"))
        self.assertIn("<key>", self._rows("add", "custom", "https://h/v1"))
        self.assertIn("<name>", self._rows("add", "openai", "k", "m"))
        self.assertIn("openai", self._rows("add", "openai", "k", "m"))
        self.assertIn("<Enter>", self._rows("add", "openai", "k", "m", "n"))

    def test_model_position_lists_the_providers_models(self):
        base = self._serve_catalog()
        rows = self._rows("add", "custom", base, "sk-test-1234567890")
        self.assertIn("<model>", rows)
        self.assertIn("m-big", rows)
        self.assertIn("m-small", rows)
        bad = self._rows("add", "custom", base, "wrong-key")
        self.assertIn("key check failed", bad["<error>"])

    def test_tab_offers_key_variables_but_not_tokens(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x", "SOME_TOKEN": "y"}):
            rows = self._rows("add", "openai")
        self.assertIn("$OPENAI_API_KEY", rows)
        self.assertNotIn("$SOME_TOKEN", rows)

    def test_hint_rows_insert_nothing(self):
        completer = laintas_cli.MetaCompleter()
        from prompt_toolkit.document import Document
        text = "/model add "
        items = list(completer.get_completions(Document(text, len(text)), None))
        self.assertEqual(sorted(c.display_text for c in items),
                         ["claude", "custom", "openai"])
        text = "/model add openai "
        items = list(completer.get_completions(Document(text, len(text)), None))
        hint = [c for c in items if c.display_text == "<key>"]
        self.assertEqual(len(hint), 1)
        self.assertEqual(hint[0].text, "")

if __name__ == "__main__":
    unittest.main()
