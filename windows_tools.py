"""windows_tools.py — the `win.*` surface, present only when it works.

These are thin: every one of them is a `win` frame to `helpwo-kernel.exe` and
back. The decisions — which mechanism answers, whether a tier permits it, what
a self-drawn window does — all live in the kernel, so that Helpwo's browser
agent and this CLI cannot drift apart on them.

Registration is conditional on purpose. The tools appear when a kernel is
connected and its probe says the tier is on, and they disappear when it goes
away. A tool that is always offered and always fails is worse than an absent
one: the model spends turns discovering the failure and tends to invent a
workaround — usually a shell command doing something cruder — instead of
telling the user the capability is switched off.

The descriptions carry the mechanism ordering, because the model is the one
choosing. `win.snapshot` then `win.invoke` costs one cheap round trip and does
not touch the user's mouse. `win.screenshot` then `win.click` costs a vision
call per look and takes the pointer away from whoever is sitting there. They
are not interchangeable and the tool text says so.
"""

from __future__ import annotations

import base64
import threading
import time
from pathlib import Path
from typing import Any, Optional

from tools import Tool, ToolCtx, get_registry

READ_OPS = frozenset({
    "windows", "foreground", "snapshot", "screenshot", "clipboard.get",
})
WRITE_OPS = frozenset({
    "invoke", "setValue", "toggle", "expand", "focus", "scrollTo",
    "click", "type", "key", "clipboard.set",
    "window.focus", "window.move", "window.close",
})

_registered: list[str] = []
_lock = threading.RLock()


def _host():
    import windows_host
    host = windows_host.get_host()
    if host is None or not host.connected:
        raise RuntimeError(
            "no Windows kernel is connected; start helpwo-kernel.exe on the "
            "Windows side")
    return host


def _call(op: str, args: Optional[dict] = None) -> dict:
    return _host().call(op, args or {})


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


def _guard(fn):
    """Turn a kernel refusal into a tool error the model can act on."""
    def wrapper(args: dict, ctx: ToolCtx) -> dict:
        try:
            return fn(args, ctx)
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim
            return _err(str(exc) or exc.__class__.__name__)
    return wrapper


# -- perception ----------------------------------------------------------

@_guard
def _win_windows(args: dict, ctx: ToolCtx) -> dict:
    result = _call("windows", {"includeUntitled": bool(args.get("all"))})
    return _ok(result)


@_guard
def _win_snapshot(args: dict, ctx: ToolCtx) -> dict:
    payload: dict = {}
    if args.get("handle"):
        payload["handle"] = int(args["handle"])
    if args.get("max_nodes"):
        payload["maxNodes"] = int(args["max_nodes"])
    result = _call("snapshot", payload)
    return _ok(result)


@_guard
def _win_screenshot(args: dict, ctx: ToolCtx) -> dict:
    payload: dict = {"target": str(args.get("target") or "screen")}
    if args.get("handle"):
        payload["handle"] = int(args["handle"])
    if args.get("rect"):
        payload["rect"] = [int(v) for v in args["rect"]]
    # Captured at the size image.describe sends as-is, so the pixels its
    # vision model reads coordinates off are the view's pixels. Larger, and
    # describe shrinks the file on the way: every coordinate in its answer
    # would be in a space win.click was never told about.
    import vision
    full = bool(args.get("full_resolution"))
    if not full:
        payload["maxEdge"] = vision.DESCRIBE_MAX_EDGE
    result = _call("screenshot", payload)

    data_url = str(result.pop("dataUrl", ""))
    if "," not in data_url:
        return _err("the kernel returned no image")
    raw = base64.b64decode(data_url.split(",", 1)[1])

    # Written to a file rather than returned inline, because the agent model
    # is text-only: what happens next is image.describe or image.to_text on
    # this path, and those take a path.
    folder = Path(ctx.cwd or ".") / "artifacts" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"win-{int(time.time())}-{payload['target']}.png"
    path.write_bytes(raw)

    result["path"] = str(path)
    size = f"{result.get('width')}x{result.get('height')}"
    if not result.get("view"):
        # A kernel from before views ignores `view` and would click these
        # numbers as screen pixels.
        result["next"] = (
            "pass this path to image.describe to ask about it; this kernel is "
            "too old to click by screenshot coordinates (needs 1.3.0+)")
    elif full:
        result["next"] = (
            "full resolution, for image.to_text; do not click from an "
            "image.describe answer about it, which is read off a shrunk copy")
    else:
        result["next"] = (
            f"ask image.describe about this path — to click something, ask "
            f"for its pixel coordinates in this {size} image, then call "
            f"win.click with view {result.get('view')!r} and those x/y")
    return _ok(result)


