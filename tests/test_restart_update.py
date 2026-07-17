import io
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import laintas_cli
import updater


class RestartResolutionTests(unittest.TestCase):
    def test_path_launch_resolves_executable_instead_of_cwd_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "laintas-cli"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.object(laintas_cli.sys, "frozen", True, create=True), \
                    mock.patch.object(laintas_cli.sys, "executable", "/missing/bootloader"), \
                    mock.patch.object(laintas_cli, "_LAUNCH_ARGV0", "laintas-cli"), \
                    mock.patch.object(laintas_cli, "_LAUNCH_CWD", "/unrelated/cwd"), \
                    mock.patch.object(
                        laintas_cli.shutil, "which", return_value=str(executable)):
                resolved = laintas_cli._resolve_launch_executable()
            self.assertEqual(resolved, str(executable.resolve()))

    def test_source_restart_uses_python_and_absolute_module_path(self):
        with mock.patch.object(laintas_cli.sys, "frozen", False, create=True), \
                mock.patch.object(
                    laintas_cli.sys, "argv", ["laintas-cli", "--resume"]), \
                mock.patch.object(laintas_cli.os, "execv") as execv:
            laintas_cli._restart_process()

        interpreter = os.path.realpath(os.path.abspath(laintas_cli.sys.executable))
        execv.assert_called_once_with(interpreter, [
            interpreter, os.path.realpath(laintas_cli._LAUNCH_SCRIPT_PATH),
            "--resume",
        ])

    def test_frozen_restart_uses_replaced_absolute_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "laintas-cli"
            executable.write_text("new", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.object(
                    laintas_cli.sys, "argv", ["laintas-cli", "--resume"]), \
                    mock.patch.object(laintas_cli.os, "execv") as execv:
                laintas_cli._restart_process(str(executable))
            target = str(executable.resolve())
            execv.assert_called_once_with(target, [target, "--resume"])


class FrozenUpdateTests(unittest.TestCase):
    @staticmethod
    def _archive(binary: bytes) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo("laintas-cli")
            info.mode = 0o755
            info.size = len(binary)
            archive.addfile(info, io.BytesIO(binary))
        return output.getvalue()

    def test_binary_replacement_is_atomic_and_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "laintas-cli"
            target.write_bytes(b"old-binary")
            target.chmod(0o751)
            archive = self._archive(b"new-binary")
            checksum = updater._sha256_bytes(archive)
            sums = f"{checksum}  laintas-cli_linux_amd64.tar.gz\n".encode()
            real_replace = os.replace
            observed_old_target = []

            def checked_replace(source, destination):
                observed_old_target.append(Path(destination).read_bytes())
                real_replace(source, destination)

            with mock.patch.object(updater.sys, "executable", str(target)), \
                    mock.patch.object(
                        updater.os, "uname",
                        return_value=SimpleNamespace(machine="x86_64")), \
                    mock.patch.object(
                        updater, "_download",
                        side_effect=[sums, archive]), \
                    mock.patch.object(
                        updater.os, "replace", side_effect=checked_replace):
                installed = updater.apply_frozen_update(
                    {"version": "9.9.9"}, "latest", lambda _message: None)

            self.assertEqual(installed, str(target))
            self.assertEqual(observed_old_target, [b"old-binary"])
            self.assertEqual(target.read_bytes(), b"new-binary")
            self.assertTrue(target.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(list(Path(tmp).glob(".laintas-cli-update-*")), [])

    def test_binary_replacement_rejects_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "laintas-cli"
            target.write_bytes(b"old-binary")
            target.chmod(0o755)
            archive = self._archive(b"corrupted-binary")
            sums = ("0" * 64 + "  laintas-cli_linux_amd64.tar.gz\n").encode()
            messages = []

            with mock.patch.object(updater.sys, "executable", str(target)), \
                    mock.patch.object(
                        updater.os, "uname",
                        return_value=SimpleNamespace(machine="x86_64")), \
                    mock.patch.object(
                        updater, "_download", side_effect=[sums, archive]):
                installed = updater.apply_frozen_update(
                    {"version": "9.9.9"}, "latest", messages.append)

            self.assertIsNone(installed)
            self.assertEqual(target.read_bytes(), b"old-binary")
            self.assertTrue(any("Checksum mismatch" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
