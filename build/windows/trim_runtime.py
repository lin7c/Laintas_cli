"""Remove what the terminal window never uses from the bundled runtime.

A fresh runtime is over a gigabyte, most of it duplicates and development
files. Git's built-in commands are installed as one executable under ~170
names; a plain copy turns each name into a full file. `git <command>` runs
built-ins without those names, so only the few that are started by name are
kept. Translations, manuals, headers, static libraries and Python's own test
suite go too. Licences stay.

Usage: trim_runtime.py <runtime-root>
"""
import hashlib
import os
import shutil
import sys

# Built-ins that other programs start by their dashed name.
GIT_KEEP = {"git.exe", "git-receive-pack.exe", "git-upload-pack.exe",
            "git-upload-archive.exe", "git-shell.exe"}
REMOVE_DIRS = (
    "usr/share/locale", "usr/share/doc", "usr/share/man", "usr/share/info",
    "usr/share/gtk-doc", "usr/include", "var/cache", "var/lib/pacman/sync",
)
PYTHON_REMOVE = ("test", "idlelib", "tkinter", "turtledemo", "lib2to3")


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _size(root: str) -> int:
    total = 0
    for base, _, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def _dedupe_git(root: str) -> int:
    core = os.path.join(root, "usr", "lib", "git-core")
    main = os.path.join(core, "git.exe")
    if not os.path.isfile(main):
        return 0
    size, digest, removed = os.path.getsize(main), _digest(main), 0
    for name in os.listdir(core):
        path = os.path.join(core, name)
        if name in GIT_KEEP or not os.path.isfile(path):
            continue
        if os.path.getsize(path) == size and _digest(path) == digest:
            os.remove(path)
            removed += 1
    return removed


def trim(root: str) -> None:
    before = _size(root)
    print(f"git built-in copies removed: {_dedupe_git(root)}")
    for rel in REMOVE_DIRS:
        shutil.rmtree(os.path.join(root, *rel.split("/")), ignore_errors=True)
    lib = os.path.join(root, "usr", "lib")
    for name in os.listdir(lib) if os.path.isdir(lib) else []:
        if name.startswith("python3"):
            for rel in PYTHON_REMOVE:
                shutil.rmtree(os.path.join(lib, name, rel), ignore_errors=True)
    for base, dirs, files in os.walk(root):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(base, "__pycache__"), ignore_errors=True)
            dirs.remove("__pycache__")
        for name in files:
            if name.endswith(".a"):
                os.remove(os.path.join(base, name))
    after = _size(root)
    print(f"runtime: {before / 1e6:.0f} MB -> {after / 1e6:.0f} MB")
    largest = []
    for base, _, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            largest.append((os.path.getsize(path), os.path.relpath(path, root)))
    for size, rel in sorted(largest, reverse=True)[:25]:
        print(f"  {size / 1e6:7.1f} MB  {rel}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not os.path.isdir(sys.argv[1]):
        sys.exit(__doc__)
    trim(sys.argv[1])
