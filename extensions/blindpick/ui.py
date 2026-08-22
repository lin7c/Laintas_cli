"""blindpick arena: two models, one task, side by side, live.

Shape
-----
Two competitors run the same task at the same time, each in its own git
worktree, and this view shows both of them working — not a progress line,
the actual stream: what each one says, which tools it runs, what it edits.
When both finish you press a/b/t/x, and only then are the model names
revealed. Nothing on screen before that verdict can tell the two apart.

Why it is built this way
------------------------
* The live stream comes from ``agent_ui_events.hub``, the same per-agent
  event index /agents renders from. The round worker ingests both children
  into it, so this module only reads: no shadow execution path.
* The view registers itself as the terminal's full-screen owner
  (``laintas_cli._enter_agents_view``). That single registration is what
  makes an in-place round possible: approval requests raised by the round
  and by the two children route into the bar at the bottom of this screen
  instead of trying to draw a separate prompt over it, which is why the old
  version had to leave the workspace to start anything — and then showed
  nothing when it came back.
* Mutating actions run on worker threads, never in a key handler. A key
  handler that blocks is a UI that stops answering keys, and every approval
  needs the loop to stay alive to receive y/n.
* Round data is untrusted text: it is sanitised and emitted as plain
  fragments, never as style names or markup.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from typing import Any, Callable, Optional

try:
    import symbols as _sym
except Exception:  # pragma: no cover - the CLI always ships symbols.py
    class _Fallback:
        OK = "✓"; FAIL = "✗"; WARN = "⚠"; INFO = "›"; BULLET = "·"
        DOT = "●"; DOT_HALF = "◐"; DOT_OPEN = "○"
        SPINNER_RELAY = ("L·", "L›", "L»", "L›")
        SPINNER_BRAILLE = SPINNER_RELAY
        ARROW_U = "↑"; ARROW_D = "↓"
    _sym = _Fallback()


# ── palette (values cloned from agents_mode.STYLE so the two views match) ──
PALETTE = {
    "root":          "bg:#0d1117 #e6edf3",
    "brand":         "bold #a78bfa",
    "header":        "bold #4ade80",
    "muted":         "#8b949e",
    "subtle":        "#6e7681",
    "separator":     "#30363d",
    "side.a":        "bold #58a6ff",
    "side.b":        "bold #d2a8ff",
    "running":       "bold #3fb950",
    "thinking":      "bold #d29922",
    "queued":        "#8b949e",
    "done":          "#3fb950",
    "error":         "bold #f85149",
    "task":          "bold #f0f6fc",
    "text":          "#c9d1d9",
    "tool":          "#d2a8ff",
    "md.h":          "bold #f0f6fc",
    "md.list":       "#d2a8ff",
    "md.quote":      "italic #8b949e",
    "md.code":       "bg:#161b22 #c9d1d9",
    "diff.add":      "#3fb950",
    "diff.del":      "#f85149",
    "diff.hunk":     "bold #8b949e",
    "diff.file":     "bold #c9d1d9",
    "approval":      "bold #e3b341",
    "notice.ok":     "bold #4ade80",
    "notice.warn":   "bold #e3b341",
    "key":           "bold #c9d1d9",
    "input.label":   "bold #4ade80",
    "input.field":   "bg:#161b22 #f0f6fc",
    "win":           "bold #4ade80",
}

MIN_COLUMN = 24          # below this the two panes stack instead of splitting
EVENT_LIMIT = 1200       # per-side events pulled from the hub
DIFF_CAP = 400           # per-side diff lines rendered

_CTRL_RE = re.compile(
    "[\x1b\x00-\x08\x0b-\x1f\x7f​-‏  ﻿]")
_MARKUP_RE = re.compile(r"\[/?[a-z0-9 ._#=/-]+\]")

try:
    from wcwidth import wcwidth as _wcwidth
except Exception:  # pragma: no cover - wcwidth ships with the CLI
    _wcwidth = None


def _char_w(ch: str) -> int:
    if _wcwidth is not None:
        width = _wcwidth(ch)
        return width if width > 0 else 0
    return 1


def _disp_len(text: str) -> int:
    return sum(_char_w(ch) for ch in text)


def _clean(text: str) -> str:
    return _CTRL_RE.sub("", str(text or "")).replace("\t", "  ")


def _strip_markup(text: str) -> str:
    return _MARKUP_RE.sub("", str(text or ""))


def _crop(text: str, width: int) -> str:
    """Crop to a display-cell budget, ellipsis included in the budget."""
    text = _clean(text)
    if width <= 0:
        return ""
    total = 0
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            break
        w = _char_w(ch)
        if w > 0 and total + w > width:
            while out and total + 1 > width:
                total -= _char_w(out[-1])
                out.pop()
            return "".join(out) + "…"
        out.append(ch)
        total += w
    return "".join(out)


def _pad(text: str, cells: int) -> str:
    return text + " " * max(0, cells - _disp_len(text))


def _wrap(text: str, width: int) -> list[str]:
    """Hard-wrap on display cells. CJK has no spaces to break on, so the
    fold is by width, with word boundaries preferred when they exist."""
    text = _clean(text)
    if width <= 1:
        return [text[:1]]
    rows: list[str] = []
    line, total = "", 0
    for ch in text:
        w = _char_w(ch)
        if total + w > width:
            cut = line.rfind(" ")
            # Only break on a space when it is not at the very start of the
            # row; otherwise a long unbroken token would emit empty rows.
            if cut > width // 2:
                rows.append(line[:cut])
                line = line[cut + 1:]
                total = _disp_len(line)
            else:
                rows.append(line)
                line, total = "", 0
        line += ch
        total += w
    rows.append(line)
    return rows or [""]


def _age(seconds: float) -> str:
    try:
        secs = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _elapsed(row: dict) -> str:
    try:
        return _age(time.time() - float(row.get("created_at") or 0))
    except (TypeError, ValueError):
        return "?"


STATE_TEXT = {
    "running": "进行中", "pending": "等你裁决", "applied": "已应用",
    "discarded": "已丢弃", "tie": "平局", "both_bad": "都不行",
    "failed": "失败",
}
CHILD_STATE = {
    "queued": ("queued", "排队中"), "running": ("running", "执行中"),
    "thinking": ("thinking", "思考中"), "waiting": ("thinking", "等待中"),
    "done": ("done", "已完成"), "error": ("error", "出错"),
    "aborted": ("error", "已中止"), "idle": ("queued", "待启动"),
}


_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`")
_HEADING_RE = re.compile(r"^(#{1,6})\s+")
_BULLET_RE = re.compile(r"^\s*([-+*]|\d+[.)])\s+")
_RULE_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")


def _plain(text: str) -> str:
    """Drop inline emphasis markers, keep what they emphasised.

    Panes carry one style per line, so inline spans cannot be styled here —
    but leaving the raw ``**`` and backticks in is worse than not rendering
    at all: it is markdown that visibly failed to render.
    """
    return _EMPHASIS_RE.sub(
        lambda m: m.group(1) or m.group(2) or m.group(3) or "", text)


def _markdown_lines(text: str, width: int) -> list[tuple[str, str]]:
    """Render a model's closing message as block-level markdown.

    For a read-only task this message IS the deliverable — there is no diff
    to compare — so it has to be readable, not a raw dump with the asterisks
    still in it.
    """
    out: list[tuple[str, str]] = []
    fenced = False
    for raw in _clean(text).splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            out.append(("md.code", " " + _crop(raw, width - 2)))
            continue
        if not stripped:
            out.append(("", ""))
            continue
        if _RULE_RE.match(stripped):
            out.append(("separator", " " + "─" * max(4, width - 2)))
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            body = _plain(stripped[heading.end():])
            for row in _wrap(body, width - 2):
                out.append(("md.h", " " + row))
            continue
        if stripped.startswith(">"):
            for row in _wrap(_plain(stripped.lstrip("> ")), width - 4):
                out.append(("md.quote", "  " + row))
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet:
            marker = bullet.group(1)
            body = _plain(raw[bullet.end():])
            rows = _wrap(body, max(4, width - 5))
            head = f" {'·' if marker in '-+*' else marker} "
            out.append(("md.list", head + rows[0]))
            # Hanging indent so a wrapped item stays visibly one item.
            out.extend(("text", "   " + row) for row in rows[1:])
            continue
        for row in _wrap(_plain(raw), width - 2):
            out.append(("text", " " + row))
    while out and out[-1][1] == "":
        out.pop()
    return out


# ── live stream: one competitor's events, rendered as (style, text) lines ──

def _side_stream(agent_id: str, width: int) -> list[tuple[str, str]]:
    """Render one child agent's event stream.

    Deliberately narrower than the /agents feed: this pane is half a screen
    wide and is read while glancing between two of them, so tool calls
    collapse to one line each and only the assistant's own words get the
    full wrap treatment.
    """
    if not agent_id:
        return [("subtle", " 等待启动…")]
    try:
        import agent_ui_events
        _revision, events = agent_ui_events.hub.agent_events_snapshot(
            agent_id, limit=EVENT_LIMIT)
    except Exception:
        return [("subtle", " (无法读取事件流)")]
    lines: list[tuple[str, str]] = []
    stream = ""
    live: dict[str, int] = {}

    def flush() -> None:
        nonlocal stream
        if not stream.strip():
            stream = ""
            return
        for paragraph in _clean(stream).splitlines():
            if not paragraph.strip():
                lines.append(("", ""))
                continue
            for row in _wrap(paragraph, width - 2):
                lines.append(("text", " " + row))
        lines.append(("", ""))
        stream = ""

    for event in events:
        kind = event.event_type
        if kind == "ai_stream":
            stream += event.detail
            continue
        if kind in ("ai_end", "stream.end", "stream.reset"):
            flush()
            continue
        flush()
        if kind == "ai":
            stream = event.detail or event.summary
            flush()
        elif kind == "tool_started":
            lines.append(("tool", f" {_sym.DOT_HALF} "
                                  + _crop(event.summary, width - 4)))
            if event.tool_call_id:
                live[event.tool_call_id] = len(lines) - 1
        elif kind == "tool_finished":
            at = live.pop(event.tool_call_id, None)
            text = f" {_sym.DOT} " + _crop(event.summary, width - 4)
            if at is None:
                lines.append(("tool", text))
            else:
                lines[at] = ("tool", text)
        elif kind == "tool_output":
            for row in event.detail.splitlines()[-6:]:
                lines.append(("subtle", "   " + _crop(row, width - 5)))
        elif kind == "approval_requested":
            lines.append(("approval",
                          f" {_sym.WARN} 等待授权：" + _crop(event.summary,
                                                          width - 8)))
        elif kind == "approval_resolved":
            ok = event.status == "approved"
            lines.append(("done" if ok else "error",
                          f" {_sym.OK if ok else _sym.FAIL} "
                          + ("已授权" if ok else "已拒绝")))
        elif kind in ("agent_error", "input_rejected"):
            lines.append(("error", f" {_sym.FAIL} "
                                   + _crop(event.summary, width - 4)))
        elif kind == "agent_done":
            lines.append(("separator", " " + "─" * max(4, width - 2)))
        elif kind in ("agent_spawned", "agent_started", "user",
                      "user_message"):
            continue          # the task is already on screen, once, up top
        elif event.summary:
            lines.append(("subtle", f" {_sym.BULLET} "
                                    + _crop(event.summary, width - 4)))
    flush()
    if not lines:
        return [("subtle", " 还没有输出…")]
    while lines and lines[-1][1] == "":
        lines.pop()
    return lines


def _diff_stream(bp, row: dict, side: str, width: int) -> list[tuple[str, str]]:
    """Render one competitor's diff: per-file header, hunks only."""
    diff = bp._side_diff(row, side)
    chunks = bp._split_diff_files(_clean(diff))
    if not chunks:
        return [("subtle", " (这一侧没有产生任何改动)")]
    lines: list[tuple[str, str]] = []
    for path, chunk in chunks:
        _f, adds, dels = bp._diff_stat(chunk)
        lines.append(("diff.file",
                      " " + _crop(f"{path}  +{adds} −{dels}", width - 2)))
        in_hunk = False
        body = 0
        for line in chunk.splitlines():
            if len(lines) >= DIFF_CAP:
                lines.append(("subtle", "   … 截断，完整改动见分支"))
                return lines
            if line.startswith("diff --git "):
                in_hunk = False
                continue
            if line.startswith("@@"):
                in_hunk = True
                lines.append(("diff.hunk", "  " + _crop(line, width - 3)))
                body += 1
                continue
            if not in_hunk:
                continue
            style = ("diff.add" if line.startswith("+")
                     else "diff.del" if line.startswith("-") else "text")
            lines.append((style, "  " + _crop(line, width - 3)))
            body += 1
        if body == 0:
            lines.append(("subtle", "   (二进制或仅元数据变更)"))
    return lines


