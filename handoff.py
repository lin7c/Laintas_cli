"""handoff.py — the envelope one worker hands to the next.

Today a handoff in this codebase is one string: ``spawn_chain`` passes
``info.result or info.last_reply or "(done)"`` to the next step, and between two
*people* there is not even that — the outgoing worker retells the state in chat
and hopes. Both are the failure this module exists to remove: the state of the
work lives in a transcript, and a transcript cannot be merged, cannot be
verified, and is gone the moment the session is.

So a handoff is a **file**, for the same reason ``contract_store`` is a file:
it is the only thing both sides can reach in every topology, it survives both
processes, it is versioned and diffable, and — unlike a message — it is still
true tomorrow. It lives in the repository at ``.laintas/handoff/<id>.json`` and
is meant to be committed.

What is deliberately NOT in it
------------------------------
The conversation. Sharing a session hands the successor 200 turns of reasoning,
including the branches that went nowhere, and makes them reconstruct the state
by reading it. The envelope carries the *conclusions* instead: where the work
is, what is left, and what not to try again.

Structure: an immutable header, an append-only log
--------------------------------------------------
The header records creation facts and never changes. Everything after that is
an event appended to ``events``. Status is a **projection** of those events,
never a stored field.

That is not ceremony, it is what makes concurrent use safe. Two people can hold
the same envelope — one on each machine, synced through Laintas storage — and
both can append. Merging is then the union of two event lists keyed by a
content hash, which is associative, commutative and idempotent: merge in any
order, any number of times, and the result is the same. A mutable ``status``
field would need a lock instead, and there is no lock that spans two machines
and an object store.

Event ids are content hashes rather than random, so re-merging the same file
cannot duplicate history. The cost is that two byte-identical events from the
same actor at the same microsecond collapse into one; nothing in this protocol
distinguishes them anyway.

Ordering is by ``(ts, id)``. The id is the tie-break purely so that every
participant projects the same status from the same set — it carries no meaning.
**Known limitation:** ordering trusts the wall clocks of the machines involved.
A badly skewed clock can win a claim it should have lost. Detecting that needs a
server-side sequencer, which this design avoids on purpose; what it does instead
is surface a contested claim rather than hide it (see ``project``).

What is still left, and who decides
-----------------------------------
``remaining()`` does not read a "todo" the outgoing worker typed. It re-runs
``agent_contract.verify`` against the **workspace**, exactly as a sub-agent's
delivery is verified — the successor's "what is left" is recomputed from the
files on disk every time they look, so it cannot go stale and cannot be
overstated by whoever wrote the envelope.

Display-free by convention (same as ``shared_storage``): this module returns
values or raises :class:`HandoffError`; the REPL command owns all printing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import agent_contract
import json_store
import paths

HANDOFF_DIR = ".laintas/handoff"
VERSION = 1

#: Remote prefix inside Laintas shared storage. Bytes go straight to object
#: storage on a presigned URL — the gateway signs and forwards, it never reads
#: an envelope — so syncing costs the server nothing per handoff.
REMOTE_PREFIX = "handoff"

#: Appendable event kinds. `open` is emitted once by `create`.
EVENT_KINDS = ("open", "claim", "release", "note", "close", "reopen")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_GIT_TIMEOUT = 5


class HandoffError(Exception):
    """Any failure worth showing the user verbatim."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def handoff_dir(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()) / HANDOFF_DIR


def handoff_path(hid: str, cwd: Optional[str] = None) -> Path:
    return handoff_dir(cwd) / f"{valid_id(hid)}.json"


def valid_id(hid: str) -> str:
    """Envelope ids become filenames, so they are checked, not trusted.

    Rejecting traversal here rather than sanitising it: an id that had to be
    rewritten to be safe is not the id the other side is using, and a handoff
    whose two copies have different names is not one handoff.
    """
    value = str(hid or "").strip().lower()
    if not _ID_RE.match(value):
        raise HandoffError(
            f"invalid handoff id {hid!r} — use 3-64 chars of a-z, 0-9 and '-'")
    return value


