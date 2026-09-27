"""Where the conversation model is called: through the backend, or directly.

Two independent choices; this module owns the second one:

  /backend use <profile>    which backend (gateway) this terminal talks to
  /model route <route>      who serves the chat model (also in the /model picker)

The route ``laintas`` (the default) means "whatever the backend serves" — the
behaviour the CLI always had. Any other route is a provider key the user added
with `/model add`: the chat request then goes straight from this
machine to that provider's OpenAI-compatible endpoint, with the user's key, and
never passes through the backend. Everything that is not the chat model —
search, rerank, OCR, image generation, /usage from the gateway — still comes
from the backend. Only the one call is rerouted.

Trust rules, in the same spirit as `backend_profiles`:

  * A provider key is a separate credential from the Laintas session and is
    never sent anywhere but the provider it was added for.
  * Keys live in their own owner-only file, not in `model_providers.json`, so
    the route list can be read or shown without the secrets in it.
  * The selection is terminal-scoped and is NOT seeded into new terminals
    (see `terminal_preferences.SEEDED_KEYS`): a direct route bypasses the
    backend's billing and audit, so a new terminal starts on ``laintas``.
  * The policy engine can forbid direct routes (``directModel: "deny"``) or
    restrict them to named hosts (``directModelHosts``). The organisation layer
    merges its policy into the same config, strictness only, so a member cannot
    route around an organisation that keeps its conversations on its gateway.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import json_store
import paths
import terminal_preferences


#: The route that means "use the backend". Reserved: cannot be a provider name.
DEFAULT_ROUTE = "laintas"
#: Subcommand words of `/model route`; a provider may not shadow them.
RESERVED_NAMES = frozenset({DEFAULT_ROUTE, "add", "remove", "list", "status",
                            "models", "help"})
PREF_KEY = "model_route"
ENV_OVERRIDE = "LAINTAS_MODEL_ROUTE"


@dataclass(frozen=True)
class Preset:
    label: str
    base_url: str
    #: OpenAI renamed the output ceiling for its reasoning models; everybody
    #: else still takes `max_tokens`.
    max_tokens_param: str = "max_tokens"
    #: `stream_options.include_usage` gives real token counts for /usage. A
    #: strict custom server may reject the unknown field, hence a switch.
    stream_usage: bool = True


PRESETS: dict[str, Preset] = {
    "openai": Preset("OpenAI", "https://api.openai.com/v1",
                     max_tokens_param="max_completion_tokens"),
    # Anthropic's OpenAI-compatible endpoint; it takes the key as a Bearer
    # token on /models and /chat/completions alike.
    "claude": Preset("Claude (Anthropic)", "https://api.anthropic.com/v1"),
    "custom": Preset("Custom", "", stream_usage=False),
}


@dataclass(frozen=True)
class DirectRoute:
    name: str
    preset: str
    base_url: str
    model: str
    auth: str = "stored"
    #: Tokens, from the provider's /models listing or set by the user; 0 means
    #: unknown and the CLI falls back to `/config budget assumed_window`.
    context_window: int = 0
    #: The provider refused `reasoning_effort` for this model once; the CLI
    #: stops sending it rather than failing every turn.
    reasoning_unsupported: bool = False

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()

    @property
    def spec(self) -> Preset:
        return PRESETS.get(self.preset, PRESETS["custom"])


# ── storage ─────────────────────────────────────────────────────────────────

_READ_CACHE: dict = {}


def _read(path) -> dict:
    # mtime-cached: the status bar and the thinking spinner ask which model is
    # live many times a second, and that answer reads these files.
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    hit = _READ_CACHE.get(str(path))
    if hit and hit[0] == stamp:
        return json.loads(hit[1])
    if not path.is_file() or not paths.ensure_private_file(path):
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    _READ_CACHE[str(path)] = (stamp, text)
    return data


def _providers() -> dict:
    entries = _read(paths.MODEL_PROVIDERS_FILE).get("providers")
    return entries if isinstance(entries, dict) else {}


def _write_providers(entries: dict) -> None:
    json_store.save_json_atomic(paths.MODEL_PROVIDERS_FILE,
                                {"version": 1, "providers": entries}, mode=0o600)


def normalize_url(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("provider URL must be an absolute http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("provider URL must not contain credentials, query or fragment")
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not local:
        # The key and every prompt travel this connection.
        raise ValueError("provider URL must be https (http only for localhost)")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _route_from(name: str, entry: dict) -> Optional[DirectRoute]:
    if not isinstance(entry, dict):
        return None
    try:
        url = normalize_url(str(entry.get("baseUrl") or ""))
    except ValueError:
        return None
    model = str(entry.get("model") or "")
    windows = entry.get("windows") if isinstance(entry.get("windows"), dict) else {}
    try:
        window = int(windows.get(model) or 0)
    except (TypeError, ValueError):
        window = 0
    return DirectRoute(name=name, preset=str(entry.get("preset") or "custom"),
                       base_url=url, model=model,
                       auth=str(entry.get("auth") or "stored"),
                       context_window=max(0, window),
                       reasoning_unsupported=model in (entry.get("noReasoning") or []))


def list_routes() -> list[DirectRoute]:
    routes = [_route_from(name, entry) for name, entry in _providers().items()]
    return sorted((r for r in routes if r), key=lambda r: r.name)


def get_route(name: str) -> Optional[DirectRoute]:
    return _route_from(name, _providers().get(name))


def api_key(route: DirectRoute) -> str:
    if route.auth.startswith("env:"):
        variable = route.auth[4:]
        return os.environ.get(variable, "").strip() if variable else ""
    keys = _read(paths.MODEL_KEYS_FILE)
    return str(keys.get(route.name) or "").strip()


def key_preview(route: DirectRoute) -> str:
    if route.auth.startswith("env:"):
        return f"${route.auth[4:]}"
    key = api_key(route)
    return f"{key[:3]}…{key[-4:]}" if len(key) > 10 else ("set" if key else "missing")


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
        return "name must be 1-64 chars of letters, digits, . _ -"
    if name.lower() in RESERVED_NAMES:
        return f"{name!r} is reserved"
    return ""


def save_route(name: str, preset: str, base_url: str, model: str,
               key: str = "", key_env: str = "",
               windows: Optional[dict] = None) -> DirectRoute:
    problem = validate_name(name)
    if problem:
        raise ValueError(problem)
    if preset not in PRESETS:
        raise ValueError(f"unknown preset: {preset}")
    url = normalize_url(base_url or PRESETS[preset].base_url)
    if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ValueError("key variable must be a plain environment variable name")

    entries = _providers()
    entries[name] = {"preset": preset, "baseUrl": url, "model": model,
                     "auth": f"env:{key_env}" if key_env else "stored",
                     "windows": {k: int(v) for k, v in (windows or {}).items()
                                 if isinstance(v, int) and v > 0}}
    keys = _read(paths.MODEL_KEYS_FILE)
    if key_env:
        keys.pop(name, None)
    elif key:
        keys[name] = key
    json_store.save_json_atomic(paths.MODEL_KEYS_FILE, keys, mode=0o600)
    _write_providers(entries)
    return _route_from(name, entries[name])


def set_route_model(name: str, model: str = "", window: int = 0) -> None:
    """Change a route's model and/or record the context window for it."""
    entries = _providers()
    if name not in entries:
        raise ValueError(f"unknown model route: {name}")
    entry = entries[name]
    if model:
        entry["model"] = model
    if window > 0:
        windows = entry.get("windows") if isinstance(entry.get("windows"), dict) else {}
        windows[entry.get("model") or model] = int(window)
        entry["windows"] = windows
    _write_providers(entries)


