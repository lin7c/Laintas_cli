#!/usr/bin/env python3
"""Build immutable official .lext packages and their static registry."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extension_manager import create_publication_archive, read_manifest  # noqa: E402


OFFICIAL_NAMES = ("blindpick", "swebench")


def main() -> None:
    output = ROOT / "laintas_cli_download" / "public" / "extensions"
    packages = output / "official"
    packages.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in OFFICIAL_NAMES:
        source = ROOT / "extensions" / name
        manifest = read_manifest(source)
        version = str(manifest["version"])
        artifact = packages / f"{name}-{version}.lext"
        create_publication_archive(source, artifact)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        entries.append({
            "id": f"laintas/{name}",
            "name": manifest.get("displayName") or name,
            "version": version,
            "description": manifest.get("description", ""),
            "url": f"https://cli.laintas.com/extensions/official/{artifact.name}",
            "sha256": digest,
        })
    (output / "official-registry.json").write_text(
        json.dumps({"schemaVersion": 1, "extensions": entries}, indent=2) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
