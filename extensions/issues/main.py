"""Extension: issues

A question asked through a Laintas Issues preset (pick one with /model) is
posted to the Issues board, and its answers keep coming after the reply that
posted it. This extension brings them back into the CLI:

    /issues            the latest question's answers
    /issues list       your latest questions asked from a chat
    /issues <id>       one question's answers

Picking the final answer is the model's tool, not a command: ask for it in
the conversation ("pick answer 2 as final"). `issues.pick` selects an already-paid
answer — the host asks you before it does — and returns it, so the picked
answer lands in the conversation the way any tool result does.

It calls Laintas as you through `ctx.laintas` (the `account` capability):
the host attaches your session; this code never sees it.
"""
from __future__ import annotations

import re

from rich.markdown import Markdown
from rich.text import Text

_ctx = None
_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# What the model is shown of one answer; the full text comes back when it is picked.
_ANSWER_CHARS = 4000
_PREVIEW_CHARS = 400


def _get(path: str) -> dict:
    out = _ctx.laintas.request("GET", path)
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "the request failed")
    return out["data"]


def _latest_id() -> str:
    issues = _get("/api/chat/issues").get("issues") or []
    if not issues:
        raise RuntimeError("you have no Issues question asked from a chat yet")
    return issues[0]["id"]


def _issue(issue_id: str = "") -> dict:
    issue_id = (issue_id or "").strip()
    if issue_id and not _ID.match(issue_id):
        raise RuntimeError("issue_id is the question's id, as in its page link")
    return _get(f"/api/chat/issues/{issue_id or _latest_id()}")


def _shown(view: dict) -> list:
    return [a for a in view.get("answers") or [] if not a.get("folded") or a.get("selected")]


def _state(view: dict) -> str:
    if view.get("peopleOnly"):
        run = (view.get('task') or {}).get('execution') or {}
        return "people only: " + (run.get('state') or view.get('status') or 'recruiting')
    if view.get("status") == "solved":
        return "final answer picked, question complete"
    if view.get("closedReason") == "stopped":
        return "you stopped it, so it is closed"
    return "open, more answers may come" if view.get("open") else "closed"


def _payment_rule(view: dict) -> str:
    return ("Every answer was paid its Solver's quote from the prepaid budget as it landed; "
            "picking one pays nothing more, and unspent budget is refunded when the question closes.")


# -- the model's tools ------------------------------------------------------------

def tool_answers(issue_id: str = "") -> dict:
    view = _issue(issue_id)
    if view.get('peopleOnly'):
        return {'issue_id': view['id'], 'title': view.get('title'), 'state': _state(view),
                'task': view.get('task'), 'can_pick': False,
                'note': 'This is a personal task. The person uses /issues choose, /issues accept or /issues reject. '
                        'Choosing an ordinary answer with issues.pick does not apply.'}
    return {"issue_id": view["id"], "title": view.get("title"), "url": view.get("url"), "state": _state(view),
            "can_pick": bool(view.get("canSelect")), "bounty_usd": int(view.get("bountyCents") or 0) / 100,
            "answer_cost_usd": int(view.get("costMicro") or 0) / 1e6,
            "remaining_usd": int(view.get("remainingMicro", int(view.get("bountyCents") or 0) * 10000)) / 1e6,
            "payment_rule": _payment_rule(view),
            "answers": [{"n": n, "answer_id": a["id"], "by": a.get("by") or "anonymous",
                         "unverified_upstream": bool(a.get("trust")) and a["trust"] not in ("official", "provider"),
                         "final": bool(a.get("selected")), "paid_usd": int(a.get("paidMicro") or 0) / 1e6,
                         "text": (a.get("body") or "")[:_ANSWER_CHARS]
                         + ("…(cut here; the whole answer comes back when it is picked)"
                            if len(a.get("body") or "") > _ANSWER_CHARS else "")}
                        for n, a in enumerate(_shown(view), 1)]}


def tool_pick(issue_id: str, answer_id: int) -> dict:
    view = _issue(issue_id)
    answer = next((a for a in _shown(view) if a["id"] == int(answer_id)), None)
    if answer is None:
        raise RuntimeError("no such answer to this question; read them with issues.answers")
    if not view.get("canSelect"):
        raise RuntimeError(f"this question takes no pick now ({_state(view)})")
    out = _ctx.laintas.request(
        "POST", f"/api/chat/issues/{view['id']}/accept", {"answer_id": answer["id"]},
        purpose=f"Pick the answer by {answer.get('by') or 'anonymous'} for “{view.get('title')}”: "
                f"it becomes the conversation's context. {_payment_rule(view)} The pick can be changed until the question closes.")
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "the pick failed")
    return {"picked": True, "issue_id": view["id"], "by": answer.get("by"),
            "final_answer": answer.get("body") or "",
            "note": "This is now the question's picked answer. Give it to the person as your reply."}


