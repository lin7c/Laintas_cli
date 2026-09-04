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
        if "/" not in line and line.endswith(".py")
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
        "laintas-cli_windows_amd64_setup.exe",
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


def test_release_doc_names_the_channel_the_windows_build_depends_on():
    """`/windows install` reads a file published by a *different* repository.

    Nothing in a laintas_cli release contains the kernel, so a release here
    cannot break it — which is exactly why it is easy to forget. A Helpwo
    deploy that drops `latest.json` breaks `/windows install` for every
    installed CLI, and build/RELEASE.md is the only place that would tell
    somebody where to look.
    """
    import re
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import windows_kernel

    doc = (Path(build_release_assets.REPO) / "build/RELEASE.md").read_text(
        encoding="utf-8")
    host = re.sub(r"^https?://", "", windows_kernel.DOWNLOAD_ORIGIN)
    assert host in doc, (
        f"build/RELEASE.md does not mention {host}, which the Windows build "
        f"downloads the kernel from")
    assert "latest.json" in doc
