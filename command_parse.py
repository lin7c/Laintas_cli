"""Shell command decomposition for policy evaluation.

Why this exists: matching a regex against the raw command line is the bug class
Adversa named GuardFall — the guard inspects what the user typed, then Bash
unwinds the quoting and runs something else. ``\\rm``, ``"rm"``, ``r''m``,
``$X``, ``eval "…"`` and ``… | sh`` all reach the same syscall as ``rm``, and a
string match sees five different strings.

The approach here is deliberately *not* "emulate Bash". That is unwinnable —
arbitrary expansion is Turing-complete and any partial emulation becomes its own
bypass surface. Instead:

  1. Tokenize with quote and escape state, and split on real operators only, so a
     ``;`` inside a string is not a command separator.
  2. Canonicalise what can be resolved with certainty: quote splicing, backslash
     escapes, leading ``VAR=value`` assignments, literal variables assigned
     earlier in the same line, absolute paths.
  3. Look *inside* shell wrappers — ``eval``, ``sh -c``, a pipe into ``sh`` —
     because the payload is the thing worth judging, not the wrapper.
  4. Refuse to guess about the rest. A command word that comes out of ``$(…)``
     or an unknown variable is reported as UNRESOLVED, and the caller is expected
     to treat that as untrusted rather than as harmless.

Point 4 is the design: the analyser's job is to be honest about what it could not
determine, so the policy can fail closed on ambiguity instead of failing open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Interpreters that take a command string as an argument. The payload passed to
# these is analysed recursively — a rule that inspects only the wrapper sees
# `bash` and waves through whatever it was handed.
_SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"}
_WRAPPER_STRING_FLAGS = {"-c", "--command"}

# The Windows build runs this CLI inside a private WSL distribution with
# interop on and the Windows PATH appended, so `powershell.exe -Command …` and
# `cmd.exe /c …` are one word away at all times — and every rule in policy.py
# was written for the Linux side of that boundary. Treating them as wrappers is
# what makes the payload visible to those rules instead of the launcher name.
#
# PowerShell accepts any unambiguous prefix of a parameter name, so `-Com`,
# `-Comma` and `-Command` are the same flag; the check below is by prefix for
# that reason, not for convenience.
_WINDOWS_WRAPPERS = {"powershell.exe", "powershell", "pwsh.exe", "pwsh",
                     "cmd.exe", "cmd", "wsl.exe", "wsl"}
_PS_COMMAND_FLAG_PREFIXES = ("-command", "-c")
_PS_ENCODED_FLAG_PREFIXES = ("-encodedcommand", "-enc", "-ec", "-e")
_CMD_STRING_FLAGS = {"/c", "/k"}

# Commands that consume a shell script on stdin. `echo "rm -rf /" | sh` is the
# canonical form of this trick.
_STDIN_SHELLS = _SHELL_WRAPPERS | {"eval"}

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

#: Marker used in place of a command word that cannot be resolved statically.
UNRESOLVED = "\x00unresolved\x00"

# Risk flags. These describe *how* a command was written, independent of what it
# does, and exist so policy can treat obfuscation itself as a signal.
RISK_SUBSTITUTION = "command_substitution"   # $( … ) or backticks
RISK_INDIRECT = "indirect_command"           # command word came from a variable
RISK_WRAPPER = "shell_wrapper"               # eval / sh -c / … | sh
RISK_UNRESOLVED = "unresolved_command"       # could not determine what runs


@dataclass
class Segment:
    """One command in a pipeline or chain, after canonicalisation."""
    raw: str
    command: str                 # canonical form, e.g. "rm -rf /"
    program: str                 # resolved program name, e.g. "rm"; may be UNRESOLVED
    risks: set = field(default_factory=set)

    @property
    def resolved(self) -> bool:
        return self.program != UNRESOLVED


@dataclass
class Analysis:
    segments: list
    risks: set

    @property
    def commands(self) -> list:
        """Canonical command strings — what policy rules should match against."""
        return [s.command for s in self.segments if s.command]

    @property
    def unresolved(self) -> bool:
        return RISK_UNRESOLVED in self.risks


def _split_tokens(text: str) -> tuple:
    """Split a command line into (tokens, operators_seen), respecting quoting.

    Returns tokens where each token keeps its original quoting so the caller can
    decide whether to unquote — separators inside quotes are not separators.
    """
    tokens, current = [], []
    operators = []
    i, n = 0, len(text)
    quote = None          # None | "'" | '"'
    depth = 0             # $( … ) nesting

    def flush():
        if current:
            tokens.append("".join(current))
            current.clear()

    while i < n:
        ch = text[i]

        if quote == "'":
            current.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue

        if quote == '"':
            current.append(ch)
            if ch == "\\" and i + 1 < n:
                current.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue

        if ch == "\\" and i + 1 < n:
            current.append(ch)
            current.append(text[i + 1])
            i += 2
            continue

        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue

        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            depth += 1
            current.append(text[i:i + 2])
            i += 2
            continue

        if ch == ")" and depth > 0:
            depth -= 1
            current.append(ch)
            i += 1
            continue

        if depth == 0:
            two = text[i:i + 2]
            if two in ("&&", "||", ";;"):
                flush()
                operators.append(two)
                tokens.append(two)
                i += 2
                continue
            if ch in ";|&\n":
                flush()
                operators.append(ch)
                tokens.append(ch)
                i += 1
                continue
            if ch.isspace():
                flush()
                i += 1
                continue

        current.append(ch)
        i += 1

    flush()
    return tokens, operators


_OPERATORS = {"&&", "||", ";;", ";", "|", "&", "\n"}


def _unquote(word: str) -> tuple:
    """Strip quoting and escapes from a word.

    Returns ``(value, had_quoting)``. ``r''m`` becomes ``rm`` — the splice is the
    whole point of the trick, and it disappears the moment quotes are removed.
    """
    out, i, n = [], 0, len(word)
    quote = None
    quoted = False

    while i < n:
        ch = word[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                out.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                out.append(word[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            else:
                out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(word[i + 1])
            quoted = True
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            quoted = True
            i += 1
            continue
        out.append(ch)
        i += 1

    return "".join(out), quoted


def _has_substitution(word: str) -> bool:
    return "$(" in word or "`" in word


def _expand_known(word: str, variables: dict) -> tuple:
    """Substitute variables assigned earlier on the same line.

    Only assignments we actually watched happen are expanded. An unknown variable
    is left in place and reported, because guessing its value would be the same
    mistake as matching the raw string.
    """
    unknown = False

    def replace(match):
        nonlocal unknown
        name = match.group(1) or match.group(2)
        if name in variables:
            return variables[name]
        unknown = True
        return match.group(0)

    return _VAR_RE.sub(replace, word), unknown


def _program_name(word: str) -> str:
    """Resolve a command word to a bare program name.

    ``/bin/rm`` and ``rm`` reach the same binary, so a rule written for one must
    see the other.
    """
    name = word.rsplit("/", 1)[-1]
    # A Windows program is addressed by either separator and by any casing:
    # `C:\\Windows\\System32\\CMD.EXE`, `cmd.exe` and `Cmd.Exe` are one program.
    # Only `.exe` names are folded — Linux filenames are case-sensitive and
    # `Make` is not `make`.
    name = name.rsplit("\\", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name.lower()
    return name


def _windows_payload(program: str, rest: list) -> tuple:
    """The script a Windows wrapper will run, plus what could not be resolved.

    Returns ``(payload, risks)``. An encoded command is decoded when it decodes
    cleanly — the point of the guard is to see what runs, and refusing to look
    at base64 would leave the most deliberate form of hiding as the one that
    works. Anything still unreadable (a `-File` script, undecodable base64) is
    reported UNRESOLVED so policy fails closed on it.
    """
    import base64

    name = program.lower()
    risks: set = set()
    words = [w for w in rest if w]
    for idx, word in enumerate(words):
        lowered = word.lower()
        following = words[idx + 1] if idx + 1 < len(words) else ""
        if name.startswith(("cmd",)):
            if lowered in _CMD_STRING_FLAGS and following:
                return " ".join(words[idx + 1:]), risks
            continue
        # powershell / pwsh / wsl
        if name.startswith(("wsl",)):
            # `wsl.exe -- <command>` and `wsl.exe <command>` both run a Linux
            # command line, which every existing rule already understands.
            if lowered == "--" and following:
                return " ".join(words[idx + 1:]), risks
            continue
        if any(lowered.startswith(flag) for flag in _PS_ENCODED_FLAG_PREFIXES) \
                and following:
            try:
                raw = base64.b64decode(following, validate=True)
                decoded = raw.decode("utf-16-le").strip()
            except Exception:
                decoded = ""
            if decoded:
                return decoded, {RISK_WRAPPER}
            return "", {RISK_WRAPPER, RISK_UNRESOLVED}
        if any(lowered.startswith(flag) for flag in _PS_COMMAND_FLAG_PREFIXES) \
                and following:
            return " ".join(words[idx + 1:]), risks
        if lowered.startswith("-file"):
            # The script is on disk; its contents are not knowable here.
            return "", {RISK_WRAPPER, RISK_UNRESOLVED}
    return "", risks


def analyze(command: str, *, _depth: int = 0) -> Analysis:
    """Decompose a command line into the commands it will actually run."""
    segments: list = []
    risks: set = set()

    if _depth > 4 or not command or not command.strip():
        return Analysis(segments, risks)

    tokens, _ = _split_tokens(command)

    # Group tokens into segments split by operators, remembering whether the
    # previous operator was a pipe — `… | sh` reads a script from stdin.
    groups: list = []
    current: list = []
    piped_into: list = []
    prev_was_pipe = False

    for token in tokens:
        if token in _OPERATORS:
            if current:
                groups.append(current)
                piped_into.append(prev_was_pipe)
                current = []
            prev_was_pipe = token == "|"
            continue
        current.append(token)
    if current:
        groups.append(current)
        piped_into.append(prev_was_pipe)

    variables: dict = {}

    for group, from_pipe in zip(groups, piped_into):
        words = list(group)

        # Leading VAR=value assignments are environment, not the command.
        while words and _ASSIGNMENT_RE.match(words[0]):
            name, _, value = words[0].partition("=")
            clean, _ = _unquote(value)
            if not _has_substitution(value):
                variables[name] = clean
            words.pop(0)

        if not words:
            continue

        head_raw = words[0]
        expanded, unknown_var = _expand_known(head_raw, variables)
        head, _ = _unquote(expanded)

        seg_risks = set()
        if head_raw != head and _VAR_RE.search(head_raw):
            seg_risks.add(RISK_INDIRECT)

        if _has_substitution(expanded):
            # The command word is produced by a subshell. Its output is not
            # knowable here, so the honest answer is "unknown", not "harmless".
            seg_risks.add(RISK_SUBSTITUTION)
            seg_risks.add(RISK_UNRESOLVED)
            program = UNRESOLVED
        elif unknown_var:
            seg_risks.add(RISK_INDIRECT)
            seg_risks.add(RISK_UNRESOLVED)
            program = UNRESOLVED
        else:
            program = _program_name(head)

        rest = []
        for word in words[1:]:
            expanded_word, _ = _expand_known(word, variables)
            if _has_substitution(expanded_word):
                seg_risks.add(RISK_SUBSTITUTION)
                rest.append(expanded_word)
                continue
            clean, _ = _unquote(expanded_word)
            rest.append(clean)

        canonical = " ".join([program if program != UNRESOLVED else head] + rest).strip()

        segments.append(Segment(raw=" ".join(group), command=canonical,
                                program=program, risks=seg_risks))
        risks |= seg_risks

        # ── Look inside shell wrappers ──────────────────────────────────────
        payloads: list = []

        if program in _SHELL_WRAPPERS:
            for idx, word in enumerate(rest):
                if word in _WRAPPER_STRING_FLAGS and idx + 1 < len(rest):
                    payloads.append(rest[idx + 1])
                    break

        if program.lower() in _WINDOWS_WRAPPERS:
            windows_payload, windows_risks = _windows_payload(program, rest)
            if windows_payload:
                payloads.append(windows_payload)
            if windows_risks:
                seg_risks |= windows_risks
                risks |= windows_risks

        if program == "eval" and rest:
            payloads.append(" ".join(rest))

        if from_pipe and program in _STDIN_SHELLS:
            # The script arrives on stdin. The producing segment was analysed on
            # its own; flagging the wrapper is what stops it being laundered.
            seg_risks.add(RISK_WRAPPER)
            risks.add(RISK_WRAPPER)

        for payload in payloads:
            if not payload:
                continue
            risks.add(RISK_WRAPPER)
            inner = analyze(payload, _depth=_depth + 1)
            segments.extend(inner.segments)
            risks |= inner.risks

    # A quoted string that is itself a command, piped into a shell, only becomes
    # visible once we treat the producer's literal argument as a script.
    if any(s.program in _STDIN_SHELLS for s in segments) and _depth < 4:
        for seg in list(segments):
            if seg.program in ("echo", "printf", "cat"):
                payload = seg.command.split(" ", 1)[1] if " " in seg.command else ""
                if payload:
                    inner = analyze(payload, _depth=_depth + 1)
                    segments.extend(inner.segments)
                    risks |= inner.risks

    return Analysis(segments, risks)


def effective_commands(command: str) -> list:
    """Canonical command strings a policy should match, including the original.

    The original is kept because existing rules were written against it, and a
    canonicalisation bug must not be able to *remove* a match that used to work.
    """
    analysis = analyze(command)
    out = [command.strip()]
    for candidate in analysis.commands:
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def obfuscation_risks(command: str) -> set:
    return analyze(command).risks
