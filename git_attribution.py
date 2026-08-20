"""Co-author attribution for commits laintas-cli makes on the user's behalf.

The user stays the commit author — the agent is recorded as a co-author, the
way a pair-programming partner is. GitHub renders a ``Co-Authored-By`` trailer
as a second avatar on the commit and counts it toward that account's
contributions, but *only* when the trailer's email is one GitHub can resolve to
an account. That is why the default address is the ``laintas`` account's own
``users.noreply.github.com`` form rather than something at laintas.com: a
noreply address is linked to the account by construction and needs no mail
delivery to verify.

Why this lives in code rather than in the prompt
------------------------------------------------
The obvious place is a line in the git skill telling the model to append the
trailer. That reaches nobody who already has laintas-cli installed: bundled
skills are copied only into a slot that does not exist yet (``skills.py``), and
``.laintas/cli.prop`` is likewise never overwritten. It also depends on the
model remembering, every time. Rewriting the command is deterministic and
version-independent, so the attribution is either on for everyone or off for
everyone.

The rewrite is deliberately timid. It inserts ``--trailer`` after an
unambiguous ``git … commit`` word and, at the first sign that it cannot read
the command line with certainty — a heredoc, unbalanced quotes, a trailer the
caller wrote themselves — returns the command untouched. A missing trailer is a
cosmetic loss; a corrupted command is the user's commit.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import Optional

import paths

#: Identity recorded as co-author when the user has not configured another.
#: ``<id>+<login>@users.noreply.github.com`` is the only address form GitHub
#: guarantees resolves back to the account.
DEFAULT_CO_AUTHOR = "laintas <318547197+laintas@users.noreply.github.com>"

#: config.json key. Set it to another ``Name <email>`` to change the identity,
#: or to an empty string / false to turn attribution off entirely.
CONFIG_KEY = "gitCoAuthor"

TRAILER_KEY = "Co-Authored-By"

# `git commit --trailer` landed in 2.32. Older git would reject the flag and
# fail a commit that would otherwise have succeeded, so we leave it alone.
_MIN_GIT_VERSION = (2, 32)

# Global options that may sit between `git` and the subcommand. The two-word
# forms take a value we must skip over as well, or `git -C /repo commit` reads
# as if `/repo` were the subcommand.
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--exec-path", "--git-dir", "--work-tree",
                        "--namespace", "--super-prefix", "--config-env"}
_GIT_OPTS_STANDALONE = {"--no-pager", "-p", "--paginate", "--no-replace-objects",
                        "--bare", "--literal-pathspecs", "--no-optional-locks",
                        "--glob-pathspecs", "--noglob-pathspecs",
                        "--icase-pathspecs", "--no-lazy-fetch"}

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

_git_version_cache: Optional[tuple] = None


def _git_version() -> tuple:
    """(major, minor) of the git on PATH, or (0, 0) when it cannot be read."""
    global _git_version_cache
    if _git_version_cache is None:
        try:
            out = subprocess.run(["git", "--version"], capture_output=True,
                                 text=True, timeout=5).stdout
            match = _VERSION_RE.search(out or "")
            _git_version_cache = ((int(match.group(1)), int(match.group(2)))
                                  if match else (0, 0))
        except (OSError, subprocess.SubprocessError, ValueError):
            _git_version_cache = (0, 0)
    return _git_version_cache


def configured_co_author() -> str:
    """The configured identity, or "" when attribution is switched off.

    A missing key means the default applies; an explicitly empty value (or
    ``false``) means the user turned it off and we honour that.
    """
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_CO_AUTHOR
    if not isinstance(data, dict) or CONFIG_KEY not in data:
        return DEFAULT_CO_AUTHOR
    value = data.get(CONFIG_KEY)
    if value is False or value is None:
        return ""
    return str(value).strip() if isinstance(value, str) else DEFAULT_CO_AUTHOR


def _words(command: str) -> Optional[list]:
    """Split into ``(text, start, end, boundary)`` words, or None if unreadable.

    ``boundary`` marks a word that starts a new command — the beginning of the
    line, or the position after an unquoted ``;``, ``&&``, ``||``, ``|``,
    newline or ``(``. Only those positions can hold a program name, so a
    ``commit`` appearing as an argument is never mistaken for the subcommand.

    Quoting is tracked so that text inside quotes is never treated as syntax.
    Unbalanced quotes return None: we could not read the line, so we must not
    edit it.
    """
    words: list = []
    in_single = in_double = False
    start = -1
    boundary_next = True
    i = 0
    while i < len(command):
        char = command[i]
        if char == "\\" and not in_single and i + 1 < len(command):
            if start < 0:
                start = i
            i += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            if start < 0:
                start = i
            i += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            if start < 0:
                start = i
            i += 1
            continue
        if in_single or in_double:
            if start < 0:
                start = i
            i += 1
            continue
        if char.isspace():
            if start >= 0:
                words.append((command[start:i], start, i, boundary_next))
                boundary_next = False
                start = -1
            i += 1
            continue
        if char in ";&|\n(":
            if start >= 0:
                words.append((command[start:i], start, i, boundary_next))
                start = -1
            boundary_next = True
            i += 1
            continue
        if start < 0:
            start = i
        i += 1
    if in_single or in_double:
        return None
    if start >= 0:
        words.append((command[start:len(command)], start, len(command),
                      boundary_next))
    return words


def _is_git(word: str) -> bool:
    return word == "git" or word.endswith("/git")


def _commit_word_ends(command: str) -> list:
    """Offsets just past every ``commit`` subcommand word in *command*."""
    words = _words(command)
    if words is None:
        return []
    ends: list = []
    index = 0
    while index < len(words):
        text, _, _, boundary = words[index]
        if not (boundary and _is_git(text)):
            index += 1
            continue
        cursor = index + 1
        while cursor < len(words):
            option = words[cursor][0]
            if option in _GIT_OPTS_WITH_VALUE:
                cursor += 2
            elif (option in _GIT_OPTS_STANDALONE
                  or (option.startswith("--") and "=" in option)):
                cursor += 1
            else:
                break
        if cursor < len(words) and words[cursor][0] == "commit":
            ends.append(words[cursor][2])
            index = cursor + 1
        else:
            index += 1
    return ends


def apply(command: str, co_author: Optional[str] = None) -> str:
    """Return *command* with a co-author trailer added to its git commits.

    Returns the command unchanged whenever attribution is off, unsupported, or
    the command cannot be rewritten with certainty.
    """
    identity = configured_co_author() if co_author is None else co_author
    if not identity or "git" not in command:
        return command
    lowered = command.lower()
    # The caller already said who the co-authors are; do not second-guess them.
    if "--trailer" in lowered or "co-authored-by" in lowered:
        return command
    # A heredoc body is data, not command line. `commit` inside one would be
    # matched as syntax and the trailer written into the user's file.
    if "<<" in command:
        return command
    if _git_version() < _MIN_GIT_VERSION:
        return command

    ends = _commit_word_ends(command)
    if not ends:
        return command
    insert = " --trailer " + shlex.quote(f"{TRAILER_KEY}: {identity}")
    out = command
    for end in sorted(ends, reverse=True):   # right to left keeps offsets valid
        out = out[:end] + insert + out[end:]
    return out
