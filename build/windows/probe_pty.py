"""Check that a POSIX pty, fork and select behave on this interpreter.

This is the mechanism every shell session in the CLI is built on. The probe
starts bash on a pty, runs one command, resizes the pty and confirms the
child saw the new size.
"""
import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time


def read_until(fd: int, needle: bytes, timeout: float = 20.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while time.time() < end and needle not in buf:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    return buf


def main() -> int:
    started = time.time()
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "--norc", "--noprofile"])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 31, 97, 0, 0))
    os.write(fd, b"echo probe-$((40+2)); stty size; uname -s\n")
    out = read_until(fd, b"probe-42")
    out += read_until(fd, b"31 97")
    out += read_until(fd, b"\n", 2.0)
    os.write(fd, b"exit\n")
    os.waitpid(pid, 0)
    text = out.decode("utf-8", "replace")
    ok = "probe-42" in text and "31 97" in text
    print(text)
    print(f"pty probe {'PASSED' if ok else 'FAILED'} in {time.time() - started:.2f}s")

    forks = 20
    started = time.time()
    for _ in range(forks):
        child = os.fork()
        if child == 0:
            os._exit(0)
        os.waitpid(child, 0)
    print(f"fork+wait: {(time.time() - started) / forks * 1000:.1f} ms each")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
