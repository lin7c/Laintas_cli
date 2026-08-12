"""
Attestation: obtain a signed JWT from the laintas gateway and derive a
per-session HMAC key for event-log integrity.

The gateway (laintas.com) is the only party that holds the signing key.  The
client trusts HTTPS and parses the JWT payload (base64, no signature check)
to extract ``jti``.  From that, a short-lived HMAC key is derived that signs
every event written to ``.laintas/events.jsonl``.

The HMAC key lives ONLY in memory (``event_log._EVENT_HMAC_KEY``) and is never
written to disk.  When the process exits the key is gone; a new session gets a
fresh ``jti`` from the gateway on restart.

Public API:
  fetch_and_arm()  →  (ok: bool, detail: str)
      Call once at laintas_cli startup.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("laintas.attestation")

# ── module-level cache ───────────────────────────────────────────────────
_attestation_jwt: Optional[str] = None


def _make_request(backend_url: str, headers: dict, cookies: dict,
                  timeout: float = 15.0) -> Optional[str]:
    """Call GET /api/cli/attest and return the JWT token string, or None.

    Uses ``requests`` when available, otherwise stdlib ``urllib``.
    """
    url = f"{backend_url}/api/cli/attest"
    try:
        import requests as _requests
        resp = _requests.get(
            url, headers=headers, cookies=cookies,
            timeout=timeout, allow_redirects=False)
        if resp.status_code == 200:
            data = resp.json()
            return str(data.get("token") or "") or None
        logger.warning("attest: HTTP %s — %s", resp.status_code,
                       (resp.text or "")[:200])
        return None
    except ImportError:
        pass

    # stdlib fallback
    from urllib import request as _ur
    try:
        # urllib does not natively support cookies dict — encode them manually
        req_headers = dict(headers)
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            req_headers["Cookie"] = cookie_str
        req = _ur.Request(url, headers=req_headers, method="GET")
        with _ur.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200:
                data = json.loads(body)
                return str(data.get("token") or "") or None
            logger.warning("attest: HTTP %s — %s", resp.status, body[:200])
        return None
    except Exception as exc:
        logger.warning("attest: request failed — %s", exc)
        return None


def _derive_hmac_key(jti: str) -> bytes:
    """HKDF-expand an HMAC-SHA256 key from the gateway-issued nonce.

    ``jti`` is already a high-entropy random value (the gateway uses
    ``secrets.token_hex(16)``), so one PBKDF2 round is sufficient.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        jti.encode("utf-8"),
        b"laintas_event_log_v1",         # domain-separation salt
        iterations=1,
        dklen=32,                         # 256-bit key for HMAC-SHA256
    )


def fetch_and_arm(
    *,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """Obtain an attestation JWT from the gateway and arm the event-log HMAC.

    Returns ``(ok, detail)``.  A failure is non-fatal — the agent loop must
    NOT break because attestation is unavailable; events are still logged,
    just without integrity signatures.
    """
    global _attestation_jwt

    # ── imports that depend on the rest of laintas_cli ────────────────
    try:
        import laintas_cli              # noqa: F811 — loaded in-process
        import backend_profiles         # noqa: F811
    except ImportError as exc:
        logger.debug("attest: cannot import laintas_cli modules — %s", exc)
        return False, "laintas_cli not available (embedded use?)"

    session = laintas_cli.load_session()
    if not session:
        return False, "not signed in — run `laintas-cli --login` first"

    profile = laintas_cli.get_backend_profile()
    if not profile.sends_laintas_credentials:
        return False, (
            "custom/local backend cannot issue attestation tokens; "
            "only official laintas.com backends are supported"
        )

    headers, cookies = backend_profiles.request_auth(profile, session)

    # ── call gateway ──────────────────────────────────────────────────
    token = _make_request(profile.base_url, headers, cookies, timeout=timeout)
    if not token:
        return False, "gateway returned no attestation token"

    # ── parse JWT payload (no signature check — we trust HTTPS) ──────
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False, "malformed attestation token (not a JWT)"
        # JWT payload is base64url-encoded
        payload_b64 = parts[1]
        # Add padding for base64 decode
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        claims = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("attest: JWT parse failed — %s", exc)
        return False, f"malformed attestation token: {exc}"

    jti = claims.get("jti")
    if not jti or not isinstance(jti, str):
        return False, "attestation token missing 'jti' claim"

    # ── derive HMAC key and arm event_log ────────────────────────────
    try:
        import event_log
        hmac_key = _derive_hmac_key(jti)
        event_log.set_hmac_key(hmac_key)
    except ImportError:
        return False, "event_log not available"

    _attestation_jwt = token
    version = claims.get("version", "unknown")
    logger.info("attest: armed — version=%s jti=%s...", version, jti[:16])
    return True, f"attested version={version}"


def get_jwt() -> Optional[str]:
    """Return the cached attestation JWT, or None if ``fetch_and_arm`` was
    never called or failed."""
    return _attestation_jwt
