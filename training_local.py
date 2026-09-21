"""Local training capture — the same interactions, kept on this machine.

`/training on|off` controls what the **Gateway** retains: the server observes
the request it actually served and records it under the account's consent.
That is the authoritative ledger and it does not change here.

`/training local on` is a second, independent switch. It writes the same
interactions to a SQLite store under `~/.laintas/training/`, from this
process, before anything is uploaded. The two are orthogonal on purpose:

    cloud on  + local on   → both keep a copy
    cloud off + local on   → only this machine keeps a copy
    cloud on  + local off  → today's behaviour, unchanged
    cloud off + local off  → nothing is kept anywhere

Why a local copy is worth having even when the cloud one exists: the local
store works with any backend (self-hosted gateways included), it survives with
no account, and it is the corpus a household would fine-tune its own adapters
from without the data ever leaving the room.

Trust model, and how it differs from the Gateway's
--------------------------------------------------
`agent_gateway/training_data.py` treats the CLI as an untrusted public client,
so it records what the *server* saw and derives no fact from client-supplied
labels. This module makes the opposite bargain, and it is only safe because of
where the data goes: the rows never leave this machine, so the user is both the
only author and the only reader. `task_kind` and `trajectory_id` are taken from
the outgoing payload verbatim — the same values the Gateway files under — so a
local export and a Gateway export bucket identically.

Schema
------
Column names deliberately mirror `training_interactions` so an importer written
for the Gateway ledger reads this store with a table rename and nothing else.
The tool catalog is content-addressed exactly as the Gateway's v3 records do:
in a real corpus a few hundred distinct catalogs back tens of thousands of
rows, and inlining them multiplied the store several times over.

Nothing here may raise into the model call path. Writes happen on a background
thread behind a bounded queue: when the queue is full, samples are dropped and
counted rather than made to wait. Losing a sample is acceptable; adding latency
to inference is not.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

import paths

SCHEMA_VERSION = 1

# Two ceilings, both on the store rather than on any one sample. Prompts here
# run to 150k tokens (~600 KB of JSON), so a cap in rows would mean nothing —
# the same 20k rows can be 200 MB or 6 GB depending on what the user did.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024      # prune oldest beyond 2 GiB
MAX_SAMPLE_BYTES = 8 * 1024 * 1024              # skip one absurd sample

_QUEUE_MAX = 64

_lock = threading.Lock()
_queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=_QUEUE_MAX)
_writer: Optional[threading.Thread] = None
_dropped = 0
_last_error = ""


# ── store ────────────────────────────────────────────────────────────────

def db_path() -> str:
    return str(paths.TRAINING_DIR / "local.sqlite3")


def _connect() -> sqlite3.Connection:
    os.makedirs(str(paths.TRAINING_DIR), mode=0o700, exist_ok=True)
    db = sqlite3.connect(db_path(), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS local_interactions(
            interaction_id     TEXT PRIMARY KEY,
            request_id         TEXT NOT NULL DEFAULT '',
            model              TEXT NOT NULL DEFAULT '',
            source_product     TEXT NOT NULL DEFAULT 'cli',
            request_json       TEXT NOT NULL,
            response_json      TEXT NOT NULL,
            usage_json         TEXT NOT NULL DEFAULT '{}',
            content_hash       TEXT NOT NULL,
            created_at         REAL NOT NULL,
            task_kind          TEXT NOT NULL DEFAULT '',
            trajectory_id      TEXT NOT NULL DEFAULT '',
            tool_catalog_hash  TEXT NOT NULL DEFAULT '',
            cwd                TEXT NOT NULL DEFAULT '',
            device             TEXT NOT NULL DEFAULT '',
            prompt_tokens      INTEGER NOT NULL DEFAULT 0,
            completion_tokens  INTEGER NOT NULL DEFAULT 0,
            schema_version     INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ix_local_content
            ON local_interactions(content_hash);
        CREATE INDEX IF NOT EXISTS ix_local_kind
            ON local_interactions(task_kind, created_at);
        CREATE INDEX IF NOT EXISTS ix_local_traj
            ON local_interactions(trajectory_id);
        CREATE TABLE IF NOT EXISTS local_tool_catalogs(
            tool_catalog_hash TEXT PRIMARY KEY,
            tools_json        TEXT NOT NULL,
            created_at        REAL NOT NULL
        );
        """
    )
    return db


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


# ── capture ──────────────────────────────────────────────────────────────

