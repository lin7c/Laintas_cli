import json
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
