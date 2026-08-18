"""Extension: blindpick

One challenger model competes against your current model on the same task.
Each side works in its own git worktree (created by this extension, seeded
with your uncommitted WIP), the finished work is committed on a private
branch, and you apply exactly one side's diff to your tree.

Two modes
---------
    /blindpick              fullscreen workspace (TTY) - /agents-style UI
                            in ui.py (rail + focus + blind judging)
    /blindpick <args>       direct subcommands, no blind selection:

        /blindpick challenger             pick the challenger (model selector)
        /blindpick run <task>             start a round (background thread)
        /blindpick show [id]              show a pending round (named)
        /blindpick pick <a|b|tie|bad> [id]  apply the chosen side
        /blindpick discard [id]           drop a pending round
        /blindpick status                 text status (also the non-TTY view)
        /blindpick ratings                Elo over every judged round
        /blindpick export [path]          judged rounds as DPO preference pairs
        /blindpick reset                  clear rounds and worktrees

Why this shape (and what the old version got wrong)
---------------------------------------------------
* The old version set the child's model override AFTER
  spawn_subagents_parallel returned — but the child thread is already
  running by then (agent_loop.spawn_subagent starts it before returning),
  so both sides often raced ahead on the default model. The model pin now
  goes through spawn_subagent(state_overrides=...), which is applied to the
  child's state before its thread is started.
* The old version committed WIP + child work into one commit and applied
  that whole diff back onto the parent tree that still held the WIP. Each
  side now makes a "baseline" commit first (the copied WIP), so the child's
  work is exactly one commit (baseline..result) and the applied patch
  contains only the child's changes.
* The old version blocked the main thread for up to 45 minutes. A round now
  runs on a background thread; /blindpick status reports progress and
  killing the CLI leaves recoverable, GC-able state behind.
* store.py shipped from the start and nothing ever called it, so a round's
  verdict lived exactly as long as the console line announcing it. Verdicts
  now reach the ledger (ratings, position bias, DPO export).
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

ROUND_TIMEOUT = 45 * 60
# Bounded copy of each side's diff kept on the round row. Only the fallback
# for rounds whose branches are gone — _side_diff regenerates from git first.
DIFF_KEEP = 60_000
ORPHAN_MAX_AGE = 24 * 3600
# Retention. A round costs two agent runs, a worktree and a branch per side;
# without a ceiling the record file, the repo's branch list and .laintas all
# grow forever. Settled rounds past EITHER limit are erased outright —
# records, worktrees and branches together, so nothing is left pointing at
# something that no longer exists. 0 disables a limit; both are overridable
# per project in blindpick_state.json (keep_rounds / keep_days).
KEEP_ROUNDS = 20
KEEP_DAYS = 14

STATUS_LABELS = {"applied": "已应用", "discarded": "已丢弃", "tie": "平局",
                 "both_bad": "都不行", "failed": "失败", "pending": "待裁决",
                 "running": "进行中"}
# A just-started round has no child ids yet (spawns happen inside the worker
# thread), so its liveness cannot be proven. /reload re-runs setup() and would
# otherwise reap a round that is actually alive.
REAP_GRACE = 90
RUN_LOCK = threading.Lock()

_ctx: Any = None
_state: dict = {"challenger": "", "challenger_provider": "",
                "keep_rounds": KEEP_ROUNDS, "keep_days": KEEP_DAYS}

# Where _say/_say_raw write. None = straight to the console. The workspace
# installs a sink for as long as it owns the alternate screen: the round
# worker is a background thread that announces "round finished" whenever it
# finishes, and printing that under a full-screen app scribbles over it.
_SINK_LOCK = threading.RLock()
_output_sink: Optional[list] = None


def capture_output(sink: Optional[list]) -> Optional[list]:
    """Point _say/_say_raw at `sink` (None = the console). Returns the old one."""
    global _output_sink
    with _SINK_LOCK:
        previous, _output_sink = _output_sink, sink
    return previous


def _emit(text: str, raw: bool) -> None:
    with _SINK_LOCK:
        sink = _output_sink
    if sink is not None:
        sink.append(str(text))
        return
    console = getattr(_ctx, "console", None)
    if console is not None and hasattr(console, "print"):
        if raw:
            console.print(text, markup=False, highlight=False)
        else:
            console.print(text)
    else:
        print(text)


def _say(text: str) -> None:
    _emit(text, raw=False)


def _hint(text: str) -> None:
    """Advice for the plain command line, skipped when a view is capturing.

    "run /blindpick status, then show + pick" is true at a prompt and false
    inside the arena, where the round is already on screen and judged with a
    single keypress.
    """
    with _SINK_LOCK:
        captured = _output_sink is not None
    if not captured:
        _say(text)


# ── state ──────────────────────────────────────────────────────────────────

def _project_dir() -> Path:
    return Path(_ctx.cwd) / ".laintas"


def _rounds_path() -> Path:
    return _project_dir() / "blindpick_rounds.jsonl"


def _positive(value: Any, fallback: int) -> int:
    """A non-negative int, or the default. 0 means "no limit"."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def _load_state() -> None:
    global _state
    path = _project_dir() / "blindpick_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _state = {
                "challenger": str(data.get("challenger") or ""),
                "challenger_provider": str(
                    data.get("challenger_provider") or ""),
                "keep_rounds": _positive(data.get("keep_rounds"), KEEP_ROUNDS),
                "keep_days": _positive(data.get("keep_days"), KEEP_DAYS),
            }
    except (OSError, ValueError):
        pass


def _save_state() -> None:
    path = _project_dir() / "blindpick_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_state, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _load_rounds() -> list[dict]:
    try:
        rows = [json.loads(line) for line in
                _rounds_path().read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if isinstance(row, dict) and row.get("round_id")]
    except (OSError, ValueError):
        return []


def _save_rounds(rounds: list[dict]) -> None:
    # Atomic replace: /blindpick status on the main thread reads this file
    # while the worker thread writes it. A plain open("w") truncates first,
    # so a reader racing the write sees a torn JSONL line and silently loses
    # EVERY round, not just the one being updated.
    path = _rounds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for row in rounds:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _update_round(round_id: str, **fields: Any) -> None:
    rounds = _load_rounds()
    for row in rounds:
        if row.get("round_id") == round_id:
            row.update(fields)
    _save_rounds(rounds)


def _in_progress() -> Optional[dict]:
    for row in _load_rounds():
        if row.get("status") == "running":
            return row
    return None


def _pending() -> list[dict]:
    return [row for row in _load_rounds() if row.get("status") == "pending"]


# ── models ─────────────────────────────────────────────────────────────────

def _incumbent() -> tuple[str, str, str]:
    """Resolve this terminal's current model, the same way /model does.

    Returns (label, model, provider). An empty model means auto-routing.
    """
    import laintas_cli
    model = provider = ""
    try:
        import agent_loop
        agent = laintas_cli.get_current_agent()
        terminal_name = agent_loop.agent_deployment_terminal(agent) or "term0"
        terminal = agent_loop.get_terminal(terminal_name)
        if terminal is not None:
            model = str(terminal.model_override or "")
            provider = str(terminal.provider_override or "")
    except Exception:
        pass
    if not model:
        model = laintas_cli.get_selected_model()
        if not provider:
            provider = laintas_cli.get_selected_provider()
    model = str(model or "").strip()
    return (_model_label(model), model, str(provider or ""))


def _model_label(model: str) -> str:
    # "" and "auto" both mean gateway auto-routing; showing the raw "auto"
    # would read as a model name.
    return "auto-routing" if model in ("", "auto") else model


def _challenger_label() -> str:
    """Display label for the configured challenger, "" when none is set.

    _model_label("") is "auto-routing", which is TRUTHY — status and the menu
    hint fed it straight into an `if challenger` and so reported a never-set
    challenger as a real model, while `run` still refused to start.
    """
    model = str(_state.get("challenger") or "").strip()
    return _model_label(model) if model else ""


