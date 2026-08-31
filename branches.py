"""The task decision tree — a few yes/no questions whose path selects a workflow.

A refactor and a bug fix are not the same job at different sizes, and neither
are "replace this with a library" and "restructure it in place". Given one set
of instructions for all of them a model does the average: it maps the whole
repository to change three lines, or renames a function without looking for
the callers, or invents an abstraction on the second occurrence of something.

So the intent pass (``intent.py``) walks a small binary tree while it is
already reading the request — one more field on the spec it was building, not
a second model call — and this module owns the tree, the walk's validation,
and the guidance the path accumulates.

Four things shape it:

  * **The path, not the leaf.** Guidance accumulates down the branch: the
    general refactor rules come from the node that decided "refactor", the
    migration specifics from the leaf below it. Common advice is written once.

  * **The path is itself context.** "This is a refactor; we are not building
    our own; the result is one-off" are decisions, not just a way of choosing
    text. They are rendered into the prompt even where the guidance is thin,
    so the model stops re-litigating them every turn.

  * **The model reports node ids, and every step is checked.** Each id must
    actually be a child of the previous one. An answer of "yes"/"no" could be
    mis-ordered without anyone noticing; an id cannot. An invalid step
    truncates the path there and the part that walked cleanly still applies —
    the same gate as intent.validate_spec's anchoring.

  * **One file, and the user's version wins.** ``~/.laintas/branches.json``
    holds the shape and the prose together, because a tree scattered across
    files is a tree whose shape you cannot see and whose links break on a
    rename. ``guidance`` accepts a string or a list of lines, since the awkward
    part of prose in JSON is the escaping, not the file.

Never raises: a malformed tree degrades to no branch, which is how the agent
worked before this module existed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import paths


TREE_FILENAME = "branches.json"
MAX_DEPTH = 8
MAX_GUIDANCE_CHARS = 6000
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class Node:
    id: str
    label: str = ""
    #: What answering the parent's question this way means. Shown to the model
    #: beside the id, so it knows which id it is choosing and why.
    choice: str = ""
    guidance: str = ""
    question: str = ""
    children: tuple = ()
    #: Agent ids or names this subtree is limited to. Narrows what
    #: ``branch_agents`` already allowed; it never widens it, so adding a node
    #: cannot switch the behaviour on for an agent the user did not enable.
    applies_to: tuple = ()

    @property
    def is_leaf(self) -> bool:
        return not (self.question and len(self.children) == 2)


@dataclass
class Tree:
    root: str = ""
    nodes: dict = field(default_factory=dict)
    problems: tuple = ()

    def get(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(str(node_id or ""))

    @property
    def usable(self) -> bool:
        return bool(self.root and self.root in self.nodes)


# ── The shipped tree ──────────────────────────────────────────────────────
# Two levels, four leaves. Small on purpose: a tree you can hold in your head
# is a tree you will actually edit, and every extra level multiplies the prose
# that has to be kept in agreement with itself.

DEFAULT_TREE = {
    "version": 1,
    "root": "kind",
    "nodes": {
        "kind": {
            "label": "",
            "question": (
                "Is this task restructuring existing code, or changing what "
                "the code does?"),
            "children": ["refactor", "modify"],
        },
        "refactor": {
            "label": "Refactor",
            "choice": (
                "restructuring existing code; the behaviour afterwards is the "
                "same"),
            "guidance": [
                "This task restructures existing code. The product is the same",
                "behaviour in a better shape, so the risks are not the ones a",
                "behaviour change carries.",
                "",
                "**Behaviour does not change.** If you find a bug while",
                "restructuring, report it and leave it. Fixing it in the same",
                "change makes the diff impossible to review and makes \"did the",
                "refactor break anything?\" unanswerable.",
                "",
                "**Move in steps that each hold.** Prefer several changes that",
                "each leave the code building and the tests passing over one",
                "that only works at the end. Run the check between steps, not",
                "once at the finish.",
                "",
                "**Confirm before crossing a boundary.** Renaming or moving",
                "something other modules import, something part of a public",
                "API, or something named in documentation or configuration is",
                "the user's decision. Say what you would change and what it",
                "would touch, then ask — once, with the list.",
            ],
            "question": (
                "Are we replacing this with something that already exists (a "
                "library, a framework, a platform feature), or restructuring "
                "what is here?"),
            "children": ["adopt-existing", "in-place"],
        },
        "adopt-existing": {
            "label": "Adopt what exists",
            "choice": "replacing our code with an existing library or feature",
            "guidance": [
                "**Prove the fit before you commit.** Check the thing actually",
                "covers the cases our code covers — the awkward ones, not the",
                "README example. List what it does not cover and say what",
                "happens to those.",
                "",
                "**Migrate behind a seam.** Put our own thin interface in",
                "front of it, and move callers to the seam before swapping the",
                "implementation. That is what makes the change reversible and",
                "reviewable in slices.",
                "",
                "**Both paths work until the last one moves.** Do not leave",
                "the tree in a state where half the callers use the old thing",
                "and nothing tells you which half.",
                "",
                "**Do not redesign while migrating.** Semantics we chose",
                "deliberately are requirements, not accidents; changing them",
                "in the same step hides a behaviour change inside a",
                "dependency change.",
                "",
                "**Say what the dependency costs.** A new dependency is a",
                "decision with a maintenance bill. Name the version, the",
                "licence if it is unusual, and what we would do if it were",
                "abandoned.",
            ],
        },
        "in-place": {
            "label": "Restructure in place",
            "choice": "reshaping the code we already have",
            "guidance": [
                "**Map before you move.** Find every caller, import, subclass,",
                "test, string reference and configuration entry that touches",
                "what you are about to change, before changing any of it.",
                "Searching is the cheap half of a refactor and the half that",
                "decides whether it works: a rename that misses one call site",
                "is not a smaller refactor, it is a bug that will look like a",
                "merge accident later. Search widely here — this is the one",
                "phase where that pays for itself.",
                "",
                "**Find the safety net first.** Locate the tests that already",
                "cover what you are moving and run them before you start, so a",
                "failure afterwards means something. If nothing covers it, say",
                "so and either write a characterisation test first or state in",
                "your report that the change is unverified. Do not discover",
                "this at the end.",
            ],
        },
        "modify": {
            "label": "Change behaviour",
            "choice": (
                "changing what the code does: a fix, a feature, an adjustment, "
                "or something new"),
            "guidance": [
                "This task changes what the code does. The product is the",
                "smallest correct change, so wide exploration costs more than",
                "it returns.",
                "",
                "**Locate, read, then edit.** Find the target, read the region",
                "you are about to change, and patch from text you have",
                "actually seen. Search narrowly and on demand: mapping the",
                "repository to change three lines is time the user pays for",
                "and attention spent away from the change.",
                "",
                "**Change what was asked and nothing else.** An adjacent",
                "problem you notice is worth reporting; it is not worth fixing",
                "here. Widening the diff is how a two-line fix becomes",
                "something nobody wants to review.",
                "",
                "**Verify the thing you changed.** Run the nearest real check",
                "— the specific test, a typecheck, or the command that",
                "exercises the path. \"It should work\" is not a verification,",
                "and neither is a suite that never touches your change.",
                "",
                "**Ask only when the change stops being reversible or",
                "in-scope.** An ordinary edit inside the requested scope needs",
                "no permission. Deleting something, changing an interface other",
                "code depends on, or editing a file the request never",
                "mentioned does.",
            ],
            "question": (
                "Is this a general capability other code will use, or a "
                "one-off for this specific case?"),
            "children": ["general", "one-off"],
        },
        "general": {
            "label": "General capability",
            "choice": "something other code is meant to use",
            "guidance": [
                "**\"General\" is not permission to abstract yet.** With two",
                "similar cases you cannot tell whether the similarity is real",
                "or a coincidence, and the wrong abstraction is more expensive",
                "than the duplication it replaced — it accumulates parameters",
                "and flags for callers that no longer match. Either point at a",
                "third real use, or write down the contract precisely enough",
                "that a third one would fit it. If you can do neither, build",
                "the specific thing and say why you did not generalise.",
                "",
                "**Duplicated knowledge is the exception.** One business rule,",
                "one security check, one validated calculation — unify those",
                "immediately, even with two copies, because divergence there",
                "is a bug rather than a style problem. Code that merely looks",
                "similar can wait.",
                "",
                "**Define the edges before the internals.** Name the inputs,",
                "the outputs, the errors, and what is explicitly out of scope.",
                "A shared thing without a stated boundary grows one caller at",
                "a time until nobody can change it.",
                "",
                "**It needs tests of its own.** Something other code depends",
                "on is tested at its own boundary, not only through whoever",
                "happens to call it today.",
            ],
        },
        "one-off": {
            "label": "One-off",
            "choice": "solving this case, here, and not building for reuse",
            "guidance": [
                "**Keep it where it is used.** Local, concrete and obvious",
                "beats general and clever. Do not add a module, a base class,",
                "or a configuration knob for a single caller.",
                "",
                "**Name it for what it does here.** A specific name is honest",
                "about the scope and stops the next reader assuming it is",
                "safe to reuse.",
                "",
                "**Some duplication is the right answer.** If this resembles",
                "code elsewhere, say so in the report rather than unifying",
                "them now — the third occurrence is when that decision can",
                "actually be made.",
            ],
        },
    },
}


# ── Loading ───────────────────────────────────────────────────────────────

def tree_path() -> Path:
    return paths.LAINTAS_HOME / TREE_FILENAME


def _text(value, limit: int = MAX_GUIDANCE_CHARS) -> str:
    """A string, or a list of lines joined — the prose-in-JSON accommodation.

    Escaping multi-paragraph markdown into one JSON string is the genuinely
    unpleasant part of this format, and a list of lines removes it for three
    lines of code.
    """
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(item) for item in value)
    return str(value or "").strip()[:limit]


def _parse_tree(data) -> Tree:
    if not isinstance(data, dict):
        return Tree(problems=("the tree file is not an object",))
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, dict):
        return Tree(problems=("the tree has no 'nodes' object",))

    problems = []
    nodes = {}
    for node_id, raw in raw_nodes.items():
        node_id = str(node_id or "")
        if not _ID_RE.match(node_id):
            problems.append(f"ignored node with an unusable id: {node_id!r}")
            continue
        if not isinstance(raw, dict):
            problems.append(f"ignored node {node_id}: not an object")
            continue
        children = raw.get("children") or ()
        if isinstance(children, str):
            children = [children]
        children = tuple(str(c) for c in children if str(c or "").strip())
        applies = raw.get("applies_to") or ()
        if isinstance(applies, str):
            applies = [item for item in re.split(r"[,\s]+", applies) if item]
        nodes[node_id] = Node(
            id=node_id,
            label=_text(raw.get("label"), 60) or node_id,
            choice=_text(raw.get("choice"), 200),
            guidance=_text(raw.get("guidance")),
            question=_text(raw.get("question"), 400),
            children=children,
            applies_to=tuple(str(a).strip() for a in applies if str(a).strip()),
        )

    # A question with anything other than two live children is not a decision.
    # Demoting it to a leaf keeps the guidance above it usable instead of
    # discarding the whole tree over one broken link.
    for node in nodes.values():
        if not node.question:
            continue
        missing = [c for c in node.children if c not in nodes]
        if missing:
            problems.append(
                f"{node.id}: children not found: {', '.join(missing)}")
        if len(node.children) != 2 or missing:
            node.question = ""
            node.children = ()

    root = str(data.get("root") or "")
    if root not in nodes:
        problems.append(f"root {root!r} is not a node")
        return Tree(root="", nodes=nodes, problems=tuple(problems))

    # Cycles: a tree that loops would let a walk run forever and would make
    # "the path so far" meaningless.
    seen = set()
    stack = [root]
    while stack:
        current = stack.pop()
        if current in seen:
            problems.append(f"{current}: reachable more than once (cycle?)")
            continue
        seen.add(current)
        node = nodes.get(current)
        if node is not None:
            stack.extend(node.children)
    for node_id in nodes:
        if node_id not in seen:
            problems.append(f"{node_id}: not reachable from the root")

    return Tree(root=root, nodes=nodes, problems=tuple(problems))


def load_tree() -> Tree:
    """The user's tree if there is one, otherwise the shipped default."""
    path = tree_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _parse_tree(DEFAULT_TREE)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        # Falling back to the default would hide the mistake and quietly give
        # the user someone else's workflow; an unusable tree means no branch.
        return Tree(problems=(f"{path}: invalid JSON ({exc})",))
    parsed = _parse_tree(data)
    if not parsed.usable:
        return parsed
    return parsed


