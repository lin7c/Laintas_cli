"""Authenticated installer for the separately distributed Enterprise edition.

This module intentionally contains no organization, seat, asset or policy
implementation. It only obtains a signed package from the logged-in user's
organization and installs it.

It therefore ships with the public CLI, and must stay that way: it holds a
*public* verification key and no secrets, and the entry point is useless without
a session and a membership the server checks on its side. What stays private is
the Enterprise edition it downloads, not the code that fetches it.

What it downloads is an **extension**, not a program. Enterprise used to be a
frozen executable that re-launched the public CLI as a library; it is now a
package that `extension_runtime` loads into the running process, where it
registers `/org`, its tools, the organisation's shared skills and — through
`policy._apply_org_policy` — the organisation's rules. The member keeps using
laintas-cli; the organisation layer appears inside it.

That change also removed a failure mode worth remembering: a frozen binary
carries its own interpreter, so it inherited the build machine's glibc and died
on startup for every customer on an older distribution. An extension is source.

Releases are served from enterprise.laintas.com only. Nothing here follows a
download URL to another origin — see `_download_payload`.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

import requests

import paths
from release import (download, extract_archive, verify_ed25519, verify_sha256)

#: Directory name of the organisation extension, and the identity it is known by
#: to `extension_runtime` and to managed skills. The manifest inside the package
#: must declare the same name.
EXTENSION_NAME = "laintas-org"

#: The `platform` a member asks for. The Enterprise CLI used to be built per
#: architecture; the extension is pure source and there is exactly one build, so
#: this constant stands where `release.platform_id()` used to.
EXTENSION_PLATFORM = "extension"

API_BASE = os.environ.get(
    "LAINTAS_ENTERPRISE_API", "https://enterprise.laintas.com").rstrip("/")

# ── Ed25519 public key (DER) that signs every enterprise release ───────────
RELEASE_PUBLIC_KEY_DER = (
    b"\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00"
    b"\x52\xec\xb1\x0a\x82\xbb\xa0\x28\xd9\xdd\xd8\xae"
    b"\x49\x9e\xdb\x8f\x58\x11\x87\x96\x16\xfb\x34\x79"
    b"\xd1\x83\x40\x54\x3c\xdc\xc9\x9a"
)

# ── session ────────────────────────────────────────────────────────────────

def _authenticated_session() -> requests.Session:
    try:
        data = json.loads(paths.SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError(
            "Sign in with /login before installing Enterprise CLI.")
    session = requests.Session()
    session.headers.update(data.get("headers") or {})
    session.cookies.update(data.get("cookies") or {})
    session.headers["User-Agent"] = "laintas-cli-enterprise-installer"
    return session


# ── install paths ──────────────────────────────────────────────────────────

def extension_path() -> Path:
    """Where the organisation extension lives.

    Machine-wide rather than project-local: an organisation's rules do not stop
    applying because the member changed directory.
    """
    return paths.global_extensions_dir() / EXTENSION_NAME


def is_installed() -> bool:
    return (extension_path() / "extension.json").is_file()


def installed_version() -> str:
    """The version currently on disk, or "" if nothing is installed."""
    try:
        data = json.loads(
            (extension_path() / "extension.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except (OSError, ValueError):
        return ""


def _version_tuple(value: str) -> tuple:
    """Compare versions numerically where possible, textually where not."""
    parts = []
    for chunk in str(value or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append((0, int(digits)) if digits == chunk and digits
                     else (1, chunk))
    return tuple(parts)


def _refuse_downgrade(offered: str, force: bool) -> None:
    """Refuse to replace an installed package with an older one.

    The signature proves Laintas published this package. It does not prove it is
    the *current* one — a signature stays valid forever, so a release store that
    was rolled back, or served stale, hands out a genuinely signed package with
    whatever weaknesses the newer one fixed. The signed policy already defends
    against exactly this with `min_version`; the package that applies the policy
    had no equivalent.
    """
    if force:
        return
    held = installed_version()
    if not held or not offered:
        return
    if _version_tuple(offered) >= _version_tuple(held):
        return
    raise RuntimeError(
        f"Refusing to replace Laintas Enterprise v{held} with the older "
        f"v{offered}.\n"
        "  A signature proves who published a package, not that it is the "
        "current one.\n"
        "  If this downgrade is intended, run:  /v enterprise --force")


def gateway_path() -> Path:
    return paths.LAINTAS_HOME / "enterprise" / "gateway"


def _legacy_executable() -> Path:
    """The pre-extension frozen binary, kept only so it can be removed."""
    suffix = ".exe" if os.name == "nt" else ""
    return paths.LAINTAS_HOME / "enterprise" / "bin" / f"laintas-enterprise{suffix}"


# ── download + verify + install ────────────────────────────────────────────

def _fetch_release(endpoint: str, *, platform: Optional[str] = None,
                   log=print) -> dict:
    """Ask enterprise.laintas.com to describe a release, and for a ticket to it.

    Authorisation is the caller's Laintas session plus their *current*
    organisation membership — nothing is carried in a header the user could
    paste elsewhere. An invite token used to serve this purpose; it was the
    wrong credential, because joining an organisation and installing software
    are different rights with different lifetimes.
    """
    session = _authenticated_session()
    params = {"platform": platform} if platform else {}
    response = session.get(f"{API_BASE}{endpoint}", params=params, timeout=30)
    try:
        manifest = response.json()
    except ValueError:
        manifest = {"error": response.text[:300]}

    if not response.ok:
        raise RuntimeError(_explain(response.status_code, manifest))
    return manifest


#: What each refusal means, and what the user can actually do about it. The
#: server sends a stable `code`; the prose lives here so it can be translated
#: and reworded without touching the API.
_REFUSALS = {
    "not_in_org": (
        "You are not in an organisation yet.\n"
        "  · Have an admin send you an invite, then run:  /org join <token>\n"
        "  · Or open your own organisation at https://enterprise.laintas.com"),
    "entitlement_inactive": (
        "This organisation's Laintas Enterprise plan is not active.\n"
        "  The owner can renew it at https://laintas.com/pricing"),
    "admin_required": (
        "The gateway bundle is for organisation owners and admins.\n"
        "  Ask an admin to deploy it, or to promote your account."),
    "not_published": (
        "No Enterprise release has been published for this platform yet."),
    "wrong_host": (
        "Enterprise releases are served only from enterprise.laintas.com.\n"
        "  Unset LAINTAS_ENTERPRISE_API to use the default."),
    "client_too_old": (
        "This Laintas CLI is too old for Laintas Enterprise.\n"
        "  Enterprise now loads into the CLI instead of installing a separate\n"
        "  executable. Run:  /v update   then  /v enterprise"),
}


def _explain(status: int, manifest: dict) -> str:
    code = str(manifest.get("code") or "")
    if code in _REFUSALS:
        return _REFUSALS[code]
    return manifest.get("error") or f"Release request failed ({status})"


def _verify_enterprise(payload: bytes, manifest: dict) -> None:
    """SHA-256 + Ed25519 verification for enterprise releases."""
    verify_sha256(payload, manifest.get("sha256") or "",
                  label="Enterprise release")
    verify_ed25519(payload, manifest.get("signature") or "",
                   RELEASE_PUBLIC_KEY_DER,
                   label="Enterprise release")


def _download_payload(manifest: dict, session, log) -> bytes:
    """Fetch the bytes from the ticket URL, which is on enterprise.laintas.com.

    The session goes with the request: the ticket alone is not authorisation,
    and a ticket that leaked without the cookie is useless.
    """
    size = int(manifest.get("size_bytes") or 0)
    if size < 1 or size > 500 * 1024 * 1024:
        raise RuntimeError(
            "Enterprise release manifest contains an invalid size.")

    download_url = str(manifest.get("download_url") or "")
    if not download_url:
        raise RuntimeError("Enterprise release manifest has no download URL.")
    # The bytes must come from the same origin that authorised them. Following a
    # download URL to anywhere else would put the release — and the session sent
    # with it — wherever a compromised or misconfigured API said to.
    if not download_url.startswith(f"{API_BASE}/"):
        raise RuntimeError(
            f"Refusing a release download that points outside {API_BASE}.")

    log(f"[bold]Downloading enterprise release v{manifest.get('version')} "
        f"({_format_size(size)})…[/bold]")
    payload = download(download_url, label="enterprise release",
                       timeout=180, session=session)

    if len(payload) != size:
        raise RuntimeError("Enterprise release download is incomplete.")
    return payload


def _format_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ── public API ─────────────────────────────────────────────────────────────

def install_extension(log=print, runtime=None, force=False) -> tuple[Path, dict]:
    """Download, verify, unpack and load the organisation extension.

    Returns ``(installed_path, manifest)``.

    The previous install is replaced only once the new one is on disk and
    verified, and the running process picks it up immediately — an organisation
    that publishes a rule should not have to ask its members to restart.
    """
    release = _fetch_release("/api/org/download/cli",
                             platform=EXTENSION_PLATFORM, log=log)
    _refuse_downgrade(str(release.get("version") or ""), force)
    payload = _download_payload(release, _authenticated_session(), log)
    _verify_enterprise(payload, release)

    target = extension_path()
    staging = target.with_name(f".{target.name}.incoming")
    previous = target.with_name(f".{target.name}.previous")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        extract_archive(payload, staging)
        if not (staging / "extension.json").is_file():
            raise RuntimeError(
                "Enterprise package is missing extension.json — refusing it.")
        shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            os.replace(target, previous)
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # The frozen binary this replaced would otherwise sit around looking
    # installed; nothing launches it any more.
    _remove_legacy_executable()

    # Mark as Ed25519-verified so the trust gate in load() lets it through
    # without a separate /extensions trust step.
    _stamp_install_metadata(target, "ed25519", str(release.get("version") or ""))

    if runtime is not None:
        ok, message = runtime.load(EXTENSION_NAME)
        if not ok:
            # Loading is the only real proof the package works. Put the previous
            # one back rather than leaving the member with a broken org layer.
            shutil.rmtree(target, ignore_errors=True)
            if previous.exists():
                os.replace(previous, target)
                runtime.load(EXTENSION_NAME)
            raise RuntimeError(f"Enterprise package failed to load: {message}")

    shutil.rmtree(previous, ignore_errors=True)
    log(f"[green]Laintas Enterprise v{release['version']} enabled — try /org status[/green]")
    return target, release


def uninstall_extension(runtime=None) -> bool:
    """Remove the organisation extension. Returns True if something was removed.

    Organisation *rules* go with it, which is the point: this is how a member
    who has left, or who is troubleshooting, gets back to a plain CLI. The
    server still knows who they are — it decides what they may install next.
    """
    if runtime is not None:
        runtime.unload(EXTENSION_NAME)
    os.environ.pop("LAINTAS_POLICY_DIGEST", None)
    target = extension_path()
    existed = target.exists()
    shutil.rmtree(target, ignore_errors=True)
    _remove_legacy_executable()
    return existed


def _stamp_install_metadata(directory: Path, trusted_by: str,
                            version: str = "") -> None:
    """Write install.trustedBy into the extension's manifest.

    Called after the signed package is extracted to its final location, so
    the trust gate in extension_runtime.load() recognises it as Ed25519-
    verified and lets it through without a /extensions trust step.
    """
    manifest_path = directory / "extension.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    manifest.setdefault("install", {})["trustedBy"] = trusted_by
    manifest["install"]["source"] = "official"
    if version:
        manifest["install"].setdefault("installedAt", _now_iso())
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _remove_legacy_executable() -> None:
    legacy = _legacy_executable()
    try:
        legacy.unlink(missing_ok=True)
        legacy.parent.rmdir()
    except OSError:
        pass


def install_gateway(log=print) -> tuple[Path, dict]:
    """Download and install the Enterprise gateway bundle.

    Returns ``(extracted_dir, manifest)``.
    """
    manifest = _fetch_release("/api/org/download/gateway", log=log)
    payload = _download_payload(manifest, _authenticated_session(), log)
    _verify_enterprise(payload, manifest)

    target = gateway_path()
    extract_archive(payload, target)
    log(f"[green]Enterprise gateway v{manifest['version']} extracted to {target}[/green]")
    return target, manifest