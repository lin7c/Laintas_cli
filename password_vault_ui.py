"""password_vault_ui: local interactive UI behind /password.

Contract (docs/password-vault-design.md):

- /password takes NO arguments; secrets never ride in as command text —
  they would land in input history and terminal mirrors.
- All secret input is read from the local terminal with echo disabled
  (direct /dev/tty input, without stdin fallback). Nothing typed here becomes a chat message, REPL
  history entry, mirror event, or model tool argument.
- Descriptions and approved origins are public metadata the AI can see.
  Usernames, passwords, and notes are private: they are never printed,
  so terminal mirrors capture only public metadata.
- Injected slash commands are blocked at dispatch. A controlling /dev/tty
  is required, but its existence cannot prove the terminal's parent is trusted.
  This storage UI does not provide the broker's OS isolation boundary.
"""

from __future__ import annotations

import atexit
import os
import sys
import termios

import password_vault as pv


def _select_dialog(*args, **kwargs):
    """Lazily import the CLI's standard selector so the vault UI reuses
    the exact interaction other commands (resume picker, approval gates,
    command palette) already use — one interaction language across the CLI.

    Importing laintas_cli at module load would be circular (it imports
    this module at dispatch time), so resolve it on first use. If the CLI
    is not importable (standalone use), the caller falls back to _ask.
    """
    import laintas_cli
    return laintas_cli.select_dialog(*args, **kwargs)

_USAGE = ("/password takes no arguments — secrets must never be typed as "
          "command text. Everything happens inside the local vault UI.")


def _local_tty() -> bool:
    """Check terminal availability, not whether its parent is trusted.

    A PTY can be remote or agent-owned; OS isolation is still required for
    confidentiality against model-controlled processes.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


def _write_private(text: str) -> None:
    """Write text straight to the controlling terminal, bypassing
    sys.stdout entirely.

    The CLI's stdout runs through repl_mirror.TeeFile, so anything printed
    normally becomes a mirror event. Revealed secrets must not: this writes
    the raw /dev/tty file descriptor, which only reaches the user's screen.
    """
    fd = os.open("/dev/tty", os.O_WRONLY)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _secret(prompt: str, *, multiline: bool = False):
    """Read hidden input from /dev/tty. None on cancel/interrupt.

    Keep echo disabled and one reader alive across an entire multiline
    value, so pasted lines are neither echoed nor discarded between prompts.
    """
    fd = None
    previous = None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        previous = termios.tcgetattr(fd)
        hidden = list(previous)
        hidden[3] = (hidden[3] | termios.ICANON | termios.ISIG) & ~(termios.ECHO | termios.ECHONL)
        # Never fall back to stdin or to echoed input if this fails.
        termios.tcsetattr(fd, termios.TCSAFLUSH, hidden)
        os.write(fd, prompt.encode("utf-8"))
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as stream:
            lines = []
            while True:
                line = stream.readline()
                if not line:
                    return None
                value = line.rstrip("\r\n")
                if not multiline:
                    return value
                if not value:
                    return "\n".join(lines)
                lines.append(value)
                os.write(fd, b"... (hidden, empty line finishes): ")
    except (EOFError, KeyboardInterrupt):
        return None
    except (OSError, termios.error, UnicodeError):
        raise pv.VaultError("secure terminal input unavailable; no fallback allowed") from None
    finally:
        if fd is not None:
            try:
                if previous is not None:
                    termios.tcsetattr(fd, termios.TCSAFLUSH, previous)
                    os.write(fd, b"\n")
            finally:
                os.close(fd)


def _secret_lines(prompt: str):
    """Read a multi-line hidden value (private keys, tokens with line
    breaks). Echo stays disabled across all lines; an empty line ends
    input. Returns the joined value (\n between lines) or None on
    cancel/interrupt. An all-empty input returns "" so callers can treat
    it as "nothing entered"."""
    return _secret(prompt, multiline=True)


def _ask(prompt: str):
    """Read one visible line. None on cancel/interrupt."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def handle_command(parts) -> bool:
    """Entry point for the /password slash command."""
    if parts:
        print(_USAGE)
        return False
    if not _local_tty():
        print("The password vault needs the local interactive terminal (/dev/tty). "
              "Remote or mirrored entry is disabled by design.")
        return False
    try:
        _run()
    except (pv.VaultError, OSError):
        # Do not send arbitrary exception contents through the CLI dispatcher.
        print("Vault unavailable; secure input or storage failed. Vault locked.")
        return False
    return True


