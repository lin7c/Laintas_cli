# SPDX-License-Identifier: MIT
# Copyright (c) 2026 AI-PoW contributors
"""AI-PoW 0.3: dependency-free process scoring and local provenance.

This file is also vendored into laintas-cli. No daemon, network or model calls.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from contextlib import contextmanager
from decimal import Decimal, localcontext
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

VERSION = "0.1.0"
EMPTY_HASH = "0" * 64
APP_VERSION = "0.8.0"
ZERO = "0" * 64
MAX_EVENT = 16 * 1024
MAX_FILES = 2000
MAX_FILE = 256 * 1024
SCAN_BYTES = 16 * 1024 * 1024
MAX_GIT_OUTPUT = 2 * 1024 * 1024
TYPES = {"human.message", "assistant.visible", "model.usage", "tool.call",
         "tool.result", "agent.spawn", "agent.stop", "skill.used", "task.change",
         "run.start", "run.stop", "file.observed", "coverage.gap"}
EXCLUDED = {".git", ".ai-pow", ".laintas", ".claude", ".codex", "node_modules",
            "vendor", ".venv", "venv", "dist", "build", "__pycache__"}


def canonical(value):
    """Protocol JSON: UTF-8, sorted keys, no whitespace, no floats."""
    def check(v):
        if v is None or isinstance(v, (str, bool)):
            return
        if type(v) is int and abs(v) <= 2**53 - 1:
            return
        if isinstance(v, list):
            for item in v:
                check(item)
            return
        if isinstance(v, dict) and all(isinstance(k, str) for k in v):
            for item in v.values():
                check(item)
            return
        raise ValueError("Protocol values must be strings, safe integers, booleans or null")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text_meta(text):
    # No tokenizer is falsely presented as a provider's tokenizer.
    text = str(text or "")
    raw = text.encode("utf-8")
    return {"bytes": len(raw), "characters": len(text),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "tokens": (len(raw) + 3) // 4, "token_method": "utf8-bytes/4-estimate"}


def git(root, *args, limit=MAX_GIT_OUTPUT, allow_fail=False):
    # Spool-free bounded reader; kill and reap even on excessive output/timeouts.
    proc = subprocess.Popen(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    timer = threading.Timer(10, proc.kill)
    timer.daemon = True
    timer.start()
    try:
        data = proc.stdout.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Git output exceeds recorder limit")
        code = proc.wait()
        if code and not allow_fail:
            raise ValueError("Git command failed: " + args[0])
        return data if code == 0 else None
    finally:
        timer.cancel()
        timer.join()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def repo_paths(cwd):
    root = Path(git(cwd, "rev-parse", "--show-toplevel").decode().strip())
    gd = Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip())
    return root, gd / "ai-pow"


def head(root):
    raw = git(root, "rev-parse", "--verify", "HEAD", allow_fail=True)
    return raw.decode().strip() if raw else None


def commit_meta(root, ref):
    raw = git(root, "show", "-s", "--format=%H%n%T%n%P", ref).decode().splitlines()
    return {"commit": raw[0], "tree": raw[1], "parents": raw[2].split() if len(raw) > 2 else []}


def validate_event(kind, data):
    if kind not in TYPES or not isinstance(data, dict):
        raise ValueError("Unknown event type or invalid data")
    if len(canonical(data)) > MAX_EVENT - 1024:
        raise ValueError("Event exceeds 16 KiB limit")
    for key in ("tokens", "input_tokens", "output_tokens", "cached_input_tokens",
                "reasoning_tokens", "cache_write_tokens", "bytes", "characters"):
        if key in data and data[key] is not None:
            if type(data[key]) is not int or data[key] < 0:
                raise ValueError("Invalid non-negative token count: " + key)
    if kind in {"human.message", "assistant.visible"}:
        method = data.get("token_method")
        if method is not None and (not isinstance(method, str) or not method):
            raise ValueError("token_method must name a tokenizer or estimation method")
        if method == "utf8-bytes/4-estimate":
            if type(data.get("bytes")) is not int or data.get("tokens") != (data["bytes"] + 3) // 4:
                raise ValueError("Byte-based token estimate is inconsistent")
    if kind == "task.change" and "status" in data:
        if data["status"] not in {"active", "completed", "dropped"} or not isinstance(data.get("task_id"), str) or not 1 <= len(data["task_id"]) <= 256:
            raise ValueError("A task needs a stable task_id and active/completed/dropped status")
        if not isinstance(data.get("evidence", []), list) or len(data.get("evidence", [])) > 32 or any(not isinstance(p, str) or len(p) != 64 or any(c not in "0123456789abcdef" for c in p) for p in data.get("evidence", [])):
            raise ValueError("Task evidence must contain SHA-256 path identifiers")
    if kind == "file.observed" and "units_before" in data:
        for key in ("units_before", "units_after"):
            units = data.get(key)
            if not isinstance(units, list) or len(units) > 64 or any(not isinstance(p, str) or len(p) != 64 or any(c not in "0123456789abcdef" for c in p) for p in units):
                raise ValueError("Artifact units must be bounded SHA-256 identifiers")
    if kind == "model.usage":
        if not data.get("model") or data.get("measurement") not in {"provider_reported", "estimated"}:
            raise ValueError("Model usage needs model and measurement")
        i, c = data.get("input_tokens"), data.get("cached_input_tokens")
        o, r = data.get("output_tokens"), data.get("reasoning_tokens")
        if i is not None and c is not None and c > i:
            raise ValueError("Cache read tokens must be a subset of input_tokens")
        if o is not None and r is not None and r > o:
            raise ValueError("Reasoning must be a subset of output_tokens")
        for key in ("reference_usd", "actual_usd"):
            if data.get(key) is not None:
                cost = Decimal(data[key])
                if not isinstance(data[key], str) or not cost.is_finite() or cost < 0:
                    raise ValueError("Costs must be finite non-negative decimal strings")
        if data.get("reference_usd") is not None and not data.get("price_snapshot"):
            raise ValueError("Reference cost requires an explicit price snapshot")
        if data.get("reference_usd") is not None:
            price = data["price_snapshot"]
            expected = price_usage({k: v for k, v in data.items() if k != "reference_usd"}, price)
            if (price["model"] != data["model"] or data.get("price_snapshot_hash") != digest(price)
                    or expected.get("reference_usd") != data["reference_usd"]):
                raise ValueError("Reference cost does not match usage and price snapshot")


def validate_price(price):
    required = ("model", "source_url", "effective_date", "input_per_million",
                "cached_input_per_million", "cache_write_per_million", "output_per_million")
    if not isinstance(price, dict) or any(k not in price for k in required):
        raise ValueError("Price snapshot missing required fields")
    canonical(price)
    if not str(price["source_url"]).startswith("https://"):
        raise ValueError("Price source must be an HTTPS URL")
    for k in required[3:]:
        value = Decimal(price[k])
        if not isinstance(price[k], str) or not value.is_finite() or value < 0:
            raise ValueError("Price rates must be non-negative decimal strings")


def price_usage(data, price):
    validate_price(price)
    fields = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
    if any(data.get(k) is None for k in fields):
        return data
    i, c, w, o = (data[k] for k in fields)
    with localcontext() as ctx:
        ctx.prec = 80
        total = ((i - c) * Decimal(price["input_per_million"])
                 + c * Decimal(price["cached_input_per_million"])
                 + w * Decimal(price["cache_write_per_million"])
                 + o * Decimal(price["output_per_million"])) / Decimal(1000000)
    return {**data, "reference_usd": str(total), "price_snapshot": price,
            "price_snapshot_hash": digest(price)}


def _summary_v1(events):
    counts, models = {}, {}
    human = {"messages": 0, "tokens_estimated": 0, "tokens_measured": 0}
    visible = {"events": 0, "tokens_estimated": 0, "tokens_measured": 0}
    tools, skills, mcps = set(), set(), set()
    total_cost, priced = Decimal(0), 0
    for e in events:
        k, d = e["type"], e["data"]
        counts[k] = counts.get(k, 0) + 1
        if k in {"human.message", "assistant.visible"}:
            dest = human if k == "human.message" else visible
            dest["messages" if k == "human.message" else "events"] += 1
            field = "tokens_estimated" if "estimate" in (d.get("token_method") or "estimate") else "tokens_measured"
            dest[field] += d.get("tokens") or 0
        if k == "tool.call":
            tools.add(d.get("name", "unknown"))
            if d.get("mcp_server"):
                mcps.add(d["mcp_server"])
        if k == "skill.used":
            skills.add(d.get("name", "unknown"))
        if k == "model.usage":
            # Do not combine provider-reported and estimated token totals.
            key = d["model"] + "/" + d["measurement"]
            m = models.setdefault(key, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                        "cached_input_tokens": 0, "reasoning_tokens": 0,
                                        "cache_write_tokens": 0, "unknown_fields": []})
            m["calls"] += 1
            for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "cache_write_tokens"):
                if d.get(field) is None:
                    if field not in m["unknown_fields"]:
                        m["unknown_fields"].append(field)
                else:
                    m[field] += d[field]
            if d.get("reference_usd") is not None:
                priced += 1
                total_cost += Decimal(d["reference_usd"])
    return {"human": human, "visible_ai": visible, "models": models,
            "reference_usd_known_subtotal": str(total_cost) if priced else None,
            "priced_calls": priced, "unpriced_calls": counts.get("model.usage", 0) - priced,
            "tools_used": sorted(tools), "skills_used": sorted(skills), "mcp_used": sorted(mcps),
            "event_counts": counts,
            "human_survival": None, "artifact_survival": None, "task_survival": None,
            "semantic_edits": None, "quality": None}


MAX_AGENT_NODES = 128
MAX_TOOL_NAMES = 32


def _agent_block(agents, tools):
    """Reconstruct the observed agent structure; unknown parents stay unknown."""
    parent, order = agents["parent"], agents["order"]
    known, depths = set(order), {}
    for start in order:
        node, chain, seen = start, [], set()
        # Walk up to a root, an already-measured ancestor, or a cycle guard.
        while node in known and node not in depths and node not in seen:
            seen.add(node)
            chain.append(node)
            node = parent.get(node) or ""
        level = depths.get(node, 0)
        for item in reversed(chain):
            level += 1
            depths[item] = level
    ranked = sorted(tools.items(), key=lambda item: (-item[1], item[0]))
    return {"spawns": agents["spawns"], "stops": agents["stops"], "nodes": len(order),
            "parents_known": sum(1 for node in order if parent.get(node)),
            "max_depth": max((depths[node] for node in order), default=0),
            "graph": [[node, parent.get(node) or ""] for node in order],
            "tool_calls_by_name": [[name, calls] for name, calls in ranked[:MAX_TOOL_NAMES]],
            "other_tool_calls": sum(calls for _, calls in ranked[MAX_TOOL_NAMES:]),
            "truncated": agents["truncated"] or len(ranked) > MAX_TOOL_NAMES}


MAX_SESSIONS = 512


def summary(events, algorithm="observed-v4"):
    """Keep unknown totals null and make byte estimates stream-chunk invariant.

    Retain the original reducers for verification of proofs created before the
    accounting review. A new report never silently rewrites an old proof.
    """
    if algorithm == "observed-v1":
        with localcontext() as ctx:
            ctx.prec = 28
            return _summary_v1(events)
    if algorithm not in {"observed-v2", "observed-v3", "observed-v4"}:
        raise ValueError("Unsupported summary algorithm")
    agents = {"parent": {}, "order": [], "spawns": 0, "stops": 0, "truncated": False}
    tool_names = {}
    window = {"first_ms": None, "last_ms": None}
    activity = {"runs": 0, "run_stops": 0, "failed_tool_calls": 0, "gaps": 0,
                "nonzero_exits": 0, "sessions": set(), "truncated": False}
    paid = {"total": Decimal(0), "calls": 0}
    text = {kind: {"events": 0, "methods": {}, "unknown_token_events": 0}
            for kind in ("human.message", "assistant.visible")}
    missing, prices, calls = {}, {}, {}
    def observed():
        for event in events:
            kind, data = event["type"], event["data"]
            if kind in text:
                dest = text[kind]
                dest["events"] += 1
                method = data.get("token_method")
                tokens = data.get("tokens")
                if not isinstance(method, str) or not method or tokens is None:
                    dest["unknown_token_events"] += 1
                else:
                    bucket = dest["methods"].setdefault(method, {"tokens": 0, "bytes": 0, "events": 0})
                    bucket["events"] += 1
                    if method == "utf8-bytes/4-estimate" and type(data.get("bytes")) is int:
                        bucket["bytes"] += data["bytes"]
                        bucket["tokens"] = (bucket["bytes"] + 3) // 4
                    else:
                        bucket["tokens"] += tokens
            when = event.get("time_ms")
            if type(when) is int:
                window["first_ms"] = when if window["first_ms"] is None else min(window["first_ms"], when)
                window["last_ms"] = when if window["last_ms"] is None else max(window["last_ms"], when)
            # Session and run ids are separate namespaces; never pool them.
            session = data.get("session_id")
            if isinstance(session, str) and session:
                if len(activity["sessions"]) < MAX_SESSIONS:
                    activity["sessions"].add(session)
                elif session not in activity["sessions"]:
                    activity["truncated"] = True
            if kind == "run.start":
                activity["runs"] += 1
            elif kind == "run.stop":
                activity["run_stops"] += 1
                activity["nonzero_exits"] += int(bool(data.get("exit_code")))
            elif kind == "coverage.gap":
                activity["gaps"] += 1
            elif kind == "tool.result" and data.get("ok") is False:
                activity["failed_tool_calls"] += 1
            if kind == "tool.call":
                name = str(data.get("name", "unknown"))[:128]
                tool_names[name] = tool_names.get(name, 0) + 1
            if kind in {"agent.spawn", "agent.stop"}:
                agents["spawns" if kind == "agent.spawn" else "stops"] += 1
                node = data.get("agent_id")
                if kind == "agent.spawn" and isinstance(node, str) and node:
                    if node in agents["parent"] or len(agents["order"]) >= MAX_AGENT_NODES:
                        agents["truncated"] = agents["truncated"] or node not in agents["parent"]
                    else:
                        agents["order"].append(node)
                        origin = data.get("parent_agent_id")
                        agents["parent"][node] = origin if isinstance(origin, str) and origin and origin != "unknown" else ""
            if kind == "model.usage":
                key = data["model"] + "/" + data["measurement"]
                counts = missing.setdefault(key, {})
                for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "cache_write_tokens"):
                    counts[field] = counts.get(field, 0) + int(data.get(field) is None)
                measurement = data["measurement"]
                priced = data.get("reference_usd") is not None
                counts = calls.setdefault(measurement, {"priced": 0, "unpriced": 0})
                counts["priced" if priced else "unpriced"] += 1
                if priced:
                    prices[measurement] = prices.get(measurement, Decimal(0)) + Decimal(data["reference_usd"])
                # What was actually paid is a separate fact from the list price.
                if data.get("actual_usd") is not None:
                    paid["total"] += Decimal(data["actual_usd"])
                    paid["calls"] += 1
            yield event
    with localcontext() as ctx:
        ctx.prec = 80
        result = _summary_v1(observed())
    for kind, label in (("human.message", "human"), ("assistant.visible", "visible_ai")):
        dest = text[kind]
        dest["messages" if kind == "human.message" else "events"] = dest["events"]
        estimated = sum(v["tokens"] for k, v in dest["methods"].items() if "estimate" in k)
        measured = sum(v["tokens"] for k, v in dest["methods"].items() if "estimate" not in k)
        dest.update(tokens_estimated=estimated, tokens_measured=measured,
                    tokens_complete=dest["unknown_token_events"] == 0,
                    attention_seconds=None)
        result[label] = dest
    for key, counts in missing.items():
        model = result["models"][key]
        model["known_token_subtotals"] = {field: model[field] for field in counts}
        model["missing_field_calls"] = counts
        for field, count in counts.items():
            if count:
                model[field] = None
    result["algorithm"] = algorithm
    result["reference_usd_by_measurement"] = {
        measurement: {"known_subtotal": format(prices[measurement], "f") if measurement in prices else None,
                      "complete_for_observed_calls": counts["unpriced"] == 0, **counts}
        for measurement, counts in calls.items()}
    result["reference_cost_complete_for_observed_calls"] = bool(calls) and result["unpriced_calls"] == 0
    result["overall_score"] = None
    result["efficiency"] = None
    result["ranking_eligible"] = False
    if algorithm in {"observed-v3", "observed-v4"}:
        result["agent"] = _agent_block(agents, tool_names)
    if algorithm == "observed-v4":
        first, last = window["first_ms"], window["last_ms"]
        result["window"] = {"first_ms": first, "last_ms": last,
                            "span_ms": None if first is None else last - first,
                            "basis": "local-observation-times"}
        result["activity"] = {"runs": activity["runs"], "run_stops": activity["run_stops"],
                              "nonzero_exits": activity["nonzero_exits"],
                              "sessions": len(activity["sessions"]),
                              "sessions_truncated": activity["truncated"],
                              "failed_tool_calls": activity["failed_tool_calls"],
                              "coverage_gaps": activity["gaps"]}
        result["actual_usd_known_subtotal"] = format(paid["total"], "f") if paid["calls"] else None
        result["actual_priced_calls"] = paid["calls"]
    return result


class Recorder:
    def __init__(self, cwd="."):
        self.root, self.directory = repo_paths(cwd)
        self.database = self.directory / "events.sqlite3"
        if self.directory.is_symlink() or self.database.is_symlink():
            raise ValueError("Recorder storage must not be a symlink")

    @contextmanager
    def connection(self):
        if not self.database.is_file():
            raise ValueError("AI-PoW is not initialized; run aipow init")
        db = sqlite3.connect(self.database, timeout=1.0)
        try:
            # DELETE journal has bounded transient size and no indefinitely growing WAL.
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            limit = db.execute("SELECT value FROM meta WHERE key='max_bytes'").fetchone()
            if limit:
                pagesize = db.execute("PRAGMA page_size").fetchone()[0]
                db.execute("PRAGMA max_page_count=" + str(int(limit[0]) // pagesize))
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def init(self, max_mib=64):
        if not 4 <= max_mib <= 4096:
            raise ValueError("Quota must be between 4 and 4096 MiB")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database.exists():
            return {"initialized": True, "existing": True, "storage": str(self.directory)}
        fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.database)
        try:
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE events(seq INTEGER PRIMARY KEY, epoch TEXT NOT NULL,
                  event_id TEXT UNIQUE NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX event_epoch ON events(epoch, seq);
                CREATE TABLE proofs(commit_id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE iterations(seq INTEGER PRIMARY KEY, commit_id TEXT UNIQUE NOT NULL,
                  hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE files(path TEXT PRIMARY KEY, hash TEXT NOT NULL, signature TEXT NOT NULL);
            """)
            for k, v in {"version": VERSION, "max_bytes": str(max_mib * 1024**2),
                         "epoch": uuid.uuid4().hex, "base": head(self.root) or "",
                         "started_ms": str(time.time_ns() // 1000000), "last_hash": ZERO,
                         "sampled": "0"}.items():
                db.execute("INSERT INTO meta VALUES (?,?)", (k, v))
            db.commit()
        finally:
            db.close()
        self.sample()
        return {"initialized": True, "storage": str(self.directory), "quota_mib": max_mib}

    @staticmethod
    def get(db, key):
        return db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value<>excluded.value", (key, str(value)))

    def _append(self, db, kind, data, source="generic", event_id=None):
        validate_event(kind, data)
        event_id = event_id or uuid.uuid4().hex
        if len(event_id) > 256 or len(source) > 128:
            raise ValueError("Event identity too long")
        old = db.execute("SELECT body FROM events WHERE event_id=?", (event_id,)).fetchone()
        if old:
            previous = json.loads(old[0])
            if previous["type"] != kind or previous["data"] != data or previous["source"] != source:
                raise ValueError("Conflicting duplicate event_id")
            return False
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        body = {"v": VERSION, "seq": seq, "epoch": self.get(db, "epoch"),
                "event_id": event_id, "time_ms": time.time_ns() // 1000000,
                "previous": self.get(db, "last_hash"), "type": kind,
                "source": source, "data": data}
        h = digest(body)
        db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                   (seq, body["epoch"], event_id, h, canonical(body).decode()))
        self.put(db, "last_hash", h)
        return True

    def record(self, kind, data, source="generic", event_id=None):
        event_id = event_id or uuid.uuid4().hex
        for attempt in range(3):
            try:
                return self._record_once(kind, data, source, event_id)
            except sqlite3.OperationalError as exc:
                if attempt == 2 or not any(word in str(exc).lower() for word in ("locked", "busy")):
                    raise
                time.sleep(.02 * (attempt + 1))

    def _record_once(self, kind, data, source, event_id):
        with self.connection() as db:
            self._boundary(db, automatic=True)
            if kind == "model.usage":
                validate_event(kind, data)
                row = db.execute("SELECT value FROM meta WHERE key=?", ("price:" + data["model"],)).fetchone()
                if row and data.get("reference_usd") is None:
                    data = price_usage(data, json.loads(row[0]))
            return self._append(db, kind, data, source, event_id)

    def set_price(self, price):
        validate_price(price)
        if len(canonical(price)) > 4096:
            raise ValueError("Price snapshot exceeds 4 KiB")
        with self.connection() as db:
            self.put(db, "price:" + price["model"], canonical(price).decode())
        return {"model": price["model"], "price_snapshot_hash": digest(price)}

    def set_quota(self, max_mib):
        if not 4 <= max_mib <= 4096:
            raise ValueError("Quota must be between 4 and 4096 MiB")
        if max_mib * 1024**2 < self.database.stat().st_size:
            raise ValueError("Quota cannot be smaller than existing database")
        with self.connection() as db:
            self.put(db, "max_bytes", max_mib * 1024**2)
        return {"quota_mib": max_mib, "health_marker": "retained; prior lost events cannot be recovered"}

    def import_claude_usage(self, path, start_at_end=False):
        """Incremental metadata-only import; never import historic prompts or tool payloads.

        Transcript format is an adapter, not part of the protocol. Model messages
        are deduplicated by provider message id; changed replays mark a gap.
        """
        path = Path(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream, self.connection() as db:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Transcript must be a regular file")
            key = "claude-cursor:" + hashlib.sha256(os.fsencode(path.absolute())).hexdigest()
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            identity = f"{info.st_dev}:{info.st_ino}"
            state = json.loads(row[0]) if row else {"offset": 0, "identity": identity}
            if start_at_end and not row:
                self.put(db, key, canonical({"offset": info.st_size, "identity": identity}).decode())
                return {"imported_calls": 0, "baseline": True}
            self._boundary(db, automatic=True)
            if state["identity"] != identity or state["offset"] > info.st_size:
                self._append(db, "coverage.gap", {"reason": "transcript_rotated_or_truncated"}, "claude-transcript")
                state = {"offset": 0, "identity": identity}
            stream.seek(state["offset"])
            used, imported, pending = 0, 0, {}
            while used < 8 * 1024 * 1024:
                offset = stream.tell()
                line = stream.readline(1024 * 1024 + 1)
                if not line:
                    break
                used += len(line)
                if len(line) > 1024 * 1024:
                    raise ValueError("Transcript row exceeds 1 MiB")
                if not line.endswith(b"\n"):
                    stream.seek(offset)
                    break
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    self._append(db, "coverage.gap", {"reason": "malformed_transcript_row"}, "claude-transcript")
                    continue
                if not isinstance(row, dict) or row.get("type") != "assistant":
                    continue
                message = row.get("message") or {}
                usage = message.get("usage") or {}
                message_id = message.get("id")
                if not usage or not message_id:
                    continue
                try:
                    timestamp = int(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
                except (ValueError, KeyError, TypeError):
                    self._append(db, "coverage.gap", {"reason": "transcript_missing_timestamp"}, "claude-transcript")
                    continue
                if timestamp < int(self.get(db, "started_ms")):
                    continue
                ci = usage.get("cache_read_input_tokens")
                fresh = usage.get("input_tokens")
                data = {"model": str(message.get("model") or "unknown")[:128],
                        "input_tokens": fresh + ci if type(fresh) is int and type(ci) is int else None,
                        "cached_input_tokens": ci, "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                        "output_tokens": usage.get("output_tokens"), "reasoning_tokens": None,
                        "measurement": "provider_reported", "observed_from": "local-transcript",
                        "session_id": str(row.get("sessionId", ""))[:128]}
                event_id = "claude-usage:" + digest([data["session_id"], message_id])
                pending[event_id] = data
            for event_id, data in pending.items():
                prior = db.execute("SELECT body FROM events WHERE event_id=?", (event_id,)).fetchone()
                if prior:
                    old = json.loads(prior[0])["data"]
                    if any(old.get(k) != v for k, v in data.items()):
                        self._append(db, "coverage.gap", {"reason": "transcript_usage_changed", "event_id": event_id}, "claude-transcript")
                    continue
                validate_event("model.usage", data)
                price = db.execute("SELECT value FROM meta WHERE key=?", ("price:" + data["model"],)).fetchone()
                if price:
                    data = price_usage(data, json.loads(price[0]))
                self._append(db, "model.usage", data, "claude-transcript", event_id)
                imported += 1
            state["offset"] = stream.tell()
            self.put(db, key, canonical(state).decode())
            return {"imported_calls": imported, "bytes_read": used, "more": stream.tell() < info.st_size}

    def _events(self, db, epoch):
        for body, h in db.execute("SELECT body,hash FROM events WHERE epoch=? ORDER BY seq", (epoch,)):
            yield {**json.loads(body), "hash": h}

    def _boundary(self, db, automatic=False):
        current = head(self.root)
        base = self.get(db, "base") or None
        if current == base:
            return None
        # Rebase/amend/checkout/missed commits cannot be assigned to a fabricated interval.
        meta = commit_meta(self.root, current) if current else None
        linear = meta and ((base and meta["parents"][:1] == [base]) or (not base and not meta["parents"]))
        if not linear:
            raise ValueError("HEAD moved outside the observed parent interval; run aipow reset-boundary")
        epoch = self.get(db, "epoch")
        first = db.execute("SELECT seq,body FROM events WHERE epoch=? ORDER BY seq LIMIT 1", (epoch,)).fetchone()
        last = db.execute("SELECT seq,hash FROM events WHERE epoch=? ORDER BY seq DESC LIMIT 1", (epoch,)).fetchone()
        p = {"v": VERSION, **meta, "base": base, "epoch": epoch,
             "first_seq": first[0] if first else None, "last_seq": last[0] if last else None,
             "anchor": json.loads(first[1])["previous"] if first else self.get(db, "last_hash"),
             "trace_root": last[1] if last else self.get(db, "last_hash"),
             "summary": summary(self._events(db, epoch)),
             "trust": "local-self-reported", "capture": "partial",
             "boundary": "head-observed-after-commit" if automatic else "post-commit",
             "limits": {"max_file_bytes": MAX_FILE, "max_files": MAX_FILES,
                        "max_scan_bytes": SCAN_BYTES},
             "caveats": ["Hashes verify consistency, not truth or completeness",
                         "File observations are sampled; intermediate writes may be missed",
                         "Events are interval-associated, not causally attributed to staged changes",
                         "Worktrees are independent; imported work is not re-counted"]}
        p["score"] = self.evaluate(db, p)
        p["proof_hash"] = digest(p)
        db.execute("INSERT INTO proofs VALUES (?,?)", (current, canonical(p).decode()))
        # One transaction: a proof never exists without its iteration row.
        self._append_iteration(db, p)
        self.put(db, "base", current)
        self.put(db, "epoch", uuid.uuid4().hex)
        # Snapshot remains the last observed working state; commit does not erase unstaged work.
        return p

    def seal(self):
        with self.connection() as db:
            p = self._boundary(db)
            if not p:
                old = db.execute("SELECT body FROM proofs WHERE commit_id=?", (head(self.root),)).fetchone()
                if not old:
                    raise ValueError("No new commit to seal (the starting commit predates recording)")
                p = json.loads(old[0])
        # Report failures do not roll back a valid proof or the user's commit.
        try:
            path = self.html_report(p["commit"])
            self.html_index(p["commit"])
            print("AI-PoW " + (p.get("score") or {}).get("value", "unscored") + " | " + str(path), file=sys.stderr)
        except Exception as exc:
            record_error(self, exc)
            print("AI-PoW: proof sealed; HTML report could not be written", file=sys.stderr)
        return p

    def evaluate(self, db, proof):
        return evaluate_score(self.root, proof, self._events(db, proof["epoch"]))

    def diff_stats(self, proof):
        """Derived from Git, not from the proof: what the commit itself changed."""
        try:
            args = ("diff", "--shortstat", proof["base"], proof["commit"]) if proof["base"] else (
                "show", "--shortstat", "--format=", proof["commit"])
            text = git(self.root, *args).decode("utf-8", "replace")
        except Exception:
            return None
        numbers = {}
        for field, word in (("files", "file"), ("insertions", "insertion"), ("deletions", "deletion")):
            match = re.search(r"(\d+) " + word, text)
            numbers[field] = int(match.group(1)) if match else 0
        return numbers

    def _ledger(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS iterations(seq INTEGER PRIMARY KEY, commit_id TEXT UNIQUE"
                   " NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL)")

    def _last_iteration(self, db):
        self._ledger(db)
        row = db.execute("SELECT body,hash FROM iterations ORDER BY seq DESC LIMIT 1").fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, EMPTY_HASH)

    def _append_iteration(self, db, proof):
        """Write T = T-1 + this commit. The previous row is the only history this needs."""
        from ai_pow_scoring import ITERATION_ALGORITHM, iteration_contribution, iteration_step
        previous, previous_hash = self._last_iteration(db)
        base = proof.get("base")
        continues = previous is None or previous["commit"] == base
        body = {"v": VERSION, "algorithm": ITERATION_ALGORITHM,
                "seq": (previous["seq"] + 1) if previous else 1,
                "commit": proof["commit"], "parent": base, "at": proof["summary"].get("window", {}).get("last_ms"),
                "previous": previous_hash, "continues": bool(continues),
                "contribution": iteration_contribution(proof)}
        body["cumulative"] = iteration_step(previous if continues else previous, body["contribution"])
        body["hash"] = digest({k: v for k, v in body.items() if k != "hash"})
        db.execute("INSERT OR REPLACE INTO iterations(commit_id,hash,body) VALUES (?,?,?)",
                   (proof["commit"], body["hash"], canonical(body).decode()))
        return body

    def rebuild_iterations(self, ref="HEAD"):
        """Recompute the chain from the sealed proofs, oldest first."""
        head_commit = git(self.root, "rev-parse", ref).decode().strip()
        lineage = git(self.root, "rev-list", "--first-parent", head_commit).decode().splitlines()
        written = 0
        with self.connection() as db:
            self._ledger(db)
            db.execute("DELETE FROM iterations")
            for commit in reversed(lineage):
                row = db.execute("SELECT body FROM proofs WHERE commit_id=?", (commit,)).fetchone()
                if not row:
                    continue  # A commit without a sealed proof contributed nothing observable.
                self._append_iteration(db, json.loads(row[0]))
                written += 1
        return {"rebuilt": written, "commits_in_lineage": len(lineage)}

    def iterations(self, db, ref="HEAD", limit=60):
        """The newest rows of the chain, with the linkage checked over that window."""
        self._ledger(db)
        rows = [json.loads(body) for body, in
                db.execute("SELECT body FROM iterations ORDER BY seq DESC LIMIT ?", (limit,))]
        verified = True
        for index, body in enumerate(rows):
            stored = dict(body)
            recorded = stored.pop("hash")
            if digest(stored) != recorded:
                verified = False
            if index + 1 < len(rows) and body["previous"] != rows[index + 1]["hash"]:
                verified = False
        return rows, verified

    def commit_meta_row(self, commit):
        subject = git(self.root, "show", "-s", "--format=%s%n%cI", commit).decode("utf-8", "replace").splitlines()
        return {"commit": commit, "title": subject[0][:240], "date": subject[-1]}

    def report_data(self, ref="HEAD"):
        """One commit: identity, the proof vector, and derived detail for it."""
        from ai_pow_scoring import ALGORITHM, score
        current = self.proof(ref)
        verification = self.verify(ref)
        with self.connection() as db:
            inputs = score_inputs(self.root, current, self._events(db, current["epoch"]))
            if not current.get("score"):
                current = {**current, "score": score(inputs["evidence"], current["summary"], ALGORITHM)}
            facts = derived_facts(self.root, current, inputs, self._events(db, current["epoch"]))
        parents = current.get("parents") or []
        baseline = None
        with self.connection() as db:
            self._ledger(db)
            row = db.execute("SELECT body FROM iterations WHERE commit_id=?", (current["commit"],)).fetchone()
            chain = json.loads(row[0]) if row else None
            if chain and chain["seq"] > 1:
                before = db.execute("SELECT body FROM iterations WHERE seq=?", (chain["seq"] - 1,)).fetchone()
                if before:
                    from ai_pow_scoring import cumulative_view
                    body = json.loads(before[0])
                    baseline = {"commit": body["commit"], "commits": body["cumulative"].get("commits"),
                                "totals": cumulative_view(body["cumulative"])}
            trend = [json.loads(body)["contribution"] for body, in db.execute(
                "SELECT body FROM iterations ORDER BY seq DESC LIMIT 12")][::-1]
        return {"project": self.root.name, "kind": "commit", "demo": False,
                "baseline": baseline, "trend": trend,
                "current": {**self.commit_meta_row(current["commit"]), "score": current.get("score"),
                            "summary": current["summary"], "proof_hash": current["proof_hash"],
                            "parent": parents[0] if parents else None, "tree": current.get("tree"),
                            "trace_root": current.get("trace_root"), "boundary": current.get("boundary")},
                "verification": verification, "derived": facts, "diff": self.diff_stats(current)}

    def index_data(self, ref="HEAD", rows=60):
        """The repository, read from the iteration chain rather than replayed."""
        from ai_pow_scoring import cumulative_view, history_stats
        head_commit = git(self.root, "rev-parse", ref).decode().strip()
        lineage = git(self.root, "rev-list", "--first-parent", head_commit).decode().splitlines()
        with self.connection() as db:
            self._ledger(db)
            known = db.execute("SELECT 1 FROM iterations WHERE commit_id=?", (head_commit,)).fetchone()
        if not known:
            # Proofs sealed before the chain existed, or a restored database.
            with self.connection() as db:
                sealed = db.execute("SELECT 1 FROM proofs WHERE commit_id=?", (head_commit,)).fetchone()
            if sealed:
                self.rebuild_iterations(ref)
        with self.connection() as db:
            chain, verified = self.iterations(db, ref, rows)
        history = []
        for body in chain:
            contribution, cumulative = body["contribution"], body["cumulative"]
            history.append({**self.commit_meta_row(body["commit"]),
                            "seq": body["seq"], "continues": body["continues"],
                            "score": contribution.get("score"), "total": cumulative["score_total"],
                            "span_ms": contribution.get("span_ms"),
                            "human_tokens": contribution.get("human_tokens"),
                            "prompts": contribution.get("prompts"),
                            "visible_tokens": contribution.get("visible_tokens"),
                            "awc": contribution.get("reference_usd"),
                            "actual_usd": contribution.get("actual_usd"),
                            "model_calls": contribution.get("model_calls"),
                            "tool_calls": contribution.get("tool_calls"),
                            "failed_tool_calls": contribution.get("failed_tool_calls"),
                            "sub_agents": contribution.get("sub_agents"),
                            "sessions": contribution.get("sessions"),
                            "input_tokens": contribution.get("input_tokens"),
                            "cached_input_tokens": contribution.get("cached_input_tokens"),
                            "output_tokens": contribution.get("output_tokens"),
                            "reasoning_tokens": contribution.get("reasoning_tokens"),
                            "operations": contribution.get("operations"),
                            "retained": contribution.get("retained"),
                            "tasks": contribution.get("task_attempts")})
        latest = chain[0] if chain else None
        totals = cumulative_view(latest["cumulative"]) if latest else cumulative_view({})
        iteration = None
        if latest:
            before = chain[1]["cumulative"]["score_total"] if len(chain) > 1 else "0.0"
            contribution = (latest["contribution"].get("score") or {}).get("value", "0.0")
            iteration = {"algorithm": "commit-sum-v1", "value": totals["score_total"],
                         "previous": before, "delta": contribution,
                         "rated_commits": totals["scored_commits"], "starting_rating": 0}
        scored = [row for row in history if row.get("score")]
        return {"project": self.root.name, "kind": "index", "demo": False,
                "lifetime": totals, "iteration": iteration, "history": history,
                "commits_in_history": len(lineage), "chain_verified": verified,
                "chain_length": (latest["seq"] if latest else 0),
                "statistics": history_stats(scored[0]["score"], scored[1:]) if scored else None,
                "span": {"first": self.commit_meta_row(history[-1]["commit"]) if history else None,
                         "last": self.commit_meta_row(history[0]["commit"]) if history else None}}

    def html_report(self, ref="HEAD", destination=None):
        from ai_pow_report import render
        data = self.report_data(ref)
        data["links"] = {"index": "../index.html"}
        html = render(data).encode("utf-8")
        if len(html) > 8 * 1024 * 1024:
            raise ValueError("HTML report exceeds 8 MiB")
        if destination:
            with open(destination, "xb") as out:
                os.chmod(destination, 0o600)
                out.write(html)
            return Path(destination)
        directory = self.directory / "reports"
        if directory.is_symlink():
            raise ValueError("Report directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (data["current"]["commit"] + ".html")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(html)
        # Only remove this recorder's generated HTML cache, never proofs or exports.
        owned = sorted((p for p in directory.iterdir() if p.suffix == ".html" and len(p.stem) in {40, 64}
                        and all(c in "0123456789abcdef" for c in p.stem) and p.is_file() and not p.is_symlink()),
                       key=lambda p: p.stat().st_mtime_ns, reverse=True)
        size = 0
        for index, cached in enumerate(owned):
            size += cached.stat().st_size
            if cached != path and (index >= 20 or size > 16 * 1024 * 1024):
                cached.unlink()
        return path

    def html_index(self, ref="HEAD", destination=None):
        from ai_pow_report import render_index
        data = self.index_data(ref)
        data["links"] = {"commit": "reports/"}
        html = render_index(data).encode("utf-8")
        if len(html) > 8 * 1024 * 1024:
            raise ValueError("HTML index exceeds 8 MiB")
        if destination:
            with open(destination, "xb") as out:
                os.chmod(destination, 0o600)
                out.write(html)
            return Path(destination)
        path = self.directory / "index.html"
        if path.is_symlink():
            raise ValueError("Index path must not be a symlink")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(html)
        return path

    def reset_boundary(self):
        with self.connection() as db:
            self._append(db, "coverage.gap", {"reason": "explicit_boundary_reset", "head": head(self.root)})
            # Old events are retained and queryable, never reassigned to another commit.
            self.put(db, "epoch", uuid.uuid4().hex)
            self.put(db, "base", head(self.root) or "")
            self.put(db, "sampled", "0")
            db.execute("DELETE FROM files")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='file_units'").fetchone():
                db.execute("DELETE FROM file_units")
        return {"reset": True, "unsealed_events": "retained in previous epoch"}

    def sample(self):
        from ai_pow_scoring import fingerprints
        with self.connection() as db:
            self._boundary(db, automatic=True)
            paths = git(self.root, "ls-files", "-c", "-o", "--exclude-standard", "-z").split(b"\0")
            old_rows = {row[0]: (row[1], row[2]) for row in db.execute("SELECT path,hash,signature FROM files")}
            db.execute("CREATE TABLE IF NOT EXISTS file_units(path TEXT PRIMARY KEY, units TEXT NOT NULL)")
            old_units = dict(db.execute("SELECT path,units FROM file_units"))
            new_units = {}
            seen, observed, signatures, skipped, read_bytes = set(), {}, {}, 0, 0
            new_paths = 0
            safe_parents = {self.root: True}
            for raw in paths:
                if not raw:
                    continue
                name = os.fsdecode(raw)
                parts = Path(name).parts
                if (any(p in EXCLUDED for p in parts) or any(p.startswith(".env") for p in parts)
                        or name.endswith((".pem", ".key", ".p12"))):
                    skipped += 1
                    continue
                if name in seen:
                    continue
                seen.add(name)
                if len(seen) > MAX_FILES:
                    skipped += 1
                    continue
                path = self.root / name
                try:
                    # Reject directories, special files, and symlinked ancestors.
                    if path.parent not in safe_parents:
                        resolved = path.parent.resolve()
                        safe_parents[path.parent] = resolved == path.parent and resolved.is_relative_to(self.root)
                    if not safe_parents[path.parent]:
                        skipped += 1
                        continue
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                        skipped += 1
                        continue
                    path_id = hashlib.sha256(raw).hexdigest()
                    if path_id not in old_rows:
                        if len(old_rows) + new_paths >= MAX_FILES:
                            skipped += 1
                            continue
                        new_paths += 1
                    signature = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
                    if old_rows.get(path_id, (None, None))[1] == signature and path_id in old_units:
                        observed[path_id] = old_rows[path_id][0]
                        signatures[path_id] = signature
                        if path_id in old_units:
                            new_units[path_id] = json.loads(old_units[path_id])
                        continue
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        signature = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
                        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE or read_bytes + info.st_size > SCAN_BYTES:
                            skipped += 1
                            continue
                        content = stream.read(MAX_FILE + 1)
                        read_bytes += len(content)
                        if len(content) > MAX_FILE:
                            skipped += 1
                            continue
                    # Content hashes deliberately omit source text and file names from exported traces.
                    observed[path_id] = hashlib.sha256(content).hexdigest()
                    signatures[path_id] = signature
                    new_units[path_id] = fingerprints(content, name)
                except FileNotFoundError:
                    continue
                except OSError:
                    skipped += 1
            old = {key: value[0] for key, value in old_rows.items()}
            initial = self.get(db, "sampled") == "0"
            # When scan is partial we must not call absent files deletions.
            for path_id in sorted(set(observed) | (set(old) if not skipped else set())):
                before, after = old.get(path_id), observed.get(path_id)
                if before != after and not initial:
                    data = {"path_hash": path_id, "before": before, "after": after}
                    previous_units = json.loads(old_units[path_id]) if path_id in old_units else None
                    following_units = new_units.get(path_id)
                    if (before is None or previous_units is not None) and (after is None or following_units is not None):
                        data.update(units_before=(previous_units or {}).get("units", []),
                                    units_after=(following_units or {}).get("units", []),
                                    unit_kind=(following_units or previous_units or {}).get("kind", "content-blocks"),
                                    units_truncated=bool((previous_units or {}).get("truncated") or (following_units or {}).get("truncated")))
                    self._append(db, "file.observed", data, "sampler")
                if after:
                    if old_rows.get(path_id) != (after, signatures[path_id]):
                        db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (path_id, after, signatures[path_id]))
                elif not skipped:
                    db.execute("DELETE FROM files WHERE path=?", (path_id,))
                    db.execute("DELETE FROM file_units WHERE path=?", (path_id,))
                if path_id in new_units:
                    packed = canonical(new_units[path_id]).decode()
                    if old_units.get(path_id) != packed:
                        db.execute("INSERT OR REPLACE INTO file_units VALUES (?,?)", (path_id, packed))
            if skipped:
                last_gap = db.execute("SELECT value FROM meta WHERE key='last_scan_skipped'").fetchone()
                if not last_gap or last_gap[0] != str(skipped):
                    self._append(db, "coverage.gap", {"reason": "scan_exclusions_or_limits", "skipped": skipped}, "sampler")
            self.put(db, "last_scan_skipped", skipped)
            self.put(db, "sampled", "1")
            return {"observed_files": len(observed), "skipped": skipped, "bytes_read": read_bytes, "baseline": initial}

    def status(self):
        with self.connection() as db:
            return {"version": VERSION, "storage": str(self.directory),
                    "database_bytes": self.database.stat().st_size,
                    "quota_bytes": int(self.get(db, "max_bytes")),
                    "base": self.get(db, "base") or None, "head": head(self.root),
                    "proofs": db.execute("SELECT COUNT(*) FROM proofs").fetchone()[0],
                    "capture": "partial", "trust": "local-self-reported",
                    "recording_error": (self.directory / "recording-error").exists(),
                    "active": summary(self._events(db, self.get(db, "epoch")))}

    def proof(self, ref="HEAD"):
        commit = commit_meta(self.root, ref)["commit"]
        with self.connection() as db:
            row = db.execute("SELECT body FROM proofs WHERE commit_id=?", (commit,)).fetchone()
            if not row:
                raise ValueError("No proof recorded for " + commit)
            return json.loads(row[0])

    def verify(self, ref="HEAD"):
        p = self.proof(ref)
        with self.connection() as db:
            return verify_proof(self.root, p, self._events(db, p["epoch"]))

    def export(self, destination, ref="HEAD"):
        p = self.proof(ref)
        # Single streaming JSONL artifact: proof header followed by hash-chained events.
        with self.connection() as db:
            verify_proof(self.root, p, self._events(db, p["epoch"]))
            with open(destination, "xb") as out:
                os.chmod(destination, 0o600)
                out.write(canonical({"proof": p}) + b"\n")
                for e in self._events(db, p["epoch"]):
                    out.write(canonical(e) + b"\n")
        return {"exported": str(destination), "proof_hash": p["proof_hash"]}


SCORING_TYPES = {"human.message", "file.observed", "task.change", "coverage.gap"}


def score_inputs(root, proof, events):
    """Shared by scoring and by the derived report breakdown: one blob pass."""
    from ai_pow_scoring import collect_evidence, fingerprints
    selected = []
    for event in events:
        if event["type"] in SCORING_TYPES:
            selected.append(event)
            if len(selected) > 4096:
                break
    truncated = len(selected) > 4096
    selected = selected[:4096]
    wanted = set()
    for event in selected:
        if event["type"] == "file.observed":
            wanted.add(event["data"]["path_hash"])
        elif event["type"] == "task.change":
            wanted.update(event["data"].get("evidence", []))
    # Only inspect bounded blobs from the committed tree, never the worktree.
    args = ("diff", "--name-only", "--no-renames", "-z", proof["base"], proof["commit"]) if proof["base"] else (
        "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", proof["commit"])
    changed = {p for p in git(root, *args).split(b"\0") if p}
    units, known_paths, names = {}, set(), {}
    scope_units, read_bytes, inspected = 0, 0, 0
    limited = truncated
    for entry in git(root, "ls-tree", "-r", "-l", "-z", proof["commit"]).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid, size = metadata.split()
        path_id = hashlib.sha256(raw_path).hexdigest()
        known_paths.add(path_id)
        if path_id in wanted or raw_path in changed:
            names[path_id] = os.fsdecode(raw_path)
        if path_id not in wanted and raw_path not in changed:
            continue
        if (kind != b"blob" or mode not in {b"100644", b"100755"} or int(size) > MAX_FILE
                or read_bytes + int(size) > SCAN_BYTES or inspected >= 128):
            limited = True
            continue
        content = git(root, "cat-file", "blob", oid.decode(), limit=MAX_FILE)
        read_bytes += len(content)
        inspected += 1
        result = fingerprints(content, os.fsdecode(raw_path))
        limited = limited or result["truncated"]
        units[path_id] = result["units"]
        if raw_path in changed:
            scope_units += len(result["units"])
    for path_id in wanted - known_paths:
        units[path_id] = []  # A verified deletion is a surviving outcome too.
    evidence = collect_evidence(selected, units, scope_units, len(changed), limited)
    return {"selected": selected, "units": units, "names": names, "evidence": evidence}


def evaluate_score(root, proof, events, algorithm=None):
    from ai_pow_scoring import ALGORITHM, score
    inputs = score_inputs(root, proof, events)
    return score(inputs["evidence"], proof["summary"], algorithm or ALGORITHM)


MAX_TIMELINE = 28
MAX_FILE_ROWS = 40


def derived_facts(root, proof, inputs, events):
    """Report-only detail: per-file survival, a timeline and the commit contents.

    Everything here is derived from the sealed trace and from Git at view time.
    None of it is part of the proof, and the report labels it as derived.
    """
    from collections import Counter
    selected, units, names = inputs["selected"], inputs["units"], inputs["names"]
    baseline, operations, writes = {}, Counter(), Counter()
    overwritten, reintroduced, lineage, seen = Counter(), Counter(), set(), {}
    timeline, extra = [], 0
    for event in selected:
        if event["type"] != "file.observed":
            continue
        data = event["data"]
        if "units_before" not in data or "units_after" not in data:
            continue
        path = data["path_hash"]
        before, after = set(data["units_before"]), set(data["units_after"])
        baseline.setdefault(path, before)
        added, removed = after - before, before - after
        if added or removed:
            writes[path] += 1
        operations[path] += len(added) + len(removed)
        for unit in added:
            history = seen.setdefault((path, unit), [])
            if history and history[-1] == "remove":
                reintroduced[path] += 1
            history.append("add")
            lineage.add((path, "add", unit))
        replaced = 0
        for unit in removed:
            history = seen.setdefault((path, unit), [])
            if "add" in history:
                replaced += 1
            history.append("remove")
            lineage.add((path, "remove", unit))
        overwritten[path] += replaced
        if replaced:
            timeline.append({"at": event.get("time_ms"), "kind": "rework",
                             "label": names.get(path, path[:10]),
                             "detail": str(replaced) + " earlier edits overwritten"})
    retained = Counter()
    for path, before in baseline.items():
        if path not in units:
            continue
        after = set(units[path])
        for action, changed in (("add", after - before), ("remove", before - after)):
            for unit in changed:
                if (path, action, unit) in lineage:
                    retained[path] += 1
    files = [{"path": names.get(path), "id": path[:10], "writes": writes[path],
              "operations": operations[path], "retained": retained[path],
              "checked": path in units, "overwritten": overwritten[path],
              "reintroduced": reintroduced[path]}
             for path in operations]
    files.sort(key=lambda row: (-row["operations"], row["path"] or row["id"]))
    mcp_calls, tool_results, tasks = 0, 0, {}
    for event in events:
        kind, data = event["type"], event["data"]
        when = event.get("time_ms")
        if kind == "tool.call" and data.get("mcp_server"):
            mcp_calls += 1
        elif kind == "tool.result":
            tool_results += 1
        elif kind == "run.start":
            timeline.append({"at": when, "kind": "session", "label": "session started",
                             "detail": str(data.get("command") or "")[:40]})
        elif kind == "run.stop":
            timeline.append({"at": when, "kind": "session", "label": "session ended",
                             "detail": "exit " + str(data.get("exit_code"))})
        elif kind == "task.change" and data.get("task_id"):
            key = data["task_id"]
            tasks[key] = tasks.get(key, 0) + 1
            timeline.append({"at": when, "kind": "task", "label": key[:48],
                             "detail": str(data.get("status") or ""),
                             "repeat": tasks[key]})
    timeline = [item for item in timeline if item["at"] is not None]
    timeline.sort(key=lambda item: item["at"])
    if len(timeline) > MAX_TIMELINE:
        extra = len(timeline) - MAX_TIMELINE
        timeline = timeline[:MAX_TIMELINE // 2] + timeline[-(MAX_TIMELINE - MAX_TIMELINE // 2):]
    return {"files": files[:MAX_FILE_ROWS], "files_total": len(files),
            "timeline": timeline, "timeline_omitted": extra,
            "mcp_calls": mcp_calls, "tool_results": tool_results,
            "result": commit_result(root, proof)}


def commit_result(root, proof):
    """What the commit itself contains, read from Git."""
    try:
        args = ("diff", "--name-status", "-z", proof["base"], proof["commit"]) if proof["base"] else (
            "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-z", proof["commit"])
        fields = [f for f in git(root, *args).split(b"\0") if f]
    except Exception:
        return None
    counts = {"added": 0, "modified": 0, "removed": 0, "renamed": 0, "tests": 0}
    index = 0
    while index < len(fields):
        status = fields[index].decode("utf-8", "replace")
        step = 3 if status[:1] in {"R", "C"} else 2
        path = fields[min(index + step - 1, len(fields) - 1)].decode("utf-8", "replace")
        counts[{"A": "added", "M": "modified", "D": "removed",
                "R": "renamed", "C": "added"}.get(status[:1], "modified")] += 1
        if re.search(r"(^|/)(tests?|spec|__tests__)/|(_test|\.test|\.spec)\.", path):
            counts["tests"] += 1
        index += step
    return counts


def verify_proof(root, proof, events):
    p = dict(proof)
    h = p.pop("proof_hash")
    if digest(p) != h or p["v"] != VERSION:
        raise ValueError("Proof hash/version mismatch")
    meta = commit_meta(root, p["commit"])
    if any(meta[k] != p[k] for k in ("commit", "tree", "parents")):
        raise ValueError("Git commit/tree/parent mismatch")
    git(root, "fsck", "--strict", "--no-reflogs", "--no-dangling", p["commit"])
    if (p["parents"][:1] or [None])[0] != p["base"]:
        raise ValueError("Proof base does not match first parent")
    previous, sequence, count = p["anchor"], p["first_seq"], 0
    scoring_events = []
    def checked():
        nonlocal previous, sequence, count
        for item in events:
            e = dict(item)
            eh = e.pop("hash")
            validate_event(e["type"], e["data"])
            if (digest(e) != eh or e["previous"] != previous or e["seq"] != sequence
                    or e["epoch"] != p["epoch"] or e["v"] != VERSION):
                raise ValueError("Trace hash/sequence/epoch mismatch")
            previous = eh
            sequence += 1
            count += 1
            if p.get("score") and e["type"] in SCORING_TYPES and len(scoring_events) < 4097:
                scoring_events.append(e)
            yield e
    totals = summary(checked(), algorithm=p["summary"].get("algorithm", "observed-v1"))
    if previous != p["trace_root"] or (sequence - 1 if count else None) != p["last_seq"]:
        raise ValueError("Trace missing or truncated")
    if bool(count) != (p["first_seq"] is not None) or totals != p["summary"]:
        raise ValueError("Summary/range mismatch")
    if p.get("score"):
        # Recompute under the algorithm the proof recorded, not the installed one.
        if evaluate_score(root, p, scoring_events, p["score"].get("algorithm", "unrecorded")) != p["score"]:
            raise ValueError("Score calculation/version mismatch")
    return {"integrity_verified": True, "git_tree_verified": True, "events": count,
            "proof_hash": h, "trust": "local-self-reported", "completeness_verified": False,
            "work_authenticity_verified": False, "quality_verified": False}


def verify_bundle(root, path):
    with open(path, "rb") as stream:
        first = stream.readline(1024 * 1024 + 1)
        if len(first) > 1024 * 1024:
            raise ValueError("Proof header exceeds limit")
        proof = json.loads(first)["proof"]
        def rows():
            while True:
                line = stream.readline(MAX_EVENT + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT:
                    raise ValueError("Bundle event exceeds limit")
                yield json.loads(line)
        return verify_proof(root, proof, rows())


def invocation():
    if getattr(sys, "frozen", False):
        return [sys.executable, "pow"]
    return [sys.executable, str(Path(__file__).resolve())]


def install_hook(recorder):
    hook = Path(git(recorder.root, "rev-parse", "--git-path", "hooks/post-commit").decode().strip())
    if not hook.is_absolute():
        hook = recorder.root / hook
    command = shlex.join(invocation() + ["--cwd", ".", "seal"])
    body = "#!/bin/sh\n# AI-PoW 0.1 (local integrity only)\n" + command + " >/dev/null || echo 'AI-PoW: proof not sealed; run aipow status' >&2\nexit 0\n"
    # A shared/custom hooksPath must not be overwritten or silently used for other worktrees.
    custom = git(recorder.root, "config", "--get", "core.hooksPath", allow_fail=True)
    gd = recorder.directory.parent
    common = git(recorder.root, "rev-parse", "--git-common-dir").decode().strip()
    common = (recorder.root / common).resolve()
    if custom or common != gd:
        return {"installed": False, "reason": "shared/custom hooks path", "manual_command": command}
    if hook.exists() or hook.is_symlink():
        return {"installed": False, "reason": "existing hook preserved", "manual_command": command}
    hook.parent.mkdir(parents=True, exist_ok=True)
    with open(hook, "x") as out:
        out.write(body)
    hook.chmod(0o700)
    return {"installed": True, "path": str(hook)}


def claude_hook(recorder, payload):
    kind = payload.get("hook_event_name")
    data = {"session_id": str(payload.get("session_id", ""))[:128],
            "agent_id": str(payload.get("agent_id", "main"))[:128]}
    event_id = None
    if kind == "UserPromptSubmit":
        typ = "human.message"
        data.update(text_meta(payload.get("prompt", "")))
    elif kind == "MessageDisplay":
        typ = "assistant.visible"
        data.update(text_meta(payload.get("delta", "")))
        data["channel"] = "display"
        event_id = digest([data["session_id"], kind, payload.get("message_id"), payload.get("index")])
    elif kind in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
        typ = "tool.call" if kind == "PreToolUse" else "tool.result"
        name = str(payload.get("tool_name", "unknown"))[:128]
        data.update(name=name, call_id=str(payload.get("tool_use_id", ""))[:128])
        if name.startswith("mcp__"):
            data["mcp_server"] = name.split("__")[1]
        if typ == "tool.result":
            data["ok"] = kind == "PostToolUse"
        if data["call_id"]:
            event_id = digest([data["session_id"], data["call_id"], typ])
    elif kind in {"SubagentStart", "SubagentStop"}:
        typ = "agent.spawn" if kind == "SubagentStart" else "agent.stop"
        origin = payload.get("parent_agent_id")
        data["parent_agent_id"] = str(origin)[:128] if origin else "unknown"
    elif kind in {"SessionStart", "Stop", "SessionEnd"}:
        typ = "run.start" if kind == "SessionStart" else "run.stop"
    else:
        return
    recorder.record(typ, data, "claude-hooks", event_id)
    transcript = payload.get("agent_transcript_path") if kind == "SubagentStop" else payload.get("transcript_path")
    if transcript and kind in {"SessionStart", "Stop", "SessionEnd", "SubagentStop"}:
        try:
            recorder.import_claude_usage(transcript, start_at_end=kind == "SessionStart")
        except FileNotFoundError:
            pass  # SessionStart can precede the creation of the transcript.
    if kind in {"PostToolUse", "PostToolUseFailure", "Stop"}:
        recorder.sample()


def run(recorder, command, interval=2):
    if not command:
        raise ValueError("Provide a command after --")
    recorder.sample()
    run_id = uuid.uuid4().hex
    recorder.record("run.start", {"run_id": run_id, "command": Path(command[0]).name}, "wrapper")
    env = dict(os.environ, AIPOW_ROOT=str(recorder.root), AIPOW_RUN_ID=run_id)
    proc = subprocess.Popen(command, env=env)
    previous_handlers = {}
    def forward(sig, frame):
        if proc.poll() is None:
            proc.send_signal(sig)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, forward)
        while True:
            try:
                code = proc.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                try:
                    recorder.sample()
                except Exception as exc:
                    record_error(recorder, exc)
        try:
            recorder.sample()
            recorder.record("run.stop", {"run_id": run_id, "exit_code": code}, "wrapper")
        except Exception as exc:
            record_error(recorder, exc)
        return code
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()


def record_error(recorder, exc):
    # Fixed-size health marker survives SQLITE_FULL; no accumulating error log.
    try:
        path = recorder.directory / "recording-error"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(type(exc).__name__ + ": recorder lost coverage; inspect boundary/quota\n")
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.environ.get("AIPOW_ROOT", "."))
    parser.add_argument("--version", action="version", version=APP_VERSION)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--max-mib", type=int, default=64)
    init.add_argument("--no-hook", action="store_true")
    for name in ("status", "sample", "seal", "reset-boundary", "install-hook", "hook-claude"):
        sub.add_parser(name)
    for name in ("report", "verify", "export"):
        p = sub.add_parser(name)
        p.add_argument("--commit", default="HEAD")
        if name == "verify":
            p.add_argument("--bundle")
        if name == "export":
            p.add_argument("destination")
        if name == "report":
            p.add_argument("--html", action="store_true", help="Generate the self-contained commit report")
            p.add_argument("--output", help="Export HTML to a new file; existing files are preserved")
    index = sub.add_parser("index")
    index.add_argument("--commit", default="HEAD")
    index.add_argument("--rebuild", action="store_true", help="Recompute the iteration chain from sealed proofs")
    index.add_argument("--html", action="store_true", help="Generate the repository summary page")
    index.add_argument("--output", help="Export HTML to a new file; existing files are preserved")
    task = sub.add_parser("task")
    task.add_argument("task_id")
    task.add_argument("--status", choices=("active", "completed", "dropped"), required=True)
    task.add_argument("--evidence", nargs="*", default=[], help="Repository-relative committed artifact paths")
    emit = sub.add_parser("emit")
    emit.add_argument("type", choices=sorted(TYPES))
    emit.add_argument("--source", default="generic")
    emit.add_argument("--event-id")
    price = sub.add_parser("price-set")
    price.add_argument("path")
    transcript = sub.add_parser("import-claude")
    transcript.add_argument("path")
    quota = sub.add_parser("quota")
    quota.add_argument("--max-mib", type=int, required=True)
    for name in ("run", "claude"):
        p = sub.add_parser(name)
        p.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    recorder = None
    try:
        recorder = Recorder(args.cwd)
        action = args.action
        if action == "init":
            result = recorder.init(args.max_mib)
            if not args.no_hook:
                result["hook"] = install_hook(recorder)
        elif action == "install-hook":
            result = install_hook(recorder)
        elif action == "price-set":
            with open(args.path, "rb") as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError("Price snapshot exceeds 4 KiB")
            result = recorder.set_price(json.loads(raw))
        elif action == "import-claude":
            result = recorder.import_claude_usage(args.path)
        elif action == "quota":
            result = recorder.set_quota(args.max_mib)
        elif action == "task":
            evidence = []
            for name in args.evidence:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Evidence paths must be repository-relative")
                evidence.append(hashlib.sha256(os.fsencode(path.as_posix())).hexdigest())
            result = {"recorded": recorder.record("task.change", {"task_id": args.task_id,
                       "status": args.status, "evidence": evidence}, "task-cli")}
        elif action in {"status", "sample", "seal", "reset-boundary"}:
            result = getattr(recorder, action.replace("-", "_"))()
        elif action == "index":
            rebuilt = recorder.rebuild_iterations(args.commit) if args.rebuild else {}
            result = ({**rebuilt, "index": str(recorder.html_index(args.commit, args.output))}
                      if args.html or args.output else {**rebuilt, **recorder.index_data(args.commit)})
        elif action == "report":
            result = {"report": str(recorder.html_report(args.commit, args.output))} if args.html or args.output else recorder.proof(args.commit)
        elif action == "verify":
            result = verify_bundle(recorder.root, args.bundle) if args.bundle else recorder.verify(args.commit)
        elif action == "export":
            result = recorder.export(args.destination, args.commit)
        elif action in {"emit", "hook-claude"}:
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Input exceeds 1 MiB")
            payload = json.loads(raw)
            if action == "hook-claude":
                if payload.get("cwd"):
                    target = Recorder(payload["cwd"])
                    if target.directory != recorder.directory:
                        if not target.database.is_file():
                            recorder.record("coverage.gap", {"reason": "uninitialized_claude_worktree"}, "claude-hooks")
                            return 0
                        recorder = target
                claude_hook(recorder, payload)
                return 0
            result = {"recorded": recorder.record(args.type, payload, args.source, args.event_id)}
        elif action in {"run", "claude"}:
            command = args.args[1:] if args.args[:1] == ["--"] else args.args
            if action == "claude":
                hooks = {}
                for event in ("SessionStart", "UserPromptSubmit", "MessageDisplay", "PreToolUse",
                              "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop", "Stop", "SessionEnd"):
                    hooks[event] = [{"hooks": [{"type": "command", "command": shlex.join(invocation() + ["--cwd", str(recorder.root), "hook-claude"]), "timeout": 5}]}]
                command = ["claude", "--settings", json.dumps({"hooks": hooks}), *command]
            return run(recorder, command)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, ArithmeticError) as exc:
        if recorder and args.action in {"hook-claude", "seal", "emit", "sample"}:
            record_error(recorder, exc)
        print("AI-PoW: " + str(exc), file=sys.stderr)
        return 0 if args.action == "hook-claude" else 1


if __name__ == "__main__":
    raise SystemExit(main())
