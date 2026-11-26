"""ACP (Agent Client Protocol) adapter — lets editors drive laintas-cli.

Run with:  laintas-cli --acp

Speaks ACP v1 (https://agentclientprotocol.com) over stdio: the editor
(Zed & friends) spawns this process as its child and exchanges JSON-RPC.
Each ACP session maps to one in-process agent turn engine:

    session/new    → fresh chat history + agent state, chdir to the cwd
    session/prompt → run_agent_loop() in a worker thread; the loop's
                     events_cb is translated into session/update
                     notifications (message chunks, tool calls)
    session/cancel → the loop's interrupt_event

stdout discipline
-----------------
The RPC channel owns the real stdout *file descriptor*: the asyncio
transport is created from the real stdout first, and only afterwards is
Python-level sys.stdout swapped for _StrayStdout — so the transport writes
straight to fd 1 while the shared Rich console (which resolves sys.stdout
dynamically through repl_mirror.TeeFile), stray print() calls and library
warnings all land on stderr. A decorative line can never corrupt the
stream, and neither can a sys.stdout.buffer.write — the proxy hands out the
stderr buffer instead.

Approvals
---------
The REPL's full-screen approval prompts are REPL-only. This adapter
replaces LoopDeps' three approval callbacks with ACP round trips
(session/request_permission), so a command/write/delete the policy gates
is asked in the editor instead. Only allow-once/deny-once are offered:
ACP's "allow always" is per tool-kind, strictly broader than the REPL's
"this exact command/path for this session", so v1 stays conservative and
lets /mode auto-approve govern the rest, exactly as --execute does.

The optional dependency is the same posture as MCP: without the SDK
installed the module imports cleanly and --acp reports what is missing.
"""

from __future__ import annotations

import asyncio
import dataclasses
from functools import partial
import itertools
import queue
import sys
import threading
from typing import Any, Optional

try:
    from acp import PROTOCOL_VERSION, Agent, run_agent
    from acp.exceptions import RequestError
    from acp.interfaces import Client
    from acp.schema import (
        AgentCapabilities,
        AgentMessageChunk,
        AgentPlanUpdate,
        AllowedOutcome,
        ContentToolCallContent,
        DeniedOutcome,
        Implementation,
        InitializeResponse,
        NewSessionResponse,
        PermissionOption,
        PlanEntry,
        PromptResponse,
        RequestPermissionResponse,
        TextContentBlock,
        ToolCallProgress,
        ToolCallStart,
        ToolCallUpdate,
    )
    _ACP_IMPORT_ERROR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover - dependency absent
    _ACP_IMPORT_ERROR = _exc
    Agent = object  # type: ignore[assignment,misc]
    Client = object  # type: ignore[assignment,misc]
    PROTOCOL_VERSION = 1

#: How long an approval waits for the editor before failing closed.
PERMISSION_TIMEOUT_SECONDS = 300.0

#: Tool-name prefixes → ACP ToolKind. Anything else is "other".
_TOOL_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("read", "read"), ("grep", "search"), ("glob", "search"), ("ls", "read"),
    ("edit", "edit"), ("write", "edit"), ("multi_edit", "edit"),
    ("shell", "execute"), ("bash", "execute"), ("terminal", "execute"),
    ("web_fetch", "fetch"), ("web_search", "fetch"), ("fetch", "fetch"),
    ("think", "think"), ("memory", "read"),
    ("delete", "delete"), ("rm", "delete"), ("spawn", "other"),
)


def _tool_kind(name: str) -> str:
    low = (name or "").lower()
    for prefix, kind in _TOOL_KIND_PREFIXES:
        if low.startswith(prefix):
            return kind
    return "other"


