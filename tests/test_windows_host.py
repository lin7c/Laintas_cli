"""The Windows kernel client, driven by a fake kernel.

The Windows build reaches its own machine through `helpwo-kernel.exe`, which
dials this process. None of that needs Windows to test: the transport is a
loopback socket and length-prefixed frames, and the parts worth pinning are
the ones a mistake is expensive in — that an unknown caller is dropped, that
tools appear only for tiers the kernel actually granted, and that they leave
when it does.
"""

import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import winbridge  # noqa: E402
import windows_host  # noqa: E402
import windows_tools  # noqa: E402
from tools import get_registry  # noqa: E402
from windows_host import FRAME_JSON, WindowsHost  # noqa: E402


class FakeKernel:
    """Reads the rendezvous file, dials the CLI, answers `win` frames."""

    def __init__(self, rendezvous: Path, tiers: dict):
        self.info = json.loads(rendezvous.read_text())
        self.tiers = tiers
        self.sock = socket.create_connection(
            (self.info["host"], self.info["port"]), timeout=5)
        self.sock.settimeout(5)
        self.seen: list[dict] = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self, token=None):
        self.send({"t": "hello", "token": token or self.info["token"],
                   "kernel": {"workspace": "C:/HelpwoWorkspace"}})
        self._thread.start()

    def send(self, payload: dict):
        raw = json.dumps(payload).encode()
        self.sock.sendall(struct.pack(">BI", FRAME_JSON, len(raw)) + raw)

    def _serve(self):
        while not self._stop:
            try:
                header = self._exact(5)
            except Exception:
                return
            if header is None:
                return
            kind, length = struct.unpack(">BI", header)
            body = self._exact(length) if length else b""
            if body is None:
                return
            try:
                message = json.loads(body.decode())
            except ValueError:
                continue
            self.seen.append(message)
            self._answer(message)

    def _answer(self, message: dict):
        if message.get("t") != "win":
            return
        op = message.get("op")
        if op == "probe":
            result = {"tiers": self.tiers, "mechanisms": {}, "adapters": [],
                      "ops": []}
        elif op == "windows":
            result = {"windows": [{"handle": 42, "title": "Notepad"}]}
        elif op == "snapshot":
            result = {"window": {"title": "Notepad"},
                      "nodes": [{"label": "e1", "role": "edit", "name": "Text"}],
                      "opaque": False}
        elif op == "invoke":
            result = {"done": "invoke"}
        else:
            self.send({"t": "win-res", "id": message["id"], "ok": False,
                       "error": f"{op} needs the machine-write tier"})
            return
        self.send({"t": "win-res", "id": message["id"], "ok": True,
                   "result": result})

    def _exact(self, count):
        chunks = []
        while count:
            chunk = self.sock.recv(count)
            if not chunk:
                return None
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def close(self):
        # shutdown() before close(): another thread of this fake is blocked
        # in recv on the same socket, and closing the object underneath it
        # does not reliably send FIN — the host then stays connected and the
        # test looks like a bug in the host. shutdown() always does.
        self._stop = True
        for step in (lambda: self.sock.shutdown(socket.SHUT_RDWR),
                     self.sock.close):
            try:
                step()
            except OSError:
                pass


@pytest.fixture
def host(tmp_path):
    windows_tools.unregister()
    h = WindowsHost(path=tmp_path / "rendezvous.json")
    assert h.start(), "the host should bind a loopback port"
    yield h, tmp_path / "rendezvous.json"
    h.stop()
    windows_tools.unregister()


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_rendezvous_names_a_port_and_a_fresh_token(host):
    _, rendezvous = host
    data = json.loads(rendezvous.read_text())
    assert 0 < data["port"] < 65536
    assert len(data["token"]) >= 32, "a guessable token is not a token"


def test_a_caller_with_the_wrong_token_is_dropped(host):
    h, rendezvous = host
    kernel = FakeKernel(rendezvous, {"machineRead": True})
    kernel.start(token="wrong" * 8)
    assert not _wait(lambda: h.connected, timeout=1.5), (
        "an unknown caller must not become the kernel")
    kernel.close()


