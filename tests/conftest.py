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
    with tempfile.TemporaryDirectory(prefix="laintas-test-sessions-") as tmp:
        real = paths.SESSIONS_DIR
        paths.SESSIONS_DIR = paths.Path(tmp)
        terminal_preferences.reset_cache()
        try:
            yield
        finally:
            paths.SESSIONS_DIR = real
            terminal_preferences.reset_cache()
