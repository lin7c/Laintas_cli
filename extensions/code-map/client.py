"""Laintas Code Map HTTP client (code-map extension).

Code Map builds a layered map of a repository on the server: a deterministic
index of modules, declarations and real call edges, with a model naming each
layer. A build takes minutes to hours, so nothing here waits for one — a build
is queued, its progress is polled, and the finished map is read as text.

Authentication is the Laintas session the CLI already holds; there is no Code
Map key. Requests go through the gateway, which resolves the account the same
way it does for search and chat.
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

BASE_PATH = "/api/agent/code-map"
MAP_ID = re.compile(r"^[0-9a-f]{32}$")
TIMEOUT = 30

#: The three prompt stages a caller may replace. The repair and assignment
#: prompts are fixed on the server: they carry the contract its verifier reads
#: back, so replacing them changes whether a build can recover, not what the
#: map says.
PROMPT_STAGES = ("l1_brief", "l1_plan", "l2_design")


class CodeMapError(RuntimeError):
    """Something the caller has to be told about, in words they can act on."""


def _call(method: str, path: str, *, params: dict | None = None,
          body: dict | None = None) -> Any:
    # Imported here, not at module scope: the extension host loads this file
    # by path, and a top-level import of a CLI module would make the client
    # unimportable outside a running CLI -- including from its own tests.
    from web_search import laintas_session

    auth = laintas_session()
    if auth is None:
        raise CodeMapError("not signed in to Laintas — run /login")
    base_url, headers, cookies = auth
    request_headers = dict(headers)
    request_headers["Content-Type"] = "application/json"
    try:
        response = requests.request(
            method, f"{base_url}{BASE_PATH}{path}", params=params, json=body,
            headers=request_headers, cookies=cookies, timeout=TIMEOUT,
            allow_redirects=False)
    except requests.RequestException as error:
        raise CodeMapError(f"could not reach Laintas: {error}") from error

    if response.status_code == 204:
        return {}
    try:
        payload = response.json()
    except ValueError:
        raise CodeMapError(
            f"unexpected reply from Laintas (HTTP {response.status_code})") from None
    if response.ok:
        return payload
    raise CodeMapError(_explain(response.status_code, payload))


def _explain(status: int, payload: dict) -> str:
    """The server's own words where it has them; ours only where it does not.

    Code Map states why it refused — one build at a time, quota full, no such
    model — and each of those is something the caller can act on. Replacing
    them with a status code would throw that away.
    """
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    for key in ("detail", "title", "message"):
        if isinstance(payload.get(key), str) and payload[key]:
            return payload[key]
    if isinstance(error, str) and error:
        return error
    if status == 401:
        return "not signed in to Laintas — run /login"
    return f"Code Map refused the request (HTTP {status})"


def build(repo_url: str, ref: str = "HEAD", *, title: str = "",
          model: str = "", prompts: dict | None = None,
          prompt_name: str = "") -> dict:
    """Queue a build. Returns the job record; it is not finished yet."""
    body: dict[str, Any] = {"repoUrl": repo_url, "ref": ref or "HEAD"}
    if title:
        body["title"] = title
    if model:
        body["model"] = model
    if prompts:
        unknown = sorted(set(prompts) - set(PROMPT_STAGES))
        if unknown:
            raise CodeMapError(
                f"unknown prompt stage(s): {', '.join(unknown)}; "
                f"editable stages are {', '.join(PROMPT_STAGES)}")
        body["prompts"] = prompts
    if prompt_name:
        body["promptName"] = prompt_name
    return _call("POST", "/maps", body=body)


def status(map_id: str) -> dict:
    if not MAP_ID.fullmatch(map_id or ""):
        raise CodeMapError("a map id is 32 hex characters")
    return _call("GET", f"/maps/{map_id}")


def maps() -> list[dict]:
    return _call("GET", "/maps").get("maps") or []


def outline(map_id: str, node: str = "") -> str:
    """The finished map as Markdown — names, summaries, arrows, no geometry."""
    return read(map_id, node).get("outline", "")


def read(map_id: str, node: str = "") -> dict:
    """The outline plus what it is a picture of: repository, ref, commit, date.

    A map is built once and read for weeks. Without the commit beside it,
    a paragraph written against last month's tree reads exactly like one
    written this morning, and a reader who trusts it walks a path that is no
    longer there.
    """
    if not MAP_ID.fullmatch(map_id or ""):
        raise CodeMapError("a map id is 32 hex characters")
    payload = _call("GET", f"/maps/{map_id}/outline",
                    params={"node": node} if node else None)
    return {
        "outline": str(payload.get("outline") or ""),
        "repository": str(payload.get("source_url") or ""),
        "ref": str(payload.get("source_ref") or ""),
        "commit": str(payload.get("commit") or ""),
        "built_at": int(payload.get("built_at") or 0),
    }


def delete(map_id: str) -> bool:
    if not MAP_ID.fullmatch(map_id or ""):
        raise CodeMapError("a map id is 32 hex characters")
    _call("DELETE", f"/maps/{map_id}")
    return True


def capacity() -> dict:
    return _call("GET", "/capacity")


def models() -> list[dict]:
    return _call("GET", "/models").get("models") or []


def prompts() -> dict[str, str]:
    stages = _call("GET", "/prompts").get("stages") or []
    return {stage["id"]: stage["prompt"] for stage in stages}


def describe(job: dict) -> str:
    """One line about a job, for a person reading a list."""
    repo = str(job.get("source_url") or "").replace("https://github.com/", "")
    state = job.get("status", "?")
    if state in ("queued", "running"):
        state = f"{state} {job.get('progress', 0)}% — {job.get('step', '')}".strip()
    elif state == "failed" and job.get("error"):
        state = f"failed — {job['error']}"
    stored = int(job.get("stored_bytes") or 0)
    size = f" · {stored // 1024} KB" if stored else ""
    return (f"{job.get('id', '')[:8]}  {job.get('title', '')}  "
            f"[{repo}@{job.get('source_ref', '')}]  {state}{size}")


def summarize_capacity(payload: dict) -> str:
    limits = payload.get("limits") or {}
    used = payload.get("used") or {}
    line = (f"maps {used.get('maps', 0)}/{limits.get('maps', 0)} · "
            f"prompt sets {used.get('presets', 0)}/{limits.get('presets', 0)}")
    if limits.get("degraded"):
        return line + " (plan unavailable; free allowance assumed)"
    if limits.get("packs"):
        line += f" · {limits['packs']} bought"
    return line + (" · membership" if limits.get("member") else "")


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
