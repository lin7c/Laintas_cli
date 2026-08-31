#!/usr/bin/env python3
"""Build and mirror the complete self-hosted release asset set.

Mirrors the manifest-generation logic in .github/workflows/release.yml so the
source-install self-updater can fetch from cli.laintas.com instead of GitHub.
Outputs into the cli.laintas.com document root:

    <repo>/laintas_cli_download/dist/releases/latest/
    <repo>/laintas_cli_download/dist/releases/v<version>/

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
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_RELEASES = os.path.join(REPO, "laintas_cli_download", "dist", "releases")

# Binary/install assets self-hosted alongside the source-update assets. They are
# built by CI and live on the GitHub Release; we mirror them onto cli.laintas.com
# so install.sh (and any future frozen /v update) never touches GitHub at runtime.
RELEASE_ASSET_TEMPLATES = (
    "laintas-cli_linux_amd64.tar.gz",
    "laintas-cli_linux_arm64.tar.gz",
    "laintas-cli_windows_amd64.zip",
    "laintas-cli_source.zip",
    "laintas-cli_{version}_amd64.deb",
    "SHA256SUMS.txt",
)

GITHUB_RELEASE_BASE = "https://github.com/lin7c/Laintas_cli/releases/download"


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
                    # package_manifest.json is the packaging source of truth.
                    # Keep every declared package/data file in the source-update
                    # bundle as well; filtering by extension silently omitted
                    # default_skills/*/SKILL.md from /v updates.
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

        # Mirror and verify every CI-built install asset before touching either
        # self-hosted channel, so a failed download cannot publish partial data.
        assets_dir = os.path.join(tmp, "assets")
        os.makedirs(assets_dir, exist_ok=True)
        release_assets = _fetch_release_assets(f"v{version}", version, assets_dir)
        _verify_release_assets(release_assets, assets_dir)

        for channel in ("latest", f"v{version}"):
            outdir = os.path.join(DIST_RELEASES, channel)
            os.makedirs(outdir, exist_ok=True)
            _remove_stale_release_assets(outdir)
            shutil.copy(manifest_path, os.path.join(outdir, "manifest.json"))
            shutil.copy(zip_path, os.path.join(outdir, "src_manifest.zip"))
            for name in release_assets:
                shutil.copy(os.path.join(assets_dir, name), os.path.join(outdir, name))
            print(f"  → {outdir}: manifest.json, src_manifest.zip"
                  + ", " + ", ".join(release_assets))

    nfiles = len(manifest["files"])
    print(f"Published v{version} assets ({nfiles} source files + "
          f"{len(release_assets)} release assets) to {DIST_RELEASES}")
    return 0


def _release_asset_names(version: str) -> list[str]:
    return [name.format(version=version) for name in RELEASE_ASSET_TEMPLATES]


def _fetch_release_assets(tag: str, version: str, dst: str) -> list[str]:
    """Download every install asset for ``tag`` from the GitHub Release.

    Prefer an authenticated ``gh`` download, but fall back to the public release
    URLs so an expired local gh token cannot leave a partially published mirror.
    """
    names = _release_asset_names(version)
    patterns = []
    for name in names:
        patterns += ["--pattern", name]
    if shutil.which("gh"):
        subprocess.run(["gh", "release", "download", tag, "--dir", dst,
                        "--clobber", *patterns], cwd=REPO, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for name in names:
        path = os.path.join(dst, name)
        if os.path.isfile(path):
            continue
        url = "/".join((GITHUB_RELEASE_BASE,
                        urllib.parse.quote(tag, safe=""),
                        urllib.parse.quote(name, safe="")))
        request = urllib.request.Request(url, headers={"User-Agent": "laintas-release-sync"})
        partial = path + ".part"
        try:
            with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as fh:
                shutil.copyfileobj(response, fh)
            os.replace(partial, path)
        finally:
            if os.path.exists(partial):
                os.unlink(partial)
    return names


def _verify_release_assets(names: list[str], dst: str) -> None:
    missing = [name for name in names if not os.path.isfile(os.path.join(dst, name))]
    if missing:
        raise RuntimeError(f"release assets missing: {', '.join(missing)}")

    sums_path = os.path.join(dst, "SHA256SUMS.txt")
    expected = {}
    with open(sums_path, encoding="utf-8") as fh:
        for line in fh:
            checksum, name = line.strip().split(None, 1)
            expected[name.lstrip("* ")] = checksum
    payloads = [name for name in names if name != "SHA256SUMS.txt"]
    if set(payloads) != set(expected):
        raise RuntimeError("SHA256SUMS.txt does not cover the complete release asset set")
    for name in payloads:
        digest = hashlib.sha256()
        with open(os.path.join(dst, name), "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected[name]:
            raise RuntimeError(f"checksum mismatch for {name}")


def _remove_stale_release_assets(outdir: str) -> None:
    """Remove managed payloads left by an older version before publishing."""
    for name in os.listdir(outdir):
        if name.startswith("laintas-cli_") or name == "SHA256SUMS.txt":
            path = os.path.join(outdir, name)
            if os.path.isfile(path):
                os.unlink(path)


if __name__ == "__main__":
    raise SystemExit(main())
