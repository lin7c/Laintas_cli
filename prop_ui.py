"""Structured terminal inspector for captured model contexts."""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

import resource_ui


def parse_target(raw_args: str) -> tuple[bool, int]:
    """Return ``(system_only, newest_index)`` for /prop arguments."""
    parts = str(raw_args or "").strip().split()
    system_only = False
    if parts and parts[0].lower() == "sys":
        system_only = True
        parts.pop(0)
    if len(parts) > 1 or (parts and not parts[0].isdigit()):
        raise ValueError("Usage: /prop [sys] [N]")
    index = int(parts[0]) if parts else 1
    if index < 1:
        raise ValueError("N must be a positive integer (1 is newest)")
    return system_only, index


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _json(value)


def _item(key: str, title: str, subtitle: str, badge: str,
          content: Any, *, kind: str = "document") -> resource_ui.UIItem:
    text = _content_text(content)
    return resource_ui.UIItem(
        key=key, title=title, subtitle=subtitle, badge=badge,
        payload={"title": title, "subtitle": subtitle, "content": text,
                 "kind": kind},
        search_text=text,
    )


def context_items(conversation: dict, *, system_only: bool = False
                  ) -> list[resource_ui.UIItem]:
    """Group one conversation around its final provider/model call.

    Group payloads intentionally retain the complete captured values.  This
    keeps both detail inspection and local search lossless without making the
    browser render a row for every message, tool, or model call.
    """
    calls = list(conversation.get("calls") or [])
    if not calls:
        return []
    call_count = len(calls)
    call = calls[-1] if calls else {}
    call_label = f"call {call_count} of {call_count} · latest/final default"
    metadata = call.get("metadata") or {}
    verified = bool(metadata.get("verified_gateway_context"))
    verification = "gateway verified" if verified else "local capture · unverified"
    system_prompt = call.get("system_prompt") or ""
    sections = list(call.get("system_sections") or [])
    messages = list(call.get("messages") or [])
    tools = list(call.get("tool_schemas") or [])

    items = [
        _item(
            "effective-system-prompt", "Effective System Prompt",
            f"{call_label} · {verification} · "
            f"{len(_content_text(system_prompt)):,} chars",
            "SYSTEM", system_prompt, kind="system"),
        _item(
            "system-sections", f"System Sections ({len(sections):,})",
            f"{call_label} · exact captured section records",
            "SECTIONS", sections, kind="system-section"),
    ]
    if system_only:
        return items

    capture_metadata = {
        "metadata": metadata,
        "gateway_context_receipt": call.get("gateway_context_receipt"),
    }
    call_summary = []
    for number, captured_call in enumerate(calls, 1):
        call_summary.append({
            "call_number": number,
            "latest_final_default": number == call_count,
            "message_count": len(captured_call.get("messages") or []),
            "tool_count": len(captured_call.get("tool_schemas") or []),
            "system_section_count": len(
                captured_call.get("system_sections") or []),
            "metadata": captured_call.get("metadata") or {},
        })
    model_calls = {
        "summary": call_summary,
        "calls": calls,
    }
    items.extend([
        _item(
            "messages", f"Messages ({len(messages):,})",
            f"{call_label} · exact provider order",
            "MESSAGES", messages, kind="message"),
        _item(
            "tools", f"Tools ({len(tools):,})",
            f"{call_label} · exact provider schemas",
            "TOOLS", tools, kind="tool"),
        _item(
            "capture-metadata", "Capture Metadata",
            f"{call_label} · loop {metadata.get('loop', '?')} · "
            f"{verification}",
            "META", capture_metadata, kind="metadata"),
        _item(
            "model-calls", f"Model Calls ({call_count:,})",
            "all calls summarized below; exact captured contexts included · "
            "latest/final call is the default",
            "CALLS", model_calls, kind="metadata"),
    ])
    return items


def item_detail(item: resource_ui.UIItem) -> resource_ui.UIDetail:
    payload = item.payload or {}
    content = str(payload.get("content") or "(empty)")
    kind = str(payload.get("kind") or "")
    base_style = "class:detail.code" if kind in {"tool", "metadata"} else "class:detail"
    lines = []
    for line in content.splitlines() or ["(empty)"]:
        style = base_style
        if line.startswith(("<", "#", "[")):
            style = "class:detail.heading"
        lines.append(resource_ui.UILine(line, style))
    return resource_ui.UIDetail(
        title=str(payload.get("title") or item.title),
        subtitle=str(payload.get("subtitle") or item.subtitle),
        lines=lines, kind=kind,
    )


def open_browser(conversation: dict, *, system_only: bool,
                 newest_index: int,
                 assistant_handler: Optional[resource_ui.AssistantHandler] = None,
                 input=None, output=None) -> resource_ui.UIOutcome:
    items = context_items(conversation, system_only=system_only)
    title = ("System Prompt" if system_only else "Conversation Context")
    call_count = len(conversation.get("calls") or [])
    browser = resource_ui.ResourceBrowser(
        title=(f"{title} · newest #{newest_index} · {call_count:,} calls · "
               "latest/final call shown by default"),
        load_items=lambda: items,
        load_detail=item_detail,
        primary_action="view", primary_label="Inspect",
        presentation="document",
        pane_labels=("STRUCTURE", "CONTENT"),
        empty_message="No captured context is available.",
        assistant_handler=assistant_handler,
        assistant_placeholder="translate, locate, explain, or propose a modification",
        input=input, output=output,
    )
    return browser.run()


def selected_context(item: Optional[resource_ui.UIItem],
                     detail: Optional[resource_ui.UIDetail]) -> str:
    if item is not None and isinstance(item.payload, dict):
        return str(item.payload.get("content") or "")
    if detail is not None:
        return "\n".join(line.text for line in detail.lines)
    return ""
