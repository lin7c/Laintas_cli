"""Secure client and local autonomous-action policy for the PPOS Agent API.

All server paths intentionally live in :data:`ENDPOINTS`.  The backend contract
is still being integrated, so changing a route must not require touching command
or tool code.  PPOS always uses an official Laintas trust profile; the active
custom backend is never an auth target for this module.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import threading
import time
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

import backend_profiles
import json_store
import paths


API_ROOT = "/api/ppos/agent"
ENDPOINTS = {
    "token": f"{API_ROOT}/token",
    "policy": f"{API_ROOT}/policy",
    "account": f"{API_ROOT}/account",
    "storage": f"{API_ROOT}/storage",
    "storage_cleanup": f"{API_ROOT}/storage/cleanup",
    "communities": f"{API_ROOT}/communities",
    "works": f"{API_ROOT}/works",
    "work": f"{API_ROOT}/works/{{work_id}}",
    "work_delete": f"{API_ROOT}/works/{{work_id}}/delete",
    "status": f"{API_ROOT}/account",
    "publish_preflight": f"{API_ROOT}/publish/preflight",
    "publish_commit": f"{API_ROOT}/publish/commit",
    "draft_preflight": f"{API_ROOT}/drafts/preflight",
    "draft_save": f"{API_ROOT}/drafts/save",
    "presign": f"{API_ROOT}/storage/presign",
    "comments": f"{API_ROOT}/works/{{work_id}}/comments",
    "community_review_queue": f"{API_ROOT}/communities/{{community}}/review-queue",
    "community_review_decision": f"{API_ROOT}/communities/{{community}}/works/{{review_id}}/{{decision}}",
    "platform_review_queue": f"{API_ROOT}/platform/review-queue",
    "platform_review_decision": f"{API_ROOT}/platform/works/{{review_id}}/{{decision}}",
}

CLIENT_NAME = "laintas_cli"
UTF16_MARKDOWN_LIMIT = 100_000
READ_CACHE_TTL = 15.0
MAX_PAGE_SIZE = 50
_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)"
    r"|<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>",
    re.IGNORECASE,
)
_OFFICIAL_DEFAULT = "https://laintas.com"
_SCOPE_KEYS = ("publish", "comment", "community_review", "platform_review", "manage")
_SERVER_WRITE_SCOPES = {
    "publish": ("storage:write", "works:write"),
    "comment": ("comments:write",),
    "community_review": ("community_reviews:write",),
    "platform_review": ("platform_reviews:write",),
    # Editing, deleting and sweeping storage touch content that already exists,
    # so they are opted into separately from the right to create new content.
    "manage": ("works:write", "storage:write"),
}

# What PPOS accepts as self-hosted media. Videos uploaded here are inherently
# the author's own, so they need no channel verification the way an embed does.
UPLOADABLE_IMAGE_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/avif", "image/bmp",
}
UPLOADABLE_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
# mimetypes' guesses vary by platform; the extension is the contract PPOS states.
_EXTENSION_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime",
}
class PPOSClientError(RuntimeError):
    pass


class PPOSPolicyError(PPOSClientError):
    pass


def _english_http_detail(value: Any, status_code: int) -> str:
    """Keep CLI diagnostics English even when an upstream legacy route is localized.

    User content is never passed through this function; it is used only for an
    HTTP error's ``detail`` field.
    """
    detail = str(value or "").strip()
    if any("\u3400" <= char <= "\u9fff" for char in detail):
        if status_code == 401:
            return "Authentication is required or the session has expired."
        if status_code == 403:
            return "PPOS denied this action for the current account or community role."
        if status_code == 404:
            return "The requested PPOS resource was not found."
        if status_code == 409:
            return "The PPOS resource changed and the requested action can no longer be applied."
        if status_code == 413:
            return "The request exceeds the PPOS storage allowance or size limit."
        return "PPOS rejected the request. Open the PPOS web app for the account-specific reason."
    return detail or "unknown error"


def utf16_units(text: str) -> int:
    """Return JavaScript's ``string.length`` for *text*."""
    return len(text.encode("utf-16-le")) // 2