def _pick_challenger() -> bool:
    """Open the same model selector /model uses; store the choice."""
    import laintas_cli
    incumbent_label, incumbent_model, _ = _incumbent()
    session = laintas_cli.load_session()
    if not session:
        _say("[red]未登录，无法获取模型列表（先 /login）。[/red]")
        return False
    try:
        with laintas_cli._safe_status(
                "[dim]正在获取可用模型… Esc/Ctrl+C 取消[/dim]"):
            models, _endpoint = laintas_cli.run_cancellable_blocking(
                lambda cancel: laintas_cli.fetch_available_models(
                    session, cancel_event=cancel))
    except laintas_cli.BlockingOperationCancelled:
        _say("[dim]已取消。[/dim]")
        return False
    except Exception as exc:
        _say(f"[red]获取模型列表失败：{exc}[/red]")
        return False
    if not models:
        _say("[red]后端没有返回任何可用模型。[/red]")
        return False
    selected = laintas_cli.show_model_selector(models, incumbent_model)
    if not selected:
        _say("[dim]已取消。[/dim]")
        return False
    challenger = str(selected.get("id") or "")
    provider = str(selected.get("provider") or "")
    if _model_label(challenger) == incumbent_label:
        _say(f"[red]挑战者不能和当前模型相同（当前：{incumbent_label}）。[/red]")
        return False
    _state["challenger"] = challenger
    _state["challenger_provider"] = provider
    _save_state()
    _say(f"[green]挑战者已设为 [bold]{_model_label(challenger)}[/bold]"
         f"（对阵当前模型 {incumbent_label}）。[/green]")
    return True


# ── git / worktree ─────────────────────────────────────────────────────────

