"""Explicit task adoption and handoff acceptance; never overwrite the source."""
import copy
import hashlib
import json
import time

import account_store
import file_lock
import json_store
import paths
import session_lifecycle


def save_received(blob):
    import agent_loop
    import session_store
    cwd, sid, aid = blob["cwd"], blob["session_id"], blob["agent_id"]
    with session_lifecycle.guard(cwd):
        if session_lifecycle.is_deleted(cwd, blob):
            raise account_store.AccountError("This received task was deleted; create a new handoff")
        dest = agent_loop._resume_session_path(cwd, sid, aid)
        if dest.exists():
            old = json.loads(dest.read_text(encoding="utf-8"))
            if (not account_store.owns(old, paths.ACCOUNT_USER_ID)
                    or old.get("cwd") != cwd or old.get("session_id") != sid):
                raise account_store.AccountError("Received task conflicts with an existing session")
            return old
        session_store._atomic_write_json(dest, blob)
        return blob


def activate(blob):
    import laintas_cli as cli
    state = getattr(cli.handle_meta_command, "_last_agent_state", None)
    history = getattr(cli.handle_meta_command, "_last_chat_history", None)
    if state is None or history is None:
        raise account_store.AccountError("Task acceptance needs an interactive CLI")
    if state.get("_session_id") == blob["session_id"]:
        cli.console.print("[dim]This received task is already current.[/dim]")
        return False
    live = getattr(cli.handle_meta_command, "_current_live_session", None)
    result = cli._switch_resume_session(blob, blob["cwd"], state, history, live)
    if result is None:
        return False
    restored, new_live = result
    existing = getattr(cli.handle_meta_command, "_last_existing_session", None)
    if existing is not None:
        existing.close()
    restored = cli._bind_current_agent_runtime(restored, history, None, state)
    cli.handle_meta_command._last_agent_state = restored
    cli.handle_meta_command._last_chat_history = history
    cli.handle_meta_command._last_existing_session = None
    cli.handle_meta_command._current_live_session = new_live
    cli.handle_meta_command._agent_switch_performed = True
    return True


def release_if_inactive(blob):
    import laintas_cli as cli
    import peer_coordination
    state = getattr(cli.handle_meta_command, "_last_agent_state", {}) or {}
    if state.get("_session_id") != blob["session_id"]:
        peer_coordination.release_session_lease(blob["cwd"], blob["session_id"])


def _primary_receiver(cli):
    if cli.get_current_agent_id() != "primary":
        cli._cmd_agent(["/agent", "primary"], {},
                       getattr(cli.handle_meta_command, "_last_existing_session", None))
        if cli.get_current_agent_id() != "primary":
            raise account_store.AccountError("Select /agent primary before accepting a task")


@account_store.admission(False)
def accept(hid, cwd):
    import handoff
    import task_handoff
    import laintas_cli as cli
    if not cli._account_idle():
        return False
    with handoff.guard(hid, cwd):
        env = handoff.load(hid, cwd)
        if handoff.foreign_repo(env, cwd):
            raise handoff.HandoffError("Open the source repository before accepting this task")
        projected = handoff.project(env)
        if projected["status"] == "closed":
            raise handoff.HandoffError("This handoff is closed; reopen it before accepting")
        if projected["holder"] and projected["holder"] != paths.ACCOUNT_USER_ID:
            raise handoff.HandoffError("Another account holds this task; it must release it first")
        blob = task_handoff.continuation(env, paths.ACCOUNT_USER_ID, cwd, "primary")
        _primary_receiver(cli)
        if cli._acquire_resume_lease(blob) is None:
            return False
        try:
            blob = save_received(blob)
            if not projected["holder"]:
                handoff.append(hid, "claim", paths.ACCOUNT_USER_ID,
                               data={"session_id": blob["session_id"]}, cwd=cwd)
        except BaseException:
            release_if_inactive(blob)
            raise
    try:
        changed = activate(blob)
    finally:
        release_if_inactive(blob)
    if changed:
        cli.console.print("[green]Task accepted into a new session for this account.[/green]")
        if not cli._enqueue_user_input("Continue the accepted handoff's remaining work. First verify the workspace and environment described in its checkpoint."):
            cli.console.print("[dim]Task is ready. Enter /continue to begin.[/dim]")
    return changed


