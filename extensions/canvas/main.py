"""The /canvas whiteboard, as an extension.

Whiteboards were built into the CLI: four modules, a slash command, and four
tools the model can draw with. Nothing else in the product depends on them —
`infinite_canvas` borrows `workflow_viz` for its glyphs and that is the only
edge — so they are a feature, not infrastructure, and they belong in a package
a user installs rather than in every copy of the agent.

What moved, unchanged in behaviour:

  canvas.py           the .excalidraw file format, read/write/describe
  canvas_edit.py      the editor: draw, label, move, erase, review turns
  canvas_view.py      the full-screen viewer (prompt_toolkit)
  infinite_canvas.py  the pan/zoom scene the viewer renders
  /canvas             the command, with its verbs and its help
  canvas.list/read/draw/update    the four tools the model uses

The command still leans on the CLI for the things the CLI owns: argument
splitting, the Helpwo sub-terminal launcher, the console. An extension runs
in-process with the same permissions, so it imports them directly — the same
arrangement the blindpick extension uses, and the reason the host's context is
documented as a convenience rather than a boundary.
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:                                   # pragma: no cover
    from tools import ToolCtx

_ctx: Any = None


def _console():
    """The console the host handed us; never import the CLI's global."""
    return _ctx.console


def _cli():
    """The CLI module, for the parts of a slash command it owns."""
    import laintas_cli
    return laintas_cli


