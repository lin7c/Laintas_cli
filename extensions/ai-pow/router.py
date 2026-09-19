"""Route agent events to the Git repository the work happened in.

The session's working directory says where the CLI was started, not where the
work is. Started in a home directory, an agent that edits a project two levels
down is working in that project -- and recording it under the start directory
(or, when that is not a repository, nowhere) is the bug this module exists to
remove.

So every event is attributed by its own target:

* a file tool by the paths in its arguments;
* a shell command by the directory it runs in, and any `cd` / `git -C` in it;
* everything without a location -- the human message, the reply, model usage,
  skills, spawns -- by the turn it belongs to.

A repository **joins** a turn the first time one of its files is touched, and
joining replays the turn's location-less events into it before sampling the
baseline. AI-PoW links edits to the human message that came before them, so the
order must be: message, baseline sample, then the write. A turn that touches no
repository at all (a question, a discussion) is flushed to the session's focus
repository when it ends: that conversation is input to the work there.

Only repositories where `aipow init` has run are recorded. Nothing here prints:
handlers run inside the agent loop, while the transcript is being rendered.
"""
from __future__ import annotations

import os
import re
import shlex
import threading
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

SOURCE = "laintas-native"
#: Location-less events one turn may hold for repositories that join late.
MAX_PENDING = 256
#: Directories one shell command may contribute.
MAX_SHELL_DIRS = 8

_PATH_KEYS = ("path", "paths", "file", "files", "file_path", "dir", "directory",
              "cwd", "root", "source", "destination", "target", "src", "dst")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$", re.M)
_FILE_TOOLS = frozenset({"read", "write", "edit", "multi_edit", "apply_patch"})
_TERMINAL_TOOLS = frozenset({"terminal.exec", "terminal.send"})
_SHELL_BREAKS = re.compile(r"[;&|()<>`]")


def _here() -> str:
    """The process directory, or / once it has been deleted."""
    try:
        return os.getcwd()
    except OSError:
        return os.sep


def _absolute(value: Any, base: str) -> str:
    text = str(value or "").strip()
    if not text or "\0" in text:
        return ""
    text = os.path.expanduser(text)
    if not os.path.isabs(text):
        text = os.path.join(base or os.sep, text)
    return os.path.realpath(text)


def _is_git_marker(marker: str) -> bool:
    """A `.git` Git itself would accept -- not an empty stray directory."""
    try:
        if os.path.isdir(marker):
            return os.path.isfile(os.path.join(marker, "HEAD"))
        if os.path.isfile(marker):
            with open(marker, "rb") as handle:
                return handle.read(8) == b"gitdir: "
    except OSError:
        pass
    return False