def _git(cwd: str, *args: str, timeout: int = 60,
         stdin_text: Optional[str] = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, input=stdin_text, text=True,
            capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", (proc.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _repo_root(cwd: str) -> Optional[str]:
    code, out, _ = _git(cwd, "rev-parse", "--show-toplevel")
    return out.strip() if code == 0 and out.strip() else None


def _commit_all(wt_path: str, message: str) -> Optional[str]:
    """Commit everything left in the worktree; return the HEAD sha.

    .laintas is excluded: the child's own runtime writes task/graph state
    there (task_manager keys off cwd), and that must never leak into the
    patch that gets applied to the parent tree.
    """
    _git(wt_path, "add", "-A", "--", ":(exclude).laintas")
    code, out, err = _git(wt_path, "commit", "-m", message, "--no-verify")
    # "nothing to commit" is legitimate (baseline of a clean tree, or a
    # child that changed nothing); which stream it lands on varies by git
    # version, so check both.
    if code != 0 and "nothing to commit" not in (out + err):
        return None
    code, head, _ = _git(wt_path, "rev-parse", "HEAD")
    return head.strip() if code == 0 and head.strip() else None


def _cleanup_side(round_row: dict, side: str, keep_branch: bool) -> None:
    """Remove one side's worktree, and unless asked to keep it, its branch.

    The worktree and the branch are reclaimed independently. They used to be
    tied together behind a single "is there a worktree path?" check, so a
    round whose worktree was already gone — every applied round, whose loser
    branch is deliberately kept at decision time — could never have that
    branch reclaimed afterwards by anything at all.
    """
    root = str(round_row.get("repo_root") or "")
    if not root:
        return
    path = str(round_row.get(f"{side}_worktree") or "")
    branch = str(round_row.get(f"{side}_branch") or "")
    if path:
        code, _out, _err = _git(root, "worktree", "remove", "--force", path)
        if code != 0 and os.path.isdir(path):
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        _git(root, "worktree", "prune")
    if branch and not keep_branch:
        _git(root, "branch", "-D", branch)


def _cleanup_worktrees(round_row: dict) -> list[str]:
    """Remove both worktrees and both branches.

    Returns resources that could NOT be reclaimed (empty when fully clean),
    so callers can tell the user instead of silently leaking them.
    """
    leftovers: list[str] = []
    root = str(round_row.get("repo_root") or "")
    for side in ("incumbent", "challenger"):
        _cleanup_side(round_row, side, keep_branch=False)
        if not root:
            continue
        branch = str(round_row.get(f"{side}_branch") or "")
        path = str(round_row.get(f"{side}_worktree") or "")
        if branch:
            code, out, _ = _git(root, "branch", "--list", branch)
            if code == 0 and out.strip():
                leftovers.append(f"分支 {branch}")
        if path:
            code, out, _ = _git(root, "worktree", "list")
            if code == 0 and path in out:
                leftovers.append(f"worktree {path}")
    return leftovers


# ── deletion and retention ─────────────────────────────────────────────────
#
# Everything a round leaves behind is reachable from its record: two worktree
# paths and two branch names. So a round is deleted the only way that cannot
# leak — clean the resources FIRST, drop the record last. A record removed
# while its branch survives is a branch nothing will ever find again.

def _delete_rounds(victims: list[dict]) -> tuple[int, list[str]]:
    """Erase these rounds: worktrees, branches, then the records.

    Returns (deleted count, resources that could not be reclaimed).
    """
    if not victims:
        return 0, []
    doomed = {str(row.get("round_id")) for row in victims}
    leftovers: list[str] = []
    for row in victims:
        leftovers += _cleanup_worktrees(row)
    remaining = [row for row in _load_rounds()
                 if str(row.get("round_id")) not in doomed]
    _save_rounds(remaining)
    return len(victims), leftovers


def _prunable(rounds: list[dict], keep: int, days: int) -> list[dict]:
    """Settled rounds outside the retention window, oldest first.

    Running and pending rounds are never pruned at any age: a pending round
    is work you paid for and have not judged yet.
    """
    settled = [row for row in rounds
               if row.get("status") not in ("running", "pending")]
    victims: list[dict] = []
    if keep > 0 and len(settled) > keep:
        victims += settled[:len(settled) - keep]
    if days > 0:
        cutoff = time.time() - days * 86400
        for row in settled:
            try:
                created = float(row.get("created_at") or 0)
            except (TypeError, ValueError):
                continue
            if created and created < cutoff and row not in victims:
                victims.append(row)
    return victims


def _prune(keep: Optional[int] = None,
           days: Optional[int] = None) -> tuple[int, list[str]]:
    keep = _state.get("keep_rounds", KEEP_ROUNDS) if keep is None else keep
    days = _state.get("keep_days", KEEP_DAYS) if days is None else days
    return _delete_rounds(_prunable(_load_rounds(), int(keep), int(days)))


def _sweep_orphan_branches(max_age: float = ORPHAN_MAX_AGE) -> list[str]:
    """Delete blindpick sandbox branches no round record points at.

    Scoped three ways, because this deletes git branches: the reserved
    ``laintas/blindpick-`` prefix only, never one a round still references,
    and never one younger than the grace period (a branch whose round row has
    not been written yet is seconds old, not a day).
    """
    root = _repo_root(str(_ctx.cwd))
    if not root:
        return []
    live = {str(row.get(f"{side}_branch") or "")
            for row in _load_rounds()
            for side in ("incumbent", "challenger")}
    checked_out = ""
    code, out, _ = _git(root, "worktree", "list")
    if code == 0:
        checked_out = out
    code, listing, _ = _git(
        root, "for-each-ref", "--format=%(committerdate:unix) %(refname:short)",
        "refs/heads/laintas/blindpick-*")
    if code != 0:
        return []
    now = time.time()
    removed: list[str] = []
    for line in listing.splitlines():
        stamp, _, branch = line.strip().partition(" ")
        branch = branch.strip()
        if not branch or branch in live or f"[{branch}]" in checked_out:
            continue
        try:
            if now - float(stamp) < max_age:
                continue
        except ValueError:
            continue
        if _git(root, "branch", "-D", branch)[0] == 0:
            removed.append(branch)
    if removed:
        _git(root, "worktree", "prune")
    return removed


def _gc_orphans() -> None:
    """Reclaim worktrees left behind by a killed CLI or an old version."""
    root = _repo_root(str(_ctx.cwd))
    if not root:
        return
    wt_dir = Path(root) / ".laintas" / "worktrees"
    if not wt_dir.is_dir():
        return
    live = {str(r.get(f"{side}_worktree") or "")
            for r in _load_rounds()
            if r.get("status") in ("running", "pending")
            for side in ("incumbent", "challenger")}
    now = time.time()
    for item in sorted(wt_dir.iterdir()):
        if not item.is_dir() or not item.name.startswith("blindpick-"):
            continue
        try:
            age = now - item.stat().st_mtime
        except OSError:
            continue
        if str(item) in live or str(item.resolve()) in live:
            continue
        if age < ORPHAN_MAX_AGE:
            continue
        import worktree_manager
        try:
            worktree_manager.remove_worktree(worktree_manager.WorktreeInfo(
                path=str(item), branch=f"laintas/{item.name}",
                repo_root=root, base_commit=""))
        except Exception:
            pass


# ── the round ──────────────────────────────────────────────────────────────

def _run_round(task: str) -> bool:
    """Start one round: two worktrees, two children, one background thread."""
    challenger = str(_state.get("challenger") or "")
    if not challenger:
        _say("[red]还没有挑战者。先 /blindpick challenger 选一个（或进入 /blindpick 菜单）。[/red]")
        return False
    incumbent_label, incumbent_model, incumbent_provider = _incumbent()
    if _model_label(challenger) == incumbent_label:
        _say(f"[red]挑战者和当前模型相同（{incumbent_label}），没有对比意义。"
             f"换个挑战者或切换当前模型。[/red]")
        return False
    root = _repo_root(str(_ctx.cwd))
    if not root:
        _say(
            f"[red]无法开局：blindpick 探测的启动目录不在任何 git 仓库里。[/red]\n"
            f"blindpick 按 CLI [bold]启动时[/bold]的工作目录探测仓库，"
            f"启动后再 cd 到别处不会更新（本次探测的目录：{_ctx.cwd}）。\n"
            f"请退出后到 git 仓库根目录（或其子目录）重新启动 CLI，再运行 /blindpick。"
            f"退出请用 [bold]/quit[/bold]（[red]/exit[/red] 会同时清除登录态）。"
        )
        return False
    code, _, _ = _git(root, "rev-parse", "--verify", "HEAD")
    if code != 0:
        _say("[red]仓库还没有任何提交，无法开局（先 commit 一次）。[/red]")
        return False
    if _in_progress() is not None:
        _say("[red]已经有一局在跑，等它结束（/blindpick status）。[/red]")
        return False
    if not RUN_LOCK.acquire(blocking=False):
        _say("[yellow]上一局刚启动，稍等一秒再试。[/yellow]")
        return False

    round_id = uuid.uuid4().hex[:12]
    import agent_loop
    import worktree_manager

    # Resolve the parent on the CALLING thread: _current_agent_id is set by
    # the REPL loop, and reading it from the worker thread would race.
    parent = agent_loop.get_current_agent()
    if parent is None:
        RUN_LOCK.release()
        _say("[red]没有活动的 agent，无法派生子 agent。[/red]")
        return False

    # Worktrees are created on the CALLING thread so the approval below can
    # show the exact sandbox paths BEFORE anything is allowed to write, and
    # so a crash right after this point leaves no round row pointing at
    # resources that were never created.
    created: list = []
    sides: dict = {}
    try:
        for side in ("incumbent", "challenger"):
            info = worktree_manager.create_isolated_worktree(
                root, label=f"blindpick-{round_id}-{side}")
            # Freeze the copied WIP as a baseline commit so the child's work
            # is exactly one commit (baseline..result).
            base = _commit_all(info.path, "blindpick: baseline (WIP snapshot)")
            if base is None:
                raise RuntimeError(f"baseline commit failed for {side}")
            created.append(info)
            sides[side] = {"info": info, "base": base}
    except Exception as exc:
        for info in created:
            try:
                worktree_manager.remove_worktree(info)
            except Exception:
                pass
        RUN_LOCK.release()
        _say(f"[red]创建 worktree 失败：{exc}[/red]")
        return False

    # One approval up front, asked while the main thread owns the terminal.
    # Enforce mode would otherwise stop on every child file write - from a
    # background thread that cannot render the interactive prompt (the
    # renderer's asyncio loop belongs to the main thread). The sandboxes are
    # disposable git worktrees; the only path back into the user's tree
    # remains the explicit `pick` decision later.
    body = (
        "两个模型将在以下一次性沙箱（git worktree）中自由读写：\n"
        f"  · {sides['incumbent']['info'].path}  ({_model_label(incumbent_model)})\n"
        f"  · {sides['challenger']['info'].path}  ({_model_label(challenger)})\n"
        "沙箱与你的工作区完全隔离；只有你稍后 pick 的一侧 diff 会进入工作区。")
    approved = False
    try:
        import laintas_cli
        choice = laintas_cli._blocking_approval_prompt(
            "blindpick", body,
            "允许本轮两个子 agent 写入这两个沙箱？", allow_always=True)
        approved = str(choice) in ("yes", "always")
    except Exception:
        approved = False
    if not approved:
        for info in created:
            try:
                worktree_manager.remove_worktree(info)
            except Exception:
                pass
        RUN_LOCK.release()
        _say("[yellow]未授权沙箱写入，对局已取消（未产生模型调用）。[/yellow]")
        return False

    # Runtime sandbox write grant: children's fs-tool writes inside the
    # worktrees are pre-approved for exactly this round. denyFileWrite
    # patterns still take precedence inside policy.evaluate_file_write.
    import policy
    for side in ("incumbent", "challenger"):
        policy.grant_file_write_prefix(sides[side]["info"].path)

    def _teardown() -> None:
        for side in ("incumbent", "challenger"):
            try:
                policy.revoke_file_write_prefix(sides[side]["info"].path)
            except Exception:
                pass
            try:
                worktree_manager.remove_worktree(sides[side]["info"])
            except Exception:
                pass

    # The A/B mapping is fixed HERE, before either model runs. It used to be
    # drawn at display time, which made a live side-by-side view impossible to
    # keep blind: the two panes have to be labelled while the work is still
    # running, long before anyone asks to see a diff.
    order = ["incumbent", "challenger"]
    random.shuffle(order)
    round_row: dict = {
        "round_id": round_id,
        "created_at": time.time(),
        "display_order": order,
        "blind": True,
        "task": str(task),
        "repo_root": root,
        "parent_id": parent.id,
        "incumbent_model": incumbent_model,
        "incumbent_provider": incumbent_provider,
        "challenger_model": challenger,
        "status": "running",
        "incumbent_worktree": sides["incumbent"]["info"].path,
        "challenger_worktree": sides["challenger"]["info"].path,
        "incumbent_branch": sides["incumbent"]["info"].branch,
        "challenger_branch": sides["challenger"]["info"].branch,
        "incumbent_base": sides["incumbent"]["base"],
        "challenger_base": sides["challenger"]["base"],
    }
    try:
        _save_rounds(_load_rounds() + [round_row])
    except OSError as exc:
        _teardown()
        RUN_LOCK.release()
        _say(f"[red]无法写对局文件：{exc}[/red]")
        return False

    def _runner() -> None:
        try:
            _run_round_worker(round_row, sides)
        except Exception as exc:  # never let the daemon die silently
            _update_round(round_id, status="failed",
                          error=f"round crashed: {exc!r}")
        finally:
            RUN_LOCK.release()

    try:
        threading.Thread(target=_runner, daemon=True,
                         name=f"blindpick-{round_id}").start()
    except Exception as exc:  # start() failed -> release here; worker never ran
        _teardown()
        RUN_LOCK.release()
        _update_round(round_id, status="failed", error=f"thread start: {exc}")
        return False
    _say(f"[green]对局 {round_id[:8]} 已启动：[bold]{incumbent_label}[/bold] vs "
         f"[bold]{_model_label(challenger)}[/bold]（约两倍成本）。[/green]")
    _hint(f"  任务: {task[:100]}")
    _hint("  进度: /blindpick status · 完成后 /blindpick show + pick")
    return True


# Neutral worktree discipline appended to BOTH sides' task text. It must not
# leak which side a child is on, and it teaches the one rule the isolation
# depends on: relative paths only.
_SANDBOX_DISCIPLINE = (
    "\n\n[工作区纪律] 你在独立的一次性 git worktree 沙箱中完成任务："
    "所有文件读写一律使用相对路径（相对当前工作目录）；"
    "不要构造或改写本沙箱之外的任何路径，仓库其余部分与你无关。")


class _NullWriter:
    """File-like sink: Rich output from a child must never hit the screen."""

    encoding = "utf-8"

    @staticmethod
    def write(value) -> int:
        return len(str(value or ""))

    @staticmethod
    def flush() -> None:
        return None

    @staticmethod
    def isatty() -> bool:
        return False


def _child_deps(laintas_cli):
    """Execution wiring for a competitor: renders nothing, streams everything.

    Same shape as agents_mode._deps_for. While a full-screen view owns the
    terminal, a child printing a Rich panel or a live status from its worker
    thread corrupts the screen; the arena reads the children through the UI
    event hub instead, so their direct rendering is turned off at the source.
    """
    import copy as _copy
    from rich.console import Console
    deps = _copy.copy(laintas_cli.get_loop_deps())
    try:
        deps.console = Console(file=_NullWriter(), force_terminal=False,
                               width=100)
    except Exception:
        pass
    for renderer in ("display_command_output", "display_sub_terminal_preview",
                     "display_file_diff", "display_task_list"):
        if hasattr(deps, renderer):
            setattr(deps, renderer, lambda *_a, **_kw: None)
    return deps


def _run_round_worker(round_row: dict, sides: dict) -> None:
    import agent_loop
    import laintas_cli
    import policy
    import worktree_manager

    root = str(round_row["repo_root"])
    task = str(round_row["task"])
    incumbent_model = str(round_row.get("incumbent_model") or "")
    challenger_model = str(round_row.get("challenger_model") or "")
    # The parent was resolved on the calling thread in _run_round; reading
    # _current_agent_id from this worker thread would race the REPL.
    parent = agent_loop.get_agent(str(round_row.get("parent_id") or ""))
    if parent is None:
        _update_round(round_row["round_id"], status="failed",
                      error="parent agent is gone")
        return

    created = [sides[s]["info"] for s in ("incumbent", "challenger")]
    child_ids: dict = {}
    try:
        # The worktrees, baseline commits, and sandbox write grants were all
        # created on the CALLING thread (_run_round). This worker never builds
        # its own pair: a duplicate pair would leak worktrees outside the
        # one-shot approval. Pinning cwd AND _task_cwd makes spawn skip its
        # own worktree creation (this extension owns the worktrees) and keeps
        # the prompt env's CWD truthful - it renders from state, not from the
        # process-global os.getcwd().
        # 1) spawn both children. state_overrides lands in the child's state
        #    BEFORE its thread starts (agent_loop.spawn_subagent), so the
        #    model pin has no race. Setting cwd makes the spawn skip its own
        #    worktree creation — this extension owns the worktrees.
        session = laintas_cli.load_session() or {}
        terminal_name = ""
        try:
            terminal_name = str(agent_loop.agent_scope_terminal(parent) or "")
        except Exception:
            terminal_name = ""
        # The A/B label each side runs under. Child agent NAMES are derived
        # from it rather than from incumbent/challenger: the arena renders
        # agent-scoped events live, and a name like "blindpick-challenger"
        # would reveal the pairing before the verdict.
        order = [str(side) for side in (round_row.get("display_order") or [])]
        if len(order) != 2 or set(order) != {"incumbent", "challenger"}:
            order = ["incumbent", "challenger"]
        labels = {order[0]: "A", order[1]: "B"}
        child_of: dict = {}
        providers = {
            "incumbent": str(round_row.get("incumbent_provider") or ""),
            "challenger": str(_state.get("challenger_provider") or ""),
        }
        for side, model in (("incumbent", incumbent_model),
                            ("challenger", challenger_model)):
            overrides: dict = {
                "cwd": sides[side]["info"].path,
                "_task_cwd": sides[side]["info"].path,
            }
            # "auto" is the model selector's auto-routing virtual entry, not
            # a model id - pinning it literally would send model="auto" to the
            # gateway. No override means the child resolves like the parent.
            if model and model != "auto":
                overrides["_model_override"] = model
                # Both sides pin their provider: an unpinned incumbent whose
                # terminal sits on a non-default provider would silently run
                # the same model id somewhere else.
                if providers[side]:
                    overrides["_provider_override"] = providers[side]
            # Empty model (auto-routing incumbent): no override at all - the
            # child then resolves through the terminal exactly like the
            # parent does (agent_loop.resolve_agent_model).
            child_name = f"bp-{round_row['round_id'][:6]}-{labels[side]}"

            def _events(events, _side=side, _expected=child_name) -> None:
                # Per-agent index in the UI hub is what the arena reads to
                # stream both sides live. spawn_subagent starts the child's
                # thread before it returns, so the id is resolved lazily with
                # the requested name as the fallback for the first events.
                try:
                    import agent_ui_events
                    agent_ui_events.hub.ingest(
                        child_of.get(_side) or _expected, events,
                        terminal_name)
                except Exception:
                    pass

            # One deps — and therefore ONE rich Console — per side. Sharing
            # a console between two agents running at the same time is fatal:
            # the second one to open a live display dies with
            # LiveError("Only one live display may be active at once"), which
            # is exactly how every real round was losing its challenger.
            child_id = agent_loop.spawn_subagent(
                parent_id=parent.id, task=task + _SANDBOX_DISCIPLINE,
                deps=_child_deps(laintas_cli),
                name=child_name, session=session,
                events_cb=_events, state_overrides=overrides)
            if not child_id:
                raise RuntimeError(f"spawn failed for {side}")
            child_of[side] = child_id
            child_ids[side] = child_id
        _update_round(round_row["round_id"],
                      incumbent_child=child_ids["incumbent"],
                      challenger_child=child_ids["challenger"])

        # 2) wait for both, then commit whatever each child left behind.
        # One deadline for the round, not one per side: the two children run
        # in parallel, so a per-side timeout let the pair burn 2x ROUND_TIMEOUT
        # and reported "timed out after 45 min" for a side that had been
        # running for ninety.
        deadline = time.time() + ROUND_TIMEOUT
        for side in ("incumbent", "challenger"):
            child_id = child_ids[side]
            info = agent_loop.wait_for_agent(
                child_id, timeout=max(1.0, deadline - time.time()))
            if info is None:
                agent_loop.abort_agent(child_id)
                _update_round(round_row["round_id"], status="failed",
                              error=f"{side} 未在 {ROUND_TIMEOUT // 60} 分钟内完成")
                return
            if info.status == "error":
                _update_round(round_row["round_id"], status="failed",
                              error=f"{side} errored: {str(info.error)[:200]}")
                return
            result = _commit_all(sides[side]["info"].path, "blindpick: result")
            if result is None:
                _update_round(round_row["round_id"], status="failed",
                              error=f"result commit failed for {side}")
                return
            # branch/worktree/base were persisted by _run_round before the
            # thread started; only the result commit is new here. The child's
            # closing message is kept as judging context, and its run id is
            # the join key back into the gateway's training ledger.
            _update_round(round_row["round_id"], **{
                f"{side}_result": result,
                f"{side}_reply": str(getattr(info, "result", "") or "")[:2000],
                f"{side}_run_id": str(
                    (getattr(info, "state", None) or {}).get("_run_id") or ""),
            })

        # 3) the child's work, isolated from the WIP baseline
        fresh = _load_rounds_by_id(round_row["round_id"])
        for side in ("incumbent", "challenger"):
            code, out, _ = _git(root, "diff",
                                fresh[f"{side}_base"], fresh[f"{side}_result"],
                                "--", ":(exclude).laintas")
            if code != 0:
                _update_round(round_row["round_id"], status="failed",
                              error=f"diff failed for {side}")
                return
            _update_round(round_row["round_id"], **{f"{side}_diff": out[:DIFF_KEEP]})
        _update_round(round_row["round_id"], status="pending")
        _record_match(_load_rounds_by_id(round_row["round_id"]))
        _say(f"[green]对局 {round_row['round_id'][:8]} 完成，等你裁决。[/green]")
        _hint("  /blindpick show 看两边的结果，然后 /blindpick pick a|b|tie|bad")
    except Exception as exc:
        _update_round(round_row["round_id"], status="failed",
                      error=str(exc)[:300])
    finally:
        # The children are done in every path reaching here, so the sandbox
        # write grants die now regardless of outcome: a pending worktree must
        # be read-only evidence for the pick decision, not a live write
        # surface.
        for side in ("incumbent", "challenger"):
            try:
                policy.revoke_file_write_prefix(sides[side]["info"].path)
            except Exception:
                pass
        # Success leaves status="pending"; the worktrees stay until the user
        # decides. Every failure path marked the round failed first, so here:
        # abort whatever children are still alive and reclaim both worktrees.
        if _load_rounds_by_id(round_row["round_id"]).get("status") != "pending":
            for child_id in child_ids.values():
                try:
                    agent_loop.abort_agent(child_id)
                except Exception:
                    pass
            for info in created:
                try:
                    worktree_manager.remove_worktree(info)
                except Exception:
                    pass


def _load_rounds_by_id(round_id: str) -> dict:
    for row in _load_rounds():
        if row.get("round_id") == round_id:
            return row
    return {}


# ── the vote ledger ────────────────────────────────────────────────────────
#
# store.py has always held the durable half of this extension — one row per
# finished pair, one row per verdict, Elo over the verdicts, DPO export — but
# nothing called it, so every round's outcome died with the console line that
# announced it. These two functions are the whole bridge: record the pair when
# it becomes judgeable, record the verdict when it is judged.

_siblings: dict = {}


def _sibling(name: str):
    """Load a sibling module of this file, once, by path.

    By path rather than by name: the extension host loads main.py under a
    generated module name, so a bare `import store` would resolve against
    whatever sys.path happens to hold.
    """
    if name not in _siblings:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            f"blindpick_{name}", str(Path(__file__).with_name(f"{name}.py")))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _siblings[name] = module
    return _siblings[name]