@_guard
def _win_clipboard_get(args: dict, ctx: ToolCtx) -> dict:
    return _ok(_call("clipboard.get"))


# -- action --------------------------------------------------------------

@_guard
def _win_invoke(args: dict, ctx: ToolCtx) -> dict:
    label = str(args.get("label") or "")
    action = str(args.get("action") or "invoke")
    if action not in ("invoke", "toggle", "expand", "focus", "scrollTo"):
        return _err(f"unknown action {action!r}")
    return _ok(_call(action, {"label": label}))


@_guard
def _win_set_value(args: dict, ctx: ToolCtx) -> dict:
    return _ok(_call("setValue", {"label": str(args.get("label") or ""),
                                  "text": str(args.get("text") or "")}))


@_guard
def _win_click(args: dict, ctx: ToolCtx) -> dict:
    payload: dict = {"button": str(args.get("button") or "left"),
                     "double": bool(args.get("double"))}
    if args.get("label"):
        payload["label"] = str(args["label"])
    elif "x" in args and "y" in args:
        payload["x"] = int(args["x"])
        payload["y"] = int(args["y"])
        if args.get("view"):
            payload["view"] = str(args["view"])
    else:
        return _err("click needs either a label from win.snapshot, or x and y")
    return _ok(_call("click", payload))


@_guard
def _win_type(args: dict, ctx: ToolCtx) -> dict:
    return _ok(_call("type", {"text": str(args.get("text") or "")}))


@_guard
def _win_key(args: dict, ctx: ToolCtx) -> dict:
    return _ok(_call("key", {"keys": str(args.get("keys") or "")}))


@_guard
def _win_window(args: dict, ctx: ToolCtx) -> dict:
    action = str(args.get("action") or "")
    handle = int(args.get("handle") or 0)
    if action == "focus":
        return _ok(_call("window.focus", {"handle": handle}))
    if action == "close":
        return _ok(_call("window.close", {"handle": handle}))
    if action == "move":
        # Only what was given: the kernel keeps the rest where it is.
        payload = {"handle": handle}
        for key in ("x", "y", "width", "height"):
            if args.get(key) is not None:
                payload[key] = int(args[key])
        return _ok(_call("window.move", payload))
    return _err("action must be focus, move or close")


@_guard
def _win_clipboard_set(args: dict, ctx: ToolCtx) -> dict:
    return _ok(_call("clipboard.set", {"text": str(args.get("text") or "")}))


# -- registration --------------------------------------------------------

