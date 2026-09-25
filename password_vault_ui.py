"""password_vault_ui: local interactive UI behind /password.

Local vault UI contract:

- /password takes NO arguments; secrets never ride in as command text —
  they would land in input history and terminal mirrors.
- All secret input is read from the local terminal with echo disabled
  (direct /dev/tty input, without stdin fallback). Nothing typed here becomes a chat message, REPL
  history entry, mirror event, or model tool argument.
- Descriptions and approved origins are public metadata the AI can see.
  Usernames, passwords, and notes are private: reveal uses explicit local
  terminal I/O on an alternate screen, bypassing stdout and terminal mirrors.
- Injected slash commands are blocked at dispatch. A controlling /dev/tty
  is required, but its existence cannot prove the terminal's parent is trusted.
  This storage UI does not provide the broker's OS isolation boundary.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import termios
import time

import password_vault as pv
import resource_ui


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


@contextlib.contextmanager
def _private_terminal():
    """Explicit TTY I/O; never let prompt_toolkit choose stdout/the mirror."""
    from prompt_toolkit.input import create_input
    from prompt_toolkit.output import create_output
    import terminal_arbiter

    fd = None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        termios.tcgetattr(fd)  # fail before constructing a reader on a bad TTY
        stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        fd = None  # stream owns it
        with stream, terminal_arbiter.hold(
                "password-private", terminal_arbiter.Mode.EXTERNAL):
            reader = create_input(stdin=stream)
            try:
                with reader.raw_mode():
                    # Library raw-mode implementations may tolerate TTY errors.
                    # Verify that hidden, noncanonical input actually took effect.
                    flags = termios.tcgetattr(stream.fileno())[3]
                    if flags & (termios.ECHO | termios.ICANON):
                        raise pv.VaultError("secure terminal input unavailable")
                    output = create_output(stdout=stream)
                    yield reader, output
            finally:
                reader.close()
    except (OSError, termios.error, UnicodeError):
        raise pv.VaultError("secure terminal input unavailable; no fallback allowed") from None
    finally:
        if fd is not None:
            os.close(fd)


def _secret(prompt: str, *, multiline: bool = False):
    """Masked, editable input with no canonical-line limit or persistent history.

    Enter finishes a single line. Ctrl+D finishes multiline input, preserving
    blank lines and pasted trailing newlines. Escape/Ctrl+C cancels either.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import DummyHistory
    from prompt_toolkit.key_binding import KeyBindings

    keys = KeyBindings()

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    @keys.add("c-d")
    def finish(event):
        event.app.exit(result=event.current_buffer.text if multiline else None)

    session = None
    try:
        with _private_terminal() as (reader, output):
            session = PromptSession(
                input=reader, output=output, history=DummyHistory(),
                is_password=True, multiline=multiline, key_bindings=keys,
                enable_open_in_editor=False, enable_suspend=False,
                enable_history_search=False)
            # A long prefix can leave no width for a multiline buffer on a
            # narrow terminal. Put instructions above the editable input.
            output.write(prompt + "\n")
            output.flush()
            return session.prompt("Hidden> ")
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        if session is not None:
            # Drop buffers, undo/redo and working history references on exit.
            session.default_buffer.reset()
            session.default_buffer.history._loaded_strings.clear()


def _secret_lines(prompt: str):
    """Read an arbitrary multiline secret; Ctrl+D saves, Escape cancels."""
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
            full_screen=True,
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
            hint="Type to filter  ↑↓ navigate  ↵ select  Esc cancel")
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


def _lock_flow(vault) -> str:
    vault.lock()
    print("  vault locked.")
    return "Vault locked."


class _VaultBrowser(resource_ui.ResourceBrowser):
    """The shared full-screen browser, containing public metadata only."""

    def __init__(self, vault, **kwargs):
        self.vault = vault
        self.feedback = ""
        super().__init__(**kwargs)

    def _pre_run(self):
        super()._pre_run()
        self.status = self.feedback or self.status
        self._feedback_until = time.monotonic() + 3

    def _refresh_live(self):
        super()._refresh_live()
        self.title = "Password vault · " + (
            "unlocked" if self.vault.is_unlocked() else "locked")
        if self.feedback and time.monotonic() >= self._feedback_until:
            if self.status == self.feedback:
                self.status = ""
            self.feedback = ""


