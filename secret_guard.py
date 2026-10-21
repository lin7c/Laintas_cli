"""Offline credential gate. No samples, verification, logging, or raw findings."""
from functools import lru_cache
import json
import re

BLOCKED = ("Message not sent: possible password or API key detected. "
           "Remove it and try again. Use /password for credentials or /model for model keys.")
UNAVAILABLE = "Message not sent: local security check unavailable. Please try again."
MAX_TEXT = 1_000_000
_PREFIX = re.compile(r"\b(?:KGAT_|sk-|ghp_|gho_|github_pat_|glpat-|xox[baprs]-|AIza)[A-Za-z0-9_-]{16,}")
_PRIVATE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_BEARER = re.compile(r"\b(?:authorization[\"']?\s*[:=]\s*[\"']?\s*)?Bearer\s+([A-Za-z0-9._~+/-]{8,}=*)", re.I)
_FIELD = re.compile(
    r"(?:\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|pwd|secret|token)\b|\u5bc6\u7801|\u53e3\u4ee4|\u5bc6\u94a5|\u4ee4\u724c)"
    r"[\"']?\s*(?:[:=\uff1a]|\bis\b|\u662f|\u4e3a)\s*(?:\"([^\"\r\n]{4,})\"|'([^'\r\n]{4,})'|([^\s\"'`,;\uff0c\uff1b<>]{4,}))", re.I)
_PLACEHOLDER = re.compile(r"^(?:\*+|x+|\[?redacted\]?|(?:your|example|sample|dummy|test)[_-](?:api[_-]?)?(?:key|token|password|secret)|password|secret|token|string|true|false|null|none|required|undefined)$", re.I)


class SecretInputError(ValueError):
    """Safe to display: the exception never contains submitted content."""


def _placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.fullmatch(value.strip())
                or re.fullmatch(r"\$(?:[A-Z_][A-Z0-9_]*|\{[A-Z_][A-Z0-9_]*\})", value)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*\(\)", value))


@lru_cache(maxsize=1)
def _detectors():
    # analyze_string only: analyze_line/scan_line can perform network verification.
    from detect_secrets.plugins.aws import AWSKeyDetector
    from detect_secrets.plugins.azure_storage_key import AzureStorageKeyDetector
    from detect_secrets.plugins.github_token import GitHubTokenDetector
    from detect_secrets.plugins.gitlab_token import GitLabTokenDetector
    from detect_secrets.plugins.openai import OpenAIDetector
    from detect_secrets.plugins.private_key import PrivateKeyDetector
    from detect_secrets.plugins.slack import SlackDetector
    from detect_secrets.plugins.stripe import StripeDetector
    from detect_secrets.plugins.sendgrid import SendGridDetector
    from detect_secrets.plugins.npm import NpmDetector
    from detect_secrets.plugins.pypi_token import PypiTokenDetector
    from detect_secrets.plugins.telegram_token import TelegramBotTokenDetector
    from detect_secrets.plugins.basic_auth import BasicAuthDetector
    return tuple(cls() for cls in (AWSKeyDetector, AzureStorageKeyDetector,
        GitHubTokenDetector, GitLabTokenDetector, OpenAIDetector, PrivateKeyDetector,
        SlackDetector, StripeDetector, SendGridDetector, NpmDetector,
        PypiTokenDetector, TelegramBotTokenDetector, BasicAuthDetector))


def input_error(text: str) -> str:
    """Return an English refusal or ''. Errors fail closed; nothing is persisted."""
    if not text:
        return ""
    try:
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            return UNAVAILABLE
        if _PRIVATE.search(text) or _PREFIX.search(text) or _JWT.search(text):
            return BLOCKED
        for pattern in (_FIELD, _BEARER):
            for match in pattern.finditer(text):
                value = next(v for v in match.groups() if v is not None)
                # Ordinary prose such as 'Bearer authentication' or 'key is
                # configured' is not an explicit credential assignment.
                if value.isalpha() and (
                    (pattern is _BEARER and match.group(0).lower().startswith('bearer'))
                    or (pattern is _FIELD and re.search(r'\bis\b', match.group(0), re.I))
                ):
                    continue
                if not _placeholder(value):
                    return BLOCKED
        for detector in _detectors():
            if any(not _placeholder(value) for value in detector.analyze_string(text)):
                return BLOCKED
        return ""
    except Exception:
        return UNAVAILABLE


def require_safe_input(text: str) -> None:
    error = input_error(text)
    if error:
        raise SecretInputError(error)


def require_safe_payload(value) -> None:
    """Check model content only. Callers exclude authentication headers/keys."""
    if isinstance(value, str):
        require_safe_input(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            require_safe_payload(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            # Binary media is not text and can be large. URLs still get scanned.
            if key in {"url", "image", "image_url", "audio", "video"} and isinstance(item, str) and item.startswith("data:"):
                continue
            if isinstance(item, str):
                require_safe_input(f'{key}={json.dumps(item, ensure_ascii=False)}')
            require_safe_payload(item)
