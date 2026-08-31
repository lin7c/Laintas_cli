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
