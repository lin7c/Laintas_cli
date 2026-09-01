"""Deterministic, zero-network routing for task-specific tool schemas.

Authorization remains in ``agent_loop``.  This module only decides which of
the authorized tools are useful enough to advertise to the model for a task.
Keeping that distinction explicit prevents a routing miss from becoming a
security decision: dispatch still checks the authoritative allow-list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


# Small, general-purpose surface needed by ordinary inspect/edit/run tasks.
CORE_TOOLS = frozenset({
    "fs.read", "fs.ls", "fs.glob", "fs.grep", "fs.diff",
    "fs.edit", "fs.multi_edit", "fs.write", "shell.exec",
    # Capability discovery must never disappear behind capability routing.
    # Web search/fetch also remain resident: current and recommendation intent
    # is too often implicit for a lexical router to hide them safely.
    "tool.search", "web.search", "web.fetch",
    "skill.list", "skill.load", "skill.unload",
    "mem.list", "mem.read", "mem.save",
    "task.complete", "task.create", "task.get", "task.list", "task.update",
    "agent_return", "workflow.phase_complete", "time.now",
    # Delegation is resident, not routed. Whether to hand part of a task to a
    # child is a judgement about the SHAPE of the work — are these pieces
    # independent — and the keyword groups below can only see the vocabulary of
    # the mechanism ("parallel", "sub-task", "delegate"). Users describe the
    # work, not the mechanism, so "fix the failing tests and update the docs"
    # routed no spawn tool at all: the prompt told the model to delegate
    # independent work while the schema for doing it was withheld. Three
    # schemas is a cheap price for closing that gap. The rest of the agent
    # surface (hire, station, tell, abort, …) stays routed.
    "agent.spawn", "agent.wait", "await_spawns",
})


_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("web", "internet", "online", "search", "latest", "current", "news",
      "recommend", "recommended", "best", "popular", "well-known",
      "evidence", "source", "paper", "research"),
     ("web.",)),
    (("browser", "website", "page", "dom", "playwright", "screenshot", "ui test"),
     ("browser.",)),
    (("canvas", "diagram", "whiteboard", "mind map"),
     ("canvas.",)),
    (("image", "photo", "picture", "ocr", "vision"),
     ("image.",)),
    (("generate image", "generate video", "video", "media"),
     ("media.",)),
    (("agent", "delegate", "parallel", "worker", "subagent", "team", "hire"),
     ("agent.", "agent_", "spawn", "await_spawns", "hwo", "hwg")),
    (("terminal", "repl", "process", "service", "server", "daemon", "logs", "port", "deploy"),
     ("terminal.", "session.", "sleep")),
    # Storage used to route on nothing at all: the one tool that reached the
    # cloud folder (file_push) hung off the "deploy" group, so a user asking
    # to put a report in their cloud folder got no storage schema whatsoever.
    (("storage", "upload", "download", "cloud", "cloud folder", "shared folder",
      "share this file", "send me the file", "helpwo workspace", "quota",
      "disk space", "allowance"),
     ("storage.", "file_push")),
    (("plan", "roadmap", "milestone", "objective", "resume", "where were we",
      "progress"),
     ("plan.", "workflow.", "work.status")),
    (("task", "todo", "issue"), ("task.",)),
    (("rule", "always", "from now on", "remember to"),
     ("rule.",)),
    (("delete", "remove", "erase"),
     ("fs.delete", "mem.delete")),
    (("backup", "snapshot", "restore", "rollback"),
     ("snapshot.",)),
    (("login", "identity", "account", "credential"),
     ("identity.",)),
    (("contract", "interface agreement", "mock"), ("contract.",)),
    (("prompt", "system prompt"), ("prompt.",)),
    (("evolve", "experiment"), ("evolve.",)),
    (("ppos",), ("ppos.",)),
    (("cost", "spend", "budget", "balance", "quota", "usage", "tokens",
      "how much", "expensive", "bill", "subscription"),
     ("account.",)),
    (("failed", "failure", "error", "crashed", "why did", "diagnose",
      "went wrong"),
     ("diag.",)),
    (("policy", "permission", "allowed", "approval", "blocked", "denied",
      "safe to run", "am i allowed"),
     ("policy.",)),
)


# Product-authored text remains English-only. Escaped aliases preserve routing
# for non-English user input without placing localized text in prompts, UI, or
# source assets. Values are the English concepts consumed by _GROUPS.
_INPUT_ALIASES = {
    "\u7f51\u9875": "web", "\u8054\u7f51": "online", "\u641c\u7d22": "search",
    "\u67e5\u8be2": "search", "\u6700\u65b0": "latest", "\u76ee\u524d": "current",
    "\u5f53\u524d": "current", "\u65b0\u95fb": "news", "\u63a8\u8350": "recommend",
    "\u516c\u8ba4": "popular", "\u6700\u597d": "best", "\u8d44\u6599": "source",
    "\u4f9d\u636e": "evidence", "\u6765\u6e90": "source", "\u8bba\u6587": "paper",
    "\u7814\u7a76": "research", "\u6d4f\u89c8\u5668": "browser", "\u7f51\u7ad9": "website",
    "\u9875\u9762": "page", "\u622a\u56fe": "screenshot", "\u754c\u9762\u6d4b\u8bd5": "ui test",
    "\u753b\u5e03": "canvas", "\u6d41\u7a0b\u56fe": "diagram", "\u767d\u677f": "whiteboard",
    "\u8111\u56fe": "mind map", "\u56fe\u7247": "image", "\u56fe\u50cf": "image",
    "\u7167\u7247": "photo", "\u8bc6\u56fe": "vision", "\u751f\u6210\u56fe\u7247": "generate image",
    "\u751f\u6210\u89c6\u9891": "generate video", "\u89c6\u9891": "video", "\u4ee3\u7406": "agent",
    "\u59d4\u6d3e": "delegate", "\u5e76\u884c": "parallel", "\u5b50\u4efb\u52a1": "subagent",
    "\u56e2\u961f": "team", "\u5458\u5de5": "worker", "\u7ec8\u7aef": "terminal",
    "\u8fdb\u7a0b": "process", "\u670d\u52a1": "service", "\u670d\u52a1\u5668": "server",
    "\u65e5\u5fd7": "logs", "\u7aef\u53e3": "port", "\u90e8\u7f72": "deploy",
    "\u89c4\u5212": "plan", "\u8ba1\u5212": "plan", "\u91cc\u7a0b\u7891": "milestone",
    "\u4efb\u52a1": "task", "\u5f85\u529e": "todo", "\u5de5\u5355": "issue",
    "\u89c4\u5219": "rule", "\u4ee5\u540e\u90fd": "from now on", "\u6bcf\u6b21\u90fd": "always",
    "\u8bb0\u4f4f\u8981": "remember to", "\u6e05\u7406": "remove", "\u5220\u9664": "delete",
    "\u79fb\u9664": "remove", "\u5907\u4efd": "backup", "\u5feb\u7167": "snapshot",
    "\u6062\u590d": "restore", "\u56de\u6eda": "rollback", "\u767b\u5f55": "login",
    "\u8eab\u4efd": "identity", "\u8d26\u53f7": "account", "\u51ed\u8bc1": "credential",
    "\u5951\u7ea6": "contract", "\u63a5\u53e3\u7ea6\u5b9a": "interface agreement",
    "\u63d0\u793a\u8bcd": "prompt", "\u7cfb\u7edf\u63d0\u793a": "system prompt",
    "\u6539\u8fdb\u5b9e\u9a8c": "experiment", "\u6f14\u5316": "evolve",
    "\u5b58\u50a8": "storage", "\u4e91\u7aef": "cloud", "\u4e91\u76d8": "cloud",
    "\u4e0a\u4f20": "upload", "\u4e0b\u8f7d": "download", "\u7f51\u76d8": "cloud",
    "\u5171\u4eab\u6587\u4ef6": "shared folder", "\u5bb9\u91cf": "quota",
    "\u7a7a\u95f4": "disk space", "\u4f59\u989d": "balance",
    "\u82b1\u8d39": "cost", "\u8d39\u7528": "cost", "\u9884\u7b97": "budget",
    "\u7528\u91cf": "usage", "\u5931\u8d25": "failed", "\u62a5\u9519": "error",
    "\u4e3a\u4ec0\u4e48\u5931\u8d25": "why did", "\u591a\u5c11\u94b1": "cost",
    "\u82b1\u4e86": "spend", "\u8ba2\u9605": "subscription",
    "\u7b56\u7565": "policy", "\u6743\u9650": "permission",
    "\u5141\u8bb8": "allowed", "\u5ba1\u6279": "approval",
    "\u62e6\u622a": "blocked", "\u5b89\u5168\u9650\u5236": "policy",
}


def _normalize_query(text: str) -> str:
    value = str(text or "").casefold()
    concepts = [english for phrase, english in _INPUT_ALIASES.items() if phrase in value]
    return value + (" " + " ".join(concepts) if concepts else "")


def _contains(text: str, term: str) -> bool:
    if any("\u4e00" <= ch <= "\u9fff" for ch in term) or " " in term:
        return term in text
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _matches_name(name: str, selectors: Iterable[str]) -> bool:
    return any(name == selector or name.startswith(selector) for selector in selectors)


def _terms(text: str) -> set[str]:
    """Language-agnostic lexical features for local, zero-network routing."""
    value = _normalize_query(text)
    found = set(re.findall(r"[a-z0-9_-]{2,}", value))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        found.update(run[i:i + 2] for i in range(max(0, len(run) - 1)))
        if len(run) == 1:
            found.add(run)
    return found


def select_tool_names(query: str, tools: Iterable[object]) -> set[str]:
    """Return the useful schema names for ``query`` without any I/O.

    Loaded skill tools remain visible.  Other extension/MCP tools are selected
    only when their name, source, or description overlaps the task, capped to
    keep an extension bundle from recreating the full-catalog problem.
    """
    tool_list = list(tools)
    available = {str(getattr(tool, "name", "")) for tool in tool_list}
    selected = set(CORE_TOOLS) & available
    lowered = _normalize_query(query)

    for triggers, selectors in _GROUPS:
        if any(_contains(lowered, trigger) for trigger in triggers):
            selected.update(name for name in available if _matches_name(name, selectors))

    extension_matches: list[tuple[int, str]] = []
    query_terms = _terms(lowered)
    for tool in tool_list:
        name = str(getattr(tool, "name", ""))
        source = str(getattr(tool, "source", "builtin"))
        if source.startswith("skill:"):
            selected.add(name)
        elif name not in CORE_TOOLS and query_terms:
            haystack = " ".join((name, source, str(getattr(tool, "description", "")))).casefold()
            score = len(query_terms & _terms(haystack))
            if score:
                extension_matches.append((score, name))
    selected.update(name for _, name in sorted(extension_matches, reverse=True)[:8])
    return selected


def discover_tool_names(query: str, tools: Iterable[object], limit: int = 12) -> list[str]:
    """Return candidates for an explicit capability-discovery request.

    Group routing handles conceptual requests while lexical overlap covers
    future built-ins, MCP tools, and extensions without hard-coding them here.
    """
    tool_list = list(tools)
    routed = select_tool_names(query, tool_list) - set(CORE_TOOLS)
    wanted = _terms(query)
    scored: list[tuple[int, str]] = []
    for tool in tool_list:
        name = str(getattr(tool, "name", ""))
        haystack = " ".join((
            name,
            str(getattr(tool, "source", "builtin")),
            str(getattr(tool, "description", "")),
        ))
        score = len(wanted & _terms(haystack))
        if name in routed:
            score += 100
        if score:
            scored.append((score, name))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [name for _, name in scored[:max(1, int(limit))]]


def stable_visible_names(query: str, tools: Iterable[object], state: dict) -> set[str]:
    """Route once and grow monotonically for the lifetime of a task."""
    routed = select_tool_names(query, tools)
    prior = state.get("_dynamic_tool_names")
    if isinstance(prior, (list, set, tuple)):
        routed.update(str(name) for name in prior)
    state["_dynamic_tool_names"] = sorted(routed)
    return routed
