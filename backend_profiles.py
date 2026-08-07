"""Backend trust profiles and credential isolation.

Official Laintas credentials are audience-bound and must never be forwarded to
an arbitrary URL selected by a project, environment variable, or CLI flag.
Custom/local backends use separate credentials and are explicitly unmetered by
Laintas.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import paths
import json_store
import terminal_preferences


BackendKind = Literal["official", "custom", "local"]

OFFICIAL_ORIGINS = frozenset({
    "https://laintas.com",          # primary — CLI gateway paths proxied here
    "https://helpwo.laintas.com",   # legacy/compat — Helpwo's own domain
    "https://api.laintas.com",
})


@dataclass(frozen=True)
class BackendProfile:
    name: str
    kind: BackendKind
    base_url: str
    auth_ref: Optional[str] = None

    @property
    def origin(self) -> str:
        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def sends_laintas_credentials(self) -> bool:
        return self.kind == "official" and self.origin in OFFICIAL_ORIGINS

    @property
    def billing_label(self) -> str:
        return "Laintas managed" if self.sends_laintas_credentials else "external/unmetered"


def _normalize_url(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("backend URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError("backend URL must not contain embedded credentials")
    if parts.query or parts.fragment:
        raise ValueError("backend URL must not contain query parameters or fragments")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _kind_for_url(url: str) -> BackendKind:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin in OFFICIAL_ORIGINS:
        if parts.scheme != "https":
            raise ValueError("official backends require HTTPS")
        return "official"
    if parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return "local"
    return "custom"


def _load_profiles() -> dict:
    path = paths.BACKENDS_FILE
    if not path.is_file():
        return {}
    if not paths.ensure_private_file(path):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve(default_url: str, selected: Optional[str] = None) -> BackendProfile:
    """Resolve the active profile, preserving the legacy URL override safely.

    A raw LAINTAS_BACKEND/--backend URL is treated as custom unless its exact
    origin is an official HTTPS origin. This maintains compatibility without
    leaking the official session to arbitrary hosts.
    """
    raw_override = os.environ.get("LAINTAS_BACKEND", "").strip()
    if raw_override:
        url = _normalize_url(raw_override)
        return BackendProfile("legacy-override", _kind_for_url(url), url)

    data = _load_profiles()
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    active = (
        selected
        or os.environ.get("LAINTAS_BACKEND_PROFILE")
        or terminal_preferences.get("backend_profile", "")
        or data.get("active")
    )
    if active and active in profiles and isinstance(profiles[active], dict):
        entry = profiles[active]
        url = _normalize_url(str(entry.get("baseUrl") or ""))
        derived = _kind_for_url(url)
        requested = str(entry.get("kind") or derived).lower()
        if requested not in ("official", "custom", "local"):
            raise ValueError(f"invalid backend kind: {requested}")
        # A config file cannot promote an arbitrary origin to official.
        kind: BackendKind = "official" if derived == "official" else derived
        return BackendProfile(str(active), kind, url, entry.get("auth"))

    url = _normalize_url(default_url)
    return BackendProfile("default", _kind_for_url(url), url)


def _policy_headers() -> dict:
    """The organisation policy fingerprint, when one has been applied.

    Set by the Enterprise edition after it verifies and applies a signed policy.
    Sent on every request so the server can check that the client it is serving
    is holding the policy it published — the client is what blocks a command,
    but the client is also what a member can edit, so its word needs something
    behind it.

    Absent on a personal install, where there is no organisation to attest to.
    """
    digest = os.environ.get("LAINTAS_POLICY_DIGEST", "").strip().lower()
    return {"X-Laintas-Policy": digest} if re.fullmatch(r"[a-f0-9]{64}", digest) else {}


def request_auth(profile: BackendProfile, session: Optional[dict]) -> tuple[dict, dict]:
    """Return (headers, cookies) appropriate for this backend trust domain."""
    headers = {"Content-Type": "application/json", **_policy_headers()}
    session = session or {}
    if profile.sends_laintas_credentials:
        authorization = (session.get("headers") or {}).get("Authorization")
        if authorization:
            headers["Authorization"] = authorization
        cookies = dict(session.get("cookies") or {})
        return headers, cookies

    # Custom credentials are intentionally separate from the Laintas session.
    token = ""
    auth_ref = (profile.auth_ref or "env:LAINTAS_CUSTOM_BACKEND_TOKEN").strip()
    if auth_ref.startswith("env:"):
        env_name = auth_ref[4:]
        if env_name and env_name.replace("_", "").isalnum():
            token = os.environ.get(env_name, "").strip()
    elif auth_ref.startswith("keyring:"):
        try:
            import keyring  # optional dependency
            service_user = auth_ref[len("keyring:"):]
            service, user = service_user.split("/", 1)
            token = (keyring.get_password(service, user) or "").strip()
        except Exception:
            token = ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers, {}


def ensure_template() -> Path:
    """Create a disabled-by-default profile template with private permissions."""
    path = paths.BACKENDS_FILE
    if path.exists():
        return path
    payload = {
        "version": 1,
        "active": "official",
        "profiles": {
            "official": {
                "kind": "official",
                "baseUrl": "https://laintas.com",
                "auth": "laintas-session",
            },
            "local": {
                "kind": "local",
                "baseUrl": "http://127.0.0.1:2913",
                "auth": "none",
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def upsert_profile(name: str, base_url: str, auth_ref: str = "env:LAINTAS_CUSTOM_BACKEND_TOKEN",
                   *, activate: bool = True) -> tuple:
    """Add or update a profile and optionally switch to it.

    Exists so adding a custom gateway is a command rather than an instruction
    to hand-edit JSON.

    Returns ``(ok, message)``; never raises for ordinary input problems.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
        return False, "profile name must be 1-64 chars of letters, digits, . _ -"
    try:
        url = _normalize_url(base_url)
    except ValueError as exc:
        return False, str(exc)

    kind = _kind_for_url(url)
    if kind == "official":
        return False, ("that URL is an official Laintas endpoint; it is reached "
                       "through the built-in profile, not a custom one")

    data = _load_profiles()
    if not data:
        ensure_template()
        data = _load_profiles()
    profiles = data.setdefault("profiles", {})
    profiles[name] = {"kind": kind, "baseUrl": url, "auth": auth_ref}
    if activate:
        data["active"] = name

    try:
        json_store.save_json_atomic(paths.BACKENDS_FILE, data, mode=0o600)
    except OSError as exc:
        return False, f"could not write {paths.BACKENDS_FILE}: {exc}"
    return True, f"backend {name!r} -> {url}"


def list_profiles() -> list[BackendProfile]:
    data = _load_profiles()
    result = []
    for name, entry in (data.get("profiles") or {}).items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        try:
            url = _normalize_url(str(entry.get("baseUrl") or ""))
            result.append(BackendProfile(
                name, _kind_for_url(url), url, entry.get("auth")))
        except ValueError:
            continue
    return sorted(result, key=lambda profile: profile.name)


def set_active(name: str) -> tuple[bool, str]:
    data = _load_profiles()
    profiles = data.get("profiles") or {}
    if name not in profiles:
        return False, f"unknown backend profile: {name}"
    # Validate before persisting selection.
    try:
        entry = profiles[name]
        url = _normalize_url(str(entry.get("baseUrl") or ""))
        _kind_for_url(url)
    except (AttributeError, ValueError) as exc:
        return False, f"invalid backend profile '{name}': {exc}"
    try:
        terminal_preferences.set_value("backend_profile", name)
    except OSError as exc:
        return False, f"could not select backend profile: {exc}"
    return True, f"active backend profile for this terminal: {name} (restart required)"