def build_record(sink: dict, result: dict, *, cwd: str = "",
                 device: str = "") -> Optional[dict]:
    """Turn one captured call into a row, or None when it must not be kept.

    `sink` is what `call_backend_stream` stashed: the exact outgoing payload,
    the model the call resolved to, and the billing block the stream reported.
    """
    if not isinstance(sink, dict) or not isinstance(result, dict):
        return None
    # Errors and refusals are not interactions. A billing refusal or a 5xx has
    # a `reply` that reads like an answer but was written by this CLI, and
    # training on it teaches the model to imitate our own error copy.
    if result.get("error"):
        return None
    payload = sink.get("payload")
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    request = {k: v for k, v in payload.items() if k != "tools"}
    tools = payload.get("tools")
    tools_json, catalog_hash = "", ""
    if isinstance(tools, list) and tools:
        tools_json = json.dumps(tools, ensure_ascii=False, sort_keys=True)
        catalog_hash = _digest(tools_json)

    assistant = {
        "role": "assistant",
        "content": str(result.get("reply") or ""),
    }
    calls = result.get("tool_calls")
    if isinstance(calls, list) and calls:
        # CLI-normalised shape (name/args), not the provider's raw
        # tool_calls — that is what this process has, and the importer is told
        # so by schema_version rather than being left to guess.
        assistant["tool_calls"] = calls
    if not assistant["content"] and "tool_calls" not in assistant:
        return None

    billing = sink.get("billing") if isinstance(sink.get("billing"), dict) else {}
    request_json = json.dumps(request, ensure_ascii=False)
    response_json = json.dumps(assistant, ensure_ascii=False)
    if len(request_json) + len(response_json) > MAX_SAMPLE_BYTES:
        return None

    return {
        "interaction_id": uuid.uuid4().hex,
        "request_id": str(billing.get("requestId") or "")[:64],
        "model": str(sink.get("model") or payload.get("model") or "")[:200],
        "source_product": "cli",
        "request_json": request_json,
        "response_json": response_json,
        "usage_json": json.dumps(billing, ensure_ascii=False),
        # Content-addressed over what was sent AND what came back, so a
        # retried turn that produced a different answer is a different sample
        # while an exact replay is not.
        "content_hash": _digest(request_json + "\x00" + response_json),
        "created_at": time.time(),
        "task_kind": str(payload.get("taskKind") or "")[:40],
        "trajectory_id": str(payload.get("trajectoryId") or "")[:64],
        "tool_catalog_hash": catalog_hash,
        "cwd": str(cwd or "")[:512],
        "device": str(device or "")[:80],
        "prompt_tokens": int(billing.get("promptTokens") or 0),
        "completion_tokens": int(billing.get("completionTokens") or 0),
        "schema_version": SCHEMA_VERSION,
        "_tools_json": tools_json,
    }


def _write(row: dict) -> None:
    db = _connect()
    try:
        with db:
            if row.get("tool_catalog_hash"):
                db.execute(
                    "INSERT OR IGNORE INTO local_tool_catalogs("
                    "tool_catalog_hash,tools_json,created_at) VALUES(?,?,?)",
                    (row["tool_catalog_hash"], row.get("_tools_json") or "",
                     time.time()))
            db.execute(
                "INSERT OR IGNORE INTO local_interactions("
                "interaction_id,request_id,model,source_product,request_json,"
                "response_json,usage_json,content_hash,created_at,task_kind,"
                "trajectory_id,tool_catalog_hash,cwd,device,prompt_tokens,"
                "completion_tokens,schema_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[k] for k in (
                    "interaction_id", "request_id", "model", "source_product",
                    "request_json", "response_json", "usage_json",
                    "content_hash", "created_at", "task_kind", "trajectory_id",
                    "tool_catalog_hash", "cwd", "device", "prompt_tokens",
                    "completion_tokens", "schema_version")))
    finally:
        db.close()


def _drain(work: "queue.Queue") -> None:
    """Writer loop for ONE queue.

    The queue is an argument rather than the module global on purpose: a
    worker that re-read the global on every iteration would, after the global
    was rebound (a test reloading the module, a future reset), take an item
    from the queue it is draining and mark it done on a different one — which
    raises "task_done() called too many times" from a thread nobody is
    watching. It owns the queue it was started for and nothing else.
    """
    global _last_error
    while True:
        item = work.get()
        try:
            if item is None:
                return
            _write(item[0])
            if item[1]:
                prune(item[2])
        except Exception as exc:          # capture must never raise
            _last_error = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            work.task_done()


def _ensure_writer() -> None:
    global _writer
    with _lock:
        if _writer is not None and _writer.is_alive():
            return
        _writer = threading.Thread(target=_drain, args=(_queue,),
                                   name="training-local", daemon=True)
        _writer.start()


_since_prune = 0


