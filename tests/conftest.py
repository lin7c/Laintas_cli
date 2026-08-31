"""Keep the suite out of the developer's real ``~/.laintas``.

Several tests exercise code paths that legitimately save a preference —
switching agent, choosing a model, activating a mode. Pointed at the real
store they leave files behind under the current terminal's id, and since a
terminal with no preferences of its own is seeded from the most recently
written file (terminal_preferences.seed_new_terminal), a stray test value
becomes what the next real session starts with. Observed: a run left
``{"agent": "Alice", "model": "model-x"}`` in the live store.

Redirecting the sessions directory for the whole run fixes that once, instead
of asking every future test to remember. Tests that patch it themselves still
win — this only moves the default.
"""
import tempfile

import pytest

import paths
import terminal_preferences


@pytest.fixture(autouse=True, scope="session")
def _isolated_preference_store():
    """Redirect only the preference file, not SESSIONS_DIR.

    Moving the whole directory reaches further than intended — other subsystems
    keep run state there, and pointing them at an empty directory changed
    behaviour that has nothing to do with preferences (it reordered the HWG
    ready queue). The file is what needs protecting, so the file is what moves.
    """
    with tempfile.TemporaryDirectory(prefix="laintas-test-prefs-") as tmp:
        real = terminal_preferences.preference_path
        directory = paths.Path(tmp)
        real_sessions = paths.SESSIONS_DIR

        def _isolated_path():
            # A test that redirected SESSIONS_DIR itself is already isolated
            # and means something by it; only the default is moved.
            base = (directory if paths.SESSIONS_DIR == real_sessions
                    else paths.SESSIONS_DIR)
            terminal_id = getattr(paths, "TERMINAL_ID", "terminal-default")
            return base / f"{terminal_id}_preferences.json"

        terminal_preferences.preference_path = _isolated_path
        terminal_preferences.reset_cache()
        try:
            yield
        finally:
            terminal_preferences.preference_path = real
            terminal_preferences.reset_cache()
