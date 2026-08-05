import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend_profiles
import laintas_cli
import mode_manager
import paths
import session_store
import terminal_preferences


class TerminalIdentityTests(unittest.TestCase):
    def test_explicit_terminal_id_is_sanitized(self):
        with mock.patch.dict(os.environ, {"LAINTAS_TERMINAL_ID": "pane / 7"}):
            self.assertEqual(paths._derive_terminal_id(), "pane---7")

    def test_tty_identity_is_stable_and_terminal_specific(self):
        cleared = {
            key: "" for key in (
                "LAINTAS_TERMINAL_ID", "TERM_SESSION_ID", "WT_SESSION",
                "TMUX_PANE", "WEZTERM_PANE", "KITTY_WINDOW_ID",
            )
        }
        with mock.patch.dict(os.environ, cleared), \
                mock.patch.object(paths.os, "getsid", return_value=42), \
                mock.patch.object(paths.os, "ttyname", return_value="/dev/pts/1"):
            first = paths._derive_terminal_id()
            again = paths._derive_terminal_id()
        with mock.patch.dict(os.environ, cleared), \
                mock.patch.object(paths.os, "getsid", return_value=43), \
                mock.patch.object(paths.os, "ttyname", return_value="/dev/pts/2"):
            second = paths._derive_terminal_id()
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)

    def test_child_terminal_identity_is_stable_and_parent_scoped(self):
        first = paths.child_terminal_id("worker", "term0")
        self.assertEqual(first, paths.child_terminal_id("worker", "term0"))
        self.assertNotEqual(first, paths.child_terminal_id("worker", "term1"))
        self.assertNotEqual(first, paths.child_terminal_id("other", "term0"))


class TerminalPreferenceTests(unittest.TestCase):
    def setUp(self):
        terminal_preferences.reset_cache()

    def tearDown(self):
        terminal_preferences.reset_cache()

    def test_model_provider_and_mode_survive_restart_per_terminal(self):
        # load_config() must be stubbed too: get_selected_model() falls back to
        # the global config.json when a terminal has no preference of its own,
        # so without this the assertions read whatever model the developer
        # happens to have selected on their real machine.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)), \
                mock.patch.object(laintas_cli, "load_config", return_value={}):
            with mock.patch.object(paths, "TERMINAL_ID", "term-a"):
                laintas_cli.set_model_selection("model-a", "provider-a")
                self.assertTrue(mode_manager.activate("auto")[0])
                terminal_preferences.reset_cache()  # simulate process restart
                self.assertEqual(laintas_cli.get_selected_model(), "model-a")
                self.assertEqual(laintas_cli.get_selected_provider(), "provider-a")
                self.assertEqual(mode_manager.get_active_mode()["name"], "auto")

            with mock.patch.object(paths, "TERMINAL_ID", "term-b"):
                terminal_preferences.reset_cache()
                self.assertEqual(laintas_cli.get_selected_model(), "")
                self.assertEqual(mode_manager.get_active_mode()["name"], "act")
                laintas_cli.set_model_selection("model-b", "provider-b")
                self.assertTrue(mode_manager.activate("review")[0])

            with mock.patch.object(paths, "TERMINAL_ID", "term-a"):
                terminal_preferences.reset_cache()
                self.assertEqual(laintas_cli.get_selected_model(), "model-a")
                self.assertEqual(mode_manager.get_active_mode()["name"], "auto")
            with mock.patch.object(paths, "TERMINAL_ID", "term-b"):
                terminal_preferences.reset_cache()
                self.assertEqual(laintas_cli.get_selected_model(), "model-b")
                self.assertEqual(mode_manager.get_active_mode()["name"], "review")

    def test_independent_updates_preserve_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)), \
                mock.patch.object(paths, "TERMINAL_ID", "term-a"):
            laintas_cli.set_model_selection("model-a", "provider-a")
            mode_manager.activate("auto")
            terminal_preferences.set_ui_preference("detail", True)
            saved = json.loads(
                terminal_preferences.preference_path().read_text(encoding="utf-8"))
            self.assertEqual(saved["model"], "model-a")
            self.assertEqual(saved["provider"], "provider-a")
            self.assertEqual(saved["mode"], "auto")
            self.assertTrue(saved["ui"]["detail"])

    def test_missing_custom_mode_repairs_only_current_terminal(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)), \
                mock.patch.object(paths, "TERMINAL_ID", "term-a"):
            terminal_preferences.set_value("mode", "deleted-custom-mode")
            self.assertEqual(mode_manager.get_active_mode()["name"], "act")
            terminal_preferences.reset_cache()
            self.assertEqual(terminal_preferences.get("mode"), "act")

    def test_backend_selection_is_terminal_scoped(self):
        config = {
            "version": 1,
            "active": "official",
            "profiles": {
                "official": {
                    "baseUrl": "https://laintas.com", "auth": "laintas-session"},
                "local": {"baseUrl": "http://127.0.0.1:2913", "auth": "none"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp) / "sessions"), \
                mock.patch.object(paths, "BACKENDS_FILE", Path(tmp) / "backends.json"):
            paths.BACKENDS_FILE.write_text(json.dumps(config), encoding="utf-8")
            paths.BACKENDS_FILE.chmod(0o600)
            with mock.patch.object(paths, "TERMINAL_ID", "term-a"):
                self.assertTrue(backend_profiles.set_active("local")[0])
                self.assertEqual(
                    backend_profiles.resolve("https://laintas.com").name, "local")
            with mock.patch.object(paths, "TERMINAL_ID", "term-b"):
                terminal_preferences.reset_cache()
                self.assertEqual(
                    backend_profiles.resolve("https://laintas.com").name, "official")


class TerminalSessionRecoveryTests(unittest.TestCase):
    def test_corrupt_pointer_cannot_recover_another_terminals_session(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)):
            with mock.patch.object(paths, "TERMINAL_ID", "term-a"):
                session_a = session_store.create_session(
                    "/work", {"objective": "A"}, [])
                pointer_a = session_store._current_path("/work")
            with mock.patch.object(paths, "TERMINAL_ID", "term-b"):
                session_store.create_session("/work", {"objective": "B"}, [])

            with mock.patch.object(paths, "TERMINAL_ID", "term-a"):
                pointer_a.write_text("{broken", encoding="utf-8")
                recovered = session_store.load_current_session("/work")
                self.assertEqual(recovered["session_id"], session_a["session_id"])
                self.assertEqual(recovered["objective"], "A")


if __name__ == "__main__":
    unittest.main()
