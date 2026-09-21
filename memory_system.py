"""
Cross-session persistent memory system for laintas_cli.

Memory architecture:
  - 4 memory types: user, feedback, project, reference
  - Each memory is a .md file with YAML-style frontmatter
  - MEMORY.md is the index (one-liner per entry, no frontmatter)
  - Stored in ~/.laintas/memory/

Memory files are loaded into the agent's context at each loop iteration,
giving the AI persistent knowledge across sessions.
"""

from __future__ import annotations

import copy
import os
import re
import json
import math
import time
import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


import paths

try:
    import mem_signals   # Memory salience/recall weak-label capture (capability #4)
except Exception:
    mem_signals = None

MEMORY_DIR = paths.MEMORY_DIR
MEMORY_INDEX = paths.MEMORY_INDEX
LOCAL_USER_SCOPE = "local-user"

#: Entries that were written but never used again are the ones worth dropping
#: first, and nothing recorded whether an entry had ever been used. Usage lives
#: in a sidecar rather than in each memory's frontmatter for two reasons: a
#: counter bump must not rewrite a user-visible file, and it must not touch its
#: mtime — recall and the prompt both order by mtime, so writing "I read this"
#: into the file would make every read look like a fresh edit.
#: Resolved through a function, never bound at import: MEMORY_DIR is
#: redirected (tests, alternate stores), and a constant captured at import
#: time would keep pointing at the real store after the redirect.
USAGE_FILENAME = ".usage.json"


def usage_file() -> Path:
    return MEMORY_DIR / USAGE_FILENAME

#: Above this many live entries in one scope, the weakest are archived (moved
#: to ``memory/archive/``, dropped from the index, never deleted). The store
#: used to grow without any bound at all while the prompt window stayed the
#: same size, so everything past the window was cost without benefit.
MEMORY_BUDGET = int(os.environ.get("LAINTAS_MEMORY_BUDGET", "150"))

#: The same idea one level up. Entries are scoped to the project they were
#: learned in, so every scope can sit inside its own budget while the directory
#: as a whole keeps growing with one more project's worth of facts forever —
#: and every scan pays for all of them, whatever scope they belong to.
MEMORY_GLOBAL_BUDGET = int(os.environ.get("LAINTAS_MEMORY_GLOBAL_BUDGET", "400"))

#: Types that are never auto-archived. These are the user's own words about
#: themselves and about how they want work done: few, small, and the most
#: expensive thing in the store to lose.
PROTECTED_TYPES = ("user", "feedback")

#: Usage half-life for the eviction score, in days.
_USAGE_HALFLIFE_DAYS = 60.0


