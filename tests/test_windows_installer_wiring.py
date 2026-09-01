from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nsis_installer_lets_user_choose_root_and_launch_after_install():
    script = (ROOT / "build/windows/installer.nsi").read_text(encoding="utf-8")

    assert 'InstallDir "$LOCALAPPDATA\\Laintas"' in script
    assert "!insertmacro MUI_PAGE_DIRECTORY" in script
    assert '!define MUI_FINISHPAGE_RUN "$INSTDIR\\bin\\laintas-cli.exe"' in script
    assert '-InstallRoot "$INSTDIR"' in script
    assert 'InstallDir "$LOCALAPPDATA\\Laintas\\app"' not in script
    assert 'StrCpy $INSTDIR "$LOCALAPPDATA\\Laintas"' in script


def test_powershell_bootstrap_opens_interactive_installer():
    script = (ROOT / "laintas_cli_download/public/install.ps1").read_text(
        encoding="utf-8"
    )

    assert "Start-Process -FilePath $Installer -Wait -PassThru" in script
    assert 'ArgumentList "/S"' not in script


def test_upgrade_rejects_accidental_private_distro_relocation():
    script = (ROOT / "build/windows/install.ps1").read_text(encoding="utf-8")

    assert "Get-DistributionBasePath" in script
    assert "Keep its existing installation directory" in script


def test_installer_and_launcher_carry_the_laintas_mark():
    """An unbranded installer is one a user cannot tell from a fake one.

    The icon has three separate places to be wired and each was empty at some
    point: the installer UI, the uninstaller, and the launcher's own PE
    resources. Losing any one of them shows the generic Windows icon in a
    place the user actually looks.
    """
    script = (ROOT / "build/windows/installer.nsi").read_text(encoding="utf-8")
    assert '!define MUI_ICON   "icon.ico"' in script
    assert '!define MUI_UNICON "icon.ico"' in script

    build = (ROOT / "build/windows/build_windows_package.sh").read_text(
        encoding="utf-8"
    )
    assert "x86_64-w64-mingw32-windres" in build
    assert "launcher.res.o" in build

    resources = (ROOT / "build/windows/launcher.rc").read_text(encoding="utf-8")
    assert '1 ICON "icon.ico"' in resources


def test_committed_icon_is_a_real_multi_size_ico():
    """The .ico is committed because CI has no SVG rasteriser.

    That makes it the one build input nothing regenerates, so a truncated or
    single-size file would ship as-is and only show up as a blurry 16px icon
    on someone's taskbar.
    """
    icon = (ROOT / "build/windows/icon.ico").read_bytes()
    assert icon[:4] == b"\x00\x00\x01\x00", "not an ICO header"

    count = int.from_bytes(icon[4:6], "little")
    assert count >= 5, f"only {count} sizes; Explorer picks per-DPI"

    sizes = set()
    for index in range(count):
        entry = 6 + 16 * index
        # 0 in the byte-wide field means 256.
        sizes.add(icon[entry] or 256)
        length = int.from_bytes(icon[entry + 8:entry + 12], "little")
        offset = int.from_bytes(icon[entry + 12:entry + 16], "little")
        assert offset + length <= len(icon), "icon entry runs past end of file"
    assert {16, 32, 48, 256} <= sizes, f"missing common sizes: {sorted(sizes)}"

    assert (ROOT / "build/windows/icon.svg").is_file(), "icon source is missing"