def _cmd_canvas(raw_args: str) -> None:
    """/canvas — whiteboards, from the terminal.

    Bare `/canvas` opens a canvas straight away, making a board to open if
    there is not already an empty one: typing the command is the whole
    request, and being asked for a filename first is the thing that made this
    feel like it needed a file before it would do anything. Existing boards
    are still one keystroke away — `b` inside the viewer, `/canvas list`
    outside it.

    Reads and creates; it does not edit. A board Helpwo has open lives in the
    editor with the file trailing behind it, so a write from here would be
    overwritten by its next autosave without either side noticing. Editing
    waits for the two to agree on who holds a board — see canvas.py.
    """
    from . import canvas as canvas_mod

    verb, rest = _cli()._split_verb(
        raw_args, ("new", "list", "open", "text", "help"),
        canvas_mod.is_canvas_path)

    if verb == "help":
        _console().print(r"[bold]/canvas[/bold] [dim]— whiteboards (.excalidraw)[/dim]")
        _console().print(r"  /canvas                   open a canvas (makes a board if needed)")
        _console().print(r"  /canvas <path>            view it (created if missing) on the infinite canvas")
        _console().print(r"  /canvas list              list the boards under this directory")
        _console().print(r"  /canvas text <path>       show what is on it, as text")
        _console().print(r"  /canvas new <path>        create a named board and open it")
        _console().print(r"  /canvas open \[path]       open the board in Helpwo, where you draw")
        _console().print(r"[dim]In the view: w draws — r/o/d shapes, a arrow, l line, "
                      r"p pencil, t text; c colour, f fill, 1-3 width; "
                      r"x deletes, u undoes, b switches board.[/dim]")
        _console().print(r"[dim]`open` starts the local Helpwo gateway for the full editor — "
                      r"a board changed there while this is open makes the next edit here "
                      r"refuse rather than overwrite it.[/dim]")
        return

    if not (raw_args or "").strip():
        _canvas_quick_start(canvas_mod)
        return

    if verb == "list":
        boards = canvas_mod.find_boards(os.getcwd())
        if not boards:
            _console().print("[dim]No .excalidraw boards under this directory. "
                          "Type /canvas to start one.[/dim]")
            return
        _console().print("[bold]Boards[/bold] [dim](run /canvas <path>, or /canvas "
                      "for a new one)[/dim]")
        for path in boards:
            try:
                scene = canvas_mod.read_scene(path)
                count = len(canvas_mod.live_elements(scene))
                pending = sum(t["count"] for t in canvas_mod.count_ai_turns(scene))
                mark = f" [yellow]{pending} unreviewed from the AI[/yellow]" if pending else ""
                _console().print(f"  [cyan]{os.path.relpath(path)}[/cyan] "
                              f"[dim]{count} element(s)[/dim]{mark}")
            except canvas_mod.CanvasError as e:
                _console().print(f"  [cyan]{os.path.relpath(path)}[/cyan] [red]{e}[/red]")
        return

    if verb == "new":
        path, _extra = _cli()._leading_path_arg(rest)
        if not path:
            _console().print(r"[yellow]/canvas new <name>.excalidraw[/yellow]")
            return
        if not canvas_mod.is_canvas_path(path):
            path += canvas_mod.CANVAS_EXTENSION
        if os.path.exists(os.path.expanduser(path)):
            _console().print(f"[yellow]{path} already exists — /canvas {path} to see it[/yellow]")
            return
        try:
            canvas_mod.write_scene(path, canvas_mod.empty_scene())
        except (canvas_mod.CanvasError, OSError) as e:
            _console().print(f"[red]{e}[/red]")
            return
        _console().print(f"[green]Created {path}[/green]")
        if _canvas_can_view():
            _canvas_view(path, canvas_mod.read_scene(path), canvas_mod)
        else:
            _console().print("[dim]Open it in Helpwo to draw: "
                          f"/canvas open {path}[/dim]")
        return

    if verb == "open":
        _canvas_open(_cli()._leading_path_arg(rest)[0], canvas_mod)
        return

    if verb and verb != "text":
        _console().print(f"[yellow]/canvas: unknown action '{verb}'. Try /canvas help[/yellow]")
        return

    path, _extra = _cli()._leading_path_arg(rest)
    if not path:
        _console().print(r"[yellow]/canvas text <path>  —  a board path is required[/yellow]")
        return
    try:
        scene = canvas_mod.read_scene(path)
    except canvas_mod.CanvasError as e:
        # A missing board is the one error worth recovering from: the user
        # named a board they want, so create it instead of sending them to
        # `new`. Every other error (bad extension, bad JSON) still stops.
        expanded = os.path.expanduser(path)
        if (canvas_mod.is_canvas_path(expanded)
                and not os.path.exists(expanded)):
            try:
                canvas_mod.write_scene(expanded, canvas_mod.empty_scene())
                scene = canvas_mod.empty_scene()
                _console().print(f"[green]Created {path}[/green] "
                              f"[dim]— draw on it in Helpwo with /canvas open {path}[/dim]")
            except (canvas_mod.CanvasError, OSError) as create_err:
                _console().print(f"[red]{create_err}[/red]")
                return
        else:
            _console().print(f"[red]{e}[/red]")
            return

    # Looking at a board is the default; the text dump stays one word away.
    # It is not merely a fallback for a dumb terminal either — the ids in it
    # are what an edit would name, and no viewport shows you those.
    if verb != "text" and _canvas_can_view():
        if _canvas_view(path, scene, canvas_mod):
            return

    _console().print(f"[bold]{path}[/bold]")
    _console().print(canvas_mod.describe_scene(scene), markup=False, highlight=False)
    pending = canvas_mod.count_ai_turns(scene)
    if pending:
        total = sum(t["count"] for t in pending)
        _console().print(f"[yellow]{total} element(s) added by the AI are still "
                      f"unreviewed — accept or undo them in Helpwo.[/yellow]")


