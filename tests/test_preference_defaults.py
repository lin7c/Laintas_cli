"""Settings that survive the next launch.

Per-terminal preference files are the right storage and the wrong starting
point. TERMINAL_ID falls back to the tty and POSIX session id, and both change
on every new SSH login, so each connection opened an empty file and the user
set their model and mode again — 45 distinct files in a month on the machine
this was found on, each holding a choice that was never read back.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import paths
import terminal_preferences as prefs


class _PrefsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.sessions = self.home / "sessions"
        self.sessions.mkdir()
        self._patches = [
            mock.patch.object(paths, "LAINTAS_HOME", self.home),
            mock.patch.object(paths, "SESSIONS_DIR", self.sessions),
            mock.patch.object(paths, "TERMINAL_ID", "term-aaa"),
        ]
        for patch in self._patches:
            patch.start()
        prefs.reset_cache()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        prefs.reset_cache()
        self._tmp.cleanup()

    def _write(self, terminal_id, data, mtime=None):
        path = self.sessions / f"{terminal_id}_preferences.json"
        payload = {"version": 1}
        payload.update(data)
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        if mtime is not None:
            import os
            os.utime(path, (mtime, mtime))
        return path

    def _as_terminal(self, terminal_id):
        prefs.reset_cache()
        return mock.patch.object(paths, "TERMINAL_ID", terminal_id)

    def _choose(self, **values):
        prefs.update(values)


class SeedingTests(_PrefsCase):
    def test_a_brand_new_terminal_inherits_the_last_choices(self):
        self._choose(model="glm-5.3")
        with self._as_terminal("term-fresh"):
            prefs.seed_new_terminal()
            self.assertEqual("glm-5.3", prefs.get("model"))

    def test_reading_alone_never_creates_a_file(self):
        """Seeding is a startup action, not something a library read does.

        Doing it inside the storage primitive materialised a file for any
        never-configured terminal that was merely read — including in tests,
        which then wrote the developer's real preferences.
        """
        self._choose(model="glm-5.3")
        with self._as_terminal("term-fresh"):
            self.assertIsNone(prefs.get("model"))
        self.assertFalse(
            (self.sessions / "term-fresh_preferences.json").exists())

    def test_seeding_an_already_configured_terminal_does_nothing(self):
        self._choose(model="glm-5.3")
        with self._as_terminal("term-bbb"):
            self._choose(model="mine")
            self.assertEqual({}, prefs.seed_new_terminal())
            self.assertEqual("mine", prefs.get("model"))

    def test_a_terminal_with_its_own_file_keeps_it(self):
        # Two open terminals stay independent; the seed is a starting point,
        # not a shared value.
        self._choose(model="glm-5.3")
        with self._as_terminal("term-bbb"):
            prefs.seed_new_terminal()
            self._choose(model="deepseek-v4")
        with self._as_terminal("term-bbb"):
            self.assertEqual("deepseek-v4", prefs.get("model"))
        prefs.reset_cache()
        self.assertEqual("glm-5.3", prefs.get("model"))

    def test_the_seed_follows_the_most_recent_choice(self):
        self._choose(model="first")
        with self._as_terminal("term-bbb"):
            self._choose(model="second")
        with self._as_terminal("term-fresh"):
            prefs.seed_new_terminal()
            self.assertEqual("second", prefs.get("model"))

    def test_nothing_is_written_anywhere_but_this_terminals_file(self):
        """There is no shared defaults file to write, on purpose: a write from
        inside the store is a global side effect — one terminal pushing its
        inherited value over a newer choice made in another."""
        self._choose(model="chosen")
        written = {p.name for p in self.sessions.glob("*")}
        self.assertEqual({"term-aaa_preferences.json"}, written)
        self.assertFalse((self.home / "preferences.json").exists())

    def test_a_value_outside_the_allowlist_is_never_inherited(self):
        # backend_profile decides which server the session talks to and
        # whether Laintas credentials are stripped; a new terminal must start
        # on the official backend, not inherit an external one.
        self._choose(backend_profile="local", model="ok")
        with self._as_terminal("term-fresh"):
            seeded = prefs.seed_new_terminal()
        self.assertNotIn("backend_profile", seeded)
        self.assertEqual("ok", seeded.get("model"))

    def test_ui_preferences_are_inherited_too(self):
        prefs.set_ui_preference("theme", "light")
        with self._as_terminal("term-fresh"):
            prefs.seed_new_terminal()
            self.assertEqual("light", prefs.get_ui_preferences().get("theme"))


class BootstrapTests(_PrefsCase):
    def test_the_default_layer_bootstraps_from_existing_files(self):
        """Upgrading must not still ask for every setting again, with the
        answers sitting in a file two directories away."""
        self._write("term-old", {"model": "remembered", "mode": "act"},
                    mtime=1000)
        with self._as_terminal("term-brand-new"):
            prefs.seed_new_terminal()
            self.assertEqual("remembered", prefs.get("model"))

    def test_bootstrapping_prefers_the_newest_file(self):
        self._write("term-older", {"model": "stale"}, mtime=1000)
        self._write("term-newer", {"model": "current"}, mtime=2000)
        with self._as_terminal("term-brand-new"):
            prefs.seed_new_terminal()
            self.assertEqual("current", prefs.get("model"))

    def test_an_empty_file_is_not_treated_as_a_choice(self):
        self._write("term-empty", {}, mtime=3000)
        self._write("term-real", {"model": "real"}, mtime=2000)
        with self._as_terminal("term-brand-new"):
            prefs.seed_new_terminal()
            self.assertEqual("real", prefs.get("model"))

    def test_no_files_at_all_is_not_an_error(self):
        with self._as_terminal("term-brand-new"):
            self.assertEqual({}, prefs.seed_new_terminal())
            self.assertIsNone(prefs.get("model"))

    def test_a_corrupt_file_is_skipped_not_fatal(self):
        bad = self.sessions / "term-bad_preferences.json"
        bad.write_text("{not json", encoding="utf-8")
        bad.chmod(0o600)
        self._write("term-good", {"model": "good"}, mtime=1000)
        with self._as_terminal("term-brand-new"):
            prefs.seed_new_terminal()
            self.assertEqual("good", prefs.get("model"))


class CurrentAgentTests(_PrefsCase):
    def setUp(self):
        super().setUp()
        self._saved = dict(agent_loop._agent_registry)
        self._saved_current = agent_loop._current_agent_id
        self._saved_previous = agent_loop._previous_agent_id
        agent_loop._agent_registry.clear()
        for index, agent_id in enumerate(("primary", "scout")):
            agent_loop._agent_registry[agent_id] = agent_loop.AgentInfo(
                id=agent_id, name=agent_id, index=index)
        agent_loop._current_agent_id = "primary"
        agent_loop._previous_agent_id = None

    def tearDown(self):
        agent_loop._agent_registry.clear()
        agent_loop._agent_registry.update(self._saved)
        agent_loop._current_agent_id = self._saved_current
        agent_loop._previous_agent_id = self._saved_previous
        super().tearDown()

    def test_switching_is_remembered_for_the_next_launch(self):
        # Being dropped back on the primary every launch is a setting the
        # user has to make again every time.
        self.assertTrue(agent_loop.switch_to_agent("scout"))
        self.assertEqual("scout", prefs.get("agent"))

    def test_switching_records_the_previous_agent(self):
        """switch_to_agent is the path /agent and Alt+A actually take; setting
        the pointer without recording the previous one left the toggle-back
        permanently empty."""
        agent_loop.switch_to_agent("scout")
        self.assertEqual("primary", agent_loop.previous_agent_id())

    def test_switching_to_something_unknown_changes_nothing(self):
        self.assertFalse(agent_loop.switch_to_agent("ghost"))
        self.assertEqual("primary", agent_loop._current_agent_id)
        self.assertIsNone(prefs.get("agent"))

    def test_the_remembered_agent_is_reselected(self):
        prefs.set_value("agent", "scout")
        self.assertEqual("scout", agent_loop.restore_current_agent("primary"))
        self.assertEqual("scout", agent_loop._current_agent_id)

    def test_an_agent_that_no_longer_exists_falls_back(self):
        prefs.set_value("agent", "fired")
        self.assertEqual("primary", agent_loop.restore_current_agent("primary"))
        self.assertEqual("primary", agent_loop._current_agent_id)

    def test_nothing_remembered_keeps_the_default(self):
        self.assertEqual("primary", agent_loop.restore_current_agent("primary"))

    def test_a_broken_preference_store_does_not_break_the_switch(self):
        with mock.patch.object(prefs, "set_value",
                               side_effect=OSError("read-only")):
            self.assertTrue(agent_loop.switch_to_agent("scout"))
        self.assertEqual("scout", agent_loop._current_agent_id)


if __name__ == "__main__":
    unittest.main()