def stable_idempotency_key(user: str, action: str, content: Any) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    digest = hashlib.sha256(
        f"ppos-v1\0{user}\0{action}\0{canonical}".encode("utf-8")
    ).hexdigest()
    return f"laintas-cli-{digest}"


def _default_policy() -> dict:
    return {
        "version": 1,
        "enabled": False,
        "scopes": {key: False for key in _SCOPE_KEYS},
        "community_allowlist": [],
        "daily_caps": {
            "publish": 1,
            "comment": 5,
            "community_review": 5,
            "platform_review": 0,
            "manage": 3,
        },
        "fee_cap_cents": 0,
        "minimum_review_confidence": 0.8,
        "usage": {"date": "", **{key: 0 for key in _SCOPE_KEYS}},
    }


def load_policy() -> dict:
    default = _default_policy()
    path = paths.PPOS_POLICY_FILE
    if not path.is_file() or not paths.ensure_private_file(path):
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if not isinstance(raw, dict):
        return default
    result = default
    result["enabled"] = raw.get("enabled") is True
    if isinstance(raw.get("scopes"), dict):
        result["scopes"].update({k: raw["scopes"].get(k) is True for k in _SCOPE_KEYS})
    allowlist = raw.get("community_allowlist")
    if isinstance(allowlist, list):
        result["community_allowlist"] = sorted({str(v).strip() for v in allowlist if str(v).strip()})
    if isinstance(raw.get("daily_caps"), dict):
        for key in _SCOPE_KEYS:
            value = raw["daily_caps"].get(key)
            if type(value) is int and value >= 0:
                result["daily_caps"][key] = value
    fee = raw.get("fee_cap_cents")
    if type(fee) is int and fee >= 0:
        result["fee_cap_cents"] = fee
    confidence = raw.get("minimum_review_confidence")
    if type(confidence) in (int, float) and 0 <= confidence <= 1:
        result["minimum_review_confidence"] = float(confidence)
    if isinstance(raw.get("usage"), dict):
        result["usage"] = dict(default["usage"])
        result["usage"]["date"] = str(raw["usage"].get("date") or "")
        for key in _SCOPE_KEYS:
            value = raw["usage"].get(key)
            if type(value) is int and value >= 0:
                result["usage"][key] = value
    return result


def save_policy(policy: dict) -> dict:
    paths.PPOS_POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    json_store.save_json_atomic(paths.PPOS_POLICY_FILE, policy, mode=0o600)
    return policy