def _read_tools() -> list[Tool]:
    return [
        Tool(
            name="win.windows",
            description=(
                "List the windows open on the Windows desktop: handle, title, "
                "process, position, whether minimised. Start here — a handle "
                "from this is what win.snapshot and win.screenshot take."),
            schema={"type": "object", "properties": {
                "all": {"type": "boolean",
                        "description": "include untitled windows (rarely useful)"},
            }},
            invoke=_win_windows,
        ),
        Tool(
            name="win.snapshot",
            description=(
                "Read a window's interactive elements through the Windows "
                "accessibility tree. THE PREFERRED WAY TO SEE AN APPLICATION: "
                "it returns named, addressable controls, costs no vision "
                "call, and the labels it hands back work with win.invoke and "
                "win.set_value without touching the user's mouse. "
                "Defaults to the foreground window. If the result says "
                "opaque:true the window draws its own interface and exposes "
                "nothing — only then fall back to win.screenshot."),
            schema={"type": "object", "properties": {
                "handle": {"type": "integer",
                           "description": "window handle from win.windows; "
                                          "omit for the foreground window"},
                "max_nodes": {"type": "integer",
                              "description": "cap on elements returned"},
            }},
            invoke=_win_snapshot,
        ),
        Tool(
            name="win.screenshot",
            description=(
                "Capture the Windows screen, one window, or a region, and "
                "save it as a PNG. Returns the path — pass it to "
                "image.describe or image.to_text to actually read it. "
                "SECOND CHOICE, not first: each look costs a separate vision "
                "call, so prefer win.snapshot on any application that exposes "
                "its controls."),
            schema={"type": "object", "properties": {
                "target": {"type": "string", "enum": ["screen", "window", "region"],
                           "description": "default screen"},
                "handle": {"type": "integer", "description": "for target=window"},
                "rect": {"type": "array", "items": {"type": "integer"},
                         "description": "for target=region: [x, y, width, height]"},
                "full_resolution": {
                    "type": "boolean",
                    "description": ("keep full size for image.to_text on small "
                                    "text; not for finding things to click")},
            }},
            invoke=_win_screenshot,
        ),
        Tool(
            name="win.clipboard_read",
            description="Read the Windows clipboard as text.",
            schema={"type": "object", "properties": {}},
            invoke=_win_clipboard_get,
        ),
    ]


def _write_tools() -> list[Tool]:
    return [
        Tool(
            name="win.invoke",
            description=(
                "Act on one element from win.snapshot by its label: press a "
                "button, select a list row, expand a node, give it focus. "
                "Calls the control's own method, so the pointer does not move "
                "and the user keeps their focus — always prefer this over "
                "win.click."),
            schema={"type": "object", "properties": {
                "label": {"type": "string", "description": "e.g. 'e12' from win.snapshot"},
                "action": {"type": "string",
                           "enum": ["invoke", "toggle", "expand", "focus", "scrollTo"],
                           "description": "default invoke"},
            }, "required": ["label"]},
            invoke=_win_invoke,
        ),
        Tool(
            name="win.set_value",
            description=(
                "Put text into a field from win.snapshot by its label. "
                "Replaces the whole value in one step and does not depend on "
                "the keyboard layout — prefer this over win.type."),
            schema={"type": "object", "properties": {
                "label": {"type": "string"},
                "text": {"type": "string"},
            }, "required": ["label", "text"]},
            invoke=_win_set_value,
        ),
        Tool(
            name="win.click",
            description=(
                "Move the real mouse and click, by element label, or by x/y "
                "pixels in a win.screenshot image together with the `view` it "
                "returned (refused if the window moved since — look again). LAST RESORT: this takes the pointer away from "
                "whoever is using the machine. Use it only when win.snapshot "
                "came back opaque, or the element offers no action."),
            schema={"type": "object", "properties": {
                "label": {"type": "string", "description": "from win.snapshot"},
                "view": {"type": "string",
                         "description": "from the win.screenshot x/y were read off"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "double": {"type": "boolean"},
            }},
            invoke=_win_click,
        ),
        Tool(
            name="win.type",
            description=(
                "Type text into whatever currently has keyboard focus. LAST "
                "RESORT, for the same reason as win.click — it uses the real "
                "keyboard. Prefer win.set_value on a labelled field."),
            schema={"type": "object", "properties": {
                "text": {"type": "string"},
            }, "required": ["text"]},
            invoke=_win_type,
        ),
        Tool(
            name="win.key",
            description=(
                "Send one key combination, e.g. 'ctrl+s' or 'alt+f4'. Uses "
                "the real keyboard and goes to whatever has focus."),
            schema={"type": "object", "properties": {
                "keys": {"type": "string"},
            }, "required": ["keys"]},
            invoke=_win_key,
        ),
        Tool(
            name="win.window",
            description=(
                "Bring a window forward, move or resize it, or ask it to "
                "close. Closing sends the normal close request, so the "
                "application may still show a save prompt."),
            schema={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["focus", "move", "close"]},
                "handle": {"type": "integer"},
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "width": {"type": "integer"}, "height": {"type": "integer"},
            }, "required": ["action", "handle"]},
            invoke=_win_window,
        ),
        Tool(
            name="win.clipboard_write",
            description="Put text on the Windows clipboard.",
            schema={"type": "object", "properties": {
                "text": {"type": "string"},
            }, "required": ["text"]},
            invoke=_win_clipboard_set,
        ),
    ]