# ── the arena controller ───────────────────────────────────────────────────

class Arena:
    """One full-screen session over the whole round list."""

    def __init__(self, bp) -> None:
        self.bp = bp
        self.app = None
        self.rounds: list[dict] = []
        self._known: set = set()
        self.index = 0
        # A running round is watched live; a finished one is read as a diff
        # — its children are gone and the hub has already forgotten their
        # events, so "live" for a settled round is a blank pane. `d` pins a
        # choice for that one round.
        self.mode_override: dict[str, str] = {}
        self.scroll = -1                    # negative = stick to the tail
        self.notice = ""
        self.notice_kind = ""
        self.reveal: list[tuple[str, str]] = []
        self.composing = False
        self.busy = ""                      # what a worker thread is doing
        self._sig: Optional[tuple] = None
        self._confirm = ("", 0.0)
        self._approval_lock = threading.RLock()
        self._approvals: list[dict] = []
        self._closed = threading.Event()
        self._log: list[str] = []
        # Everything main.py prints while this view owns the screen lands
        # here — including the round worker's "round finished", which is
        # written from its own thread and would otherwise be painted over
        # the alternate screen.
        self.sink: list[str] = []
        self.refresh(force=True)
        # Land on something worth looking at rather than on row zero.
        for want in ("running", "pending"):
            for position, row in enumerate(self.rounds):
                if row.get("status") == want:
                    self.index = position
                    break
            else:
                continue
            break

    # ── data ──────────────────────────────────────────────────────────

    def refresh(self, force: bool = False) -> bool:
        try:
            stat = self.bp._rounds_path().stat()
            sig = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            sig = (None, None)
        if not force and sig == self._sig:
            return False
        self._sig = sig
        selected = self.round_id()
        known = self._known
        # Newest first: the round you just started is the one you watch.
        self.rounds = list(reversed(self.bp._load_rounds()))
        self._known = {str(row.get("round_id")) for row in self.rounds}
        # A round that did not exist a moment ago is the one you just asked
        # for: follow it. Keeping the old selection meant starting a round
        # and then watching some finished round instead — the new one was
        # live one keypress away and nothing said so.
        fresh = next((str(row.get("round_id")) for row in self.rounds
                      if str(row.get("round_id")) not in known
                      and str(row.get("status")) == "running"), "")
        if fresh and known:
            selected = fresh
            self.scroll = -1
            self.reveal = []
        if selected:
            for position, row in enumerate(self.rounds):
                if str(row.get("round_id")) == selected:
                    self.index = position
                    break
        self.index = max(0, min(self.index, max(0, len(self.rounds) - 1)))
        return True

    def current(self) -> Optional[dict]:
        return self.rounds[self.index] if self.rounds else None

    def round_id(self) -> str:
        row = self.current()
        return str(row.get("round_id")) if row else ""

    def order(self) -> list:
        row = self.current()
        return self.bp._round_order(row) if row else ["incumbent", "challenger"]

    def mode(self) -> str:
        """live while it runs, the answer when it is done, diff on request."""
        row = self.current()
        pinned = self.mode_override.get(self.round_id())
        if pinned:
            return pinned
        if row is not None and str(row.get("status")) == "running":
            return "live"
        return "reply"

    def side_of(self, label: str) -> str:
        return self.order()[0 if label == "A" else 1]

    def child_state(self, row: dict, side: str) -> tuple[str, str]:
        child_id = str(row.get(f"{side}_child") or "")
        status = str(row.get("status") or "")
        if not child_id:
            return ("queued", "排队中") if status == "running" else (
                "done", "已完成")
        try:
            import agent_loop
            info = agent_loop.get_agent(child_id)
        except Exception:
            info = None
        if info is None:
            return ("done", "已完成")
        return CHILD_STATE.get(str(info.status), ("queued", str(info.status)))

    def is_running(self) -> bool:
        return any(row.get("status") == "running" for row in self.rounds)

    # ── notices ───────────────────────────────────────────────────────

    def say(self, kind: str, text: str) -> None:
        self.notice_kind = kind
        self.notice = _strip_markup(text)
        self.invalidate()

    def invalidate(self) -> None:
        try:
            if self.app is not None and self.app.is_running:
                self.app.invalidate()
        except Exception:
            pass

    def on_event(self, _event) -> None:
        """Hub subscription: any agent activity redraws the arena."""
        self.invalidate()

    def drain(self) -> bool:
        """Surface what background work printed. True when something came."""
        lines: list[str] = []
        while self.sink:              # list.pop is atomic; no lock needed
            try:
                lines.append(self.sink.pop(0))
            except IndexError:
                break
        lines = [text for text in (_strip_markup(l).strip() for l in lines)
                 if text]
        if not lines:
            return False
        self._log = lines
        self.notice_kind = "ok"
        self.notice = lines[0]
        return True

    # ── approvals (bridged from worker threads, resolved with y/n) ─────

    def _request_approval(self, agent_id: str, kind: str,
                          summary: str, detail: str) -> bool:
        """laintas_cli._blocking_approval_prompt routes here while we own
        the screen — for the round's own sandbox gate and for anything the
        two competitors ask for."""
        request = {
            "id": f"bp-approval-{time.time_ns()}",
            "agent_id": str(agent_id or ""),
            "kind": str(kind or "confirm"),
            "summary": _strip_markup(summary),
            "detail": _strip_markup(detail),
            "done": threading.Event(),
            "approved": False,
        }
        with self._approval_lock:
            if self._closed.is_set():
                return False
            self._approvals.append(request)
        self.invalidate()
        while not request["done"].wait(timeout=0.2):
            if self._closed.is_set():
                with self._approval_lock:
                    if request in self._approvals:
                        self._approvals.remove(request)
                break
        return bool(request["approved"])

    def pending_approval(self) -> Optional[dict]:
        with self._approval_lock:
            return self._approvals[0] if self._approvals else None

    def resolve_approval(self, approved: bool) -> None:
        with self._approval_lock:
            if not self._approvals:
                return
            request = self._approvals.pop(0)
        request["approved"] = bool(approved)
        request["done"].set()
        self.say("ok" if approved else "warn",
                 "已授权" if approved else "已拒绝")

    def close_approvals(self) -> None:
        """Nothing may block on a screen that is going away."""
        with self._approval_lock:
            self._closed.set()
            pending, self._approvals = self._approvals, []
        for request in pending:
            request["approved"] = False
            request["done"].set()

    # ── actions (always on a worker thread) ───────────────────────────

    LOG_ROWS = 8

    def show_log(self, lines: list[str]) -> None:
        """Put an action's output in the panel above the notice line."""
        rows = [("text", " " + _crop(line, self.size()[0] - 2))
                for line in lines[:self.LOG_ROWS]]
        if len(lines) > self.LOG_ROWS:
            rows.append(("muted", f" … 还有 {len(lines) - self.LOG_ROWS} 行"))
        self.reveal = rows

    def run_action(self, label: str, func: Callable, *args: Any,
                   log: bool = True) -> None:
        """Run one main.py action off the UI loop, capturing its output.

        A key handler that calls this directly would freeze the loop, and
        the approval this very action raises could never be answered.
        """
        if self.busy:
            self.say("warn", f"正在{self.busy}，稍等")
            return
        self.busy = label

        def _worker() -> None:
            buffer: list[str] = []
            previous = self.bp.capture_output(buffer)
            try:
                func(*args)
            except Exception as exc:
                buffer.append(f"[内部错误] {type(exc).__name__}: {exc}")
            finally:
                self.bp.capture_output(previous)
                self.busy = ""
            lines = [_strip_markup(line).strip() for line in buffer]
            lines = [line for line in lines if line]
            self._log = lines
            if log and lines:
                # Multi-line output (a ratings table, a cleanup warning) is
                # the answer to what was asked; a one-line notice would drop
                # everything after the first line on the floor.
                self.show_log(lines)
            self.refresh(force=True)
            self.say("ok" if lines else "warn",
                     lines[0] if lines else f"{label}完成")

        threading.Thread(target=_worker, daemon=True,
                         name=f"blindpick-{label}").start()
        self.invalidate()

    def start_round(self, task: str) -> None:
        self.run_action("开局", self.bp._run_round, task)

    def judge(self, answer: str) -> None:
        row = self.current()
        if row is None or row.get("status") != "pending":
            self.say("warn", "这一局还不能裁决")
            return
        order = self.order()
        frozen = dict(row)

        def _decide() -> None:
            self.bp._pending_order[str(frozen.get("round_id"))] = (order, True)
            self.bp._do_pick(frozen, answer)

        # log=False: the verdict's own reveal is what belongs on screen,
        # and the worker finishes later — it would overwrite it.
        self.run_action("裁决", _decide, log=False)
        self.reveal = self._reveal_lines(frozen, order, answer)

    def _reveal_lines(self, row: dict, order: list,
                      answer: str) -> list[tuple[str, str]]:
        winner = {"a": order[0], "b": order[1]}.get(answer, "")
        out: list[tuple[str, str]] = []
        for label, side in zip(("A", "B"), order):
            model = self.bp._model_label(str(row.get(f"{side}_model") or ""))
            role = "当前模型" if side == "incumbent" else "挑战者"
            mark = f" {_sym.OK} 你选的" if side == winner else ""
            out.append(("win" if side == winner else "text",
                        f" 〔{label}〕 {model}（{role}）{mark}"))
        return out

    # ── geometry ──────────────────────────────────────────────────────

    @staticmethod
    def size() -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(100, 30))
        return size.columns, size.lines

    def chrome_rows(self) -> int:
        # header, rule, task, rule, notice, keys
        rows = 6 if self.current() is not None else 5
        if self.composing:
            rows += 1
        if self.reveal:
            rows += len(self.reveal) + 1
        if self.pending_approval() is not None:
            rows += 5
        return rows

    def body_height(self) -> int:
        return max(4, self.size()[1] - self.chrome_rows())

    def split(self) -> bool:
        """True when the two competitors get a column each."""
        return self.size()[0] >= (MIN_COLUMN * 2 + 3)

    def column_width(self) -> int:
        cols = self.size()[0]
        return (cols - 3) // 2 if self.split() else cols - 1

    # ── panes ─────────────────────────────────────────────────────────

    def head_line(self, label: str, row: dict, width: int) -> tuple[str, str]:
        """This side's own state and shape. Never the model behind it."""
        side = self.side_of(label)
        status = str(row.get("status") or "")
        if status == "running":
            style, text = self.child_state(row, side)
        elif status == "applied":
            # The verdict belongs to the round, not to a side: printing
            # "都不行" as each competitor's status read as if neither had
            # produced anything.
            won = str(row.get("applied_side")) == side
            style, text = ("win", f"{_sym.OK} 已采纳") if won else (
                "subtle", "未采纳")
        elif status == "failed":
            style, text = "error", "失败"
        else:
            style, text = "done", "已完成"
        shape = ""
        if status != "running":
            try:
                files, adds, dels = self.bp._diff_stat(
                    self.bp._side_diff(row, side))
                shape = (f"  {files} 文件 +{adds} −{dels}" if files
                         else "  无改动")
            except Exception:
                shape = ""
        head = f"〔{label}〕 {text}{shape}"
        return ("side.a" if label == "A" else "side.b", _pad(
            " " + _crop(head, width - 1), width))

    def pane_lines(self, label: str, width: int) -> list[tuple[str, str]]:
        row = self.current()
        if row is None:
            return []
        side = self.side_of(label)
        mode = self.mode()
        if mode == "diff":
            return _diff_stream(self.bp, row, side, width)
        if mode == "reply":
            return self._reply_lines(row, side, width)
        return _side_stream(str(row.get(f"{side}_child") or ""), width)

    def _reply_lines(self, row: dict, side: str,
                     width: int) -> list[tuple[str, str]]:
        """What this side ended up saying, plus the size of what it changed."""
        lines: list[tuple[str, str]] = []
        try:
            files, adds, dels = self.bp._diff_stat(
                self.bp._side_diff(row, side))
        except Exception:
            files = adds = dels = 0
        if files:
            lines.append(("muted", " " + _crop(
                f"改动 {files} 个文件 +{adds} −{dels} · 按 d 看 diff",
                width - 2)))
            lines.append(("", ""))
        reply = str(row.get(f"{side}_reply") or "")
        if reply.strip():
            lines += _markdown_lines(reply, width)
        elif files:
            lines.append(("subtle", " （没有留下说明，按 d 看改动）"))
        else:
            # Neither words nor changes: say so plainly instead of leaving an
            # empty pane that looks like a rendering failure.
            lines.append(("subtle", " 这一侧既没有改动，也没有留下说明。"))
            if str(row.get("status")) == "failed":
                lines.append(("error", " " + _crop(
                    str(row.get("error") or ""), width - 2)))
        return lines

    def body_lines(self) -> list[tuple[str, str]]:
        """The non-split fallback: the two panes stacked."""
        width = self.column_width()
        out: list[tuple[str, str]] = []
        row = self.current()
        if row is None:
            return []
        for label in ("A", "B"):
            out.append(self.head_line(label, row, width))
            out.extend(self.pane_lines(label, width))
            out.append(("", ""))
        return out

    def intro_lines(self, width: int) -> list[tuple[str, str]]:
        challenger = self.bp._challenger_label()
        incumbent = self.bp._incumbent()[0]
        return [
            ("brand", " 还没有对局"),
            ("", ""),
            ("text", " 同一个任务交给两个模型，各自在独立 worktree 里做，"),
            ("text", " 你在这里同时看着两边跑完，选一个更好的，"),
            ("text", " 然后才揭晓谁是谁 —— 只有胜者的改动进入工作区。"),
            ("", ""),
            ("text", f" 当前模型   {incumbent}"),
            ("text", " 挑战者     " + (challenger or "未设置，先按 c 选一个")),
            ("", ""),
            ("muted", " 按 " + ("n 输入任务开一局" if challenger
                                else "c 选挑战者，再按 n 开一局")),
        ]

    # ── fragments ─────────────────────────────────────────────────────

    def header_fragments(self):
        self.drain()
        self.refresh()
        row = self.current()
        parts = [("class:brand", " blindpick")]
        if row is None:
            parts += [("class:muted", f"  {_sym.BULLET}  "),
                      ("class:subtle", "空闲")]
            return parts
        status = str(row.get("status") or "")
        parts += [("class:muted", f"  {_sym.BULLET}  "),
                  ("class:subtle",
                   f"对局 {self.index + 1}/{len(self.rounds)}")]
        parts += [("class:muted", f"  {_sym.BULLET}  ")]
        if status == "running":
            frames = getattr(_sym, "SPINNER_RELAY", _sym.SPINNER_BRAILLE)
            spin = frames[int(time.monotonic() * 7) % len(frames)]
            parts += [("class:running", f"{spin} 两边同时执行中 "
                                        f"{_elapsed(row)}")]
        elif status == "pending":
            parts += [("class:approval", f"{_sym.DOT_HALF} 等你裁决")]
        else:
            parts += [("class:subtle", STATE_TEXT.get(status, status))]
        if self.busy:
            parts += [("class:muted", f"  {_sym.BULLET}  "),
                      ("class:thinking", f"{self.busy}中…")]
        parts += [("class:muted", f"  {_sym.BULLET}  "),
                  ("class:key", {"diff": "改动", "reply": "回复"}.get(
                      self.mode(), "实况"))]
        return parts

    def task_fragments(self):
        row = self.current()
        width = self.size()[0] - 2
        if row is None:
            return [("class:muted", "")]
        return [("class:task",
                 " " + _crop(" ".join(str(row.get("task") or "").split()),
                             width))]

    def _rows_for(self, label: str, width: int, height: int):
        row = self.current()
        if row is None:
            return []
        lines = [self.head_line(label, row, width),
                 ("separator", " " + "─" * max(2, width - 2))]
        lines += self.pane_lines(label, width)
        return self._window(lines, height)

    def _window(self, lines: list, height: int) -> list:
        """Apply the shared scroll offset; stick to the tail while live."""
        total = len(lines)
        if total <= height:
            return lines + [("", "")] * (height - total)
        maximum = total - height
        start = maximum if self.follow() else max(0, min(self.scroll, maximum))
        return lines[start:start + height]

    def follow(self) -> bool:
        """Tail the stream unless the reader has scrolled up on purpose."""
        return self.scroll < 0

    def column_fragments(self, label: str):
        width = self.column_width()
        height = self.body_height()
        frags = []
        for style, text in self._rows_for(label, width, height):
            frags.append((f"class:{style}" if style else "", _pad(text, width)))
            frags.append(("", "\n"))
        return frags

    def stacked_fragments(self):
        height = self.body_height()
        width = self.column_width()
        row = self.current()
        lines = (self.intro_lines(width) if row is None
                 else self.body_lines())
        frags = []
        for style, text in self._window(lines, height):
            frags.append((f"class:{style}" if style else "", text))
            frags.append(("", "\n"))
        return frags

    def reveal_fragments(self):
        if not self.reveal:
            return []
        frags = [("class:separator", " " + "─" * max(4, self.size()[0] - 2)),
                 ("", "\n")]
        for style, text in self.reveal:
            frags.append((f"class:{style}", text))
            frags.append(("", "\n"))
        return frags

    def approval_fragments(self):
        request = self.pending_approval()
        if request is None:
            return []
        width = self.size()[0] - 4
        with self._approval_lock:
            queued = len(self._approvals)
        head = f" {_sym.WARN} 需要授权"
        if queued > 1:
            head += f"（还有 {queued - 1} 个等待）"
        frags = [("class:approval", head), ("", "\n"),
                 ("class:text", " " + _crop(request["summary"], width)),
                 ("", "\n")]
        for line in request["detail"].splitlines()[:2]:
            frags += [("class:muted", "   " + _crop(line, width - 2)),
                      ("", "\n")]
        frags += [("class:key", " y"), ("class:text", " 允许    "),
                  ("class:key", "n"), ("class:text", " 拒绝"), ("", "\n")]
        return frags

    def notice_fragments(self):
        if self.notice:
            style = ("notice.warn" if self.notice_kind == "warn"
                     else "notice.ok")
            return [(f"class:{style}",
                     " " + _crop(self.notice, self.size()[0] - 2))]
        row = self.current()
        if row is not None and str(row.get("status")) == "pending":
            return [("class:approval",
                     " 两边都跑完了 —— a / b 选一个更好的，t 平局，x 都不行；"
                     "选完才揭晓模型名")]
        if row is not None and str(row.get("status")) == "failed":
            return [("class:error",
                     " " + _crop(str(row.get("error") or "失败"),
                                 self.size()[0] - 2))]
        return [("class:muted", "")]

    def key_fragments(self):
        row = self.current()
        cols = self.size()[0]
        if self.composing:
            items = [("Enter", "开局"), ("Esc", "取消")]
        elif self.pending_approval() is not None:
            items = [("y", "允许"), ("n", "拒绝")]
        else:
            items = []
            if row is not None and str(row.get("status")) == "pending":
                items += [("a", "A 更好"), ("b", "B 更好"),
                          ("t", "平局"), ("x", "都不行")]
            if row is not None:
                items += [("d", "改动/回复" if str(row.get("status")) != "running"
                           else "改动/实况"), ("PgUp/PgDn", "滚动")]
            items += [("n", "新对局"), ("c", "挑战者"),
                      ("[ ]", "切换对局"), ("v", "评分")]
            if row is not None:
                items.append(("D×2", "删除本局"))
        items.append(("q", "退出"))

        def width_of(rows) -> int:
            return sum(_disp_len(f" {k} {d} {_sym.BULLET}") for k, d in rows)

        while len(items) > 1 and width_of(items) > cols:
            items.pop(-2)
        frags = []
        for position, (key, desc) in enumerate(items):
            frags += [("class:key", f" {key}"), ("class:muted", f" {desc}")]
            if position < len(items) - 1:
                frags.append(("class:muted", f" {_sym.BULLET}"))
        return frags

    # ── the application ───────────────────────────────────────────────

    def build(self, input=None, output=None):
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import (
            ConditionalContainer, HSplit, Layout, VSplit, Window)
        from prompt_toolkit.layout.controls import (
            BufferControl, FormattedTextControl)
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.styles import Style

        buffer = Buffer(multiline=False)
        self.buffer = buffer
        composing = Condition(lambda: self.composing)
        awaiting = Condition(lambda: self.pending_approval() is not None)
        idle = ~composing & ~awaiting
        has_round = Condition(lambda: self.current() is not None)
        # Two columns only when there IS a round AND the terminal is wide
        # enough; otherwise one full-width pane. Without the round check the
        # empty-state text was drawn in the stacked pane while two blank
        # columns sat beside it.
        split = Condition(self.split) & has_round
        kb = KeyBindings()
        opened = time.monotonic()

        def in_grace() -> bool:
            # Typeahead from the REPL must not press a key for the user.
            return (time.monotonic() - opened) < 0.25

        def confirm(token: str) -> bool:
            """True when the same destructive key repeats within 3 seconds."""
            last, deadline = self._confirm
            now = time.monotonic()
            self._confirm = (token, now + 3.0)
            return last == token and now < deadline

        def leave_compose(event) -> None:
            self.composing = False
            buffer.reset()
            try:
                event.app.layout.focus(self.body_window)
            except Exception:
                pass
            event.app.invalidate()

        @kb.add("n", filter=idle)
        def _(event):
            if in_grace():
                return
            if not str(self.bp._state.get("challenger") or ""):
                self.say("warn", "先按 c 选一个挑战者模型")
                return
            if self.is_running():
                self.say("warn", "已经有一局在跑，等它结束")
                return
            self.composing = True
            buffer.reset()
            try:
                event.app.layout.focus(self.input_window)
            except Exception:
                pass
            event.app.invalidate()

        @kb.add("enter", filter=composing, eager=True)
        def _(event):
            task = buffer.text.strip()
            leave_compose(event)
            if task:
                # In place: no leaving the screen, no separate prompt. The
                # sandbox approval this raises appears in the bar below.
                self.start_round(task)

        @kb.add("escape", filter=composing, eager=True)
        def _(event):
            leave_compose(event)

        @kb.add("y", filter=awaiting)
        def _(event):
            self.resolve_approval(True)
            event.app.invalidate()

        @kb.add("n", filter=awaiting)
        def _(event):
            self.resolve_approval(False)
            event.app.invalidate()

        for key, answer in (("a", "a"), ("b", "b"), ("t", "tie"), ("x", "bad")):
            def _judge(event, _a=answer):
                if in_grace():
                    return
                self.judge(_a)
                event.app.invalidate()
            kb.add(key, filter=idle)(_judge)

        @kb.add("D", filter=idle)
        def _(event):
            if in_grace():
                return
            row = self.current()
            if row is None:
                return
            if str(row.get("status")) == "running":
                self.say("warn", "这一局还在跑，不能删除")
                event.app.invalidate()
                return
            rid = str(row.get("round_id"))
            pending = str(row.get("status")) == "pending"
            if not confirm("D" + rid):
                self.say("warn", "再按一次 D 确认删除这一局"
                                 + ("（还没裁决过）" if pending else "")
                                 + " —— worktree 和分支一起清掉")
                event.app.invalidate()
                return
            frozen = dict(row)
            self.reveal = []
            self.run_action("删除", lambda: self.bp._delete_rounds([frozen]))
            event.app.invalidate()

        @kb.add("d", filter=idle)
        def _(event):
            rid = self.round_id()
            if rid:
                row = self.current()
                natural = ("live" if str(row.get("status")) == "running"
                           else "reply")
                self.mode_override[rid] = (
                    natural if self.mode() == "diff" else "diff")
            self.scroll = -1
            event.app.invalidate()

        for key, delta in (("[", -1), ("]", 1),
                           ("left", -1), ("right", 1)):
            def _move(event, _d=delta):
                if not self.rounds:
                    return
                self.index = max(0, min(self.index + _d,
                                        len(self.rounds) - 1))
                self.scroll = -1
                self.reveal = []
                event.app.invalidate()
            kb.add(key, filter=idle)(_move)

        for key, delta in (("pageup", -10), ("pagedown", 10),
                           ("up", -3), ("down", 3)):
            def _scroll(event, _d=delta):
                if self.follow():
                    # Leaving tail mode: start from a large offset so the
                    # first PgUp moves off the end rather than to the top.
                    self.scroll = max(0, len(self.pane_lines(
                        "A", self.column_width())))
                self.scroll = max(0, self.scroll + _d)
                event.app.invalidate()
            kb.add(key, filter=idle)(_scroll)

        @kb.add("end", filter=idle)
        def _(event):
            self.scroll = -1
            event.app.invalidate()

        @kb.add("home", filter=idle)
        def _(event):
            self.scroll = 0
            event.app.invalidate()

        @kb.add("v", filter=idle)
        def _(event):
            if in_grace():
                return
            self.run_action("评分", self.bp._cmd_ratings)
            event.app.invalidate()

        @kb.add("c", filter=idle)
        def _(event):
            if in_grace():
                return
            # The model selector is its own full-screen dialog; it cannot
            # share the terminal, so this is the one action that steps out.
            event.app.exit(result="challenger")

        @kb.add("q", filter=idle)
        @kb.add("escape", filter=idle)
        def _(event):
            event.app.exit(result=None)

        @kb.add("c-c")
        def _(event):
            if self.composing:
                leave_compose(event)
                return
            event.app.exit(result=None)

        header = Window(FormattedTextControl(self.header_fragments,
                                             show_cursor=False), height=1)
        task_bar = Window(FormattedTextControl(self.task_fragments,
                                               show_cursor=False), height=1)
        column_a = Window(FormattedTextControl(
            lambda: self.column_fragments("A"), show_cursor=False),
            wrap_lines=False)
        column_b = Window(FormattedTextControl(
            lambda: self.column_fragments("B"), show_cursor=False),
            wrap_lines=False)
        stacked = Window(FormattedTextControl(self.stacked_fragments,
                                              show_cursor=False),
                         wrap_lines=False)
        self.body_window = stacked
        body = VSplit([
            ConditionalContainer(column_a, filter=split),
            ConditionalContainer(
                Window(width=1, char="│", style="class:separator"),
                filter=split),
            ConditionalContainer(column_b, filter=split),
            ConditionalContainer(stacked, filter=~split),
        ])
        self.input_window = Window(
            BufferControl(buffer, input_processors=[
                BeforeInput(" 任务描述 > ", style="class:input.label")]),
            height=1, style="class:input.field", wrap_lines=False)
        root = HSplit([
            header,
            Window(height=1, char="─", style="class:separator"),
            ConditionalContainer(task_bar, filter=has_round),
            body,
            ConditionalContainer(
                Window(FormattedTextControl(self.reveal_fragments,
                                            show_cursor=False),
                       height=lambda: len(self.reveal) + 1),
                filter=Condition(lambda: bool(self.reveal))),
            ConditionalContainer(
                Window(FormattedTextControl(self.approval_fragments,
                                            show_cursor=False), height=5),
                filter=awaiting),
            ConditionalContainer(self.input_window, filter=composing),
            Window(height=1, char="─", style="class:separator"),
            Window(FormattedTextControl(self.notice_fragments,
                                        show_cursor=False), height=1),
            Window(FormattedTextControl(self.key_fragments,
                                        show_cursor=False), height=1),
        ])
        self.app = Application(
            layout=Layout(root, focused_element=stacked),
            key_bindings=kb,
            style=Style.from_dict(PALETTE),
            full_screen=True,
            refresh_interval=None,
            min_redraw_interval=0.05,
            input=input, output=output)
        return self.app

    def run(self, input=None, output=None) -> Optional[str]:
        import agent_ui_events
        app = self.build(input=input, output=output)
        agent_ui_events.hub.subscribe(self.on_event)

        def _pre_run() -> None:
            async def _tick() -> None:
                # Hub events cover the two competitors' output. This covers
                # what they cannot: the elapsed clock, and the worker's own
                # writes to the rounds file — the transition from running to
                # "waiting for your verdict" happens AFTER the last agent
                # event, so without polling the file the arena would still
                # be showing a finished round as running when you press a.
                import asyncio
                while self.app is not None and not self.app.is_done:
                    live = self.is_running() or bool(self.busy)
                    await asyncio.sleep(0.25 if live else 0.8)
                    if self.app is None or self.app.is_done:
                        return
                    changed = self.refresh()
                    if self.drain() or changed or live:
                        self.app.invalidate()
            app.create_background_task(_tick())

        previous_sink = self.bp.capture_output(self.sink)
        try:
            return app.run(pre_run=_pre_run)
        except (KeyboardInterrupt, EOFError):
            return None
        finally:
            self.bp.capture_output(previous_sink)
            agent_ui_events.hub.unsubscribe(self.on_event)
            self.close_approvals()
            self.app = None


# ── session entry point ────────────────────────────────────────────────────

def run_ui(bp) -> None:
    """Own the terminal until the user quits.

    Registering as the CLI's full-screen owner is what makes approvals land
    in this view. Only the model selector — a full-screen dialog of its own
    — leaves and comes back.
    """
    import laintas_cli
    if laintas_cli._agents_view_is_active():
        bp._say("[yellow]另一个全屏视图正在占用终端（/agents），先退出它。[/yellow]")
        return
    while True:
        arena = Arena(bp)
        laintas_cli._enter_agents_view(arena)
        try:
            result = arena.run()
        finally:
            laintas_cli._exit_agents_view()
        if result != "challenger":
            return
        # Outside the alternate screen: the selector draws its own.
        try:
            bp._pick_challenger()
        except Exception as exc:
            bp._say(f"[red]选择挑战者失败：{exc}[/red]")
