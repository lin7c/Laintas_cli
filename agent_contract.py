"""What a sub-agent was asked for, and whether it delivered it.

Why this exists
---------------
`agent.spawn` took one free-text `task` and returned one free-text `summary`.
Nothing else: no declared product, no acceptance condition, no bounded scope,
no evidence requirement. So "done" meant "the child stopped talking", the
parent could not tell a finished job from an abandoned one without re-reading
prose, and a child that was cut off mid-run left `"(interrupted)"` — thirteen
tool calls of work with no structure to salvage.

Measured on the 2026-08-28 review batch: two of six children "succeeded", and
both of those ended on `provider_stop` rather than on the completion protocol,
because their role's tool whitelist did not even include the tool the prompt
told them to call. Nothing noticed.

HWO already had the right idea — `io: {in(...), out(...)}` plus `agent_return`
— but only inside the workflow language, and only as a FILTER: a missing
declared output was quietly replaced by a default. This module is that idea
made general and made a gate:

* the contract declares typed outputs, acceptance checks and required evidence;
* `verify()` is deterministic — it reads the workspace, never the model's
  opinion of the workspace;
* a child that fails verification is `rejected`, with the specific gaps sent
  to its parent; the parent decides whether retrying, revising the assignment,
  or accepting the partial result is worth the tokens;
* `returned` (the child's claim) and `verified` (the runtime's finding) are
  different states, because conflating them is what let a stopped agent count
  as a finished one.

The contract is data, so it is also what the critic scores against and what the
scheduler can retry: a node whose inputs are declared and whose output is
checkable can be re-run without asking anyone what it was supposed to do.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

#: Lifecycle of one contracted child. `status` on AgentInfo stays the coarse
#: legacy view (idle/running/done/error); this is the one that distinguishes a
#: claim from a finding.
STAGE_QUEUED = "queued"
STAGE_RUNNING = "running"
STAGE_RETURNED = "returned"      # the child says it is done
STAGE_VERIFIED = "verified"      # the runtime checked and agrees
STAGE_REJECTED = "rejected"      # the runtime checked and does not
STAGE_WAITING_PARENT = "waiting_parent"   # blocked on a question only the caller can answer
STAGE_DONE = "done"
STAGE_FAILED = "failed"

_OUTPUT_TYPES = ("string", "file", "object", "array", "number", "boolean")
_CHECK_KINDS = ("file_exists", "contains", "matches", "min_length",
                "json_object", "line_ref")

#: `path:line` as it appears in a finding. The line must exist in the file, or
#: the citation is decoration.
_LINE_REF = re.compile(r"([\w./\\-]+\.[A-Za-z0-9_]+):(\d+)")


class ContractError(ValueError):
    """The contract itself is malformed — an authoring bug, not a verdict."""


def normalize(raw: Optional[dict]) -> Optional[dict]:
    """Validate and canonicalize a contract. Returns None when there is none.

    Raises ContractError for a contract that cannot be checked, because a
    contract nobody can fail is worse than no contract: it reads like a
    guarantee and enforces nothing.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ContractError("contract must be an object")

    outputs = []
    for item in raw.get("outputs") or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ContractError("each output needs a name")
        typ = str(item.get("type") or "string").strip().lower()
        if typ not in _OUTPUT_TYPES:
            raise ContractError(
                f"unknown output type {typ!r}; use one of {', '.join(_OUTPUT_TYPES)}")
        outputs.append({
            "name": str(item["name"]).strip(),
            "type": typ,
            "required": bool(item.get("required", True)),
            "description": str(item.get("description") or "").strip(),
        })

    checks = []
    for item in raw.get("acceptance") or []:
        if not isinstance(item, dict):
            raise ContractError("each acceptance check must be an object")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _CHECK_KINDS:
            raise ContractError(
                f"unknown acceptance check {kind!r}; use one of {', '.join(_CHECK_KINDS)}")
        when = item.get("when")
        if when is not None and not isinstance(when, dict):
            raise ContractError("a check's `when` must be an object")
        checks.append({
            "kind": kind,
            "output": str(item.get("output") or "").strip(),
            "value": item.get("value"),
            # `when: {output: X, nonzero: true}` — the check applies only if X
            # was submitted non-zero/non-empty. Without this, "cite a line for
            # every finding" also demands a citation from a reviewer that
            # correctly found nothing, and the cheapest way to satisfy a gate
            # you cannot satisfy honestly is to invent something.
            "when": dict(when) if when else None,
        })

    scope = raw.get("scope") or {}
    if not isinstance(scope, dict):
        raise ContractError("scope must be an object")

    contract = {
        "goal": str(raw.get("goal") or "").strip(),
        "inputs": dict(raw.get("inputs") or {}),
        "outputs": outputs,
        "acceptance": checks,
        "evidence": [str(e).strip() for e in (raw.get("evidence") or []) if str(e).strip()],
        "scope": {
            "tools": [str(t) for t in (scope.get("tools") or []) if str(t).strip()],
            "paths": [str(p) for p in (scope.get("paths") or []) if str(p).strip()],
            "max_loops": int(scope.get("max_loops") or 0),
            "deadline_seconds": int(scope.get("deadline_seconds") or 0),
        },
    }
    if not contract["outputs"]:
        raise ContractError(
            "a contract with no declared outputs cannot be verified; declare "
            "at least one output or spawn without a contract")
    return contract


