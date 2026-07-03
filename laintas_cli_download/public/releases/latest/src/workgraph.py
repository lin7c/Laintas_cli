"""Transactional project WorkGraph: one source for plans, steps and workflow.

The database is project-local (``.laintas/workgraph.db``).  Markdown plans,
slash-command views and workflow phases are projections of this state, not
independent authorities.  Connections are intentionally short-lived so
multiple CLI processes can safely cooperate through SQLite WAL transactions.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import paths


WORK_STATUSES = {
    "DRAFT", "REVIEW_PENDING", "APPROVED", "EXECUTING", "VERIFYING",
    "NEEDS_USER", "BLOCKED", "COMPLETED", "CANCELLED", "FAILED",
}
STEP_STATUSES = {"pending", "in_progress", "completed", "blocked", "skipped", "deleted"}
STEP_TRANSITIONS = {
    "pending": {"in_progress", "completed", "blocked", "skipped", "deleted"},
    "in_progress": {"pending", "completed", "blocked", "skipped", "deleted"},
    "blocked": {"pending", "in_progress", "skipped", "deleted"},
    "completed": {"in_progress", "deleted"},
    "skipped": {"pending", "deleted"},
    "deleted": set(),
}


class WorkGraphError(RuntimeError):
    pass


class WorkGraphConflict(WorkGraphError):
    pass


def db_path(cwd: Optional[str] = None) -> Path:
    root = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    return root / ".laintas" / "workgraph.db"


def _connect(cwd: Optional[str] = None) -> sqlite3.Connection:
    path = db_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS work_items (
      id TEXT PRIMARY KEY,
      objective TEXT NOT NULL,
      status TEXT NOT NULL,
      workflow_template TEXT NOT NULL DEFAULT '',
      workflow_phase TEXT NOT NULL DEFAULT '',
      workflow_state TEXT NOT NULL DEFAULT '{}',
      current_revision INTEGER NOT NULL DEFAULT 0,
      approved_revision INTEGER,
      approved_sha TEXT,
      session_id TEXT,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS plan_revisions (
      work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
      revision INTEGER NOT NULL,
      content TEXT NOT NULL,
      content_sha TEXT NOT NULL,
      author TEXT NOT NULL DEFAULT 'ai',
      created_at REAL NOT NULL,
      PRIMARY KEY(work_id, revision)
    );
    CREATE TABLE IF NOT EXISTS steps (
      work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
      id TEXT NOT NULL,
      subject TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      progress INTEGER NOT NULL DEFAULT 0,
      parent_id TEXT,
      owner_agent_id TEXT,
      metadata TEXT NOT NULL DEFAULT '{}',
      notes TEXT NOT NULL DEFAULT '[]',
      result TEXT NOT NULL DEFAULT '',
      session_only INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      PRIMARY KEY(work_id, id),
      FOREIGN KEY(work_id, parent_id) REFERENCES steps(work_id, id)
    );
    CREATE TABLE IF NOT EXISTS step_dependencies (
      work_id TEXT NOT NULL,
      step_id TEXT NOT NULL,
      blocked_by_step_id TEXT NOT NULL,
      PRIMARY KEY(work_id, step_id, blocked_by_step_id),
      FOREIGN KEY(work_id, step_id) REFERENCES steps(work_id, id) ON DELETE CASCADE,
      FOREIGN KEY(work_id, blocked_by_step_id) REFERENCES steps(work_id, id) ON DELETE CASCADE,
      CHECK(step_id <> blocked_by_step_id)
    );
    CREATE TABLE IF NOT EXISTS approvals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
      revision INTEGER NOT NULL,
      content_sha TEXT NOT NULL,
      decision TEXT NOT NULL,
      actor TEXT NOT NULL DEFAULT 'user',
      decided_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS work_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      payload TEXT NOT NULL DEFAULT '{}',
      created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_state (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(work_id, status);
    CREATE INDEX IF NOT EXISTS idx_events_work ON work_events(work_id, id);
    """)


