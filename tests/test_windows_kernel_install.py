"""Installing the Windows kernel, against a local file server.

The install path downloads an executable and runs it. Two things about it
are worth pinning on every platform, because getting either wrong is
expensive and neither needs Windows to test:

  * **The checksum decides.** A file that does not match its published digest
    must be deleted and must never reach the installer step.
  * **Install is not permission.** `install()` puts a program on disk;
    nothing about it grants the kernel a tier. That separation is the whole
    reason the tiers exist.
"""

import hashlib
import http.server
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import winbridge  # noqa: E402
import windows_kernel  # noqa: E402
from windows_kernel import KernelInstallError, Release  # noqa: E402


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A directory served over HTTP, standing in for the downloads folder."""
    root = tmp_path / "downloads"
    root.mkdir()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            """The size-limit and checksum tests abandon a response
            mid-body, so the handler thread gets a broken pipe. That is the
            behaviour under test, not a fault — and letting it surface as an
            unhandled thread exception puts an intermittent warning in the
            suite that a real one could hide behind."""
            if not isinstance(sys.exc_info()[1], (BrokenPipeError,
                                                  ConnectionResetError)):
                super().handle_error(request, client_address)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = Server(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(windows_kernel, "DOWNLOAD_ORIGIN",
                        f"http://127.0.0.1:{port}")
    try:
        yield root
    finally:
        server.shutdown()


def publish(root: Path, name="helpwo-kernel-setup-1.2.3-abcdef01.exe",
            body=b"MZ fake installer", version="1.2.3", digest=None):
    (root / name).write_bytes(body)
    (root / "latest.json").write_text(json.dumps({
        "version": version, "asset": name,
        "sha256": digest or hashlib.sha256(body).hexdigest(),
    }), encoding="utf-8")
    return name


# -- the published pointer ----------------------------------------------

def test_latest_reads_the_published_listing(served):
    publish(served)
    release = windows_kernel.latest()
    assert release.version == "1.2.3"
    assert release.asset.endswith(".exe")
    assert release.url.endswith(release.asset)


def test_a_listing_naming_a_path_is_refused(served):
    """The asset name becomes a URL and a filename. It arrives over the
    network, so it is validated rather than trusted."""
    (served / "latest.json").write_text(json.dumps({
        "version": "1.0.0", "asset": "../../../etc/passwd.exe",
        "sha256": "0" * 64,
    }), encoding="utf-8")
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel.latest()
    assert "not usable" in str(exc.value)


def test_a_listing_without_a_checksum_is_refused(served):
    (served / "latest.json").write_text(json.dumps({
        "version": "1.0.0", "asset": "helpwo-kernel-setup-1.0.0-aa.exe",
    }), encoding="utf-8")
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel.latest()
    assert "checksum" in str(exc.value)


def test_an_unreachable_origin_says_where_it_tried(served, monkeypatch):
    monkeypatch.setattr(windows_kernel, "DOWNLOAD_ORIGIN",
                        "http://127.0.0.1:1")
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel.latest()
    assert "127.0.0.1:1" in str(exc.value)


# -- the download --------------------------------------------------------

def test_a_good_download_lands_and_verifies(served, tmp_path):
    body = b"MZ" + b"\x00" * 4096
    name = publish(served, body=body)
    release = windows_kernel.latest()
    into = tmp_path / "dl"
    into.mkdir()
    path = windows_kernel._download(release, into, None)
    assert path.name == name
    assert path.read_bytes() == body


def test_a_tampered_download_is_deleted_not_installed(served, tmp_path):
    """The failure this check exists for looks exactly like success to
    anything that only reads the HTTP status."""
    publish(served, body=b"the real thing", digest="f" * 64)
    release = windows_kernel.latest()
    into = tmp_path / "dl"
    into.mkdir()
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel._download(release, into, None)
    assert "checksum" in str(exc.value)
    assert "nothing was installed" in str(exc.value)
    assert list(into.iterdir()) == [], (
        "a file that failed its checksum must not be left where something "
        "could run it")


def test_an_oversized_download_stops(served, tmp_path, monkeypatch):
    monkeypatch.setattr(windows_kernel, "MAX_INSTALLER_BYTES", 128)
    publish(served, body=b"x" * 4096)
    release = windows_kernel.latest()
    into = tmp_path / "dl"
    into.mkdir()
    with pytest.raises(KernelInstallError):
        windows_kernel._download(release, into, None)
    assert list(into.iterdir()) == []


def test_progress_is_reported(served, tmp_path):
    publish(served, body=b"y" * (600 * 1024))
    release = windows_kernel.latest()
    into = tmp_path / "dl"
    into.mkdir()
    seen = []
    windows_kernel._download(release, into, lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == 600 * 1024


# -- what install does and does not do ------------------------------------

def test_install_refuses_outside_wsl(monkeypatch):
    monkeypatch.setattr(winbridge, "in_wsl", lambda: False)
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel.install()
    assert "Windows build" in str(exc.value)


def test_installing_grants_no_tier():
    """Putting the program on disk is not permission to use the machine.

    `install()` has no tier argument and `start()` will not take one it was
    not given — the flags come from a word the user typed in `/windows
    start`, never from the install step.
    """
    import inspect
    assert "tier" not in inspect.signature(windows_kernel.install).parameters
    source = inspect.getsource(windows_kernel.install)
    assert "allow-machine" not in source


def test_start_refuses_an_unknown_tier(monkeypatch, tmp_path):
    exe = tmp_path / "helpwo-kernel.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(windows_kernel, "kernel_exe", lambda: exe)
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel.start("everything")
    assert "unknown tier" in str(exc.value)


def test_start_maps_tiers_to_the_kernels_own_flags(monkeypatch, tmp_path):
    exe = tmp_path / "helpwo-kernel.exe"
    exe.write_bytes(b"MZ")
    launched = {}

    monkeypatch.setattr(windows_kernel, "kernel_exe", lambda: exe)
    monkeypatch.setattr(winbridge, "to_windows_path",
                        lambda p: "C:\\HelpwoKernel\\helpwo-kernel.exe")
    monkeypatch.setattr(windows_kernel.subprocess, "Popen",
                        lambda argv, **kw: launched.setdefault("argv", argv))

    assert windows_kernel.start("workspace")["flags"] == []
    assert windows_kernel.start("read")["flags"] == ["--allow-machine-read"]
    assert windows_kernel.start("write")["flags"] == ["--allow-machine-write"]
    assert "start" in launched["argv"], (
        "the kernel gets its own console window; the user has to be able to "
        "see it and close it")


def test_start_without_an_install_says_what_to_run(monkeypatch):
    monkeypatch.setattr(windows_kernel, "kernel_exe", lambda: None)
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel.start("read")
    assert "/windows install" in str(exc.value)


# -- status --------------------------------------------------------------

def test_status_is_readable_on_a_machine_with_none_of_this(monkeypatch):
    monkeypatch.setattr(winbridge, "in_wsl", lambda: False)
    state = windows_kernel.status()
    assert state["wsl"] is False
    assert state["installed"] is False
    assert state["connected"] is False


# -- what the first real Windows run taught -------------------------------

def test_windows_console_output_does_not_crash_the_status(monkeypatch):
    """A Chinese Windows answers `tasklist` with GBK bytes.

    `信息: 没有运行的任务…` begins with 0xD0, so decoding it as UTF-8 raised
    and `/windows status` reported a codec error instead of a status. The
    only thing being looked for in that output is an ASCII process name.
    """
    gbk = "信息: 没有运行的任务匹配指定标准。".encode("gbk")
    assert gbk[0] == 0xD0

    class Done:
        stdout = gbk
        stderr = b""

    monkeypatch.setattr(winbridge, "in_wsl", lambda: True)
    monkeypatch.setattr(windows_kernel.subprocess, "run",
                        lambda *a, **k: Done())
    assert windows_kernel.running() is False


def test_decode_never_raises_on_any_byte():
    assert winbridge.decode(b"") == ""
    assert winbridge.decode(b"plain ascii") == "plain ascii"
    assert winbridge.decode("信息".encode("gbk"))  # no exception
    assert winbridge.decode(bytes(range(256)))    # no exception


def test_the_localappdata_lookup_asks_windows_for_utf8(monkeypatch):
    """The one interop call whose content must survive intact.

    A replaced character in the path points at a directory that does not
    exist, so this call switches the console to UTF-8 rather than decoding
    defensively.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv

        class Done:
            stdout = b"C:\\Users\\lin7c\\AppData\\Local\n"
        return Done()

    monkeypatch.setattr(winbridge.subprocess, "run", fake_run)
    monkeypatch.setattr(winbridge, "in_wsl", lambda: True)
    monkeypatch.setattr(winbridge, "to_wsl_path", lambda p: Path("/mnt/c/x"))
    winbridge.reset_cache()
    try:
        assert winbridge.localappdata() == Path("/mnt/c/x")
    finally:
        winbridge.reset_cache()
    assert "chcp 65001" in " ".join(seen["argv"])