def test_one_click_installers_download_from_the_live_release_channel():
    """The bootstrap scripts must point at the channel that actually serves.

    Both fetched from `cli.laintas.com/releases/latest/`, the retired
    self-hosted channel whose `latest/` pointer does not exist — every
    `curl … | bash` and `irm … | iex` ended at a 404. The site still serves
    the scripts themselves, so only the package URL moved.
    """
    RELEASES = "https://github.com/lin7c/Laintas_cli/releases/latest/download"

    for path in ("laintas_cli_download/public/install.sh",
                 "laintas_cli_download/public/install.ps1"):
        script = (ROOT / path).read_text(encoding="utf-8")
        assert RELEASES in script, f"{path} does not use the release channel"
        assert "cli.laintas.com/releases/" not in script, (
            f"{path} still fetches packages from the retired channel")

    powershell = (ROOT / "laintas_cli_download/public/install.ps1").read_text(
        encoding="utf-8"
    )
    # The checksum is the only thing standing between a mirror and an
    # executable the user is about to run as themselves.
    assert "SHA256SUMS.txt" in powershell
    assert "Checksum mismatch" in powershell


def test_launcher_fixes_the_console_it_is_handed():
    """The legacy console breaks a TUI in three separate ways.

    A shortcut on Windows 10 opens conhost, where QuickEdit eats every mouse
    event before the application sees it, escape sequences arrive as literal
    text because virtual-terminal processing is off, and the default raster
    font has no box-drawing or CJK glyphs. None of it is fixable from the
    Linux side, and all of it is why the terminal was unusable.
    """
    launcher = (ROOT / "build/windows/launcher.cpp").read_text(encoding="utf-8")

    # QuickEdit is the mouse. Clearing its bit does nothing unless
    # ENABLE_EXTENDED_FLAGS is set in the same call.
    assert "~static_cast<DWORD>(ENABLE_QUICK_EDIT_MODE)" in launcher
    assert "ENABLE_EXTENDED_FLAGS" in launcher
    assert "ENABLE_MOUSE_INPUT" in launcher

    assert "ENABLE_VIRTUAL_TERMINAL_PROCESSING" in launcher
    assert "ENABLE_VIRTUAL_TERMINAL_INPUT" in launcher
    assert "SetConsoleOutputCP(CP_UTF8)" in launcher
    assert "SetCurrentConsoleFontEx" in launcher

    # The raster font is told apart by the TMPF_TRUETYPE bit and nothing else.
    # conhost reports the legacy font as face "Terminal", family 48 — not as
    # "family 0 with an empty name" — so a guard phrased that way returned
    # early on the exact console it was supposed to repair.
    assert "TMPF_TRUETYPE" in launcher
    assert "font.FontFamily != 0" not in launcher

    # LAINTAS_HOST is how the Linux side learns it is the Windows product and
    # turns the mouse on. The distribution name cannot carry that: it is
    # user-settable through LAINTAS_WSL_DISTRO.
    assert "LAINTAS_HOST=windows" in launcher

    # Whatever was there before is put back: this process does not own the
    # window it was started in.
    assert "RestoreConsole" in launcher

    # WslLaunchInteractive hands over the Windows environment, which has no
    # TERM. An application that finds none assumes it may not draw at all.
    assert "TERM=" in launcher
    assert "xterm-256color" in launcher