def _store():
    return _sibling("store")


def _side_payload(round_row: dict, side: str) -> dict:
    return {
        "model": _model_label(str(round_row.get(f"{side}_model") or "")),
        "reasoning": str(round_row.get(f"{side}_reply") or "")[:4000],
        "action": str(round_row.get(f"{side}_diff") or "")[:20000],
        "run_id": str(round_row.get(f"{side}_run_id") or ""),
    }


def _record_match(round_row: dict) -> None:
    """Store the finished (still unjudged) pair; remember its id on the round."""
    if not round_row or round_row.get("match_id"):
        return
    try:
        root = str(round_row.get("repo_root") or "")
        code, head, _ = _git(root, "rev-parse", "HEAD") if root else (1, "", "")
        match_id = _store().record_match(
            _project_dir(),
            task=str(round_row.get("task") or ""),
            context_digest=head.strip() if code == 0 else "",
            left=_side_payload(round_row, "incumbent"),
            right=_side_payload(round_row, "challenger"))
        _update_round(str(round_row.get("round_id")), match_id=match_id)
    except Exception:
        pass  # a ledger write must never sink a finished round


def _record_vote(round_row: dict, winner: str, order: list) -> None:
    """Store the verdict. `winner` is a store-side key: left/right/tie/both_bad."""
    match_id = str(round_row.get("match_id") or "")
    if not match_id:
        return
    try:
        _store().record_vote(
            _project_dir(), match_id=match_id, winner=winner,
            # Which stored side was rendered first, so position bias stays
            # measurable after the fact.
            shown_first=("left" if (order or ["incumbent"])[0] == "incumbent"
                         else "right"))
    except Exception:
        pass