def merge(base: Optional[dict], override: Optional[dict]) -> Optional[dict]:
    """Combine a role's mandatory contract with the caller's own.

    Union, not replacement. A role whose product is a judgement owes citations
    whatever the caller happened to write, so the caller can ADD outputs and
    checks and can sharpen a declaration (a type, a description), but cannot
    drop the role's requirements — that would make "mandatory" mean "unless the
    spawning model preferred otherwise", and the spawning model is exactly who
    the requirement is there to bind.
    """
    if not base:
        return override
    if not override:
        return base
    outputs = {o["name"]: dict(o) for o in base["outputs"]}
    for out in override["outputs"]:
        existing = outputs.get(out["name"])
        if existing is None:
            outputs[out["name"]] = dict(out)
        else:
            # Sharper wins, except that a required output stays required.
            existing.update({k: v for k, v in out.items()
                             if k != "required" or v})
    checks = list(base.get("acceptance") or [])
    for check in override.get("acceptance") or []:
        if check not in checks:
            checks.append(check)
    evidence = list(base.get("evidence") or [])
    for item in override.get("evidence") or []:
        if item not in evidence:
            evidence.append(item)
    scope = dict(base.get("scope") or {})
    for key, value in (override.get("scope") or {}).items():
        if value:
            scope[key] = value
    return {
        "goal": override.get("goal") or base.get("goal") or "",
        "inputs": {**(base.get("inputs") or {}), **(override.get("inputs") or {})},
        "outputs": list(outputs.values()),
        "acceptance": checks,
        "evidence": evidence,
        "scope": scope,
    }


# ── Rendering: what the child is told ──────────────────────────────────────

def render(contract: dict) -> str:
    """The contract as the child sees it, in its own prompt."""
    lines = ["<contract>",
             "This is what you were hired to produce. It is checked "
             "mechanically when you finish, not read for tone."]
    if contract.get("goal"):
        lines.append(f"Goal: {contract['goal']}")
    if contract.get("inputs"):
        lines.append("Inputs:")
        for name, value in contract["inputs"].items():
            lines.append(f"  {name} = {_short(value)}")
    lines.append("Outputs you must submit with task_complete(outputs={...}):")
    for out in contract["outputs"]:
        opt = "" if out["required"] else " (optional)"
        desc = f" - {out['description']}" if out["description"] else ""
        lines.append(f"  {out['name']}: {out['type']}{opt}{desc}")
        if out["type"] == "file":
            lines.append(f"    submit the PATH; the file must exist when you finish")
    for check in contract.get("acceptance") or []:
        lines.append(f"  check: {_describe_check(check)}")
    if contract.get("evidence"):
        lines.append("Evidence required (a citation whose line does not exist "
                     "fails the check):")
        for item in contract["evidence"]:
            lines.append(f"  - {item}")
    scope = contract.get("scope") or {}
    if scope.get("paths"):
        lines.append("Stay inside: " + ", ".join(scope["paths"]))
    if scope.get("max_loops"):
        lines.append(f"Budget: {scope['max_loops']} steps.")
    lines.append("Submitting outputs that do not pass the checks sends the "
                 "gaps back to you once; it does not end the task.")
    lines.append("</contract>")
    return "\n".join(lines)


