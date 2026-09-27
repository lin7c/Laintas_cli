"""Platform boundaries required before publishing a native macOS CLI."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import updater
import worktree_manager
import laintas_cli


def test_mac_live_worktree_owner_is_not_reaped_without_proc():
    owner = {"pid": 8123, "boot_id": "", "starttime": ""}
    with mock.patch.object(worktree_manager.sys, "platform", "darwin"), \
            mock.patch.object(worktree_manager, "_pid_alive", return_value=True):
        assert worktree_manager._owner_alive(owner)


def test_mac_cli_does_not_start_helpwo_kernel():
    with mock.patch.object(laintas_cli.sys, "platform", "darwin"), \
            mock.patch("windows_host.start_host") as start_host:
        assert laintas_cli.AgentRegistry._kernel_link(wait=1) is None
        start_host.assert_not_called()


def test_beta_sorts_between_previous_and_final_versions():
    assert updater.is_newer("1.32.5-beta.1", "1.32.4")
    assert updater.is_newer("1.32.5", "1.32.5-beta.1")
    assert not updater.is_newer("1.32.5-beta.1", "1.32.5")


def test_mac_frozen_update_requests_mac_archive():
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "laintas-cli"
        target.write_bytes(b"old")
        requested = []

        def download(url, **_kwargs):
            requested.append(url)
            if url.endswith("SHA256SUMS.txt"):
                return (b"0" * 64
                        + b"  laintas-cli_darwin_arm64.tar.gz\n")
            raise RuntimeError("stop after asset selection")

        with mock.patch.object(updater.sys, "platform", "darwin"), \
                mock.patch.object(updater.sys, "executable", str(target)), \
                mock.patch.object(updater.os, "uname",
                                  return_value=SimpleNamespace(machine="arm64")), \
                mock.patch.object(updater, "_download", side_effect=download):
            assert updater.apply_frozen_update(
                {"version": "1.32.5-beta.1"}, "v1.32.5-beta.1",
                lambda _message: None) is None

        assert requested[-1].endswith("laintas-cli_darwin_arm64.tar.gz")
        assert target.read_bytes() == b"old"


def test_mac_updates_use_site_mirror_and_custom_tag():
    with mock.patch.object(updater.sys, "platform", "darwin"), \
            mock.patch.dict(updater.os.environ, {}, clear=True):
        assert updater._asset_url("latest", "manifest.json") == (
            "https://cli.laintas.com/releases/latest/manifest.json")
        assert updater._asset_url("laintas-cli-beta-v1", "manifest.json") == (
            "https://cli.laintas.com/releases/laintas-cli-beta-v1/manifest.json")
