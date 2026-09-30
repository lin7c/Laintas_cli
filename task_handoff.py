"""Portable, inert task checkpoints. Receiving one never runs a tool."""
import copy
import hashlib
import json
import time

import account_store

MAX_BYTES = 2 * 1024 * 1024
STATE_FIELDS = ("objective", "shortTermMemory", "lastReply", "lastOutput",
                "terminalHistory", "_thread_messages", "_thread_summary",
                "_thread_call_seq", "_step_counter")
SECRET_FIELDS = {"token", "session_token", "authorization", "cookies", "headers",
                 "api_key", "apikey", "password", "passwd", "secret",
                 "access_token", "refresh_token", "agent_secret"}


def scrub(value):
    if isinstance(value, dict):
        return {str(key): scrub(item) for key, item in value.items()
                if str(key).lower().replace("-", "_") not in SECRET_FIELDS}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        import redactor
        spans = [span for span in redactor.scan_text(value)
                 if span["type"] in {"KEY", "JWT", "PRIVATE_KEY"}]
        return redactor.apply_spans(value, spans)
    return value


def validate(checkpoint):
    if (not isinstance(checkpoint, dict) or checkpoint.get("version") != 1
            or not isinstance(checkpoint.get("state"), dict)
            or not isinstance(checkpoint.get("chat_history"), list)
            or not isinstance(checkpoint.get("tasks"), list)
            or not isinstance(checkpoint.get("source_session_id"), str)
            or not checkpoint.get("source_session_id")
            or not isinstance(checkpoint.get("source_user_id"), str)
            or not checkpoint.get("source_user_id")):
        raise ValueError("Invalid task checkpoint; upgrade laintas-cli or create it again")
    if not checkpoint["chat_history"] or not all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in checkpoint["chat_history"]):
        raise ValueError("Task checkpoint has no valid conversation")
    if any(not isinstance(task, dict) for task in checkpoint["tasks"]):
        raise ValueError("Invalid task plan")
    state = checkpoint["state"]
    for key in ("objective", "shortTermMemory", "lastReply", "lastOutput", "_thread_summary"):
        if key in state and not isinstance(state[key], str):
            raise ValueError(f"Invalid checkpoint state: {key}")
    for key in ("_thread_call_seq", "_step_counter"):
        if key in state and (type(state[key]) is not int or state[key] < 0):
            raise ValueError(f"Invalid checkpoint state: {key}")
    if "terminalHistory" in state and not isinstance(state["terminalHistory"], list):
        raise ValueError("Invalid checkpoint terminal history")
    if "_thread_messages" in state and (not isinstance(state["_thread_messages"], list)
            or any(not isinstance(m, dict) or m.get("role") not in {"user", "assistant", "tool"}
                   for m in state["_thread_messages"])):
        raise ValueError("Invalid checkpoint message thread")
    if len(json.dumps(checkpoint, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError("Task checkpoint is too large; /compact before creating the handoff")
    return checkpoint


def capture(state, history, tasks, user_id, *, older_summary="", rules=None):
    if not user_id:
        raise ValueError("Sign in before handing off a task")
    payload = {
        "version": 1, "created_at": time.time(), "source_user_id": user_id,
        "source_session_id": str(state.get("_session_id") or ""),
        "created_by": str(state.get("created_by", user_id)),
        "state": {key: copy.deepcopy(state[key]) for key in STATE_FIELDS if key in state},
        "chat_history": copy.deepcopy(history), "tasks": copy.deepcopy(tasks or []),
        "older_summary": str(older_summary or ""), "rules": copy.deepcopy(rules or []),
        "recreate_resources": ["shell sessions", "browser sessions", "pending approvals"],
    }
    thread = payload["state"].get("_thread_messages")
    if isinstance(thread, list):
        payload["state"]["_thread_messages"] = [m for m in thread
            if isinstance(m, dict) and m.get("role") in {"user", "assistant", "tool"}]
    return validate(scrub(payload))


def continuation(env, user_id, cwd, agent_id="primary"):
    checkpoint = validate(env.get("checkpoint"))
    if not user_id:
        raise ValueError("Sign in before accepting a task")
    target = str(env.get("target_user_id") or "")
    if target and target != user_id:
        raise ValueError("This task is addressed to another account")
    sid = hashlib.sha256(f"{user_id}\0{env['id']}".encode()).hexdigest()[:32]
    state = {key: copy.deepcopy(checkpoint["state"][key]) for key in STATE_FIELDS
             if key in checkpoint["state"]}
    state.update(_session_id=sid, _task_cwd=cwd, owner_user_id=user_id,
                 created_by=checkpoint.get("created_by", checkpoint["source_user_id"]),
                 handoff_from={"handoff_id": env["id"],
                               "session_id": checkpoint["source_session_id"],
                               "user_id": checkpoint["source_user_id"]})
    account_store.stamp(state, user_id)
    history = copy.deepcopy(checkpoint["chat_history"])
    notice = ("[accepted task handoff]\n"
              f"Objective: {state.get('objective') or env.get('title') or ''}\n"
              f"Workspace is now {cwd}. Source paths may refer to the previous machine. "
              "Verify files and environment here before continuing. Shell/browser sessions "
              "and approvals must be recreated.\n"
              f"Avoid: {json.dumps(env.get('avoid') or [], ensure_ascii=False)}\n"
              f"Task constraints: {json.dumps(checkpoint.get('rules') or [], ensure_ascii=False)}")
    history.append({"role": "knowledge", "content": notice})
    if state.get("_thread_messages"):
        state["_thread_messages"].append({"role": "user", "content": notice})
    return {"id": sid, "session_id": sid, "kind": "autosave", "cwd": cwd,
            "agent_id": agent_id, "owner_user_id": user_id,
            "created_by": state["created_by"], "handoff_from": state["handoff_from"],
            "timestamp": time.time(), "title": env.get("title") or "Accepted task",
            "state": state, "chat_history": history,
            "tasks": copy.deepcopy(checkpoint["tasks"]),
            "older_summary": checkpoint.get("older_summary") or ""}
