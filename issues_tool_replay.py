"""Durable deduplication for gateway-namespaced Issues tool executions."""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class ReplayPending(RuntimeError):
    pass


class Journal:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os,"O_NOFOLLOW",0)
        fd = os.open(self.path,flags,0o600)
        os.close(fd)
        db = sqlite3.connect(str(self.path),timeout=10,isolation_level=None)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,row TEXT)")
            yield db
        finally:
            db.close()

    def claim(self, call_id, name, arguments):
        digest = hashlib.sha256(json.dumps([name,arguments],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT digest,state,row FROM calls WHERE id=?", (call_id,)).fetchone()
            if old:
                db.rollback()
                if old[0] != digest:
                    raise ReplayPending("Issues call identity was reused for different arguments; execution refused.")
                if old[1] == "done":
                    return json.loads(old[2])
                raise ReplayPending("This Issues call is running or its prior outcome is unknown. Inspect the original execution before retrying; it was not executed again.")
            db.execute("INSERT INTO calls VALUES(?,?,'running',NULL)", (call_id,digest))
            db.commit()
        return None

    def complete(self, call_id, row):
        with self.db() as db:
            db.execute("UPDATE calls SET state='done',row=? WHERE id=? AND state='running'",
                       (json.dumps(row,ensure_ascii=False),call_id))


def journal():
    import paths
    return Journal(paths.SESSIONS_DIR / "issues-tool-results.sqlite3")