def test_downloads_do_not_go_out_as_python_urllib():
    """The CDN in front of the download host answers that UA with 403.

    It looked exactly like the file being missing, which is the reason this
    is pinned rather than left to whatever urllib defaults to.
    """
    request = windows_kernel._request("https://example.test/latest.json")
    agent = request.get_header("User-agent")
    assert agent
    assert "urllib" not in agent.lower()
    assert agent.startswith("laintas-cli/")


def test_every_download_request_carries_that_agent(served, monkeypatch):
    publish(served)
    agents = []
    real = windows_kernel.urllib.request.urlopen

    def spy(req, *args, **kwargs):
        agents.append(req.get_header("User-agent"))
        return real(req, *args, **kwargs)

    monkeypatch.setattr(windows_kernel.urllib.request, "urlopen", spy)
    release = windows_kernel.latest()
    into = served.parent / "dl"
    into.mkdir()
    windows_kernel._download(release, into, None)
    assert len(agents) == 2, "both the listing and the installer"
    assert all(a and a.startswith("laintas-cli/") for a in agents)


def test_the_installer_is_made_executable_before_it_is_run(tmp_path,
                                                           monkeypatch):
    """Python writes files without the execute bit, and a DrvFs mounted
    with `metadata` keeps that faithfully — so the download that succeeded
    fails to start with EACCES."""
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    installer.chmod(0o644)
    monkeypatch.setattr(windows_kernel.subprocess, "run",
                        lambda *a, **k: object())

    windows_kernel._run_installer(installer, "C:\\Temp\\setup.exe")
    assert installer.stat().st_mode & 0o111, "not executable"


