; Laintas CLI Windows installer. The public artifact is this single EXE.
; Its private payload is unpacked only while installing; normal CLI launches
; use the installed launcher and private WSL distribution directly.

Unicode true
ManifestDPIAware true

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "0.0.0"
!endif
!ifndef PAYLOAD_DIR
  !define PAYLOAD_DIR "payload"
!endif

!define APP_NAME "Laintas CLI"
!define APP_SHORT "LaintasCLI"
!define APP_PUBLISHER "Laintas"
!define APP_URL "https://cli.laintas.com"
!define REG_UNINSTALL "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_SHORT}"

Name "${APP_NAME}"
OutFile "..\..\laintas-cli_windows_amd64_setup.exe"
; The selected directory is the root for the launcher, private WSL virtual
; disk, and uninstaller. InstallDirRegKey makes upgrades reopen at the path
; the user selected previously.
InstallDir "$LOCALAPPDATA\Laintas"
InstallDirRegKey HKCU "Software\${APP_SHORT}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUnInstDetails show

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "FileDescription" "${APP_NAME} installer"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"

!define MUI_ABORTWARNING
; The installer, the uninstaller and the Add/Remove entry all carry the same
; mark the download site does. build/windows/icon.svg is the source and
; icon.ico is generated from it by build/windows/build_icon.py; the launcher
; gets the same file through launcher.rc. Without this NSIS ships its own
; default icon, which tells a user nothing about who made the program they
; are about to run.
!define MUI_ICON   "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\bin\laintas-cli.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start Laintas CLI now"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${PAYLOAD_DIR}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  ; v1.23.0 stored the NSIS app directory instead of the installation root.
  ; Migrate that one released layout so its upgrade keeps the existing
  ; Laintas-CLI distro at $LOCALAPPDATA\Laintas\WSL.
  ReadRegStr $0 HKCU "Software\${APP_SHORT}" "InstallDir"
  ${If} $0 == "$LOCALAPPDATA\Laintas\app"
    StrCpy $INSTDIR "$LOCALAPPDATA\Laintas"
  ${EndIf}
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Section "Install"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\payload"
  File "${PAYLOAD_DIR}\laintas-cli.exe"
  File "${PAYLOAD_DIR}\laintas-cli-linux"
  File "${PAYLOAD_DIR}\laintas-rootfs.tar.gz"
  File "${PAYLOAD_DIR}\install.ps1"
  File "${PAYLOAD_DIR}\icon.ico"
  File "${PAYLOAD_DIR}\terminal-fragment.json"
  File "${PAYLOAD_DIR}\terminal-settings.json"
  ; The bundled Windows Terminal. The portable-mode marker is not shipped
  ; with it: it is a dotfile, and upload-artifact drops hidden files, so a
  ; payload that carried one here would arrive without it and fail this
  ; build. install.ps1 creates the marker at the destination instead.
  SetOutPath "$PLUGINSDIR\payload\terminal"
  File /r "${PAYLOAD_DIR}\terminal\*.*"
  SetOutPath "$PLUGINSDIR\payload"

  DetailPrint "Installing the private Laintas CLI runtime..."
  nsExec::ExecToLog 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\payload\install.ps1" -InstallRoot "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "Laintas CLI installation failed (exit code $0). WSL 2 must be enabled before installation."
    SetErrorLevel $0
    Quit
  ${EndIf}

  SetOutPath "$INSTDIR"
  File "${PAYLOAD_DIR}\uninstall.ps1"
  File /oname=LICENSE "${PAYLOAD_DIR}\LICENSE"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  Delete "$INSTDIR\app\uninstall.ps1"
  Delete "$INSTDIR\app\uninstall.exe"
  RMDir "$INSTDIR\app"

  WriteRegStr HKCU "Software\${APP_SHORT}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REG_UNINSTALL}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "URLInfoAbout" "${APP_URL}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "DisplayIcon" "$INSTDIR\bin\laintas-cli.exe"
  WriteRegStr HKCU "${REG_UNINSTALL}" "UninstallString" '$\"$INSTDIR\uninstall.exe$\"'
  WriteRegDWORD HKCU "${REG_UNINSTALL}" "NoModify" 1
  WriteRegDWORD HKCU "${REG_UNINSTALL}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  nsExec::ExecToLog 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\uninstall.ps1" -InstallRoot "$INSTDIR"'
  ; The bundled terminal is ours, not the user's data: it goes with us.
  RMDir /r "$INSTDIR\terminal"
  RMDir "$INSTDIR\bin"
  Delete "$INSTDIR\uninstall.ps1"
  Delete "$INSTDIR\uninstall.exe"
  Delete "$INSTDIR\LICENSE"
  ; Keep $INSTDIR itself because WSL\ext4.vhdx and the user's ~/.laintas
  ; data intentionally survive a normal uninstall.
  DeleteRegKey HKCU "${REG_UNINSTALL}"
  DeleteRegKey HKCU "Software\${APP_SHORT}"
SectionEnd
