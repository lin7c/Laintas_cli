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

    def __init__(self, runtime=None, console=None):
        self._rt = runtime
        self._console = console

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
                answer = self._console.input(f"{prompt} [y/N] ").strip().lower()
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
                return InstallResult(
                    ok=False,
                    message=f"Marketplace install is not yet available. "
                            f"Use a local path, .lext file, or URL.\n"
                            f"  Source: {source}")
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

        errors = validate_manifest(manifest, staging.name)
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
            desc = manifest.get("description", "(no description)")
            author = (manifest.get("author") or {})
            author_name = author.get("name", "unknown") if isinstance(author, dict) else str(author)
            self._print(
                f"[bold]Extension:[/bold] {name} v{version}\n"
                f"  [dim]description : {desc}[/dim]\n"
                f"  [dim]author       : {author_name}[/dim]\n"
                f"  [dim]integrity    : {integrity[:16]}[/dim]\n"
                f"  [dim]source       : {source_label}[/dim]\n"
                f"  [yellow]This extension will execute Python code with your "
                f"permissions.[/yellow]")
            if not self._confirm("Install and trust this extension?"):
                return InstallResult(ok=False, message="Installation cancelled.")

        # 7. Atomic install: unload old -> swap directories -> load new
        if self._rt is not None:
            self._rt.unload(name)
        backup = target.with_name(f".{target.name}.old")
        shutil.rmtree(backup, ignore_errors=True)
        if target.exists():
            os.replace(target, backup)
        try:
            shutil.copytree(staging, target)
            shutil.rmtree(backup, ignore_errors=True)
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

        # 9. Record trust (for user-confirm mode)
        if trust_mode == "user-confirm":
            self.trust(name)

        # 10. Load
        if self._rt is not None:
            ok, msg = self._rt.load(name)
            if not ok:
                return InstallResult(
                    ok=False, name=name, version=version, integrity=integrity,
                    source=source_label,
                    message=f"Installed but failed to load: {msg}")
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
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        data = response.content
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


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
