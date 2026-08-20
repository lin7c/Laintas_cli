"""contract_store.py — the API contract two agents coordinate through.

When Helpwo writes the frontend and laintas_cli writes the backend, the thing
they have to agree on is the interface between them. Today that agreement is
carried by a human retelling it in two chat windows, which is exactly the
failure mode multi-agent work is known for: each agent optimises its own task
in isolation and the seam between them rots.

The fix is not a better message. It is a **file**:

    .laintas/contract/openapi.json      the interface itself (OpenAPI 3.1)
    .laintas/contract/contract.lock.json who agreed to what, and whether the
                                         code still matches

A file is the only thing both sides can reach in both topologies. When Helpwo
runs against a remote CLI it edits that CLI's disk over the P2P filesystem;
when it runs against the local bridge it edits the same disk over loopback.
Either way the contract lives in the repository, so it is versioned, diffable,
reviewable, and — unlike a message — still true after both processes restart.
That last property matters more than it looks: a delegated CLI run starts with
an empty conversation history by design (laintas_cli._handle_delegate), so the
contract file is also the backend agent's memory of what it agreed to.

Why JSON and not YAML: OpenAPI is defined for both, YAML is nicer to read, and
Helpwo has no YAML parser in the browser while the CLI's is only a transitive
dependency. A format one of the two participants cannot parse is not a shared
source of truth, so the machine-readable copy is JSON on both sides.

## The state machine

    proposed ──agree──> agreed ──implement──> implemented ──verify──> verified
        ^                  ^                       │                     │
        └───── counter ────┘                       └──── drift ──────────┘

`proposed`     a consumer (normally the frontend) says what it needs. It can
               start building against a generated mock immediately — waiting
               for the backend is the stall this design exists to remove.
`agreed`       the provider accepted the shape. Now it is a commitment.
`implemented`  the provider says the code exists, and names the files.
`verified`     a real request was made and the real response matched the
               declared schema. Nothing else counts as done.
`drift`        the code moved without the contract moving, or the contract
               moved without the code. Both are reported; neither is silently
               tolerated, because a contract nobody enforces is a comment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

CONTRACT_DIR = ".laintas/contract"
SPEC_FILE = "openapi.json"
LOCK_FILE = "contract.lock.json"

STATES = ("proposed", "agreed", "implemented", "verified", "drift")
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

_MAX_HISTORY = 50
_MAX_PROBE_BYTES = 256 * 1024
_lock = threading.RLock()


class ContractError(Exception):
    """Something the caller asked for cannot be done as asked."""


# ---------------------------------------------------------------------------
# Paths and IO
# ---------------------------------------------------------------------------

def contract_dir(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()) / CONTRACT_DIR


def spec_path(cwd: Optional[str] = None) -> Path:
    return contract_dir(cwd) / SPEC_FILE


def lock_path(cwd: Optional[str] = None) -> Path:
    return contract_dir(cwd) / LOCK_FILE


def _empty_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Project API", "version": "0.1.0",
                 "description": "Shared contract between the Helpwo frontend "
                                "and the laintas_cli backend. Edit through the "
                                "contract tools so the lock file stays in step."},
        "servers": [],
        "paths": {},
    }


def _empty_lock() -> dict:
    return {"version": 1, "updatedAt": _now(), "operations": {}}


def _now() -> float:
    return round(time.time(), 3)


def _read_json(path: Path, fallback: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(fallback)
    except (OSError, ValueError) as e:
        raise ContractError(f"{path} is not readable JSON: {e}") from e
    if not isinstance(data, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def load(cwd: Optional[str] = None) -> tuple[dict, dict]:
    with _lock:
        return (_read_json(spec_path(cwd), _empty_spec()),
                _read_json(lock_path(cwd), _empty_lock()))


# The contract lives under `.laintas/`, which every laintas_cli project
# gitignores — that directory is this CLI's runtime state and checking it in is
# a documented mistake. The contract is the one thing in there that is NOT
# runtime state: it is the interface two agents agreed on, and it is worthless
# unless it is versioned and shows up in review.
#
# Git cannot re-include a file whose parent directory is excluded, so a bare
# `!.laintas/contract/**` would silently do nothing. The three lines below
# un-exclude the directory, re-exclude everything directly inside it, then
# un-exclude the contract — which is the documented way to carve one path out
# of an ignored tree.
_GITIGNORE_MARKER = "!.laintas/contract/"
_GITIGNORE_BLOCK = """
# laintas_cli's project state stays ignored; the API contract does not. It is
# the agreement between the frontend and backend agents, so it belongs in
# review and in history like any other interface definition.
!.laintas/
.laintas/*
!.laintas/contract/
"""


def _ensure_gitignore_exception(cwd: Optional[str] = None) -> bool:
    """Make sure the contract is committable. Best-effort and idempotent.

    Returns whether the file was modified. Does nothing when there is no
    .gitignore to amend, when `.laintas/` was never ignored in the first place,
    or when the exception is already present.
    """
    try:
        path = Path(cwd or os.getcwd()) / ".gitignore"
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        if _GITIGNORE_MARKER in text:
            return False
        lines = {line.strip() for line in text.splitlines()}
        if not ({".laintas/", ".laintas", "/.laintas/", "/.laintas"} & lines):
            return False        # nothing is excluding it; no exception needed
        with open(path, "a", encoding="utf-8") as f:
            if not text.endswith("\n"):
                f.write("\n")
            f.write(_GITIGNORE_BLOCK)
        return True
    except OSError:
        return False


def save(spec: dict, lock: dict, cwd: Optional[str] = None) -> None:
    with _lock:
        first_time = not spec_path(cwd).exists()
        lock["updatedAt"] = _now()
        _write_json(spec_path(cwd), spec)
        _write_json(lock_path(cwd), lock)
        if first_time:
            _ensure_gitignore_exception(cwd)


# ---------------------------------------------------------------------------
# Operation keys
# ---------------------------------------------------------------------------

_OP_RE = re.compile(r"^(GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE)\s+(/\S*)$")


def normalize_operation(operation: str) -> tuple[str, str, str]:
    """"get /api/x" → ("GET /api/x", "get", "/api/x"). Raises on nonsense."""
    text = " ".join(str(operation or "").split())
    parts = text.split(" ", 1)
    if len(parts) != 2:
        raise ContractError(
            f"operation must be '<METHOD> <path>', e.g. 'GET /api/orders' (got {operation!r})")
    method, path = parts[0].upper(), parts[1]
    match = _OP_RE.match(f"{method} {path}")
    if not match:
        raise ContractError(
            f"operation must be '<METHOD> <path>' with an absolute path (got {operation!r})")
    return f"{method} {path}", method.lower(), path


def _spec_operation(spec: dict, method: str, path: str) -> Optional[dict]:
    item = (spec.get("paths") or {}).get(path)
    if not isinstance(item, dict):
        return None
    op = item.get(method)
    return op if isinstance(op, dict) else None


def _operation_fingerprint(spec: dict, method: str, path: str) -> str:
    """Stable hash of one operation's declared shape.

    Compared against what was recorded at `agree` time, so a contract edited
    after the fact demotes the operation instead of quietly disagreeing with
    the code that implements it.
    """
    op = _spec_operation(spec, method, path) or {}
    blob = json.dumps(op, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _files_fingerprint(files: list, cwd: Optional[str] = None) -> str:
    """Hash of the implementing files' contents, in the order given."""
    digest = hashlib.sha256()
    base = Path(cwd or os.getcwd())
    for rel in files:
        target = (base / rel).resolve()
        digest.update(str(rel).encode("utf-8"))
        try:
            digest.update(target.read_bytes())
        except OSError:
            digest.update(b"\0<missing>")
    return digest.hexdigest()[:32]


def _entry(lock: dict, key: str) -> dict:
    entry = lock.setdefault("operations", {}).setdefault(key, {})
    entry.setdefault("state", "proposed")
    entry.setdefault("history", [])
    return entry


def _record(entry: dict, actor: str, state: str, note: str = "") -> None:
    entry["state"] = state
    entry["actor"] = actor
    entry["updatedAt"] = _now()
    entry["history"].append({"t": _now(), "actor": actor, "state": state, "note": note[:400]})
    if len(entry["history"]) > _MAX_HISTORY:
        del entry["history"][:len(entry["history"]) - _MAX_HISTORY]


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def propose(operation: str, definition: dict, actor: str,
            note: str = "", cwd: Optional[str] = None) -> dict:
    """Declare (or re-declare) what an endpoint should look like.

    Re-proposing an operation that is already agreed is a counter-offer: it
    goes back to `proposed`, because the other side agreed to something else
    and has to see the change rather than inherit it.
    """
    key, method, path = normalize_operation(operation)
    if not isinstance(definition, dict) or not definition:
        raise ContractError("definition must be a non-empty OpenAPI operation object")

    spec, lock = load(cwd)
    paths = spec.setdefault("paths", {})
    item = paths.setdefault(path, {})
    if not isinstance(item, dict):
        raise ContractError(f"paths[{path}] in the spec is not an object")
    previous_state = _entry(lock, key).get("state")
    item[method] = definition

    entry = _entry(lock, key)
    entry["proposedBy"] = actor
    entry["specHash"] = _operation_fingerprint(spec, method, path)
    _record(entry, actor, "proposed",
            note or (f"counter-offer (was {previous_state})"
                     if previous_state in ("agreed", "implemented", "verified") else "proposed"))
    save(spec, lock, cwd)
    return {"ok": True, "operation": key, "state": "proposed",
            "wasState": previous_state, "specHash": entry["specHash"]}


def agree(operation: str, actor: str, note: str = "", cwd: Optional[str] = None) -> dict:
    """Accept a proposed shape. From here it is a commitment, not a wish."""
    key, method, path = normalize_operation(operation)
    spec, lock = load(cwd)
    if _spec_operation(spec, method, path) is None:
        raise ContractError(f"{key} is not in the spec — propose it first")
    entry = _entry(lock, key)
    if entry.get("state") not in ("proposed", "drift"):
        raise ContractError(
            f"{key} is {entry.get('state')}, not proposed — re-propose it to change the shape")
    entry["agreedBy"] = actor
    entry["specHash"] = _operation_fingerprint(spec, method, path)
    _record(entry, actor, "agreed", note)
    save(spec, lock, cwd)
    return {"ok": True, "operation": key, "state": "agreed"}


def implement(operation: str, actor: str, files: list, base_url: str = "",
              note: str = "", cwd: Optional[str] = None) -> dict:
    """Claim an agreed operation is now built, naming the files that build it.

    The files are not decoration: their combined hash is what `drift` compares
    against later, so an implementation that changes without the contract
    changing is detectable instead of merely regrettable.
    """
    key, method, path = normalize_operation(operation)
    if not files:
        raise ContractError("name at least one file that implements this operation")
    spec, lock = load(cwd)
    if _spec_operation(spec, method, path) is None:
        raise ContractError(f"{key} is not in the spec")
    entry = _entry(lock, key)
    if entry.get("state") == "proposed":
        raise ContractError(
            f"{key} has not been agreed yet — agree to the shape before building it")

    base = Path(cwd or os.getcwd())
    missing = [f for f in files if not (base / f).exists()]
    if missing:
        raise ContractError(f"these files do not exist: {', '.join(missing)}")

    entry["implementedBy"] = actor
    entry["implFiles"] = list(files)
    entry["implHash"] = _files_fingerprint(files, cwd)
    entry["specHash"] = _operation_fingerprint(spec, method, path)
    if base_url:
        entry["baseUrl"] = base_url
    _record(entry, actor, "implemented", note)
    save(spec, lock, cwd)
    return {"ok": True, "operation": key, "state": "implemented",
            "implHash": entry["implHash"]}


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

def drift(cwd: Optional[str] = None, mark: bool = False) -> dict:
    """Find every operation whose contract and code have parted ways.

    Two directions, both of which matter:

      spec-moved   the declared shape changed after it was agreed. The code
                   that was written against the old shape is now wrong.
      code-moved   an implementing file changed after `implement`. The code
                   may no longer do what the contract says it does.

    `mark=True` writes the finding back as state `drift`, which is what a
    pre-commit or CI check wants; the default is read-only so an agent can ask
    without changing anything.
    """
    spec, lock = load(cwd)
    findings = []
    for key, entry in sorted((lock.get("operations") or {}).items()):
        try:
            _, method, path = normalize_operation(key)
        except ContractError:
            continue
        state = entry.get("state")
        if state not in ("agreed", "implemented", "verified"):
            continue
        reasons = []
        current_spec_hash = _operation_fingerprint(spec, method, path)
        if entry.get("specHash") and current_spec_hash != entry["specHash"]:
            reasons.append("the declared shape changed after it was agreed")
        if entry.get("implFiles"):
            current_impl = _files_fingerprint(entry["implFiles"], cwd)
            if entry.get("implHash") and current_impl != entry["implHash"]:
                reasons.append("an implementing file changed after it was declared done")
        if reasons:
            findings.append({"operation": key, "wasState": state, "reasons": reasons,
                             "files": entry.get("implFiles") or []})
            if mark:
                _record(entry, "drift-check", "drift", "; ".join(reasons))

    if mark and findings:
        save(spec, lock, cwd)
    return {"ok": not findings, "drifted": findings, "checked": len(lock.get("operations") or {})}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _schema_problems(value: Any, schema: dict, where: str = "response") -> list:
    """A deliberately small JSON-Schema subset: type, required, properties, items.

    Enough to catch the mistakes that actually happen between a frontend and a
    backend — a field renamed, a list that turned into an object, a number sent
    as a string — without dragging a validator dependency into a module that
    has to run on both sides of this project.
    """
    problems: list[str] = []
    if not isinstance(schema, dict):
        return problems

    declared = schema.get("type")
    types = declared if isinstance(declared, list) else ([declared] if declared else [])
    if types:
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
        }
        if not any(checks.get(t, lambda _v: True)(value) for t in types):
            problems.append(f"{where}: expected {'/'.join(types)}, got {type(value).__name__}")
            return problems  # a wrong type makes every nested complaint noise

    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{where}: missing required property '{name}'")
        for name, sub in (schema.get("properties") or {}).items():
            if name in value:
                problems.extend(_schema_problems(value[name], sub, f"{where}.{name}"))
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value[:20]):
            problems.extend(_schema_problems(item, schema["items"], f"{where}[{index}]"))
    return problems


def _resolve_ref(spec: dict, schema: Any) -> Any:
    """Follow a local $ref one level; anything else is returned unchanged."""
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref.startswith("#/"):
            node: Any = spec
            for part in ref[2:].split("/"):
                if not isinstance(node, dict) or part not in node:
                    return {}
                node = node[part]
            return node
    return schema


def verify(operation: str = "", base_url: str = "", cwd: Optional[str] = None,
           timeout: int = 15) -> dict:
    """Make the real request and check the real answer against the contract.

    This is the only transition that is not a claim. `implemented` is an agent
    saying it built the thing; `verified` is the thing answering correctly.
    """
    import urllib.error
    import urllib.request

    spec, lock = load(cwd)
    operations = lock.get("operations") or {}
    if operation:
        key, _method, _path = normalize_operation(operation)
        if key not in operations:
            raise ContractError(f"{key} is not in the contract")
        targets = [key]
    else:
        targets = [k for k, v in operations.items()
                   if v.get("state") in ("implemented", "verified")]
    if not targets:
        return {"ok": True, "results": [], "note": "nothing is implemented yet"}

    results = []
    for key in targets:
        entry = operations[key]
        _key, method, path = normalize_operation(key)
        url_base = (base_url or entry.get("baseUrl")
                    or ((spec.get("servers") or [{}])[0] or {}).get("url") or "")
        if not url_base:
            results.append({"operation": key, "ok": False,
                            "error": "no base URL — pass base_url or set servers[0].url"})
            continue
        if "{" in path:
            results.append({"operation": key, "ok": False,
                            "error": "path has parameters; verify it with an explicit base_url "
                                     "and a concrete path instead"})
            continue

        url = url_base.rstrip("/") + path
        request = urllib.request.Request(url, method=method.upper())
        request.add_header("Accept", "application/json")
        status, body, error = 0, b"", ""
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                body = response.read(_MAX_PROBE_BYTES)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read(_MAX_PROBE_BYTES)
            except OSError:
                body = b""
        except (urllib.error.URLError, OSError, ValueError) as e:
            error = f"could not reach {url}: {e}"

        if error:
            results.append({"operation": key, "ok": False, "url": url, "error": error})
            _record(entry, "verify", entry.get("state", "implemented"), error)
            continue

        problems = []
        op_spec = _spec_operation(spec, method, path) or {}
        responses = op_spec.get("responses") or {}
        declared = responses.get(str(status)) or responses.get("default")
        if declared is None:
            problems.append(f"status {status} is not declared "
                            f"(contract declares {', '.join(sorted(responses)) or 'nothing'})")
        else:
            schema = (((declared.get("content") or {}).get("application/json") or {})
                      .get("schema"))
            if isinstance(schema, dict):
                try:
                    payload = json.loads(body.decode("utf-8", "replace") or "null")
                except ValueError:
                    problems.append("response is not valid JSON")
                else:
                    problems.extend(_schema_problems(payload, _resolve_ref(spec, schema)))

        ok = not problems
        results.append({"operation": key, "ok": ok, "url": url, "status": status,
                        "problems": problems})
        entry["lastVerify"] = {"t": _now(), "ok": ok, "status": status,
                               "problems": problems[:20]}
        _record(entry, "verify", "verified" if ok else "drift",
                "verified" if ok else "; ".join(problems)[:400])

    save(spec, lock, cwd)
    return {"ok": all(r.get("ok") for r in results), "results": results}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def status(cwd: Optional[str] = None) -> dict:
    """One-glance summary: how many operations sit in each state."""
    _spec, lock = load(cwd)
    counts: dict[str, int] = {}
    for entry in (lock.get("operations") or {}).values():
        state = entry.get("state", "proposed")
        counts[state] = counts.get(state, 0) + 1
    return {
        "ok": True,
        "path": str(contract_dir(cwd)),
        "exists": spec_path(cwd).exists(),
        "counts": counts,
        "total": sum(counts.values()),
        "updatedAt": lock.get("updatedAt"),
    }


def read(operation: str = "", state: str = "", cwd: Optional[str] = None) -> dict:
    """The contract, or the slice of it a caller actually needs.

    Filtering is not a convenience. A frontend agent should be given the four
    endpoints it calls, not the whole surface of the backend — a role-specific
    view keeps the useful part of the contract inside the part of the context
    window the model still reads carefully.
    """
    spec, lock = load(cwd)
    operations = lock.get("operations") or {}
    if operation:
        key, method, path = normalize_operation(operation)
        entry = operations.get(key)
        if entry is None and _spec_operation(spec, method, path) is None:
            raise ContractError(f"{key} is not in the contract")
        return {"ok": True, "operation": key,
                "definition": _spec_operation(spec, method, path),
                "lock": entry or {"state": "unknown"}}

    selected = {}
    for key, entry in sorted(operations.items()):
        if state and entry.get("state") != state:
            continue
        _key, method, path = normalize_operation(key)
        selected[key] = {
            "state": entry.get("state"),
            "proposedBy": entry.get("proposedBy"),
            "implementedBy": entry.get("implementedBy"),
            "definition": _spec_operation(spec, method, path),
        }
    return {"ok": True, "info": spec.get("info"), "servers": spec.get("servers"),
            "operations": selected, "count": len(selected)}


def mock_response(operation: str, cwd: Optional[str] = None) -> dict:
    """A sample response synthesised from the declared schema.

    The point of proposing before building is that the consumer does not have
    to wait, so the contract has to be able to answer for the provider until
    the provider exists.
    """
    key, method, path = normalize_operation(operation)
    spec, _lock = load(cwd)
    op_spec = _spec_operation(spec, method, path)
    if op_spec is None:
        raise ContractError(f"{key} is not in the spec")
    responses = op_spec.get("responses") or {}
    status_code = next((c for c in sorted(responses) if c.startswith("2")), None) or "default"
    declared = responses.get(status_code) or {}
    schema = ((declared.get("content") or {}).get("application/json") or {}).get("schema")
    return {"ok": True, "operation": key, "status": status_code,
            "body": _sample(_resolve_ref(spec, schema), spec)}


def _sample(schema: Any, spec: dict, depth: int = 0) -> Any:
    schema = _resolve_ref(spec, schema)
    if not isinstance(schema, dict) or depth > 6:
        return None
    if "example" in schema:
        return schema["example"]
    declared = schema.get("type")
    kind = declared[0] if isinstance(declared, list) and declared else declared
    if kind == "object":
        return {name: _sample(sub, spec, depth + 1)
                for name, sub in (schema.get("properties") or {}).items()}
    if kind == "array":
        # No item schema means nothing was promised about the elements, so an
        # empty list is the honest sample; [null] just looks like a bug.
        items = schema.get("items")
        return [_sample(items, spec, depth + 1)] if isinstance(items, dict) else []
    if kind == "string":
        return schema.get("default", "string")
    if kind in ("number", "integer"):
        return schema.get("default", 0)
    if kind == "boolean":
        return schema.get("default", False)
    return None