def test_windows_terminal_profile_is_installable_and_consistent():
    """`wt -p <name>` fails unless every copy of the name agrees.

    The name is written in three places — the fragment that defines the
    profile, the launcher that selects it, and the shortcut the installer
    creates — and a mismatch shows up only as a Windows Terminal error at
    the moment a user clicks the shortcut.
    """
    import json

    raw = (ROOT / "build/windows/terminal-fragment.json").read_text(
        encoding="utf-8")
    fragment = json.loads(raw)

    profile = fragment["profiles"][0]
    assert profile["name"] == "Laintas CLI"
    assert profile["colorScheme"] == fragment["schemes"][0]["name"]
    # A fixed GUID: a generated one would add a second profile on every
    # reinstall instead of updating this one.
    assert profile["guid"].startswith("{") and profile["guid"].endswith("}")
    assert "__LAUNCHER__" in raw and "__ICON__" in raw

    # `commandline` is a command line, so a path with a space in it has to
    # arrive quoted. The default install path contains the Windows account
    # name, which makes an account like "Zhang San" enough to break the
    # profile on its first launch. `icon` is a plain path and must not be
    # quoted or Windows Terminal cannot find the file.
    launcher_path = r"C:\Users\Zhang San\AppData\Local\Laintas\bin\laintas-cli.exe"
    icon_path = r"C:\Users\Zhang San\AppData\Local\Laintas\bin\laintas-cli.ico"
    # Exactly what install.ps1 does to the bundled template.
    substituted = raw.replace("__LAUNCHER__", launcher_path.replace("\\", "\\\\"))
    substituted = substituted.replace("__ICON__", icon_path.replace("\\", "\\\\"))
    installed = json.loads(substituted)["profiles"][0]
    assert installed["commandline"] == f'"{launcher_path}"'
    assert installed["icon"] == icon_path

    launcher = (ROOT / "build/windows/launcher.cpp").read_text(encoding="utf-8")
    assert 'kTerminalProfile[] = L"Laintas CLI"' in launcher

    install = (ROOT / "build/windows/install.ps1").read_text(encoding="utf-8")
    assert '-p "Laintas CLI"' in install
    assert "Fragments\\Laintas.LaintasCLI" in install
    # JSON string values: unescaped backslashes make the fragment unparseable
    # and the profile simply never appears.
    assert '.Replace("\\", "\\\\")' in install
    # A BOM makes Windows Terminal reject the fragment outright.
    assert "UTF8Encoding $false" in install

    uninstall = (ROOT / "build/windows/uninstall.ps1").read_text(encoding="utf-8")
    assert "Fragments\\Laintas.LaintasCLI" in uninstall


def test_a_terminal_is_bundled_because_conhost_cannot_deliver_a_click():
    """Windows 10 ships no terminal this CLI can be used in.

    conhost reports mouse input only as MOUSE_EVENT INPUT_RECORDs and never
    as VT sequences, which Microsoft states is deliberate
    (microsoft/terminal#15296). A WSL process reads bytes from a pty and can
    only ever see VT sequences, so no console mode, flag or font makes a
    click reach the CLI there. The bundled Terminal is the fix; these are the
    parts of it that fail silently if they drift.
    """
    import json

    build = (ROOT / "build/windows/build_windows_package.sh").read_text(
        encoding="utf-8")
    # Pinned by version *and* hash: an unpinned download makes the installer's
    # contents depend on the day CI ran, and the hash is what makes running
    # someone else's binary defensible at all.
    assert "WT_VERSION=" in build and "WT_SHA256=" in build
    assert "sha256sum -c" in build
    assert "package/terminal" in build
    # The marker is a dotfile and upload-artifact drops hidden files, so it
    # cannot be shipped in the payload — it is created at the destination.
    assert ': > "$WORK_DIR/package/terminal/.portable"' not in build

    script = (ROOT / "build/windows/installer.nsi").read_text(encoding="utf-8")
    assert 'File "${PAYLOAD_DIR}\\terminal-settings.json"' in script
    # NSIS must not ask for the dotfile it will never receive.
    assert '.portable' not in script
    assert 'File /r "${PAYLOAD_DIR}\\terminal\\*.*"' in script
    assert 'RMDir /r "$INSTDIR\\terminal"' in script

    launcher = (ROOT / "build/windows/launcher.cpp").read_text(encoding="utf-8")
    # The user's own Terminal wins; the bundled copy is the fallback. Compared
    # inside the selection function, since the bundled path is also named at
    # the top of the file where the constants live.
    start = launcher.index("bool RelaunchInBetterTerminal()")
    chooser = launcher[start:launcher.index("\n}", start)]
    assert "wt.exe" in chooser and "BundledTerminalPath" in chooser
    assert chooser.index("wt.exe") < chooser.index("BundledTerminalPath")
    # Unpackaged Terminal does not carry the VC++ runtime, and starting it
    # without one fails with no window and no message.
    assert "vcruntime140_1.dll" in launcher

    raw = (ROOT / "build/windows/terminal-settings.json").read_text(
        encoding="utf-8")
    settings = json.loads(raw)
    profile = settings["profiles"]["list"][0]
    # It launches with no arguments, so our profile has to be the default.
    assert settings["defaultProfile"] == profile["guid"]
    assert profile["name"] == "Laintas CLI"
    assert profile["colorScheme"] == settings["schemes"][0]["name"]
    # Same quoting rule as the fragment: commandline is a command line.
    assert profile["commandline"] == '"__LAUNCHER__"'
    assert profile["icon"] == "__ICON__"

    install = (ROOT / "build/windows/install.ps1").read_text(encoding="utf-8")
    assert "terminal-settings.json" in install
    # Portable mode exists only because install.ps1 creates this marker; the
    # bundled copy silently edits the user's own Terminal settings without it.
    assert '.portable' in install
    assert "New-Item -ItemType File -Path $portableMarker" in install
    uninstall = (ROOT / "build/windows/uninstall.ps1").read_text(
        encoding="utf-8")
    assert 'Join-Path $InstallRoot "terminal"' in uninstall