def record(sink: dict, result: dict, *, cwd: str = "", device: str = "",
           max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Queue one interaction. Never raises, never blocks."""
    global _dropped, _since_prune, _last_error
    try:
        row = build_record(sink, result, cwd=cwd, device=device)
        if row is None:
            return False
        _since_prune += 1
        check_size = _since_prune % 50 == 0
        _ensure_writer()
        _queue.put_nowait((row, check_size, max_bytes))
        return True
    except queue.Full:
        _dropped += 1
        return False
    except Exception as exc:
        _last_error = f"{type(exc).__name__}: {exc}"[:200]
        return False


def flush(timeout: float = 5.0) -> None:
    """Block until queued rows are written (tests and /training local status)."""
    if _writer is None or not _writer.is_alive():
        return
    # `empty()` goes True as soon as the last item is HANDED to the writer,
    # not when it is written — flushing on that returned a store one sample
    # short. `unfinished_tasks` only falls to zero after the matching
    # `task_done()`, which the writer calls once the row is committed.
    deadline = time.time() + timeout
    while _queue.unfinished_tasks and time.time() < deadline:
        time.sleep(0.01)


# ── maintenance ──────────────────────────────────────────────────────────

def store_bytes() -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(db_path() + suffix)
        except OSError:
            pass
    return total


def prune(max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """Drop the oldest rows until the store fits. Returns rows removed.

    Oldest-first rather than largest-first: a 150k-token main_loop turn is the
    most expensive row AND one of the more useful ones, so size is the wrong
    thing to select on. Recency is what a household actually wants kept.
    """
    if store_bytes() <= max_bytes:
        return 0
    removed = 0
    db = _connect()
    try:
        while store_bytes() > max_bytes:
            with db:
                cur = db.execute(
                    "DELETE FROM local_interactions WHERE interaction_id IN ("
                    "SELECT interaction_id FROM local_interactions "
                    "ORDER BY created_at LIMIT 200)")
                if not cur.rowcount:
                    break
                removed += cur.rowcount
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
    except Exception:
        pass
    finally:
        db.close()
    return removed


def stats() -> dict:
    """Row counts and size for `/training local status`."""
    out = {"rows": 0, "by_kind": [], "bytes": store_bytes(),
           "oldest": None, "newest": None, "trajectories": 0,
           "dropped": _dropped, "error": _last_error,
           "path": db_path(), "exists": os.path.exists(db_path())}
    if not out["exists"]:
        return out
    db = _connect()
    try:
        out["rows"] = db.execute(
            "SELECT COUNT(*) FROM local_interactions").fetchone()[0]
        out["trajectories"] = db.execute(
            "SELECT COUNT(DISTINCT trajectory_id) FROM local_interactions "
            "WHERE trajectory_id<>''").fetchone()[0]
        out["by_kind"] = db.execute(
            "SELECT CASE WHEN task_kind='' THEN '(none)' ELSE task_kind END,"
            "COUNT(*) FROM local_interactions GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        row = db.execute("SELECT MIN(created_at),MAX(created_at) "
                         "FROM local_interactions").fetchone()
        out["oldest"], out["newest"] = row[0], row[1]
    except Exception:
        pass
    finally:
        db.close()
    return out


def purge() -> int:
    """Delete every local sample. Returns rows removed."""
    if not os.path.exists(db_path()):
        return 0
    db = _connect()
    try:
        n = db.execute("SELECT COUNT(*) FROM local_interactions").fetchone()[0]
        with db:
            db.execute("DELETE FROM local_interactions")
            db.execute("DELETE FROM local_tool_catalogs")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        return n
    except Exception:
        return 0
    finally:
        db.close()


def export_jsonl(path: str, *, task_kinds: Optional[list] = None) -> int:
    """Write samples as {"messages":[...]} lines. Returns lines written.

    One line per interaction, tools re-inlined from the catalog table so the
    file stands alone. This is the handoff to a trainer; it is deliberately
    the same OpenAI message shape the Gateway exporter emits.
    """
    if not os.path.exists(db_path()):
        return 0
    sql = ("SELECT request_json,response_json,task_kind,trajectory_id,model,"
           "tool_catalog_hash,created_at FROM local_interactions")
    args: list[Any] = []
    if task_kinds:
        sql += " WHERE task_kind IN (%s)" % ",".join("?" * len(task_kinds))
        args = list(task_kinds)
    sql += " ORDER BY created_at"
    written = 0
    db = _connect()
    try:
        catalogs = dict(db.execute(
            "SELECT tool_catalog_hash,tools_json FROM local_tool_catalogs"))
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".",
                    exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for req, resp, kind, traj, model, chash, created in db.execute(sql, args):
                try:
                    request = json.loads(req)
                    assistant = json.loads(resp)
                except (ValueError, TypeError):
                    continue
                messages = list(request.get("messages") or [])
                system = str(request.get("systemPrompt") or "")
                if system and not (messages and messages[0].get("role") == "system"):
                    messages = [{"role": "system", "content": system}] + messages
                line = {
                    "messages": messages + [assistant],
                    "task_kind": kind,
                    "trajectory_id": traj,
                    "model": model,
                    "created_at": created,
                }
                if chash and catalogs.get(chash):
                    try:
                        line["tools"] = json.loads(catalogs[chash])
                    except (ValueError, TypeError):
                        pass
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                written += 1
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass
    finally:
        db.close()
    return written