def _short(value, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _describe_check(check: dict) -> str:
    kind, out, val = check["kind"], check.get("output"), check.get("value")
    when = check.get("when")
    if when and when.get("nonzero"):
        return (f"if {when.get('output')} is not 0: "
                + _describe_check({**check, "when": None}))
    if kind == "file_exists":
        return f"{out} names a file that exists"
    if kind == "contains":
        return f"{out} contains {val!r}"
    if kind == "matches":
        return f"{out} matches /{val}/"
    if kind == "min_length":
        return f"{out} is at least {val} characters"
    if kind == "json_object":
        return f"{out} parses as a JSON object"
    if kind == "line_ref":
        return f"{out} cites at least {val or 1} real path:line location(s)"
    return f"{out} {kind}"


# ── Verification: deterministic, reads the workspace ───────────────────────

def verify(contract: Optional[dict], submitted: Optional[dict], cwd: str) -> dict:
    """Check a submission against its contract.

    Returns ``{"ok": bool, "gaps": [str], "checked": int}``. Every gap names
    what is missing in terms the child can act on, because the first thing that
    happens to a rejection is that it goes back to the child.
    """
    if not contract:
        return {"ok": True, "gaps": [], "checked": 0}
    submitted = submitted if isinstance(submitted, dict) else {}
    gaps: list = []
    checked = 0

    for out in contract["outputs"]:
        name, typ = out["name"], out["type"]
        if name not in submitted or submitted[name] in (None, ""):
            if out["required"]:
                gaps.append(f"missing required output '{name}' ({typ})")
            continue
        checked += 1
        gaps.extend(_check_type(name, typ, submitted[name], cwd))

    for check in contract.get("acceptance") or []:
        name = check.get("output") or ""
        if name and name not in submitted:
            continue                      # already reported as missing
        if not _check_applies(check, submitted):
            continue
        checked += 1
        gap = _run_check(check, submitted, cwd)
        if gap:
            gaps.append(gap)

    if contract.get("evidence"):
        checked += 1
        gaps.extend(_check_evidence(submitted, cwd))

    return {"ok": not gaps, "gaps": gaps, "checked": checked}


def _check_applies(check: dict, submitted: dict) -> bool:
    """Whether a conditional check is in force for this submission."""
    when = check.get("when")
    if not when:
        return True
    guard = submitted.get(str(when.get("output") or ""))
    if when.get("nonzero"):
        try:
            return bool(guard) and float(guard) != 0
        except (TypeError, ValueError):
            return bool(guard)
    return True


def _check_type(name: str, typ: str, value, cwd: str) -> list:
    if typ == "file":
        path = str(value)
        if not os.path.isabs(path):
            path = os.path.join(cwd or os.getcwd(), path)
        if not os.path.isfile(path):
            return [f"output '{name}' names {value!r}, which is not a file that "
                    f"exists; write the file, then submit its path"]
        if os.path.getsize(path) == 0:
            return [f"output '{name}' names an empty file ({value})"]
        return []
    if typ == "object" and not isinstance(value, dict):
        return [f"output '{name}' must be a JSON object, got {type(value).__name__}"]
    if typ == "array" and not isinstance(value, list):
        return [f"output '{name}' must be a JSON array, got {type(value).__name__}"]
    if typ == "number" and not isinstance(value, (int, float)):
        return [f"output '{name}' must be a number, got {type(value).__name__}"]
    if typ == "boolean" and not isinstance(value, bool):
        return [f"output '{name}' must be true or false, got {type(value).__name__}"]
    return []


def _resolve_text(name: str, submitted: dict, cwd: str) -> str:
    """The text a check runs against: a file output's CONTENT, else its value."""
    value = submitted.get(name)
    if isinstance(value, str):
        path = value if os.path.isabs(value) else os.path.join(cwd or os.getcwd(), value)
        if os.path.isfile(path):
            try:
                return open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                return ""
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _run_check(check: dict, submitted: dict, cwd: str) -> str:
    kind = check["kind"]
    name = check.get("output") or ""
    value = check.get("value")
    if kind == "file_exists":
        target = submitted.get(name)
        if not target:
            return f"check failed: '{name}' is empty, so no file to check"
        path = str(target)
        if not os.path.isabs(path):
            path = os.path.join(cwd or os.getcwd(), path)
        return "" if os.path.isfile(path) else (
            f"check failed: '{name}' points at {target!r}, which does not exist")
    text = _resolve_text(name, submitted, cwd)
    if kind == "contains":
        return "" if str(value) in text else (
            f"check failed: '{name}' does not contain {value!r}")
    if kind == "matches":
        try:
            ok = re.search(str(value), text) is not None
        except re.error as exc:
            return f"check '{name}' has an invalid pattern: {exc}"
        return "" if ok else f"check failed: '{name}' does not match /{value}/"
    if kind == "min_length":
        need = int(value or 0)
        return "" if len(text) >= need else (
            f"check failed: '{name}' is {len(text)} characters, needs {need}")
    if kind == "json_object":
        raw = submitted.get(name)
        if isinstance(raw, dict):
            return ""
        try:
            return "" if isinstance(json.loads(str(raw)), dict) else (
                f"check failed: '{name}' is not a JSON object")
        except (TypeError, ValueError):
            return f"check failed: '{name}' is not valid JSON"
    if kind == "line_ref":
        # `value` may name another output ("as many citations as you claim
        # findings"), which is what stops a report with five findings and one
        # real location from passing a fixed threshold of one.
        need = value
        if isinstance(need, str) and need in submitted:
            try:
                need = int(float(submitted[need]))
            except (TypeError, ValueError):
                need = 1
        need = max(1, int(need or 1))
        found = _real_line_refs(text, cwd)
        return "" if len(found) >= need else (
            f"check failed: '{name}' cites {len(found)} real path:line "
            f"location(s), needs {need} (a citation whose line does not exist "
            f"in the file does not count)")
    return ""


def _real_line_refs(text: str, cwd: str) -> list:
    """Citations that point at a line that actually exists."""
    real = []
    for match in _LINE_REF.finditer(text or ""):
        path, line = match.group(1), int(match.group(2))
        target = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
        try:
            if not os.path.isfile(target):
                continue
            with open(target, "rb") as fh:
                if sum(1 for _ in fh) >= line >= 1:
                    real.append(f"{path}:{line}")
        except OSError:
            continue
    return real


def _check_evidence(submitted: dict, cwd: str) -> list:
    """At least one citation in the submission must resolve to a real line."""
    blob = " ".join(
        _resolve_text(name, submitted, cwd) for name in submitted)
    if _real_line_refs(blob, cwd):
        return []
    return ["evidence required: no citation in the submission resolves to a "
            "real path:line location"]


# ── Scope enforcement ──────────────────────────────────────────────────────
#
# `scope.paths` was prompt text: the child was told to stay inside a set of
# paths and nothing checked that it did. A boundary that only exists in a
# sentence is not a boundary — it is a request, addressed to the one party the
# contract exists to bind.
#
# Reads are deliberately NOT restricted. Understanding a change usually means
# reading around it, and a reviewer that cannot read the caller of the function
# it is reviewing produces worse findings, not safer ones. What is bounded is
# what the child can CHANGE.

def path_in_scope(contract: Optional[dict], abs_path: str, cwd: str) -> bool:
    """May this contracted agent write to `abs_path`?

    True when there is no contract, no declared paths, or the target is inside
    one of them. A declared path may name a file or a directory; a directory
    covers everything under it.
    """
    if not contract:
        return True
    allowed = (contract.get("scope") or {}).get("paths") or []
    if not allowed:
        return True
    # Resolve symlinks before comparing. A lexical check alone lets an allowed
    # directory contain ``escape -> /outside`` and then accepts
    # ``allowed/escape/file`` even though the bytes land outside the contract.
    base = os.path.realpath(os.path.abspath(cwd or os.getcwd()))
    target = os.path.realpath(os.path.abspath(
        abs_path if os.path.isabs(abs_path) else os.path.join(base, abs_path)))
    for entry in allowed:
        root = os.path.realpath(os.path.abspath(
            entry if os.path.isabs(entry) else os.path.join(base, entry)))
        try:
            inside = os.path.commonpath((target, root)) == root
        except ValueError:  # different Windows drives, if reused there
            inside = False
        if inside:
            return True
    return False


def scope_violation(contract: dict, abs_path: str) -> dict:
    """The refusal a child gets when it writes outside its declared scope."""
    allowed = ", ".join((contract.get("scope") or {}).get("paths") or [])
    return {
        "ok": False,
        "error": (f"{abs_path} is outside the scope your contract declares "
                  f"({allowed}). Reading anywhere is fine; changing anything "
                  f"outside that list is not. If the work genuinely needs it, "
                  f"say so in your outputs and let your caller decide."),
        "path": abs_path,
        "_advisory": True,
    }


# ── Bridging the workflow languages ────────────────────────────────────────
#
# HWO/HWG nodes already declare `io: {out: [{name, type}]}`. HWG checked that
# the declared names came back; nothing checked what came back. So
# `out(report: file)` — whose whole point, per HWO's own type guidance, is
# "return a path, not inline content" — passed with a path to a file that was
# never written. One verifier for both, so a contract means the same thing
# wherever it is declared.

_IO_TYPE_MAP = {"file": "file", "object": "object", "array": "array",
                "number": "number", "int": "number", "float": "number",
                "bool": "boolean", "boolean": "boolean"}


def unknown_io_types(io_spec: Optional[dict]) -> list:
    """Declared types this module does not recognise.

    `out(report: fil)` is a typo, and a typo that silently becomes `string`
    turns a file contract — the one whose whole purpose is "the file must
    exist" — into no contract at all. Reported so the compile step can say so;
    not raised, because .hwo files predate this module and breaking a workflow
    to enforce a rule it never knew about is the worse failure.
    """
    unknown = []
    for spec in (io_spec or {}).get("out") or []:
        declared = str((spec or {}).get("type") or "").strip().lower()
        if declared and declared not in _IO_TYPE_MAP and declared != "string":
            unknown.append(f"{(spec or {}).get('name', '?')}: {declared}")
    return unknown


def from_io(io_spec: Optional[dict], protocol_outputs=()) -> Optional[dict]:
    """Build a contract from an HWO/HWG `io` declaration.

    Unknown types degrade to `string` rather than raising (see
    `unknown_io_types` for why, and for how the degradation is surfaced).
    """
    outs = (io_spec or {}).get("out") or []
    outputs = []
    for spec in outs:
        name = str((spec or {}).get("name") or "").strip()
        if not name:
            continue
        typ = _IO_TYPE_MAP.get(str(spec.get("type") or "").strip().lower(), "string")
        outputs.append({
            "name": name,
            "type": typ,
            # An explicit optional marker or default is an author-provided
            # fallback, not a missing product. Runtime protocol fields such as
            # HWG's verdict are synthesized separately and are checked by the
            # protocol-specific caller.
            "required": (name not in protocol_outputs
                         and not bool(spec.get("optional"))
                         and spec.get("default") is None),
            "description": "",
        })
    if not outputs:
        return None
    return {"goal": "", "inputs": {}, "outputs": outputs, "acceptance": [],
            "evidence": [], "scope": {"tools": [], "paths": [],
                                      "max_loops": 0, "deadline_seconds": 0}}
