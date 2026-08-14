"""Detailed, per-conversation tool traces for the interactive CLI.

Trace payloads live beside normal chat-history messages so session/resume
persistence keeps their chronology without a second journal.  This module is
terminal-UI agnostic; it only captures and formats semantic content.
"""

from __future__ import annotations

import difflib
import json
import os
from typing import Any, Optional


_FILE_MUTATION_TOOLS = frozenset({
    "fs.write", "fs.edit", "fs.multi_edit", "fs.delete",
})


def _absolute_path(value: Any, cwd: str) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _read_text(path: str) -> tuple[Optional[str], str]:
    if not path or not os.path.isfile(path):
        return None, ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(), ""
    except OSError as exc:
        return None, str(exc)


def capture_before(tool_name: str, arguments: dict, cwd: str) -> dict:
    """Capture a mutation target before dispatch so deleted lines survive."""
    if tool_name not in _FILE_MUTATION_TOOLS:
        return {}
    path = _absolute_path(arguments.get("path"), cwd)
    content, error = _read_text(path)
    return {"path": path, "before": content, "read_error": error}


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _result_content(tool_name: str, result: dict, fallback: str) -> str:
    payload = result.get("result")
    if tool_name == "fs.grep" and isinstance(payload, list):
        rows = []
        last_file = None
        for match in payload:
            if not isinstance(match, dict):
                rows.append(str(match))
                continue
            filename = str(match.get("file") or "")
            if filename != last_file:
                if rows:
                    rows.append("")
                rows.append(filename)
                last_file = filename
            rows.append(f"{int(match.get('line') or 0):>6} | {match.get('content', '')}")
        return "\n".join(rows)
    if tool_name in {"fs.ls", "fs.list"} and isinstance(payload, list):
        return "\n".join(
            (str(item.get("name") or "") + ("/" if item.get("type") == "dir" else ""))
            if isinstance(item, dict) else str(item)
            for item in payload
        )
    if tool_name == "fs.glob" and isinstance(payload, list):
        return "\n".join(
            (str(item.get("path") or "") + ("/" if item.get("type") == "dir" else ""))
            if isinstance(item, dict) else str(item)
            for item in payload
        )
    if payload is not None:
        return _json_text(payload)
    if not result.get("ok", True) and result.get("error"):
        return str(result["error"])
    return fallback


def build_tool_trace(tool_name: str, display_name: str, arguments: dict,
                     result: dict, model_output: str, elapsed: float,
                     cwd: str, before: Optional[dict] = None) -> dict:
    """Build a serializable trace payload after one tool finishes."""
    before = before or {}
    path = str(result.get("path") or before.get("path")
               or _absolute_path(arguments.get("path"), cwd) or "")
    after = None
    file_error = ""
    if tool_name in _FILE_MUTATION_TOOLS and tool_name != "fs.delete":
        after, file_error = _read_text(path)
    trace = {
        "tool": tool_name,
        "display_name": display_name,
        "arguments": arguments,
        "ok": bool(result.get("ok", False)),
        "returncode": result.get("returncode"),
        "elapsed_seconds": round(float(elapsed or 0.0), 3),
        "path": path,
        "content": _result_content(tool_name, result, model_output),
        "model_output": model_output,
    }
    if tool_name in _FILE_MUTATION_TOOLS:
        trace.update({
            "before": before.get("before"),
            "after": after,
            "file_error": file_error or before.get("read_error", ""),
            "diff": str(result.get("diff") or ""),
        })
    return trace


def conversation_traces(chat_history: list) -> list[dict]:
    """Return recorded prompt turns in chronological order."""
    turns = []
    current = None
    for message in chat_history or ():
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            if current is not None:
                turns.append(current)
            current = {
                "prompt": str(message.get("content") or ""),
                "enabled": bool(message.get("detail_trace")),
                "items": [],
            }
            continue
        if current is None or not current["enabled"]:
            continue
        if message.get("role") == "tool" and isinstance(message.get("trace"), dict):
            current["items"].append({"kind": "tool", "message": message})
        elif message.get("role") == "assistant" and message.get("content"):
            current["items"].append({"kind": "ai", "message": message})
    if current is not None:
        turns.append(current)
    return turns


def _line_number_width(before_lines: list[str], after_lines: list[str]) -> int:
    return max(1, len(str(max(len(before_lines), len(after_lines), 1))))


def full_file_diff(before: Optional[str], after: Optional[str]) -> list[dict]:
    """Return a full-file view with inserted deletion rows and change styles."""
    old = [] if before is None else before.splitlines()
    new = [] if after is None else after.splitlines()
    width = _line_number_width(old, new)
    rows: list[dict] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset, text in enumerate(new[j1:j2]):
                number = j1 + offset + 1
                rows.append({"style": "same", "line": number,
                             "text": f"  {number:>{width}} | {text}"})
        else:
            for offset, text in enumerate(old[i1:i2]):
                number = i1 + offset + 1
                rows.append({"style": "delete", "line": number,
                             "text": f"- {number:>{width}} | {text}"})
            for offset, text in enumerate(new[j1:j2]):
                number = j1 + offset + 1
                rows.append({"style": "add", "line": number,
                             "text": f"+ {number:>{width}} | {text}"})
    return rows


def trace_item_label(item: dict) -> str:
    message = item.get("message") or {}
    if item.get("kind") == "ai":
        text = " ".join(str(message.get("content") or "").split())
        return f"AI      {text[:90]}"
    trace = message.get("trace") or {}
    name = str(trace.get("display_name") or trace.get("tool") or "Tool")
    summary = str(message.get("summary") or trace.get("path") or "")
    suffix = ""
    diff = str(trace.get("diff") or "")
    if diff:
        adds = sum(1 for line in diff.splitlines()
                   if line.startswith("+") and not line.startswith("+++"))
        deletes = sum(1 for line in diff.splitlines()
                      if line.startswith("-") and not line.startswith("---"))
        suffix = f" · +{adds} -{deletes}"
    elif trace.get("returncode") is not None:
        suffix = f" · exit {trace['returncode']}"
    return f"{name:<7} {summary[:90]}{suffix}".rstrip()