def write_default_tree() -> Optional[Path]:
    """Put the shipped tree on disk so it can be read and edited.

    Only when absent. Refreshing it would overwrite an edited tree with the
    shipped one — the failure mode that made pushed defaults so destructive
    the last time this codebase did it.
    """
    path = tree_path()
    if path.exists():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(DEFAULT_TREE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        return None
    return path


# ── Walking ───────────────────────────────────────────────────────────────

def render_questions(tree: Tree, *, agent_id: str = "",
                     agent_name: str = "") -> str:
    """The tree as the intent pass sees it: ids, questions, and what each means.

    Ids rather than yes/no, because the walk is validated step by step against
    the tree — an id that is not a child of the previous node is caught, while
    a mis-ordered "yes" is not.
    """
    if not tree.usable:
        return ""
    lines = []

    def walk(node_id: str, depth: int):
        node = tree.get(node_id)
        if node is None or depth > MAX_DEPTH or not node.question:
            return
        indent = "  " * depth
        lines.append(f"{indent}Q: {node.question}")
        for child_id in node.children:
            child = tree.get(child_id)
            if child is None or not _subtree_allowed(child, agent_id, agent_name):
                continue
            lines.append(
                f"{indent}  -> \"{child.id}\": {child.choice or child.label}")
            walk(child_id, depth + 1)

    walk(tree.root, 0)
    if not lines:
        return ""
    return ("DECISION TREE — walk it and report the ids of the nodes you pass "
            "through, in order, as `branch_path`. Stop at the node that asks "
            "no further question. Report an empty list if the request does not "
            "fit the tree; a wrong path is worse than none, because it is "
            "followed with confidence.\n" + "\n".join(lines))


def _subtree_allowed(node: Node, agent_id: str, agent_name: str) -> bool:
    if not node.applies_to:
        return True
    wanted = {str(item).casefold() for item in node.applies_to}
    return (str(agent_id or "").casefold() in wanted
            or str(agent_name or "").casefold() in wanted)


def walk(tree: Tree, path, *, agent_id: str = "", agent_name: str = "") -> list:
    """Validate a reported path, returning the nodes it actually reaches.

    Each id must be a child of the node before it (and the first a child of
    the root). The first step that is not truncates the path: what walked
    cleanly still applies, and nothing invented gets to speak with authority.
    """
    if not tree.usable or not isinstance(path, (list, tuple)):
        return []
    current = tree.get(tree.root)
    reached = []
    for raw in list(path)[:MAX_DEPTH]:
        node_id = str(raw or "")
        if current is None or node_id not in current.children:
            break
        node = tree.get(node_id)
        if node is None or not _subtree_allowed(node, agent_id, agent_name):
            break
        reached.append(node)
        current = node
    return reached


def agent_enabled(agent_id: str, agent_name: str, allowed: str) -> bool:
    """Whether this agent gets a branch at all.

    ``allowed`` is the raw ``branch_agents`` setting: agent ids or names,
    comma-separated, or ``*``. The default does not include the primary — a
    prescriptive workflow suits a specialist and not the agent you talk to all
    day.
    """
    wanted = {item.strip().casefold()
              for item in str(allowed or "").split(",") if item.strip()}
    if not wanted:
        return False
    if "*" in wanted:
        return True
    return (str(agent_id or "").casefold() in wanted
            or str(agent_name or "").casefold() in wanted)


def path_label(reached) -> str:
    return " → ".join(node.label for node in reached if node.label)


def render(reached) -> str:
    """The system-prompt section: the decisions, then their accumulated advice.

    The path is rendered even when the guidance under it is thin. "This is a
    refactor, we are not adopting a library, the result is one-off" are
    decisions about the task; stating them stops the model re-opening them
    every turn.
    """
    reached = [node for node in (reached or [])]
    if not reached:
        return ""
    label = path_label(reached)
    body = "\n\n".join(node.guidance for node in reached if node.guidance)
    if not body:
        body = "No further guidance for this path; work the way it implies."
    return (f'<task_branch path="{label}">\n{body}\n</task_branch>')


def select(path, *, agent_id: str = "", agent_name: str = "",
           allowed: str = "", tree: Optional[Tree] = None) -> list:
    """The nodes to pin, or an empty list.

    Empty is a real answer and a common one: an agent nobody enabled, an
    unusable tree, or a model that could not place the request all mean "work
    the way you would have worked anyway".
    """
    if not agent_enabled(agent_id, agent_name, allowed):
        return []
    tree = tree if tree is not None else load_tree()
    return walk(tree, path, agent_id=agent_id, agent_name=agent_name)