def _side_key(side: str) -> str:
    return "left" if side == "incumbent" else "right"


# ── judging / apply ────────────────────────────────────────────────────────

def _apply(round_row: dict, side: str) -> bool:
    """Apply the chosen side's diff (baseline..result) to the main tree."""
    root = str(round_row.get("repo_root") or "")
    base = str(round_row.get(f"{side}_base") or "")
    result = str(round_row.get(f"{side}_result") or "")
    if not (root and base and result):
        _say("[red]缺少分支信息，无法应用。[/red]")
        return False
    code, patch, _ = _git(root, "diff", "--binary", base, result,
                           "--", ":(exclude).laintas")
    if code != 0 or not patch.strip():
        _say(f"[yellow]这一侧（{_model_label(str(round_row.get(side + '_model') or ''))}）"
             f"没有产生任何改动。[/yellow]")
        return False
    code, _out, err = _git(root, "apply", "-", stdin_text=patch)
    if code != 0:
        _say(f"[red]补丁未能干净应用 —— 工作区在这局期间变了（git 未改动你的文件）。[/red]")
        _say_raw(f"  {err.splitlines()[0] if err else 'apply failed'}")
        for name in ("incumbent", "challenger"):
            _say(f"  分支保留：{round_row.get(name + '_branch')} "
                 f"({_model_label(str(round_row.get(name + '_model') or ''))})")
        _say("  手动处理后可 /blindpick discard 清理 worktree。")
        return False
    # Count files with numstat, not by grepping "+++ " out of the patch body:
    # an added line whose own text starts with "++ " renders as "+++ ..." and
    # was inflating the count.
    code, stat, _ = _git(root, "diff", "--numstat", base, result,
                         "--", ":(exclude).laintas")
    changed = (len([l for l in stat.splitlines() if l.strip()])
               if code == 0 else _diff_stat(patch)[0])
    loser = "challenger" if side == "incumbent" else "incumbent"
    # Neutral wording on purpose: in the UI's blind flow the caller reveals
    # model names only AFTER the decision, and _apply runs before that.
    _cleanup_side(round_row, side, keep_branch=False)
    _cleanup_side(round_row, loser, keep_branch=True)
    _update_round(round_row["round_id"], status="applied",
                  applied_side=side, applied_at=time.time())
    _say(f"[green]已应用所选一侧的改动，{changed} 个文件。[/green]")
    _say(f"  落选分支保留待查：{round_row.get(loser + '_branch')}，"
         f"不要了就 git branch -D")
    return True