def update_policy(*, enabled: Optional[bool] = None, scope: Optional[str] = None,
                  scope_enabled: Optional[bool] = None,
                  community_allowlist: Optional[list[str]] = None,
                  daily_cap: Optional[tuple[str, int]] = None,
                  fee_cap_cents: Optional[int] = None,
                  minimum_review_confidence: Optional[float] = None) -> dict:
    policy = load_policy()
    if enabled is not None:
        policy["enabled"] = bool(enabled)
    if scope is not None:
        if scope not in _SCOPE_KEYS:
            raise ValueError(f"unknown PPOS scope: {scope}")
        policy["scopes"][scope] = bool(scope_enabled)
    if community_allowlist is not None:
        policy["community_allowlist"] = sorted({v.strip() for v in community_allowlist if v.strip()})
    if daily_cap is not None:
        cap_scope, cap = daily_cap
        if cap_scope not in _SCOPE_KEYS or type(cap) is not int or cap < 0:
            raise ValueError("daily cap needs a known scope and a non-negative integer")
        policy["daily_caps"][cap_scope] = cap
    if fee_cap_cents is not None:
        if type(fee_cap_cents) is not int or fee_cap_cents < 0:
            raise ValueError("fee cap must be a non-negative integer number of cents")
        policy["fee_cap_cents"] = fee_cap_cents
    if minimum_review_confidence is not None:
        if not 0 <= minimum_review_confidence <= 1:
            raise ValueError("minimum confidence must be between 0 and 1")
        policy["minimum_review_confidence"] = float(minimum_review_confidence)
    return save_policy(policy)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def enforce_policy(scope: str, *, community: str = "", confidence: Optional[float] = None,
                   fee_cents: int = 0) -> dict:
    policy = load_policy()
    if scope not in _SCOPE_KEYS:
        raise PPOSPolicyError(f"unknown autonomous scope: {scope}")
    if not policy["enabled"]:
        raise PPOSPolicyError("PPOS autonomous agent is disabled; use /ppos agent enable")
    if not policy["scopes"].get(scope):
        raise PPOSPolicyError(f"PPOS autonomous {scope.replace('_', '-')} is not enabled")
    allowed = policy["community_allowlist"]
    if community and allowed and community not in allowed:
        raise PPOSPolicyError(f"community {community!r} is not in the PPOS allowlist")
    usage = policy["usage"]
    used = usage.get(scope, 0) if usage.get("date") == _today() else 0
    cap = policy["daily_caps"].get(scope, 0)
    if cap <= 0 or used >= cap:
        raise PPOSPolicyError(f"daily {scope.replace('_', '-')} cap reached ({used}/{cap})")
    if fee_cents > policy["fee_cap_cents"]:
        raise PPOSPolicyError(
            f"fee {fee_cents} cents exceeds local cap {policy['fee_cap_cents']} cents")
    if scope.endswith("review"):
        if confidence is None or confidence < policy["minimum_review_confidence"]:
            raise PPOSPolicyError(
                f"review confidence must be at least {policy['minimum_review_confidence']:.2f}")
    return policy


def record_policy_use(scope: str) -> None:
    policy = load_policy()
    today = _today()
    if policy["usage"].get("date") != today:
        policy["usage"] = {"date": today, **{key: 0 for key in _SCOPE_KEYS}}
    policy["usage"][scope] = int(policy["usage"].get(scope, 0)) + 1
    save_policy(policy)


@dataclass(frozen=True)
class LocalAsset:
    reference: str
    path: Path
    size: int
    sha256: str
    media_type: str
    is_video: bool = False
    # Every raw spelling in the document that resolves to this file. `./a.png`
    # and `a.png` are one upload but two links, and both have to be rewritten.
    references: tuple[str, ...] = ()


def media_type_for(path: Path) -> str:
    """Content type PPOS will accept for this file, by extension first."""
    extension = path.suffix.lower()
    if extension in _EXTENSION_TYPES:
        return _EXTENSION_TYPES[extension]
    guessed = (mimetypes.guess_type(path.name)[0] or "").lower()
    return guessed or "application/octet-stream"


def _media_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return target


def _media_match(match: re.Match) -> tuple[str, tuple[int, int]]:
    """Return the URL and its exact span for Markdown or HTML image syntax."""
    group = 1 if match.group(1) is not None else 2
    return _media_target(match.group(group)), match.span(group)


