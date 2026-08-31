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
InstallDir "$LOCALAPPDATA\Laintas\app"
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
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Section "Install"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\payload"
  File "${PAYLOAD_DIR}\laintas-cli.exe"
  File "${PAYLOAD_DIR}\laintas-cli-linux"
  File "${PAYLOAD_DIR}\laintas-rootfs.tar.gz"
  File "${PAYLOAD_DIR}\install.ps1"

  DetailPrint "Installing the private Laintas CLI runtime..."
  nsExec::ExecToLog 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\payload\install.ps1" -InstallRoot "$LOCALAPPDATA\Laintas"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "Laintas CLI installation failed (exit code $0). WSL 2 must be enabled before installation."
    SetErrorLevel $0
    Quit
  ${EndIf}

  SetOutPath "$INSTDIR"
  File "${PAYLOAD_DIR}\uninstall.ps1"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKCU "Software\${APP_SHORT}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REG_UNINSTALL}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "URLInfoAbout" "${APP_URL}"
  WriteRegStr HKCU "${REG_UNINSTALL}" "DisplayIcon" "$LOCALAPPDATA\Laintas\bin\laintas-cli.exe"
  WriteRegStr HKCU "${REG_UNINSTALL}" "UninstallString" '$\"$INSTDIR\uninstall.exe$\"'
  WriteRegDWORD HKCU "${REG_UNINSTALL}" "NoModify" 1
  WriteRegDWORD HKCU "${REG_UNINSTALL}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  nsExec::ExecToLog 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\uninstall.ps1" -InstallRoot "$LOCALAPPDATA\Laintas"'
  Delete "$INSTDIR\uninstall.ps1"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${REG_UNINSTALL}"
  DeleteRegKey HKCU "Software\${APP_SHORT}"
SectionEnd
