"""Verifies the organisation trust chain, on the machine being governed.

    Laintas key ──signs──▶ licence (carries the org's policy public key)
                                │
    org key     ──signs──▶ policy  ◀── verified here, with the key from the licence

Two things make this worth having. A policy that is not signed is a JSON file on
a laptop whose owner has root: editing it is not an attack, it is a text editor.
And a policy signed by *any* key proves nothing, because anyone can mint a
keypair — only the key the licence names counts, and the licence is signed by a
key that is not on this machine.

**No `cryptography` dependency, on purpose.** Enterprise policy has to work on
every install, not on the installs where an optional wheel happened to build, so
Ed25519 verification is implemented here in plain Python (RFC 8032 §5.1.7). When
`cryptography` *is* present it is used instead, purely for speed — and a test
runs both against the same tokens, because a fast path that disagrees with the
fallback would be worse than not having one.

Verification only. Nothing here signs, and nothing here holds a private key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Optional

# ── Laintas licence keys ────────────────────────────────────────────────────
#
# Public halves, by key id. A rotation ships a build carrying both, and the
# header's `kid` picks one — which is what makes rotating possible without a
# flag day where every deployment must upgrade at once.
LICENCE_PUBLIC_KEYS = {
    "ent-2026-08": "+eA9blc9Y0QwV14UnZiNfqFz+JXx7f4NrzueempytJA=",
}

#: Clock skew allowed before a not-yet-valid token is called a clock rollback.
_SKEW_SECONDS = 300


# ── Ed25519 ─────────────────────────────────────────────────────────────────

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_MODP_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_G_Y = 4 * pow(5, _P - 2, _P) % _P


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0

    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _MODP_SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_G = (_recover_x(_G_Y, 0), _G_Y, 1, _recover_x(_G_Y, 0) * _G_Y % _P)


def _point_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    dd = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(s: int, p):
    """Double-and-add. Not constant time — and it does not need to be: every
    input here is public (a signature, a public key, a message)."""
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _point_add(q, p)
        p = _point_add(p, p)
        s >>= 1
    return q


def _point_equal(p, q) -> bool:
    if (p[0] * q[2] - q[0] * p[2]) % _P != 0:
        return False
    return (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _point_decompress(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _verify_ed25519_pure(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 §5.1.7, unabridged. One verification per session; speed is not
    the point, working everywhere is."""
    if len(public_key) != 32 or len(signature) != 64:
        return False

    a = _point_decompress(public_key)
    if a is None:
        return False
    r_bytes, s_bytes = signature[:32], signature[32:]
    r = _point_decompress(r_bytes)
    if r is None:
        return False
    s = int.from_bytes(s_bytes, "little")
    if s >= _Q:
        return False

    h = int.from_bytes(
        hashlib.sha512(r_bytes + public_key + message).digest(), "little") % _Q
    return _point_equal(_point_mul(s, _G), _point_add(r, _point_mul(h, a)))


def _verify_ed25519_fast(public_key: bytes, message: bytes, signature: bytes):
    """Use `cryptography` when it is installed. Returns None when it is not, so
    the caller falls back rather than treating "unavailable" as "invalid"."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return None
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        return True
    except InvalidSignature:
        return False
    except Exception:
        # A malformed key is not a failed signature; let the pure path decide so
        # both implementations answer the same question.
        return None


def _verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    verdict = _verify_ed25519_fast(public_key, message, signature)
    if verdict is None:
        return _verify_ed25519_pure(public_key, message, signature)
    return verdict


# ── Token plumbing ──────────────────────────────────────────────────────────

def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _split(token: str):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    head, body, sig = parts
    try:
        header = json.loads(_b64url_decode(head))
        payload = json.loads(_b64url_decode(body))
        signature = _b64url_decode(sig)
    except Exception:
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    return header, payload, signature, f"{head}.{body}".encode("ascii")


def _raw_key(b64_value: str) -> Optional[bytes]:
    """A 32-byte Ed25519 key from its base64 form, or None if it is not one."""
    try:
        raw = base64.b64decode(str(b64_value or ""), validate=True)
    except Exception:
        return None
    return raw if len(raw) == 32 else None


# ── Licence ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LicenseState:
    """What this deployment's licence says, once it has been believed."""

    valid: bool
    reason: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def org_id(self) -> str:
        return str(self.payload.get("sub") or "")

    @property
    def org_name(self) -> str:
        return str(self.payload.get("org_name") or "")

    @property
    def seats(self) -> int:
        return int(self.payload.get("seats") or 0)

    @property
    def modules(self) -> list:
        return list(self.payload.get("modules") or [])

    @property
    def policy_key(self) -> str:
        """The organisation's policy public key, or "" when none is registered.

        Empty is meaningful: it says this organisation may not sign its own
        policy, so a policy bearing an org signature must be refused rather than
        trusted on its own say-so.
        """
        return str(self.payload.get("pol_key") or "")

    @property
    def policy_required(self) -> bool:
        return bool(self.payload.get("pol_req"))

    @property
    def in_grace(self) -> bool:
        return self.valid and time.time() > float(self.payload.get("exp") or 0)

    def __bool__(self) -> bool:
        return self.valid