def _run() -> None:
    vault = pv.PasswordVault()
    atexit.register(vault.lock)
    try:
        _run_session(vault)
    finally:
        vault.lock()
        atexit.unregister(vault.lock)


def _confirm(question: str, *, default_no: bool = True) -> bool:
    """Yes/no confirmation via the CLI's standard selector (same component
    the approval gates use: y/n letter shortcuts, Esc/q cancels = No)."""
    try:
        chosen = _select_dialog(
            [("Yes", ""), ("No", "")],
            title=question,
            full_screen=False,
            letter_shortcuts=True,
            selected_index=1 if default_no else 0,
            hint="y yes  ·  n no  ·  Esc/q cancel",
        )
    except Exception:
        # Standalone fallback: plain prompt, same semantics.
        answer = _ask(f"{question} [y/N] ")
        return bool(answer) and answer.strip().lower() in ("y", "yes")
    return bool(chosen) and str(chosen[0]).strip().lower() == "yes"


def _choose_entry(vault, title: str):
    """Pick one entry via the CLI's standard searchable selector — the same
    interaction as /resume's session picker. Returns the entry id or None."""
    entries = vault.list_entries()
    if not entries:
        print("  no entries yet — add one first (a).")
        return None
    rows = []
    for entry in entries:
        origins = ", ".join(entry["origins"]) or "no origin"
        rows.append((f"{entry['description']}  [{entry['kind']}]",
                     f"{entry['id']}  ·  {origins}"))
    try:
        chosen = _select_dialog(
            rows, title=title, search=True, full_screen=True,
            hint="Type to filter  ↑↓ navigate  ↵ select  Esc/q cancel")
    except Exception:
        # Standalone fallback: numbered list on stdout.
        for index, entry in enumerate(entries, 1):
            origins = ", ".join(entry["origins"]) or "no origin"
            print(f"  {index}. {entry['id']}  [{entry['kind']}]  "
                  f"{entry['description']}  [{origins}]")
        answer = _ask("Entry number or id (empty cancels): ")
        if not answer:
            return None
        answer = answer.strip()
        if answer.isdigit() and 1 <= int(answer) <= len(entries):
            return entries[int(answer) - 1]["id"]
        for entry in entries:
            if entry["id"] == answer:
                return entry["id"]
        print("  no such entry.")
        return None
    if chosen is None:
        return None
    return entries[rows.index(chosen)]["id"]


def _lock_flow(vault) -> None:
    vault.lock()
    print("  vault locked.")


