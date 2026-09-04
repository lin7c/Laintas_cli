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


def _make_response(payload):
    class Response:
        headers = {"Content-Length": str(len(payload))}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def iter_content(chunk_size=65536):
            return (payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size))

    return Response()


class DownloadProgressTests(unittest.TestCase):
    def test_tty_path_writes_single_line_progress_to_fd(self):
        """When /dev/tty is available, progress goes to the tty fd via \r,
        not to con.print. Verify output content and byte integrity."""
        payload = b"a" * 70000 + b"b" * 70000
        StdoutProxy = type("StdoutProxy", (), {"__module__": "prompt_toolkit.patch_stdout"})
        messages = []
        console = SimpleNamespace(
            is_terminal=True,
            file=StdoutProxy(),
            print=messages.append,
        )
        r_fd, w_fd = os.pipe()
        try:
            with mock.patch.object(updater.requests, "get", return_value=_make_response(payload)), \
                    mock.patch.object(updater, "_open_tty", return_value=(w_fd, 120)):
                downloaded = updater._download(
                    "https://example.invalid/release.tar.gz",
                    label="release.tar.gz",
                    console=console,
                )
            # _download closes w_fd via os.close(tty_fd), so it's already closed.
            # Read what was written to the pipe.
            output = os.read(r_fd, 65536).decode("utf-8", errors="replace")
        finally:
            try:
                os.close(r_fd)
            except OSError:
                pass
        # Should contain the label, 100%, and a final newline
        self.assertIn("release.tar.gz", output)
        self.assertIn("100%", output)
        self.assertTrue(output.endswith("\n"))

    def test_no_tty_fallback_uses_throttled_console_print(self):
        """When /dev/tty is unavailable, fall back to throttled con.print."""
        payload = b"a" * 70000 + b"b" * 70000
        StdoutProxy = type("StdoutProxy", (), {"__module__": "prompt_toolkit.patch_stdout"})
        messages = []
        console = SimpleNamespace(
            is_terminal=True,
            file=StdoutProxy(),
            print=messages.append,
        )
        with mock.patch.object(updater.requests, "get", return_value=_make_response(payload)), \
                mock.patch.object(updater, "_open_tty", return_value=(None, 0)):
            downloaded = updater._download(
                "https://example.invalid/release.tar.gz",
                label="release.tar.gz",
                console=console,
            )

        self.assertEqual(downloaded, payload)
        self.assertGreaterEqual(len(messages), 2)
        self.assertIn("release.tar.gz", messages[0])
        self.assertTrue(any("100%" in m for m in messages))