def parse_local_media(markdown: str, markdown_path: Path) -> list[LocalAsset]:
    """Resolve safe local Markdown media targets within the document directory.

    Images and self-hosted video are treated the same way: PPOS stores both in
    its own R2 bucket, and an uploaded video needs no channel verification.
    """
    base = markdown_path.parent.resolve()
    assets: list[LocalAsset] = []
    by_path: dict[Path, list[str]] = {}
    for match in _IMAGE_RE.finditer(markdown):
        raw, _span = _media_match(match)
        split = urlsplit(raw)
        if split.scheme or split.netloc or raw.startswith(("#", "/", "\\")):
            if split.scheme in ("http", "https", "data") or raw.startswith("#"):
                continue
            raise PPOSClientError(f"unsafe media reference: {raw}")
        decoded = unquote(split.path)
        if not decoded or "\\" in decoded or Path(decoded).is_absolute():
            raise PPOSClientError(f"unsafe media reference: {raw}")
        try:
            candidate = (base / decoded).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PPOSClientError(f"local media cannot be resolved safely: {raw}") from exc
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise PPOSClientError(f"media escapes Markdown directory: {raw}") from exc
        if not candidate.is_file():
            raise PPOSClientError(f"media is not a regular file: {raw}")
        if candidate in by_path:
            if raw not in by_path[candidate]:
                by_path[candidate].append(raw)
            continue
        by_path[candidate] = [raw]
        media_type = media_type_for(candidate)
        is_video = media_type in UPLOADABLE_VIDEO_TYPES
        if not is_video and media_type not in UPLOADABLE_IMAGE_TYPES:
            raise PPOSClientError(
                f"PPOS does not accept this media type: {raw} ({media_type}). "
                "Images: jpg/png/webp/gif/avif/bmp — video: mp4/webm/mov.")
        data = candidate.read_bytes()
        assets.append(LocalAsset(
            reference=raw, path=candidate, size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            media_type=media_type, is_video=is_video,
        ))
    return [
        dataclass_replace(asset, references=tuple(by_path[asset.path]))
        for asset in assets
    ]


# Kept as the old name so existing callers and tests keep working.
parse_local_images = parse_local_media


def rewrite_media_references(markdown: str, replacements: dict[str, str]) -> str:
    """Swap local media targets for their public URLs, span by span.

    Replacing by substring corrupts a document where one filename is a prefix of
    another (or appears in prose), so each link's own target span is rewritten.
    """
    if not replacements:
        return markdown
    pieces: list[str] = []
    last = 0
    for match in _IMAGE_RE.finditer(markdown):
        raw, (start, end) = _media_match(match)
        url = replacements.get(raw)
        if not url:
            continue
        pieces.append(markdown[last:start])
        pieces.append(url)
        last = end
    pieces.append(markdown[last:])
    return "".join(pieces)


def read_markdown(path: str | Path) -> tuple[Path, str, list[LocalAsset]]:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise PPOSClientError("Markdown path is not a regular file")
    try:
        markdown = source.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PPOSClientError("Markdown must be valid UTF-8") from exc
    units = utf16_units(markdown)
    if units > UTF16_MARKDOWN_LIMIT:
        raise PPOSClientError(
            f"Markdown is {units} UTF-16 units; limit is {UTF16_MARKDOWN_LIMIT}")
    return source, markdown, parse_local_media(markdown, source)


_cache_lock = threading.Lock()
_read_cache: dict[tuple, tuple[float, Any]] = {}


def _official_profile() -> backend_profiles.BackendProfile:
    """Return an official auth audience, never an active custom URL."""
    try:
        active = backend_profiles.resolve(_OFFICIAL_DEFAULT)
    except ValueError:
        active = backend_profiles.BackendProfile("ppos-official", "official", _OFFICIAL_DEFAULT)
    if active.sends_laintas_credentials:
        return active
    return backend_profiles.BackendProfile("ppos-official", "official", _OFFICIAL_DEFAULT)


