"""Extension: ai-pow

AI-PoW binds the work behind a Git commit -- human input, visible replies,
model usage, tool activity, sampled file revisions -- to the commit as a proof,
and scores the process. This extension records it from laintas-cli sessions.

    /pow                       where this session records, and hook health
    /pow init [path]           start recording the repository containing path
    /pow off | /pow on         pause / resume recording for this session
    /pow focus [path|auto]     the repository chat-only turns count toward
    /pow report [commit]       the commit proof page
    /pow history               the repository summary page
    /pow verify [commit]       check a sealed proof against Git
    /pow export <file> [commit]
    /pow repair hook|boundary|rebuild

Recording is automatic and follows the work, not the start directory: see
`router.py`. Nothing is offered to the model -- the score describes a person's
work, and an agent that can read it is an agent that can optimise for it.

`laintas pow <args>` is AI-PoW's own command line (`aipow <args>`), served from
this package. The post-commit hook calls it, so the hook names the launcher and
survives this package being updated or moved.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# The vendored modules import each other by top-level name, exactly as the
# standalone `ai-pow` package does; they are registered under those names
# before `ai_pow` is imported so the files stay byte-identical to upstream.
from . import ai_pow_scoring as _scoring
from . import ai_pow_report as _report

sys.modules["ai_pow_scoring"] = _scoring
sys.modules["ai_pow_report"] = _report

from . import ai_pow  # noqa: E402
from . import router as _router_mod  # noqa: E402

_ctx = None
_router: Optional[_router_mod.Router] = None

_SUBCOMMANDS = [
    ("status", "Where this session records, and hook health"),
    ("init", "Start recording the repository containing a path"),
    ("off", "Pause recording for this session"),
    ("on", "Resume recording"),
    ("focus", "Pin the repository chat-only turns count toward (auto: follow the work)"),
    ("report", "The commit proof page"),
    ("history", "The repository summary page"),
    ("verify", "Check a sealed proof against Git"),
    ("export", "Write a shareable proof bundle"),
    ("repair", "hook | boundary | rebuild"),
    ("help", "Show usage"),
]

_ARG_RULES = (
    ("", 1, "/pow [status|init|off|on|focus|report|history|verify|export|repair]"),
    ("status", 1, "/pow status"),
    ("help", 1, "/pow help"),
    ("init", 2, "/pow init [path]"),
    ("off", 1, "/pow off"),
    ("on", 1, "/pow on"),
    ("focus", 2, "/pow focus [path|auto]"),
    ("report", 2, "/pow report [commit]"),
    ("history", 1, "/pow history"),
    ("verify", 2, "/pow verify [commit]"),
    ("export", 3, "/pow export <file> [commit]"),
    ("repair", 2, "/pow repair hook|boundary|rebuild"),
)


def _host_invocation() -> Optional[list]:
    """The launcher a post-commit hook should run: `laintas pow`, not a file here."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "pow"]
    for key in ("laintas_cli", "__main__"):
        source = getattr(sys.modules.get(key), "__file__", "") or ""
        if Path(source).name == "laintas_cli.py":
            return [sys.executable, str(Path(source).resolve()), "pow"]
    return None


def _escape(value) -> str:
    try:
        from rich.markup import escape
        return escape(str(value))
    except Exception:
        return str(value).replace("[", "\\[")


def _say(text: str = "") -> None:
    console = getattr(_ctx, "console", None)
    if console is not None:
        console.print(text)
    else:
        print(text)


def _cwd() -> str:
    try:
        return os.path.realpath(os.getcwd())
    except OSError:
        return os.path.realpath(getattr(_ctx, "cwd", None) or os.sep)


def _resolve(path: str) -> str:
    return _router_mod._absolute(path, _cwd())


def _target_repo() -> Optional[str]:
    """The repository a bare /pow report|history|verify|repair acts on."""
    if _router is not None and _router.focus:
        return _router.focus
    return _router_mod.find_repo_root(_cwd())


def _recorder_for(root: Optional[str]):
    if not root:
        _say("[yellow]No repository in focus.[/yellow] "
             "Use /pow focus <path>, or run /pow from inside a repository.")
        return None
    rec = _router.initialized(root) if _router is not None else None
    if rec is None:
        _say(f"[yellow]{_escape(root)} is not recording.[/yellow] "
             f"Start it with /pow init {_escape(root)}")
    return rec


def _hook_line(rec) -> str:
    try:
        state = ai_pow.hook_status(rec)
    except Exception as exc:
        return f"[red]hook unknown[/red] ({_escape(exc)})"
    kind = state["state"]
    if kind == "installed":
        return "[green]hook ok[/green]"
    if kind == "stale":
        return "[yellow]hook points at an old launcher[/yellow] -- /pow repair hook"
    if kind == "missing":
        return "[yellow]no post-commit hook[/yellow] -- /pow repair hook"
    return (f"[yellow]hook not managed by AI-PoW ({kind})[/yellow] -- "
            f"add to it: {_escape(state['command'])}")