def _settle(round_row: dict, status: str, order: Optional[list] = None) -> None:
    """Close a round without applying anything; reclaim worktrees/branches."""
    if status in ("tie", "both_bad"):
        _record_vote(round_row, status, order or ["incumbent", "challenger"])
    leftovers = _cleanup_worktrees(round_row)
    _update_round(round_row["round_id"], status=status, decided_at=time.time())
    label = {"discarded": "已丢弃", "tie": "平局，两边都未应用",
             "both_bad": "两边都不行，都未应用"}.get(status, status)
    if leftovers:
        _say(f"[yellow]对局 {round_row['round_id'][:8]}：{label}，"
             f"但以下资源未能自动清理：[/yellow]")
        for item in leftovers:
            _say(f"  · {item}")
        _say("  手动清理：git worktree remove --force <路径> / git branch -D <名>")
    else:
        _say(f"[yellow]对局 {round_row['round_id'][:8]}：{label}，"
             f"worktree 与分支已清理。[/yellow]")


MAX_FILES_SHOWN = 12


def _split_diff_files(diff_text: str) -> list[tuple[str, str]]:
    """Split a multi-file unified diff into (path, single-file diff) pairs."""
    files: list[tuple[str, str]] = []
    path = ""
    buf: list[str] = []
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            if buf:
                files.append((path, "\n".join(buf)))
            buf = []
            # Take the b/ side: the post-image name, and the surviving name
            # across a rename.
            head, sep, tail = line.partition(" b/")
            path = tail.strip() if sep else line[len("diff --git "):].strip()
        buf.append(line)
    if buf:
        files.append((path, "\n".join(buf)))
    return files


def _diff_stat(diff_text: str) -> tuple[int, int, int]:
    """(files, additions, deletions) for a unified diff.

    Counted inside hunks rather than by "starts with + but not +++": a source
    line whose own text begins with "++ " is indistinguishable from a file
    header under that rule, and the ribbon above a diff should not be able to
    disagree with the diff below it.
    """
    files = adds = dels = 0
    in_hunk = False
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            files += 1
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if line.startswith("+"):
                adds += 1
            elif line.startswith("-"):
                dels += 1
    return files, adds, dels


def _side_diff(round_row: dict, side: str) -> str:
    """This side's changes, regenerated from git whenever the commits survive.

    The copy on the round row is capped, so judging off it means judging a
    diff that was silently cut mid-hunk. baseline..result is still in the
    object store until the branch is pruned and gc'd, so ask git first and
    keep the stored copy as the fallback for old/pruned rounds.
    """
    root = str(round_row.get("repo_root") or "")
    base = str(round_row.get(f"{side}_base") or "")
    result = str(round_row.get(f"{side}_result") or "")
    if root and base and result:
        code, out, _ = _git(root, "diff", base, result,
                            "--", ":(exclude).laintas")
        if code == 0:
            return out
    return str(round_row.get(f"{side}_diff") or "")


def _render_side(head: str, round_row: dict, side: str) -> None:
    """One side's work: a stat ribbon, the model's own summary, the diff."""
    diff_text = _side_diff(round_row, side)
    files, adds, dels = _diff_stat(diff_text)
    _say(f"[bold]{head}[/bold]  [dim]{files} 个文件[/dim] "
         f"[green]+{adds}[/green] [red]−{dels}[/red]")
    reply = str(round_row.get(f"{side}_reply") or "").strip()
    if reply:
        _say_raw("  说明: " + " ".join(reply.split())[:240])
    if not diff_text.strip():
        _say("  [dim](这一侧没有产生任何改动)[/dim]\n")
        return
    chunks = _split_diff_files(diff_text)
    rendered = False
    render = None
    with _SINK_LOCK:
        captured = _output_sink is not None
    # The CLI's file-diff view writes to its own console, which would walk
    # straight past an installed sink and onto the workspace's screen.
    if not captured:
        try:
            import laintas_cli
            render = laintas_cli.display_file_diff
        except Exception:
            render = None
    if render is not None:
        for path, chunk in chunks[:MAX_FILES_SHOWN]:
            try:
                render(path or "(unknown)", chunk, depth=1)
                rendered = True
            except Exception:
                rendered = False
                break
    if not rendered:
        # Fallback: raw text, still without Rich markup parsing.
        _say_raw(diff_text[:6000])
        if len(diff_text) > 6000:
            _say("  [dim]…（已截断，完整改动见分支）[/dim]")
    elif len(chunks) > MAX_FILES_SHOWN:
        _say(f"  [dim]… 还有 {len(chunks) - MAX_FILES_SHOWN} 个文件未展开"
             f"（完整改动见分支 {round_row.get(side + '_branch')}）[/dim]")
    _say("")


def _round_order(round_row: dict) -> list:
    """The A/B mapping fixed when the round was created."""
    order = [str(side) for side in (round_row.get("display_order") or [])]
    if len(order) != 2 or set(order) != {"incumbent", "challenger"}:
        return ["incumbent", "challenger"]
    return order


def _show_round(round_row: dict, blind: bool) -> list:
    """Print both diffs. Returns the display order (which side was A/B)."""
    order = _round_order(round_row)
    _say(f"[bold]── 对局 {str(round_row.get('round_id'))[:8]} · 任务 ──[/bold]")
    _say_raw(str(round_row.get("task") or "")[:600])
    _say("")
    for label, side in zip(("A", "B"), order):
        model = str(round_row.get(f"{side}_model") or "")
        head = f"【{label}】" if blind else f"【{label}】{_model_label(model)}"
        _render_side(head, round_row, side)
    if blind:
        _say("[dim]模型名已隐藏，裁决后揭晓。[/dim]")
    else:
        _say("[dim]A/B 的归属每局随机，见上面各自的标注 · "
             "/blindpick pick a|b|tie|bad[/dim]")
    return order


# ── judging entry points ───────────────────────────────────────────────────

# round_id -> (display_order, was_blind). Set by _show_round so a later pick
# knows which side "a"/"b" referred to, and whether a reveal is owed.
_pending_order: dict[str, tuple] = {}


def _say_raw(text: str) -> None:
    """Print without Rich markup - diffs and user task text can contain
    brackets that would otherwise be parsed (or raise) as markup."""
    _emit(text, raw=True)


def _reveal(round_row: dict, order: list, winner: str = "") -> None:
    """Name both sides after the verdict, and mark the one that won."""
    for label, side in zip(("A", "B"), order):
        model = _model_label(str(round_row.get(side + "_model") or ""))
        role = "当前模型" if side == "incumbent" else "挑战者"
        mark = "  [green]← 你选的[/green]" if side == winner else ""
        _say(f"[dim]揭晓[/dim] {label} = [bold]{model}[/bold]（{role}）{mark}")


def _do_pick(round_row: dict, answer: str) -> None:
    rid = str(round_row.get("round_id"))
    cached = _pending_order.get(rid)
    if cached is not None:
        order, blind = cached
    else:
        # After /reload (or a new session) the in-memory map is gone. Fall
        # back to the order persisted at show time; a stale default here
        # would silently flip which side "a" refers to.
        order = _round_order(round_row)
        blind = bool(round_row.get("blind"))
    answer = answer.lower()
    side = {"a": order[0], "b": order[1]}.get(answer)
    if side is None:
        if answer not in ("tie", "bad"):
            _say_raw(_PICK_USAGE)
            return
        _settle(round_row, "tie" if answer == "tie" else "both_bad", order)
        if blind:
            _reveal(round_row, order)
        _pending_order.pop(rid, None)
        return
    if _apply(round_row, side):
        _record_vote(round_row, _side_key(side), order)
        if blind:
            _reveal(round_row, order, winner=side)
        _pending_order.pop(rid, None)
    # On apply failure the round stays pending: keep the A/B order cached so
    # a retried `pick a` still maps to the same side the user judged.