@contextmanager
def transaction(cwd: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(cwd)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    item = dict(row)
    if "workflow_state" in item:
        item["workflow_state"] = _decode(item["workflow_state"], {})
    if "metadata" in item:
        item["metadata"] = _decode(item["metadata"], {})
    if "notes" in item:
        item["notes"] = _decode(item["notes"], [])
    if "session_only" in item:
        item["session_only"] = bool(item["session_only"])
    return item


def _event(conn: sqlite3.Connection, work_id: str, event_type: str,
           payload: Optional[dict] = None) -> None:
    conn.execute(
        "INSERT INTO work_events(work_id,event_type,payload,created_at) VALUES(?,?,?,?)",
        (work_id, event_type, _json(payload or {}), time.time()),
    )


def _active_id(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT value FROM project_state WHERE key='active_work_id'").fetchone()
    return str(row[0]) if row and row[0] else None


def set_active_work(work_id: Optional[str], cwd: Optional[str] = None) -> None:
    with transaction(cwd) as conn:
        if work_id and not conn.execute("SELECT 1 FROM work_items WHERE id=?", (work_id,)).fetchone():
            raise WorkGraphError(f"Work item not found: {work_id}")
        conn.execute(
            "INSERT INTO project_state(key,value) VALUES('active_work_id',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (work_id or "",),
        )


def get_project_value(key: str, *, cwd: Optional[str] = None) -> Any:
    if not db_path(cwd).exists():
        return None
    with _connect(cwd) as conn:
        row = conn.execute("SELECT value FROM project_state WHERE key=?", (key,)).fetchone()
    return _decode(row[0], None) if row else None


def set_project_value(key: str, value: Any, *, cwd: Optional[str] = None) -> None:
    with transaction(cwd) as conn:
        conn.execute(
            "INSERT INTO project_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _json(value)),
        )


def create_work(objective: str, *, cwd: Optional[str] = None,
                workflow_template: str = "", session_id: Optional[str] = None,
                activate: bool = True) -> dict:
    objective = str(objective or "").strip()
    if not objective:
        raise WorkGraphError("objective is required")
    work_id = f"work-{uuid.uuid4().hex[:12]}"
    now = time.time()
    with transaction(cwd) as conn:
        conn.execute(
            "INSERT INTO work_items(id,objective,status,workflow_template,session_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (work_id, objective, "DRAFT", workflow_template, session_id, now, now),
        )
        if activate:
            conn.execute(
                "INSERT INTO project_state(key,value) VALUES('active_work_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (work_id,))
        _event(conn, work_id, "work.created", {"objective": objective})
    return get_work(work_id, cwd=cwd) or {}


def get_work(work_id: str, *, cwd: Optional[str] = None) -> Optional[dict]:
    if not db_path(cwd).exists():
        return None
    with _connect(cwd) as conn:
        return _row(conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone())


def get_active_work(*, cwd: Optional[str] = None) -> Optional[dict]:
    if not db_path(cwd).exists():
        return None
    with _connect(cwd) as conn:
        work_id = _active_id(conn)
        if not work_id:
            return None
        return _row(conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone())


def ensure_active_work(objective: str = "Project tasks", *, cwd: Optional[str] = None) -> dict:
    active = get_active_work(cwd=cwd)
    if active and active.get("status") not in {"COMPLETED", "CANCELLED", "FAILED"}:
        return active
    work = create_work(objective, cwd=cwd)
    # Ad-hoc task collections execute immediately and do not require a plan review.
    with transaction(cwd) as conn:
        conn.execute("UPDATE work_items SET status='EXECUTING',updated_at=? WHERE id=?",
                     (time.time(), work["id"]))
        _event(conn, work["id"], "work.adhoc_started")
    return get_work(work["id"], cwd=cwd) or work


def list_work(*, cwd: Optional[str] = None) -> list[dict]:
    if not db_path(cwd).exists():
        return []
    with _connect(cwd) as conn:
        return [_row(row) for row in conn.execute(
            "SELECT * FROM work_items ORDER BY updated_at DESC").fetchall()]


def list_events(work_id: str, *, cwd: Optional[str] = None,
                limit: int = 50) -> list[dict]:
    if not db_path(cwd).exists():
        return []
    with _connect(cwd) as conn:
        rows = conn.execute(
            "SELECT * FROM work_events WHERE work_id=? ORDER BY id DESC LIMIT ?",
            (work_id, max(1, min(int(limit), 500)))).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = _decode(item.get("payload"), {})
        result.append(item)
    return result


def add_revision(work_id: str, content: str, *, cwd: Optional[str] = None,
                 author: str = "ai") -> dict:
    content = str(content or "").strip()
    if not content:
        raise WorkGraphError("plan content is empty")
    with transaction(cwd) as conn:
        work = conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise WorkGraphError(f"Work item not found: {work_id}")
        revision = int(work["current_revision"] or 0) + 1
        digest = _sha(content)
        now = time.time()
        conn.execute(
            "INSERT INTO plan_revisions(work_id,revision,content,content_sha,author,created_at) "
            "VALUES(?,?,?,?,?,?)", (work_id, revision, content, digest, author, now))
        conn.execute(
            "UPDATE work_items SET current_revision=?,status='DRAFT',approved_revision=NULL,"
            "approved_sha=NULL,updated_at=? WHERE id=?", (revision, now, work_id))
        _event(conn, work_id, "plan.revised", {"revision": revision, "sha": digest})
    return get_revision(work_id, revision, cwd=cwd) or {}


def get_revision(work_id: str, revision: Optional[int] = None,
                 *, cwd: Optional[str] = None) -> Optional[dict]:
    if not db_path(cwd).exists():
        return None
    with _connect(cwd) as conn:
        if revision is None:
            row = conn.execute(
                "SELECT r.* FROM plan_revisions r JOIN work_items w ON w.id=r.work_id "
                "WHERE r.work_id=? AND r.revision=w.current_revision", (work_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM plan_revisions WHERE work_id=? AND revision=?",
                (work_id, int(revision))).fetchone()
        return _row(row)


def submit_plan(work_id: str, *, cwd: Optional[str] = None) -> dict:
    with transaction(cwd) as conn:
        work = conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
        if not work or int(work["current_revision"] or 0) <= 0:
            raise WorkGraphError("plan has no revision to submit")
        revision = conn.execute(
            "SELECT * FROM plan_revisions WHERE work_id=? AND revision=?",
            (work_id, work["current_revision"])).fetchone()
        content = str(revision["content"] if revision else "").strip()
        required = ("## Context", "## Architecture", "## Implementation Steps",
                    "## Risks & Edge Cases", "## Test Plan")
        placeholders = ("[What problem", "[How should", "[Step 1", "[What could", "[How will")
        if (not revision or len(content) < 300
                or any(section not in content for section in required)
                or any(marker in content for marker in placeholders)):
            raise WorkGraphError(
                "plan is incomplete: replace template placeholders and include context, "
                "architecture, implementation steps, risks, and tests")
        implementation = content.split("## Implementation Steps", 1)[1]
        implementation = implementation.split("\n## ", 1)[0]
        subjects = []
        for line in implementation.splitlines():
            match = re.match(r"^\s*\d+[.)]\s+(.+?)\s*$", line)
            if match and not match.group(1).startswith("["):
                subjects.append(match.group(1))
        if not subjects:
            raise WorkGraphError("plan has no concrete numbered implementation steps")
        # Steps are the executable projection of the submitted plan revision.
        # Replace only untouched plan-generated steps; user-created steps remain.
        generated = []
        for row in conn.execute(
                "SELECT id,subject,metadata,status FROM steps WHERE work_id=?", (work_id,)).fetchall():
            meta = _decode(row["metadata"], {})
            if meta.get("source") == "plan":
                generated.append(dict(row))
        kept: set[str] = set()
        now = time.time()
        for subject in subjects:
            existing = next(
                (row for row in generated
                 if row["id"] not in kept and row["subject"] == subject[:300]), None)
            if existing:
                kept.add(str(existing["id"]))
                conn.execute(
                    "UPDATE steps SET metadata=?,updated_at=? WHERE work_id=? AND id=?",
                    (_json({"source": "plan", "revision": revision["revision"]}),
                     now, work_id, existing["id"]))
                continue
            step_id = _next_step_id(conn, work_id, False)
            conn.execute("""
              INSERT INTO steps(work_id,id,subject,metadata,created_at,updated_at)
              VALUES(?,?,?,?,?,?)
            """, (work_id, step_id, subject[:300],
                  _json({"source": "plan", "revision": revision["revision"]}), now, now))
            kept.add(step_id)
        for row in generated:
            if str(row["id"]) not in kept and row["status"] == "pending":
                conn.execute(
                    "DELETE FROM steps WHERE work_id=? AND id=?", (work_id, row["id"]))
        conn.execute("UPDATE work_items SET status='REVIEW_PENDING',updated_at=? WHERE id=?",
                     (time.time(), work_id))
        _event(conn, work_id, "plan.submitted", {
            "revision": revision["revision"], "sha": revision["content_sha"]})
    return review_snapshot(work_id, cwd=cwd)


def review_snapshot(work_id: str, *, cwd: Optional[str] = None) -> dict:
    work = get_work(work_id, cwd=cwd)
    if not work:
        raise WorkGraphError(f"Work item not found: {work_id}")
    revision = get_revision(work_id, work.get("current_revision"), cwd=cwd)
    if not revision:
        raise WorkGraphError("plan revision not found")
    return {"work": work, "revision": revision, "steps": list_steps(work_id, cwd=cwd)}


def approve_plan(work_id: str, revision: int, content_sha: str,
                 *, cwd: Optional[str] = None, actor: str = "user") -> dict:
    with transaction(cwd) as conn:
        work = conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
        rev = conn.execute(
            "SELECT * FROM plan_revisions WHERE work_id=? AND revision=?",
            (work_id, int(revision))).fetchone()
        if not work or not rev:
            raise WorkGraphError("work or revision not found")
        if work["status"] != "REVIEW_PENDING":
            raise WorkGraphConflict(f"plan is not awaiting review: {work['status']}")
        if int(work["current_revision"]) != int(revision) or rev["content_sha"] != content_sha:
            raise WorkGraphConflict("plan changed after review; review the new revision")
        now = time.time()
        conn.execute(
            "UPDATE work_items SET status='APPROVED',approved_revision=?,approved_sha=?,updated_at=? "
            "WHERE id=?", (revision, content_sha, now, work_id))
        conn.execute(
            "INSERT INTO approvals(work_id,revision,content_sha,decision,actor,decided_at) "
            "VALUES(?,?,?,?,?,?)", (work_id, revision, content_sha, "approved", actor, now))
        _event(conn, work_id, "plan.approved", {"revision": revision, "sha": content_sha})
    return get_work(work_id, cwd=cwd) or {}


def reject_plan(work_id: str, revision: int, content_sha: str,
                *, cwd: Optional[str] = None, actor: str = "user") -> dict:
    with transaction(cwd) as conn:
        now = time.time()
        conn.execute(
            "INSERT INTO approvals(work_id,revision,content_sha,decision,actor,decided_at) "
            "VALUES(?,?,?,?,?,?)", (work_id, revision, content_sha, "rejected", actor, now))
        conn.execute("UPDATE work_items SET status='DRAFT',updated_at=? WHERE id=?", (now, work_id))
        _event(conn, work_id, "plan.rejected", {"revision": revision})
    return get_work(work_id, cwd=cwd) or {}


def begin_execution(work_id: str, revision: int, content_sha: str,
                    *, cwd: Optional[str] = None) -> dict:
    with transaction(cwd) as conn:
        work = conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
        if not work or work["status"] != "APPROVED":
            raise WorkGraphConflict("work is not approved")
        if int(work["approved_revision"] or -1) != int(revision) or work["approved_sha"] != content_sha:
            raise WorkGraphConflict("execution revision does not match approval")
        rev = conn.execute(
            "SELECT content_sha FROM plan_revisions WHERE work_id=? AND revision=?",
            (work_id, int(revision))).fetchone()
        if not rev or rev["content_sha"] != content_sha:
            raise WorkGraphConflict("approved plan content changed")
        conn.execute("UPDATE work_items SET status='EXECUTING',updated_at=? WHERE id=?",
                     (time.time(), work_id))
        _event(conn, work_id, "work.execution_started", {"revision": revision})
    return get_work(work_id, cwd=cwd) or {}


def update_work(work_id: str, *, cwd: Optional[str] = None, **fields: Any) -> dict:
    allowed = {"status", "workflow_template", "workflow_phase", "workflow_state", "session_id"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "status" in updates and updates["status"] not in WORK_STATUSES:
        raise WorkGraphError(f"invalid work status: {updates['status']}")
    if not updates:
        return get_work(work_id, cwd=cwd) or {}
    if "workflow_state" in updates:
        updates["workflow_state"] = _json(updates["workflow_state"])
    updates["updated_at"] = time.time()
    clause = ",".join(f"{key}=?" for key in updates)
    with transaction(cwd) as conn:
        cursor = conn.execute(
            f"UPDATE work_items SET {clause} WHERE id=?",
            (*updates.values(), work_id))
        if cursor.rowcount != 1:
            raise WorkGraphError(f"Work item not found: {work_id}")
        _event(conn, work_id, "work.updated", fields)
    return get_work(work_id, cwd=cwd) or {}


def _next_step_id(conn: sqlite3.Connection, work_id: str, session_only: bool) -> str:
    rows = conn.execute("SELECT id FROM steps WHERE work_id=?", (work_id,)).fetchall()
    prefix = "s" if session_only else ""
    maximum = 0
    for row in rows:
        value = str(row[0])
        if bool(value.startswith("s")) != bool(session_only):
            continue
        try:
            maximum = max(maximum, int(value[1:] if prefix else value))
        except ValueError:
            pass
    return f"{prefix}{maximum + 1}"


def create_step(work_id: str, subject: str, description: str = "", *,
                cwd: Optional[str] = None, metadata: Optional[dict] = None,
                session_only: bool = False, parent_id: Optional[str] = None) -> dict:
    subject = str(subject or "").strip()
    if not subject:
        raise WorkGraphError("step subject is required")
    with transaction(cwd) as conn:
        if not conn.execute("SELECT 1 FROM work_items WHERE id=?", (work_id,)).fetchone():
            raise WorkGraphError(f"Work item not found: {work_id}")
        if parent_id and not conn.execute(
                "SELECT 1 FROM steps WHERE work_id=? AND id=?", (work_id, str(parent_id))).fetchone():
            raise WorkGraphError(f"Parent step not found: {parent_id}")
        step_id = _next_step_id(conn, work_id, session_only)
        now = time.time()
        conn.execute(
            "INSERT INTO steps(work_id,id,subject,description,parent_id,metadata,session_only,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (work_id, step_id, subject, description, str(parent_id) if parent_id else None,
             _json(metadata or {}), int(session_only), now, now))
        _event(conn, work_id, "step.created", {"id": step_id, "subject": subject})
    return get_step(work_id, step_id, cwd=cwd) or {}


def get_step(work_id: str, step_id: str, *, cwd: Optional[str] = None) -> Optional[dict]:
    if not db_path(cwd).exists():
        return None
    with _connect(cwd) as conn:
        row = conn.execute("SELECT * FROM steps WHERE work_id=? AND id=?",
                           (work_id, str(step_id))).fetchone()
        return _step_projection(conn, row) if row else None


def _step_projection(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    item = _row(row) or {}
    deps = [str(value[0]) for value in conn.execute(
        "SELECT blocked_by_step_id FROM step_dependencies WHERE work_id=? AND step_id=?",
        (row["work_id"], row["id"])).fetchall()]
    blocks = [str(value[0]) for value in conn.execute(
        "SELECT step_id FROM step_dependencies WHERE work_id=? AND blocked_by_step_id=?",
        (row["work_id"], row["id"])).fetchall()]
    children = [str(value[0]) for value in conn.execute(
        "SELECT id FROM steps WHERE work_id=? AND parent_id=?", (row["work_id"], row["id"])).fetchall()]
    item["blockedBy"] = deps
    item["blocks"] = blocks
    item["children"] = children
    return item


def list_steps(work_id: str, *, cwd: Optional[str] = None,
               include_deleted: bool = False) -> list[dict]:
    if not db_path(cwd).exists():
        return []
    with _connect(cwd) as conn:
        query = "SELECT * FROM steps WHERE work_id=?"
        args: list[Any] = [work_id]
        if not include_deleted:
            query += " AND status<>'deleted'"
        rows = conn.execute(query, args).fetchall()
        items = [_step_projection(conn, row) for row in rows]
    def key(item: dict):
        value = str(item.get("id", ""))
        try:
            return (0 if value.startswith("s") else 1, int(value.lstrip("s")))
        except ValueError:
            return (2, 0)
    return sorted(items, key=key)


def _dependency_cycle(conn: sqlite3.Connection, work_id: str,
                      step_id: str, blocker_id: str) -> bool:
    # Adding step -> blocker is invalid when blocker already reaches step.
    row = conn.execute("""
      WITH RECURSIVE reach(id) AS (
        SELECT blocked_by_step_id FROM step_dependencies WHERE work_id=? AND step_id=?
        UNION
        SELECT d.blocked_by_step_id FROM step_dependencies d JOIN reach r ON d.step_id=r.id
        WHERE d.work_id=?
      ) SELECT 1 FROM reach WHERE id=? LIMIT 1
    """, (work_id, blocker_id, work_id, step_id)).fetchone()
    return row is not None


def add_dependency(work_id: str, step_id: str, blocker_id: str,
                   *, cwd: Optional[str] = None) -> None:
    step_id, blocker_id = str(step_id), str(blocker_id)
    if step_id == blocker_id:
        raise WorkGraphError("a step cannot depend on itself")
    with transaction(cwd) as conn:
        existing = conn.execute(
            "SELECT id FROM steps WHERE work_id=? AND id IN (?,?)",
            (work_id, step_id, blocker_id)).fetchall()
        if len(existing) != 2:
            raise WorkGraphError("dependency references a missing step")
        if _dependency_cycle(conn, work_id, step_id, blocker_id):
            raise WorkGraphError("dependency would create a cycle")
        conn.execute(
            "INSERT OR IGNORE INTO step_dependencies(work_id,step_id,blocked_by_step_id) VALUES(?,?,?)",
            (work_id, step_id, blocker_id))
        _event(conn, work_id, "step.dependency_added", {
            "step_id": step_id, "blocked_by": blocker_id})


def remove_dependency(work_id: str, step_id: str, blocker_id: str,
                      *, cwd: Optional[str] = None) -> None:
    with transaction(cwd) as conn:
        conn.execute(
            "DELETE FROM step_dependencies WHERE work_id=? AND step_id=? AND blocked_by_step_id=?",
            (work_id, str(step_id), str(blocker_id)))


def update_step(work_id: str, step_id: str, *, cwd: Optional[str] = None,
                **fields: Any) -> dict:
    step_id = str(step_id)
    with transaction(cwd) as conn:
        row = conn.execute("SELECT * FROM steps WHERE work_id=? AND id=?",
                           (work_id, step_id)).fetchone()
        if not row:
            raise WorkGraphError(f"Step not found: {step_id}")
        status = fields.get("status", row["status"])
        if status not in STEP_STATUSES:
            raise WorkGraphError(f"invalid step status: {status}")
        if "status" in fields and status != row["status"]:
            allowed = STEP_TRANSITIONS.get(row["status"], set())
            if status not in allowed:
                raise WorkGraphConflict(
                    f"invalid step transition: {row['status']} -> {status}")
        try:
            progress = int(fields.get("progress", row["progress"]))
        except (TypeError, ValueError) as exc:
            raise WorkGraphError("progress must be an integer") from exc
        progress = max(0, min(100, progress))
        if progress == 100 and "status" not in fields:
            status = "completed"
        if status == "completed":
            progress = 100
        elif row["status"] == "completed" and status == "in_progress" and "progress" not in fields:
            progress = 0
        if status in {"in_progress", "completed"}:
            blockers = conn.execute("""
              SELECT d.blocked_by_step_id FROM step_dependencies d
              JOIN steps b ON b.work_id=d.work_id AND b.id=d.blocked_by_step_id
              WHERE d.work_id=? AND d.step_id=? AND b.status NOT IN ('completed','skipped','deleted')
            """, (work_id, step_id)).fetchall()
            if blockers:
                raise WorkGraphConflict(
                    "step is blocked by incomplete step(s): " + ", ".join(str(x[0]) for x in blockers))
        subject = str(fields.get("subject", row["subject"]))
        description = str(fields.get("description", row["description"]))
        metadata = _decode(row["metadata"], {})
        if fields.get("metadata") is not None:
            if not isinstance(fields["metadata"], dict):
                raise WorkGraphError("metadata must be an object")
            metadata.update(fields["metadata"])
        notes = _decode(row["notes"], [])
        if fields.get("notes"):
            notes.append({"at": time.time(), "text": str(fields["notes"])})
        conn.execute("""
          UPDATE steps SET subject=?,description=?,status=?,progress=?,metadata=?,notes=?,updated_at=?
          WHERE work_id=? AND id=?
        """, (subject, description, status, progress, _json(metadata), _json(notes),
              time.time(), work_id, step_id))
        _event(conn, work_id, "step.updated", {
            "id": step_id, "status": status, "progress": progress})
    return get_step(work_id, step_id, cwd=cwd) or {}


def clear_session_steps(*, cwd: Optional[str] = None) -> None:
    with transaction(cwd) as conn:
        conn.execute("DELETE FROM steps WHERE session_only=1")


def import_session_steps(work_id: str, items: list[dict], *,
                         cwd: Optional[str] = None,
                         session_key: str = "") -> int:
    """Restore ephemeral steps without duplicating IDs or persisted subjects."""
    if not isinstance(items, list):
        return 0
    count = 0
    with transaction(cwd) as conn:
        if not conn.execute("SELECT 1 FROM work_items WHERE id=?", (work_id,)).fetchone():
            raise WorkGraphError(f"Work item not found: {work_id}")
        persisted_subjects = {
            str(row[0]).strip() for row in conn.execute(
                "SELECT subject FROM steps WHERE work_id=? AND session_only=0", (work_id,)).fetchall()
        }
        used_ids = {
            str(row[0]) for row in conn.execute(
                "SELECT id FROM steps WHERE work_id=?", (work_id,)).fetchall()
        }
        id_map: dict[str, str] = {}
        next_session = 1
        for item in items:
            if not isinstance(item, dict) or not str(item.get("subject") or "").strip():
                continue
            subject = str(item["subject"]).strip()
            if subject in persisted_subjects:
                continue
            requested = str(item.get("id") or "")
            if not requested.startswith("s") or requested in used_ids:
                while f"s{next_session}" in used_ids:
                    next_session += 1
                requested = f"s{next_session}"
                next_session += 1
            used_ids.add(requested)
            id_map[str(item.get("id") or requested)] = requested
            status = str(item.get("status") or "pending")
            if status not in STEP_STATUSES:
                status = "pending"
            progress = max(0, min(100, int(item.get("progress") or 0)))
            if status == "completed":
                progress = 100
            now = time.time()
            metadata = dict(item.get("metadata") or {})
            if session_key:
                metadata["_session_key"] = session_key
            conn.execute("""
              INSERT INTO steps(work_id,id,subject,description,status,progress,metadata,notes,
                                session_only,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,1,?,?)
            """, (work_id, requested, subject, str(item.get("description") or ""),
                  status, progress, _json(metadata),
                  _json(item.get("notes") or []), now, now))
            count += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            step_id = id_map.get(str(item.get("id") or ""))
            if not step_id:
                continue
            for old_blocker in item.get("blockedBy", []) or []:
                blocker = id_map.get(str(old_blocker))
                if not blocker or blocker == step_id:
                    continue
                try:
                    if not _dependency_cycle(conn, work_id, step_id, blocker):
                        conn.execute(
                            "INSERT OR IGNORE INTO step_dependencies(work_id,step_id,blocked_by_step_id) "
                            "VALUES(?,?,?)", (work_id, step_id, blocker))
                except sqlite3.IntegrityError:
                    continue
        if count:
            _event(conn, work_id, "steps.session_imported", {"count": count})
    return count


def active_plan_context(*, cwd: Optional[str] = None) -> str:
    work = get_active_work(cwd=cwd)
    if not work:
        return ""
    revision_no = work.get("approved_revision") or work.get("current_revision")
    revision = get_revision(work["id"], revision_no, cwd=cwd) if revision_no else None
    steps = list_steps(work["id"], cwd=cwd)
    payload = {
        "id": work["id"], "objective": work["objective"], "status": work["status"],
        "revision": revision_no, "sha": revision.get("content_sha") if revision else None,
        "workflow_phase": work.get("workflow_phase"),
        "steps": [{k: step.get(k) for k in ("id", "subject", "status", "progress", "blockedBy")}
                  for step in steps[:20]],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def approved_plan_context(*, cwd: Optional[str] = None) -> str:
    """Return the exact approved revision used as the Act-stage authority."""
    work = get_active_work(cwd=cwd)
    if not work or work.get("status") not in {"APPROVED", "EXECUTING", "VERIFYING"}:
        return ""
    revision_no = work.get("approved_revision")
    digest = work.get("approved_sha")
    if not revision_no or not digest:
        return ""
    revision = get_revision(work["id"], int(revision_no), cwd=cwd)
    if not revision or revision.get("content_sha") != digest:
        return ""
    return (
        f'<approved_work_plan id="{work["id"]}" revision="{revision_no}" sha="{digest}">\n'
        f'{revision["content"][:24000]}\n'
        '</approved_work_plan>\n'
        'Execute only this approved revision. If it must change, stop and request a new plan revision.'
    )