def _load_usage() -> dict:
    try:
        with open(usage_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_usage(data: dict) -> None:
    try:
        _atomic_write_text(usage_file(), json.dumps(data, ensure_ascii=False))
    except OSError:
        pass


class _UsageLock:
    """Cross-thread and cross-process lock around the usage sidecar.

    ``touch`` is read-modify-write on one shared file, and this CLI runs
    several agents at once, each rebuilding its own prompt. Measured without
    this: four concurrent writers recording 50 uses each landed 55 of 200 on
    disk — three quarters of the signal the eviction score depends on, lost to
    interleaving. A flock is held for microseconds and costs nothing next to
    being wrong.
    """

    def __init__(self):
        self._fh = None

    def __enter__(self):
        try:
            import os as _os
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = open(usage_file().with_suffix(".lock"), "a+")
            if _os.name == "nt":
                # D5 (bughunt): `import fcntl` raises on Windows and the old
                # handler fell through to "proceed unlocked", so the very
                # platform the lock exists for ran with silent lost updates.
                # msvcrt.locking on the first byte is the stdlib flock twin
                # (same pattern as session_lifecycle.guard).
                import msvcrt
                self._fh.seek(0)
                self._fh.write(" ")
                self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            # No lock primitive (or no directory): proceed unlocked rather
            # than lose the write entirely. Usage is a heuristic, not an
            # accounting ledger.
            self._close()
        return self

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                import os as _os
                if _os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self._close()
        return False

    def _close(self):
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None


#: Serialises same-process writers too: two threads holding two separate flocks
#: on the same file DO contend, but taking this first keeps the critical
#: section short and the lock file opened once per writer rather than per race.
_usage_thread_lock = __import__("threading").Lock()


def touch(names, *, kind: str = "read") -> None:
    """Record that these memories were actually USED.

    Called when an entry is read through the ``mem.read`` tool or shown in the
    prompt by recall — not when a listing walks past it. A memory that is
    merely on disk and a memory the agent keeps coming back to should not look
    the same to whatever decides what to drop.
    """
    if isinstance(names, str):
        names = [names]
    names = [str(n) for n in (names or []) if str(n or "").strip()]
    if not names:
        return
    now = time.time()
    with _usage_thread_lock, _UsageLock():
        data = _load_usage()
        for name in names:
            row = data.get(name)
            if not isinstance(row, dict):
                row = {"uses": 0, "last_used": 0.0}
            row["uses"] = int(row.get("uses", 0) or 0) + 1
            row["last_used"] = now
            row["kind"] = kind
            data[name] = row
        _save_usage(data)


def usage_of(name: str) -> dict:
    row = _load_usage().get(str(name))
    return row if isinstance(row, dict) else {"uses": 0, "last_used": 0.0}

# Valid memory types and their descriptions
MEMORY_TYPES = {
    "user": "User profile, role, preferences, knowledge level",
    "feedback": "User corrections and confirmations about how to approach work",
    "project": "Project goals, deadlines, constraints, ongoing initiatives",
    "structure": "Project structure / architecture facts (module map, layout, key files)",
    "reference": "Pointers to external resources (dashboards, repos, channels)",
}

# User-facing category labels for each underlying type. Single source of truth
# shared by the /memory manager UI and the extraction prompt so the taxonomy
# never drifts between the two.
CATEGORY_LABELS = {
    "user": "User Info",
    "feedback": "Preferences",
    "project": "Project Updates",
    "structure": "Project Structure",
    "reference": "External Resources",
}

# Stable display order for the manager UI (global types first, then local).
CATEGORY_ORDER = {"user": 0, "feedback": 1, "project": 2, "structure": 3, "reference": 4}


def scope_label(scope: str) -> str:
    """Human label for a stored scope: user scope = global, project scope = local."""
    return "global" if scope == "user" else "local"


# ── Assertion lifecycle (evidence-backed memories) ─────────────────────────
# A memory that cites source is an assertion about that source, and source
# moves. Rather than let it rot silently or delete it on the first edit, an
# entry carries the evidence it was derived from and a status:
#
#   active      — evidence still hashes to what it did when the entry was written
#   stale       — the cited file changed; the claim is UNVERIFIED, not wrong
#   superseded  — a successor entry replaced it; kept on disk, dropped from the
#                 index, so the Y → Y' → Y'' chain stays walkable
#
# Nothing here deletes: these files are user-visible and a vanished memory is
# worse than a labelled one. Modelled on the bitemporal invalidate-don't-delete
# rule from temporal knowledge-graph memory (Zep/Graphiti).
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_SUPERSEDED = "superseded"
VALID_STATUSES = (STATUS_ACTIVE, STATUS_STALE, STATUS_SUPERSEDED)

#: One evidence item: ``/abs/path@<sha12>`` with an optional ``:start-end``
#: line range. Items are joined with ";" because the frontmatter parser is a
#: flat key/value reader with no list support — a single line keeps the file
#: readable and the parser unchanged.
_EVIDENCE_RE = re.compile(
    r"^(?P<path>.+?)@(?P<sha>[0-9a-f]{6,64})(?::(?P<start>\d+)-(?P<end>\d+))?$")


def parse_evidence(raw) -> list[dict]:
    """Decode the ``evidence`` frontmatter value into dicts.

    Unparseable items are dropped rather than raising: a hand-edited memory
    file must never make the whole store unreadable.
    """
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict) and item.get("path")]
    out: list[dict] = []
    for chunk in str(raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _EVIDENCE_RE.match(chunk)
        if not m:
            continue
        item = {"path": m.group("path").strip(), "sha": m.group("sha")}
        if m.group("start"):
            item["start"] = int(m.group("start"))
            item["end"] = int(m.group("end"))
        out.append(item)
    return out


def format_evidence(items) -> str:
    """Encode evidence dicts back into the single-line frontmatter form."""
    parts = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        sha = str(item.get("sha") or "").strip()
        if not path or not sha:
            continue
        span = ""
        if item.get("start") and item.get("end"):
            span = f":{int(item['start'])}-{int(item['end'])}"
        parts.append(f"{path}@{sha}{span}")
    return ";".join(parts)

_MEMORY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_LEGACY_MEMORY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,79}$")


def _memory_path(name: str, *, strict: bool = False) -> Path:
    """Resolve a validated memory slug without permitting path traversal."""
    candidate = str(name or "").strip()
    accepted = (_MEMORY_NAME_RE if strict else _LEGACY_MEMORY_NAME_RE).fullmatch(candidate)
    if not accepted:
        raise ValueError(
            "memory name must be a safe slug without path separators"
        )
    root = MEMORY_DIR.resolve()
    path = (root / f"{candidate}.md").resolve()
    if path.parent != root:
        raise ValueError("memory name escapes the memory directory")
    return path


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))
    finally:
        _forget_parsed(path)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# D4: same-process serializer for the shared MEMORY.md index. fcntl/msvcrt
# cross-process locking lives in _UsageLock; the index is per-CLI-process
# here, so a module lock plus atomic replace covers both races and crashes.
import threading as _threading
_index_lock = _threading.RLock()


