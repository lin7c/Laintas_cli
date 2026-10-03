import json
import hashlib
import subprocess
from pathlib import Path

from scripts import build_release_assets


def test_manifest_covers_all_tracked_top_level_modules():
    """Every git-tracked top-level .py module must be registered in
    package_manifest.json — that file drives setup.py, the PyInstaller
    spec, the CI source bundle, and the /v self-update manifest.  A module
    missing from ``modules`` silently ships in NO release artifact, so an
    installed CLI would ImportError at runtime.

    Untracked work-in-progress files are intentionally excluded: they are
    not part of any release until they are committed and registered.
    """
    repo = Path(build_release_assets.REPO)
    pm = json.loads((repo / "package_manifest.json").read_text(encoding="utf-8"))
    modules = set(pm["modules"])

    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=repo,
        capture_output=True, text=True, check=True)
    tracked_top = {
        line[:-3] for line in out.stdout.splitlines()
        if "/" not in line and line.endswith(".py") and (repo / line).is_file()
    }
    # setup.py is the packaging entry point, not a shipped module.
    missing = sorted(m for m in tracked_top - modules if m != "setup")
    assert not missing, f"top-level modules not registered in package_manifest.json: {missing}"
    assert "stuck_signals" in modules



def test_source_update_bundle_includes_declared_data_files(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "version.py").write_text('__version__ = "9.9.9"\n')
    (repo / "module.py").write_text("VALUE = 1\n")
    skill_dir = repo / "default_skills" / "example"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Example\n")
    (skill_dir / "extension.json").write_text("{}\n")
    (repo / "package_manifest.json").write_text(json.dumps({
        "modules": ["module", "version"],
        "extra_files": [],
        "packages": [],
        "data_dirs": ["default_skills"],
    }))

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(build_release_assets, "REPO", str(repo))
    monkeypatch.syspath_prepend(str(repo))

    manifest = build_release_assets._gen_src_out(str(out))

    assert "default_skills/example/SKILL.md" in manifest["files"]
    assert "default_skills/example/extension.json" in manifest["files"]
    assert (out / "default_skills" / "example" / "SKILL.md").is_file()


def test_source_update_bundle_includes_license(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "version.py").write_text('__version__ = "9.9.9"\n')
    (repo / "LICENSE").write_text("license terms\n")
    (repo / "package_manifest.json").write_text(json.dumps({
        "modules": ["version"],
        "extra_files": ["LICENSE"],
        "packages": [],
        "data_dirs": [],
    }))

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(build_release_assets, "REPO", str(repo))
    monkeypatch.syspath_prepend(str(repo))

    manifest = build_release_assets._gen_src_out(str(out))

    assert "LICENSE" in manifest["files"]
    assert (out / "LICENSE").read_text() == "license terms\n"


def test_release_assets_include_source_and_versioned_deb():
    assert build_release_assets._release_asset_names("1.8.3") == [
        "laintas-cli_linux_amd64.tar.gz",
        "laintas-cli_linux_arm64.tar.gz",
        "laintas-cli_darwin_amd64.tar.gz",
        "laintas-cli_darwin_arm64.tar.gz",
        "laintas-cli_source.zip",
        "laintas-cli_1.8.3_amd64.deb",
        "SHA256SUMS.txt",
    ]


def test_release_asset_verification_and_stale_cleanup(tmp_path):
    names = build_release_assets._release_asset_names("1.8.3")
    payloads = names[:-1]
    checksums = []
    for name in payloads:
        data = name.encode()
        (tmp_path / name).write_bytes(data)
        checksums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    (tmp_path / "SHA256SUMS.txt").write_text("".join(checksums))

    build_release_assets._verify_release_assets(names, str(tmp_path))

    (tmp_path / "laintas-cli_1.8.2_amd64.deb").write_bytes(b"stale")
    (tmp_path / "manifest.json").write_text("{}")
    build_release_assets._remove_stale_release_assets(str(tmp_path))

    assert not any(path.name.startswith("laintas-cli_") for path in tmp_path.iterdir())
    assert not (tmp_path / "SHA256SUMS.txt").exists()
    assert (tmp_path / "manifest.json").is_file()


def test_published_unix_downloads_survive_a_website_rebuild(tmp_path, monkeypatch):
    """Use checksum-verified fixture assets to exercise both publication copies."""
    import shutil
    durable = tmp_path / "public" / "releases"
    live = tmp_path / "dist" / "releases"
    windows = tmp_path / "public" / "windows" / "LaintasSetup.exe"
    windows.parent.mkdir(parents=True)
    windows.write_bytes(b"independent-windows-fixture")
    monkeypatch.setattr(build_release_assets, "PUBLIC_RELEASES", str(durable))
    monkeypatch.setattr(build_release_assets, "DIST_RELEASES", str(live))
    monkeypatch.setenv("LAINTAS_RELEASE_TAG", "v9.9.9")

    def stage_source(directory):
        (Path(directory) / "manifest.json").write_text('{"version":"9.9.9","files":{}}')
        return {"version": "9.9.9", "files": {}}

    def stage_assets(tag, version, directory):
        names = build_release_assets._release_asset_names(version)
        checksums = []
        for name in names[:-1]:
            payload = name.encode()
            (Path(directory) / name).write_bytes(payload)
            checksums.append(f"{hashlib.sha256(payload).hexdigest()}  {name}\n")
        (Path(directory) / "SHA256SUMS.txt").write_text("".join(checksums))
        return names

    monkeypatch.setattr(build_release_assets, "_gen_src_out", stage_source)
    monkeypatch.setattr(build_release_assets, "_fetch_release_assets", stage_assets)
    assert build_release_assets.main() == 0
    offered = {path.name: path.read_bytes() for path in (live / "latest").iterdir()}
    assert offered == {path.name: path.read_bytes() for path in (durable / "latest").iterdir()}
    # A static rebuild copies public resources back into an empty dist.
    shutil.rmtree(live)
    shutil.copytree(durable, live)
    assert offered == {path.name: path.read_bytes() for path in (live / "latest").iterdir()}
    assert (live / "v9.9.9" / "src_manifest.zip").is_file()
    assert windows.read_bytes() == b"independent-windows-fixture"
    assert not any("windows" in name.casefold() for name in offered)
