"""Extension: code-map

Laintas Code Map from the terminal and from the model: queue a build of a
public GitHub repository, watch it, and read the finished map as text.

Why an extension rather than a built-in
---------------------------------------
Code Map used to ship in the core: `code_map.py` plus five tools registered
on every request, for every user, whether or not the account had ever built a
map. That put five schemas in the cached prefix of every conversation, and it
put the model in the position of having to *discover* whether a map existed
before it could decide to use one.

As an extension, presence carries the answer. Install it and the `code_map.*`
schemas are in front of the model, so a repository question starts by asking
the map. Do not install it and the tools are simply absent -- nothing to
probe, nothing to explain, and code reading falls back to `grep`/`read` with
no instruction needed. The runtime states the fact; the model never spends a
turn establishing it.

Install
-------
    /extensions install extensions/code-map --global

It also ships its own skill: `skills/code-map/SKILL.md` carries the method
(read before you build, what a map is not), so the guidance arrives with the
tools and leaves with them.

Commands
--------
    /codemap                              list the account's maps
    /codemap build <url> [ref] [--model <id>]   queue a build
    /codemap status <id>                  progress of one build
    /codemap read <id> [node]             the finished map as text
    /codemap delete <id>                  free the slot it holds

A build runs for minutes to hours on the server, so nothing here waits on
one: `build` queues it and prints an id, and `read` collects it later.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Optional

_ctx: Any = None
_client: Any = None


def _cm():
    """The Code Map HTTP client, loaded by path.

    By path rather than by name: the extension host loads main.py under a
    generated module name, so a bare `import client` would resolve against
    whatever sys.path happens to hold.
    """
    global _client
    if _client is None:
        spec = importlib.util.spec_from_file_location(
            "code_map_client", str(Path(__file__).with_name("client.py")))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _client = module
    return _client


def _say(text: str) -> None:
    if _ctx is not None and _ctx.console is not None:
        _ctx.console.print(text)
    else:
        print(text)


def _escape(text: str) -> str:
    try:
        from rich.markup import escape
    except ImportError:
        return str(text)
    return escape(str(text))


# ── Tools ───────────────────────────────────────────────────────────────

def _call(action, params: dict) -> dict:
    """Run one Code Map action, turning its refusals into readable errors.

    Code Map states why it refused -- one build at a time, quota full, unknown
    model -- and those sentences are what the agent should read back to the
    user, so they are passed through rather than replaced with a status code.
    """
    cm = _cm()
    try:
        return {"ok": True, **action(cm, params)}
    except cm.CodeMapError as problem:
        return {"ok": False, "error": str(problem)}
    except Exception as problem:  # noqa: BLE001 - a tool never raises at the loop
        return {"ok": False, "error": f"code map failed: {problem}"}


def _tool_build(params: dict, ctx) -> dict:
    """Queue a map of a public GitHub repository. Does not wait for it."""
    def run(cm, p):
        prompts = p.get("prompts") if isinstance(p.get("prompts"), dict) else None
        job = cm.build(str(p.get("repo_url") or "").strip(),
                       str(p.get("ref") or "HEAD").strip(),
                       title=str(p.get("title") or "").strip(),
                       model=str(p.get("model") or "").strip(),
                       prompts=prompts)
        return {"map_id": job.get("id"), "status": job.get("status"),
                "title": job.get("title"),
                "note": "Building takes minutes to hours. Poll code_map.status; "
                        "do other work meanwhile rather than waiting in a loop."}
    return _call(run, params)


def _tool_status(params: dict, ctx) -> dict:
    def run(cm, p):
        job = cm.status(str(p.get("map_id") or "").strip())
        return {"status": job.get("status"), "progress": job.get("progress"),
                "step": job.get("step"), "error": job.get("error") or "",
                "title": job.get("title")}
    return _call(run, params)


def _tool_list(params: dict, ctx) -> dict:
    def run(cm, p):
        return {"maps": [
            {"map_id": job.get("id"), "title": job.get("title"),
             "repo": job.get("source_url"), "ref": job.get("source_ref"),
             "status": job.get("status")}
            for job in cm.maps()],
            "capacity": cm.summarize_capacity(cm.capacity())}
    return _call(run, params)


def _tool_read(params: dict, ctx) -> dict:
    def run(cm, p):
        text = cm.outline(str(p.get("map_id") or "").strip(),
                          str(p.get("node") or "").strip())
        return {"outline": text or "No such node in this map."}
    return _call(run, params)


def _tool_delete(params: dict, ctx) -> dict:
    def run(cm, p):
        cm.delete(str(p.get("map_id") or "").strip())
        return {"deleted": True}
    return _call(run, params)


_TOOLS = [
    ("build",
     "Build a layered architecture map of a public GitHub repository on "
     "Laintas Code Map. Returns a map id immediately; the build itself takes "
     "minutes to hours and is billed to the user's account. Use it when a "
     "repository is too large to read file by file and the question is how it "
     "is put together. For code already checked out locally, read the files "
     "instead - this maps a remote repository, not the working directory.",
     {"type": "object", "properties": {
         "repo_url": {"type": "string", "description": "https://github.com/owner/repository"},
         "ref": {"type": "string", "description": "branch, tag or commit (default HEAD)"},
         "title": {"type": "string", "description": "display name; defaults to the repository name"},
         "model": {"type": "string", "description": "model id from code_map.list capacity/models; omit for the default"},
         "prompts": {"type": "object",
                     "description": "replace a stage's prompt: keys l1_brief (architecture brief), "
                                    "l1_plan (top layer), l2_design (module layer). Omit to use the built-ins."},
     }, "required": ["repo_url"], "additionalProperties": False},
     _tool_build),
    ("status",
     "How far a queued Code Map build has got. Poll this occasionally rather "
     "than in a tight loop - a build takes minutes to hours, so do other work "
     "between checks and tell the user it is running.",
     {"type": "object", "properties": {
         "map_id": {"type": "string", "description": "id from code_map.build"},
     }, "required": ["map_id"], "additionalProperties": False},
     _tool_status),
    ("list",
     "The account's code maps and how many more it may keep.",
     {"type": "object", "properties": {}, "additionalProperties": False},
     _tool_list),
    ("read",
     "Read a finished map as text. With no node: the whole system - what it "
     "is, every part with its summary, and the arrows between them, in about "
     "15 KB. With node='l1:<id>': that part's components. With "
     "node='l2:<part>:<component>': its declarations with file:line, which is "
     "where to start reading actual source. Prefer this over fetching diagrams: "
     "the diagrams carry layout coordinates you cannot use.",
     {"type": "object", "properties": {
         "map_id": {"type": "string"},
         "node": {"type": "string", "description": "a box id from a previous read; omit for the whole map"},
     }, "required": ["map_id"], "additionalProperties": False},
     _tool_read),
    ("delete",
     "Delete one of the account's code maps, freeing the slot it holds. "
     "Ask the user first: a map costs model calls to rebuild.",
     {"type": "object", "properties": {
         "map_id": {"type": "string"},
     }, "required": ["map_id"], "additionalProperties": False},
     _tool_delete),
]


# ── Slash command ───────────────────────────────────────────────────────

def _tail(raw_line: str, drop: int) -> str:
    """The raw remainder after `drop` whitespace-delimited words.

    The host hands a command handler the WHOLE line, `/codemap` included,
    where the built-in dispatcher used to hand over the arguments alone. A URL
    or a node id must survive intact, so the tail is sliced rather than
    re-joined from a token list.
    """
    rest = str(raw_line or "")
    for _ in range(drop):
        parts = rest.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    return rest.strip()


def _cmd_codemap(parts: list, raw_line: str = "") -> None:
    """Laintas Code Map from the prompt: queue a build, watch it, read it.

    Deliberately not a blocking wait. A build runs for minutes to hours on the
    server, and a terminal command that sat on that would be a terminal the
    user cannot use - so the build is queued and its id is printed, and the
    same command reads it back later.
    """
    cm = _cm()
    action = parts[1].strip().lower() if len(parts) > 1 else "list"
    rest = _tail(raw_line, 2)

    try:
        if action in ("list", "ls"):
            jobs = cm.maps()
            if not jobs:
                _say("[dim]No code maps yet. /codemap build <github url>[/dim]")
            for job in jobs:
                _say(_escape(cm.describe(job)))
            _say(f"[dim]{_escape(cm.summarize_capacity(cm.capacity()))}[/dim]")

        elif action == "build":
            words = rest.split()
            if not words:
                _say("[yellow]Usage: /codemap build <github url> [ref] "
                     "[--model <id>][/yellow]")
                return
            model = ""
            if "--model" in words:
                at = words.index("--model")
                model = words[at + 1] if at + 1 < len(words) else ""
                words = words[:at] + words[at + 2:]
            job = cm.build(words[0], words[1] if len(words) > 1 else "HEAD", model=model)
            _say(f"[green]Queued[/green] {_escape(str(job.get('id')))} - "
                 f"{_escape(str(job.get('title')))}")
            _say("[dim]Takes minutes to hours. /codemap status <id>[/dim]")

        elif action in ("status", "st"):
            if not rest:
                _say("[yellow]Usage: /codemap status <id>[/yellow]")
                return
            _say(_escape(cm.describe(cm.status(rest.split()[0]))))

        elif action in ("read", "show"):
            words = rest.split()
            if not words:
                _say("[yellow]Usage: /codemap read <id> [node][/yellow]")
                return
            _say(_escape(cm.outline(words[0], words[1] if len(words) > 1 else "")
                         or "No such node in this map."))

        elif action in ("delete", "rm"):
            if not rest:
                _say("[yellow]Usage: /codemap delete <id>[/yellow]")
                return
            cm.delete(rest.split()[0])
            _say("[green]Deleted.[/green]")

        else:
            _say("[yellow]Usage: /codemap [list|build <url> [ref] "
                 "[--model <id>]|status <id>|read <id> [node]|delete <id>]"
                 "[/yellow]")
    except cm.CodeMapError as problem:
        _say(f"[red]{_escape(str(problem))}[/red]")


# ── Registration ────────────────────────────────────────────────────────

def setup(ctx) -> None:
    global _ctx
    _ctx = ctx

    # The Tool form, not the (name, handler, spec) form: these handlers return
    # the {"ok": ..., "error": ...} envelope the loop reads, and the plain
    # form would wrap that inside a second {"ok": True, "result": ...} -- so
    # the server's own refusal sentence would stop being the tool's error.
    from tools import Tool

    for name, description, schema, invoke in _TOOLS:
        ctx.register_tool(Tool(
            name=name, description=description, schema=schema, invoke=invoke))

    # The method for using these tools ships with them. A bundled skill
    # describing `code_map.*` would keep describing them after the extension
    # was removed -- prose the model cannot tell from a capability it has.
    ctx.register_skills()

    ctx.register_command(
        "/codemap", _cmd_codemap,
        description="Build and read a Laintas Code Map of a GitHub repository",
        subcommands=[
            ("list", "the account's maps and remaining slots"),
            ("build", "queue a build: <url> [ref] [--model <id>]"),
            ("status", "progress of one build: <id>"),
            ("read", "the finished map as text: <id> [node]"),
            ("delete", "free the slot a map holds: <id>"),
        ])


def teardown() -> None:
    global _ctx, _client
    _ctx = None
    _client = None
