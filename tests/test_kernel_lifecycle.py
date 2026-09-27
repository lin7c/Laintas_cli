"""Kernel lifecycle with fake Windows processes; no real programs installed."""
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

import windows_host
import windows_kernel as kernel
import winbridge


@pytest.fixture
def machine(tmp_path, monkeypatch):
    folder = tmp_path / "installed"
    folder.mkdir()
    exe = folder / kernel.KERNEL_EXE
    exe.write_bytes(b"old")
    temp = tmp_path / "temp"
    temp.mkdir()
    state = SimpleNamespace(version="1.0.0", running=True, tier="read", events=[])
    monkeypatch.setattr(winbridge, "in_wsl", lambda: True)
    monkeypatch.setattr(winbridge, "windows_temp", lambda: temp)
    monkeypatch.setattr(winbridge, "to_windows_path", lambda p: "C:\\Temp\\" + p.name)
    monkeypatch.setattr(kernel, "install_dir", lambda: folder)
    monkeypatch.setattr(kernel, "kernel_exe", lambda: exe if exe.exists() else None)
    monkeypatch.setattr(kernel, "installed_version", lambda: state.version)
    monkeypatch.setattr(kernel, "latest", lambda: kernel.Release("2.0.0", "setup.exe", "0" * 64))
    monkeypatch.setattr(kernel, "running", lambda: state.running)
    monkeypatch.setattr(kernel, "_connected_tier", lambda: state.tier)
    monkeypatch.setattr(windows_host, "start_host", lambda: object())
    monkeypatch.setattr(windows_host, "stop_host", lambda: None)

    def download(release, into, progress):
        state.events.append("download")
        target = into / release.asset
        target.write_bytes(b"verified")
        return target

    def stop():
        state.events.append("stop")
        state.running = False
        return True

    def start(tier):
        state.events.append(("start", tier))
        state.running = True
        return {"tier": tier}

    def install(*args):
        state.events.append("install")
        state.version = "2.0.0"
        exe.write_bytes(b"new")
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(kernel, "_download", download)
    monkeypatch.setattr(kernel, "stop", stop)
    monkeypatch.setattr(kernel, "start", start)
    monkeypatch.setattr(kernel, "_run_installer", install)
    state.exe, state.temp, state.folder = exe, temp, folder
    return state


@pytest.mark.parametrize("version", ["2.0.0", "3.0.0"])
def test_current_or_newer_install_is_not_downloaded_or_stopped(machine, version):
    machine.version = version
    assert kernel.update()["action"] == "kept"
    assert machine.events == []


@pytest.mark.parametrize("tier", ["workspace", "read", "write"])
def test_update_preserves_connected_tier_after_verification(machine, tier):
    machine.tier = tier
    result = kernel.update()
    assert machine.events == ["download", "stop", "install", ("start", tier)]
    assert result["restarted"] and not result["restartNeeded"]
    assert list(machine.temp.iterdir()) == []


def test_install_does_not_start_or_grant_access(machine):
    result = kernel.install()
    assert machine.events == ["download", "stop", "install"]
    assert result["restartNeeded"]


def test_unconnected_update_never_guesses_machine_access(machine):
    machine.tier = None
    result = kernel.update()
    assert machine.events == ["download", "stop", "install"]
    assert result["restartNeeded"]


def test_stopped_kernel_stays_stopped_after_update(machine):
    machine.running = False
    result = kernel.update()
    assert machine.events == ["download", "install"]
    assert not result["restartNeeded"]


def test_failed_download_leaves_running_kernel_untouched(machine, monkeypatch):
    monkeypatch.setattr(kernel, "_download", mock.Mock(side_effect=kernel.KernelInstallError("checksum")))
    with pytest.raises(kernel.KernelInstallError, match="checksum"):
        kernel.update()
    assert machine.running and machine.events == []
    assert list(machine.temp.iterdir()) == []


def test_stop_failure_never_runs_installer(machine, monkeypatch):
    monkeypatch.setattr(kernel, "stop", mock.Mock(side_effect=kernel.KernelInstallError("busy")))
    with pytest.raises(kernel.KernelInstallError, match="busy"):
        kernel.update()
    assert machine.events == ["download"]
    assert list(machine.temp.iterdir()) == []


@pytest.mark.parametrize("code", [0, 7])
def test_old_exe_does_not_make_failed_install_look_successful(machine, monkeypatch, code):
    monkeypatch.setattr(kernel, "_run_installer", lambda *a: subprocess.CompletedProcess([], code, b"", b"failed"))
    with pytest.raises(kernel.KernelInstallError):
        kernel.update()
    assert machine.exe.read_bytes() == b"old"
    assert not any(isinstance(e, tuple) for e in machine.events)
    assert list(machine.temp.iterdir()) == []