def _proof_line(rec) -> str:
    try:
        proof = rec.proof("HEAD")
    except Exception:
        return "no proof for HEAD yet"
    score = proof.get("score") or {}
    commit = str(proof.get("commit") or "")[:7]
    if score.get("value") is None:
        return f"HEAD {commit}: sealed, unscored"
    return f"HEAD {commit}: {score['value']} ({score.get('grade') or '?'})"


def _cmd_status() -> None:
    router = _router
    _say("[bold]AI-PoW[/bold]" + ("  [yellow]paused[/yellow] -- /pow on to resume"
                                   if router.paused else ""))
    focus = router.focus
    if focus:
        how = "pinned" if router.focus_pinned else "follows the work"
        _say(f"  focus: {_escape(focus)} [dim]({how})[/dim]")
    else:
        _say("  focus: [dim]none yet -- set when the agent first works in a "
             "recording repository[/dim]")
    roots = list(router.watched)
    if focus and focus not in roots:
        roots.append(focus)
    if not roots:
        start = _router_mod.find_repo_root(_cwd())
        if start and router.initialized(start) is None:
            _say(f"  {_escape(start)} is not recording -- /pow init")
    for root in roots:
        rec = router.initialized(root)
        if rec is None:
            _say(f"  [yellow]{_escape(root)}[/yellow]: not recording -- "
                 f"/pow init {_escape(root)}")
            continue
        _say(f"  [bold]{_escape(root)}[/bold]")
        try:
            status = rec.status()
            active = status.get("active") or {}
            human = (active.get("human") or {}).get("messages", 0)
            events = sum((active.get("event_counts") or {}).values())
            _say(f"    since last commit: {events} events, {human} human messages")
            if status.get("recording_error"):
                _say("    [red]a recording error was logged[/red] -- "
                     f"see {_escape(status['storage'])}/recording-error")
        except Exception as exc:
            _say(f"    [red]{_escape(exc)}[/red]")
        _say(f"    {_proof_line(rec)}")
        _say(f"    {_hook_line(rec)}")
    if router.unrecorded:
        _say("  [dim]worked in, not recording:[/dim] "
             + ", ".join(_escape(r) for r in router.unrecorded))


def _cmd_init(path: str) -> None:
    start = _resolve(path) if path else _cwd()
    root = _router_mod.find_repo_root(start)
    if not root:
        _say(f"[yellow]{_escape(start)} is not inside a Git repository.[/yellow]")
        candidates = [r for r in (_router.unrecorded + _router.watched) if r]
        if candidates and not path:
            _say("Repositories this session worked in: "
                 + ", ".join(_escape(r) for r in candidates)
                 + " -- /pow init <path>")
        return
    _router.forget(root)
    rec = _router.recorder(root)
    if rec is None:
        _say(f"[red]Git does not accept {_escape(root)} as a work tree.[/red]")
        return
    try:
        result = rec.init()
        hook = ai_pow.install_hook(rec)
    except Exception as exc:
        _say(f"[red]AI-PoW init failed:[/red] {_escape(exc)}")
        return
    _router.forget(root)
    _router.watch(root)
    verb = "already recording" if result.get("existing") else "recording"
    _say(f"[green]AI-PoW {verb}[/green] {_escape(root)}")
    _say(f"  storage: {_escape(result.get('storage'))}")
    if hook.get("installed"):
        _say(f"  post-commit hook: {_escape(hook.get('path'))}")
    elif hook.get("reason") == "already installed":
        _say("  post-commit hook: already installed")
    else:
        _say(f"  [yellow]hook not installed ({_escape(hook.get('reason'))}).[/yellow] "
             f"Run after each commit, or add to your hook: "
             f"{_escape(hook.get('manual_command'))}")


def _cmd_focus(arg: str) -> None:
    if not arg:
        _say(f"focus: {_escape(_router.focus or 'none')}"
             + (" (pinned)" if _router.focus_pinned else ""))
        return
    if arg == "auto":
        _router.set_focus(_router.watched[-1] if _router.watched else None,
                          pinned=False)
        _say("focus follows the work again")
        return
    root = _router_mod.find_repo_root(_resolve(arg))
    if not root:
        _say(f"[yellow]{_escape(arg)} is not inside a Git repository.[/yellow]")
        return
    _router.set_focus(root, pinned=True)
    note = "" if _router.initialized(root) else \
        f" [yellow](not recording -- /pow init {_escape(root)})[/yellow]"
    _say(f"focus pinned: {_escape(root)}{note}")