def _run_session(vault) -> None:
    if not vault.exists():
        if not _create_flow(vault):
            return
    print("Password vault — descriptions and origins ARE visible to the AI assistant;")
    print("secret fields use hidden input and encrypted storage; model tools return metadata only.")
    print("At hidden prompts NOTHING appears as you type — no dots, no stars. That is intentional;")
    print("type blind, press Enter. Protected autofill is unavailable (no isolation boundary).")
    # Reuses the CLI's standard selector (same component as approval gates
    # and /resume): arrow keys + Enter, single-letter shortcuts select
    # immediately, Esc/q leaves. First letters are unique (a s v e d u l c q)
    # so shortcuts are unambiguous, and dispatch is keyed by label — a
    # renamed menu item can never silently reroute to another flow.
    menu = [
        ("Add entry", "Store a new login or secret"),
        ("Show entries", "List ids, kinds, descriptions, origins"),
        ("View entry", "Reveal one secret on this screen"),
        ("Edit entry", "Change description/origins/secret fields"),
        ("Delete entry", "Remove one entry permanently"),
        ("Unlock vault", "Ask for the vault passphrase"),
        ("Lock vault", "Drop the in-memory key immediately"),
        ("Change passphrase", "Re-encrypt the vault with a new passphrase"),
        ("Quit", "Lock and leave the vault UI"),
    ]
    by_label = {
        "Add entry": _add_flow,
        "Show entries": _list_flow,
        "View entry": _view_flow,
        "Edit entry": _edit_flow,
        "Delete entry": _delete_flow,
        "Unlock vault": _unlock_flow,
        "Lock vault": _lock_flow,
        "Change passphrase": _passphrase_flow,
    }
    fallback_keys = {"a": _add_flow, "s": _list_flow, "v": _view_flow,
                     "e": _edit_flow, "d": _delete_flow, "u": _unlock_flow,
                     "l": _lock_flow, "c": _passphrase_flow}
    while True:
        try:
            choice = _select_dialog(
                menu, title="Password vault", full_screen=False,
                letter_shortcuts=True,
                hint="↑↓ navigate  ↵ select  Esc/q lock & quit")
        except Exception:
            # Standalone fallback: the same menu as plain letters.
            raw = _ask("[vault] (a)dd (s)how (v)iew (e)dit (d)elete "
                       "(u)nlock (l)ock (c)hange-passphrase (q)uit: ")
            raw = (raw or "").strip().lower()
            if not raw:
                continue
            handler = fallback_keys.get(raw)
            if handler is None:
                print("  unknown choice — a s v e d u l c q")
                continue
        else:
            if choice is None:          # Esc/q — lock & quit
                break
            label = (str(choice[0]) if isinstance(choice, (tuple, list))
                     else str(choice)).strip()
            if label == "Quit":
                break
            handler = by_label.get(label)
            if handler is None:
                break
        try:
            handler(vault)
        except pv.VaultLocked:
            print("  vault is locked — unlock first (u).")
        except pv.VaultError:
            print("  vault operation failed; check input or unlock the vault again.")
    vault.lock()
    try:
        count = len(vault.list_entries())
        print(f"Vault closed — {count} {'entry' if count == 1 else 'entries'}, locked.")
    except pv.VaultError:
        print("Vault closed.")


def _create_flow(vault) -> bool:
    print("No password vault exists yet for this account.")
    if not _confirm("Create a password vault now?"):
        print("Cancelled — no vault was created.")
        return False
    while True:
        first = _secret("New vault passphrase (nothing will show as you type; empty cancels): ")
        if first is None or not first:
            print("Cancelled — no vault was created.")
            return False
        if len(first) < 8 and not _confirm(
                "That passphrase is shorter than 8 characters. Use it anyway?"):
            continue
        second = _secret("Repeat passphrase: ")
        if second is None:
            return False
        if first != second:
            print("  Passphrases do not match — try again.")
            continue
        try:
            vault.create(first)
        except pv.VaultExists:
            print("  A vault appeared meanwhile — opening the existing one.")
            return vault.unlock(first)
        print("  Vault created and unlocked.")
        return True


def _add_flow(vault) -> None:
    if not vault.is_unlocked():
        raise pv.VaultLocked("vault is locked")
    description = _ask("Description (visible to the AI, e.g. 'Work account'): ")
    if description is None or not description.strip():
        print("  cancelled — description is required.")
        return
    # Kind picker reuses the CLI's standard selector (l/s letter shortcuts,
    # arrows + Enter, Esc/q cancels) — same interaction as approval gates.
    try:
        chosen = _select_dialog(
            [("login", "username + password for a site"),
             ("secret", "a single key/token, any format")],
            title="Entry kind", full_screen=False, letter_shortcuts=True,
            hint="l login  ·  s secret  ·  Esc/q cancel")
        kind = (str(chosen[0]) if isinstance(chosen, (tuple, list))
                else str(chosen)) if chosen is not None else None
    except Exception:
        raw = _ask("Kind: (1) login — username + password for a site  "
                   "(2) secret — a single key/token, any format [1] ")
        kind = {"1": "login", "2": "secret"}.get((raw or "1").strip() or "1")
    if kind == "secret":
        _add_secret_flow(vault, description.strip())
    elif kind == "login":
        _add_login_flow(vault, description.strip())
    else:
        print("  cancelled.")


