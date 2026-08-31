"""Surviving the deletion of the directory the session is sitting in.

Deleting a working directory out from under a long-running process is
ordinary — /tmp cleanup, `git worktree remove`, a build tree the agent itself
removes. On Linux the process is then holding an unlinked inode: os.getcwd()
raises ENOENT and every path call after it fails, which ended the REPL with a
traceback naming getcwd rather than the deleted directory.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import laintas_cli   # imported before any test deletes a directory
import paths


class _CwdCase(unittest.TestCase):
    """Runs each test from a directory it is free to delete."""

    def setUp(self):
        self._origin = os.getcwd()
        self._root = tempfile.mkdtemp(prefix="cwd-test-")
        self.deep = os.path.join(self._root, "a", "b", "c")
        os.makedirs(self.deep)
        os.chdir(self.deep)
        paths._last_live_cwd = self.deep

    def tearDown(self):
        os.chdir(self._origin)
        shutil.rmtree(self._root, ignore_errors=True)
        paths._last_live_cwd = self._origin


class EnsureLiveCwdTests(_CwdCase):
    def test_a_live_directory_is_reported_unchanged(self):
        cwd, left = paths.ensure_live_cwd()
        self.assertEqual(os.path.realpath(self.deep), os.path.realpath(cwd))
        self.assertEqual("", left)

    def test_a_deleted_directory_lands_on_the_nearest_survivor(self):
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        cwd, left = paths.ensure_live_cwd()
        self.assertEqual(self.deep, left)
        self.assertEqual(os.path.realpath(os.path.join(self._root, "a")),
                         os.path.realpath(cwd))
        # And the process really moved — not just the return value.
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(os.getcwd()))

    def test_a_recreated_directory_is_preferred_over_climbing(self):
        # `rm -rf build && mkdir build` is one action to a person; landing
        # them two levels up afterwards is not a recovery they asked for.
        shutil.rmtree(os.path.join(self._root, "a"))
        os.makedirs(self.deep)
        cwd, left = paths.ensure_live_cwd()
        self.assertEqual(self.deep, left)
        self.assertEqual(os.path.realpath(self.deep), os.path.realpath(cwd))

    def test_ancestors_are_preferred_over_an_explicit_fallback(self):
        # Landing beside where you were beats landing somewhere unrelated,
        # even when the caller offered a destination.
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        cwd, _ = paths.ensure_live_cwd(fallback=tempfile.gettempdir())
        self.assertEqual(os.path.realpath(os.path.join(self._root, "a")),
                         os.path.realpath(cwd))

    def test_candidate_order_is_self_then_ancestors_then_fallback_then_home(self):
        with mock.patch.object(Path, "home", return_value=Path("/home/x")):
            order = paths._cwd_candidates("/w/a/b", "/spare")
        self.assertEqual(
            ["/w/a/b", "/w/a", "/w", "/", "/spare", "/home/x"], order)

    def test_a_blank_fallback_is_not_offered_as_a_destination(self):
        with mock.patch.object(Path, "home", return_value=Path("/home/x")):
            order = paths._cwd_candidates("/w", "   ")
        self.assertEqual(["/w", "/", "/home/x"], order)

    def test_it_never_raises_even_with_nothing_to_return_to(self):
        shutil.rmtree(self._root)
        with mock.patch.object(paths.os, "chdir", side_effect=OSError(2, "no")):
            cwd, left = paths.ensure_live_cwd()
        self.assertEqual("", cwd)
        self.assertEqual(self.deep, left)

    def test_live_cwd_gives_callers_a_path_they_can_use(self):
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        self.assertTrue(os.path.isdir(paths.live_cwd()))

    def test_repeated_recovery_is_stable(self):
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        first, _ = paths.ensure_live_cwd()
        second, left = paths.ensure_live_cwd()
        self.assertEqual(first, second)
        self.assertEqual("", left)      # nothing wrong the second time


class ReplRecoveryTests(_CwdCase):
    """The REPL helper: move, tell the user, and bring the shell along."""

    def _recover(self, session=None):
        printed = []
        info = mock.Mock()
        info.session = session
        with mock.patch.object(laintas_cli.console, "print",
                               side_effect=lambda *a, **k: printed.append(
                                   " ".join(str(x) for x in a))), \
                mock.patch.object(laintas_cli, "get_terminal",
                                  return_value=info):
            cwd = laintas_cli._recover_deleted_cwd()
        return cwd, printed

    def test_a_live_directory_is_silent(self):
        cwd, printed = self._recover()
        self.assertEqual(os.path.realpath(self.deep), os.path.realpath(cwd))
        self.assertEqual([], printed)

    def test_the_move_is_announced_with_both_directories(self):
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        cwd, printed = self._recover()
        self.assertTrue(printed)
        self.assertIn(self.deep, printed[0])
        self.assertIn(cwd, printed[0])

    def test_the_shell_is_moved_too(self):
        """The prompt and term0's bash disagreeing about the directory would
        be worse than the crash this replaces."""
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        session = mock.Mock()
        session.is_alive.return_value = True
        cwd, _ = self._recover(session=session)
        session.send_keys.assert_called_once()
        self.assertIn(cwd, session.send_keys.call_args[0][0])
        self.assertEqual(cwd, session._laintas_last_cwd)

    def test_a_dead_shell_is_not_written_to(self):
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        session = mock.Mock()
        session.is_alive.return_value = False
        self._recover(session=session)
        session.send_keys.assert_not_called()

    def test_a_broken_shell_does_not_break_the_recovery(self):
        shutil.rmtree(os.path.join(self._root, "a", "b"))
        session = mock.Mock()
        session.is_alive.return_value = True
        session.send_keys.side_effect = OSError("pty gone")
        cwd, printed = self._recover(session=session)
        self.assertTrue(os.path.isdir(cwd))
        self.assertTrue(printed)


class AgentLoopRecoveryTests(_CwdCase):
    """A task whose own working directory disappears mid-run."""

    def test_the_run_continues_and_the_model_is_told(self):
        import agent_loop

        class _Console:
            width = 100

            def print(self, *a, **k):
                pass

        gone = self.deep
        removed = {"done": False}

        def backend(**kw):
            if not removed["done"]:
                removed["done"] = True
                shutil.rmtree(os.path.join(self._root, "a", "b"))
                # A tool call, so the loop takes another turn: a plain reply
                # ends the turn and the recovery would never be reached.
                return {"reply": "继续",
                        "tool_calls": [{"id": "c1", "name": "ls",
                                        "arguments": {"path": "."}}],
                        "finish_reason": "tool_calls", "done": False,
                        "error": False}
            return {"reply": "完成", "tool_calls": [], "finish_reason": "stop",
                    "done": True, "error": False}

        deps = mock.Mock()
        deps.call_backend = backend
        deps.console = _Console()
        deps.read_file = lambda p: ""
        deps.generate_prompt = lambda: "prompt"
        Path(self.deep).mkdir(parents=True, exist_ok=True)
        (Path(self.deep) / ".laintas").mkdir(exist_ok=True)
        os.chdir(self.deep)
        paths._last_live_cwd = self.deep

        result = agent_loop.run_agent_loop(
            deps, "go", {}, {"cwd": gone},
            [{"role": "user", "content": "go"}], max_loops_override=3)

        state = result["state"]
        self.assertTrue(os.path.isdir(state["cwd"]))
        self.assertNotEqual(gone, state["cwd"])
        self.assertIn("no longer exists", state.get("shortTermMemory", ""))


if __name__ == "__main__":
    unittest.main()