def legacy_tasks(cwd):
    import laintas_cli as cli
    records = {}
    key = hashlib.sha256(str(cwd).encode()).hexdigest()[:16]
    deleted = json_store.load_json(paths.ROOT_HOME / "sessions" / f"{key}_deleted.json", [])
    deleted = set(deleted) if isinstance(deleted, list) and all(isinstance(sid, str) for sid in deleted) else set()
    for path in (paths.ROOT_HOME / "sessions").glob("*.json"):
        if path.is_symlink():
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(blob, dict) or blob.get("cwd") != cwd
                    or blob.get("owner_user_id") or not blob.get("chat_history")):
                continue
            sid = cli._resume_effective_session_id(blob)
            if not sid or sid in deleted:
                continue
            previous = records.get(sid)
            if previous is None or float(blob.get("timestamp") or 0) > float(previous.get("timestamp") or 0):
                records[sid] = blob
        except (OSError, ValueError, TypeError):
            continue
    return sorted(records.values(), key=lambda blob: float(blob.get("timestamp") or 0), reverse=True)


@account_store.admission(False)
def adopt(source_id, cwd):
    import laintas_cli as cli
    if not paths.ACCOUNT_USER_ID:
        raise account_store.AccountError("Select an account before adopting a legacy task")
    if not cli._account_idle():
        return False
    source = next((item for item in legacy_tasks(cwd)
                   if cli._resume_effective_session_id(item) == source_id), None)
    if source is None:
        raise account_store.AccountError("Unknown legacy task; use /account legacy")
    if cli._live_session_lease_owner(cwd, source_id, owner_user_id=""):
        raise account_store.AccountError("The legacy task is still running in another CLI")
    key = hashlib.sha256(f"{cwd}\0{source_id}".encode()).hexdigest()
    receipt = paths.ROOT_HOME / "accounts" / "legacy_claims" / f"{key}.json"
    with file_lock.guard(receipt.with_suffix(".lock")):
        prior = json_store.load_json(receipt, {})
        if receipt.exists() and (not isinstance(prior, dict) or not prior.get("owner_user_id")):
            raise account_store.AccountError("Legacy ownership record is unreadable; adoption cancelled")
        if prior and prior.get("owner_user_id") != paths.ACCOUNT_USER_ID:
            raise account_store.AccountError("This legacy task has already been adopted by another account")
        _primary_receiver(cli)
        sid = hashlib.sha256(f"{paths.ACCOUNT_USER_ID}\0legacy\0{key}".encode()).hexdigest()[:32]
        blob = copy.deepcopy(source)
        blob.pop("_path", None)
        blob.update(id=sid, session_id=sid, cwd=cwd, kind="autosave", timestamp=time.time(),
                    agent_id=cli.get_current_agent_id(), owner_user_id=paths.ACCOUNT_USER_ID,
                    created_by="", handoff_from={"legacy": True, "session_id": source_id})
        blob["state"] = cli.prepare_state_for_repl(blob.get("state") or blob.get("agent_state") or {})
        blob["state"].update(_session_id=sid, owner_user_id=paths.ACCOUNT_USER_ID,
                             created_by="", handoff_from=blob["handoff_from"], _fork_lineage=[])
        for field in ("active_work_id", "parent_session_id", "fork_parent_session_id"):
            blob.pop(field, None)
        blob["fork_lineage"] = []
        if cli._acquire_resume_lease(blob) is None:
            return False
        try:
            blob = save_received(blob)
            json_store.save_json_atomic(receipt, {"owner_user_id": paths.ACCOUNT_USER_ID,
                                                 "session_id": sid}, mode=0o600,
                                        account_independent=True)
        except BaseException:
            release_if_inactive(blob)
            raise
    try:
        changed = activate(blob)
    finally:
        release_if_inactive(blob)
    cli.console.print("[green]Legacy task adopted; its original files are preserved.[/green]")
    return changed
