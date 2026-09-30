"""Two-level terminal adoption. Controllers hold proxies; workers stay remote.

Relations belong to a running CLI incarnation. Restarting either end requires
new consent; a stale request can never acquire a later incarnation's resources.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

import account_store
import json_store

MAX_BYTES = 512 * 1024
MAX_RECORDS = 1024
INVITE_TTL = 300
LEASE_SECONDS = 30
OPS = frozenset({"inspect", "offer", "commit", "status", "assign", "deploy", "cancel", "release"})
_service = None
_delegation = threading.local()


class LinkError(ValueError):
    pass


def valid_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", value) or value == "term0":
        raise LinkError("Use a terminal name of 1-48 letters, digits, dot, underscore or hyphen; term0 is reserved")
    return value


def valid_ref(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:local|remote):[A-Za-z0-9._-]{1,80}", value):
        raise LinkError("Use the exact local:… or remote:… terminal ID from /term peers")
    return value


@contextmanager
def delegated():
    previous = getattr(_delegation, "active", False)
    _delegation.active = True
    try:
        yield
    finally:
        _delegation.active = previous


def local_admission_allowed():
    return _service is None or not _service.parent or bool(getattr(_delegation, "active", False))


def check_terminal_creation():
    if _service is not None and _service.parent:
        raise LinkError("This terminal is adopted. Create execution terminals from its controller; a third terminal level is not allowed")


def get_service():
    if _service is None:
        raise LinkError("Terminal linking is not running in this CLI")
    return _service


def stop_service():
    global _service
    current, _service = _service, None
    if current is not None:
        current.close()


def _json(value):
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    if len(raw) > MAX_BYTES:
        raise LinkError("Terminal request or result is too large")
    return raw


class LocalTransport:
    """Private loopback rendezvous; never follows a peer-provided URL."""
    def __init__(self, root, instance, user_id, handler):
        self.root = Path(root) / "terminal-endpoints"
        self.ref = valid_ref("local:" + instance)
        self.user_id = str(user_id)
        self.secret = secrets.token_hex(32)
        self.handler = handler
        self.server = None
        self.thread = None
        self.remote_call = None
        self.remote_source = None

    def record(self, ref):
        ref = valid_ref(ref)
        if not ref.startswith("local:"):
            raise LinkError("Not a local terminal")
        path = self.root / (ref[6:] + ".json")
        if path.is_symlink() or self.root.is_symlink():
            raise LinkError("Unsafe terminal endpoint")
        try:
            stat = path.stat()
            if os.name != "nt" and (stat.st_uid != os.getuid() or stat.st_mode & 0o077):
                raise LinkError("Unsafe terminal endpoint permissions")
            row = json.loads(path.read_text())
            if (not isinstance(row, dict) or row.get("ref") != ref or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("secret") or ""))
                    or not isinstance(row.get("port"), int) or not 1 <= row["port"] <= 65535
                    or time.time() - float(row.get("updated", 0)) > 15):
                raise LinkError("Terminal is offline")
            return row
        except (OSError, ValueError, TypeError) as exc:
            raise LinkError("Terminal endpoint is unavailable") from exc

    def peers(self, all_accounts=False):
        result = []
        for path in self.root.glob("*.json"):
            try:
                row = self.record("local:" + path.stem)
                if row["ref"] != self.ref and (all_accounts or row.get("user_id") == self.user_id):
                    result.append({k: v for k, v in row.items() if k not in {"secret", "port"}})
            except LinkError:
                continue
        return result

    def source(self, target):
        if target.startswith("remote:"):
            if not callable(self.remote_source):
                raise LinkError("Connect this terminal with /helpwo and update its kernel to use remote terminal linking")
            return valid_ref(self.remote_source())
        return self.ref

    def call(self, target, op, payload):
        target = valid_ref(target)
        if op not in OPS:
            raise LinkError("Unknown terminal operation")
        if target.startswith("remote:"):
            if not callable(self.remote_call):
                raise LinkError("Remote terminal transport is unavailable")
            response = self.remote_call(target, op, payload)
        else:
            row = self.record(target)
            envelope = {"source": self.ref, "op": op, "payload": payload, "at": time.time()}
            raw = _json(envelope)
            signature = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
            conn = http.client.HTTPConnection("127.0.0.1", row["port"], timeout=8)
            try:
                conn.request("POST", "/term", raw, {"Authorization": "Bearer " + row["secret"],
                             "Content-Type": "application/json", "X-Term-Signature": signature})
                reply = conn.getresponse()
                body = reply.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise LinkError("Terminal response too large")
                response = json.loads(body)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                raise LinkError("Terminal did not answer; retry using the same request identity") from exc
            finally:
                conn.close()
        if not isinstance(response, dict) or not response.get("ok"):
            raise LinkError(str(response.get("error") if isinstance(response, dict) else "Invalid terminal response"))
        return response.get("result", {})

    def start(self):
        transport = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    if self.path != "/term" or not hmac.compare_digest(
                            self.headers.get("Authorization", ""), "Bearer " + transport.secret):
                        raise LinkError("Unauthorized terminal request")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BYTES:
                        raise LinkError("Invalid terminal request size")
                    self.connection.settimeout(8)
                    raw = self.rfile.read(size)
                    envelope = json.loads(raw)
                    row = transport.record(envelope.get("source"))
                    signature = hmac.new(row["secret"].encode(), raw, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(signature, self.headers.get("X-Term-Signature", "")):
                        raise LinkError("Invalid terminal source")
                    if abs(time.time() - float(envelope.get("at", 0))) > 30:
                        raise LinkError("Expired terminal request")
                    reply = transport.handler(row["ref"], row.get("user_id", ""),
                                              envelope.get("op"), envelope.get("payload"))
                except Exception as exc:
                    reply = {"ok": False, "error": str(exc)}
                body = _json(reply)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 16
            slots = threading.BoundedSemaphore(16)
            def process_request(self, request, address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except BaseException:
                    self.slots.release()
                    raise
            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    self.slots.release()

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="term-endpoint", daemon=True)
        self.publish()
        self.thread.start()

    def publish(self):
        if self.server is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise LinkError("Unsafe terminal endpoint directory")
        if os.name != "nt":
            self.root.chmod(0o700)
        json_store.save_json_atomic(self.root / (self.ref[6:] + ".json"),
            {"ref": self.ref, "pid": os.getpid(), "user_id": self.user_id,
             "port": self.server.server_port, "secret": self.secret, "updated": time.time()}, mode=0o600)

    def close(self):
        if self.server is not None and self.thread is not None and self.thread.ident is not None:
            self.server.shutdown()
        if self.server is not None:
            self.server.server_close()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(3)
        path = self.root / (self.ref[6:] + ".json")
        try:
            if json_store.load_json(path, {}).get("secret") == self.secret:
                path.unlink(missing_ok=True)
        except OSError:
            pass


class Service:
    def __init__(self, adapter, transport, user_id):
        self.adapter, self.transport, self.user_id = adapter, transport, str(user_id)
        self.lock = threading.RLock()
        self.parent = None
        self.children = {}
        self.invitations = {}
        self.pending = {}
        self.jobs = {}
        self.outgoing = {}
        self.stop = threading.Event()
        self.worker = None
        self.poller = ThreadPoolExecutor(max_workers=32, thread_name_prefix="term-poll")

    def start(self):
        self.transport.start()
        self.worker = threading.Thread(target=self._watch, name="term-relations", daemon=True)
        self.worker.start()

    def _watch(self):
        while not self.stop.wait(1):
            try:
                self.transport.publish()
                self.tick()
            except Exception:
                # A transport outage leaves the relation offline, never adopted
                # by another controller automatically.
                pass

    def tick(self):
        with self.lock:
            children = list(self.children.items())
            if self.parent and time.time() - self.parent.get("contact", time.time()) > LEASE_SECONDS:
                self.parent["offline"] = True
                self.adapter.cancel_owned(self.parent["id"])
            expired = [key for key, value in self.pending.items() if value["expires"] < time.time()]
            for key in expired:
                self.pending.pop(key, None)
        futures = [self.poller.submit(self._poll_child, alias, child) for alias, child in children]
        for future in futures:
            future.result()

    def _poll_child(self, alias, child):
        if not self.adapter.has_terminal(alias):
            try:
                self.release(alias)
            except LinkError:
                pass
            return
        if self.stop.is_set():
            return
        try:
            result = self.transport.call(child["target"], "status", {"id": child["id"], "token": child["token"]})
            with self.lock:
                if self.children.get(alias) is child:
                    child["offline"] = False
                    self.adapter.sync(child, result)
        except LinkError:
            with self.lock:
                if self.children.get(alias) is child:
                    child["offline"] = True
                    self.adapter.offline(child)

    @account_store.admission(None)
    def adopt(self, target, alias, owner):
        target, alias = valid_ref(target), valid_name(alias)
        with self.lock:
            if self.parent:
                raise LinkError("An adopted terminal cannot adopt another terminal")
            if self.adapter.has_terminal(alias) or alias in self.children or any(p["alias"] == alias for p in self.pending.values()):
                raise LinkError("Terminal name is already in use")
            if len(self.pending) + len(self.children) >= 32:
                raise LinkError("Terminal relation capacity is full")
            if not self.adapter.manager_exists(owner):
                raise LinkError("Manager is unavailable")
            source = self.transport.source(target)
            if source == target:
                raise LinkError("A terminal cannot adopt itself")
            invite = {"id": uuid.uuid4().hex, "token": secrets.token_hex(32), "target": target,
                      "alias": alias, "owner": owner, "source": source, "expires": time.time() + INVITE_TTL}
            self.pending[invite["id"]] = invite
        try:
            self.transport.call(target, "offer", {k: invite[k] for k in ("id", "token", "alias", "owner", "expires")})
        except Exception:
            # Preserve the reservation for an uncertain response: the worker
            # may have received it. It cannot accept an expired reservation.
            raise
        return invite["id"]

    @account_store.admission(None)
    def prepare_created(self, alias, owner):
        """The /term creator consents for the CLI it is about to launch."""
        alias = valid_name(alias)
        with self.lock:
            if self.parent or self.adapter.has_terminal(alias) or alias in self.children or any(p["alias"] == alias for p in self.pending.values()):
                raise LinkError("Terminal name is unavailable or this terminal is controlled")
            if not self.adapter.manager_exists(owner) or len(self.children) + len(self.pending) >= 32:
                raise LinkError("Manager unavailable or terminal capacity full")
            instance = "term-" + uuid.uuid4().hex
            invite = {"id": uuid.uuid4().hex, "token": secrets.token_hex(32), "target": "local:" + instance,
                "alias": alias, "owner": owner, "source": self.transport.ref,
                "expires": time.time() + INVITE_TTL, "created": True}
            self.pending[invite["id"]] = invite
            return instance, dict(invite)

    @account_store.admission(None)
    def accept(self, invite_id):
        with self.lock:
            invite = self.invitations.get(invite_id)
            if not invite or invite["expires"] < time.time():
                raise LinkError("Unknown or expired invitation")
            if self.parent and self.parent["id"] != invite_id:
                raise LinkError("This terminal already has a controller")
            if self.children or self.pending or self.adapter.has_child_terminals():
                raise LinkError("A terminal with execution children cannot be adopted (two levels only)")
            if not self.adapter.idle() and not (self.parent and not self.parent.get("prepared")):
                raise LinkError("Finish active work before accepting a controller")
            # Reserve before I/O. Simultaneous inverse invitations cannot both
            # accept, and a third level cannot appear during the handshake.
            if self.parent is None:
                self.parent = dict(invite, contact=time.time(), prepared=True)
            inventory = self.adapter.inventory()
        result = self.transport.call(invite["parent"], "commit", {
            "id": invite_id, "token": invite["token"], "inventory": inventory})
        with self.lock:
            if self.parent is None or self.parent["id"] != invite_id:
                raise LinkError("Invitation was released during acceptance")
            if self.parent.get("prepared"):
                self.adapter.enter_child(invite)
            self.parent.update(prepared=False, contact=time.time(), offline=False)
        return result

    @account_store.admission({"ok": False, "error": "Account switch is in progress"})
    def dispatch(self, source, user_id, op, payload):
        try:
            source = valid_ref(source)
            if op not in OPS or not isinstance(payload, dict):
                raise LinkError("Invalid terminal operation")
            _json(payload)
            if source.startswith("remote:") and (not self.user_id or str(user_id) != self.user_id):
                raise LinkError("Remote terminal linking requires the same account")
            with self.lock:
                if self.stop.is_set():
                    raise LinkError("Terminal is shutting down")
                result = self._dispatch(source, str(user_id), op, payload)
            return {"ok": True, "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _dispatch(self, source, user_id, op, p):
        if op == "inspect":
            return {"user_id": self.user_id, "controlled": bool(self.parent),
                    "has_children": bool(self.children or self.pending or self.adapter.has_child_terminals())}
        if op == "offer":
            if self.parent or self.children or self.pending or self.adapter.has_child_terminals():
                raise LinkError("Terminal is not an independent leaf")
            key = str(p.get("id") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", key) or not re.fullmatch(r"[0-9a-f]{64}", str(p.get("token") or "")):
                raise LinkError("Invalid invitation")
            alias = valid_name(p.get("alias"))
            expires = min(float(p.get("expires", 0)), time.time() + INVITE_TTL)
            if expires <= time.time() or len(self.invitations) >= MAX_RECORDS:
                raise LinkError("Invitation expired or inbox full")
            row = {"id": key, "parent": source, "user_id": user_id, "token": p["token"],
                   "owner": str(p.get("owner") or ""), "alias": alias, "expires": expires}
            old = self.invitations.get(key)
            if old is not None and old != row:
                raise LinkError("Invitation identity conflict")
            if old is None:
                self.invitations[key] = row
                self.adapter.notice(f"Terminal {source} requests control as {alias}. Accept explicitly: /term accept {key}")
            return {"invitation": key}
        if op == "commit":
            current = next((row for row in self.children.values() if row["id"] == p.get("id")), None)
            if current is not None:
                self._check_child(current, source, p)
                return {"alias": current["alias"]}
            invite = self.pending.get(p.get("id"))
            if not invite or invite["expires"] < time.time():
                raise LinkError("Invitation is no longer valid")
            self._check_child(invite, source, p)
            if self.parent or not self.adapter.manager_exists(invite["owner"]):
                raise LinkError("Controller changed before acceptance")
            if invite.get("created") and not self.adapter.has_terminal(invite["alias"]):
                raise LinkError("Created terminal is still starting")
            inventory = p.get("inventory")
            if not isinstance(inventory, list) or not 1 <= len(inventory) <= 128:
                raise LinkError("Invalid Agent inventory")
            self.adapter.import_child(invite, inventory)
            self.children[invite["alias"]] = invite
            self.pending.pop(invite["id"], None)
            return {"alias": invite["alias"]}
        if op == "release":
            if self.parent and self.parent["parent"] == source:
                self._check_parent(source, user_id, p, allow_prepared=True)
                self._drop_parent()
                return {}
            child = next((row for row in self.children.values() if row["id"] == p.get("id")), None)
            if child is None:
                invitation = self.invitations.get(p.get("id"))
                if invitation and invitation["parent"] == source and hmac.compare_digest(invitation["token"], str(p.get("token") or "")):
                    self.invitations.pop(invitation["id"], None)
                pending = self.pending.get(p.get("id"))
                if pending:
                    self._check_child(pending, source, p)
                    self.pending.pop(pending["id"], None)
                return {}
            self._check_child(child, source, p)
            self._drop_child(child)
            return {}
        self._check_parent(source, user_id, p)
        self.parent.update(contact=time.time(), offline=False)
        if op == "status":
            self.parent.update(contact=time.time(), offline=False)
            inventory = [{k: row.get(k) for k in ("id", "tools", "status")}
                         for row in self.adapter.inventory()]
            jobs = self.adapter.jobs(self.parent["id"])
            # Status is a bounded summary. Keep every selected job's identity
            # and status even when many completed tasks have large outputs.
            def clip(value, size):
                raw = str(value or "").encode("utf-8")
                return raw[-size:].decode("utf-8", errors="ignore") if size else ""
            rows = [dict(job, task=clip(job.get("task"), 256),
                         error=clip(job.get("error"), 256), result="", result_truncated=True) for job in jobs]
            result = {"inventory": inventory, "jobs": rows}
            budget = MAX_BYTES - len(_json(result)) - 1024
            limit = max(0, budget // max(1, len(rows)) // 6)
            # JSON escaping can expand an input byte into six output bytes.
            for row, job in zip(rows, jobs):
                row["result"] = clip(job.get("result"), limit)
                row["result_truncated"] = row["result"] != str(job.get("result") or "")
            _json(result)
            return result
        if op == "assign":
            key = str(p.get("job_id") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", key):
                raise LinkError("Invalid job identity")
            fingerprint = hashlib.sha256(_json({k: p.get(k) for k in ("agent", "task", "tools")})).hexdigest()
            previous = self.jobs.get(key)
            if previous:
                if previous["relation"] != self.parent["id"] or previous["fingerprint"] != fingerprint:
                    raise LinkError("Job identity conflicts with another task")
                return previous["result"]
            if len(self.jobs) >= MAX_RECORDS:
                raise LinkError("Task request history is full; start a new runtime")
            result = self.adapter.start_job(self.parent["id"], key, p)
            if not result.get("ok"):
                raise LinkError(result.get("error") or "Task admission failed")
            self.jobs[key] = {"relation": self.parent["id"], "fingerprint": fingerprint, "result": result}
            return result
        if op == "deploy":
            return self.adapter.deploy(p.get("agent"))
        if op == "cancel":
            self.adapter.cancel_job(self.parent["id"], p.get("job_id"))
            return {}
        raise LinkError("Unknown operation")

    def _check_child(self, child, source, p):
        if child["id"] != p.get("id") or child["target"] != source or not hmac.compare_digest(child["token"], str(p.get("token") or "")):
            raise LinkError("Invalid child identity or relation token")

    def _check_parent(self, source, user_id, p, allow_prepared=False):
        parent = self.parent
        if (parent is None or parent["parent"] != source or parent["user_id"] != user_id
                or parent["id"] != p.get("id") or (parent.get("prepared") and not allow_prepared)
                or not hmac.compare_digest(parent["token"], str(p.get("token") or ""))):
            raise LinkError("Controller relation is invalid or has been released")

    def assign(self, agent, request, tools):
        with self.lock:
            child = self.children.get(agent.remote_terminal)
            if not child or child["owner"] != request.owner_id:
                raise LinkError("Agent is not owned by this controller")
            key = (child["id"], request.session_id, request.run_id, request.owner_id, request.request_id)
            if key not in self.outgoing:
                if len(self.outgoing) >= MAX_RECORDS:
                    raise LinkError("Terminal request history is full")
                self.outgoing[key] = {"id": child["id"], "token": child["token"], "agent": agent.remote_agent,
                    "task": request.task, "job_id": uuid.uuid4().hex, "tools": sorted(tools)}
            payload = self.outgoing[key]
            if payload["agent"] != agent.remote_agent or payload["task"] != request.task or payload["tools"] != sorted(tools):
                raise LinkError("Request identity conflicts with another task")
        result = self.transport.call(child["target"], "assign", payload)
        with self.lock:
            if self.children.get(child["alias"]) is not child:
                raise LinkError("Relation was released during task admission")
            self.adapter.admitted(agent, result, request.task)
        return result

    def deploy(self, agent, terminal, owner):
        with self.lock:
            child = self.children.get(agent.remote_terminal)
            if not child or child["owner"] != owner or terminal != child["alias"]:
                raise LinkError("Remote Agents can only be stationed in their original execution terminal")
            payload = {"id": child["id"], "token": child["token"], "agent": agent.remote_agent}
        result = self.transport.call(child["target"], "deploy", payload)
        self.tick()
        return result

    def cancel(self, agent, job_id):
        with self.lock:
            child = self.children.get(agent.remote_terminal)
            if not child:
                return
            payload = {"id": child["id"], "token": child["token"], "job_id": job_id}
        self.transport.call(child["target"], "cancel", payload)

    def _drop_parent(self):
        if self.parent:
            self.adapter.cancel_owned(self.parent["id"])
            self.adapter.leave_child()
        self.parent = None

    def _drop_child(self, child):
        self.children.pop(child["alias"], None)
        self.adapter.remove_child(child)

    def release(self, alias=None):
        with self.lock:
            if alias is None:
                if not self.parent:
                    raise LinkError("This terminal has no controller")
                row = dict(self.parent)
                self._drop_parent()
                target = row["parent"]
            else:
                row = self.children.get(alias)
                if row is None:
                    row = next((p for p in self.pending.values() if p["alias"] == alias), None)
                    if row is None:
                        raise LinkError("No adopted terminal with this name")
                    self.pending.pop(row["id"], None)
                else:
                    self._drop_child(row)
                target = row["target"]
        try:
            self.transport.call(target, "release", {"id": row["id"], "token": row["token"]})
        except LinkError:
            self.adapter.notice("Relation released locally; the other endpoint is offline. Its execution lease will expire")

    def close(self):
        self.stop.set()
        for alias in list(self.children):
            self.release(alias)
        if self.parent:
            self.release()
        self.transport.close()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(30)
        self.poller.shutdown(wait=True, cancel_futures=True)
        self.adapter.close()
