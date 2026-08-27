"""Generic extension lifecycle: install, uninstall, pack, scaffold.

Enterprise (signed, official) and community (hash-approved) extensions share
this infrastructure.  The difference is the trust tier, not the mechanics of
putting files on disk and loading them.

Three installation sources are supported:

  * **Local directory** -- ``/extensions install ./my-ext``
  * **Local archive**  -- ``/extensions install ./my-ext.lext``
  * **URL**             -- ``/extensions install https://example.com/my-ext.lext``

A ``.lext`` file is a ZIP archive with a ``.lext`` extension whose contents
are an extension directory (``extension.json`` + ``main.py`` + extras).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import paths
import trust_store

try:
    from version import __version__ as _cli_version
except Exception:
    _cli_version = "0.0.0"

#: Regex for valid extension names (same rule as extension_runtime).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: Fields that ``_validate_manifest`` checks for schemaVersion 2.
_REQUIRED_V2 = ("name", "version", "entrypoint")

#: The trust-store kind prefix for extensions managed by this module.
_TRUST_KIND = "runtime"

#: Archive extension.
ARCHIVE_SUFFIX = ".lext"

COMMUNITY_REGISTRY_ORIGIN = os.environ.get(
    "LAINTAS_COMMUNITY_EXTENSION_ORIGIN",
    "https://cli.laintas.com").rstrip("/")
OFFICIAL_REGISTRY_URL = os.environ.get(
    "LAINTAS_OFFICIAL_EXTENSION_REGISTRY",
    "https://cli.laintas.com/extensions/official-registry.json")
MAX_REMOTE_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_ARCHIVE_FILES = 200
MAX_UNPACKED_BYTES = 20 * 1024 * 1024
_COMMUNITY_ID = re.compile(
    r"^@([a-z0-9][a-z0-9-]{1,38}[a-z0-9])/"
    r"([a-z0-9][a-z0-9-]{0,62}[a-z0-9])$")


def _escape_markup(value: Any) -> str:
    try:
        from rich.markup import escape
        return escape(str(value))
    except Exception:
        return str(value).replace("[", "\\[")


# ── result type ────────────────────────────────────────────────────────────

@dataclass
class InstallResult:
    ok: bool
    message: str
    name: str = ""
    version: str = ""
    integrity: str = ""
    source: str = ""


# ── manifest helpers ───────────────────────────────────────────────────────

def read_manifest(directory: Path) -> dict:
    """Read and parse ``extension.json`` from *directory*."""
    path = directory / "extension.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(value: dict, directory_name: str = "") -> list[str]:
    """Return a list of validation errors (empty list = valid).

    Supports both schemaVersion 1 (legacy, e.g. enterprise) and 2.
    """
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["manifest is not a JSON object"]
    sv = value.get("schemaVersion")
    if sv not in (1, 2):
        errors.append(f"unsupported schemaVersion: {sv!r} (expected 1 or 2)")
        return errors
    name = value.get("name")
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        errors.append("name must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    if directory_name and name != directory_name:
        errors.append(f"manifest name ({name!r}) must match directory name ({directory_name!r})")
    if value.get("entrypoint", "main.py") != "main.py":
        errors.append("entrypoint must be 'main.py'")
    if sv == 2:
        for field_name in _REQUIRED_V2:
            if not value.get(field_name):
                errors.append(f"missing required field: {field_name}")
        version = value.get("version", "")
        if not re.match(r"^\d+\.\d+\.\d+", str(version)):
            errors.append("version must look like semver (e.g. 1.0.0)")
    prefix = value.get("toolPrefix")
    if prefix is not None:
        if not isinstance(prefix, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,15}\.", prefix):
            errors.append("toolPrefix must look like 'org.' (lower-case, dot-terminated)")
    return errors


def _version_tuple(value: str) -> tuple:
    """Compare versions numerically where possible, textually where not."""
    parts = []
    for chunk in str(value or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append((0, int(digits)) if digits == chunk and digits
                     else (1, chunk))
    return tuple(parts)


def check_compatibility(manifest: dict) -> Optional[str]:
    """Return an error message if the extension is incompatible, else None."""
    min_ver = str(manifest.get("minCliVersion") or "")
    if min_ver and _version_tuple(min_ver) > _version_tuple(_cli_version):
        return (f"Extension requires laintas-cli >= {min_ver}, "
                f"but this is v{_cli_version}.")
    return None


# ── integrity ──────────────────────────────────────────────────────────────

def compute_integrity(directory: Path) -> str:
    """SHA-256 of every file in the extension, sorted by relative path.

    This is a content fingerprint, not a single-file hash: it changes when any
    file in the extension changes, which is what trust-store needs to detect
    modification after approval.
    """
    hashes: list[tuple[str, str]] = []
    for file_path in sorted(directory.rglob("*")):
        if file_path.is_file() and not file_path.is_symlink():
            rel = file_path.relative_to(directory)
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            hashes.append((str(rel), digest))
    combined = "\n".join(f"{rel}:{digest}" for rel, digest in hashes)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _related_files(directory: Path) -> tuple[Path, ...]:
    """All .py files besides main.py, for trust-store related_paths."""
    return tuple(
        p for p in sorted(directory.rglob("*.py"))
        if p.is_file() and not p.is_symlink() and p.name != "main.py"
    )


# ── archive helpers ────────────────────────────────────────────────────────

def _safe_extract_zip(data: bytes, target: Path) -> None:
    """Extract a ZIP archive into *target*, refusing path-traversal entries."""
    root = target.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise RuntimeError(
                f"Extension archive contains more than {MAX_ARCHIVE_FILES} entries.")
        if sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
            raise RuntimeError("Extension archive exceeds the unpacked size limit.")
        for entry in archive.infolist():
            destination = (target / entry.filename).resolve()
            if root not in destination.parents and destination != root:
                raise RuntimeError(f"Unsafe archive path: {entry.filename}")
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, open(destination, "wb") as output:
                    shutil.copyfileobj(source, output)


def create_archive(source_dir: Path, output: Path) -> Path:
    """Pack *source_dir* into a ``.lext`` (ZIP) archive at *output*."""
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(source_dir)
                archive.write(file_path, arcname)
    return output


def create_publication_archive(source_dir: Path, output: Path) -> Path:
    """Pack source without local install provenance, CLI state, or transient files."""
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if (not file_path.is_file() or file_path.is_symlink()
                    or "__pycache__" in file_path.parts
                    or ".laintas" in file_path.parts
                    or file_path.suffix in {".pyc", ".pyo"}):
                continue
            relative = file_path.relative_to(source_dir)
            if relative.as_posix() == "extension.json":
                manifest = read_manifest(source_dir)
                manifest.pop("install", None)
                data = (json.dumps(manifest, indent=2, ensure_ascii=False)
                        + "\n").encode("utf-8")
            else:
                data = file_path.read_bytes()
            # A publication hash must describe content, not the time it was
            # packed. Fixed metadata makes repeated builds byte-identical.
            info = zipfile.ZipInfo(relative.as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output


def _fetch_registry_json(url: str) -> dict:
    """Fetch a bounded marketplace registry without trusting its shape."""
    import requests

    try:
        with requests.get(url, timeout=15, stream=True) as response:
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_REGISTRY_BYTES:
                raise RuntimeError("Extension registry exceeds the 1 MB limit.")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_REGISTRY_BYTES:
                    raise RuntimeError("Extension registry exceeds the 1 MB limit.")
                chunks.append(chunk)
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Extension registry returned invalid JSON.") from exc
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Extension registry returned an invalid response.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("extensions"), list):
        raise RuntimeError("Extension registry has an invalid document shape.")
    return value


# ── scaffold template ──────────────────────────────────────────────────────

_SCAFFOLD_MANIFEST = """\
{{
  "schemaVersion": 2,
  "name": "{name}",
  "version": "0.1.0",
  "entrypoint": "main.py",
  "description": "{description}",
  "author": {{
    "name": "Your Name"
  }},
  "license": "MIT",
  "toolPrefix": "{prefix}"
}}
"""

_SCAFFOLD_MAIN = '''\
"""Extension: {name}