def _make_browser(vault, **io_options):
    def items():
        try:
            entries = vault.list_entries()
        except pv.VaultError:
            # A missing/disposable cache must still leave Unlock available.
            return []
        return [resource_ui.UIItem(
            key=e["id"], title=e["description"],
            subtitle=", ".join(e["origins"]) or "No related site",
            badge=e["kind"], payload=e,
            search_text=e["id"],
        ) for e in entries]

    def detail(item):
        e = item.payload
        return resource_ui.UIDetail.text(item.title, "\n".join([
            f"Type       {e['kind']}",
            f"ID         {e['id']}",
            "Sites      " + (", ".join(e["origins"]) or "None"),
            f"Created    {e['created']}",
            f"Updated    {e['updated']}", "",
            "Descriptions and sites are visible to the AI.",
            "Secret fields stay private. Press v to reveal locally.",
            "e Edit · d Delete · a Add · u Unlock · l Lock · c Passphrase",
        ]))

    return _VaultBrowser(
        vault, title="Password vault", load_items=items, load_detail=detail,
        primary_action="view", primary_label="Details",
        actions=[
            resource_ui.UIAction("a", "add", "Add", allow_empty=True),
            resource_ui.UIAction("v", "reveal", "Reveal"),
            resource_ui.UIAction("e", "edit", "Edit"),
            resource_ui.UIAction("d", "delete", "Delete", style="class:error"),
            resource_ui.UIAction("u", "unlock", "Unlock", allow_empty=True),
            resource_ui.UIAction("l", "lock", "Lock", allow_empty=True),
            resource_ui.UIAction("c", "passphrase", "Passphrase", allow_empty=True),
        ],
        pane_labels=("ENTRIES", "DETAILS"), refresh_interval=1.0,
        empty_message="No entries available. a Add · u Unlock / rebuild list",
        **io_options)


def _run_session(vault) -> None:
    if not vault.exists() and not _create_flow(vault):
        return
    browser = _make_browser(vault)
    handlers = {"add": _add_flow, "reveal": _view_flow, "edit": _edit_flow,
                "delete": _delete_flow, "unlock": _unlock_flow,
                "lock": _lock_flow, "passphrase": _passphrase_flow}
    while True:
        browser.title = "Password vault · " + (
            "unlocked" if vault.is_unlocked() else "locked")
        try:
            outcome = browser.run()
        except Exception:
            # If the renderer fails, retain a small usable local fallback.
            choice = _ask("[vault] a add · v view · e edit · d delete · "
                          "u unlock · l lock · c passphrase · q quit: ")
            if choice is None or choice.strip().lower() == "q":
                break
            action = {"a": "add", "v": "reveal", "e": "edit", "d": "delete",
                      "u": "unlock", "l": "lock", "c": "passphrase"}.get(
                          choice.strip().lower())
            outcome = resource_ui.UIOutcome(action or "unknown")
        if outcome.action == "cancel":
            break
        if outcome.item:
            browser._last_selected_key = outcome.item.key
        handler = handlers.get(outcome.action)
        if handler is None:
            continue
        try:
            if outcome.action in ("reveal", "edit", "delete"):
                result = handler(vault, outcome.item.key if outcome.item else None)
            else:
                result = handler(vault)
            browser.feedback = (result if isinstance(result, str) else
                                "Vault unlocked." if result is True else
                                "Cancelled.")
        except pv.VaultLocked:
            browser.feedback = "Vault locked; retry the action to unlock."
        except pv.VaultError:
            browser.feedback = "Operation failed; check input or unlock again."
        except OSError:
            browser.feedback = "Storage unavailable; operation could not complete."
        if vault.cache_warning:
            browser.feedback = ("Vault saved; list cache unavailable. "
                                "Unlock to read the current entries.")
        browser.detail_cache.clear()
        browser.detail = None
        browser.detail_key = ""
    vault.lock()
    print("Vault closed — locked.")


def _create_flow(vault) -> bool:
    print("No password vault exists yet for this account.")
    if not _confirm("Create a password vault now?"):
        print("Cancelled — no vault was created.")
        return False
    while True:
        first = _secret("New vault passphrase (hidden; empty cancels): ")
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


def _add_flow(vault) -> str | None:
    if not _ensure_unlocked(vault):
        return
    description = _description_input()
    if description is None:
        return
    # Kind picker reuses the CLI's standard selector (l/s letter shortcuts,
    # arrows + Enter, Esc/q cancels) — same interaction as approval gates.
    try:
        chosen = _select_dialog(
            [("login", "username + password for a site"),
             ("secret", "a single key/token, any format")],
            title="Entry kind", full_screen=True, letter_shortcuts=True,
            hint="l login  ·  s secret  ·  Esc/q cancel")
        kind = (str(chosen[0]) if isinstance(chosen, (tuple, list))
                else str(chosen)) if chosen is not None else None
    except Exception:
        raw = _ask("Kind: (1) login — username + password for a site  "
                   "(2) secret — a single key/token, any format [1] ")
        if raw is None:
            return
        kind = {"1": "login", "2": "secret"}.get((raw or "1").strip() or "1")
    if kind == "secret":
        return _add_secret_flow(vault, description.strip())
    elif kind == "login":
        return _add_login_flow(vault, description.strip())
    else:
        print("  cancelled.")