def _add_login_flow(vault, description: str) -> None:
    origins_raw = _ask("Approved HTTPS origin(s), comma-separated "
                       "(e.g. https://example.com): ")
    if origins_raw is None:
        print("  cancelled.")
        return
    origins = [part.strip() for part in origins_raw.split(",") if part.strip()]
    username = _secret("Username (hidden): ")
    if username is None:
        print("  cancelled.")
        return
    while True:
        first = _secret("Password (hidden, empty cancels): ")
        if first is None or not first:
            print("  cancelled.")
            return
        second = _secret("Repeat password: ")
        if second is None:
            print("  cancelled.")
            return
        if second is not None and first == second:
            break
        print("  Passwords do not match — try again.")
    notes = _secret("Notes (hidden, empty to skip): ")
    if notes is None:
        print("  cancelled.")
        return
    try:
        entry_id = vault.add_entry(description, origins, username, first, notes)
    except pv.VaultError as exc:
        print(f"  not stored: {exc}")
        return
    print(f"  added {entry_id} (login): {description}")
    print("  (description and origins are visible to the AI; "
          "username/password/notes are not)")


def _add_secret_flow(vault, description: str) -> None:
    origins_raw = _ask("Related HTTPS origin(s), comma-separated — optional "
                       "(Enter to skip): ")
    if origins_raw is None:
        print("  cancelled.")
        return
    origins = [part.strip() for part in origins_raw.split(",") if part.strip()]
    secret = _secret_lines("Secret value (hidden, multi-line, "
                           "empty line finishes): ")
    if secret is None or not secret:
        print("  cancelled — secret value is required.")
        return
    notes = _secret("Notes (hidden, empty to skip): ")
    if notes is None:
        print("  cancelled.")
        return
    try:
        entry_id = vault.add_entry(description, origins or None, kind="secret",
                                   secret=secret, notes=notes)
    except pv.VaultError as exc:
        print(f"  not stored: {exc}")
        return
    print(f"  added {entry_id} (secret): {description}")
    print("  (description and any origins are visible to the AI; "
          "the secret value and notes are not)")


def _list_flow(vault) -> None:
    entries = vault.list_entries()
    if not entries:
        print("  no entries yet — (a)dd one.")
        return
    for entry in entries:
        origins = ", ".join(entry["origins"]) or "no origin"
        print(f"  {entry['id']}  [{entry['kind']}]  {entry['description']}"
              f"  [{origins}]")


def _view_flow(vault) -> None:
    """Reveal one entry's secret on the local screen only.

    The value is written straight to /dev/tty (never sys.stdout), so it
    bypasses the CLI's mirror tee and cannot become a mirror event or chat
    artifact. The user is warned about scrollback, and a keypress clears
    the screen with an ANSI erase. The value never enters any Python
    variable that outlives this function.
    """
    if not vault.is_unlocked():
        raise pv.VaultLocked("vault is locked")
    entry_id = _choose_entry(vault, "View which entry?")
    if not entry_id:
        return
    if not _confirm("Show the secret on this screen? It stays in the "
                    "terminal scrollback until cleared."):
        print("  not shown.")
        return
    secret = vault.get_secret(entry_id)
    lines = [""]
    if secret["kind"] == "login":
        lines.append(f"  username: {secret['username']}")
        lines.append(f"  password: {secret['password']}")
    else:
        lines.append("  secret:")
        for value_line in secret["secret"].splitlines() or [""]:
            lines.append(f"    {value_line}")
    if secret.get("notes"):
        lines.append(f"  notes:    {secret['notes']}")
    lines.append("")
    lines.append("  Press Enter to clear this screen — the text above "
                 "remains in your terminal's scrollback buffer.")
    _write_private("\n".join(lines) + "\n")
    _ask("")
    _write_private("\x1b[2J\x1b[H")   # ANSI: erase screen, cursor home
    print("  screen cleared; secret dropped from memory.")