def _show(round_row: dict, blind: bool) -> None:
    """Render a round and remember the mapping a later `pick` will use."""
    order = _show_round(round_row, blind)
    rid = str(round_row.get("round_id"))
    _pending_order[rid] = (order, blind)
    # The order itself was persisted at creation; only re-save it for rounds
    # created by an older version that had none.
    if not round_row.get("display_order"):
        _update_round(rid, display_order=order, blind=blind)


# ── status ─────────────────────────────────────────────────────────────────

def _child_state(child_id: str) -> str:
    if not child_id:
        return "—"
    try:
        import agent_loop
        info = agent_loop.get_agent(str(child_id))
        return str(info.status) if info else "已结束"
    except Exception:
        return "—"


def _round_summary(row: dict) -> str:
    """One line describing a finished round, safe to print without markup."""
    status = str(row.get("status") or "")
    label = STATUS_LABELS.get(status, status or "?")
    if status == "applied":
        side = str(row.get("applied_side") or "")
        role = "当前模型" if side == "incumbent" else "挑战者"
        return (f"{label} {_model_label(str(row.get(side + '_model') or ''))}"
                f"（{role}）")
    if status == "failed":
        return f"{label} {str(row.get('error') or '')[:120]}"
    return label


def _cmd_status() -> None:
    incumbent_label, _, _ = _incumbent()
    challenger = _challenger_label()
    _say(f"当前模型  [bold]{incumbent_label}[/bold]")
    if challenger:
        _say(f"挑战者    [bold]{challenger}[/bold]")
    else:
        _say("挑战者    [dim]未设置 · /blindpick challenger[/dim]")
    running = _in_progress()
    if running:
        mins = (time.time() - float(running.get("created_at") or 0)) / 60
        _say(f"\n进行中    {str(running.get('round_id'))[:8]} · "
             f"已跑 {mins:.0f} 分钟 / 上限 {ROUND_TIMEOUT // 60} 分钟")
        _say_raw(f"          任务 {str(running.get('task') or '')[:120]}")
        for side, role in (("incumbent", "当前模型"), ("challenger", "挑战者")):
            cid = str(running.get(f"{side}_child") or "")
            _say(f"          {role}: {_child_state(cid)}")
    pending = _pending()
    if pending:
        _say(f"\n待裁决    {len(pending)} 局 · /blindpick show")
        for row in pending:
            _say_raw(f"          {str(row.get('round_id'))[:8]}  "
                     f"{str(row.get('task') or '')[:80]}")
    else:
        _say("\n待裁决    无")
    rounds = _load_rounds()
    keep = int(_state.get("keep_rounds", KEEP_ROUNDS))
    days = int(_state.get("keep_days", KEEP_DAYS))
    limits = " · ".join(
        part for part in ((f"最多 {keep} 局" if keep else ""),
                          (f"{days} 天" if days else "")) if part)
    _say(f"\n留存      {len(rounds)} 局在册 · "
         + (f"自动清理（{limits}）" if limits else "不自动清理")
         + " · /blindpick prune")
    done = [r for r in rounds if r.get("status") in
            ("applied", "discarded", "tie", "both_bad", "failed")]
    if done:
        _say("\n最近")
        for row in done[-3:]:
            _say_raw(f"          {str(row.get('round_id'))[:8]}  "
                     f"{_round_summary(row)}")


def _cmd_ratings() -> None:
    """The accumulated verdicts: Elo per model, plus a position-bias check."""
    try:
        store = _store()
        table = store.ratings(_project_dir())
        bias = store.position_bias(_project_dir())
    except Exception as exc:
        _say(f"[red]读取对局记录失败：{exc}[/red]")
        return
    if not table:
        _say("[dim]还没有任何裁决记录。跑一局并 pick 之后这里才有数。[/dim]")
        return
    rows = sorted(table.values(), key=lambda r: -r["rating"])
    width = max(len(r["model"]) for r in rows)
    _say(f"[bold]{'模型'.ljust(width)}   评分    胜  负  平   局数[/bold]")
    for row in rows:
        _say_raw(f"{row['model'].ljust(width)}  {row['rating']:6.0f}  "
                 f"{row['wins']:3d} {row['losses']:3d} {row['ties']:3d}  "
                 f"{row['matches']:5d}")
    rate = bias.get("rate")
    if rate is None:
        return
    note = (f"\n位置偏差  先显示的一侧胜率 {rate * 100:.0f}%"
            f"（{bias['decisive']} 次有胜负的裁决）")
    # Far from 50% means the ordering is doing the choosing. Flagged rather
    # than corrected: the fix is more blind rounds, not a fudge factor.
    if bias["decisive"] >= 8 and abs(rate - 0.5) > 0.2:
        _say(f"[yellow]{note} —— 偏离 50% 太多，这更像在测布局而不是质量。[/yellow]")
    else:
        _say(f"[dim]{note}[/dim]")


def _cmd_export(target: str = "") -> None:
    """Export judged rounds as DPO preference pairs."""
    out = (Path(target).expanduser() if target
           else _project_dir() / "blindpick" / "preferences.jsonl")
    try:
        written = _store().export_preferences(_project_dir(), out)
    except Exception as exc:
        _say(f"[red]导出失败：{exc}[/red]")
        return
    if not written:
        _say("[dim]没有可导出的偏好对（平局和“都不行”不计入）。[/dim]")
        return
    _say(f"[green]已导出 {written} 组偏好对 → {out}[/green]")


# ── the arena (TTY) ─────────────────────────────────────────────────────────

def _open_arena() -> None:
    """Open the full-screen side-by-side view.

    Every mutation stays in this module: ui.py reads state through it and
    calls these same functions, so the arena and the direct subcommands can
    never drift into two behaviours.
    """
    _sibling("ui").run_ui(sys.modules[__name__])


# ── commands ───────────────────────────────────────────────────────────────

def _round_still_alive(row: dict) -> bool:
    """True if any of the round's child agents is still scheduled/running.

    Guards _reap_interrupted: an in-process /reload reinstalls the module
    while an old worker thread is still mid-round. Those rounds must not be
    reaped just because setup() ran again.
    """
    try:
        import agent_loop
        for side in ("incumbent", "challenger"):
            cid = str(row.get(f"{side}_child") or "")
            if not cid:
                continue
            info = agent_loop.get_agent(cid)
            if info is not None and info.status in (
                    "queued", "running", "thinking", "waiting"):
                return True
    except Exception:
        pass
    return False


def _reap_interrupted() -> None:
    """Fail rounds left status=running by a killed CLI.

    Workers are daemon threads: if the process died, nothing is coming to
    finish them, and a phantom running round would block every new round
    forever. Worktrees are left alone here - they dropped out of the live
    set, so the 24h GC reclaims them.
    """
    rounds = _load_rounds()
    dirty = False
    now = time.time()
    for row in rounds:
        if row.get("status") != "running" or _round_still_alive(row):
            continue
        # Spawn window: the round row is on disk with status=running before
        # any child id is, so liveness cannot be proven yet. Wait out the
        # grace period instead of reaping a round that just started. (If the
        # children finished but the worker is still committing, a reap here
        # still self-heals: the worker's final status write lands later.)
        try:
            age = now - float(row.get("created_at") or 0)
        except (TypeError, ValueError):
            age = REAP_GRACE + 1
        if age < REAP_GRACE:
            continue
        row["status"] = "failed"
        row["error"] = "interrupted (CLI restarted or killed); worktree reaped by GC"
        dirty = True
    if dirty:
        _save_rounds(rounds)


