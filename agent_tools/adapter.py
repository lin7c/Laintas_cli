"""Python adapter for the language-neutral agent tool catalog.

Single source of truth = ``catalog.json`` (sibling file). This module is
dependency-free (stdlib only) so it can be vendored verbatim into any Python
agent product (the gateway backend, laintas_cli, future agents).

Responsibilities:
  * load the canonical catalog,
  * emit provider-ready (OpenAI-style) tool schemas using the canonical FLAT
    names — no dots, so no DeepSeek mangling is ever needed,
  * translate between a product's legacy tool name and the canonical name,
    so existing executors keep working during the migration window.

It deliberately does NOT execute anything. Each product wires the canonical
names to its own executors (host vs backend per ``execContext``).
"""
from __future__ import annotations

import json
import os
from typing import Iterable, Optional

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json")

_NAME_RE_OK = None  # canonical names are pre-validated flat snake_case


class Catalog:
    def __init__(self, data: dict):
        self.version: str = data.get("version", "0")
        self._tools: list[dict] = data.get("tools", [])
        self._by_name: dict[str, dict] = {t["name"]: t for t in self._tools}
        # legacy -> canonical, keyed per product
        self._legacy_to_canon: dict[str, dict[str, str]] = {}
        # canonical -> legacy, keyed per product
        self._canon_to_legacy: dict[str, dict[str, str]] = {}
        for t in self._tools:
            for product, legacy in (t.get("aliases") or {}).items():
                self._legacy_to_canon.setdefault(product, {})[legacy] = t["name"]
                self._canon_to_legacy.setdefault(product, {})[t["name"]] = legacy

    # ---- introspection ----
    def names(self) -> list[str]:
        return list(self._by_name)

    def get(self, name: str) -> Optional[dict]:
        return self._by_name.get(name)

    def exec_context(self, name: str) -> Optional[str]:
        t = self._by_name.get(name)
        return t.get("execContext") if t else None

    def by_context(self, ctx: str) -> list[str]:
        """Canonical names whose execContext is ctx (or 'either')."""
        return [t["name"] for t in self._tools
                if t.get("execContext") == ctx or t.get("execContext") == "either"]

    # ---- name translation (migration window) ----
    def canonical(self, legacy_name: str, product: str) -> Optional[str]:
        """A product's legacy tool name -> canonical name (None if unknown)."""
        return self._legacy_to_canon.get(product, {}).get(legacy_name)

    def legacy(self, canonical_name: str, product: str) -> Optional[str]:
        """Canonical name -> a product's legacy executor name (None if the
        product has no such tool)."""
        return self._canon_to_legacy.get(product, {}).get(canonical_name)

    # ---- provider serialization ----
    def to_openai_tools(self, allowed: Optional[Iterable[str]] = None) -> list[dict]:
        """OpenAI-style ``tools`` array using canonical flat names.

        ``allowed`` (canonical names) restricts the set — this is the
        per-context allow-list pattern (Claude Code subagents / Helpwo skill
        allowedTools): scope by selection, never by renaming.
        """
        allow = set(allowed) if allowed is not None else None
        out = []
        for t in self._tools:
            if allow is not None and t["name"] not in allow:
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("params") or {"type": "object", "properties": {}},
                },
            })
        return out


_cache: Optional[Catalog] = None


def load(path: str = _CATALOG_PATH) -> Catalog:
    """Load (and cache) the catalog."""
    global _cache
    if _cache is None:
        with open(path, "r", encoding="utf-8") as f:
            _cache = Catalog(json.load(f))
    return _cache


def reload(path: str = _CATALOG_PATH) -> Catalog:
    global _cache
    _cache = None
    return load(path)