class PPOSClient:
    def __init__(self, session: Optional[dict], *, agent_id: str = "", requests_module=None):
        self.session = session or {}
        self.agent_id = agent_id or ""
        self.profile = _official_profile()
        if not self.profile.sends_laintas_credentials:
            raise PPOSClientError("no trusted official Laintas backend is available")
        if requests_module is None:
            import requests
            requests_module = requests
        self.requests = requests_module
        self._tokens: dict[tuple[str, ...], tuple[int, str]] = {}

    def _metadata(self) -> dict:
        metadata = {"client": CLIENT_NAME}
        if self.agent_id:
            metadata["agent_id"] = self.agent_id
        return metadata

    def _request(self, method: str, endpoint: str, *, params: Optional[dict] = None,
                 body: Optional[dict] = None, idempotency_key: str = "",
                 timeout: int = 15, scopes: tuple[str, ...] = ()) -> Any:
        headers, cookies = backend_profiles.request_auth(self.profile, self.session)
        headers["X-Agent-Client"] = CLIENT_NAME
        if scopes:
            headers["X-PPOS-Agent-Token"] = self._capability(scopes)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = f"{self.profile.base_url}{endpoint}"
        try:
            response = self.requests.request(
                method, url, params=params, json=body, headers=headers,
                cookies=cookies, timeout=timeout)
        except Exception as exc:
            raise PPOSClientError(f"could not reach official Laintas PPOS API: {exc}") from exc
        if response.status_code >= 300:
            try:
                error_body = response.json() or {}
                detail = error_body.get("detail") or error_body.get("error")
            except Exception:
                detail = response.text[:300]
            detail = _english_http_detail(detail, response.status_code)
            raise PPOSClientError(f"PPOS request failed (HTTP {response.status_code}): {detail}")
        if not getattr(response, "content", b""):
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PPOSClientError("PPOS returned invalid JSON") from exc

    def _capability(self, scopes: tuple[str, ...]) -> str:
        key = tuple(sorted(set(scopes)))
        cached = self._tokens.get(key)
        now = int(time.time())
        if cached and cached[0] > now + 15:
            return cached[1]
        result = self._request("POST", ENDPOINTS["token"],
                               body={"client": CLIENT_NAME, "scopes": list(key)})
        token = str(result.get("token") or "")
        expires_at = int(result.get("expires_at") or 0)
        if not token or expires_at <= now:
            raise PPOSClientError("PPOS returned an invalid capability token")
        self._tokens[key] = (expires_at, token)
        return token

    def sync_server_policy(self) -> Any:
        policy = load_policy()
        scopes = {"account:read", "storage:read", "communities:read", "works:read",
                  "comments:read", "community_reviews:read"}
        if policy["enabled"]:
            for local_scope, server_scopes in _SERVER_WRITE_SCOPES.items():
                if policy["scopes"].get(local_scope):
                    scopes.update(server_scopes)
                    if local_scope == "platform_review":
                        scopes.add("platform_reviews:read")
        return self._request("PATCH", ENDPOINTS["policy"],
                             body={"client": CLIENT_NAME, "scopes": sorted(scopes)})

    _PAGED_READS = ("communities", "works", "community_review_queue", "platform_review_queue")
    _READ_SCOPES = {
        "account": "account:read", "status": "account:read", "storage": "storage:read",
        "communities": "communities:read", "works": "works:read", "work": "works:read",
        "community_review_queue": "community_reviews:read",
        "platform_review_queue": "platform_reviews:read",
    }

    def read(self, kind: str, *, page: int = 1, page_size: int = 20,
             filters: Optional[dict] = None) -> Any:
        if kind not in self._READ_SCOPES:
            raise PPOSClientError(f"unknown PPOS read: {kind}")
        params = dict(filters or {})
        endpoint = ENDPOINTS[kind]
        if kind == "community_review_queue":
            community = str(params.pop("community_id", "") or params.pop("community", ""))
            if not community:
                raise PPOSClientError("community is required for the community review queue")
            endpoint = endpoint.format(community=quote(community, safe=""))
        elif kind == "work":
            work_id = str(params.pop("work_id", "") or params.pop("id", ""))
            if not work_id:
                raise PPOSClientError("work_id is required to read one work")
            endpoint = endpoint.format(work_id=quote(work_id, safe=""))
        if kind in self._PAGED_READS:
            # Pagination is real: page N is sent as an offset, not dropped on the
            # floor while the caller believes it is advancing.
            limit = max(1, min(MAX_PAGE_SIZE, int(page_size)))
            params["limit"] = limit
            params["offset"] = (max(1, int(page)) - 1) * limit
        params = {key: value for key, value in params.items() if value not in (None, "")}
        cache_key = (self.profile.base_url, str(self.session.get("userId") or ""), kind,
                     endpoint, json.dumps(params, sort_keys=True, default=str))
        now = time.monotonic()
        with _cache_lock:
            cached = _read_cache.get(cache_key)
            if cached and now - cached[0] < READ_CACHE_TTL:
                return cached[1]
        result = self._request("GET", endpoint, params=params,
                               scopes=(self._READ_SCOPES[kind],))
        with _cache_lock:
            _read_cache[cache_key] = (now, result)
        return result

    @staticmethod
    def _invalidate_reads() -> None:
        """Drop cached reads after a write so the next look is the new truth."""
        with _cache_lock:
            _read_cache.clear()

    def _write(self, action: str, endpoint: str, content: dict, *,
               idempotency_content: Optional[dict] = None,
               scopes: tuple[str, ...]) -> Any:
        user = str(self.session.get("userId") or self.session.get("user", {}).get("id") or "anonymous")
        body = dict(content)
        key = stable_idempotency_key(
            user, action, body if idempotency_content is None else idempotency_content)
        return self._request("POST", endpoint, body=body, idempotency_key=key, scopes=scopes)

    def upload_asset(self, asset: LocalAsset, *, replacing_draft_id: str = "") -> str:
        """Upload one local file into PPOS's own R2 and return its public URL."""
        upload = self._write("storage-presign", ENDPOINTS["presign"], {
            "content_type": asset.media_type,
            "is_video": asset.is_video,
            "size": asset.size,
            **({"replacing_draft_id": replacing_draft_id} if replacing_draft_id else {}),
        }, idempotency_content={"sha256": asset.sha256, "size": asset.size},
            scopes=("storage:write",))
        url = str(upload.get("upload_url") or "")
        public_url = str(upload.get("public_url") or "")
        if not url.startswith("https://") or not public_url.startswith("https://"):
            raise PPOSClientError("PPOS returned an invalid asset upload URL")
        data = asset.path.read_bytes()
        if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
            raise PPOSClientError(f"asset changed after preflight: {asset.reference}")
        # The presigned URL signs this exact length, so R2 refuses anything else.
        upload_headers = {"Content-Type": asset.media_type, "Content-Length": str(asset.size)}
        response = self.requests.put(
            url, data=data, headers=upload_headers,
            timeout=max(60, 30 + asset.size // (256 * 1024)))
        if response.status_code >= 300:
            raise PPOSClientError(
                f"asset upload failed for {asset.reference} (HTTP {response.status_code})")
        return public_url

    def publish_markdown(self, path: str | Path, *, community: str, self_score: float,
                         title: str = "", draft_id: str = "",
                         autonomous: bool = True) -> Any:
        source, markdown, assets = read_markdown(path)
        if type(self_score) not in (int, float) or not 0 <= self_score <= 100:
            raise PPOSClientError("self_score must be a number from 0 to 100")
        if autonomous:
            enforce_policy("publish", community=community, fee_cents=0)
        planned_bytes = sum(asset.size for asset in assets)
        preflight_content = {
            "community_id": community, "title": title or source.stem,
            "description": markdown, "tags": [], "media_type": "markdown",
            "self_score": self_score,
            **({"draft_id": draft_id} if draft_id else {}),
        }
        preflight = self._write(
            "publish-preflight", ENDPOINTS["publish_preflight"],
            # Quote the finished work, media included, instead of learning about
            # a full quota halfway through uploading it.
            {**preflight_content, "planned_asset_bytes": planned_bytes},
            scopes=("works:write",))
        fee = int(preflight.get("review_fee_cents") or 0)
        if autonomous:
            enforce_policy("publish", community=community, fee_cents=fee)
        replacements: dict[str, str] = {}
        for asset in assets:
            public_url = self.upload_asset(asset, replacing_draft_id=draft_id)
            for reference in (asset.references or (asset.reference,)):
                replacements[reference] = public_url
        rewritten = rewrite_media_references(markdown, replacements)
        commit_content = {**preflight_content, "description": rewritten}
        result = self._write("publish-commit", ENDPOINTS["publish_commit"], commit_content,
                             idempotency_content=preflight_content, scopes=("works:write",))
        self._invalidate_reads()
        if autonomous:
            record_policy_use("publish")
        return result

    def save_draft(self, path: str | Path, *, draft_id: str = "", title: str = "",
                   autonomous: bool = True) -> Any:
        """Save or replace private Markdown without review or a review fee."""
        source, markdown, assets = read_markdown(path)
        if autonomous:
            enforce_policy("publish", fee_cents=0)
        content = {
            "title": title or source.stem,
            "description": markdown,
            "tags": [],
            "media_type": "markdown",
            "self_score": 50,
            **({"draft_id": draft_id} if draft_id else {}),
        }
        planned_bytes = sum({(asset.path, asset.sha256): asset.size for asset in assets}.values())
        self._write(
            "draft-preflight", ENDPOINTS["draft_preflight"],
            {**content, "planned_asset_bytes": planned_bytes},
            scopes=("works:write",))
        replacements: dict[str, str] = {}
        for asset in assets:
            public_url = self.upload_asset(asset, replacing_draft_id=draft_id)
            for reference in (asset.references or (asset.reference,)):
                replacements[reference] = public_url
        rewritten = rewrite_media_references(markdown, replacements)
        result = self._write(
            "draft-save", ENDPOINTS["draft_save"], {**content, "description": rewritten},
            idempotency_content={"draft_id": draft_id, "title": content["title"],
                                 "description": markdown},
            scopes=("works:write",))
        self._invalidate_reads()
        if autonomous:
            record_policy_use("publish")
        return result

    def update_work(self, work_id: str, *, title: str = "", markdown_path: str | Path = "",
                    tags: Optional[list] = None, self_score: Optional[float] = None,
                    community: str = "", autonomous: bool = True) -> Any:
        """Edit an existing work. Media in a replacement Markdown is re-uploaded.

        Changing text, title or score re-enters platform review and re-charges
        the review fee, so it goes through the same fee cap as publishing.
        """
        patch: dict[str, Any] = {}
        if title:
            patch["title"] = title
        if tags is not None:
            patch["tags"] = list(tags)
        if self_score is not None:
            if type(self_score) not in (int, float) or not 0 <= self_score <= 100:
                raise PPOSClientError("self_score must be a number from 0 to 100")
            patch["self_score"] = self_score
        if community:
            patch["community_id"] = community
        assets: list[LocalAsset] = []
        markdown = ""
        if markdown_path:
            _source, markdown, assets = read_markdown(markdown_path)
        if not patch and not markdown:
            raise PPOSClientError("nothing to update: pass a title, tags, score, "
                                  "community, or a Markdown file")
        if autonomous:
            # A text edit re-charges the review fee; anything cheaper still has
            # to be inside the manage cap.
            enforce_policy("manage", community=community,
                           fee_cents=0 if not (markdown or title or self_score is not None) else 100)
        if markdown:
            replacements: dict[str, str] = {}
            for asset in assets:
                public_url = self.upload_asset(asset)
                for reference in (asset.references or (asset.reference,)):
                    replacements[reference] = public_url
            patch["description"] = rewrite_media_references(markdown, replacements)
        result = self._request(
            "PATCH", ENDPOINTS["work"].format(work_id=quote(str(work_id), safe="")),
            body=patch, scopes=("works:write",),
            idempotency_key=stable_idempotency_key(
                str(self.session.get("userId") or "anonymous"), "work-update",
                {"work_id": str(work_id), "patch": patch}))
        self._invalidate_reads()
        if autonomous:
            record_policy_use("manage")
        return result

    def delete_work(self, work_id: str, *, autonomous: bool = True) -> Any:
        if autonomous:
            enforce_policy("manage")
        result = self._write(
            "work-delete", ENDPOINTS["work_delete"].format(work_id=quote(str(work_id), safe="")),
            {}, idempotency_content={"work_id": str(work_id)}, scopes=("works:write",))
        self._invalidate_reads()
        if autonomous:
            record_policy_use("manage")
        return result

    def cleanup_storage(self, *, dry_run: bool = True, min_age_hours: int = 24,
                        autonomous: bool = True) -> Any:
        """Reclaim R2 objects that no live work references any more."""
        if autonomous and not dry_run:
            enforce_policy("manage")
        result = self._write("storage-cleanup", ENDPOINTS["storage_cleanup"],
                             {"dry_run": bool(dry_run), "min_age_hours": int(min_age_hours)},
                             idempotency_content={"dry_run": bool(dry_run),
                                                  "min_age_hours": int(min_age_hours),
                                                  "at": int(time.time() // 60)},
                             scopes=("storage:write",))
        self._invalidate_reads()
        if autonomous and not dry_run:
            record_policy_use("manage")
        return result

    def comment(self, work_id: str, comment: str, *, rating: Optional[int] = None,
                community: str = "", autonomous: bool = True) -> Any:
        if rating is not None and (type(rating) not in (int, float) or not 0 <= rating <= 100):
            raise PPOSClientError("rating must be a number from 0 to 100")
        if autonomous:
            enforce_policy("comment", community=community)
        body = {"body": comment}
        if rating is not None:
            body["rating"] = rating
        result = self._write(
            "comment", ENDPOINTS["comments"].format(work_id=quote(work_id, safe="")), body,
            # Content alone would make a deliberate repeat of the same words look
            # like a retry and silently return the first comment. The minute
            # bucket still absorbs network retries, which is what dedupe is for.
            idempotency_content={"work_id": work_id, "body": body,
                                 "minute": int(time.time() // 60)},
            scopes=("comments:write",))
        self._invalidate_reads()
        if autonomous:
            record_policy_use("comment")
        return result

    def review_decision(self, level: str, review_id: str, decision: str, *,
                        comment: str = "", reason: str = "", evidence: Optional[list] = None,
                        confidence: float, score: Optional[float] = None,
                        community: str = "", autonomous: bool = True) -> Any:
        if level not in ("community", "platform"):
            raise PPOSClientError("review level must be community or platform")
        if decision not in ("approve", "reject", "escalate"):
            raise PPOSClientError("decision must be approve, reject, or escalate")
        if not 0 <= confidence <= 1:
            raise PPOSClientError("confidence must be between 0 and 1")
        evidence = evidence or []
        if not evidence or any(not isinstance(item, dict) or not item.get("claim") or not item.get("source")
                               for item in evidence):
            raise PPOSClientError("review evidence must contain at least one {claim, source} object")
        if decision in ("reject", "escalate") and not (reason.strip() or comment.strip()):
            raise PPOSClientError("reject and escalate decisions require a reason or comment")
        if level == "community" and decision == "approve" and (score is None or not comment.strip()):
            raise PPOSClientError("community approval requires a 0-100 score and comment")
        scope = f"{level}_review"
        if autonomous:
            enforce_policy(scope, community=community, confidence=confidence)
        if level == "community" and not community:
            raise PPOSClientError("community is required for a community review decision")
        endpoint = ENDPOINTS[f"{level}_review_decision"].format(
            review_id=quote(review_id, safe=""), community=quote(community, safe=""),
            decision=decision)
        result = self._write(f"{level}-review-{decision}", endpoint, {
            "comment": comment, "reason": reason,
            "evidence": evidence, "confidence": confidence,
            **({"score": score} if score is not None else {}),
        }, scopes=(f"{level}_reviews:write",))
        self._invalidate_reads()
        if autonomous:
            record_policy_use(scope)
        return result
