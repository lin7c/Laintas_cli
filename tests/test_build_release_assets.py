import json
import hashlib
from pathlib import Path

from scripts import build_release_assets


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


def test_release_assets_include_source_and_versioned_deb():
    assert build_release_assets._release_asset_names("1.8.3") == [
        "laintas-cli_linux_amd64.tar.gz",
        "laintas-cli_linux_arm64.tar.gz",
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