def _cmd_delete(token: str) -> None:
    """Erase one round by id prefix, whatever state it is in."""
    token = token.strip().lower()
    hits = [row for row in _load_rounds()
            if str(row.get("round_id", "")).lower().startswith(token)]
    if len(hits) != 1:
        _say(f"[red]{'没有' if not hits else '不止一个'}对局匹配 “{token}”。[/red]")
        for row in _load_rounds():
            _say_raw(f"  {str(row.get('round_id'))[:8]}  "
                     f"{STATUS_LABELS.get(str(row.get('status')), '?')}  "
                     f"{' '.join(str(row.get('task') or '').split())[:50]}")
        return
    row = hits[0]
    if row.get("status") == "running":
        _say("[red]这一局还在跑，不能删除（等它结束或 /blindpick reset）。[/red]")
        return
    if row.get("status") == "pending":
        _say("[yellow]注意：这是还没裁决的对局，删掉就没有了。[/yellow]")
    _report_cleanup(f"对局 {str(row.get('round_id'))[:8]} 已删除",
                    *_delete_rounds([row]))


def _cmd_prune(args: list) -> None:
    """Manual retention sweep; --all keeps only running/pending rounds."""
    keep = days = None
    if "--all" in args:
        keep = days = 0
        victims = [row for row in _load_rounds()
                   if row.get("status") not in ("running", "pending")]
        deleted, leftovers = _delete_rounds(victims)
    else:
        for arg in args:
            if arg.isdigit():
                keep = int(arg)
        deleted, leftovers = _prune(keep=keep, days=days)
    branches = _sweep_orphan_branches(max_age=0)
    _report_cleanup(f"清理了 {deleted} 局", deleted, leftovers)
    for branch in branches:
        _say(f"  [dim]顺带删掉无主分支 {branch}[/dim]")
    if not deleted and not branches:
        _say("[dim]没有可清理的东西。[/dim]")


def _report_cleanup(headline: str, deleted: int, leftovers: list) -> None:
    if deleted:
        _say(f"[yellow]{headline}，worktree 与分支已回收。[/yellow]")
    if leftovers:
        _say("[yellow]以下资源未能自动清理：[/yellow]")
        for item in leftovers:
            _say_raw(f"  · {item}")
        _say("  手动清理：git worktree remove --force <路径> / git branch -D <名>")


def _cmd_reset() -> None:
    if _in_progress() is not None:
        _say("[red]有一局正在跑，不能 reset。[/red]")
        return
    leftovers: list[str] = []
    kept: list[str] = []
    for row in _load_rounds():
        if row.get("status") == "applied":
            # The loser branch of an applied round is deliberate evidence;
            # reset drops the record of it, so name it instead of deleting
            # something the user may still be reading.
            loser = ("challenger" if row.get("applied_side") == "incumbent"
                     else "incumbent")
            branch = str(row.get(f"{loser}_branch") or "")
            if branch:
                kept.append(branch)
            continue
        # Not just pending: a failed round's worktrees were reclaimed by its
        # own teardown only when the worker got that far — a killed CLI
        # leaves them, and after the record is gone the 24h GC is the only
        # thing that would ever find them.
        leftovers += _cleanup_worktrees(row)
    _save_rounds([])
    if leftovers:
        _say("[yellow]已清空对局记录（挑战者设置保留），"
             "但这些资源未能自动清理：[/yellow]")
        for item in leftovers:
            _say(f"  · {item}")
        _say("  手动清理：git worktree remove --force <路径> / git branch -D <名>")
    else:
        _say("[yellow]已清空对局记录（挑战者设置保留）。[/yellow]")
    for branch in kept:
        _say(f"  [dim]已应用对局的落选分支保留：{branch}[/dim]")
    _say("[dim]评分与裁决记录不受影响（.laintas/blindpick/）。[/dim]")


def _resolve_round(token: str = "") -> Optional[dict]:
    """The pending round a command should act on.

    Without an id: the oldest, but only when it is unambiguous — silently
    judging the oldest of several is how a verdict lands on the wrong round.
    """
    waiting = _pending()
    if not waiting:
        _say("没有待判的对局。")
        return None
    token = (token or "").strip().lower()
    if token:
        hits = [r for r in waiting
                if str(r.get("round_id", "")).lower().startswith(token)]
        if len(hits) == 1:
            return hits[0]
        _say(f"[red]{'没有' if not hits else '不止一个'}待判对局匹配 "
             f"“{token}”。[/red]")
    elif len(waiting) == 1:
        return waiting[0]
    else:
        _say(f"[yellow]有 {len(waiting)} 局待判，请指定对局 id：[/yellow]")
    for row in waiting:
        _say_raw(f"  {str(row.get('round_id'))[:8]}  "
                 f"{' '.join(str(row.get('task') or '').split())[:60]}")
    return None


# Printed with _say_raw: Rich would parse "[id]" as a style tag and eat it,
# which is how the usage line lost the very argument it documents.
_USAGE = ("子命令：challenger · run <task> · show [id] · "
          "pick a|b|tie|bad [id] · discard [id] · delete <id> · "
          "prune [--all] · status · ratings · export [路径] · reset；"
          "无参数进入全屏同台界面。")
_PICK_USAGE = "用法：/blindpick pick a|b|tie|bad [对局 id]"


def handle(parts, raw_line: str = "") -> None:
    """Dispatch /blindpick [<sub> ...]."""
    argv = [str(p).strip() for p in parts[1:] if str(p).strip()]
    if not argv:
        if sys.stdin.isatty():
            _open_arena()
        else:
            _cmd_status()
        return
    action = argv[0].lower()
    if action == "challenger":
        _pick_challenger()
    elif action == "run":
        task = " ".join(argv[1:]).strip()
        if not task:
            _say("用法：/blindpick run <任务描述>")
            return
        _run_round(task)
    elif action == "show":
        row = _resolve_round(argv[1] if len(argv) > 1 else "")
        if row is not None:
            _show(row, blind=False)
    elif action == "pick":
        if len(argv) < 2 or argv[1].lower() not in ("a", "b", "tie", "bad"):
            _say_raw(_PICK_USAGE)
            return
        row = _resolve_round(argv[2] if len(argv) > 2 else "")
        if row is not None:
            _do_pick(row, argv[1])
    elif action in ("delete", "rm"):
        if len(argv) < 2:
            _say("用法：/blindpick delete <对局 id>")
            return
        _cmd_delete(argv[1])
    elif action == "prune":
        _cmd_prune(argv[1:])
    elif action == "discard":
        row = _resolve_round(argv[1] if len(argv) > 1 else "")
        if row is not None:
            _settle(row, "discarded")
    elif action == "status":
        _cmd_status()
    elif action == "ratings":
        _cmd_ratings()
    elif action == "export":
        _cmd_export(argv[1] if len(argv) > 1 else "")
    elif action == "reset":
        _cmd_reset()
    else:
        _say_raw(_USAGE)


def setup(ctx) -> None:
    global _ctx
    _ctx = ctx
    _load_state()
    ctx.register_command(
        "blindpick", handle,
        description="当前模型 vs 挑战者模型：同任务双分支对垒，裁决后只应用胜者",
        subcommands=[
            ("challenger", "从模型选择器里挑一个挑战者"),
            ("run <task>", "开一局（后台运行，两倍成本）"),
            ("show [id]", "查看待判对局的两份 diff（直接模式，标注模型）"),
            ("pick a|b|tie|bad [id]", "应用某一侧 / 平局 / 都不行"),
            ("discard", "丢弃待判对局"),
            ("status", "状态"),
            ("delete <id>", "删除某一局（连 worktree 和分支一起）"),
            ("prune [--all]", "按留存策略清理旧对局"),
            ("ratings", "累计评分与位置偏差"),
            ("export [路径]", "导出裁决过的对局为 DPO 偏好对"),
            ("reset", "清空对局记录"),
        ])
    try:
        _reap_interrupted()
        _gc_orphans()
        # Retention is automatic: an extension that needs to be tidied by
        # hand is an extension that quietly fills a repo with branches.
        _prune()
        _sweep_orphan_branches()
    except Exception:
        pass


def teardown() -> None:
    global _ctx
    _ctx = None