{description}
"""
from __future__ import annotations


def setup(ctx) -> None:
    """Called when the extension is loaded.

    Register commands, tools, and loop handlers here.  See:
      ctx.register_command(name, handler, description="", subcommands=[])
      ctx.register_tool(tool)
      ctx.register_loop(handler)
      ctx.backend.chat(message, system_prompt="")
    """
    pass


def teardown() -> None:
    """Called when the extension is unloaded."""
    pass
'''

_SCAFFOLD_README = """\
# {name}

{description}

## Development

Edit `main.py` and `extension.json`, then install locally:

```
/extensions install ./{name}
```
"""


# ── extension manager ──────────────────────────────────────────────────────

class ExtensionManager:
    """Install, uninstall, list, pack, scaffold, and trust extensions."""

    def __init__(self, runtime=None, console=None, community_scanner=None):
        self._rt = runtime
        self._console = console
        self._community_scanner = community_scanner

    # ── output helper ──────────────────────────────────────────────────
    def _print(self, message: str = "") -> None:
        if self._console is not None:
            self._console.print(message)
        else:
            print(message)

    def _confirm(self, prompt: str) -> bool:
        """Ask the user for a yes/no confirmation."""
        if self._console is not None and hasattr(self._console, "input"):
            try:
                # markup=False: "[y/N]" must not be parsed as a Rich style tag
                answer = self._console.input(
                    f"{prompt} [y/N] ", markup=False).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return answer in ("y", "yes")
        return False

    # ── source detection ───────────────────────────────────────────────
    @staticmethod
    def detect_source(source: str) -> str:
        """Classify an install source string."""
        if source.startswith(("http://", "https://")):
            return "url"
        path = Path(source)
        if path.is_dir():
            return "local-dir"
        if path.is_file() and path.suffix == ARCHIVE_SUFFIX:
            return "local-archive"
        # Bare name -> treat as marketplace slug (future)
        return "marketplace"

    # ── public API ─────────────────────────────────────────────────────

    def install(self, source: str, *, global_install: bool = False,
                force: bool = False) -> InstallResult:
        """Install an extension from any supported source.

        Returns an :class:`InstallResult`.
        """
        kind = self.detect_source(source)
        try:
            if kind == "local-dir":
                return self._install_from_directory(
                    Path(source), global_install=global_install, force=force)
            elif kind == "local-archive":
                return self._install_from_archive(
                    Path(source), global_install=global_install, force=force)
            elif kind == "url":
                return self._install_from_url(
                    source, global_install=global_install, force=force)
            else:
                if source.startswith("@"):
                    return self._install_community(
                        source, global_install=global_install, force=force)
                return self._install_official(
                    source, global_install=global_install, force=force)
        except Exception as exc:
            return InstallResult(ok=False, message=str(exc))

    def uninstall(self, name: str) -> bool:
        """Remove an extension by name.  Returns True if something was removed."""
        if not _SAFE_NAME.fullmatch(name or ""):
            return False
        if self._rt is not None:
            self._rt.unload(name)
        trust_store.revoke_extension(_TRUST_KIND, name)
        removed = False
        for root in (paths.extensions_dir(), paths.global_extensions_dir()):
            target = root / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed = True
        return removed

    def list_installed(self) -> list[dict]:
        """List all installed extensions with their metadata and trust status."""
        results: list[dict] = []
        for scope, root in [("project", paths.extensions_dir()),
                            ("global", paths.global_extensions_dir())]:
            if not root.is_dir():
                continue
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                manifest_path = directory / "extension.json"
                if not manifest_path.is_file():
                    continue
                try:
                    manifest = read_manifest(directory)
                except (OSError, ValueError):
                    results.append({
                        "name": directory.name, "scope": scope,
                        "version": "", "description": "(invalid manifest)",
                        "trusted": False, "source": "unknown",
                    })
                    continue
                entrypoint = directory / "main.py"
                related = _related_files(directory)
                status = trust_store.extension_status(
                    _TRUST_KIND, directory.name, entrypoint,
                    related_paths=related)
                install_meta = manifest.get("install") or {}
                results.append({
                    "name": manifest.get("name") or directory.name,
                    "scope": scope,
                    "version": manifest.get("version", ""),
                    "description": manifest.get("description", ""),
                    "trusted": status.get("trusted", False),
                    "source": install_meta.get("source", "unknown"),
                    "trusted_by": install_meta.get("trustedBy", ""),
                    "path": str(directory),
                })
        return results

    def list_available(self, source: str = "all", query: str = "") -> list[dict]:
        """Return normalized official and community marketplace entries."""
        source = str(source or "all").strip().lower()
        if source not in {"all", "official", "community"}:
            raise ValueError("Source must be all, official, or community.")
        needle = str(query or "").strip().casefold()
        results: list[dict] = []

        if source in {"all", "official"}:
            registry = _fetch_registry_json(OFFICIAL_REGISTRY_URL)
            for raw in registry.get("extensions", []):
                if not isinstance(raw, dict):
                    continue
                identifier = str(raw.get("id") or "")
                if not identifier.startswith("laintas/"):
                    continue
                results.append({
                    "id": identifier,
                    "name": str(raw.get("name") or identifier.split("/", 1)[-1]),
                    "version": str(raw.get("version") or ""),
                    "description": str(raw.get("description") or ""),
                    "source": "official",
                })

        if source in {"all", "community"}:
            url = f"{COMMUNITY_REGISTRY_ORIGIN}/api/extensions/community?limit=200"
            registry = _fetch_registry_json(url)
            latest: dict[str, dict] = {}
            for candidate in registry.get("extensions", []):
                if not isinstance(candidate, dict):
                    continue
                identifier = str(candidate.get("id") or "")
                if not _COMMUNITY_ID.fullmatch(identifier):
                    continue
                previous = latest.get(identifier)
                try:
                    published = float(candidate.get("publishedAt") or 0)
                    previous_published = float(
                        (previous or {}).get("publishedAt") or 0)
                except (TypeError, ValueError):
                    published = previous_published = 0
                if previous is None or published > previous_published:
                    latest[identifier] = candidate
            for raw in latest.values():
                if not isinstance(raw, dict):
                    continue
                identifier = str(raw.get("id") or "")
                manifest = raw.get("manifest")
                manifest = manifest if isinstance(manifest, dict) else {}
                results.append({
                    "id": identifier,
                    "name": str(manifest.get("displayName") or raw.get("slug")
                                or identifier.split("/", 1)[-1]),
                    "version": str(raw.get("version") or ""),
                    "description": str(manifest.get("summary")
                                       or manifest.get("description") or ""),
                    "source": "community",
                })

        if needle:
            results = [item for item in results if needle in " ".join((
                item["id"], item["name"], item["description"],
                item["version"], item["source"],
            )).casefold()]
        return sorted(results, key=lambda item: (
            item["source"] != "official", item["id"].casefold(),
            _version_tuple(item["version"])), reverse=False)

    def trust(self, name: str) -> tuple[bool, str]:
        """Approve an extension's current hashes in trust_store."""
        directory = self._find_directory(name)
        if directory is None:
            return False, f"Extension {name!r} is not installed."
        entrypoint = directory / "main.py"
        if not entrypoint.is_file():
            return False, f"No main.py in {directory}"
        related = _related_files(directory)
        result = trust_store.trust_extension(
            _TRUST_KIND, name, entrypoint, related_paths=related)
        return True, f"Trusted {name} ({result.get('sha256', '')[:12]})."

    def untrust(self, name: str) -> tuple[bool, str]:
        """Revoke trust for an extension."""
        removed = trust_store.revoke_extension(_TRUST_KIND, name)
        if removed:
            return True, f"Trust revoked for {name}."
        return False, f"Extension {name!r} was not trusted."

    def info(self, name: str) -> Optional[dict]:
        """Return detailed information about an installed extension."""
        directory = self._find_directory(name)
        if directory is None:
            return None
        manifest = read_manifest(directory)
        entrypoint = directory / "main.py"
        related = _related_files(directory)
        status = trust_store.extension_status(
            _TRUST_KIND, name, entrypoint, related_paths=related)
        integrity = compute_integrity(directory)
        return {
            "manifest": manifest,
            "directory": str(directory),
            "integrity": integrity,
            "trust": status,
            "files": sorted(p.name for p in directory.rglob("*") if p.is_file()),
        }

    def pack(self, name: str, output: Optional[Path] = None) -> Path:
        """Pack an installed extension into a ``.lext`` archive."""
        directory = self._find_directory(name)
        if directory is None:
            raise RuntimeError(f"Extension {name!r} is not installed.")
        if output is None:
            output = Path.cwd() / f"{name}.lext"
        create_archive(directory, output)
        return output

    def publish(self, name: str, shared_storage) -> dict:
        """Package an installed extension into Laintas Storage and publish it."""
        directory = self._find_directory(name)
        if directory is None:
            raise RuntimeError(f"Extension {name!r} is not installed.")
        manifest = read_manifest(directory)
        errors = validate_manifest(manifest, directory.name)
        if errors:
            raise RuntimeError("Manifest validation failed:\n  " + "\n  ".join(errors))
        slug = str(manifest["name"])
        version = str(manifest["version"])
        remote_folder = f"Extensions/{slug}"
        with tempfile.TemporaryDirectory(prefix=".lext-publish-") as tmp:
            archive_path = Path(tmp) / "extension.lext"
            publication_path = Path(tmp) / "publish.json"
            create_publication_archive(directory, archive_path)
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            publication = {
                "schemaVersion": 1,
                "slug": slug,
                "version": version,
                "artifact": "extension.lext",
                "sha256": digest,
                "displayName": manifest.get("displayName") or slug,
                "summary": manifest.get("description", ""),
                "minCliVersion": manifest.get("minCliVersion", ""),
                "capabilities": manifest.get("capabilities") or [],
                "visibility": "public",
            }
            publication_path.write_text(
                json.dumps(publication, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            shared_storage.push_file(
                str(archive_path), f"{remote_folder}/extension.lext")
            # Uploaded last: readers never observe a marker for an incomplete package.
            shared_storage.push_file(
                str(publication_path), f"{remote_folder}/publish.json")
            return shared_storage.publish_extension(remote_folder)

    def create(self, name: str, directory: Optional[Path] = None,
               description: str = "") -> Path:
        """Scaffold a new extension directory.

        *directory* defaults to ``./<name>``.  The directory must not exist or
        must be empty.
        """
        if not _SAFE_NAME.fullmatch(name):
            raise RuntimeError(
                f"Invalid extension name: {name!r}\n"
                f"  Must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}")
        target = directory or Path.cwd() / name
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(f"Directory is not empty: {target}")
        target.mkdir(parents=True, exist_ok=True)
        prefix = name.replace("-", "")[:8] + "."
        manifest_text = _SCAFFOLD_MANIFEST.format(
            name=name, description=description or "A laintas-cli extension.",
            prefix=prefix)
        (target / "extension.json").write_text(manifest_text, encoding="utf-8")
        (target / "main.py").write_text(
            _SCAFFOLD_MAIN.format(name=name, description=description or "A laintas-cli extension."),
            encoding="utf-8")
        (target / "README.md").write_text(
            _SCAFFOLD_README.format(name=name, description=description or "A laintas-cli extension."),
            encoding="utf-8")
        return target

    # ── internals ──────────────────────────────────────────────────────

    @staticmethod
    def _find_directory(name: str) -> Optional[Path]:
        """Locate an extension directory in project or global roots."""
        for root in (paths.extensions_dir(), paths.global_extensions_dir()):
            candidate = root / name
            if (candidate / "extension.json").is_file():
                return candidate
        return None

    def _install_target(self, name: str, global_install: bool) -> Path:
        root = (paths.global_extensions_dir() if global_install
                else paths.extensions_dir())
        return root / name

    def _install_staged(self, staging: Path, *, global_install: bool,
                        force: bool, trust_mode: str,
                        source_label: str) -> InstallResult:
        """Validate, confirm, and install from a staging directory."""
        # 1. Validate manifest
        manifest_path = staging / "extension.json"
        if not manifest_path.is_file():
            return InstallResult(ok=False,
                                 message="No extension.json found in the package.")
        try:
            manifest = read_manifest(staging)
        except ValueError as exc:
            return InstallResult(ok=False, message=f"Invalid manifest: {exc}")

        errors = validate_manifest(manifest, "")
        if errors:
            return InstallResult(
                ok=False,
                message="Manifest validation failed:\n  "
                        + "\n  ".join(errors))

        name = manifest["name"]
        version = manifest.get("version", "")
        entrypoint = staging / "main.py"
        if not entrypoint.is_file() or entrypoint.is_symlink():
            return InstallResult(ok=False,
                                 message="main.py entrypoint is missing.")

        # 2. Check CLI version compatibility
        compat = check_compatibility(manifest)
        if compat:
            return InstallResult(ok=False, message=compat)

        # 3. Determine install location
        target = self._install_target(name, global_install)

        # 4. Check existing installation
        if target.exists() and not force:
            try:
                existing = read_manifest(target)
                existing_ver = existing.get("version", "")
                if existing_ver and _version_tuple(version) <= _version_tuple(existing_ver):
                    return InstallResult(
                        ok=False,
                        message=f"{name} v{existing_ver} is already installed.\n"
                                f"  Use --force to replace it.")
            except (OSError, ValueError):
                pass  # Existing install is broken; proceed with replacement.

        # 5. Compute integrity hash
        integrity = compute_integrity(staging)

        # 6. Trust confirmation
        if trust_mode == "user-confirm":
            desc = _escape_markup(manifest.get("description", "(no description)"))
            author = (manifest.get("author") or {})
            author_name = _escape_markup(
                author.get("name", "unknown") if isinstance(author, dict) else author)
            self._print(
                f"[bold]Extension:[/bold] {name} v{version}\n"
                f"  [dim]description : {desc}[/dim]\n"
                f"  [dim]author       : {author_name}[/dim]\n"
                f"  [dim]integrity    : {integrity[:16]}[/dim]\n"
                f"  [dim]source       : {_escape_markup(source_label)}[/dim]\n"
                f"  [yellow]This extension will execute Python code with your "
                f"permissions.[/yellow]")
            if not self._confirm("Install and trust this extension?"):
                return InstallResult(ok=False, message="Installation cancelled.")

        # 7. Atomic install: keep the old tree until the new setup succeeds.
        old_was_trusted = False
        if target.exists():
            try:
                old_entry = target / "main.py"
                old_was_trusted = bool(trust_store.extension_status(
                    _TRUST_KIND, name, old_entry,
                    related_paths=_related_files(target)).get("trusted"))
            except Exception:
                old_was_trusted = False
        if self._rt is not None:
            self._rt.unload(name)
        backup = target.with_name(f".{target.name}.old")
        shutil.rmtree(backup, ignore_errors=True)
        if target.exists():
            os.replace(target, backup)
        try:
            shutil.copytree(staging, target)
        except Exception:
            # Roll back
            if backup.exists():
                shutil.rmtree(target, ignore_errors=True)
                os.replace(backup, target)
            raise

        # 8. Write install metadata into manifest
        try:
            installed_manifest = read_manifest(target)
            installed_manifest["install"] = {
                "source": source_label,
                "installedAt": _now_iso(),
                "integrity": integrity,
                "trustedBy": trust_mode,
            }
            (target / "extension.json").write_text(
                json.dumps(installed_manifest, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass  # Non-fatal: the extension still works without install metadata.

        # 9. Record trust for explicitly confirmed executable code.
        if trust_mode in {"user-confirm", "community-ai-confirm"}:
            self.trust(name)

        # 10. Load
        if self._rt is not None:
            ok, msg = self._rt.load(name)
            if not ok:
                self._rt.unload(name)
                trust_store.revoke_extension(_TRUST_KIND, name)
                shutil.rmtree(target, ignore_errors=True)
                if backup.exists():
                    os.replace(backup, target)
                    if old_was_trusted:
                        self.trust(name)
                    self._rt.load(name)
                return InstallResult(
                    ok=False, name=name, version=version, integrity=integrity,
                    source=source_label,
                    message=f"Extension failed to load and was rolled back: {msg}")
        shutil.rmtree(backup, ignore_errors=True)
        return InstallResult(
            ok=True, name=name, version=version, integrity=integrity,
            source=source_label,
            message=f"Installed {name} v{version} ({integrity[:12]}).")

    def _install_from_directory(self, source: Path, *,
                                global_install: bool,
                                force: bool) -> InstallResult:
        """Install from a local directory."""
        if not source.is_dir():
            return InstallResult(ok=False, message=f"Not a directory: {source}")
        return self._install_staged(
            source, global_install=global_install, force=force,
            trust_mode="user-confirm",
            source_label=f"local:{source}")

    def _install_from_archive(self, source: Path, *,
                              global_install: bool,
                              force: bool) -> InstallResult:
        """Install from a local .lext (ZIP) archive."""
        if not source.is_file():
            return InstallResult(ok=False, message=f"Not a file: {source}")
        data = source.read_bytes()
        with tempfile.TemporaryDirectory(prefix=".lext-install-") as tmp:
            staging = Path(tmp) / "content"
            _safe_extract_zip(data, staging)
            # If the archive has a single top-level directory, descend into it.
            entries = [p for p in staging.iterdir() if not p.name.startswith(".")]
            if len(entries) == 1 and entries[0].is_dir():
                staging = entries[0]
            return self._install_staged(
                staging, global_install=global_install, force=force,
                trust_mode="user-confirm",
                source_label=f"archive:{source}")

    def _install_from_url(self, url: str, *,
                          global_install: bool,
                          force: bool) -> InstallResult:
        """Download and install from a URL."""
        import requests
        self._print(f"[dim]Downloading {url}…[/dim]")
        with requests.get(url, timeout=120, stream=True) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_content(256 * 1024):
                size += len(chunk)
                if size > MAX_REMOTE_ARCHIVE_BYTES:
                    raise RuntimeError("Extension download exceeds the 10 MB limit.")
                chunks.append(chunk)
            data = b"".join(chunks)
        with tempfile.TemporaryDirectory(prefix=".lext-install-") as tmp:
            staging = Path(tmp) / "content"
            _safe_extract_zip(data, staging)
            entries = [p for p in staging.iterdir() if not p.name.startswith(".")]
            if len(entries) == 1 and entries[0].is_dir():
                staging = entries[0]
            return self._install_staged(
                staging, global_install=global_install, force=force,
                trust_mode="user-confirm",
                source_label=f"url:{url}")

    def _install_community(self, identifier: str, *,
                           global_install: bool,
                           force: bool) -> InstallResult:
        match = _COMMUNITY_ID.fullmatch(identifier)
        if not match:
            return InstallResult(
                ok=False,
                message="Community extension IDs must use @author/slug.")
        if self._community_scanner is None:
            return InstallResult(
                ok=False,
                message="AI source review is unavailable; community installation was stopped.")
        author, slug = match.groups()
        import requests
        info_url = (f"{COMMUNITY_REGISTRY_ORIGIN}/api/extensions/community/"
                    f"{quote(author)}/{quote(slug)}")
        info_response = requests.get(info_url, timeout=30)
        info_response.raise_for_status()
        info = info_response.json()
        version = str(info.get("version") or "")
        download_response = requests.post(
            info_url + "/download", timeout=30)
        download_response.raise_for_status()
        download = download_response.json()
        archive_url = str(download.get("downloadUrl") or "")
        expected_sha = str(download.get("sha256") or info.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise RuntimeError("Community registry returned an invalid artifact hash.")
        self._print(f"[dim]Downloading {identifier} v{version}…[/dim]")
        with requests.get(archive_url, timeout=120, stream=True) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_content(256 * 1024):
                size += len(chunk)
                if size > MAX_REMOTE_ARCHIVE_BYTES:
                    raise RuntimeError("Extension download exceeds the 10 MB limit.")
                chunks.append(chunk)
        data = b"".join(chunks)
        if not hashlib.sha256(data).hexdigest() == expected_sha:
            raise RuntimeError("Downloaded extension does not match the published SHA-256.")
        with tempfile.TemporaryDirectory(prefix=".lext-community-") as tmp:
            staging = Path(tmp) / "content"
            _safe_extract_zip(data, staging)
            entries = [p for p in staging.iterdir() if not p.name.startswith(".")]
            if len(entries) == 1 and entries[0].is_dir():
                staging = entries[0]
            manifest = read_manifest(staging)
            if manifest.get("name") != slug or str(manifest.get("version")) != version:
                raise RuntimeError("Downloaded package identity does not match the registry.")
            report = self._community_scanner(staging)
            risk = str(report.get("risk") or "").lower()
            if risk not in {"low", "medium", "high", "critical"}:
                raise RuntimeError("AI source review returned an invalid risk level.")
            self._print(
                "[bold yellow]COMMUNITY EXTENSION — NOT REVIEWED BY LAINTAS[/bold yellow]\n\n"
                f"  [bold]Extension:[/bold] {identifier} v{version}\n"
                f"  [bold]Artifact:[/bold]  {expected_sha[:16]}\n"
                f"  [bold]Risk:[/bold]      {risk.upper()}\n"
                f"  [bold]Review:[/bold]    {_escape_markup(report.get('summary', ''))}\n\n"
                "  [yellow]This extension executes Python code with your user permissions.\n"
                "  AI-assisted analysis cannot guarantee that the code is safe.[/yellow]")
            for finding in (report.get("findings") or [])[:12]:
                severity = str(finding.get("severity", "medium")).upper()
                self._print(
                    f"  [bold]{severity}[/bold] "
                    f"{_escape_markup(finding.get('file', 'unknown'))}:"
                    f"{finding.get('line', 1)} — "
                    f"{_escape_markup(finding.get('description', ''))}")
            if risk == "critical":
                return InstallResult(
                    ok=False, message="Critical-risk community extensions cannot be installed.")
            prompt = (f"Type {identifier} to install this high-risk extension:"
                      if risk == "high" else
                      f"Install and trust {identifier}?")
            if risk == "high":
                try:
                    answer = self._console.input(f"{prompt} ", markup=False).strip() \
                        if self._console is not None else ""
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                confirmed = answer == identifier
            else:
                confirmed = self._confirm(prompt)
            if not confirmed:
                return InstallResult(ok=False, message="Installation cancelled.")
            return self._install_staged(
                staging, global_install=global_install, force=force,
                trust_mode="community-ai-confirm",
                source_label=f"community:{identifier}@{version}")

    def _install_official(self, identifier: str, *,
                          global_install: bool,
                          force: bool) -> InstallResult:
        official_id = identifier if "/" in identifier else f"laintas/{identifier}"
        import requests
        response = requests.get(OFFICIAL_REGISTRY_URL, timeout=30)
        response.raise_for_status()
        registry = response.json()
        item = next((entry for entry in registry.get("extensions", [])
                     if entry.get("id") == official_id), None)
        if item is None:
            return InstallResult(
                ok=False,
                message=f"No official extension named {official_id!r} was found. "
                        "Community extensions must use @author/slug.")
        url = str(item.get("url") or "")
        expected_sha = str(item.get("sha256") or "").lower()
        if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise RuntimeError("Official extension registry entry is invalid.")
        with requests.get(url, timeout=120, stream=True) as download:
            download.raise_for_status()
            chunks = []
            size = 0
            for chunk in download.iter_content(256 * 1024):
                size += len(chunk)
                if size > MAX_REMOTE_ARCHIVE_BYTES:
                    raise RuntimeError("Extension download exceeds the 10 MB limit.")
                chunks.append(chunk)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != expected_sha:
            raise RuntimeError("Official extension does not match the registry SHA-256.")
        with tempfile.TemporaryDirectory(prefix=".lext-official-") as tmp:
            staging = Path(tmp) / "content"
            _safe_extract_zip(data, staging)
            entries = [p for p in staging.iterdir() if not p.name.startswith(".")]
            if len(entries) == 1 and entries[0].is_dir():
                staging = entries[0]
            manifest = read_manifest(staging)
            if manifest.get("name") != official_id.split("/", 1)[1]:
                raise RuntimeError("Official package identity does not match the registry.")
            return self._install_staged(
                staging, global_install=global_install, force=force,
                trust_mode="user-confirm",
                source_label=f"official:{official_id}@{item.get('version', '')}")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