def test_connecting_registers_only_the_granted_tier(host):
    h, rendezvous = host
    h.on_connect = lambda host_: windows_tools.register(host_)
    kernel = FakeKernel(rendezvous, {"machineRead": True, "machineWrite": False})
    kernel.start()
    assert _wait(lambda: windows_tools.registered_names())

    names = set(windows_tools.registered_names())
    assert "win.snapshot" in names
    assert "win.screenshot" in names
    assert "win.click" not in names, (
        "a write tool offered without the write tier is a tool that always "
        "fails")
    assert "win.type" not in names
    kernel.close()


def test_the_write_tier_adds_the_input_tools(host):
    h, rendezvous = host
    h.on_connect = lambda host_: windows_tools.register(host_)
    kernel = FakeKernel(rendezvous, {"machineRead": True, "machineWrite": True})
    kernel.start()
    assert _wait(lambda: "win.click" in windows_tools.registered_names())
    assert "win.invoke" in windows_tools.registered_names()
    kernel.close()


def test_tools_are_in_the_real_registry_and_leave_again(host):
    h, rendezvous = host
    h.on_connect = lambda host_: windows_tools.register(host_)
    h.on_disconnect = windows_tools.unregister
    kernel = FakeKernel(rendezvous, {"machineRead": True})
    kernel.start()
    assert _wait(lambda: get_registry().get("win.snapshot") is not None)
    kernel.close()
    assert _wait(lambda: get_registry().get("win.snapshot") is None), (
        "a kernel that went away must take its tools with it")


def test_a_call_round_trips(host):
    h, rendezvous = host
    kernel = FakeKernel(rendezvous, {"machineRead": True})
    kernel.start()
    assert _wait(lambda: h.connected)
    result = h.call("windows")
    assert result["windows"][0]["title"] == "Notepad"
    kernel.close()


def test_a_refusal_becomes_an_exception_not_a_silent_empty(host):
    h, rendezvous = host
    kernel = FakeKernel(rendezvous, {"machineRead": True})
    kernel.start()
    assert _wait(lambda: h.connected)
    with pytest.raises(windows_host.KernelUnavailable) as exc:
        h.call("click", {"x": 1, "y": 1})
    assert "machine-write" in str(exc.value)
    kernel.close()


def test_calls_without_a_kernel_say_so(tmp_path):
    h = WindowsHost(path=tmp_path / "r.json")
    with pytest.raises(windows_host.KernelUnavailable):
        h.call("probe", timeout=1)


def test_a_disconnect_releases_a_waiting_call(host):
    h, rendezvous = host
    kernel = FakeKernel(rendezvous, {"machineRead": True})
    kernel.start()
    assert _wait(lambda: h.connected)

    errors = []

    def caller():
        try:
            h.call("never_answered", timeout=10)
        except Exception as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    time.sleep(0.2)
    kernel.close()
    thread.join(timeout=5)
    assert errors, "a dropped connection must not leave a caller hanging"


def test_off_wsl_the_host_waits_for_a_linux_kernel(monkeypatch, tmp_path):
    """The Helpwo kernel is how a session reaches Helpwo on every platform,
    so a Linux session publishes itself too — in the laintas home, and never
    the single Windows file."""
    monkeypatch.delenv("LAINTAS_KERNEL_RENDEZVOUS", raising=False)
    monkeypatch.setenv("LAINTAS_KERNEL_RENDEZVOUS_DIR", str(tmp_path / "rdv"))
    monkeypatch.setattr(winbridge, "in_wsl", lambda: False)
    windows_host.stop_host()
    host = windows_host.start_host()
    try:
        assert host is not None
        files = list((tmp_path / "rdv").glob("*.json"))
        assert len(files) == 1
        assert oct(files[0].stat().st_mode & 0o777) == "0o600", "the token is a key"
        assert host._path is None, "no single file off Windows"
    finally:
        windows_host.stop_host()
    assert list((tmp_path / "rdv").glob("*.json")) == [], "stopping removes it"