def _cmd_pages(verb: str, argv: list) -> None:
    rec = _recorder_for(_target_repo())
    if rec is None:
        return
    try:
        if verb == "report":
            page = rec.html_report(argv[0] if argv else "HEAD")
        elif verb == "history":
            page = rec.html_index("HEAD")
        elif verb == "verify":
            # A failed check raises, with the reason, into the handler below.
            result = rec.verify(argv[0] if argv else "HEAD")
            _say(f"[green]verified[/green]: {result.get('events', 0)} events, "
                 f"proof {_escape(str(result.get('proof_hash') or '')[:16])} "
                 "[dim](integrity against Git; not completeness or quality)[/dim]")
            return
        else:  # export
            if not argv:
                _say("Usage: /pow export <file> [commit]")
                return
            written = rec.export(_resolve(argv[0]), argv[1] if len(argv) > 1 else "HEAD")
            _say(f"exported: {_escape(written.get('exported'))}")
            return
    except Exception as exc:
        _say(f"[red]{_escape(exc)}[/red]")
        return
    _say(f"{_escape(page)}")
    _say(f"[dim]file://{_escape(page)}[/dim]")


def _cmd_repair(what: str) -> None:
    rec = _recorder_for(_target_repo())
    if rec is None:
        return
    try:
        if what == "hook":
            result = ai_pow.install_hook(rec)
            if result.get("installed"):
                _say(f"[green]hook {'replaced' if result.get('replaced') else 'installed'}[/green]: "
                     f"{_escape(result.get('path'))}")
            elif result.get("reason") == "already installed":
                _say("[green]hook ok[/green]")
            else:
                _say(f"[yellow]left alone ({_escape(result.get('reason'))})[/yellow]; add: "
                     f"{_escape(result.get('manual_command'))}")
        elif what == "boundary":
            rec.reset_boundary()
            _say("[green]boundary reset[/green] -- the gap is recorded in the next proof")
        elif what == "rebuild":
            result = rec.rebuild_iterations("HEAD")
            page = rec.html_index("HEAD")
            _say(f"[green]iteration chain rebuilt[/green] ({_escape(result)})")
            _say(f"{_escape(page)}")
        else:
            _say("Usage: /pow repair hook|boundary|rebuild")
    except Exception as exc:
        _say(f"[red]{_escape(exc)}[/red]")


def _cmd_help() -> None:
    _say("[bold]/pow[/bold] [dim]-- AI-PoW: a process score and work journal per commit[/dim]")
    for line in (
            "/pow                       where this session records, and hook health",
            "/pow init [path]           start recording the repository containing path",
            "/pow off | /pow on         pause / resume recording for this session",
            "/pow focus [path|auto]     the repository chat-only turns count toward",
            "/pow report [commit]       the commit proof page (HTML)",
            "/pow history               the repository summary page (HTML)",
            "/pow verify [commit]       check a sealed proof against Git",
            "/pow export <file> [commit]",
            "/pow repair hook|boundary|rebuild"):
        _say("  " + _escape(line))
    _say("[dim]Recording follows the files the agent works on, not the "
         "directory the CLI started in. Proofs are sealed by the post-commit "
         "hook.[/dim]")


def handle(parts: list, raw_line: str = "") -> None:
    """`/pow ...` -- the host passes the split parts and the raw line."""
    argv = [str(p) for p in (parts or [])[1:]]
    verb = argv[0].lower() if argv else "status"
    rest = argv[1:]
    if _router is None:
        _say("[red]ai-pow is not set up[/red]")
        return
    if verb == "status":
        _cmd_status()
    elif verb == "init":
        _cmd_init(rest[0] if rest else "")
    elif verb in ("off", "on"):
        marked = _router.set_paused(verb == "off")
        if verb == "off":
            _say("[yellow]AI-PoW paused for this session[/yellow]"
                 + (" -- the gap is recorded in " + ", ".join(_escape(r) for r in marked)
                    if marked else ""))
        else:
            _say("[green]AI-PoW recording[/green]")
    elif verb == "focus":
        _cmd_focus(rest[0] if rest else "")
    elif verb in ("report", "history", "verify", "export"):
        _cmd_pages(verb, rest)
    elif verb == "repair":
        _cmd_repair(rest[0].lower() if rest else "")
    else:
        _cmd_help()


def cli(argv: list) -> int:
    """`laintas pow <args>`: the standalone `aipow` command line."""
    return int(ai_pow.main(list(argv)) or 0)


def setup(ctx) -> None:
    global _ctx, _router
    if not callable(getattr(ctx, "on", None)):
        raise RuntimeError("ai-pow needs a laintas-cli that raises lifecycle "
                           "events (ctx.on, v1.31.0 or later)")
    _ctx = ctx
    ai_pow.HOST_INVOCATION = _host_invocation()
    _router = _router_mod.Router(ai_pow)
    for event, handler in _router.handlers().items():
        ctx.on(event, handler)
    ctx.register_command(
        "pow", handle,
        description="AI-PoW: process score and work journal per Git commit",
        subcommands=_SUBCOMMANDS,
        arg_rules=_ARG_RULES)


def teardown() -> None:
    global _ctx, _router
    for name, module in (("ai_pow_scoring", _scoring), ("ai_pow_report", _report)):
        if sys.modules.get(name) is module:
            sys.modules.pop(name, None)
    _router = None
    _ctx = None