# -- the person's command ---------------------------------------------------------

def _plain(value) -> str:
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', str(value or ''))


def _post(path, body, purpose):
    out = _ctx.laintas.request('POST', path, body, purpose=purpose)
    if not out.get('ok'):
        raise RuntimeError(out.get('error') or 'the request failed')
    return out['data']


def _quote(a):
    if a['billing'] == 'prepaid':
        return f"${a['price_usd']:.2f} human fee + verified token usage"
    if a.get('human_fee_usd'):
        return f"token usage + ${a['human_fee_usd']:.2f} human fee"
    return f"token usage + {a.get('human_fee_pct', 0) * 100:g}% human fee"


def _show_task(view):
    console, task = _ctx.console, view.get('task') or {}
    console.print(Text(f"{view['title']} | {_state(view)} | escrow ${task.get('escrow_usd', 0):.2f}"))
    for a in task.get('applications_list') or []:
        person = a.get('applicant') or {}
        console.print(Text(f"\nApplication {a['id']} | {person.get('name') or person.get('id')} | {a['status']} | {_quote(a)}"))
        console.print(Text(_plain(a.get('note'))))
        console.print(Text('Squad: ' + ', '.join(s['name'] + ' (' + s['model'] + ')' for s in a['squad'])))
        credit = a.get('credit') or {}
        console.print(Text(f"Accepted jobs: {credit.get('done', 0)} | quoted time: {a['deadline_hours']}h"))
    run = task.get('execution') or {}
    if run:
        console.print(Text(f"\nRun: {run['id']} | {run['state']} | verified cost ${run['cost']['verified_usd']:.6f}"))
        declaration = run.get('declaration') or {}
        evidence = {i['index']: i['evidence'] for i in declaration.get('items', [])}
        for item in run.get('checklist', []):
            console.print(Text(_plain(f"{item['index']}. {item['text']}\n   {evidence.get(item['index'], 'No declaration yet')}")))
        if declaration.get('status') == 'awaiting':
            console.print(Text(f"/issues accept {view['id']}\n/issues reject {view['id']} <item> <reason>"))
    if view['status'] == 'open':
        console.print(Text(f"\n/issues choose {view['id']} [application-id]"))


def _task_action(words):
    action = words[0]
    if len(words) < 2:
        raise RuntimeError(f'Usage: /issues {action} <issue-id>')
    view = _issue(words[1])
    if not view.get('task'):
        raise RuntimeError('This is not a personal task.')
    _show_task(view)
    task = view['task']
    path = f"/api/chat/issues/{view['id']}/task/"
    if action == 'choose':
        offers = [a for a in task['applications_list'] if a['status'] == 'applied']
        if view['status'] != 'open' or not offers:
            raise RuntimeError('No open applications to choose from.')
        if len(words) > 2:
            selected = int(words[2])
        else:
            from prompt_toolkit.shortcuts import radiolist_dialog
            selected = radiolist_dialog(title='Choose an applicant', text='Select a quote. You will confirm before it starts.',
                values=[(a['id'], _plain(f"{a['applicant'].get('name') or a['applicant']['id']} | {_quote(a)}")) for a in offers]).run()
        offer = next((a for a in offers if a['id'] == selected), None)
        if not offer:
            return
        result = _post(path + 'pick', {'answer_id': offer['id']},
                       f"Choose application {offer['id']} for {view['title']}: {_quote(offer)}. "
                       "Share the sealed task and conversation with this person and start the run. Keep the original checklist.")
    else:
        run = task.get('execution') or {}
        if (run.get('declaration') or {}).get('status') != 'awaiting':
            raise RuntimeError('No delivery waiting for acceptance.')
        body = {'run_id': run['id']}
        if action == 'reject':
            if len(words) < 4:
                raise RuntimeError('Usage: /issues reject <issue-id> <item> <reason>')
            body.update(item=int(words[2]), reason=' '.join(words[3:]))
            purpose = f"Reject delivery of {view['title']}, item {body['item']}: {body['reason']}. Opens the appeal window."
        else:
            purpose = f"Accept delivery of {view['title']} and settle the agreed token costs and human fee from escrow."
        result = _post(path + action, body, purpose)
    _show_task(result)


