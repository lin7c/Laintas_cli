"""Authenticated installer for the separately distributed Enterprise edition.

This module intentionally contains no organization, seat, asset or policy
implementation. It only obtains a platform-specific signed executable from the
logged-in user's organization and installs it atomically.

It therefore ships with the public CLI, and must stay that way: it holds a
*public* verification key and no secrets, and the entry point is useless without
a session and a membership the server checks on its side. What stays private is
the Enterprise edition it downloads, not the code that fetches it.

Releases are served from enterprise.laintas.com only. Nothing here follows a
download URL to another origin — see `_download_payload`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import requests

import paths
from release import (atomic_write, download, platform_id, verify_ed25519,
                     verify_sha256)

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

def executable_path() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return paths.LAINTAS_HOME / "enterprise" / "bin" / f"laintas-enterprise{suffix}"


def gateway_path() -> Path:
    return paths.LAINTAS_HOME / "enterprise" / "gateway"


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

    log(f"Downloading enterprise release v{manifest.get('version')} "
        f"({_format_size(size)})…")
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

def install_cli(log=print) -> tuple[Path, dict]:
    """Download and install the Enterprise CLI binary.

    Returns ``(installed_path, manifest)``.
    """
    manifest = _fetch_release("/api/org/download/cli", platform=platform_id(), log=log)
    payload = _download_payload(manifest, _authenticated_session(), log)
    _verify_enterprise(payload, manifest)
    target = executable_path()
    atomic_write(payload, target)
    log(f"Enterprise CLI v{manifest['version']} installed at {target}")
    return target, manifest


def install_gateway(log=print) -> tuple[Path, dict]:
    """Download and install the Enterprise gateway bundle.

    Returns ``(extracted_dir, manifest)``.
    """
    manifest = _fetch_release("/api/org/download/gateway", log=log)
    payload = _download_payload(manifest, _authenticated_session(), log)
    _verify_enterprise(payload, manifest)

    target = gateway_path()
    from release import extract_archive
    extract_archive(payload, target)
    log(f"Enterprise gateway v{manifest['version']} extracted to {target}")
    return target, manifest


def install_and_launch(log=print) -> None:
    """Download, install, and launch the Enterprise CLI."""
    target, _ = install_cli(log=log)
    subprocess.run([str(target)], check=False)