class _StrayStdout:
    """sys.stdout stand-in for ACP mode.

    The ACP transport writes JSON-RPC frames straight to the real stdout
    fd — it captures the stream before the CLI starts printing and only
    ever touches fileno()/buffer. Every Python-level write that arrives
    through sys.stdout goes to stderr instead, so console art, Rich
    spinners and stray prints cannot corrupt the RPC stream.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, real):
        self._real = real

    @property
    def buffer(self):
        # The RPC transport already holds fd 1 via its own asyncio pipe; any
        # Python-level buffered write must not share that descriptor. Some
        # stderr replacements (StringIO in tests) have no .buffer — hand
        # back the object itself, which still satisfies a .write() caller.
        err = sys.stderr
        return getattr(err, "buffer", None) or err

    def fileno(self):
        return self._real.fileno()

    def writable(self):
        return True

    def write(self, text):
        try:
            sys.stderr.write(text)
        except Exception:
            pass
        return len(text) if isinstance(text, str) else 0

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        try:
            sys.stderr.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def close(self):
        # Never close the file behind the RPC channel.
        pass


class _PendingApproval:
    """One editor-side permission request the agent thread is blocked on."""

    def __init__(self):
        self.future: Optional[Any] = None
        self.result: Optional[bool] = None
        self.resolved = threading.Event()


class _AcpSession:
    """Everything one ACP session keeps between prompts."""

    def __init__(self, session_id: str, cwd: str):
        self.id = session_id
        self.cwd = cwd
        self.chat_history: list = []
        self.agent_state: dict = {
            "shortTermMemory": "", "lastReply": "", "lastOutput": "",
        }
        self.interrupt = threading.Event()
        self.message_queue = queue.Queue()
        self.worker: Optional[Any] = None          # executor future
        self.pending_approvals: dict[int, _PendingApproval] = {}
        self.open_tool_calls: set[str] = set()
        self.last_tool_call_id: Optional[str] = None
        self.last_tool_ok: Optional[bool] = None
        self.tool_ok_by_id: dict[str, bool] = {}
        self.saw_ai_event = False
        self.notifications: list = []
        self.model_selection: Optional[tuple[str, str]] = None
        self.model_catalog: list[dict] = []
        self._ids = itertools.count(1)

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"


class LaintasAcpAgent(Agent):
    """ACP v1 agent facade over run_agent_loop()."""

    def __init__(self, backend_session: dict, depth: int = 0):
        self._conn: Optional[Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._backend_session = backend_session
        self._depth = depth
        self._sessions: dict[str, _AcpSession] = {}
        self._ids = itertools.count(1)

    # ── lifecycle ───────────────────────────────────────────────────────

    def on_connect(self, conn) -> None:
        self._conn = conn

    async def initialize(
        self, protocol_version: int, client_capabilities=None,
        client_info=None, **kwargs: Any,
    ) -> InitializeResponse:
        from version import __version__
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(
                name="laintas-cli", title="Laintas CLI",
                version=str(__version__)),
        )

    async def authenticate(self, method_id: str, **kwargs: Any):
        from acp.schema import AuthenticateResponse
        return AuthenticateResponse()

    async def new_session(
        self, cwd: str, additional_directories=None, mcp_servers=None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        import os
        if not cwd or not os.path.isdir(cwd):
            raise RequestError.invalid_params(
                {"cwd": cwd, "reason": "not a directory"})
        # The agent loop and its tools run in this process, so the session
        # cwd is the process cwd. Multiple sessions from one editor share it;
        # editors keep a session per project, which is the common case.
        import os as _os
        _os.chdir(cwd)
        session_id = f"s{next(self._ids)}"
        self._sessions[session_id] = _AcpSession(session_id, cwd)
        if self._conn is not None:
            await self._advertise_commands(session_id)
        return NewSessionResponse(session_id=session_id, modes=None)

    async def _advertise_commands(self, session_id: str) -> None:
        import laintas_cli as cli
        try:
            from acp.schema import AvailableCommandsUpdate
        except ImportError:
            # Older SDKs can still pass a typed command through session/prompt.
            return
        spec = cli._find_command_spec("/model")
        if spec is None:
            return
        commands = [{"name": "model", "description": "List or select this session's chat model",
                     "input": {"hint": "Model ID, number from the list, or reset"}}]
        await self._conn.session_update(session_id, AvailableCommandsUpdate(
            session_update="available_commands_update", available_commands=commands))

    def _command_reply(self, s: _AcpSession, text: str) -> Optional[str]:
        first = text.split(maxsplit=1)[0].lower()
        if not first.startswith("/") or len(first) == 1 or "/" in first[1:]:
            return None
        import laintas_cli as cli
        if first != "/model":
            if cli._find_command_spec(first) is None and not first[1:].replace("-", "").isidentifier():
                return None
            return f"{first} is not available in Laintas CLI's ACP session. Use /model to choose a chat model."
        try:
            _, _, argv = cli._parse_slash_command(text)
        except cli.SlashCommandUsageError as error:
            return str(error)
        if len(argv) > 2:
            return "Usage: /model [model-id | number | reset]"
        choice = argv[1] if len(argv) == 2 else ""
        if choice.lower() in {"auto", "auto-routing", "clear", "default", "reset"}:
            s.model_selection = ("auto", "")
            return "This ACP session now uses backend auto-routing."
        if choice.isdecimal():
            index = int(choice) if len(choice) <= 6 else 0
            if not 1 <= index <= len(s.model_catalog):
                return "Run /model and choose a number from the displayed list."
            return self._select_model(s, s.model_catalog[index - 1])

        matches = [row for row in s.model_catalog if row["id"] == choice] if choice else []
        if not matches:
            try:
                rows, _ = cli.fetch_available_models(self._backend_session, cancel_event=s.interrupt)
            except cli.BlockingOperationCancelled:
                return "Model selection cancelled."
            except Exception:
                return "The backend model list is unavailable. Try /model again."
            if s.interrupt.is_set():
                return "Model selection cancelled."
            s.model_catalog = [row for row in rows if row["id"] not in {"auto", "auto-routing"}]
            matches = [row for row in s.model_catalog if row["id"] == choice] if choice else []

        if choice:
            if len(matches) == 1:
                return self._select_model(s, matches[0])
            if matches:
                return f"Several providers offer `{choice}`. Run /model and select the corresponding number."
            return f"Model `{choice}` is unavailable. Run /model to see the available models."

        current = s.model_selection[0] if s.model_selection else (cli.get_selected_model() or "auto")
        if s.model_selection is None and cli.model_route.selected_name() != cli.model_route.DEFAULT_ROUTE:
            current = "direct route " + cli.model_route.selected_name()
        lines = [f"Current model: `{current}`", "", "Select with `/model <number-or-model-id>`, or use `/model reset` for auto-routing."]
        if not s.model_catalog:
            lines.extend(["", "The backend offered no named models."])
        else:
            lines.append("")
            for number, row in enumerate(s.model_catalog[:200], 1):
                supplied_by = row.get("provider") or row.get("supply")
                label = f"{number}. `{row['id']}`"
                if supplied_by:
                    label += f" — {supplied_by}"
                lines.append(label)
            if len(s.model_catalog) > 200:
                lines.append("Use an exact model ID for models outside the displayed list.")
        return "\n".join(lines)

    @staticmethod
    def _select_model(s: _AcpSession, row: dict) -> str:
        model, provider = row["id"], row.get("provider") or ""
        s.model_selection = (model, provider)
        description = f"Selected backend model `{model}` for this ACP session."
        return description + (f" Provider: `{provider}`." if provider else "")

    async def _run_native_command(self, s: _AcpSession, text: str) -> Optional[PromptResponse]:
        s.interrupt.clear()
        worker = s.worker = asyncio.get_running_loop().run_in_executor(None, self._command_reply, s, text)
        try:
            reply = await asyncio.shield(worker)
            if s.interrupt.is_set():
                return PromptResponse(stop_reason="cancelled")
            if reply is None:
                return None
            if self._conn is not None:
                await self._conn.session_update(s.id, AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=reply)))
            return PromptResponse(stop_reason="end_turn")
        except asyncio.CancelledError:
            await self.cancel(s.id)
            return PromptResponse(stop_reason="cancelled")
        finally:
            self._release_worker(s, worker)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        s = self._sessions.get(session_id)
        if s is None:
            return
        s.interrupt.set()
        # Deny any approval still waiting on the editor so the loop is not
        # hostage to a dialog the user already dismissed by cancelling.
        for pending in list(s.pending_approvals.values()):
            if pending.future is not None:
                pending.future.cancel()

    # ── prompting ───────────────────────────────────────────────────────

    async def prompt(self, session_id: str, prompt, **kwargs: Any) -> PromptResponse:
        s = self._sessions.get(session_id)
        if s is None:
            raise RequestError.invalid_params(
                {"session_id": session_id, "reason": "unknown session"})
        if s.worker is not None:
            raise RequestError.invalid_params(
                {"session_id": session_id,
                 "reason": "a prompt is already running; cancel it first"})
        text = "\n".join(
            block.text for block in (prompt or [])
            if getattr(block, "text", None)
        ).strip()
        if not text:
            raise RequestError.invalid_params({"prompt": "no text content"})

        if text.startswith("/"):
            result = await self._run_native_command(s, text)
            if result is not None:
                return result

        s.interrupt.clear()
        s.saw_ai_event = False
        s.chat_history.append(
            {"role": "user", "content": text, "input_kind": "prompt"})
        deps = self._build_deps(s)
        loop = asyncio.get_running_loop()
        worker = s.worker = loop.run_in_executor(None, self._run_turn, deps, text, s)
        try:
            response = await asyncio.shield(worker)
            for dangling in list(s.open_tool_calls):
                ok = s.tool_ok_by_id.pop(dangling, None)
                s.open_tool_calls.discard(dangling)
                self._notify(s.id, ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=dangling,
                    status=("failed" if ok is False or response.get("success") is False else "completed"),
                ))
            final_msg = str(response.get("msg") or "")
            if not s.saw_ai_event and final_msg:
                self._notify(s.id, AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=final_msg)))
            notifications, s.notifications = s.notifications, []
            if notifications:
                await asyncio.gather(*(asyncio.wrap_future(future) for future in notifications),
                                     return_exceptions=True)
            if s.interrupt.is_set():
                return PromptResponse(stop_reason="cancelled")
            if response.get("success") is False:
                if response.get("exit_reason") == "user_denied":
                    return PromptResponse(stop_reason="refusal")
                raise RequestError.internal_error({"reason": "turn_failed"})
            return PromptResponse(stop_reason="end_turn")
        except asyncio.CancelledError:
            await self.cancel(session_id)
            return PromptResponse(stop_reason="cancelled")
        finally:
            self._release_worker(s, worker)

    @staticmethod
    def _release_worker(s: _AcpSession, worker) -> None:
        if worker.done():
            s.worker = None
            return

        def clear_worker(done):
            if not done.cancelled():
                done.exception()
            s.worker = None
        worker.add_done_callback(clear_worker)

    # ── turn engine ─────────────────────────────────────────────────────

    def _run_turn(self, deps, text: str, s: _AcpSession) -> dict:
        import laintas_cli as cli
        # Same as --execute: apply the active mode's auto-approve posture
        # before the loop starts, or a mode with auto_approve="writes" stops
        # at the first write and fails closed with no TTY to ask.
        try:
            cli._sync_session_approval_from_mode()
        except Exception:
            pass
        from agent_loop import run_agent_loop
        state = dict(s.agent_state)
        if s.model_selection:
            state["_model_override"], state["_provider_override"] = s.model_selection
        response = run_agent_loop(
            deps,
            original_input=text,
            session=self._backend_session,
            state=state,
            chat_history=s.chat_history,
            events_cb=self._make_events_cb(s),
            existing_session=None,
            depth=self._depth,
            interrupt_event=s.interrupt,
            message_queue=s.message_queue,
            continue_thread=False,
        )
        # Carry the loop's final state into the next prompt of this session.
        new_state = response.get("state")
        if isinstance(new_state, dict):
            s.agent_state = new_state
        return response

    def _build_deps(self, s: _AcpSession):
        import laintas_cli as cli
        base = cli.get_loop_deps()
        return dataclasses.replace(
            base,
            call_backend=partial(base.call_backend, use_direct_route=False) if s.model_selection else base.call_backend,
            request_command_approval=self._approval_cb(
                s, kind="execute", allow_always=False),
            request_file_write_approval=self._approval_cb(
                s, kind="edit", allow_always=False),
            request_file_delete_approval=self._approval_cb(
                s, kind="delete", allow_always=False),
        )

    # ── events → session/update ─────────────────────────────────────────

    def _make_events_cb(self, s: _AcpSession):
        def cb(events):
            for event in events or []:
                try:
                    self._forward_event(s, event)
                except Exception:
                    # A UI translation failure must never kill the run.
                    pass
        return cb

    def _notify(self, session_id: str, update) -> None:
        if self._conn is None or self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            self._conn.session_update(session_id, update), self._loop)
        session = self._sessions.get(session_id)
        if session is not None:
            session.notifications = [pending for pending in session.notifications if not pending.done()]
            session.notifications.append(future)

    def _forward_event(self, s: _AcpSession, event: dict) -> None:
        kind = event.get("type")
        if kind in ("ai_stream", "ai"):
            content = event.get("content") or ""
            if content:
                s.saw_ai_event = True
                self._notify(s.id, AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=content)))
        elif kind == "plan":
            statuses = {"pending": "pending", "in_progress": "in_progress",
                        "completed": "completed", "skipped": "completed"}
            self._notify(s.id, AgentPlanUpdate(
                session_update="plan",
                entries=[PlanEntry(content=task["subject"], priority="medium",
                                   status=statuses.get(task.get("status"), "pending"))
                         for task in event.get("tasks", [])
                         if task.get("subject") and task.get("status") != "deleted"],
            ))
        elif kind == "tool_started":
            call_id = (event.get("toolCallId")
                       or s.next_id("tool"))
            s.open_tool_calls.add(call_id)
            s.last_tool_call_id = call_id
            s.last_tool_ok = None
            s.tool_ok_by_id.pop(call_id, None)
            name = event.get("name") or event.get("content") or "tool"
            blocks = []
            command = event.get("command") or ""
            if command:
                blocks.append(ContentToolCallContent(
                    type="content",
                    content=TextContentBlock(type="text", text=str(command))))
            self._notify(s.id, ToolCallStart(
                session_update="tool_call",
                tool_call_id=call_id, title=str(name), kind=_tool_kind(name),
                status="in_progress", content=blocks or None))
        elif kind == "system":
            sub = event.get("kind")
            if sub == "tool":
                meta = event.get("meta") or {}
                call_id = meta.get("call_id") or s.last_tool_call_id
                if call_id:
                    s.last_tool_call_id = call_id
                    ok = bool(meta.get("ok"))
                    s.last_tool_ok = ok
                    s.tool_ok_by_id[call_id] = ok
                    if call_id not in s.open_tool_calls:
                        # No tool_started was seen (older loop path): open it
                        # now so the update below lands somewhere visible.
                        s.open_tool_calls.add(call_id)
                        title = str(event.get("content") or "tool")
                        self._notify(s.id, ToolCallStart(
                            session_update="tool_call",
                            tool_call_id=call_id, title=title,
                            kind=_tool_kind(title), status="in_progress"))
            elif sub == "output" and s.last_tool_call_id:
                call_id = (event.get("meta") or {}).get("call_id") or s.last_tool_call_id
                ok = s.tool_ok_by_id.pop(call_id, s.last_tool_ok)
                content = str(event.get("content") or "")
                s.open_tool_calls.discard(call_id)
                self._notify(s.id, ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=call_id,
                    status=("completed" if ok is not False else "failed"),
                    content=([ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text=content))]
                        if content else None),
                ))
        # ai_end / billing / unknown kinds carry nothing an editor needs.

    # ── approvals → session/request_permission ──────────────────────────

    def _approval_cb(self, s: _AcpSession, kind: str, allow_always: bool):
        def callback(*args) -> bool:
            if kind == "execute":
                command, reason = args[0], (args[1] if len(args) > 1 else "")
                title = command or "Run a command"
                body = f"{command}\n{reason}".strip()
            else:
                path, detail, reason = args[0], args[1], (
                    args[2] if len(args) > 2 else "")
                verb = {"edit": "Write", "delete": "Delete"}[kind]
                title = f"{verb} {path}"
                body = f"{path}\n{reason}\n\n{detail}".strip()
            return self._ask_editor(s, kind, title, body, allow_always)
        return callback

    def _ask_editor(self, s: _AcpSession, kind: str, title: str, body: str,
                    allow_always: bool) -> bool:
        if self._conn is None or self._loop is None:
            return False
        pending = _PendingApproval()
        s.pending_approvals[id(pending)] = pending
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._request_permission(s, kind, title, body, allow_always,
                                         pending),
                self._loop,
            )
            pending.future = future
            try:
                return bool(future.result(timeout=PERMISSION_TIMEOUT_SECONDS))
            except Exception:
                # Editor went away, cancelled, or stayed silent: fail closed.
                return False
        finally:
            s.pending_approvals.pop(id(pending), None)

    async def _request_permission(self, s: _AcpSession, kind: str, title: str,
                                  body: str, allow_always: bool,
                                  pending: _PendingApproval) -> bool:
        options = [PermissionOption(option_id="allow", name="Allow",
                                    kind="allow_once")]
        if allow_always:
            options.append(PermissionOption(
                option_id="allow_always", name="Always allow",
                kind="allow_always"))
        options.append(PermissionOption(option_id="deny", name="Deny",
                                        kind="reject_once"))
        call_id = s.last_tool_call_id
        tool_call = ToolCallUpdate(
            tool_call_id=call_id if call_id in s.open_tool_calls else s.next_id("approval"), title=title[:200],
            kind=kind, status="pending",
            content=[ContentToolCallContent(
                type="content",
                content=TextContentBlock(type="text", text=body[:8000]))],
        )
        try:
            response: RequestPermissionResponse = (
                await self._conn.request_permission(
                    s.id, tool_call=tool_call, options=options))
        except Exception:
            return False
        return isinstance(response.outcome, AllowedOutcome) and response.outcome.option_id == "allow"


# ── entry point ─────────────────────────────────────────────────────────

def serve(depth: int = 0) -> int:
    """Run the ACP agent over stdio. Returns a process exit code."""
    if _ACP_IMPORT_ERROR is not None:
        sys.stderr.write(
            "laintas-cli --acp: the Agent Client Protocol SDK is not "
            "available (" + str(_ACP_IMPORT_ERROR) + ").\n"
            "Install it with: pip install agent-client-protocol\n")
        return 1

    # RPC owns the real stdout fd from here on; everything else → stderr.
    import laintas_cli as cli

    # Same posture as --execute: never interactive sign-in over stdio.
    backend = cli.get_backend_profile()
    backend_session: dict = {}
    if getattr(backend, "sends_laintas_credentials", False):
        cached = cli.load_session() or {}
        if cached:
            try:
                status, user_info = cli.check_session(cached)
                if (status == "ok" and (not cli.paths.ACCOUNT_USER_ID
                        or str(user_info["id"]) == cli.paths.ACCOUNT_USER_ID)):
                    cached["userId"] = str(user_info["id"])
                    cached["userName"] = user_info.get("name", "")
                    cached["userEmail"] = user_info.get("email", "")
                    backend_session = cli.account_store.frozen_auth(
                        cached, cli.paths.ACCOUNT_USER_ID or None)
            except Exception:
                backend_session = {}
        if not backend_session.get("userId"):
            sys.stderr.write(
                "laintas-cli --acp: sign-in required. Start laintas-cli, "
                "run /login, then retry.\n")
            return 1

    agent = LaintasAcpAgent(backend_session, depth=depth)
    real_stdout = sys.stdout

    # TeeFile (repl_mirror) resolves sys.stdout on every write, so the swap
    # below also reroutes the shared Rich console's console.file. TeeFile's
    # own encoding attribute stays as-is; _StrayStdout matches it.
    async def _serve() -> None:
        from acp.stdio import stdio_streams
        agent._loop = asyncio.get_running_loop()
        # Bind the transport to the REAL stdout first (stdio_streams resolves
        # sys.stdout right now), then swap the Python-level object so every
        # later console print lands on stderr instead of the RPC stream.
        reader, writer = await stdio_streams()
        sys.stdout = _StrayStdout(real_stdout)
        await run_agent(agent, writer, reader)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover - manual/debug entry
    raise SystemExit(serve())