def test_no_powershell_wildcard_is_passed_to_literalpath():
    """-LiteralPath does not expand wildcards; it looks for that exact name.

    `Copy-Item -LiteralPath (Join-Path $dir "*")` asked for a file actually
    named "*", threw, and left an empty directory behind — which is what
    v1.23.6 installed instead of a terminal. The mistake reads as correct,
    and its only symptom was a line in a scrolling installer log.
    """
    import re

    for name in ("install.ps1", "uninstall.ps1"):
        script = (ROOT / "build/windows" / name).read_text(encoding="utf-8")
        for number, line in enumerate(script.splitlines(), 1):
            if "-LiteralPath" not in line:
                continue
            after = line.split("-LiteralPath", 1)[1]
            # Only the argument, not a later flag or a trailing comment.
            argument = re.split(r"\s+-\w|#", after, maxsplit=1)[0]
            assert "*" not in argument and "?" not in argument, (
                f"{name}:{number} passes a wildcard to -LiteralPath: {line.strip()}")


def test_the_installer_checks_the_terminal_actually_landed():
    """A copy that fails must not leave something that looks installed."""
    install = (ROOT / "build/windows/install.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $TerminalDir "WindowsTerminal.exe"' in install
    # An empty directory reads as a working install to anyone checking, the
    # launcher included, so a failure has to clean up after itself.
    assert "Remove-Item -LiteralPath $TerminalDir -Recurse -Force -ErrorAction SilentlyContinue" in install
    assert "Write-Warning" in install


def test_the_bundled_and_installed_profiles_agree():
    """Two files now define the same profile; `wt -p` matches on the name."""
    import json

    fragment = json.loads((ROOT / "build/windows/terminal-fragment.json")
                          .read_text(encoding="utf-8"))
    settings = json.loads((ROOT / "build/windows/terminal-settings.json")
                          .read_text(encoding="utf-8"))
    installed = fragment["profiles"][0]
    bundled = settings["profiles"]["list"][0]
    assert installed["name"] == bundled["name"]
    assert installed["guid"] == bundled["guid"]
    assert installed["commandline"] == bundled["commandline"]
    assert fragment["schemes"][0] == settings["schemes"][0]


def test_the_installer_ships_what_the_profile_points_at():
    """A profile that names a missing icon is a broken profile."""
    build = (ROOT / "build/windows/build_windows_package.sh").read_text(
        encoding="utf-8")
    assert "package/icon.ico" in build
    assert "package/terminal-fragment.json" in build

    script = (ROOT / "build/windows/installer.nsi").read_text(encoding="utf-8")
    assert 'File "${PAYLOAD_DIR}\\icon.ico"' in script
    assert 'File "${PAYLOAD_DIR}\\terminal-fragment.json"' in script