def mark_reasoning_unsupported(name: str, model: str) -> None:
    entries = _providers()
    entry = entries.get(name)
    if not isinstance(entry, dict):
        return
    refused = list(entry.get("noReasoning") or [])
    if model not in refused:
        entry["noReasoning"] = refused + [model]
        _write_providers(entries)


def remove_route(name: str) -> bool:
    entries = _providers()
    if entries.pop(name, None) is None:
        return False
    _write_providers(entries)
    keys = _read(paths.MODEL_KEYS_FILE)
    if keys.pop(name, None) is not None:
        json_store.save_json_atomic(paths.MODEL_KEYS_FILE, keys, mode=0o600)
    if selected_name() == name:
        select(DEFAULT_ROUTE)
    return True


# ── policy ──────────────────────────────────────────────────────────────────

def _host_allowed(host: str, patterns: list) -> bool:
    for pattern in patterns:
        pattern = str(pattern or "").strip().lower()
        if not pattern:
            continue
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


def policy_refusal(route: DirectRoute, config: Optional[dict] = None) -> str:
    """Why the policy in force forbids *route*, or "" when it may be used."""
    if config is None:
        try:
            import policy
            config = policy.get_config()
        except Exception:
            config = {}
    config = config or {}
    source = "organisation policy" if config.get("_org_policy") else "policy"
    if str(config.get("directModel", "allow")).lower() == "deny":
        return f"{source} forbids direct model routes (directModel: deny)"
    hosts = config.get("directModelHosts")
    if isinstance(hosts, list) and not _host_allowed(route.host, hosts):
        return f"{source} does not allow {route.host} (directModelHosts)"
    return ""


# ── selection ───────────────────────────────────────────────────────────────

def selected_name() -> str:
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return override
    return str(terminal_preferences.get(PREF_KEY, "") or DEFAULT_ROUTE)


def select(name: str) -> None:
    if name == DEFAULT_ROUTE:
        terminal_preferences.delete(PREF_KEY)
    else:
        terminal_preferences.set_value(PREF_KEY, name)


def resolve() -> tuple[Optional[DirectRoute], str]:
    """``(route, note)`` for the current call.

    ``route`` is None when the backend serves the model. ``note`` explains a
    selection that could not be honoured (removed, missing key, forbidden by
    policy). Failing back to the backend rather than failing the turn would
    silently send the conversation somewhere the user did not choose, so the
    caller refuses the turn when ``note`` is set.
    """
    name = selected_name()
    if name == DEFAULT_ROUTE:
        return None, ""
    route = get_route(name)
    if route is None:
        return None, f"model route {name!r} is not configured (/model route list)"
    if not api_key(route):
        return None, f"model route {name!r} has no API key ({key_preview(route)})"
    if not route.model:
        return None, f"model route {name!r} has no model (/model route {name} <model>)"
    refusal = policy_refusal(route)
    if refusal:
        return None, f"model route {name!r} is blocked: {refusal}"
    return route, ""