def _description_input():
    while True:
        value = _ask("Description (visible to the AI, empty cancels): ")
        if value is None or not value.strip():
            return None
        try:
            return pv._clean_description(value)
        except pv.VaultError:
            print("  Use a single line of 1–200 characters.")


def _origins_input(*, optional=False):
    while True:
        value = _ask("HTTPS origin(s), comma-separated (e.g. https://example.com)"
                     + ("; Enter for none: " if optional else ": "))
        if value is None:
            return None
        origins = [p.strip() for p in value.split(",") if p.strip()]
        if not origins and optional:
            return []
        try:
            return pv._clean_origins(origins)
        except pv.VaultError:
            print("  Enter an HTTPS site origin, without a path, query or wildcard.")


def _password_input(prompt):
    while True:
        first = _secret(prompt)
        if not first:
            return None
        second = _secret("Repeat password: ")
        if second is None:
            return None
        if first == second:
            return first
        print("  Passwords do not match — try again.")


def _add_login_flow(vault, description: str) -> str | None:
    origins = _origins_input()
    if origins is None:
        return
    username = _secret("Username (hidden): ")
    if username is None:
        print("  cancelled.")
        return
    first = _password_input("Password (hidden, empty cancels): ")
    if first is None:
        return
    notes = _secret("Notes (hidden, empty to skip): ")
    if notes is None:
        print("  cancelled.")
        return
    if not _ensure_unlocked(vault):
        return
    try:
        entry_id = vault.add_entry(description, origins, username, first, notes)
    except pv.VaultError as exc:
        print(f"  not stored: {exc}")
        return "Not stored; check input or unlock again."
    print(f"  added {entry_id} (login): {description}")
    print("  (description and origins are visible to the AI; "
          "username/password/notes are not)")
    return f"Added {description}."


def _add_secret_flow(vault, description: str) -> str | None:
    origins = _origins_input(optional=True)
    if origins is None:
        return
    secret = _secret_lines("Secret value (hidden, multi-line, "
                           "Enter adds a line, Ctrl+D saves, Esc cancels): ")
    if secret is None or not secret:
        print("  cancelled — secret value is required.")
        return
    notes = _secret("Notes (hidden, empty to skip): ")
    if notes is None:
        print("  cancelled.")
        return
    if not _ensure_unlocked(vault):
        return
    try:
        entry_id = vault.add_entry(description, origins or None, kind="secret",
                                   secret=secret, notes=notes)
    except pv.VaultError as exc:
        print(f"  not stored: {exc}")
        return "Not stored; check input or unlock again."
    print(f"  added {entry_id} (secret): {description}")
    print("  (description and any origins are visible to the AI; "
          "the secret value and notes are not)")
    return f"Added {description}."


def _list_flow(vault) -> None:
    entries = vault.list_entries()
    if not entries:
        print("  no entries yet — (a)dd one.")
        return
    for entry in entries:
        origins = ", ".join(entry["origins"]) or "no origin"
        print(f"  {entry['id']}  [{entry['kind']}]  {entry['description']}"
              f"  [{origins}]")


def _display_value(value):
    # Render control bytes as visible escapes, never terminal instructions.
    return "".join(ch if ch in "\n\t" or (ord(ch) >= 32 and not 127 <= ord(ch) <= 159)
                   else repr(ch)[1:-1] for ch in value)


class _PrivateView(resource_ui.ResourceBrowser):
    """Use the shared renderer with close semantics for a private detail view."""

    def _build_application(self):
        super()._build_application()

        @self.app.key_bindings.add("enter")
        @self.app.key_bindings.add("c-j")
        @self.app.key_bindings.add("escape")
        def close(event):
            event.app.exit(result=resource_ui.UIOutcome("close"))

    def _footer_fragments(self):
        return [("class:footer.key", " Enter / Esc"),
                ("class:footer", " Close · ↑↓ Scroll · PgUp/PgDn Page")]


