"""Focused tests for launch-directory loss detection in the CLI watchdog."""

import errno
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import laintas_cli


class StartupCwdIdentityTests(unittest.TestCase):
    def _capture(self, path: Path):
        identity = laintas_cli._StartupCwdIdentity.capture(str(path))
        self.assertIsNotNone(identity)
        self.addCleanup(identity.close)
        return identity

    def test_deleted_startup_cwd_is_lost(self):
        with tempfile.TemporaryDirectory() as parent_raw:
            startup = Path(parent_raw) / "launch"
            startup.mkdir()
            identity = self._capture(startup)

            shutil.rmtree(startup)

            self.assertTrue(identity.is_lost())

    def test_unlinked_process_cwd_is_lost_while_fd_remains_open(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as parent_raw:
            startup = Path(parent_raw) / "launch"
            startup.mkdir()
            try:
                os.chdir(startup)
                identity = self._capture(startup)
                startup.rmdir()
                self.assertTrue(identity.is_lost())
            finally:
                os.chdir(original)

    def test_replaced_startup_cwd_is_lost(self):
        with tempfile.TemporaryDirectory() as parent_raw:
            parent = Path(parent_raw)
            startup = parent / "launch"
            displaced = parent / "launch-old"
            startup.mkdir()
            identity = self._capture(startup)

            startup.rename(displaced)
            startup.mkdir()

            self.assertTrue(identity.is_lost())

    def test_ordinary_chdir_does_not_lose_startup_cwd(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as parent_raw:
            parent = Path(parent_raw)
            startup = parent / "launch"
            elsewhere = parent / "elsewhere"
            startup.mkdir()
            elsewhere.mkdir()
            identity = self._capture(startup)
            try:
                os.chdir(elsewhere)
                self.assertFalse(identity.is_lost())
            finally:
                os.chdir(original)

    def test_permission_probe_failure_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as startup_raw:
            identity = self._capture(Path(startup_raw))
            denied = PermissionError(errno.EACCES, "denied", identity.path)
            with mock.patch.object(laintas_cli.os, "stat", side_effect=denied):
                self.assertFalse(identity.is_lost())

    def test_headless_watchdog_still_observes_startup_cwd(self):
        with tempfile.TemporaryDirectory() as parent_raw:
            startup = Path(parent_raw) / "launch"
            startup.mkdir()
            identity = self._capture(startup)
            stopped = threading.Event()
            hard_exit = threading.Event()

            with mock.patch.object(laintas_cli.sys.stdin, "fileno", return_value=0), \
                 mock.patch.object(laintas_cli.os, "isatty", return_value=False), \
                 mock.patch.object(laintas_cli.os, "_exit",
                                   side_effect=lambda _code: hard_exit.set()):
                laintas_cli._install_terminal_watchdog(
                    lambda **_kwargs: stopped.set(), startup_cwd=identity,
                    interval=0.01, grace=0.05)

                shutil.rmtree(startup)
                self.assertTrue(stopped.wait(1.0))
                self.assertTrue(hard_exit.wait(1.0))


if __name__ == "__main__":
    unittest.main()
