#!/usr/bin/env python3
"""Build the self-hosted /v update assets (manifest.json + src_manifest.zip).

Mirrors the manifest-generation logic in .github/workflows/release.yml so the
source-install self-updater can fetch from cli.laintas.com instead of GitHub.
Outputs into the cli.laintas.com document root:

    <repo>/laintas_cli_download/dist/releases/latest/{manifest.json,src_manifest.zip}
    <repo>/laintas_cli_download/dist/releases/v<version>/{...}

Run from anywhere:  python3 scripts/build_release_assets.py
The `latest/` copy is what /v (channel=latest) reads; the versioned dir keeps
an immutable copy so a pinned LAINTAS_UPDATE_CHANNEL=v1.7.4 also resolves.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_RELEASES = os.path.join(REPO, "laintas_cli_download", "dist", "releases")


def _gen_src_out(dst: str) -> dict:
    """Replicate the CI source staging + manifest into ``dst`` (a src_out dir)."""
    pm = json.load(open(os.path.join(REPO, "package_manifest.json")))
    top = [m + ".py" for m in pm["modules"]] + pm["extra_files"]
    dirs = pm["packages"] + pm["data_dirs"]

    for f in top:
        src = os.path.join(REPO, f)
        if os.path.isfile(src) and f.endswith((".py", ".json", ".txt")):
            shutil.copy(src, dst)
    for d in dirs:
        srcd = os.path.join(REPO, d)
        if os.path.isdir(srcd):
            for root, subdirs, files in os.walk(srcd):
                subdirs[:] = [x for x in subdirs if x != "__pycache__"]
                for fn in files:
                    if fn.endswith((".py", ".json")):
                        target = os.path.join(dst, os.path.relpath(root, REPO))
                        os.makedirs(target, exist_ok=True)
                        shutil.copy(os.path.join(root, fn), target)

    sys.path.insert(0, REPO)
    from version import __version__

    import datetime
    manifest = {"version": __version__,
                "released": datetime.date.today().isoformat(),
                "files": {}}
    for root, subdirs, files in os.walk(dst):
        subdirs[:] = [x for x in subdirs if x != "__pycache__"]
        for fn in files:
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, dst)
            data = open(path, "rb").read()
            manifest["files"][rel] = {
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    with open(os.path.join(dst, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="laintas-relassets-") as tmp:
        src_out = os.path.join(tmp, "src_out")
        os.makedirs(src_out, exist_ok=True)
        manifest = _gen_src_out(src_out)
        version = manifest["version"]

        zip_path = os.path.join(tmp, "src_manifest.zip")
        subprocess.run(["zip", "-qr", zip_path, "."], cwd=src_out, check=True)
        manifest_path = os.path.join(src_out, "manifest.json")

        for channel in ("latest", f"v{version}"):
            outdir = os.path.join(DIST_RELEASES, channel)
            os.makedirs(outdir, exist_ok=True)
            shutil.copy(manifest_path, os.path.join(outdir, "manifest.json"))
            shutil.copy(zip_path, os.path.join(outdir, "src_manifest.zip"))
            print(f"  → {outdir}/manifest.json,src_manifest.zip")

    nfiles = len(manifest["files"])
    print(f"Published v{version} assets ({nfiles} source files) to {DIST_RELEASES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
