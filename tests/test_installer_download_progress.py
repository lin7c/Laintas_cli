"""Exercise bootstrap download functions without running either installer."""
import errno
import fcntl
import http.server
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b"laintas-download-test\n" * 8192


@pytest.fixture
def download_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/missing":
                self.send_error(404)
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/known")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            if self.path != "/unknown":
                self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            try:
                for offset in range(0, len(PAYLOAD), 8192):
                    self.wfile.write(PAYLOAD[offset:offset + 8192])
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

    class TestServer(http.server.ThreadingHTTPServer):
        daemon_threads = False  # server_close joins every owned request thread.
    server = TestServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="download-test-server")
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        assert not thread.is_alive()


def run_terminal(command, env, width=80):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                               env=env, start_new_session=True)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            elif process.poll() is not None:
                break
        else:
            raise TimeoutError("installer download test timed out")
        return process.wait(timeout=2), output.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        os.close(master)


def launcher(tmp_path, platform):
    if platform == "bash":
        source = (ROOT / "laintas_cli_download/public/install.sh").read_text()
        script = tmp_path / "download.sh"
        script.write_text(source.split('\necho ""\n', 1)[0] +
                          '\ndownload_with_progress "$1" "$2" "$3"\n')
        return ["bash", str(script)]
    pwsh = os.environ.get("LAINTAS_TEST_PWSH")
    if not pwsh:
        pytest.skip("Set LAINTAS_TEST_PWSH to exercise PowerShell bootstrap downloads")
    source = (ROOT / "laintas_cli_download/public/install.ps1").read_text()
    functions = source[source.index("    # ASCII source"):source.index("    [Net.ServicePointManager]")]
    script = tmp_path / "download.ps1"
    script.write_text("$ErrorActionPreference = 'Stop'\n" + functions +
                      "\nGet-LaintasDownload $args[0] $args[1] ([long]$args[2])\n")
    return [pwsh, "-NoLogo", "-NoProfile", "-File", str(script)]


def environment(tmp_path):
    return dict(os.environ, TERM="xterm-256color", TMPDIR=str(tmp_path),
                POWERSHELL_TELEMETRY_OPTOUT="1", XDG_CACHE_HOME=str(tmp_path / "cache"),
                XDG_CONFIG_HOME=str(tmp_path / "config"), DOTNET_CLI_HOME=str(tmp_path / "dotnet"))


@pytest.mark.parametrize("platform", ["bash", "powershell"])
@pytest.mark.parametrize("path", ["/known", "/unknown", "/redirect"])
def test_bootstrap_streams_in_place_and_leaves_no_owned_child(platform, path, tmp_path, download_server):
    command = launcher(tmp_path, platform)
    target = tmp_path / "payload"
    final_arg = "package.tar.gz" if platform == "bash" else str(len(PAYLOAD))
    code, output = run_terminal(command + [download_server + path, str(target), final_arg], environment(tmp_path))
    assert code == 0, output
    assert target.read_bytes() == PAYLOAD
    assert ("━" in output or "╸" in output) and "100%" in output and "✓" in output
    assert "Failed" not in output
    assert "/s" in output
    if platform == "bash" and path == "/unknown":
        assert "--%" not in output and "--:--" not in output
    assert not list(tmp_path.glob("laintas-install.*"))


@pytest.mark.parametrize("platform", ["bash", "powershell"])
def test_bootstrap_failure_is_visible_and_never_claims_completion(platform, tmp_path, download_server):
    command = launcher(tmp_path, platform)
    final_arg = "missing.tar.gz" if platform == "bash" else str(len(PAYLOAD))
    code, output = run_terminal(command + [download_server + "/missing", str(tmp_path / "payload"), final_arg], environment(tmp_path))
    assert code != 0
    assert "✗ Failed" in output
    assert "✓" not in output and "100%" not in output
    assert not list(tmp_path.glob("laintas-install.*"))


@pytest.mark.parametrize("platform", ["bash", "powershell"])
def test_redirected_output_has_only_persistent_lines(platform, tmp_path, download_server):
    command = launcher(tmp_path, platform)
    final_arg = "package.tar.gz" if platform == "bash" else str(len(PAYLOAD))
    result = subprocess.run(command + [download_server + "/known", str(tmp_path / "payload"), final_arg],
                            env=environment(tmp_path), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "\x1b" not in output and "\r" not in output
    assert "✓ 100%" in output
    assert len(output.splitlines()) == 2


@pytest.mark.parametrize("platform", ["bash", "powershell"])
@pytest.mark.parametrize("path", ["/known", "/unknown"])
def test_narrow_terminal_stays_on_two_rows(platform, path, tmp_path, download_server):
    import pyte
    command = launcher(tmp_path, platform)
    final_arg = "laintas-cli_linux_amd64.tar.gz" if platform == "bash" else str(len(PAYLOAD))
    code, output = run_terminal(command + [download_server + path, str(tmp_path / "payload"), final_arg],
                                environment(tmp_path), width=24)
    assert code == 0, output
    screen = pyte.Screen(24, 24)
    pyte.Stream(screen).feed(output)
    assert screen.cursor.y == 2, screen.display
    assert "✓ 100%" in screen.display[1]


def test_windows_size_mismatch_reports_failure(tmp_path, download_server):
    command = launcher(tmp_path, "powershell")
    code, output = run_terminal(command + [download_server + "/known", str(tmp_path / "payload"), str(len(PAYLOAD) + 1)],
                                environment(tmp_path))
    assert code != 0
    assert "✗ Failed" in output and "✓ 100%" not in output


@pytest.mark.parametrize("platform", ["bash", "powershell"])
def test_no_color_removes_color_but_keeps_refresh(platform, tmp_path, download_server):
    command = launcher(tmp_path, platform)
    final_arg = "package.tar.gz" if platform == "bash" else str(len(PAYLOAD))
    code, output = run_terminal(command + [download_server + "/known", str(tmp_path / "payload"), final_arg],
                                dict(environment(tmp_path), NO_COLOR="1"))
    assert code == 0, output
    assert "\r" in output and "✓ 100%" in output
    assert "38;" not in output and "[32m" not in output and "[92m" not in output



def test_windows_unknown_size_omits_fake_percentage_and_eta(tmp_path):
    command = launcher(tmp_path, "powershell")
    # The production Windows manifest has a signed expected size, so an HTTP
    # response without Content-Length still has a known total. Exercise the
    # genuinely unknown state directly rather than pretending it is unknown.
    script = Path(command[-1])
    script.write_text(script.read_text().split("\nGet-LaintasDownload $args")[0] + """
$state = [PSCustomObject]@{
    Bytes = 8192L; Total = 0L; Width = 80; Animate = $true; Plain = $true
    Timer = [Diagnostics.Stopwatch]::StartNew(); LastPaint = -1.0
}
Show-LaintasProgress $state
Write-Host ''
""")
    code, output = run_terminal(command, environment(tmp_path))
    assert code == 0, output
    assert "--%" not in output and "--:--" not in output
