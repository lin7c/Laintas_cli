"""Optional AI-PoW adapter, activated only in explicitly initialized repos.

No prompt/tool payload is persisted. Recorder failures never break the agent.
"""
import os
from pathlib import Path
import threading

import ai_pow

_local = threading.local()


def bind(cwd):
    _local.cwd = str(cwd)


def _recorder():
    cwd = Path(getattr(_local, "cwd", None) or os.getcwd())
    for directory in (cwd, *cwd.parents):
        marker = directory / ".git"
        if marker.is_dir():
            if not (marker / "ai-pow" / "events.sqlite3").is_file():
                return None
            return ai_pow.Recorder(cwd)
        if marker.is_file():
            rec = ai_pow.Recorder(cwd)
            return rec if rec.database.is_file() else None
    return None


def emit(kind, data, event_id=None):
    rec = None
    try:
        rec = _recorder()
        if rec:
            rec.record(kind, data, "laintas-native", event_id)
    except Exception as exc:
        if rec:
            ai_pow.record_error(rec, exc)


def message(kind, text, **context):
    try:
        if _recorder() is not None:
            emit(kind, {**ai_pow.text_meta(text), **context})
    except Exception:
        pass


def sample():
    rec = None
    try:
        rec = _recorder()
        if rec:
            rec.sample()
    except Exception as exc:
        if rec:
            ai_pow.record_error(rec, exc)


def journal(kind, fields):
    if kind not in {"tool_call", "tool_result"}:
        return
    data = {"name": str(fields.get("name") or "unknown")[:128],
            "call_id": str(fields.get("call_id") or "")[:128],
            "session_id": str(fields.get("session_id") or "")[:128],
            "run_id": str(fields.get("run_id") or "")[:128]}
    source = str(fields.get("source") or "")
    if source.startswith("mcp"):
        data["mcp_server"] = source[:128]
    if kind == "tool_result":
        data["ok"] = bool(fields.get("ok"))
    emit(kind.replace("_", "."), data)


def usage(rec):
    from decimal import Decimal
    emit("model.usage", {
        "model": rec["model"], "input_tokens": rec["in"],
        "output_tokens": rec["out"], "cached_input_tokens": rec["cachedIn"],
        "reasoning_tokens": None, "cache_write_tokens": None,
        "measurement": "estimated" if rec["estimated"] else "provider_reported",
        "actual_usd": str(Decimal(rec["costCents"]) / 100) if rec["official"] else None,
        "provider": rec["backend"] or "unknown",
    })