def find_repo_root(path: str) -> Optional[str]:
    """The innermost Git work tree containing `path`, or None.

    A path that does not exist yet (a file about to be written) belongs to the
    repository of its nearest existing ancestor.
    """
    if not path:
        return None
    current = path
    while current and not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    if not os.path.isdir(current):
        current = os.path.dirname(current)
    while True:
        if _is_git_marker(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def shell_directories(command: str, cwd: str) -> list:
    """Directories a shell command works in: `cd`/`pushd` targets and `git -C`.

    Heuristic by nature -- a variable or a subshell cannot be followed -- and
    that is acceptable: a missed directory still has its changes sampled at
    the next turn, only without the link to this turn's message.
    """
    try:
        tokens = shlex.split(command or "", posix=True)
    except ValueError:
        tokens = (command or "").split()
    found, base = [], cwd
    for index, token in enumerate(tokens[:-1]):
        target = None
        if token in ("cd", "pushd"):
            target = tokens[index + 1]
        elif token == "-C" and "git" in tokens[max(0, index - 3):index]:
            target = tokens[index + 1]
        if not target:
            continue
        target = _SHELL_BREAKS.split(target, 1)[0]
        if not target or target.startswith(("-", "$")):
            continue
        resolved = _absolute(target, base)
        if not resolved:
            continue
        if token != "-C":
            base = resolved
        if resolved not in found:
            found.append(resolved)
        if len(found) >= MAX_SHELL_DIRS:
            break
    return found


def tool_targets(name: str, arguments: dict, cwd: str) -> list:
    """Absolute paths a tool call works on; empty when it has no location."""
    name = str(name or "")
    arguments = arguments if isinstance(arguments, dict) else {}
    found: list = []

    def add(value):
        path = _absolute(value, cwd)
        if path and path not in found:
            found.append(path)

    if name.startswith("fs.") or name in _FILE_TOOLS:
        for key in _PATH_KEYS:
            value = arguments.get(key)
            for item in (value if isinstance(value, (list, tuple)) else [value]):
                if isinstance(item, str):
                    add(item)
        for key in ("patch", "input"):
            text = arguments.get(key)
            if isinstance(text, str) and "*** " in text:
                for match in _PATCH_FILE.findall(text) + _PATCH_MOVE.findall(text):
                    add(match)
        return found
    if name == "shell.exec":
        base = _absolute(arguments.get("cwd"), cwd) if arguments.get("cwd") else cwd
        if base:
            add(base)
        for directory in shell_directories(str(arguments.get("command") or ""), base or cwd):
            add(directory)
        return found
    if name in _TERMINAL_TOOLS:
        # A named terminal keeps its own directory; only an explicit cd says
        # anything about where it is.
        text = str(arguments.get("command") or arguments.get("input")
                   or arguments.get("text") or "")
        for directory in shell_directories(text, cwd):
            add(directory)
    return found


@dataclass
class Turn:
    depth: int
    run_id: str
    agent_id: str
    cwd: str
    foreground: bool
    #: (kind, data, event_id) of every location-less event, in order.
    pending: list = field(default_factory=list)
    #: root -> Recorder, in join order.
    joined: dict = field(default_factory=dict)
    #: call_id -> roots its tool.call was recorded in.
    calls: dict = field(default_factory=dict)


class Router:
    """Session-wide routing state. One per loaded extension."""

    def __init__(self, pow_module):
        self.pow = pow_module
        self._lock = threading.RLock()
        self._local = threading.local()
        self._recorders: dict = {}
        self._root_locks: dict = {}
        #: Initialized repositories this session has worked in, oldest first.
        self.watched: list = []
        #: Repositories touched this session that are not recording.
        self.unrecorded: list = []
        self.focus: Optional[str] = None
        self.focus_pinned = False
        self.paused = False
        self._hook_checked: set = set()

    # ── repositories ──────────────────────────────────────────────────────

    def recorder(self, root: str):
        """The cached Recorder for a work tree, or None when Git refuses it."""
        with self._lock:
            if root in self._recorders:
                return self._recorders[root]
        try:
            rec = self.pow.Recorder(root)
        except Exception:
            rec = None
        with self._lock:
            self._recorders[root] = rec
            self._root_locks.setdefault(root, threading.Lock())
        return rec

    def initialized(self, root: Optional[str]):
        """The Recorder when `root` records, else None. A stat per call."""
        if not root:
            return None
        rec = self.recorder(root)
        if rec is None:
            return None
        try:
            return rec if rec.database.is_file() else None
        except OSError:
            return None

    def forget(self, root: str) -> None:
        """Drop cached state after an `init`, so the next event re-reads it."""
        with self._lock:
            self._recorders.pop(root, None)
            self._hook_checked.discard(root)
            if root in self.unrecorded:
                self.unrecorded.remove(root)

    def watch(self, root: str) -> None:
        with self._lock:
            if root in self.watched:
                self.watched.remove(root)
            self.watched.append(root)
            if root in self.unrecorded:
                self.unrecorded.remove(root)
            if not self.focus_pinned:
                self.focus = root

    def _note_unrecorded(self, root: str) -> None:
        with self._lock:
            if root not in self.unrecorded and root not in self.watched:
                self.unrecorded.append(root)
                del self.unrecorded[:-16]

    def _write(self, root: str, rec, kind: str, data: dict, event_id: str) -> None:
        with self._root_locks[root]:
            try:
                rec.record(kind, data, SOURCE, event_id)
            except Exception as exc:
                self._error(rec, exc)

    def _sample(self, root: str, rec) -> None:
        with self._root_locks[root]:
            try:
                rec.sample()
            except Exception as exc:
                self._error(rec, exc)

    def _error(self, rec, exc) -> None:
        try:
            self.pow.record_error(rec, exc)
        except Exception:
            pass

    def _repair_own_hook(self, root: str, rec) -> None:
        """Once per session: point a hook AI-PoW wrote at the current launcher.

        The hook that sealed proofs before this became an extension names a
        file that no longer exists. Leaving it would make every commit print
        "proof not sealed" while recording looked healthy.
        """
        with self._lock:
            if root in self._hook_checked:
                return
            self._hook_checked.add(root)
        try:
            if self.pow.hook_status(rec)["state"] == "stale":
                self.pow.install_hook(rec)
        except Exception:
            pass

    # ── turns ─────────────────────────────────────────────────────────────

    def _stack(self) -> list:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def current(self) -> Optional[Turn]:
        stack = self._stack()
        return stack[-1] if stack else None

    def join(self, turn: Optional[Turn], root: str):
        """Bring `root` into this turn: replay what it missed, then sample.

        Returns the Recorder, or None when the repository is not recording.
        """
        if turn is not None and root in turn.joined:
            return turn.joined[root]
        rec = self.initialized(root)
        if rec is None:
            # A sub-agent's private worktree is not something to offer `init` for.
            if turn is None or turn.foreground:
                self._note_unrecorded(root)
            return None
        self._repair_own_hook(root, rec)
        if turn is not None:
            for kind, data, event_id in list(turn.pending):
                self._write(root, rec, kind, data, event_id)
        self._sample(root, rec)
        if turn is not None:
            turn.joined[root] = rec
        self.watch(root)
        return rec

    def _pending(self, turn: Optional[Turn], kind: str, data: dict) -> None:
        """A location-less event: to every joined repository, and kept for late ones."""
        event_id = uuid.uuid4().hex
        if turn is None:
            # Outside any agent turn (a background model call, say): the focus
            # repository is the only reasonable owner.
            with self._lock:
                focus = self.focus
            rec = self.initialized(focus)
            if rec is not None:
                self._write(focus, rec, kind, data, event_id)
            return
        for root, rec in list(turn.joined.items()):
            self._write(root, rec, kind, data, event_id)
        if len(turn.pending) >= MAX_PENDING:
            # Keep the human message: it is what every later edit links to.
            for index, item in enumerate(turn.pending):
                if item[0] != "human.message":
                    del turn.pending[index]
                    break
            else:
                return
        turn.pending.append((kind, data, event_id))

    # ── event handlers (ctx.on) ───────────────────────────────────────────

    def on_turn_start(self, p: dict) -> None:
        depth = int(p.get("depth") or 0)
        stack = self._stack()
        while stack and stack[-1].depth >= depth:
            stack.pop()
        cwd = os.path.realpath(str(p.get("cwd") or _here()))
        turn = Turn(depth=depth, run_id=str(p.get("run_id") or ""),
                    agent_id=str(p.get("agent_id") or "main"), cwd=cwd,
                    foreground=bool(p.get("foreground")))
        stack.append(turn)
        if self.paused:
            return
        if turn.foreground:
            start_root = find_repo_root(cwd)
            start = self.initialized(start_root)
            with self._lock:
                if start is not None and self.focus is None and not self.focus_pinned:
                    self.focus = start_root
                baseline = list(self.watched)
            if start is not None and start_root not in baseline:
                baseline.append(start_root)
            # A shell command can change a repository no path names; sampling
            # here keeps those changes out of this turn's baseline.
            for root in baseline:
                rec = self.initialized(root)
                if rec is not None:
                    self._sample(root, rec)
        text = p.get("human_text")
        if text is not None:
            self._pending(turn, "human.message", {
                **self.pow.text_meta(text),
                "session_id": str(p.get("session_id") or "")[:128],
                "run_id": turn.run_id[:128], "agent_id": turn.agent_id[:128]})

    def on_turn_end(self, p: dict) -> None:
        depth = int(p.get("depth") or 0)
        stack = self._stack()
        while stack and stack[-1].depth >= depth:
            turn = stack.pop()
            if self.paused or turn.joined or not turn.pending:
                continue
            # Touched no repository: the conversation still counts, toward the
            # repository the session is working in.
            with self._lock:
                focus = self.focus
            if focus:
                self.join(turn, focus)

    def on_tool_call(self, p: dict) -> None:
        if self.paused:
            return
        turn = self.current()
        name = str(p.get("name") or "unknown")
        call_id = str(p.get("call_id") or "")
        data = {"name": name[:128], "call_id": call_id[:128],
                "session_id": str(p.get("session_id") or "")[:128],
                "run_id": str(p.get("run_id") or "")[:128]}
        source = str(p.get("source") or "")
        if source.startswith("mcp"):
            data["mcp_server"] = source[:128]
        cwd = os.path.realpath(str(p.get("cwd") or (turn.cwd if turn else "") or _here()))
        roots = []
        for path in tool_targets(name, p.get("arguments") or {}, cwd):
            root = find_repo_root(path)
            if root and root not in roots:
                roots.append(root)
        recorded = []
        for root in roots:
            rec = self.join(turn, root)
            if rec is not None:
                recorded.append(root)
                self._write(root, rec, "tool.call", data, uuid.uuid4().hex)
        if recorded:
            if turn is not None:
                turn.calls[call_id] = tuple(recorded)
        else:
            self._pending(turn, "tool.call", data)

    def on_tool_result(self, p: dict) -> None:
        if self.paused:
            return
        turn = self.current()
        call_id = str(p.get("call_id") or "")
        data = {"name": str(p.get("name") or "unknown")[:128],
                "call_id": call_id[:128], "ok": bool(p.get("ok")),
                "session_id": str(p.get("session_id") or "")[:128],
                "run_id": str(p.get("run_id") or "")[:128]}
        roots = turn.calls.pop(call_id, ()) if turn is not None else ()
        if not roots:
            self._pending(turn, "tool.result", data)
            return
        event_id = uuid.uuid4().hex
        for root in roots:
            rec = turn.joined.get(root)
            if rec is not None:
                self._write(root, rec, "tool.result", data, event_id)

    def on_tool_batch_end(self, _p: dict) -> None:
        if self.paused:
            return
        turn = self.current()
        if turn is None:
            return
        for root, rec in list(turn.joined.items()):
            self._sample(root, rec)

    def on_assistant_reply(self, p: dict) -> None:
        # What a person saw: a sub-agent's internal text is not a reply to them.
        if self.paused or not p.get("visible"):
            return
        self._pending(self.current(), "assistant.visible", {
            **self.pow.text_meta(p.get("text") or ""),
            "channel": "final" if p.get("final") else "commentary",
            "session_id": str(p.get("session_id") or "")[:128],
            "run_id": str(p.get("run_id") or "")[:128],
            "agent_id": str(p.get("agent_id") or "main")[:128]})

    def on_model_usage(self, p: dict) -> None:
        if self.paused:
            return
        try:
            cents = Decimal(int(p.get("costCents") or 0))
        except Exception:
            cents = Decimal(0)
        self._pending(self.current(), "model.usage", {
            "model": str(p.get("model") or "(default)")[:128],
            "input_tokens": max(0, int(p.get("in") or 0)),
            "output_tokens": max(0, int(p.get("out") or 0)),
            "cached_input_tokens": max(0, min(int(p.get("cachedIn") or 0),
                                              int(p.get("in") or 0))),
            "reasoning_tokens": None, "cache_write_tokens": None,
            "measurement": "estimated" if p.get("estimated") else "provider_reported",
            "actual_usd": str(cents / 100) if p.get("official") else None,
            "provider": str(p.get("backend") or "unknown")[:128],
        })

    def on_agent_spawn(self, p: dict) -> None:
        if self.paused:
            return
        self._pending(self.current(), "agent.spawn", {
            "agent_id": str(p.get("agent_id") or "")[:128],
            "parent_agent_id": str(p.get("parent_agent_id") or "")[:128]})

    def on_skill_used(self, p: dict) -> None:
        if self.paused:
            return
        self._pending(self.current(), "skill.used", {
            "name": str(p.get("name") or "")[:128],
            "basis": str(p.get("basis") or "")[:64]})

    # ── session controls ──────────────────────────────────────────────────

    def set_paused(self, paused: bool) -> list:
        """Pause or resume; a pause is written as a gap, never hidden."""
        with self._lock:
            if self.paused == paused:
                return []
            self.paused = paused
            roots = list(self.watched)
            if self.focus and self.focus not in roots:
                roots.append(self.focus)
        marked = []
        if paused:
            for root in roots:
                rec = self.initialized(root)
                if rec is not None:
                    self._write(root, rec, "coverage.gap",
                                {"reason": "paused_by_user"}, uuid.uuid4().hex)
                    marked.append(root)
        return marked

    def set_focus(self, root: Optional[str], pinned: bool = True) -> None:
        with self._lock:
            self.focus = root
            self.focus_pinned = bool(root) and pinned

    def handlers(self) -> dict:
        return {
            "turn.start": self.on_turn_start,
            "turn.end": self.on_turn_end,
            "tool.call": self.on_tool_call,
            "tool.result": self.on_tool_result,
            "tool.batch_end": self.on_tool_batch_end,
            "assistant.reply": self.on_assistant_reply,
            "model.usage": self.on_model_usage,
            "agent.spawn": self.on_agent_spawn,
            "skill.used": self.on_skill_used,
        }