def ensure_memory_dir() -> Path:
    """Create the memory directory and index if they don't exist."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_INDEX.exists():
        MEMORY_INDEX.write_text(
            "# laintas_cli Memory Index\n"
            "# One line per memory entry. Lines starting with # are ignored.\n"
            "# Format: - [Title](file.md) — one-line description\n\n",
            encoding="utf-8",
        )
    migrate_legacy_scopes()
    return MEMORY_DIR


_legacy_migrated_dirs: set = set()


def migrate_legacy_scopes() -> int:
    """Persist scope/scope_id for entries written before scoping existed.

    Runs at most once per process. This is the write half of the compatibility
    shim in ``list_memories``, kept out of the read path so that listing the
    store can never modify it.
    """
    key = str(MEMORY_DIR)
    if key in _legacy_migrated_dirs:
        return 0
    _legacy_migrated_dirs.add(key)
    fixed = 0
    for f in MEMORY_DIR.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
            if meta.get("scope") and meta.get("scope_id"):
                continue
            entry_type = meta.get("type") or meta.get("metadata", {}).get("type", "unknown")
            scope, scope_id = _resolve_scope(entry_type)
            meta.update({
                "product": "laintas_cli",
                "scope": scope,
                "scope_id": scope_id,
                "importance": meta.get("importance", "0.5"),
            })
            _atomic_write_text(f, _format_frontmatter(meta, body))
            fixed += 1
        except (OSError, ValueError):
            continue
    return fixed


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML-like frontmatter from a memory file.

    Expects:
    ---
    name: slug-name
    description: one-line summary
    metadata:
      type: user|feedback|project|reference
    ---
    Body content follows.

    Returns (metadata_dict, body_text).
    """
    meta: dict = {}
    body = content

    # Match frontmatter delimited by ---
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', content, re.DOTALL)
    if not m:
        return meta, content

    front = m.group(1)
    body = m.group(2)

    # Parse simple key: value lines (nested via indentation)
    current_section = meta
    section_stack: list = []
    for line in front.split('\n'):
        # Skip empty lines
        if not line.strip():
            continue
        # Detect indentation level
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if ':' in stripped:
            key, _, value = stripped.partition(':')
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            # Pop sections that are at same or higher level
            while section_stack and section_stack[-1][0] >= indent:
                section_stack.pop()
                current_section = meta
                for _, sec in section_stack:
                    current_section = current_section.setdefault(sec, {})

            if not value:  # This is a section header
                current_section = current_section.setdefault(key, {})
                section_stack.append((indent, key))
            else:
                current_section[key] = value

    # Normalize: extract 'metadata' sub-dict if present
    if 'metadata' in meta and isinstance(meta['metadata'], dict):
        for k, v in meta['metadata'].items():
            meta.setdefault(k, v)

    return meta, body.strip()


def _sanitize_frontmatter_value(value) -> str:
    """One line, no leading key shape (E2, bughunt).

    Frontmatter is line-oriented YAML; a value carrying a newline writes a
    new key into the header (LLM-generated descriptions reach this), and a
    value like `type: user` after `description: ` smuggles a second key onto
    the same line. Collapse whitespace to single spaces and strip the
    leading `key:` shape so round-trips cannot inject or corrupt fields.
    """
    text = str(value if value is not None else "")
    # Collapse all whitespace (newlines included) to single spaces: the
    # value can never break out of its line, so no injected key can exist.
    return " ".join(text.split())