def verify_license(token: str, *, now: Optional[float] = None) -> LicenseState:
    """Check a licence offline.

    Expiry inside the grace window still verifies — a lapsed licence degrades a
    deployment rather than stopping it dead, and `in_grace` is how a console
    knows to say so.
    """
    now = time.time() if now is None else now
    parsed = _split(token)
    if parsed is None:
        return LicenseState(False, "malformed")
    header, payload, signature, signing_input = parsed

    if header.get("alg") != "EdDSA":
        return LicenseState(False, "unsupported_alg", payload)

    key = _raw_key(LICENCE_PUBLIC_KEYS.get(str(header.get("kid") or ""), ""))
    if key is None:
        return LicenseState(False, "unknown_key", payload)
    if not _verify_ed25519(key, signing_input, signature):
        return LicenseState(False, "bad_signature", payload)

    nbf = float(payload.get("nbf") or 0)
    exp = float(payload.get("exp") or 0)
    grace = float(payload.get("grace_days") or 0) * 86400
    if nbf and now + _SKEW_SECONDS < nbf:
        return LicenseState(False, "clock_rollback", payload)
    if now > exp + grace:
        return LicenseState(False, "expired", payload)
    return LicenseState(True, "", payload)


# ── Policy ──────────────────────────────────────────────────────────────────

def verify_policy(token: str, *, policy_key: str = "", min_version: int = 0,
                  now: Optional[float] = None) -> dict:
    """Check a policy document against the key the licence vouched for.

    `policy_key` empty means the licence registered none, so only a
    Laintas-signed policy is acceptable — an org signature with nothing behind it
    is a document somebody wrote, not a policy.

    `min_version` is the version already held. A signature stays valid forever,
    so without this a member could reinstate last month's more permissive rules
    by replaying an old file that verifies perfectly.
    """
    now = time.time() if now is None else now
    parsed = _split(token)
    if parsed is None:
        return {"valid": False, "reason": "malformed"}
    header, payload, signature, signing_input = parsed

    if header.get("alg") != "EdDSA":
        return {"valid": False, "reason": "unsupported_alg"}

    if policy_key:
        key = _raw_key(policy_key)
    else:
        key = _raw_key(LICENCE_PUBLIC_KEYS.get(str(header.get("kid") or ""), ""))
    if key is None:
        return {"valid": False, "reason": "unknown_key"}

    if not _verify_ed25519(key, signing_input, signature):
        return {"valid": False, "reason": "bad_signature"}

    document = payload.get("policy")
    if not isinstance(document, dict):
        return {"valid": False, "reason": "malformed"}

    version = payload.get("ver")
    if not isinstance(version, int) or version < 1:
        return {"valid": False, "reason": "malformed"}
    if version < int(min_version or 0):
        return {"valid": False, "reason": "version_rollback", "version": version}

    nbf = float(payload.get("nbf") or 0)
    exp = float(payload.get("exp") or 0)
    if nbf and now + _SKEW_SECONDS < nbf:
        return {"valid": False, "reason": "clock_rollback"}
    # Short-lived on purpose: revocation is "stop re-issuing", which needs no
    # CRL and no reachable client — but only works if the thing being revoked
    # expires soon.
    if exp and now > exp:
        return {"valid": False, "reason": "expired", "version": version}

    return {
        "valid": True,
        "reason": "",
        "policy": document,
        "version": version,
        "org_id": str(payload.get("sub") or ""),
        "digest": policy_digest(document),
    }


def policy_digest(document: dict) -> str:
    """The fingerprint a client reports to prove which policy it applied.

    Canonical JSON — sorted keys, no incidental whitespace — so the same
    document always produces the same digest no matter who serialised it. The
    server compares this against what it published; a member who edited their
    local copy produces a different one and is refused.
    """
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