def test_a_noexec_mount_falls_back_to_letting_windows_run_it(tmp_path,
                                                             monkeypatch):
    """`chmod` cannot help on a `/mnt` mounted noexec, which some hardened
    setups use. Windows was always going to be the one running this."""
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise PermissionError(13, "Permission denied")
        return object()

    monkeypatch.setattr(windows_kernel.subprocess, "run", fake_run)
    windows_kernel._run_installer(installer, "C:\\Temp\\setup.exe")

    assert len(calls) == 2
    assert calls[1][0] == "cmd.exe"
    assert "C:\\Temp\\setup.exe" in calls[1]
    assert "/S" in calls[1], "the fallback must stay a silent install"


def test_a_failure_to_run_names_the_file_the_user_can_run(tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(winbridge, "in_wsl", lambda: True)
    monkeypatch.setattr(windows_kernel, "latest",
                        lambda: Release("9.9.9", "s.exe", "0" * 64))
    monkeypatch.setattr(windows_kernel, "installed_version", lambda: None)
    monkeypatch.setattr(winbridge, "windows_temp", lambda: tmp_path)
    monkeypatch.setattr(windows_kernel, "_download",
                        lambda *a, **k: tmp_path / "s.exe")
    monkeypatch.setattr(winbridge, "to_windows_path",
                        lambda p: "C:\\Temp\\s.exe")

    def explode(*a, **k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(windows_kernel, "_run_installer", explode)
    with pytest.raises(KernelInstallError) as exc:
        windows_kernel.install()
    assert "C:\\Temp\\s.exe" in str(exc.value), (
        "a user who cannot be helped automatically needs the path to "
        "double-click")