def _format_frontmatter(meta: dict, body: str) -> str:
    """Format a memory file with frontmatter."""
    lines = ["---"]
    lines.append(f"name: {_sanitize_frontmatter_value(meta.get('name', ''))}")
    lines.append(f"description: {_sanitize_frontmatter_value(meta.get('description', ''))}")
    if 'type' in meta:
        lines.append("metadata:")
        lines.append(f"  type: {meta['type']}")
    if meta.get('product'):
        lines.append(f"product: {_sanitize_frontmatter_value(meta['product'])}")
    if meta.get('scope'):
        lines.append(f"scope: {_sanitize_frontmatter_value(meta['scope'])}")
    if meta.get('scope_id'):
        lines.append(f"scope_id: {_sanitize_frontmatter_value(meta['scope_id'])}")
    if meta.get('importance') is not None:
        lines.append(f"importance: {meta['importance']}")
    # When the entry was first written. Not the same question as the file's
    # mtime, which answers "when was it last touched" and gets reset by merges.
    if meta.get('created_at'):
        lines.append(f"created_at: {meta['created_at']}")
    if meta.get('archived_at'):
        lines.append(f"archived_at: {meta['archived_at']}")
    if meta.get('archived_reason'):
        lines.append(f"archived_reason: {_sanitize_frontmatter_value(str(meta['archived_reason'])[:200])}")
    # Assertion lifecycle. Written only when non-default so untouched memories
    # keep the exact bytes they had before this existed.
    if meta.get('evidence'):
        value = (meta['evidence'] if isinstance(meta['evidence'], str)
                 else format_evidence(meta['evidence']))
        if value:
            lines.append(f"evidence: {value}")
    if meta.get('status') and meta['status'] != STATUS_ACTIVE:
        lines.append(f"status: {meta['status']}")
    if meta.get('stale_reason'):
        lines.append(f"stale_reason: {_sanitize_frontmatter_value(str(meta['stale_reason'])[:300])}")
    if meta.get('superseded_by'):
        lines.append(f"superseded_by: {meta['superseded_by']}")
    # L4 (bughunt): a rewrite is a round-trip, not a projection. Keys the
    # whitelist above does not know (written by a newer version, another
    # product, or a manual edit) used to vanish on every rewrite — silent
    # data loss for anything this build cannot interpret yet. Re-emit them
    # verbatim, sanitized like every other header value.
    _KNOWN_KEYS = {
        'name', 'description', 'metadata', 'type', 'product', 'scope',
        'scope_id', 'importance', 'created_at', 'archived_at',
        'archived_reason', 'evidence', 'status', 'stale_reason',
        'superseded_by',
    }
    for key, value in meta.items():
        if key not in _KNOWN_KEYS and not key.startswith('_'):
            lines.append(f"{key}: {_sanitize_frontmatter_value(value)}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return '\n'.join(lines)


def _project_scope_id(cwd: str = None) -> str:
    start = Path(os.path.realpath(cwd or os.getcwd()))
    root = start
    for candidate in (start, *start.parents):
        if (candidate / ".laintas").is_dir() or (candidate / ".git").exists():
            root = candidate
            break
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _resolve_scope(mem_type: str, scope: str = None,
                   scope_id: str = None) -> tuple[str, str]:
    resolved = scope or ("user" if mem_type in ("user", "feedback") else "project")
    if resolved not in ("user", "project"):
        raise ValueError("scope must be 'user' or 'project'")
    if resolved == "user":
        return resolved, scope_id or LOCAL_USER_SCOPE
    return resolved, scope_id or _project_scope_id()


def _visible_in_current_scope(meta: dict) -> bool:
    scope = meta.get("scope")
    if scope == "user":
        return meta.get("scope_id", LOCAL_USER_SCOPE) == LOCAL_USER_SCOPE
    if scope == "project":
        return meta.get("scope_id") == _project_scope_id()
    return False


#: Parsed-memory cache. search_memories reads every file twice (once via
#: list_memories, once via read_memory) and load_all_for_prompt rescans the
#: whole directory on every prompt build — with the budget in the hundreds of
#: entries that is real IO on the agent's hot path.
#:
#: Two invalidation paths, because a stat key alone is not enough: file
#: timestamps are tick-coarse and os.replace recycles inodes, so a same-length
#: rewrite in the same tick could reproduce an older version's
#: (path, inode, mtime_ns, size) exactly and read back stale. Every write and
#: delete in this module therefore drops the path's entries (_forget_parsed);
#: the stat key only has to catch other processes, whose same-tick rewrites
#: are the one residual window. meta is deep-copied out so callers may mutate
#: it (nested `metadata` included) without touching the cache.
_parse_cache: dict[tuple, tuple[dict, str]] = {}
_parse_cache_lock = _threading.RLock()
_PARSE_CACHE_MAX = 512


def _forget_parsed(path) -> None:
    """Drop every cached parse of `path` (called on each write/delete)."""
    target = os.path.abspath(str(path))
    with _parse_cache_lock:
        for key in [k for k in _parse_cache if k[0] == target]:
            del _parse_cache[key]


def _parse_memory_file(f) -> Optional[tuple[dict, str]]:
    """Read+parse one memory file, cached by (path, inode, mtime_ns, size).

    Returns a fresh (meta, body) copy, or None when the file vanished or
    cannot be read. A miss costs one read+parse; a hit costs zero IO.
    """
    try:
        st = f.stat()
        key = (os.path.abspath(str(f)), st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _parse_cache_lock:
        cached = _parse_cache.get(key)
    if cached is not None:
        return copy.deepcopy(cached[0]), cached[1]
    try:
        content = f.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = _parse_frontmatter(content)
    with _parse_cache_lock:
        if len(_parse_cache) >= _PARSE_CACHE_MAX:
            _parse_cache.clear()
        _parse_cache[key] = parsed
    return copy.deepcopy(parsed[0]), parsed[1]


def list_memories(mem_type: str = None, *,
                  include_superseded: bool = False,
                  all_scopes: bool = False) -> list[dict]:
    """List all memory entries, optionally filtered by type.

    Returns list of {name, description, type, path, mtime, status, evidence}.
    Superseded entries are hidden by default — they stay on disk so the
    supersession chain can be walked, but they are not current knowledge.
    """
    ensure_memory_dir()
    usage = _load_usage()
    results = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            parsed = _parse_memory_file(f)
            if parsed is None:
                continue
            meta, body = parsed
            entry_type = meta.get("type") or meta.get("metadata", {}).get("type", "unknown")
            if not meta.get("scope") or not meta.get("scope_id"):
                # Compatibility: bind legacy project/reference facts to the
                # current project; user/feedback stays global. Resolved in
                # memory only. Listing used to PERSIST this, which made a read
                # a write: every scan could rewrite files, bumping the mtime
                # that recall and the prompt order by. migrate_legacy_scopes()
                # does the writing, once, off this path.
                scope, scope_id = _resolve_scope(entry_type)
                meta.update({
                    "product": "laintas_cli",
                    "scope": scope,
                    "scope_id": scope_id,
                    "importance": meta.get("importance", "0.5"),
                })
            if mem_type and entry_type != mem_type:
                continue
            if not all_scopes and not _visible_in_current_scope(meta):
                continue
            status = str(meta.get("status") or STATUS_ACTIVE)
            if status not in VALID_STATUSES:
                status = STATUS_ACTIVE
            if status == STATUS_SUPERSEDED and not include_superseded:
                continue
            _usage = usage.get(meta.get("name", f.stem)) or {}
            results.append({
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": entry_type,
                "scope": meta.get("scope"),
                "scope_id": meta.get("scope_id"),
                "importance": float(meta.get("importance", 0.5) or 0.5),
                "status": status,
                "evidence": parse_evidence(meta.get("evidence")),
                "stale_reason": str(meta.get("stale_reason") or ""),
                "superseded_by": str(meta.get("superseded_by") or ""),
                "path": str(f),
                "mtime": f.stat().st_mtime,
                "created_at": float(meta.get("created_at") or 0.0) or None,
                "uses": int(_usage.get("uses", 0) or 0),
                "last_used": float(_usage.get("last_used", 0.0) or 0.0),
            })
        except (OSError, ValueError):
            pass
    return results


def read_memory(name: str) -> Optional[dict]:
    """Read a single memory file. Returns {meta, body, path} or None."""
    ensure_memory_dir()
    try:
        f = _memory_path(name)
    except ValueError:
        return None
    if not f.exists():
        # An archived entry is still readable by name. Archiving bounds what is
        # live, not what is knowable — a pointer from some other memory must
        # not dead-end because the target fell out of the budget.
        archived = archive_dir() / f.name
        if not archived.exists():
            return None
        f = archived
    try:
        parsed = _parse_memory_file(f)
        if parsed is None:
            return None
        meta, body = parsed
        entry_type = meta.get("type") or meta.get("metadata", {}).get("type", "unknown")
        if not meta.get("scope") or not meta.get("scope_id"):
            # Resolved for this read only; migrate_legacy_scopes() persists it.
            scope, scope_id = _resolve_scope(entry_type)
            meta.update({
                "product": "laintas_cli",
                "scope": scope,
                "scope_id": scope_id,
                "importance": meta.get("importance", "0.5"),
            })
        if not _visible_in_current_scope(meta):
            return None
        return {"meta": meta, "body": body, "path": str(f)}
    except (OSError, ValueError):
        return None


def search_memories(query: str = "", mem_type: str = None,
                    limit: int = 10) -> list[dict]:
    """Return visible memories ranked by lexical relevance and importance."""
    query_terms = set(re.findall(r"[\w-]+", str(query or "").lower()))
    ranked = []
    for entry in list_memories(mem_type):
        data = read_memory(entry["name"])
        if not data:
            continue
        searchable = (
            f"{entry['name']} {entry.get('description', '')} {data['body']}"
        ).lower()
        hits = sum(1 for term in query_terms if term in searchable)
        if query_terms and hits == 0:
            continue
        score = hits * 10.0 + float(entry.get("importance", 0.5))
        ranked.append({
            **entry,
            "body_preview": data["body"][:500],
            "score": round(score, 3),
        })
    ranked.sort(
        key=lambda item: (item["score"], item.get("mtime", 0)), reverse=True)
    _out = ranked[:max(1, min(int(limit or 10), 50))]
    # Recall signal: only log genuine queries (empty query = bulk enumeration).
    if mem_signals is not None and str(query or "").strip():
        try:
            mem_signals.on_search(query, _out)
        except Exception:
            pass
    return _out


def write_memory(name: str, mem_type: str, description: str,
                 body: str, overwrite: bool = True,
                 scope: str = None, scope_id: str = None,
                 importance: float = 0.5,
                 evidence=None) -> tuple[bool, str]:
    """Create or update a memory file. Updates MEMORY.md index.

    ``evidence`` is a list of ``{path, sha, start?, end?}`` (or the encoded
    string) naming the source this claim was derived from. Supplying it is a
    re-attestation: the entry returns to ``active`` even if it was stale.
    Omitting it on an update PRESERVES whatever evidence and status the entry
    already had — rewriting the prose of a stale claim does not re-verify it,
    and silently clearing the flag would hide exactly what it exists to show.

    Returns (ok, message).
    """
    ensure_memory_dir()

    if mem_type not in MEMORY_TYPES and mem_type != "unknown":
        return False, f"Invalid memory type: {mem_type}. Use: {list(MEMORY_TYPES)}"

    try:
        f = _memory_path(name, strict=True)
    except ValueError as exc:
        return False, str(exc)
    _existed_before = f.exists()
    if _existed_before and not overwrite:
        return False, f"Memory '{name}' already exists. Use overwrite=True."

    try:
        resolved_scope, resolved_scope_id = _resolve_scope(mem_type, scope, scope_id)
        resolved_importance = max(0.0, min(1.0, float(importance)))
    except (TypeError, ValueError) as exc:
        return False, str(exc)

    prior_meta: dict = {}
    if _existed_before:
        try:
            prior_meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
        except OSError:
            prior_meta = {}

    if evidence is not None:
        evidence_value = (evidence if isinstance(evidence, str)
                          else format_evidence(evidence))
        status = STATUS_ACTIVE
        stale_reason = ""
    else:
        evidence_value = str(prior_meta.get("evidence") or "")
        status = str(prior_meta.get("status") or STATUS_ACTIVE)
        if status not in VALID_STATUSES:
            status = STATUS_ACTIVE
        stale_reason = str(prior_meta.get("stale_reason") or "")

    # First write wins: created_at is the entry's birthday, not its last edit,
    # and a merge that folds a new fact into an old entry must not make the old
    # entry look new. Without it nothing could tell a memory written today from
    # one written in July — file mtime answers a different question.
    try:
        created_at = float(prior_meta.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    if created_at <= 0:
        created_at = time.time()

    meta = {
        "name": name,
        "description": description,
        "type": mem_type,
        "product": "laintas_cli",
        "created_at": round(created_at, 3),
        "scope": resolved_scope,
        "scope_id": resolved_scope_id,
        "importance": resolved_importance,
        "evidence": evidence_value,
        "status": status,
        "stale_reason": stale_reason if status == STATUS_STALE else "",
        "superseded_by": str(prior_meta.get("superseded_by") or ""),
    }
    content = _format_frontmatter(meta, body)
    try:
        _atomic_write_text(f, content)
    except OSError as e:
        return False, str(e)

    _update_index(name, description, mem_type, resolved_scope, status=status)
    if mem_signals is not None:
        try:
            mem_signals.on_write(name, mem_type, description, body,
                                 is_update=_existed_before,
                                 importance=resolved_importance)
        except Exception:
            pass
    return True, str(f)


def delete_memory(name: str) -> tuple[bool, str]:
    """Delete a memory file. Removes from MEMORY.md index.

    Returns (ok, message).
    """
    try:
        f = _memory_path(name)
    except ValueError as exc:
        return False, str(exc)
    if not f.exists():
        return False, f"Memory '{name}' not found"
    try:
        f.unlink()
        _forget_parsed(f)
        _remove_from_index(name)
        if mem_signals is not None:
            try:
                mem_signals.on_delete(name)
            except Exception:
                pass
        return True, f"Deleted {name}"
    except OSError as e:
        return False, str(e)


def _update_index(name: str, description: str, mem_type: str,
                  scope: str = "user", status: str = STATUS_ACTIVE) -> None:
    """Add or update an entry in MEMORY.md.

    A superseded entry is removed from the index instead: the file survives so
    the chain stays walkable, but it is no longer current knowledge and must
    not be recalled as if it were.
    """
    ensure_memory_dir()
    if status == STATUS_SUPERSEDED:
        _remove_from_index(name)
        return
    # D4 (bughunt): read-modify-write of a shared index with bare write_text
    # lost concurrent updates (last writer wins, first writer's entry gone)
    # and a crash mid-write corrupted the whole index. Serialise with the
    # module lock and land via the atomic tmp+replace path.
    with _index_lock:
        lines = MEMORY_INDEX.read_text(encoding="utf-8").split('\n') \
            if MEMORY_INDEX.exists() else []
        flag = " [stale — evidence changed, unverified]" if status == STATUS_STALE else ""
        new_entry = f"- [{name}]({name}.md) — {description} [{mem_type}; {scope}]{flag}"

        # Replace existing entry if present
        replaced = False
        for i, line in enumerate(lines):
            if f"]({name}.md)" in line:
                lines[i] = new_entry
                replaced = True
                break

        if not replaced:
            lines.append(new_entry)

        _atomic_write_text(MEMORY_INDEX, '\n'.join(lines))


def _remove_from_index(name: str) -> None:
    """Remove an entry from MEMORY.md."""
    if not MEMORY_INDEX.exists():
        return
    with _index_lock:
        lines = MEMORY_INDEX.read_text(encoding="utf-8").split('\n')
        lines = [l for l in lines if f"]({name}.md)" not in l]
        _atomic_write_text(MEMORY_INDEX, '\n'.join(lines))


def _rewrite_meta(name: str, updates: dict) -> tuple[bool, str]:
    """Rewrite one memory's frontmatter in place, leaving the body untouched.

    The lifecycle transitions all go through here so there is exactly one place
    that can change a memory's status, and none of them can touch its prose.
    """
    try:
        f = _memory_path(name)
    except ValueError as exc:
        return False, str(exc)
    if not f.exists():
        return False, f"Memory '{name}' not found"
    try:
        meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
    except OSError as exc:
        return False, str(exc)
    meta.update(updates)
    try:
        _atomic_write_text(f, _format_frontmatter(meta, body))
    except OSError as exc:
        return False, str(exc)
    _update_index(meta.get("name", name), meta.get("description", ""),
                  meta.get("type", "unknown"), meta.get("scope", "user"),
                  status=str(meta.get("status") or STATUS_ACTIVE))
    return True, str(f)


def mark_stale(name: str, reason: str) -> tuple[bool, str]:
    """Flag a memory as unverified because the source it cites changed.

    Deliberately not a delete and not an edit: the claim may well still be
    true, and only re-reading the source can say. Idempotent.
    """
    entry = read_memory(name)
    if entry is None:
        return False, f"Memory '{name}' not found"
    if str(entry["meta"].get("status") or STATUS_ACTIVE) == STATUS_SUPERSEDED:
        return False, f"Memory '{name}' is superseded"
    return _rewrite_meta(name, {"status": STATUS_STALE,
                                "stale_reason": str(reason or "")[:300]})


def clear_stale(name: str, evidence=None) -> tuple[bool, str]:
    """Return a stale memory to active, optionally re-pinning its evidence."""
    updates = {"status": STATUS_ACTIVE, "stale_reason": ""}
    if evidence is not None:
        updates["evidence"] = (evidence if isinstance(evidence, str)
                               else format_evidence(evidence))
    return _rewrite_meta(name, updates)


def supersede_memory(name: str, successor: str) -> tuple[bool, str]:
    """Retire a memory in favour of ``successor`` without deleting it.

    This is what makes the Y → Y' → Y'' chain readable after the fact: the old
    file keeps its body and gains a forward pointer, and only the index entry
    goes away.
    """
    if not str(successor or "").strip():
        return False, "successor name is required"
    if successor == name:
        return False, "a memory cannot supersede itself"
    if read_memory(successor) is None:
        return False, f"successor '{successor}' does not exist"
    return _rewrite_meta(name, {"status": STATUS_SUPERSEDED,
                                "superseded_by": successor,
                                "stale_reason": ""})


def retire_memory(name: str, reason: str) -> tuple[bool, str]:
    """Retire a memory that is no longer true and has no replacement.

    Terminal, like supersession, and for the same reason kept on disk: a user
    who goes looking for a fact they know they taught the agent should find it
    with a note saying when it stopped holding, not an empty directory.
    """
    return _rewrite_meta(name, {"status": STATUS_SUPERSEDED,
                                "superseded_by": "",
                                "stale_reason": str(reason or "")[:300]})


def successor_name(name: str) -> str:
    """Next free ``<base>-N`` slug, so a revision chain reads in order."""
    base = re.sub(r"-(\d+)$", "", str(name or "memory"))
    n = 2
    while n < 100:
        candidate = f"{base}-{n}"
        try:
            if not _memory_path(candidate, strict=True).exists():
                return candidate
        except ValueError:
            break
        n += 1
    return f"{base}-{uuid.uuid4().hex[:6]}"


def list_stale(mem_type: str = None) -> list[dict]:
    """Every visible memory whose cited source has moved under it."""
    return [entry for entry in list_memories(mem_type)
            if entry.get("status") == STATUS_STALE]


# ── Budget ────────────────────────────────────────────────────────────────
# Writing was unbounded and reading was not: the store grew every turn while
# the prompt kept showing the same handful of entries, so past a point new
# memories cost tokens and disk and bought nothing. A scope now has a budget;
# past it, the weakest entries are ARCHIVED — moved aside and dropped from the
# index, never deleted, because "nothing here destroys a memory" is the rule
# the rest of this module is built on.

ARCHIVE_DIRNAME = "archive"


def archive_dir() -> Path:
    """Where archived entries live. Resolved per call, like ``usage_file``."""
    return MEMORY_DIR / ARCHIVE_DIRNAME


def eviction_score(entry: dict, now: float = None) -> float:
    """How much an entry has earned its place. Lower is weaker.

    Three things matter and they are all on the entry: how important it was
    thought to be when written, how long since it was last USED (not written —
    a file nobody has read in two months is the candidate, however recently
    some merge rewrote it), and how often it has been used at all.
    """
    now = time.time() if now is None else now
    importance = float(entry.get("importance", 0.5) or 0.5)
    uses = int(entry.get("uses", 0) or 0)
    last = float(entry.get("last_used") or 0.0)
    if last <= 0:
        last = float(entry.get("created_at") or entry.get("mtime") or now)
    age_days = max(0.0, (now - last) / 86400.0)
    recency = 0.5 ** (age_days / _USAGE_HALFLIFE_DAYS)
    return importance * recency * (1.0 + math.log1p(uses))


def archive_memory(name: str, reason: str = "") -> tuple[bool, str]:
    """Move one memory out of the live set. The file survives under archive/."""
    try:
        f = _memory_path(name)
    except ValueError as exc:
        return False, str(exc)
    if not f.exists():
        return False, f"Memory '{name}' not found"
    try:
        target_dir = archive_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
        meta["archived_at"] = round(time.time(), 3)
        meta["archived_reason"] = str(reason or "")[:200]
        _atomic_write_text(target_dir / f.name, _format_frontmatter(meta, body))
        f.unlink()
        _forget_parsed(f)
    except OSError as exc:
        return False, str(exc)
    _remove_from_index(name)
    return True, str(target_dir / f.name)


def enforce_budget(limit: int = None, *, dry_run: bool = False,
                   max_archived: int = 0) -> list[dict]:
    """Archive the weakest entries until the visible scope fits its budget.

    Returns the entries archived (or, with ``dry_run``, the ones that would
    be). ``user`` and ``feedback`` entries are never chosen: they are what the
    person told us about themselves and about how to work, they are few, and
    re-learning them costs far more than the space they take.

    ``max_archived`` caps one pass. The automatic caller sets it: a store that
    has been growing for months is suddenly hundreds over budget the first time
    this runs, and moving hundreds of the user's memories aside inside one turn
    is not a housekeeping step, it is an event. Converging a few per pass keeps
    it visible and reversible; ``/memory`` can run an uncapped pass on request.
    """
    limit = int(MEMORY_BUDGET if limit is None else limit)
    if limit <= 0:
        return []
    entries = list_memories()
    if len(entries) <= limit:
        return []
    now = time.time()
    candidates = [e for e in entries if e.get("type") not in PROTECTED_TYPES]
    protected = len(entries) - len(candidates)
    # An unmeetable budget must not be met by emptying everything else. With
    # more protected entries than the whole limit, archiving every project fact
    # in the store still would not reach it — so the store would be stripped of
    # what it knows about the work and the budget would be missed anyway.
    # Measured, before this check: budget 3 against 6 protected entries
    # archived all 4 project entries and still reported 6 live.
    if protected >= limit:
        return []
    excess = len(entries) - limit
    if excess <= 0 or not candidates:
        return []
    candidates.sort(key=lambda e: eviction_score(e, now))
    take = min(excess, len(candidates))
    if max_archived and max_archived > 0:
        take = min(take, int(max_archived))
    chosen = candidates[:take]
    if dry_run:
        return chosen
    archived = []
    for entry in chosen:
        ok, _ = archive_memory(
            entry["name"],
            f"over budget ({len(entries)} > {limit}; {protected} protected)")
        if ok:
            archived.append(entry)
    return archived


def enforce_global_budget(limit: int = None, *, dry_run: bool = False,
                          max_archived: int = 0) -> list[dict]:
    """The budget across every scope on disk, not just the visible one.

    Same score, same protections, same archive. Entries belonging to other
    projects are judged only on what is true of them regardless of where you
    are standing: how important they were thought to be, when they were last
    used, and how often.
    """
    limit = int(MEMORY_GLOBAL_BUDGET if limit is None else limit)
    if limit <= 0:
        return []
    entries = list_memories(all_scopes=True)
    excess = len(entries) - limit
    if excess <= 0:
        return []
    now = time.time()
    candidates = [e for e in entries if e.get("type") not in PROTECTED_TYPES]
    if not candidates:
        return []
    candidates.sort(key=lambda e: eviction_score(e, now))
    take = min(excess, len(candidates))
    if max_archived and max_archived > 0:
        take = min(take, int(max_archived))
    chosen = candidates[:take]
    if dry_run:
        return chosen
    archived = []
    for entry in chosen:
        ok, _ = archive_memory(
            entry["name"], f"over global budget ({len(entries)} > {limit})")
        if ok:
            archived.append(entry)
    return archived


def load_all_for_prompt(max_per_type: int = 8) -> str:
    """Load persistent memories formatted for the AI system prompt.

    Context-frugal by design: the model sees only the CATEGORY + one-line SUMMARY
    (description) of each memory — never the full body — so this stays small even
    as bodies grow. The agent expands any entry on demand via the ``mem.read``
    tool. Grouped by category, capped at ``max_per_type`` per category.
    """
    ensure_memory_dir()
    memories = list_memories()
    if not memories:
        return ""

    by_type: dict[str, list] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)

    sections = []
    type_labels = {
        "user": "USER PROFILE",
        "feedback": "FEEDBACK & PREFERENCES",
        "project": "PROJECT CONTEXT",
        "structure": "PROJECT STRUCTURE",
        "reference": "REFERENCES",
    }

    for mem_type, label in type_labels.items():
        entries = sorted(
            by_type.get(mem_type, []),
            key=lambda entry: (entry.get("importance", 0.5), entry.get("mtime", 0)),
            reverse=True,
        )[:max_per_type]
        if not entries:
            continue
        lines = [f"[{label}] ({CATEGORY_LABELS.get(mem_type, mem_type)})"]
        for entry in entries:
            scope_tag = scope_label(entry.get("scope"))
            summary = (entry.get("description") or "").strip() or "(no summary)"
            # A stale entry is shown, never hidden: the model has to know both
            # that the claim exists and that it is no longer verified, or it
            # will act on it with unearned confidence.
            flag = (" [STALE — source changed since this was written; "
                    "re-check before relying on it]"
                    if entry.get("status") == STATUS_STALE else "")
            lines.append(f"  {entry['name']} [{scope_tag}] — {summary}{flag}")
        sections.append('\n'.join(lines))

    if not sections:
        return ""
    sections.append("(Only summaries are shown. Use the mem.read tool with a "
                    "memory's name to load its full content when needed.)")
    return '\n\n'.join(sections)


# ── Integration with agent_loop ─────────────────────────────────────────

def get_memory_context() -> str:
    """Called by agent_loop to get memory context for the prompt.

    Shortcut that loads all memories and returns formatted text.
    """
    ctx = load_all_for_prompt(max_per_type=5)
    return ctx or "(no persistent memories)"