def test_a_kernel_that_dies_before_the_probe_leaves_no_tools(host):
    """The probe runs on its own thread, so its failure can arrive after the
    disconnect has already been handled. Announcing anyway would register
    tools for a kernel that is gone — which is exactly what it once did."""
    h, rendezvous = host
    h.on_connect = lambda host_: windows_tools.register(host_)
    h.on_disconnect = windows_tools.unregister

    data = json.loads(rendezvous.read_text())
    sock = socket.create_connection((data["host"], data["port"]), timeout=5)
    raw = json.dumps({"t": "hello", "token": data["token"],
                      "kernel": {}}).encode()
    sock.sendall(struct.pack(">BI", FRAME_JSON, len(raw)) + raw)
    assert _wait(lambda: h.connected)
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()

    assert _wait(lambda: not h.connected)
    time.sleep(0.4)
    assert windows_tools.registered_names() == []
    assert get_registry().get("win.snapshot") is None


# -- the link the relay rides on -----------------------------------------

@pytest.fixture
def dir_host(tmp_path):
    windows_tools.unregister()
    h = WindowsHost(directory=tmp_path / "rdv", publish_legacy=False)
    assert h.start()
    rendezvous = next((tmp_path / "rdv").glob("*.json"))
    yield h, rendezvous
    h.stop()
    windows_tools.unregister()


def test_a_kernel_without_machine_tiers_is_linked_but_not_probed(dir_host):
    """A Linux kernel (or a Windows one without the switches) connects for
    the relay; asking it for Windows tools would only be refused."""
    h, rendezvous = dir_host
    links = []
    h.add_link_listener(links.append)
    kernel = FakeKernel(rendezvous, {})
    kernel.send({"t": "hello", "token": kernel.info["token"],
                 "kernel": {"features": ["cli-relay"], "machineRead": False}})
    kernel._thread.start()
    assert _wait(lambda: links == [True])
    assert h.supports("cli-relay")
    time.sleep(0.2)
    assert not any(m.get("t") == "win" for m in kernel.seen)
    kernel.close()
    assert _wait(lambda: links == [True, False])


def test_unprompted_frames_reach_their_handler(dir_host):
    h, rendezvous = dir_host
    got = []
    h.set_handler("cli-inputs", got.append)
    kernel = FakeKernel(rendezvous, {})
    kernel.send({"t": "hello", "token": kernel.info["token"],
                 "kernel": {"features": ["cli-relay"]}})
    kernel._thread.start()
    assert _wait(lambda: h.connected)
    kernel.send({"t": "cli-inputs", "agentId": "a", "inputs": [{"kind": "chat"}]})
    assert _wait(lambda: got)
    assert got[0]["inputs"][0]["kind"] == "chat"
    kernel.close()


def test_stale_files_of_dead_sessions_are_collected(tmp_path):
    directory = tmp_path / "rdv"
    directory.mkdir()
    dead = directory / "old.json"
    dead.write_text(json.dumps({"port": 1, "token": "x" * 32,
                                "hostname": socket.gethostname(),
                                "pid": 2 ** 22 + 12345}))
    elsewhere = directory / "other-machine.json"
    elsewhere.write_text(json.dumps({"port": 1, "token": "y" * 32,
                                     "hostname": "not-this-host", "pid": 1}))
    windows_host._collect_stale(directory)
    assert not dead.exists()
    assert elsewhere.exists(), "another machine's session is not ours to judge"


def test_stop_leaves_a_single_file_another_session_took_over(tmp_path):
    single = tmp_path / "single.json"
    first = WindowsHost(path=single)
    assert first.start()
    second = WindowsHost(path=single)
    assert second.start()
    first.stop()
    assert single.exists(), "the newer session still needs it"
    second.stop()
    assert not single.exists()