def new_id(title: str = "") -> str:
    """A readable slug plus enough entropy to not collide across machines."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")[:32]
    slug = slug.strip("-") or "handoff"
    if not slug[0].isalnum():
        slug = f"h-{slug}"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def remote_path(env: dict) -> str:
    """Where this envelope lives in shared storage.

    Keyed by the repository's own identity rather than by local path, so the
    same repo checked out at different paths on two machines still lines up.
    """
    return f"{REMOTE_PREFIX}/{env['repo'].get('key') or 'unkeyed'}/{env['id']}.json"


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def _git(args: list, cwd: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=_GIT_TIMEOUT, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_baseline(cwd: Optional[str] = None) -> dict:
    """Where the work sits right now: branch, commit, and whether it is clean.

    ``dirty`` matters more than it looks. An envelope created on a dirty tree
    points at a commit that does not contain the work, so the successor needs
    to be told the files are only on the outgoing worker's disk — which is what
    the REPL command warns about, using this flag.

    ``key`` identifies the repository across checkouts: the first commit's hash
    is stable for every clone and independent of remote naming. Falls back to
    the directory name so a non-git workspace still gets a stable-enough bucket
    rather than colliding with every other one.
    """
    root = os.path.realpath(cwd or os.getcwd())
    first = _git(["rev-list", "--max-parents=0", "HEAD"], root).split("\n")[0].strip()
    status = _git(["status", "--porcelain"], root)
    inside = _git(["rev-parse", "--is-inside-work-tree"], root) == "true"
    key = first[:16] if first else f"dir-{hashlib.sha256(root.encode()).hexdigest()[:12]}"
    return {
        "key": key,
        "git": bool(inside),
        "root": root,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], root),
        "commit": _git(["rev-parse", "HEAD"], root),
        # `status --porcelain` is empty exactly when the tree is clean; a git
        # failure returns "" too, so the flag is only trusted inside a repo.
        "dirty": bool(inside and status),
    }


def contract_ref(cwd: Optional[str] = None) -> Optional[dict]:
    """A digest of the API contract, not a copy of it.

    Copying would create a second source of truth that goes stale silently.
    A digest answers the one question a successor has — "is the contract still
    the one this handoff was written against?" — and nothing else.
    """
    try:
        import contract_store
    except ImportError:
        return None
    path = contract_store.lock_path(cwd)
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    ref = {
        "path": str(Path(contract_store.CONTRACT_DIR) / contract_store.LOCK_FILE),
        "digest": hashlib.sha256(raw).hexdigest()[:16],
    }
    try:
        ref["counts"] = contract_store.status(cwd).get("counts") or {}
    except Exception:
        ref["counts"] = {}
    return ref


def contract_drifted(env: dict, cwd: Optional[str] = None) -> Optional[bool]:
    """True/False when the envelope named a contract, None when it did not."""
    ref = env.get("contractRef")
    if not ref:
        return None
    now = contract_ref(cwd)
    if not now:
        return True          # it existed when the envelope was written; now it does not
    return now["digest"] != ref.get("digest")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def make_event(kind: str, actor: str, note: str = "",
               data: Optional[dict] = None, ts: Optional[float] = None) -> dict:
    if kind not in EVENT_KINDS:
        raise HandoffError(
            f"unknown event {kind!r}; use one of {', '.join(EVENT_KINDS)}")
    event = {
        "ts": float(ts if ts is not None else time.time()),
        "actor": str(actor or "unknown").strip() or "unknown",
        "kind": kind,
        "note": str(note or "").strip(),
        "data": dict(data or {}),
    }
    event["id"] = hashlib.sha256(_canonical([
        event["ts"], event["actor"], event["kind"], event["note"], event["data"],
    ]).encode("utf-8")).hexdigest()[:16]
    return event


def _clean_events(raw: Any) -> list:
    """Keep only events that can be ordered and identified.

    A malformed entry is dropped rather than raising: an envelope is synced
    between machines and may be hand-edited, and losing one unreadable event is
    better than making the whole handoff unopenable. Dropping is safe because
    ``id`` is a content hash — a dropped event reappears on the next merge with
    any copy that still has it.
    """
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("id") or "")
        kind = str(item.get("kind") or "")
        if not eid or eid in seen or kind not in EVENT_KINDS:
            continue
        try:
            ts = float(item.get("ts"))
        except (TypeError, ValueError):
            continue
        seen.add(eid)
        out.append({
            "id": eid,
            "ts": ts,
            "actor": str(item.get("actor") or "unknown"),
            "kind": kind,
            "note": str(item.get("note") or ""),
            "data": item.get("data") if isinstance(item.get("data"), dict) else {},
        })
    out.sort(key=lambda e: (e["ts"], e["id"]))
    return out


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def create(title: str, actor: str, *, to: str = "", avoid: Optional[list] = None,
           contract: Optional[dict] = None, expect: Optional[dict] = None,
           note: str = "", cwd: Optional[str] = None) -> dict:
    """Write a new envelope. Raises if one with the same id already exists.

    ``contract`` is an ``agent_contract`` contract and is normalized here, so a
    contract that could never be checked is refused at creation rather than
    discovered by the successor. ``expect`` maps each declared output to the
    value the checks run against — a path, a string — which is what makes
    ``remaining()`` a question about the workspace instead of about this file.
    """
    title = str(title or "").strip()
    if not title:
        raise HandoffError("a handoff needs a one-line title saying what it is")

    normalized = None
    if contract:
        try:
            normalized = agent_contract.normalize(contract)
        except agent_contract.ContractError as exc:
            raise HandoffError(f"handoff contract: {exc}") from exc

    env = {
        "version": VERSION,
        "id": new_id(title),
        "title": title,
        "to": str(to or "").strip(),
        "createdAt": time.time(),
        "createdBy": str(actor or "unknown").strip() or "unknown",
        "repo": repo_baseline(cwd),
        "contractRef": contract_ref(cwd),
        "contract": normalized,
        "expect": dict(expect or {}),
        "avoid": [str(a).strip() for a in (avoid or []) if str(a).strip()],
        "events": [],
    }
    env["events"] = [make_event("open", env["createdBy"], note,
                                {"to": env["to"]}, ts=env["createdAt"])]

    path = handoff_path(env["id"], cwd)
    if path.exists():                       # new_id collided; astronomically unlikely
        raise HandoffError(f"handoff {env['id']} already exists")
    _write(env, cwd)
    _ensure_gitignore_exception(cwd)
    return env


def header_digest(env: dict) -> str:
    """Fingerprint of the immutable half, so a merge can prove it is the same one."""
    return hashlib.sha256(_canonical([
        env.get("version"), env.get("id"), env.get("title"), env.get("to"),
        env.get("createdAt"), env.get("createdBy"), env.get("repo"),
        env.get("contractRef"), env.get("contract"), env.get("expect"),
        env.get("avoid"),
    ]).encode("utf-8")).hexdigest()[:16]


def _write(env: dict, cwd: Optional[str] = None) -> None:
    env["events"] = _clean_events(env.get("events"))
    try:
        json_store.save_json_atomic(handoff_path(env["id"], cwd), env)
    except (OSError, TypeError, ValueError) as exc:
        raise HandoffError(f"could not write handoff: {exc}") from exc


def load(hid: str, cwd: Optional[str] = None) -> dict:
    path = handoff_path(hid, cwd)
    if not path.exists():
        raise HandoffError(f"no handoff {valid_id(hid)!r} here — /handoff list")
    raw = json_store.load_json(path, None)
    return parse(raw, source=str(path))


def parse(raw: Any, source: str = "") -> dict:
    """Validate an envelope from disk or from storage. Never trusts its shape."""
    where = f" ({source})" if source else ""
    if not isinstance(raw, dict):
        raise HandoffError(f"not a handoff envelope{where}")
    try:
        version = int(raw.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != VERSION:
        # Refusing a newer file rather than reading it partially: a half-
        # understood handoff is worse than none, because it looks complete.
        raise HandoffError(
            f"handoff format v{version or '?'} is not v{VERSION}{where} — upgrade laintas_cli")
    env = dict(raw)
    try:
        env["id"] = valid_id(env.get("id"))
    except HandoffError as exc:
        raise HandoffError(f"{exc}{where}") from exc
    # Coerced, not merely read: `list_all` sorts on this, and a string here
    # would make the sort raise TypeError — which is not a HandoffError, so one
    # hand-edited envelope would take the whole listing down with it.
    try:
        env["createdAt"] = float(env.get("createdAt") or 0)
    except (TypeError, ValueError):
        env["createdAt"] = 0.0
    env["createdBy"] = str(env.get("createdBy") or "unknown")
    env["title"] = str(env.get("title") or "")
    env["to"] = str(env.get("to") or "")
    env["repo"] = env.get("repo") if isinstance(env.get("repo"), dict) else {}
    env["expect"] = env.get("expect") if isinstance(env.get("expect"), dict) else {}
    env["avoid"] = [str(a) for a in (env.get("avoid") or []) if str(a).strip()]
    env["events"] = _clean_events(env.get("events"))
    if env.get("contract"):
        try:
            env["contract"] = agent_contract.normalize(env["contract"])
        except agent_contract.ContractError as exc:
            raise HandoffError(f"handoff contract is not checkable{where}: {exc}") from exc
    return env


def list_all(cwd: Optional[str] = None) -> list:
    """Every envelope in the workspace, newest first. Unreadable ones are skipped."""
    out = []
    try:
        entries = sorted(handoff_dir(cwd).glob("*.json"))
    except OSError:
        return []
    for path in entries:
        try:
            out.append(parse(json_store.load_json(path, None), source=str(path)))
        except HandoffError:
            continue
    out.sort(key=lambda e: e.get("createdAt") or 0, reverse=True)
    return out


def append(hid: str, kind: str, actor: str, note: str = "",
           data: Optional[dict] = None, cwd: Optional[str] = None) -> dict:
    """Append one event and persist.

    Read-modify-write with no lock. That is safe for the same reason the merge
    is: the event carries a content-hash id, so the worst a lost update can do
    is drop an event that the next sync puts back from the other copy. It can
    never corrupt the header or reorder history.
    """
    env = load(hid, cwd)
    env["events"] = _clean_events([*env["events"], make_event(kind, actor, note, data)])
    _write(env, cwd)
    return env


def merge(local: dict, remote: dict) -> tuple[dict, int]:
    """Union of two copies of the same envelope. Returns (merged, events_gained).

    Associative, commutative and idempotent, because it is a set union over
    content-hashed ids followed by a total ordering on ``(ts, id)``. Merging in
    any order, or twice, gives the same file.

    A header mismatch raises instead of picking a side: two envelopes with the
    same id but different creation facts are two different handoffs, and
    silently keeping one would discard the other's premise while keeping its
    events.
    """
    if local.get("id") != remote.get("id"):
        raise HandoffError(
            f"refusing to merge different handoffs: {local.get('id')} vs {remote.get('id')}")
    lh, rh = header_digest(local), header_digest(remote)
    if lh != rh:
        raise HandoffError(
            f"handoff {local.get('id')} has two different headers ({lh} vs {rh}) — "
            "the same id was created twice; rename one and re-share it")
    before = {e["id"] for e in local["events"]}
    merged = dict(local)
    merged["events"] = _clean_events([*local["events"], *remote["events"]])
    return merged, len([e for e in merged["events"] if e["id"] not in before])


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def project(env: dict) -> dict:
    """Status derived from the events — never stored, so it cannot disagree.

    Claims are exclusive by *resolution*, not by locking: when two people claim
    the same envelope from two machines, both events survive the merge and the
    earliest ``(ts, id)`` wins. The losers are returned in ``contested`` rather
    than dropped, because two people who both thought they owned the work is
    exactly the thing a handoff protocol must not hide.
    """
    holder, contested, closed = "", [], False
    closed_at = 0.0
    for event in env.get("events") or []:
        kind = event["kind"]
        if kind == "claim":
            if not holder:
                holder = event["actor"]
            elif event["actor"] != holder:
                contested.append({"actor": event["actor"], "ts": event["ts"]})
        elif kind == "release":
            # Only the holder can release; anyone else's release is a no-op
            # rather than a way to steal an envelope out from under them.
            #
            # Releasing also drops the losing claims instead of promoting the
            # next one. A contested claimant was told at the time that somebody
            # else holds it, so promoting them silently would hand them work
            # they had already been told to leave alone. Going back to `open`
            # means whoever picks it up does so deliberately.
            if event["actor"] == holder:
                holder, contested = "", []
        elif kind == "close":
            closed, closed_at = True, event["ts"]
        elif kind == "reopen":
            closed, closed_at = False, 0.0
    status = "closed" if closed else ("claimed" if holder else "open")
    return {
        "status": status,
        "holder": holder,
        "contested": contested,
        "closedAt": closed_at,
        "events": len(env.get("events") or []),
        "lastAt": max((e["ts"] for e in env.get("events") or []), default=0.0),
    }


def remaining(env: dict, cwd: Optional[str] = None) -> dict:
    """What the workspace still does not satisfy. Recomputed, never stored.

    Delegates to the same verifier a sub-agent's delivery goes through, so the
    successor's "what is left" is a reading of the files, not of whatever the
    outgoing worker believed when they wrote the envelope.
    """
    contract = env.get("contract")
    if not contract:
        return {"checked": False, "ok": True, "gaps": [], "outputs": []}
    result = agent_contract.verify(contract, env.get("expect") or {},
                                   cwd or os.getcwd())
    return {
        "checked": True,
        "ok": bool(result.get("ok")),
        "gaps": list(result.get("gaps") or []),
        "outputs": [o["name"] for o in contract.get("outputs") or []],
    }


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

_GITIGNORE_NOTE = (
    "# laintas_cli's project state stays ignored; handoff envelopes do not.\n"
    "# They are how one worker hands the work to the next, so they belong in\n"
    "# review and in history like any other shared artefact.\n")


def _ensure_gitignore_exception(cwd: Optional[str] = None) -> bool:
    """Make the envelope committable when ``.laintas/`` is ignored wholesale.

    Best-effort and idempotent, same contract as ``contract_store`` — which
    shares the implementation, because the scaffolding those rules need must be
    written at most once however many subdirectories opt in. A handoff nobody
    can commit is a handoff that does not reach the other person through the
    repository.
    """
    return paths.ensure_project_path_committable("handoff", _GITIGNORE_NOTE, cwd)


# ---------------------------------------------------------------------------
# Sync through Laintas shared storage
# ---------------------------------------------------------------------------
#
# Fetch, merge, push — the same three steps as a git sync, and safe for the
# same reason: the merge is a union over content-hashed events, so a push can
# only ever add to what is already there. Nothing here reads or writes the
# envelope on the server. `shared_storage` hands back a presigned URL and the
# bytes go straight to object storage, so a handoff costs the gateway one
# signature and no transfer, however large the history gets.
#
# The client is injected rather than constructed here, for the same reason this
# module prints nothing: building it needs a backend profile and a session, and
# that is the REPL's business.

def _remote_exists(client, remote: str) -> bool:
    """Whether the envelope is already in storage.

    Asked by listing the folder rather than by attempting a download and
    reading the error text: "not found" and "your session expired" both arrive
    as a failed call, and treating the second as the first would silently push
    a local copy over a remote one it never saw.
    """
    folder = remote.rsplit("/", 1)[0]
    name = remote.rsplit("/", 1)[-1]
    for entry in client.list(folder):
        if entry.path == remote or (not entry.is_dir and entry.name == name):
            return True
    return False


def sync(hid: str, client, cwd: Optional[str] = None) -> dict:
    """Merge the shared copy into this one and publish the result.

    Returns ``{"remote", "gained", "existed"}`` — ``gained`` being how many
    events this machine did not already have, which is the only number a user
    actually wants after a sync.

    Raises rather than resolving when the two copies disagree about the header:
    that is two different handoffs wearing one id, and picking a side would
    keep one premise while keeping the other's events.
    """
    env = load(hid, cwd)
    remote = remote_path(env)
    gained, existed = 0, False

    if _remote_exists(client, remote):
        existed = True
        scratch = handoff_dir(cwd) / f".{env['id']}.remote.tmp"
        try:
            client.pull_file(remote, str(scratch))
            incoming = parse(json_store.load_json(scratch, None), source=remote)
            env, gained = merge(env, incoming)
        finally:
            try:
                scratch.unlink(missing_ok=True)
            except OSError:
                pass
        if gained:
            _write(env, cwd)

    # Pushed unconditionally, including when nothing was gained: the remote may
    # be missing events this machine has, and that is exactly the case where
    # "nothing changed locally" would be the wrong reason to skip.
    client.push_file(str(handoff_path(env["id"], cwd)), remote)
    return {"remote": remote, "gained": gained, "existed": existed}


def fetch(remote: str, client, cwd: Optional[str] = None) -> dict:
    """Bring down an envelope this machine has never seen. Returns it.

    Separate from :func:`sync` because it answers a different question: sync
    reconciles a handoff you already hold, this one adopts a handoff somebody
    else created. A local copy that already exists is merged into, never
    overwritten — receiving a handoff must not be able to destroy local events.
    """
    name = remote.rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        raise HandoffError(f"{remote} is not a handoff envelope")
    hid = valid_id(name[:-len(".json")])
    scratch = handoff_dir(cwd) / f".{hid}.remote.tmp"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.pull_file(remote, str(scratch))
        incoming = parse(json_store.load_json(scratch, None), source=remote)
    finally:
        try:
            scratch.unlink(missing_ok=True)
        except OSError:
            pass
    if incoming["id"] != hid:
        raise HandoffError(
            f"{remote} contains handoff {incoming['id']}, not {hid}")
    if handoff_path(hid, cwd).exists():
        incoming, _ = merge(load(hid, cwd), incoming)
    _write(incoming, cwd)
    _ensure_gitignore_exception(cwd)
    return incoming


def list_remote(client, key: str = "", cwd: Optional[str] = None) -> list:
    """Remote paths of envelopes for a repository (this one, by default)."""
    bucket = key or repo_baseline(cwd).get("key") or "unkeyed"
    folder = f"{REMOTE_PREFIX}/{bucket}"
    return [e.path for e in client.list(folder)
            if not e.is_dir and e.path.endswith(".json")]