def active_model() -> str:
    """The model a direct route will call, or "" when the backend serves it.

    Cheap (cached reads, no policy evaluation) because the status bar and the
    window bookkeeping key on it. A blocked route still reports its model
    here; the turn itself is refused in `resolve`.
    """
    name = selected_name()
    if name == DEFAULT_ROUTE:
        return ""
    route = get_route(name)
    return route.model if route else ""


# ── wire ────────────────────────────────────────────────────────────────────

#: `/config reasoning_effort` gears → the OpenAI-compatible `reasoning_effort`
#: values. The gateway does this per (key × model) from a measured table; the
#: direct path has no table, so it sends the nearest standard value and learns
#: from a refusal (`mark_reasoning_unsupported`). `auto` is the gateway's own
#: per-request choice and has no provider equivalent: nothing is sent.
GEAR_TO_EFFORT = {"none": "minimal", "low": "low", "medium": "medium",
                  "high": "high", "max": "high"}

_MESSAGE_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id")


def _clean_message(message: dict) -> dict:
    # Strict providers reject unknown message fields; the gateway strips its
    # own bookkeeping before forwarding, so this end has to as well.
    return {k: message[k] for k in _MESSAGE_KEYS if k in message}


def _messages(payload: dict) -> list:
    if payload.get("messages"):
        thread = [_clean_message(m) for m in payload["messages"] if isinstance(m, dict)]
    else:
        thread = [_clean_message(m) for m in (payload.get("history") or [])
                  if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        if payload.get("message"):
            thread.append({"role": "user", "content": str(payload["message"])})
    system = str(payload.get("systemPrompt") or "")
    if system and not (thread and thread[0].get("role") == "system"):
        thread.insert(0, {"role": "system", "content": system})
    return thread


def build_request(route: DirectRoute, payload: dict) -> tuple[str, dict, dict]:
    """Translate a gateway `/api/chat/stream` payload into a provider request.

    Returns ``(url, body, headers)``. The response is an OpenAI chat-completions
    SSE stream, which is what the gateway passes through as well, so the
    caller's stream parser needs no second implementation.
    """
    spec = route.spec
    body: dict = {"model": route.model, "messages": _messages(payload), "stream": True}
    if payload.get("maxTokens"):
        body[spec.max_tokens_param] = int(payload["maxTokens"])
    effort = GEAR_TO_EFFORT.get(str(payload.get("reasoningEffort") or "").lower())
    if effort and not route.reasoning_unsupported:
        body["reasoning_effort"] = effort
    if payload.get("tools"):
        body["tools"] = payload["tools"]
        body["tool_choice"] = payload.get("tool_choice") or "auto"
    if spec.stream_usage:
        body["stream_options"] = {"include_usage": True}
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key(route)}"}
    return f"{route.base_url}/chat/completions", body, headers


def usage_billing(usage: dict) -> dict:
    """An OpenAI `usage` block in the shape the gateway's `_billing` event has."""
    details = usage.get("prompt_tokens_details") or {}
    return {
        "promptTokens": int(usage.get("prompt_tokens") or 0),
        "completionTokens": int(usage.get("completion_tokens") or 0),
        "cachedPromptTokens": int(details.get("cached_tokens") or 0),
        "costCents": 0,
        "billingDomain": "direct",
        "official": False,
    }


def error_message(body) -> str:
    """Providers nest the reason: ``{"error": {"message": ...}}``."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or err)
        if err:
            return str(err)
        return str(body.get("message") or body.get("detail") or "")
    return ""


def fetch_models(base_url: str, key: str, timeout: float = 20.0) -> list[str]:
    """`GET /models` — proves the key works and lists what it can call."""
    return sorted(fetch_catalog(base_url, key, timeout))


def _window_of(item: dict) -> int:
    # OpenRouter says `context_length`; some vLLM/others `max_model_len`;
    # OpenAI and DeepSeek say nothing, and the user supplies it instead.
    for field in ("context_length", "context_window", "max_model_len", "max_context_length"):
        try:
            value = int(item.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def fetch_catalog(base_url: str, key: str, timeout: float = 20.0) -> dict:
    """``{model id: context window or 0}`` from `GET /models`."""
    import requests
    response = requests.get(f"{normalize_url(base_url)}/models",
                            headers={"Authorization": f"Bearer {key}"},
                            timeout=timeout, allow_redirects=False)
    if response.status_code != 200:
        try:
            reason = error_message(response.json())
        except ValueError:
            reason = response.text[:200]
        raise RuntimeError(f"HTTP {response.status_code}: {reason or 'request refused'}")
    data = response.json().get("data") or []
    return {str(item["id"]): _window_of(item) for item in data
            if isinstance(item, dict) and item.get("id")}