class RestartResolutionTests(unittest.TestCase):
    POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    LOGIN_URL = "https://accounts.laintas.com/login?state=a&code=b"

    def _run_wsl_open(self, returncode=0):
        """Call the opener as it runs inside the private distribution."""
        with mock.patch.dict(
                laintas_cli.os.environ,
                {"WSL_DISTRO_NAME": "Laintas-CLI", "WSLENV": "KEEP/p"},
                clear=False), \
                mock.patch.object(
                    laintas_cli.shutil, "which", return_value=self.POWERSHELL), \
                mock.patch.object(
                    laintas_cli.subprocess, "run",
                    return_value=SimpleNamespace(returncode=returncode)) as run, \
                mock.patch.object(laintas_cli.webbrowser, "open") as browser:
            opened = laintas_cli._open_external_url(self.LOGIN_URL)
        return opened, run, browser

    def test_wsl_browser_open_uses_windows_default_url_handler(self):
        opened, run, browser = self._run_wsl_open()

        self.assertTrue(opened)
        browser.assert_not_called()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], [
            self.POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command"])
        self.assertEqual(len(argv), 6, "nothing may follow the -Command text")

    def test_wsl_browser_open_never_puts_the_url_on_the_command_line(self):
        """`-Command <text>` swallows everything after it.

        v1.23.1 passed the URL as a trailing argument expecting $args[0] to
        pick it up. PowerShell appends it to the command text instead, so
        Start-Process got a null path, nothing opened, and — because the exit
        code was ignored — the CLI reported success. Verified against
        PowerShell: `-Command 'Write-Output $args.Count' x` prints 0.
        """
        opened, run, _ = self._run_wsl_open()

        argv = run.call_args.args[0]
        command = argv[5]
        self.assertNotIn("$args", command)
        self.assertNotIn(self.LOGIN_URL, argv,
                         "the URL must not travel on the command line")
        self.assertIn("$env:LAINTAS_OPEN_URL", command)
        # Windows PowerShell 5.1 exits 0 after a cmdlet error unless told not
        # to, which is what let the failure read as success.
        self.assertIn("exit 1", command)

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["LAINTAS_OPEN_URL"], self.LOGIN_URL)
        # A Win32 process sees only what WSLENV names, and an existing entry
        # belongs to something else that still needs it.
        self.assertEqual(environment["WSLENV"], "KEEP/p:LAINTAS_OPEN_URL/w")
        self.assertTrue(opened)

    def test_wsl_browser_open_falls_back_when_windows_opened_nothing(self):
        """A failed handler must not be reported as an opened browser."""
        opened, _, browser = self._run_wsl_open(returncode=1)

        browser.assert_called_once_with(self.LOGIN_URL)
        self.assertIs(opened, bool(browser.return_value))

    def test_private_windows_runtime_enables_mouse_by_default(self):
        with mock.patch.dict(
                laintas_cli.os.environ,
                {"WSL_DISTRO_NAME": "Laintas-CLI"}, clear=False):
            preferences = laintas_cli._initial_ui_preferences_for_host({})
        self.assertIs(preferences["enable_mouse"], True)

    def test_private_windows_runtime_respects_explicit_mouse_choice(self):
        with mock.patch.dict(
                laintas_cli.os.environ,
                {"WSL_DISTRO_NAME": "Laintas-CLI"}, clear=False):
            preferences = laintas_cli._initial_ui_preferences_for_host(
                {"enable_mouse": False})
        self.assertIs(preferences["enable_mouse"], False)

    def test_launcher_marker_enables_mouse_whatever_the_distro_is_called(self):
        """The launcher's own signal, not the distribution's name.

        LAINTAS_WSL_DISTRO renames the distribution, so a name check loses the
        mouse for anyone who used it — with nothing on screen connecting the
        two facts.
        """
        with mock.patch.dict(
                laintas_cli.os.environ,
                {"LAINTAS_HOST": "windows",
                 "WSL_DISTRO_NAME": "Laintas-Work"}, clear=False):
            preferences = laintas_cli._initial_ui_preferences_for_host({})
        self.assertIs(preferences["enable_mouse"], True)

    def test_an_ordinary_linux_terminal_leaves_the_mouse_off(self):
        environment = {k: v for k, v in laintas_cli.os.environ.items()
                       if k not in ("LAINTAS_HOST", "WSL_DISTRO_NAME")}
        with mock.patch.dict(laintas_cli.os.environ, environment, clear=True):
            preferences = laintas_cli._initial_ui_preferences_for_host({})
        self.assertNotIn("enable_mouse", preferences)

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

    def test_unwritable_install_dir_replaces_through_sudo(self):
        """The Windows distribution's layout: root-owned dir, user session.

        /usr/local/bin belongs to root because the Windows installer writes it
        as root, while the distribution logs in as `laintas`. Without the sudo
        path every `/v update` there aborted after downloading the whole
        binary.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "laintas-cli"
            target.write_bytes(b"old-binary")
            target.chmod(0o755)
            archive = self._archive(b"new-binary")
            checksum = updater._sha256_bytes(archive)
            sums = f"{checksum}  laintas-cli_linux_amd64.tar.gz\n".encode()
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if command[2:] == ["true"]:
                    return SimpleNamespace(returncode=0, stdout=b"")
                # Stand in for what root's `install` would do.
                Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
                return SimpleNamespace(returncode=0, stdout=b"")

            with mock.patch.object(updater.sys, "executable", str(target)), \
                    mock.patch.object(
                        updater.os, "uname",
                        return_value=SimpleNamespace(machine="x86_64")), \
                    mock.patch.object(
                        updater.os, "access", return_value=False), \
                    mock.patch.object(
                        updater.shutil, "which",
                        side_effect=lambda name: "/usr/bin/" + name), \
                    mock.patch.object(
                        updater.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(
                        updater, "_download", side_effect=[sums, archive]):
                installed = updater.apply_frozen_update(
                    {"version": "9.9.9"}, "latest", lambda _message: None)

            self.assertEqual(installed, str(target))
            self.assertEqual(target.read_bytes(), b"new-binary")
            self.assertEqual(calls[0], ["/usr/bin/sudo", "-n", "true"])
            self.assertEqual(calls[1][:4],
                             ["/usr/bin/sudo", "-n", "/usr/bin/install", "-m"])

    def test_unwritable_install_dir_without_sudo_never_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "laintas-cli"
            target.write_bytes(b"old-binary")
            messages = []

            with mock.patch.object(updater.sys, "executable", str(target)), \
                    mock.patch.object(
                        updater.os, "access", return_value=False), \
                    mock.patch.object(
                        updater.shutil, "which", return_value=None), \
                    mock.patch.dict(
                        updater.os.environ,
                        {"LAINTAS_HOST": "windows"}, clear=False), \
                    mock.patch.object(
                        updater, "_download",
                        side_effect=AssertionError(
                            "downloaded before checking write access")):
                installed = updater.apply_frozen_update(
                    {"version": "9.9.9"}, "latest", messages.append)

            self.assertIsNone(installed)
            self.assertEqual(target.read_bytes(), b"old-binary")
            self.assertTrue(any("No write permission" in m for m in messages))
            # Telling a Windows user to "re-run with sudo" points at a command
            # line the launcher never gives them.
            self.assertTrue(any("Windows installer" in m for m in messages))
            self.assertFalse(any("sudo" in m for m in messages))

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


class UpdateChannelTests(unittest.TestCase):
    """Where `/v` fetches from.

    The site's own /releases/ path stopped being populated when releasing
    moved into CI, so every update — and every download link on the page —
    resolved to a 404 that only showed up at the moment a user ran `/v`.
    """

    def _url(self, channel, asset, base=None):
        environment = {} if base is None else {"LAINTAS_DOWNLOAD_BASE": base}
        with mock.patch.dict(updater.os.environ, environment, clear=False):
            if base is None:
                updater.os.environ.pop("LAINTAS_DOWNLOAD_BASE", None)
            return updater._asset_url(channel, asset)

    def test_latest_uses_the_github_rolling_pointer(self):
        self.assertEqual(
            self._url("latest", "manifest.json"),
            "https://github.com/lin7c/Laintas_cli/releases/latest/download/"
            "manifest.json")

    def test_a_pinned_version_uses_the_tag_form(self):
        """GitHub spells a pinned tag differently from `latest`.

        `/releases/<tag>/<asset>` is not a URL GitHub serves; getting this
        wrong 404s only for users who pinned a channel.
        """
        expected = ("https://github.com/lin7c/Laintas_cli/releases/download/"
                    "v1.23.2/laintas-cli_linux_amd64.tar.gz")
        self.assertEqual(
            self._url("v1.23.2", "laintas-cli_linux_amd64.tar.gz"), expected)
        self.assertEqual(
            self._url("1.23.2", "laintas-cli_linux_amd64.tar.gz"), expected)

    def test_an_overridden_base_keeps_the_flat_mirror_layout(self):
        """A static directory has to remain usable as a mirror for testing."""
        self.assertEqual(
            self._url("latest", "manifest.json", base="http://127.0.0.1:8000"),
            "http://127.0.0.1:8000/releases/latest/manifest.json")
        self.assertEqual(
            self._url("v1.2.3", "manifest.json", base="http://127.0.0.1:8000/"),
            "http://127.0.0.1:8000/releases/v1.2.3/manifest.json")

    def test_download_page_links_at_the_same_channel(self):
        """The page's buttons and `/v` must not drift apart again."""
        page = (Path(__file__).resolve().parents[1]
                / "laintas_cli_download/src/components/DownloadSection.jsx"
                ).read_text(encoding="utf-8")
        self.assertIn(
            "https://github.com/lin7c/Laintas_cli/releases/latest/download",
            page)
        self.assertNotIn("cli.laintas.com/releases/", page)
        # The two files the page links directly; Linux goes through the
        # installer script, which picks its own architecture.
        for asset in ("laintas-cli_windows_amd64_setup.exe",
                      "laintas-cli_source.zip"):
            self.assertIn(asset, page)