def handle(argv: list) -> None:
    console = _ctx.console
    words = [w for w in argv if w.strip()]
    try:
        if words and words[0] in ('choose', 'accept', 'reject'):
            _task_action(words)
            return
        if words and words[0] == "list":
            issues = _get("/api/chat/issues").get("issues") or []
            if not issues:
                console.print("[dim]No Issues question asked from a chat yet. Pick an Issues preset with /model and ask.[/dim]")
            for q in issues:
                tag = "people only" if q.get("peopleOnly") else f"{q.get('answers', 0)} answer(s)"
                console.print(Text.assemble((q["id"][:8], "dim"), "  ", (q.get("title") or "", "bold"),
                                            f"  {q.get('status')} · {tag}"))
            return
        rest = list(words)
        expand = None
        if rest and rest[0].isdigit():
            expand, rest = int(rest[0]), rest[1:]
        if len(rest) > 1 and rest[1].isdigit():
            expand = int(rest[1])
        view = _issue(rest[0] if rest else "")
    except (RuntimeError, ValueError) as exc:
        console.print(Text(_plain(exc), style='yellow'))
        return
    if view.get('peopleOnly'):
        _show_task(view)
        return
    answers = _shown(view)
    console.print(Text.assemble(("Issues", "bold"), f"  {view.get('title', '')}  ", (view.get("url", ""), "dim")))
    console.print(f"[dim]{len(answers)} answer(s) · {_state(view)}[/dim]")
    console.print(f"[dim]Answer costs: ${int(view.get('costMicro') or 0)/1e6:.6f} · "
                  f"Remaining budget: ${int(view.get('remainingMicro') or 0)/1e6:.6f}[/dim]")
    console.print(Text(_payment_rule(view), style="dim"))
    cut = False
    for n, a in enumerate(answers, 1):
        tags = [t for t in ("Solver" if a.get("solver") else "",
                            "unverified upstream" if a.get("trust") and a["trust"] not in ("official", "provider") else "",
                            "PICKED" if a.get("selected") else "", f"paid ${int(a.get('paidMicro') or 0)/1e6:.6f}" if a.get("paidMicro") else "") if t]
        console.print(Text.assemble("\n", (f"[{n}] ", "bold"), (a.get("by") or "anonymous", "bold"),
                                    (f"  {' · '.join(tags)}" if tags else "", "dim")))
        body = a.get("body") or ""
        if expand == n or len(body) <= _PREVIEW_CHARS:
            console.print(Markdown(body))
        else:
            cut = True
            console.print(Markdown(body[:_PREVIEW_CHARS].rstrip() + " ..."))
    if cut:
        console.print(f"\n[dim]Answers are previews; /issues {view['id']} <number> shows one in full.[/dim]")
    if view.get("canSelect") and answers:
        console.print("\n[dim]To pick the final answer, say so in the conversation, e.g. “pick answer 2 as final”.[/dim]")


def setup(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_tool("answers", tool_answers, {
        "description": "Read the answers to a Laintas Issues question the person asked through an Issues preset "
                       "(its page link is in your earlier reply). Omit issue_id for their latest one.",
        "parameters": {"type": "object", "properties": {
            "issue_id": {"type": "string", "description": "The question's id, from its page link"}}}})
    ctx.register_tool("pick", tool_pick, {
        "description": "Pick one answer as the question's picked answer, ONLY when the person has said which one. "
                       "It makes that answer the conversation's context (with confirmation); answers were already paid "
                       "as they landed, so it moves no money. The result carries the full picked answer to give them.",
        "parameters": {"type": "object", "required": ["issue_id", "answer_id"], "properties": {
            "issue_id": {"type": "string"}, "answer_id": {"type": "integer"}}}})
    ctx.register_command(
        "issues", handle,
        description="Issues answers, task applicants and acceptance",
        subcommands=[("list", "Your latest questions asked from a chat"),
                     ("<id>", "Answers or task applicants and delivery"),
                     ("<id> <n>", "One answer in full"),
                     ("choose", "Choose an applicant: <issue-id> [application-id]"),
                     ("accept", "Accept delivered work: <issue-id>"),
                     ("reject", "Reject delivery: <issue-id> <item> <reason>")])


def teardown() -> None:
    global _ctx
    _ctx = None
