"""Task branches — one workflow for restructuring, another for changing.

A refactor and a bug fix are not the same job done at different sizes. A
refactor is worth mapping every caller before touching anything and must not
change behaviour; a fix should read one region, change it, and verify that.
Given one set of instructions for both, a model does the average of the two:
it maps the repository to change three lines, or it renames a function without
looking for the callers.

So the intent pass (``intent.py``) already decides which of the two this is —
it is one more field on the spec it was building anyway, not a second model
call — and this module supplies the workflow that decision selects. The chosen
branch is pinned into the system prompt beside the agreed reading.

Two rules shape it:

  * **The user's file wins.** A branch is a markdown file with frontmatter, the
    same shape as SKILL.md, so ``~/.laintas/branches/refactor.md`` replaces the
    built-in of that name outright. Defaults are written to disk only when
    nothing is there, never over an edited copy — pushed defaults that
    overwrite user edits is a mistake this codebase has already paid for once.

  * **Off unless asked for.** A branch is prescriptive: it tells the agent how
    to work. That is welcome on a specialist and unwelcome on the agent the
    user talks to all day, so ``branch_agents`` names who gets one and the
    primary is not in it by default.

Never raises: a malformed branch file is skipped, and no branch at all is the
behaviour that existed before this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import paths
# One frontmatter parser, not two. A second implementation would drift, and
# the user writing their first branch file would meet quirks that their skill
# files do not have.
from skills import _parse_frontmatter


BRANCHES_DIRNAME = "branches"

#: Task kinds a branch can claim. ``any`` matches whatever was decided;
#: ``unclear`` deliberately has no branch — a workflow chosen by a coin flip
#: is worse than no workflow, because it is stated with authority.
REFACTOR = "refactor"
MODIFY = "modify"
ANY = "any"
KINDS = (REFACTOR, MODIFY, ANY)

MAX_BODY_CHARS = 6000
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class Branch:
    name: str
    when: str = ANY
    description: str = ""
    body: str = ""
    #: Agent ids or names this branch is limited to. Empty means "whoever the
    #: branch_agents setting already allowed" — the file narrows, it never
    #: widens, so dropping a file into the directory cannot switch the
    #: behaviour on for an agent the user did not enable.
    applies_to: tuple = ()
    source: str = "builtin"

    def matches(self, task_kind: str, agent_id: str, agent_name: str) -> bool:
        if self.when not in (ANY, str(task_kind or "")):
            return False
        if not self.applies_to:
            return True
        wanted = {str(item).strip().casefold() for item in self.applies_to}
        return (str(agent_id or "").casefold() in wanted
                or str(agent_name or "").casefold() in wanted)


_REFACTOR_BODY = """\
This task restructures existing code. The product is the same behaviour in a
better shape, so the risks are different from a change that is meant to alter
what the program does.

**Map before you move.** Find every caller, import, subclass, test, string
reference and configuration entry that touches what you are about to change,
before changing any of it. Searching is the cheap half of a refactor and the
half that decides whether it works: a rename that misses one call site is not
a smaller refactor, it is a bug that will look like a merge accident later.
Use `fs.grep` and `fs.glob` broadly here — this is the one phase where a wide
search pays for itself.

**Find the safety net first.** Locate the tests that already cover the code
you are moving and run them before you start, so a failure afterwards means
something. If nothing covers it, say so plainly and either write a
characterisation test first or state in your report that the change is
unverified. Do not discover this at the end.

**Behaviour does not change.** If you find a bug while restructuring, report
it and leave it. Fixing it in the same change makes the diff impossible to
review, and makes "did the refactor break anything?" unanswerable.

**Move in steps that each hold.** Prefer several changes that each leave the
code building and the tests passing over one that only works at the end. Run
the check between steps, not once at the finish.

**Confirm scope before crossing a boundary.** Renaming or moving something
that other modules import, that is part of a public API, or that appears in
documentation or configuration is a decision the user should make. Say what
you would change and what it would touch, then ask — once, with the list, not
one file at a time.
"""

_MODIFY_BODY = """\
This task changes what the code does — a fix, a small feature, an adjustment.
The product is the smallest correct change, so wide exploration costs more
than it returns.

**Locate, read, then edit.** Find the target, read the region you are about to
change, and patch from text you have actually seen. Search narrowly and on
demand: mapping the repository to change three lines is time the user is
paying for and attention spent away from the change itself.

**Change what was asked and nothing else.** An adjacent problem you notice is
worth reporting; it is not worth fixing in this change. Widening the diff is
how a two-line fix becomes something nobody wants to review.

