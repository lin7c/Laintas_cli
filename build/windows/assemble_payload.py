"""Assemble the CLI sources the Windows terminal window ships with.

Copies exactly what `package_manifest.json` declares, the same list every
other package is built from, into `build/windows/app/payload/app`, and adds
the launcher and terminal host. The bundled runtime is installed beside it
by the build workflow.
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WINDOWS_FILES = ("launch.sh", "term_host.py")


def assemble(dest: str) -> int:
    with open(os.path.join(ROOT, "package_manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    count = 0
    for name in [m + ".py" for m in manifest["modules"]] + manifest["extra_files"]:
        shutil.copy2(os.path.join(ROOT, name), os.path.join(dest, name))
        count += 1
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in manifest["packages"] + manifest["data_dirs"]:
        shutil.copytree(os.path.join(ROOT, name), os.path.join(dest, name), ignore=ignore)
        count += sum(len(files) for _, _, files in os.walk(os.path.join(dest, name)))
    target = os.path.join(dest, "build", "windows")
    os.makedirs(target)
    for name in WINDOWS_FILES:
        shutil.copy2(os.path.join(HERE, name), os.path.join(target, name))
        count += 1
    return count


def main() -> int:
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "app", "payload", "app")
    print(f"{assemble(dest)} files -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