def test_restart_failure_reports_successful_update_separately(machine, monkeypatch):
    monkeypatch.setattr(kernel, "start", mock.Mock(side_effect=kernel.KernelInstallError("launch failed")))
    result = kernel.update()
    assert result["action"] == "upgraded"
    assert result["restartNeeded"]
    assert "restart failed" in result["warning"]


def test_start_is_idempotent_but_switching_access_restarts(machine):
    assert kernel.ensure_started("read")["action"] == "running"
    assert machine.events == []
    assert kernel.ensure_started("write")["action"] == "started"
    assert machine.events == ["stop", ("start", "write")]


def test_start_installs_missing_kernel_then_uses_explicit_tier(machine):
    machine.exe.unlink()
    machine.version = None
    machine.running = False
    kernel.ensure_started("read")
    assert machine.events == ["download", "install", ("start", "read")]


def test_bad_tier_does_not_install_or_stop(machine):
    with pytest.raises(kernel.KernelInstallError):
        kernel.ensure_started("typo")
    assert machine.events == []


def test_uninstall_uses_official_program_and_preserves_workspaces(machine, monkeypatch, tmp_path):
    uninstaller = machine.folder / "uninstall.exe"
    uninstaller.write_bytes(b"uninstaller")
    work = tmp_path / "project.txt"
    work.write_text("my work")
    def remove(path, windows_path):
        assert path == uninstaller
        assert machine.events == ["stop"]
        machine.exe.unlink()
        uninstaller.unlink()
        return subprocess.CompletedProcess([], 0, b"", b"")
    monkeypatch.setattr(kernel, "_run_installer", remove)
    assert kernel.uninstall()["action"] == "uninstalled"
    assert work.read_text() == "my work"
    assert kernel.uninstall()["action"] == "absent"


def test_uninstall_does_not_announce_success_until_files_disappear(machine, monkeypatch):
    (machine.folder / "uninstall.exe").write_bytes(b"uninstaller")
    monkeypatch.setattr(kernel, "_run_installer", lambda *a: subprocess.CompletedProcess([], 0, b"", b""))
    times = iter([0, 61])
    monkeypatch.setattr(kernel.time, "monotonic", lambda: next(times))
    with pytest.raises(kernel.KernelInstallError, match="not finished"):
        kernel.uninstall()


def test_missing_uninstaller_gives_repair_command(machine):
    with pytest.raises(kernel.KernelInstallError, match="/windows install --force"):
        kernel.uninstall()
    assert machine.events == []


def test_taskkill_failure_is_not_reported_as_stopped(monkeypatch):
    monkeypatch.setattr(kernel, "running", lambda: True)
    monkeypatch.setattr(kernel.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 5, b"", b"access denied"))
    with pytest.raises(kernel.KernelInstallError, match="still running"):
        kernel.stop()


def test_failed_update_says_the_kernel_was_stopped(machine, monkeypatch):
    monkeypatch.setattr(kernel, "_run_installer",
                        lambda *a: subprocess.CompletedProcess([], 2, b"", b"denied"))
    with pytest.raises(kernel.KernelInstallError, match="stopped for the update"):
        kernel.update()
    assert list(machine.temp.iterdir()) == []


def test_undeletable_temp_folder_does_not_fail_a_finished_update(machine, monkeypatch):
    real_rmtree = kernel.shutil.rmtree

    def locked(path, ignore_errors=False):
        if not ignore_errors:
            raise PermissionError("installer still open")
        real_rmtree(path, ignore_errors=True)
    monkeypatch.setattr(kernel.shutil, "rmtree", locked)
    assert kernel.update()["action"] == "upgraded"


def test_start_does_not_capture_the_kernels_inherited_pipes(tmp_path, monkeypatch):
    exe = tmp_path / kernel.KERNEL_EXE
    exe.write_bytes(b"x")
    monkeypatch.setattr(winbridge, "in_wsl", lambda: True)
    monkeypatch.setattr(kernel, "kernel_exe", lambda: exe)
    monkeypatch.setattr(winbridge, "to_windows_path", lambda p: "C:\\k.exe")
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(kernel.subprocess, "run", run)
    kernel.start("read")
    assert "capture_output" not in seen
    assert seen["stdout"] is subprocess.DEVNULL and seen["stderr"] is subprocess.DEVNULL


def test_registry_lookup_is_cached_until_forgotten(monkeypatch):
    calls = []
    monkeypatch.setattr(winbridge, "_run", lambda argv: calls.append(argv) or
                        "    InstallDir    REG_SZ    D:\\Apps\\Kernel")
    monkeypatch.setattr(winbridge, "to_wsl_path", lambda p: p)
    winbridge.reset_cache()
    try:
        assert winbridge.registered_kernel_dir() == "D:\\Apps\\Kernel"
        winbridge.registered_kernel_dir()
        assert len(calls) == 1
        winbridge.forget("kernel_dir")
        winbridge.registered_kernel_dir()
        assert len(calls) == 2
    finally:
        winbridge.reset_cache()
