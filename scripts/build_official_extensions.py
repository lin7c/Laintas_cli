#!/usr/bin/env python3
"""Build immutable official .lext packages and their static registry."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extension_manager import create_publication_archive, read_manifest  # noqa: E402


OFFICIAL_NAMES = ("blindpick", "swebench", "whatsapp")


def main() -> None:
    output = ROOT / "laintas_cli_download" / "public" / "extensions"
    packages = output / "official"
    packages.mkdir(parents=True, exist_ok=True)
    entries = []
    drifted = []
    for name in OFFICIAL_NAMES:
        source = ROOT / "extensions" / name
        manifest = read_manifest(source)
        version = str(manifest["version"])
        artifact = packages / f"{name}-{version}.lext"

        # A published version is immutable: the registry pins a SHA-256, and
        # `/extensions install` verifies the download against it. Rewriting
        # `<name>-<version>.lext` with different bytes silently changes what
        # that version means and invalidates every copy already installed --
        # so build to the side, and only adopt the result if the version is
        # new. Drift means the source moved without a version bump; say so
        # and keep the published artifact.
        with tempfile.TemporaryDirectory(prefix=".lext-build-") as tmp:
            candidate = Path(tmp) / artifact.name
            create_publication_archive(source, candidate)
            data = candidate.read_bytes()
            if not artifact.exists():
                artifact.write_bytes(data)
            elif artifact.read_bytes() != data:
                drifted.append(f"{name} {version}")

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

    for item in drifted:
        print(f"warning: {item} differs from the published package; kept the "
              f"published bytes. Bump the version to release the change.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
