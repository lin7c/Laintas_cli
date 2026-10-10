"""Terminal host: runs the CLI on a pty and relays it over two pipes.

The Windows terminal window is not a console and has no pty of its own. It
starts this program with ordinary pipes on stdin and stdout; this program
gives the CLI a real pty, so the CLI behaves exactly as it does in a Linux
terminal, and carries the byte stream to the window in frames.

Frame: one type byte, a four-byte big-endian payload length, the payload.

    window -> host   I  input bytes for the pty
                     R  resize: rows, cols as two big-endian uint16
                     X  window is closing
    host -> window   H  hello: JSON {"protocol", "pid"}
                     O  output bytes from the pty
                     E  child exited: big-endian int32 exit status

When stdin reaches end of file, or this process is told to stop, the window
is gone, so the child's session is hung up rather than left running without
a terminal.

Usage: term_host.py [--rows N] [--cols N] -- program [args...]
"""
import argparse
import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

PROTOCOL = 1
HEADER = struct.Struct(">cI")
MAX_PAYLOAD = 1 << 20
READ_CHUNK = 65536
HANGUP_GRACE = 3.0


def encode(kind: bytes, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


class FrameReader:
    """Incremental decoder; feed() returns the frames completed so far."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        self._buf += data
        frames = []
        while len(self._buf) >= HEADER.size:
            kind, length = HEADER.unpack_from(self._buf)
            if length > MAX_PAYLOAD:
                raise ValueError(f"frame of {length} bytes exceeds the limit")
            end = HEADER.size + length
            if len(self._buf) < end:
                break
            frames.append((kind, bytes(self._buf[HEADER.size:end])))
            del self._buf[:end]
        return frames


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 1.0)


def _set_size(fd: int, rows: int, cols: int) -> None:
    rows, cols = max(1, min(rows, 1000)), max(1, min(cols, 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _exit_status(status: int) -> int:
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _hang_up(pid: int) -> int:
    """End the child's session and return its exit status."""
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            pass
        deadline = time.time() + HANGUP_GRACE
        while time.time() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return _exit_status(status)
            time.sleep(0.05)
    try:
        return _exit_status(os.waitpid(pid, 0)[1])
    except ChildProcessError:
        return 1


class _Stopped(Exception):
    """This process was signalled to end."""


def _raise_stopped(signum, frame):
    raise _Stopped()


def serve(argv: list, rows: int, cols: int, in_fd: int = 0, out_fd: int = 1) -> int:
    # pty.fork() makes the child a session leader with the pty as its
    # controlling terminal, so its pid is also its process-group id.
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            os.write(2, f"term_host: cannot start {argv[0]}: {exc}\n".encode())
        os._exit(127)

    _set_size(master, rows, cols)
    _write_all(out_fd, encode(b"H", json.dumps({"protocol": PROTOCOL, "pid": pid}).encode()))
    reader = FrameReader()
    watched = [master, in_fd]
    status = None
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _raise_stopped)
    try:
        status = _relay(pid, master, in_fd, out_fd, reader, watched)
    except _Stopped:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        status = _hang_up(pid)
    try:
        _write_all(out_fd, encode(b"E", struct.pack(">i", status)))
    except OSError:
        pass
    return status


def _relay(pid, master, in_fd, out_fd, reader, watched) -> int:
    status = None
    while status is None:
        try:
            ready, _, _ = select.select(watched, [], [], 0.5)
        except InterruptedError:
            continue
        if master in ready:
            try:
                data = os.read(master, READ_CHUNK)
            except OSError as exc:
                # EIO is how a pty master reports that the last slave closed.
                if exc.errno not in (errno.EIO, errno.EBADF):
                    raise
                data = b""
            if data:
                _write_all(out_fd, encode(b"O", data))
            else:
                status = _exit_status(os.waitpid(pid, 0)[1])
                break
        if in_fd in ready:
            data = os.read(in_fd, READ_CHUNK)
            if not data:
                status = _hang_up(pid)
                break
            for kind, payload in reader.feed(data):
                if kind == b"I":
                    _write_all(master, payload)
                elif kind == b"R" and len(payload) == 4:
                    _set_size(master, *struct.unpack(">HH", payload))
                elif kind == b"X":
                    status = _hang_up(pid)
                    break
        if status is None and not ready:
            done, raw = os.waitpid(pid, os.WNOHANG)
            if done:
                status = _exit_status(raw)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rows", type=int, default=30)
    parser.add_argument("--cols", type=int, default=100)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no program given after --")
    return serve(command, args.rows, args.cols)


if __name__ == "__main__":
    sys.exit(main())