def register(host: Any) -> list[str]:
    """Offer the tools this kernel would actually accept."""
    tiers = host.tiers()
    tools: list[Tool] = []
    if tiers.get("machineRead"):
        tools += _read_tools()
    if tiers.get("machineWrite"):
        tools += _write_tools()

    registry = get_registry()
    with _lock:
        unregister()
        for tool in tools:
            # `builtin`, not a source tag of their own, even though these are
            # registered and dropped in a batch. The registry refuses to
            # invoke anything that is not builtin or a signed extension, and
            # hands non-builtins a stripped context — correct for third-party
            # code, wrong for a first-party surface that needs `ctx.cwd` to
            # write a screenshot where the user will find it. `_registered`
            # tracks the batch instead.
            tool.source = "builtin"
            if registry.register(tool):
                _registered.append(tool.name)
        return list(_registered)


def unregister() -> None:
    registry = get_registry()
    with _lock:
        for name in _registered:
            try:
                registry.unregister(name)
            except Exception:
                pass
        _registered.clear()


def registered_names() -> list[str]:
    with _lock:
        return list(_registered)


def render_prompt_section() -> str:
    """The system-prompt block that exists exactly while these tools do.

    Registering the tools alone was not enough to change what the model does:
    asked to "open the browser", it reached for `browser.*` — a headless Chrome
    inside WSL that the user cannot see and that has none of their logins —
    because nothing said a real desktop was now within reach, or how to start
    a program on it (there is deliberately no `win.launch`).
    """
    with _lock:
        names = set(_registered)
    if "win.snapshot" not in names:
        return ""
    can_act = "win.invoke" in names
    tier = ("read and write: you may look and act" if can_act else
            "read only: you may look but not act — to launch, click or type, "
            "ask the user to restart the kernel with the write tier")
    return f"""[THIS WINDOWS MACHINE — present only while helpwo-kernel is connected]
The user is at this Windows computer and switched the kernel on for a task.
Tier: {tier}.

- "The browser", "open an app", "click that", "on my screen" mean the Windows
  desktop the user is looking at, not this WSL shell. Prefer the `win.*` tools
  and the Windows side for them.
- `browser.*` is a different, invisible headless Chrome inside WSL, with none
  of the user's logins. Use it only for background page work (scraping,
  testing a site) or when the user asks for it by name.
- Starting a program is the shell's job; there is no `win.launch`. Use
  `explorer.exe`: it hands the request to Windows and returns at once, so it
  neither blocks the command nor dies with it, and it needs no approval.
    URL in the user's default browser: explorer.exe "https://example.com"
    an installed program: explorer.exe "$(wslpath -w "<Start menu .lnk>")"
      (find them: find "/mnt/c/ProgramData/Microsoft/Windows/Start Menu/Programs"
       /mnt/c/Users/*/AppData/Roaming/Microsoft/Windows/Start\\ Menu/Programs -maxdepth 3 -iname '*.lnk')
    a Windows built-in on PATH: explorer.exe notepad.exe
  explorer.exe exits with status 1 even on success; judge by the window.
  Do not run a GUI .exe directly: the command waits for it to close, and the
  program is killed when the command ends. powershell.exe and cmd.exe work
  but can stop for the user's approval, depending on their policy.
  Then poll `win.windows` until its window appears — a cold start takes
  seconds — and work on it by handle.
- Then read before you act: `win.snapshot` → `win.invoke` / `win.set_value`;
  screenshot + click only when a snapshot is `opaque`. Load the
  `windows-machine` skill before the first `win.*` call.
- Narrow what you read, do not repeat what you saw in passing, never write a
  secret down, and never sign in, approve or pass 2FA on the user's behalf."""