def _view_flow(vault, entry_id=None) -> str | None:
    """Private full-screen reveal; neither renderer nor values touch stdout."""
    if not _ensure_unlocked(vault):
        return
    entry_id = entry_id or _choose_entry(vault, "View which entry?")
    if not entry_id:
        return
    if not _confirm("Show the secret on this local screen?"):
        return "Not shown."
    if not _ensure_unlocked(vault):
        return
    secret = vault.get_secret(entry_id)
    if secret["kind"] == "login":
        lines = [f"username: {secret['username']}", f"password: {secret['password']}"]
    else:
        lines = ["secret:", secret["secret"]]
    if secret.get("notes"):
        lines.extend(["notes:", secret["notes"]])
    content = _display_value("\n".join(lines))
    secret.clear()
    lines.clear()
    browser = None
    try:
        with _private_terminal() as (reader, output):
            def detail(_item):
                nonlocal content
                if not vault.is_unlocked():
                    content = ""
                return resource_ui.UIDetail.text(
                    "Private entry", "Enter / Esc closes · local screen only\n\n" + (
                        content or "Vault locked."))

            browser = _PrivateView(
                title="Private entry", load_items=lambda: [
                    resource_ui.UIItem(entry_id, "Private entry")],
                load_detail=detail, searchable=False,
                primary_action="close", primary_label="Close",
                refresh_interval=1.0, input=reader, output=output)
            browser.mode = browser.focus = "detail"
            browser.run()
    finally:
        content = ""
        if browser is not None:
            browser.detail = None
            browser.detail_cache.clear()
    return "Private view closed."


def _edit_flow(vault, entry_id=None) -> str | None:
    if not _ensure_unlocked(vault):
        return
    entry_id = entry_id or _choose_entry(vault, "Edit which entry?")
    if not entry_id:
        return
    current = next((e for e in vault.list_entries() if e["id"] == entry_id), None)
    if current is None:
        return "Entry disappeared — not updated."
    changes = {}
    fields = [("Description", "description"), ("Origins", "origins")]
    fields += ([("Username", "username"), ("Password", "password")]
               if current["kind"] == "login" else [("Secret", "secret")])
    fields.append(("Notes", "notes"))

    def field_status(field):
        if field not in changes:
            return "Keep current"
        if not changes[field]:
            return "Clear on save"
        if field == "description":
            return changes[field]
        if field == "origins":
            return ", ".join(changes[field])
        return "Changed (hidden)"

    while True:
        rows = [(label, field_status(field)) for label, field in fields]
        rows += [("Save changes", "Apply all changes"),
                 ("Cancel", "Discard all changes")]
        try:
            chosen = _select_dialog(rows, title="Edit " + current["description"],
                                    full_screen=True,
                                    hint="↑↓ choose field · Enter edit · Esc cancel all")
        except Exception:
            for index, row in enumerate(rows, 1):
                print(f"  {index}. {row[0]} — {row[1]}")
            raw = _ask("Field number (empty cancels): ")
            chosen = (rows[int(raw) - 1] if raw and raw.isdigit()
                      and 1 <= int(raw) <= len(rows) else None)
        if chosen is None or chosen[0] == "Cancel":
            return "Edit cancelled — entry unchanged."
        if chosen[0] == "Save changes":
            if not changes:
                return "Nothing to change."
            if not _ensure_unlocked(vault):
                return "Edit cancelled — entry unchanged."
            if vault.update_entry(entry_id, **changes):
                return "Entry updated."
            return "Entry disappeared — not updated."
        field = dict(fields)[chosen[0]]
        if field == "description":
            value = _description_input()
        elif field == "origins":
            value = _origins_input(optional=current["kind"] == "secret")
        elif field == "password":
            value = _password_input("New password (hidden, empty cancels): ")
        elif field == "secret":
            value = _secret_lines("New secret (hidden; Ctrl+D saves, Esc cancels): ")
            if value == "":
                print("  Secret must not be empty; field unchanged.")
                continue
        else:
            value = _secret(f"New {field} (hidden; Enter clears, Esc keeps current): ")
        if value is not None:
            changes[field] = value


def _delete_flow(vault, entry_id=None) -> str | None:
    if not _ensure_unlocked(vault):
        return
    entry_id = entry_id or _choose_entry(vault, "Delete which entry?")
    if not entry_id:
        return
    if not _confirm(f"Delete {entry_id} permanently?"):
        print("  not deleted.")
        return "Not deleted."
    if not _ensure_unlocked(vault):
        return
    if vault.delete_entry(entry_id):
        print(f"  deleted {entry_id}.")
        return "Entry deleted."
    else:
        print("  entry disappeared — not deleted.")
        return "Entry disappeared — not deleted."


def _ensure_unlocked(vault) -> bool:
    return vault.is_unlocked() or _unlock_flow(vault)


def _unlock_flow(vault) -> bool:
    if vault.is_unlocked():
        print("  already unlocked.")
        return True
    while True:
        passphrase = _secret("Vault passphrase (hidden; Esc cancels): ")
        if passphrase is None:
            return False
        if vault.unlock(passphrase):
            print("  vault unlocked.")
            return True
        print("  unlock failed — wrong passphrase or tampered vault.")


def _passphrase_flow(vault) -> str | None:
    if not _ensure_unlocked(vault):
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
        return "Passphrases do not match — unchanged."
    if not _ensure_unlocked(vault):
        return
    vault.change_passphrase(first)
    print("  passphrase changed; vault re-encrypted with a fresh salt.")
    return "Passphrase changed."
