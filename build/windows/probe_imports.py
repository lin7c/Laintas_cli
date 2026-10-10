"""Report which packaged modules import on this interpreter.

Run by the Windows probe workflow under two interpreters (native CPython and
the MSYS2 POSIX-layer Python) to measure how much of the CLI loads on each
before any porting work is chosen. Each module is imported in its own
process so one failure cannot hide the next.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    with open(os.path.join(ROOT, "package_manifest.json"), encoding="utf-8") as fh:
        modules = json.load(fh)["modules"]
    print(f"python {sys.version.split()[0]} platform={sys.platform} os.name={os.name}")
    for name in ("pty", "termios", "fcntl", "tty", "resource", "pwd", "select"):
        try:
            __import__(name)
            state = "ok"
        except Exception as exc:  # noqa: BLE001 - the message is the report
            state = f"MISSING ({type(exc).__name__})"
        print(f"  stdlib {name:9s} {state}")
    print(f"  os.fork {'ok' if hasattr(os, 'fork') else 'MISSING'}")
    failed = []
    home = tempfile.mkdtemp(prefix="laintas-probe-")
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8",
               HOME=home, USERPROFILE=home, LAINTAS_HOME=os.path.join(home, ".laintas"))
    for mod in modules:
        try:
            proc = subprocess.run(
                [sys.executable, "-c",
                 f"import paths; paths.configure_account(''); import {mod}"],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
                stdin=subprocess.DEVNULL,
            )
            ok, detail = proc.returncode == 0, (proc.stderr.strip().splitlines() or [""])[-1]
        except subprocess.TimeoutExpired:
            ok, detail = False, "timed out after 120s"
        if not ok:
            failed.append((mod, detail))
    print(f"\n{len(modules) - len(failed)}/{len(modules)} modules import")
    for mod, detail in failed:
        print(f"  FAIL {mod}: {detail[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
