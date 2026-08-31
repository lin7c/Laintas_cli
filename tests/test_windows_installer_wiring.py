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