def _canvas_can_view() -> bool:
    """A full-screen view needs a real terminal on both ends."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _canvas_quick_start(canvas_mod) -> None:
    """Bare `/canvas`: put a canvas on the screen, with nothing else asked.

    A board is made when there is not already an empty one to reuse, so
    running this five times leaves one file rather than five. Whatever else
    is in the directory stays one keystroke away — `b` in the viewer.
    """
    try:
        path, needs_creating = canvas_mod.scratch_board(os.getcwd())
    except OSError as e:
        _console().print(f"[red]{e}[/red]")
        return
    if needs_creating:
        try:
            canvas_mod.write_scene(path, canvas_mod.empty_scene())
        except (canvas_mod.CanvasError, OSError) as e:
            _console().print(f"[red]{e}[/red]")
            return
        _console().print(f"[green]New board {os.path.relpath(path)}[/green]")
    else:
        _console().print(f"[dim]Reusing the empty board "
                      f"{os.path.relpath(path)}[/dim]")

    others = [b for b in canvas_mod.find_boards(os.getcwd())
              if os.path.abspath(b) != os.path.abspath(path)]
    try:
        scene = canvas_mod.read_scene(path)
    except canvas_mod.CanvasError as e:
        _console().print(f"[red]{e}[/red]")
        return

    if _canvas_can_view() and _canvas_view(path, scene, canvas_mod):
        return
    # No terminal to take over (piped stdin, a test, a dumb terminal): say
    # what exists instead of opening nothing.
    _console().print(canvas_mod.describe_scene(scene), markup=False, highlight=False)
    _console().print(f"[dim]Draw on it in Helpwo: /canvas open "
                  f"{os.path.relpath(path)}[/dim]")
    if others:
        _console().print(f"[dim]{len(others)} other board(s) — /canvas list[/dim]")


def _canvas_view(path: str, scene: dict, canvas_mod) -> bool:
    """Open a board on the infinite canvas. False = fall back to text."""
    try:
        from . import canvas_view
        from . import infinite_canvas
    except Exception as e:                       # pragma: no cover - import guard
        _console().print(f"[dim]canvas view unavailable ({e}); showing text[/dim]")
        return False
    data = canvas_mod.to_canvas_scene(scene, title=os.path.basename(path))
    built = infinite_canvas.scene_from_json(data)

    def load(other_path: str):
        """Open another board without leaving the session."""
        other = canvas_mod.to_canvas_scene(
            canvas_mod.read_scene(other_path),
            title=os.path.basename(other_path))
        return (infinite_canvas.scene_from_json(other),
                os.path.relpath(other_path))

    # Drawing is offered when the board can actually be written; a board that
    # cannot be opened for editing is still perfectly viewable, so a failure
    # here costs the drawing keys and nothing else.
    editor = None
    try:
        from . import canvas_edit
        editor = canvas_edit.BoardEditor(os.path.expanduser(path), canvas_mod)
    except Exception as exc:                     # unreadable, or no module
        _console().print(f"[dim]drawing unavailable ({type(exc).__name__}: "
                      f"{exc})[/dim]")

    def reload_scene():
        """The scene again, after an edit — one place that rebuilds it."""
        fresh = canvas_mod.to_canvas_scene(
            editor.scene, title=os.path.basename(path))
        return infinite_canvas.scene_from_json(fresh)

    boards = [b for b in canvas_mod.find_boards(os.getcwd())
              if os.path.abspath(b) != os.path.abspath(os.path.expanduser(path))]
    hints = [f"w — draw here (r rect · o ellipse · d diamond · t text)"
             if editor else
             f"draw on it in Helpwo:  /canvas open {os.path.relpath(path)}",
             "b — open another board" if boards else "",
             "q — close"]
    try:
        return canvas_view.open_scene(
            built, title=os.path.relpath(path),
            allow_empty=True, boards=boards, load_board=load,
            editor=editor,
            reload_scene=(reload_scene if editor else None),
            empty_hint=hints)
    except Exception as exc:
        # A viewer that dies takes the screen with it; the board is still
        # readable, so say what happened and print it rather than leaving the
        # user with a traceback and nothing.
        _console().print(f"[yellow]canvas view failed "
                      f"({type(exc).__name__}: {exc}); showing text[/yellow]")
        return False


def _canvas_open(path: str, canvas_mod) -> None:
    """Hand the user a way into the drawing surface.

    A whiteboard cannot be shown in a terminal, and the gap between "the file
    exists" and "I can draw on it" is exactly where this feature was invisible.
    The local Helpwo gateway mounts the working directory as a workspace, so a
    board created here is already in its file tree — all that was missing was
    somebody saying so.
    """
    if path:
        if not canvas_mod.is_canvas_path(path):
            path += canvas_mod.CANVAS_EXTENSION
        if not os.path.exists(os.path.expanduser(path)):
            _console().print(f"[yellow]No board at {path} — /canvas new {path} first.[/yellow]")
            return

    try:
        import helpwo_server
    except ImportError:
        _console().print("[red]The Helpwo gateway is not available in this build.[/red]")
        return

    if helpwo_server.is_running():
        url = helpwo_server.get_url(with_token=True)
    elif _cli()._hosts_helpwo_here():
        _console().print("[yellow]Helpwo is not running in this sub-terminal. "
                      "Run /helpwo here, then open the board from its file tree.[/yellow]")
        return
    else:
        # Helpwo lives in its own sub-terminal; wait for it, since the whole
        # point of this command is the URL.
        try:
            runtime = _cli()._launch_app_subterminal(
                app_host.HELPWO_APP, persistent=True, options={},
                agent_registry=None, open_url=False, wait=True) or {}
        except Exception as e:
            _console().print(f"[red]Could not start it: {type(e).__name__}: {e}[/red]")
            _console().print("[dim]Run /helpwo yourself, then open the board from its file tree.[/dim]")
            return
        url = runtime.get("open_url") or ""
        if not url:
            if runtime.get("status") == "launching":
                _console().print("[dim]Run /canvas open again once Helpwo reports ready.[/dim]")
            return

    _console().print(f"[bold]Open:[/bold] [cyan]{url}[/cyan]")
    if path:
        _console().print(f"[dim]This folder is mounted there as a workspace — open "
                      f"{os.path.relpath(os.path.expanduser(path))} from the file tree "
                      f"and it opens as a whiteboard.[/dim]")


# ── Tools ────────────────────────────────────────────────────────────────
# Registered under the `canvas.` prefix this extension claims in its manifest,
# so the model sees the same names it always has.


def _canvas_board(params: dict, ctx: ToolCtx, create: bool = False):
    """Resolve a board path against the working directory. (editor, error)."""
    import os
    from . import canvas as canvas_mod
    from . import canvas_edit

    raw = str(params.get("path") or "").strip()
    if not raw:
        return (None, "canvas: a board path is required")
    path = raw if os.path.isabs(raw) else os.path.join(ctx.cwd or os.getcwd(), raw)
    if not canvas_mod.is_canvas_path(path):
        path += canvas_mod.CANVAS_EXTENSION
    if create and not os.path.exists(path):
        try:
            canvas_mod.write_scene(path, canvas_mod.empty_scene())
        except (canvas_mod.CanvasError, OSError) as e:
            return (None, f"canvas: {e}")
    try:
        editor = canvas_edit.BoardEditor(
            path, canvas_mod, author="ai", turn=ctx.run_id or "cli-run")
    except canvas_mod.CanvasError as e:
        return (None, f"canvas: {e}")
    except OSError as e:
        return (None, f"canvas: {e}")
    return (editor, "")


def _bi_canvas_list(params: dict, ctx: ToolCtx) -> dict:
    """Boards under the working directory, newest first."""
    import os
    from . import canvas as canvas_mod
    boards = canvas_mod.find_boards(ctx.cwd or os.getcwd())
    if not boards:
        return {"ok": True, "result": "no .excalidraw boards here"}
    lines = []
    for path in boards:
        try:
            live = canvas_mod.live_elements(canvas_mod.read_scene(path))
            lines.append(f"{os.path.relpath(path, ctx.cwd or os.getcwd())}  "
                         f"{len(live)} element(s)")
        except canvas_mod.CanvasError as e:
            lines.append(f"{os.path.relpath(path)}  [{e}]")
    return {"ok": True, "result": "\n".join(lines)}


def _bi_canvas_read(params: dict, ctx: ToolCtx) -> dict:
    """What is on a board: ids, labels, and what each arrow connects."""
    from . import canvas as canvas_mod
    editor, error = _canvas_board(params, ctx)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True,
            "result": canvas_mod.describe_scene(editor.scene),
            "path": editor.path}


def _bi_canvas_draw(params: dict, ctx: ToolCtx) -> dict:
    """Add shapes (and the arrows between them) to a board in one write."""
    shapes = params.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        return {"ok": False, "error": "canvas.draw: shapes must be a non-empty list"}
    connect = params.get("connect")
    connect = connect if isinstance(connect, list) else []
    editor, error = _canvas_board(params, ctx, create=True)
    if error:
        return {"ok": False, "error": error}
    try:
        ok, message, names = editor.draw_batch(shapes, connect)
    except (ValueError, KeyError, TypeError) as e:
        return {"ok": False, "error": f"canvas.draw: {type(e).__name__}: {e}"}
    if not ok:
        return {"ok": False, "error": f"canvas.draw: {message}"}
    drawn = f"{len(shapes)} shape(s)"
    if connect:
        drawn += f", {len(connect)} arrow(s)"
    return {"ok": True,
            "result": f"drew {drawn} on {editor.path}\n"
                      f"ids: {names}" if names else f"drew {drawn} on {editor.path}",
            "ids": names}


def _bi_canvas_update(params: dict, ctx: ToolCtx) -> dict:
    """Relabel, move or erase elements that are already on a board."""
    editor, error = _canvas_board(params, ctx)
    if error:
        return {"ok": False, "error": error}

    from . import canvas_edit
    elements = list(editor.elements)
    done: list[str] = []
    missing: list[str] = []

    for entry in (params.get("label") or []):
        element_id = str(entry.get("id") or "")
        if editor._in(elements, element_id) is None:
            missing.append(element_id)
            continue
        elements = canvas_edit.label(elements, element_id,
                                    str(entry.get("text") or ""),
                                    author=editor.author)
        done.append(f"labelled {element_id}")
    for entry in (params.get("move") or []):
        element_id = str(entry.get("id") or "")
        if editor._in(elements, element_id) is None:
            missing.append(element_id)
            continue
        elements = canvas_edit.move(elements, element_id,
                                    float(entry.get("dx") or 0),
                                    float(entry.get("dy") or 0))
        done.append(f"moved {element_id}")
    for element_id in (params.get("erase") or []):
        element_id = str(element_id)
        if editor._in(elements, element_id) is None:
            missing.append(element_id)
            continue
        elements = canvas_edit.delete(elements, element_id)
        done.append(f"erased {element_id}")

    if not done:
        return {"ok": False,
                "error": ("canvas.update: nothing to do"
                          + (f"; no such element: {', '.join(missing)}"
                             if missing else ""))}
    ok, message = editor.apply(elements)
    if not ok:
        return {"ok": False, "error": f"canvas.update: {message}"}
    result = "; ".join(done)
    if missing:
        result += f" (not found: {', '.join(missing)})"
    return {"ok": True, "result": result}

# ── Registration ─────────────────────────────────────────────────────────

#: Keywords that should make the drawing tools visible to the model. The
#: router matches an extension's tools by name and description on its own, but
#: "whiteboard", "diagram" and "mind map" never appear in those strings, and
#: they are exactly what a person says when they want one.
_ROUTER_KEYWORDS = ("canvas", "diagram", "whiteboard", "mind map")

#: Argument shapes for `/canvas`, so completion and arity checking work the
#: same way they do for a built-in command.
_ARG_RULES = (
    ("", 1, "/canvas [<path>|text <path>|new <path>|open [path]|list]"),
    ("list", 1, "/canvas list"),
    ("help", 1, "/canvas help"),
    ("new", 2, "/canvas new <path>"),
    ("open", 2, "/canvas open [path]"),
    ("text", 2, "/canvas text <path>"),
)

#: A board is a file, so the argument completes like one.
_FILE_ARGUMENTS = (
    ("", (".excalidraw",)),
    ("new", (".excalidraw",)),
    ("open", (".excalidraw",)),
    ("text", (".excalidraw",)),
)


def handle(parts: list, raw_line: str = "") -> None:
    """`/canvas …` — the host passes the split parts and the raw line."""
    raw = str(raw_line or "")
    _, _, rest = raw.partition(" ")
    if not rest.strip() and len(parts) > 1:
        rest = " ".join(str(p) for p in parts[1:])
    _cmd_canvas(rest.strip())


def _canvas_tools() -> list:
    from tools import Tool
    return [
        Tool(
            name="canvas.list",
            description=(
                "List the whiteboards (.excalidraw files) under the working "
                "directory. A board is where a diagram lives that a person "
                "will look at and edit — use it for architecture sketches, "
                "flows and layouts, not for anything you would rather write "
                "as text."),
            schema={"type": "object", "properties": {}},
            capabilities=frozenset({"fs.read"}),
            invoke=_bi_canvas_list,
        ),
        Tool(
            name="canvas.read",
            description=(
                "Read what is on a board: every element's id, its label, and "
                "what each arrow connects. Read before you update — the ids "
                "in this listing are the ones canvas.update needs."),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "board path, e.g. flow.excalidraw"},
                },
                "required": ["path"],
            },
            capabilities=frozenset({"fs.read"}),
            invoke=_bi_canvas_read,
        ),
        Tool(
            name="canvas.draw",
            description=(
                "Draw on a board (created if it does not exist), in one "
                "write. Not only box-and-arrow diagrams: `line` and "
                "`freedraw` take a list of points, so you can draw a curve, "
                "an axis, a sketch, a route — anything a path describes — and "
                "every element takes colour, fill and stroke width. For "
                "diagrams: give each shape a short `id` of your own and use "
                "those ids in `connect`, without reading the file back first; "
                "shapes with no coordinates are laid out in rows below "
                "whatever is already on the board. What you draw is marked as "
                "yours, so the person can review or undo it in the editor."),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "board path, e.g. flow.excalidraw"},
                    "shapes": {
                        "type": "array",
                        "description": "shapes to add, in reading order",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "your name for it, used in connect"},
                                "kind": {"type": "string", "enum": ["rectangle", "ellipse", "diamond", "text", "line", "freedraw"]},
                                "label": {"type": "string", "description": "text on the shape (or the text itself for kind=text)"},
                                "x": {"type": "number"}, "y": {"type": "number"},
                                "width": {"type": "number"}, "height": {"type": "number"},
                                "points": {"type": "array",
                                           "description": "for line/freedraw: [[x,y], …] in board coordinates, at least two",
                                           "items": {"type": "array", "items": {"type": "number"}}},
                                "color": {"type": "string", "description": "stroke colour, e.g. #1971c2"},
                                "background": {"type": "string", "description": "fill colour, e.g. #a5d8ff"},
                                "fill": {"type": "string", "enum": ["solid", "hachure", "cross-hatch"]},
                                "strokeWidth": {"type": "number", "description": "1 thin, 2 medium, 4 thick"},
                                "strokeStyle": {"type": "string", "enum": ["solid", "dashed", "dotted"]},
                                "opacity": {"type": "number", "description": "0-100"},
                                "sloppy": {"type": "boolean", "description": "true = hand-drawn look, false = clean lines"},
                            },
                            "required": ["kind"],
                        },
                    },
                    "connect": {
                        "type": "array",
                        "description": "arrows: from/to are shape ids from this call or from canvas.read",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"}, "to": {"type": "string"},
                                "label": {"type": "string"},
                            },
                            "required": ["from", "to"],
                        },
                    },
                },
                "required": ["path", "shapes"],
            },
            capabilities=frozenset({"fs.read", "fs.write"}),
            invoke=_bi_canvas_draw,
        ),
        Tool(
            name="canvas.update",
            description=(
                "Change elements already on a board: relabel, move by an "
                "offset, or erase. Ids come from canvas.read. Erasing leaves "
                "the element recoverable in the editor rather than shredding "
                "it."),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "label": {
                        "type": "array",
                        "items": {"type": "object",
                                  "properties": {"id": {"type": "string"},
                                                 "text": {"type": "string"}},
                                  "required": ["id", "text"]},
                    },
                    "move": {
                        "type": "array",
                        "items": {"type": "object",
                                  "properties": {"id": {"type": "string"},
                                                 "dx": {"type": "number"},
                                                 "dy": {"type": "number"}},
                                  "required": ["id"]},
                    },
                    "erase": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["path"],
            },
            capabilities=frozenset({"fs.read", "fs.write"}),
            invoke=_bi_canvas_update,
        ),
    ]


def setup(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_command(
        "canvas", handle,
        description="Whiteboards: open a canvas, draw on it, list boards",
        subcommands=[
            ("<path>", "View that board on the infinite canvas (created if missing)"),
            ("list", "List the boards under this directory"),
            ("text <path>", "Show what is on it, as text"),
            ("new <path>", "Create a named board and open it"),
            ("open [path]", "Open the board in Helpwo, where you draw"),
        ],
        arg_rules=_ARG_RULES,
        file_arguments=_FILE_ARGUMENTS)
    for tool in _canvas_tools():
        ctx.register_tool(tool)
    try:
        import context_router
        context_router.register_group(_ROUTER_KEYWORDS, ("canvas.",))
    except Exception:
        # Routing is an optimization: without it the tools are still offered
        # whenever the query overlaps their names or descriptions.
        pass


def teardown() -> None:
    global _ctx
    try:
        import context_router
        context_router.unregister_group(("canvas.",))
    except Exception:
        pass
    _ctx = None