def _edit_flow(vault) -> None:
    if not vault.is_unlocked():
        raise pv.VaultLocked("vault is locked")
    entry_id = _choose_entry(vault, "Edit which entry?")
    if not entry_id:
        return
    current = {e["id"]: e for e in vault.list_entries()}[entry_id]
    origins_text = ", ".join(current["origins"]) or "no origin"
    print(f"  editing {entry_id} [{current['kind']}]: "
          f"{current['description']}  [{origins_text}]")
    print("  Press Enter to keep each field. Secret fields stay hidden.")
    kwargs = {}
    description = _ask(f"  New description [{current['description']}]: ")
    if description is None:
        return
    if description is not None and description.strip():
        kwargs["description"] = description.strip()
    origins_raw = _ask(f"  New origins [{origins_text}]: ")
    if origins_raw is None:
        return
    if origins_raw is not None and origins_raw.strip():
        kwargs["origins"] = [p.strip() for p in origins_raw.split(",") if p.strip()]
    if current["kind"] == "login":
        username = _secret("  New username (hidden, empty keeps current): ")
        if username is None:
            return
        if username:
            kwargs["username"] = username
        password = _secret("  New password (hidden, empty keeps current): ")
        if password is None:
            return
        if password:
            confirm = _secret("  Repeat new password: ")
            if confirm is None or confirm != password:
                print("  cancelled or passwords do not match — entry unchanged.")
                return
            else:
                kwargs["password"] = password
    else:
        secret = _secret_lines("  New secret value (hidden, multi-line, "
                               "empty first line keeps current): ")
        if secret is None:
            return
        if secret:
            kwargs["secret"] = secret
    notes = _secret("  New notes (hidden, empty keeps current): ")
    if notes is None:
        return
    if notes:
        kwargs["notes"] = notes
    if not kwargs:
        print("  nothing to change.")
        return
    try:
        changed = vault.update_entry(entry_id, **kwargs)
    except pv.VaultError as exc:
        print(f"  not updated: {exc}")
        return
    if changed:
        print(f"  updated {entry_id}.")
    else:
        print("  entry disappeared — not updated.")


def _delete_flow(vault) -> None:
    entry_id = _choose_entry(vault, "Delete which entry?")
    if not entry_id:
        return
    if not _confirm(f"Delete {entry_id} permanently?"):
        print("  not deleted.")
        return
    if vault.delete_entry(entry_id):
        print(f"  deleted {entry_id}.")
    else:
        print("  entry disappeared — not deleted.")


def _unlock_flow(vault) -> None:
    if vault.is_unlocked():
        print("  already unlocked.")
        return
    passphrase = _secret("Vault passphrase (hidden): ")
    if passphrase is None:
        return
    if vault.unlock(passphrase):
        print("  vault unlocked.")
    else:
        print("  unlock failed — wrong passphrase or tampered vault.")


def _passphrase_flow(vault) -> None:
    if not vault.is_unlocked():
        print("  unlock first (u).")
        return
    first = _secret("New passphrase (hidden, empty cancels): ")
    if not first:
        print("  cancelled.")
        return
    if len(first) < 8 and not _confirm(
            "Shorter than 8 characters. Use it anyway?"):
        print("  cancelled.")
        return
    second = _secret("Repeat new passphrase: ")
    if second is None or first != second:
        print("  passphrases do not match — unchanged.")
        return
    vault.change_passphrase(first)
    print("  passphrase changed; vault re-encrypted with a fresh salt.")