**Verify the thing you changed.** Run the nearest real check — the specific
test, a typecheck, or the command that exercises the path. "It should work" is
not a verification, and neither is running a test suite that never touches
your change.

**Ask only when the change stops being reversible or in-scope.** An ordinary
edit inside the requested scope does not need permission. Deleting something,
changing an interface other code depends on, or editing a file the request
never mentioned does — those are the moments to stop and describe what you are
about to do.
"""

_BUILTINS = (
    Branch(
        name=REFACTOR, when=REFACTOR,
        description="Restructuring existing code without changing behaviour",
        body=_REFACTOR_BODY),
    Branch(
        name=MODIFY, when=MODIFY,
        description="Changing behaviour: a fix, a small feature, an adjustment",
        body=_MODIFY_BODY),
)


def branches_dir() -> Path:
    return paths.LAINTAS_HOME / BRANCHES_DIRNAME


def _parse_file(path: Path) -> Optional[Branch]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        meta, body = _parse_frontmatter(text)
    except Exception:
        return None
    body = str(body or "").strip()[:MAX_BODY_CHARS]
    if not body:
        return None
    name = str(meta.get("name") or path.stem).strip()
    if not _NAME_RE.match(name):
        return None
    when = str(meta.get("when") or ANY).strip().casefold()
    if when not in KINDS:
        when = ANY
    applies = meta.get("applies_to") or meta.get("agents") or ()
    if isinstance(applies, str):
        applies = [item for item in re.split(r"[,\s]+", applies) if item]
    applies = tuple(str(item).strip() for item in applies if str(item).strip())
    return Branch(name=name, when=when,
                  description=str(meta.get("description") or "").strip()[:200],
                  body=body, applies_to=applies, source=str(path))


def load_all() -> list:
    """Built-in branches, with any same-named user file replacing one.

    Replacing rather than merging: a user who wrote ``refactor.md`` wrote the
    workflow they want, and quietly appending the built-in text underneath
    would leave them arguing with instructions they thought they had deleted.
    """
    found = {branch.name: branch for branch in _BUILTINS}
    directory = branches_dir()
    try:
        files = sorted(directory.glob("*.md"))
    except OSError:
        files = []
    for path in files:
        branch = _parse_file(path)
        if branch is not None:
            found[branch.name] = branch
    return sorted(found.values(), key=lambda b: b.name)


def agent_enabled(agent_id: str, agent_name: str, allowed: str) -> bool:
    """Whether this agent gets a branch at all.

    ``allowed`` is the raw ``branch_agents`` setting: a comma-separated list of
    agent ids or names, or ``*`` for everyone. The default does not include the
    primary — a prescriptive workflow is welcome on a specialist and unwelcome
    on the agent the user talks to all day.
    """
    wanted = {item.strip().casefold()
              for item in str(allowed or "").split(",") if item.strip()}
    if not wanted:
        return False
    if "*" in wanted:
        return True
    return (str(agent_id or "").casefold() in wanted
            or str(agent_name or "").casefold() in wanted)


def select(task_kind: str, *, agent_id: str = "", agent_name: str = "",
           allowed: str = "") -> Optional[Branch]:
    """The branch to pin, or None.

    None is a real answer and the common one: an unclear task kind, a disabled
    agent, or no branch claiming this kind all mean "work the way you would
    have worked anyway".
    """
    kind = str(task_kind or "").strip().casefold()
    if kind not in (REFACTOR, MODIFY):
        return None
    if not agent_enabled(agent_id, agent_name, allowed):
        return None
    for branch in load_all():
        if branch.matches(kind, agent_id, agent_name):
            return branch
    return None


def render(branch: Optional[Branch]) -> str:
    """The system-prompt section, or "" when there is no branch."""
    if branch is None or not str(getattr(branch, "body", "")).strip():
        return ""
    return (f'<task_branch kind="{branch.when}" name="{branch.name}">\n'
            f"{branch.body.strip()}\n"
            "</task_branch>")


def write_default_files() -> list:
    """Put the built-ins on disk so they can be read and edited.

    Only what is absent. Refreshing an existing file would overwrite a
    customised workflow with the shipped one — the failure mode that made
    pushed defaults so destructive the last time this codebase did it.
    Returns the paths actually created.
    """
    directory = branches_dir()
    created = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return created
    for branch in _BUILTINS:
        path = directory / f"{branch.name}.md"
        if path.exists():
            continue
        text = (f"---\nname: {branch.name}\nwhen: {branch.when}\n"
                f"description: {branch.description}\n---\n\n{branch.body}")
        try:
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
            created.append(path)
        except OSError:
            continue
    return created
