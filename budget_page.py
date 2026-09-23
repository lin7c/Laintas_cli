"""`/prop budget N output`: a self-contained page for tuning the context budget.

The page is built from what one captured request actually held — the model,
its window, every block's size and text, every tool's schema cost. It shows
the budget tree two ways: a structure tree, and nested partitions (blocks
inside blocks, each sized by its quota) that open level by level from the
whole window down. Dragging a divider or changing a share re-runs the budget
arithmetic in the browser, and the selected block's original text is coloured
by what those settings would send. It exports only the values that changed,
as a `.config` file — one `/config` line per setting, the format
`/config import` reads. The gateway's own block is never part of it.

The template (context_policy/budget_page.html) is shared with Helpwo, whose
copy is src/context/budget-tuner.html; keep the two identical. Helpwo runs it
in `mode: "helpwo"` and applies the result in place; here it runs in
`mode: "cli"` and downloads the file.

One file, no network: it has to open from a server's disk as easily as from a
laptop, and a budget page that fetched a script from a CDN would stop working
exactly where it is most needed.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

import config_file

_TEXT_LIMIT = 200_000
_TEMPLATE_PATH = Path(__file__).resolve().parent / "context_policy" / "budget_page.html"
#: Top-level trees the CLI fits (Helpwo fits only output/system/live).
ACTIVE_TREES = ("output", "system", "tools", "live", "thread")


def page_data(rows: dict, window: dict, described: dict, *, newest_index: int) -> dict:
    """Everything the page needs, with the gateway left out."""
    nodes: dict = {}
    scalars: dict = {}
    for key, meta in described.items():
        if not key.startswith("budget."):
            continue
        rest = key[len("budget."):]
        if "." not in rest:
            # Single values: hysteresis, assumed windows, chars per token.
            scalars[rest] = {"value": meta["value"], "default": meta["default"],
                             "description": meta.get("description", "")}
            continue
        path, leaf = rest.rsplit(".", 1)
        nodes.setdefault(path, {})[leaf] = {"value": meta["value"], "default": meta["default"]}
    blocks = {}
    for section in ("system", "tools", "live", "thread"):
        for row in rows.get(section) or []:
            path = str(row.get("path") or "")
            if not path or ".gateway" in path:
                continue
            blocks[path] = {
                "depth": int(row.get("depth") or 1),
                "natural": int(row.get("natural") or 0),
                "allotted": int(row.get("allotted") or 0),
                "delivered": int(row.get("delivered") or 0),
                "triggered": bool(row.get("triggered")),
                "original": str(row.get("original") or "")[:_TEXT_LIMIT],
                "compressed": str(row.get("compressed") or "")[:_TEXT_LIMIT],
                "items": row.get("items") or [],
            }
    window = window or {}
    return {"version": 2, "mode": "cli", "lang": "auto", "origin": "",
            "request": newest_index,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": str(window.get("model") or ""),
            "window": int(window.get("window") or 0),
            "maxOutput": int(window.get("max_output") or 0),
            "maxTokens": int(window.get("max_tokens") or 0),
            "nodes": nodes, "scalars": scalars, "blocks": blocks,
            "activeTrees": list(ACTIVE_TREES),
            "exportHeader": config_file.HEADER}


def write_page(data: dict, target: Path) -> Path:
    # `<` escaped as in Helpwo: no text in a block can close the data script.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    title = f"Context budget · #{data.get('request', 1)}"
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.replace("__LANG__", "en").replace("__TITLE__", html.escape(title))
                      .replace("__DATA__", payload, 1), encoding="utf-8")
    return target
