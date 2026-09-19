"""Layered, share-based context budget.

The request a model receives is a tree: the window splits into the output
reserve and the input; the input into the system prompt, the tool schemas, the
live-state tail and the conversation thread; each of those into its own blocks.
Every node takes ``share`` of its parent's budget, floored at ``min_tokens``.
There are no ceilings. A node's children are left exactly as they are while the
node fits its allotment — the second level only comes into force once the first
is exceeded — and once it is exceeded the children are compressed in
proportion to their shares.

Two tag rules keep the prompt readable without leaking the bookkeeping:

* Level-2 blocks (the children of ``system``/``live``) are real tags the model
  sees, e.g. ``<execution>…</execution>``, and keep them.
* Level-3-and-deeper markers only partition a block for budgeting. Whether the
  runtime wrapped a template slot in one or the user wrote ``<tools>…</tools>``
  into their own prompt, the marker is removed from what the model receives.

Only tags that name a configured node are treated as structure; any other tag
is text. The ``gateway`` node belongs to the gateway: its content never reaches
this process, only its size, and it is never shown to the user.

Defaults come from ``context_policy/budget.json`` (vendored from the gateway).
Every numeric knob is also a dotted ``/config`` key — ``budget.system.share``,
``budget.system.capabilities.share`` — so a user override is a config value
rather than a second file.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_DEFAULT_PATH = Path(__file__).resolve().parent / "context_policy" / "budget.json"
SHRINK_CHOICES = ("none", "truncate", "tail", "drop", "narrow")
#: Single values at the top of budget.json, each its own `/config budget <name>`.
SCALARS = {
    "hysteresis": "Once compressed, a block is released only below this fraction of its allotment",
    "assumed_window": "Window (tokens) assumed for a model that has not reported one yet",
    "assumed_summary_window": "Window (tokens) assumed for a summarizer that has not reported one yet",
    "chars_per_token": "Characters per token when a token budget becomes a character cut",
}
#: Config keys are ``budget.`` + the node path, with the ``input`` level left
#: out: it is the whole of what the prompt may use, not a choice anyone makes.
PREFIX = "budget."


@dataclass
class Node:
    name: str
    path: str
    share: float = 0.0
    min_tokens: int = 0
    shrink: str = "truncate"
    hidden: bool = False
    rest: bool = False
    params: dict = field(default_factory=dict)
    children: dict = field(default_factory=dict)

    def child(self, name: str) -> Optional["Node"]:
        return self.children.get(name)

    def walk(self):
        yield self
        for node in self.children.values():
            yield from node.walk()

    def find(self, path: str) -> Optional["Node"]:
        node = self
        for part in [p for p in str(path).split(".") if p]:
            node = node.children.get(part)
            if node is None:
                return None
        return node


# ── Loading ────────────────────────────────────────────────────────────────

_RAW: Optional[dict] = None


def raw_defaults() -> dict:
    global _RAW
    if _RAW is None:
        _RAW = json.loads(_DEFAULT_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(_RAW)


def _build(name: str, path: str, raw: dict, inherited_shrink: str) -> Node:
    shrink = str(raw.get("shrink") or inherited_shrink)
    node = Node(
        name=name, path=path,
        share=float(raw.get("share") or 0.0),
        min_tokens=int(raw.get("min_tokens") or 0),
        shrink=shrink,
        hidden=bool(raw.get("hidden")),
        rest=bool(raw.get("rest")),
        params=dict(raw.get("params") or {}),
    )
    for child_name, child_raw in (raw.get("children") or {}).items():
        child_path = f"{path}.{child_name}" if path else child_name
        node.children[child_name] = _build(child_name, child_path, child_raw or {}, shrink)
    return node


def _raw_trees(raw: dict) -> dict:
    """Top-level trees, keyed by the path prefix their config keys use."""
    inputs = dict((raw.get("input") or {}).get("children") or {})
    return {
        "output": raw.get("output") or {},
        **inputs,
        "aux": raw.get("aux") or {},
    }


def config_defaults() -> dict:
    """Every tunable as a dotted ``budget.*`` key with its default value."""
    raw = raw_defaults()
    out = {PREFIX + name: raw[name] for name in SCALARS if name in raw}
    for top, subtree in _raw_trees(raw).items():
        node = _build(top, top, subtree, "truncate")
        for item in node.walk():
            if item.hidden:
                continue
            spec = _raw_at(raw, item.path)
            if "share" in spec:
                out[f"{PREFIX}{item.path}.share"] = float(spec["share"])
            if "min_tokens" in spec:
                out[f"{PREFIX}{item.path}.min"] = int(spec["min_tokens"])
            if "shrink" in spec:
                out[f"{PREFIX}{item.path}.shrink"] = str(spec["shrink"])
            for key, value in (spec.get("params") or {}).items():
                out[f"{PREFIX}{item.path}.{key}"] = value
    return out


def _raw_at(raw: dict, path: str) -> dict:
    parts = path.split(".")
    trees = _raw_trees(raw)
    spec = trees.get(parts[0]) or {}
    for part in parts[1:]:
        spec = (spec.get("children") or {}).get(part) or {}
    return spec


def config_descriptions() -> dict:
    out = {}
    for key in config_defaults():
        leaf = key.rsplit(".", 1)[-1]
        where = key[len(PREFIX):].rsplit(".", 1)[0].replace(".", " ")
        out[key] = {
            "share": f"Share of the parent budget given to {where} (floor-only; no ceiling)",
            "min": f"Minimum tokens for {where}, whatever the window",
            "shrink": f"How {where} is compressed once over budget: " + "/".join(SHRINK_CHOICES),
            **SCALARS,
        }.get(leaf, f"{where}: {leaf.replace('_', ' ')} (fraction of the thread budget)")
    return out


def validate(key: str, value):
    """Coerce and check one ``budget.*`` value. Raises ValueError."""
    leaf = key.rsplit(".", 1)[-1]
    if key[len(PREFIX):] in ("assumed_window", "assumed_summary_window"):
        value = int(float(value))
        if value <= 0:
            raise ValueError(f"{key} must be greater than 0")
        return value
    if key[len(PREFIX):] == "chars_per_token":
        value = float(value)
        if not value > 0:
            raise ValueError(f"{key} must be greater than 0")
        return value
    if leaf == "shrink":
        value = str(value).strip().lower()
        if value not in SHRINK_CHOICES:
            raise ValueError(f"{key} expects " + ", ".join(SHRINK_CHOICES))
        return value
    if leaf == "min":
        value = int(float(value))
        if value < 0:
            raise ValueError(f"{key} must be 0 or greater")
        return value
    value = float(value)
    if not 0 <= value <= 1:
        raise ValueError(f"{key} must be between 0 and 1")
    if leaf == "hysteresis" and value <= 0:
        raise ValueError(f"{key} must be greater than 0")
    return value


def load(get: Optional[Callable[[str], object]] = None) -> dict:
    """The effective trees: defaults with every ``budget.*`` override applied.

    ``get`` reads a config key (agent_loop.get_runtime_config); without it the
    shipped defaults are returned.
    """
    raw = raw_defaults()
    trees = {}
    for top, subtree in _raw_trees(raw).items():
        trees[top] = _build(top, top, subtree, "truncate")
    if get is not None:
        for key in config_defaults():
            value = get(key)
            if value is None:
                continue
            path, leaf = key[len(PREFIX):].rsplit(".", 1) if "." in key[len(PREFIX):] else ("", key[len(PREFIX):])
            if not path:
                continue
            top, _, rest = path.partition(".")
            node = trees[top].find(rest) if rest else trees[top]
            if node is None:
                continue
            if leaf == "share":
                node.share = float(value)
            elif leaf == "min":
                node.min_tokens = int(value)
            elif leaf == "shrink":
                node.shrink = str(value)
            else:
                node.params[leaf] = value
    scalars = {}
    for name in SCALARS:
        configured = get(PREFIX + name) if get is not None else None
        scalars[name] = configured if configured is not None else raw.get(name)
    trees["_scalars"] = scalars
    trees["_hysteresis"] = float(scalars.get("hysteresis") or 0.9)
    return trees


# ── Arithmetic ─────────────────────────────────────────────────────────────

def allot(node: Node, parent_budget: int) -> int:
    return max(int(node.min_tokens), int(node.share * max(0, parent_budget)))


def split_window(trees: dict, window: int, max_output: int = 0) -> tuple[int, int]:
    """(output reserve, input budget) for a model window.

    The reserve never exceeds what the model can actually write: holding back
    more than its output ceiling only takes room from the prompt.
    """
    window = max(0, int(window))
    output = allot(trees["output"], window)
    if max_output and max_output > 0:
        output = min(output, int(max_output))
    output = min(output, window)
    return output, window - output


def triggered(state: Optional[dict], path: str, natural: int, budget: int,
              hysteresis: float) -> bool:
    """Over budget, with a release band so a block at the line does not flap.

    A compressed block changes the system prompt; flipping every turn would
    cost the provider's prefix cache every turn.
    """
    marks = state.setdefault("_budget_triggered", {}) if state is not None else {}
    if natural > budget:
        marks[path] = True
    elif marks.get(path) and natural > int(budget * hysteresis):
        pass
    else:
        marks.pop(path, None)
    return bool(marks.get(path))


def distribute(budget: int, naturals: dict, nodes: dict) -> dict:
    """Allot ``budget`` among children that together exceed it.

    Fixed (``shrink: none``) children keep their natural size. The rest start
    from ``max(min, share * budget)``; a child that needs less than that keeps
    its natural size and the surplus is handed to those still over, in
    proportion to their shares. Floors always hold, even when they overrun.
    """
    result = {}
    remaining = budget
    flexible = []
    for name, size in naturals.items():
        node = nodes.get(name)
        if node is None or node.shrink == "none":
            result[name] = size
            remaining -= size
        else:
            flexible.append(name)
    remaining = max(0, remaining)
    open_set = list(flexible)
    while open_set:
        weights = {n: max(nodes[n].share, 1e-9) for n in open_set}
        total_weight = sum(weights.values())
        target = {n: max(nodes[n].min_tokens, int(remaining * weights[n] / total_weight))
                  for n in open_set}
        settled = [n for n in open_set if naturals[n] <= target[n]]
        if not settled:
            for n in open_set:
                result[n] = min(naturals[n], target[n])
            break
        for n in settled:
            result[n] = naturals[n]
            remaining = max(0, remaining - naturals[n])
            open_set.remove(n)
    return result


# ── Structure ──────────────────────────────────────────────────────────────

@dataclass
class Segment:
    text: str
    node: Optional[Node] = None
    open_tag: str = ""
    inner: str = ""
    close_tag: str = ""


def _open_re(name: str):
    return re.compile(r"<" + re.escape(name) + r"(?=[\s>])[^<>]*>")


def split(text: str, node: Node) -> list[Segment]:
    """Cut ``text`` into the configured children of ``node`` and plain text.

    Outermost configured element wins; unconfigured tags stay text. An opening
    tag without its closing tag is text too — half a block is not a block.
    """
    names = [n for n in node.children if not node.children[n].hidden]
    if not text or not names:
        return [Segment(text)] if text else []
    pattern = re.compile(r"<(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                         + r")(?=[\s>])[^<>]*>")
    out, pos = [], 0
    while True:
        m = pattern.search(text, pos)
        if m is None:
            break
        name = m.group(1)
        close = _matching_close(text, name, m.end())
        if close is None:
            nxt = m.end()
            out.append(Segment(text[pos:nxt]))
            pos = nxt
            continue
        close_start, close_end = close
        if m.start() > pos:
            out.append(Segment(text[pos:m.start()]))
        out.append(Segment(text[m.start():close_end], node.children[name],
                           m.group(0), text[m.end():close_start], text[close_start:close_end]))
        pos = close_end
    if pos < len(text):
        out.append(Segment(text[pos:]))
    merged = []
    for seg in out:
        if seg.node is None and merged and merged[-1].node is None:
            merged[-1].text += seg.text
        else:
            merged.append(seg)
    return merged


def _matching_close(text: str, name: str, start: int):
    opener, closer = _open_re(name), f"</{name}>"
    depth, pos = 1, start
    while True:
        c = text.find(closer, pos)
        if c < 0:
            return None
        o = opener.search(text, pos, c)
        while o is not None:
            depth += 1
            o = opener.search(text, o.end(), c)
        depth -= 1
        if depth == 0:
            return c, c + len(closer)
        pos = c + len(closer)


def strip_markers(text: str, node: Node) -> str:
    """Remove every configured level-3+ marker below ``node``, keep content."""
    parts = []
    for seg in split(text, node):
        if seg.node is None:
            parts.append(seg.text)
        else:
            parts.append(strip_markers(seg.inner, seg.node))
    return "".join(parts)


# ── Compression ────────────────────────────────────────────────────────────

def fit_text(text: str, budget: int, shrink: str, count: Callable[[str], int],
             label: str = "") -> str:
    """Deterministically cut ``text`` to ``budget`` tokens.

    Same input, same bytes out: the compressed prompt must stay cacheable.
    The marker names what was removed and where the whole block can be read.
    """
    if shrink == "none" or count(text) <= budget:
        return text
    if shrink == "drop" or budget <= 0:
        return ""
    note = (f"\n[… {label or 'block'} compressed to fit its budget; "
            f"full text: .laintas/budget/ (or /prop budget)]\n")
    room = max(0, budget - count(note))
    lines = text.splitlines(keepends=True)
    if shrink == "tail":
        lines = list(reversed(lines))
    low, high = 0, len(lines)
    while low < high:
        mid = (low + high + 1) // 2
        if count("".join(lines[:mid])) <= room:
            low = mid
        else:
            high = mid - 1
    kept = lines[:low]
    if low < len(lines):
        # A single long line (minified code, a JSON blob) would otherwise cost
        # the whole block: fill what is left of the budget from the next line.
        partial = lines[low] if shrink != "tail" else lines[low][::-1]
        spare = room - count("".join(kept))
        lo, hi = 0, len(partial)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count(partial[:mid]) <= spare:
                lo = mid
            else:
                hi = mid - 1
        if lo:
            piece = partial[:lo] if shrink != "tail" else partial[:lo][::-1]
            kept = kept + [piece]
    if shrink == "tail":
        return note.lstrip("\n") + "".join(reversed(kept))
    return "".join(kept).rstrip("\n") + note


@dataclass
class Row:
    path: str
    depth: int
    natural: int
    allotted: int
    delivered: int
    triggered: bool
    shrink: str
    original: str = ""
    compressed: str = ""


def fit(text: str, node: Node, budget: int, count: Callable[[str], int], *,
        depth: int, state: Optional[dict] = None, hysteresis: float = 0.9,
        extra: Optional[dict] = None, rows: Optional[list] = None,
        enforce: bool = True) -> str:
    """Render ``text`` for ``node`` within ``budget`` tokens.

    ``depth`` is the node's level (system/live are 1). Children at level 2 keep
    their tags; deeper markers are always removed. ``extra`` adds children that
    have a size but no text here (the gateway's injection), so they compete for
    the budget and can be ceded without this process ever holding them.
    ``enforce=False`` renders without compressing (the parent fit).
    """
    segments = split(text, node)
    extra = dict(extra or {})
    naturals, rendered_children = {}, {}
    rest_text = "".join(s.text for s in segments if s.node is None)
    for seg in segments:
        if seg.node is not None:
            naturals[seg.node.name] = naturals.get(seg.node.name, 0) + count(seg.inner)
    for name, size in extra.items():
        naturals[name] = naturals.get(name, 0) + int(size)
    rest_natural = count(rest_text)
    total = sum(naturals.values()) + rest_natural
    is_over = enforce and triggered(state, node.path, total, budget, hysteresis)

    child_budget = {}
    if is_over:
        pool = dict(naturals)
        pool["\x00rest"] = rest_natural
        nodes = dict(node.children)
        split_to = distribute(budget, pool, nodes)
        child_budget = {k: v for k, v in split_to.items() if k != "\x00rest"}
    if rows is not None:
        rows.append(Row(node.path, depth, total, budget if enforce else total,
                        0, is_over, node.shrink, original=text))
        mine = rows[-1]

    parts = []
    for seg in segments:
        if seg.node is None:
            parts.append(seg.text)
            continue
        child = seg.node
        inner_budget = child_budget.get(child.name, count(seg.inner))
        body = fit(seg.inner, child, inner_budget, count, depth=depth + 1,
                   state=state, hysteresis=hysteresis, rows=rows,
                   enforce=is_over)
        if is_over and count(body) > inner_budget:
            body = fit_text(body, inner_budget, child.shrink, count, child.path)
        if depth + 1 <= 2:
            if body or child.shrink != "drop":
                parts.append(seg.open_tag + body + seg.close_tag)
        else:
            parts.append(body)
    out = "".join(parts)
    if is_over and count(out) > budget and node.shrink != "none" and not node.children:
        out = fit_text(out, budget, node.shrink, count, node.path)
    if rows is not None:
        mine.delivered = count(out)
        mine.compressed = out
        for name, size in extra.items():
            given = child_budget.get(name, size) if is_over else size
            rows.append(Row(f"{node.path}.{name}", depth + 1, int(size), given,
                            min(int(size), given), is_over and given < size,
                            "drop"))
    return out


def ceded(rows: list, path: str) -> bool:
    """Whether an ``extra`` child (the gateway) was given less than it holds."""
    for row in rows:
        if row.path == path:
            return row.delivered < row.natural
    return False


def fit_items(items: list, budget: int, keep: set) -> list:
    """Keep ``(key, cost)`` items in order until ``budget``; ``keep`` always stays."""
    used = sum(cost for key, cost in items if key in keep)
    out = []
    for key, cost in items:
        if key in keep:
            out.append(key)
        elif used + cost <= budget:
            out.append(key)
            used += cost
    return out
