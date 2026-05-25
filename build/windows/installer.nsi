; Laintas CLI Windows Installer — self-contained NSIS script
; Build: makensis build/windows/installer.nsi
;
; Prerequisites:
;   1. Run PyInstaller:  pyinstaller build/windows/laintas_cli.spec
;   2. Copy dist/laintas_cli.exe → build/windows/
;   3. Run:  makensis build/windows/installer.nsi

Unicode true
!include "MUI2.nsh"
!include "StrFunc.nsh"
${StrRep}

!define PRODUCT_NAME "Laintas CLI"
!define PRODUCT_VERSION "0.1.0"
!define PRODUCT_PUBLISHER "Laintas"
!define PRODUCT_WEB_SITE "https://github.com/lin7c/laintas_cli_pre"
!define PRODUCT_EXE "laintas_cli.exe"
!define REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "laintas_cli_setup.exe"
InstallDir "$PROGRAMFILES64\Laintas CLI"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

;──────────────────────────────────────────────────────────────────────────
; Modern UI pages
;──────────────────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "license.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

;──────────────────────────────────────────────────────────────────────────
; Helper: Add to PATH (machine-wide)
;──────────────────────────────────────────────────────────────────────────
Function AddToPath
    ; Read current machine PATH
    ReadRegStr $0 HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "Path"
    ; Check if already present
    ${If} $0 != ""
        StrCpy $1 "$0"  ; save original
        StrLen $2 "$INSTDIR"
        StrCpy $3 0
        ; Scan for existing entry
loop:
        IntOp $3 $3 + 1
        StrCpy $4 $0 1
        ${If} $4 == ""
            Goto notfound
        ${EndIf}
        StrCpy $0 $0 ${NSIS_MAX_STRLEN} 1
        Goto loop
notfound:
        ; Append
        WriteRegExpandStr HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" \
                               "Path" "$1;$INSTDIR"
    ${Else}
        WriteRegExpandStr HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" \
                               "Path" "$INSTDIR"
    ${EndIf}
    ; Broadcast WM_SETTINGCHANGE
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=1000
FunctionEnd

Function un.RemoveFromPath
    ; Read current PATH
    ReadRegStr $0 HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "Path"
    ${If} $0 != ""
        ; Remove $INSTDIR from PATH
        ${StrRep} $1 "$0" "$INSTDIR;" ""
        ${StrRep} $2 "$1" "$INSTDIR" ""
        WriteRegExpandStr HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" \
                               "Path" "$2"
        SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=1000
    ${EndIf}
FunctionEnd

;──────────────────────────────────────────────────────────────────────────
; Sections
;──────────────────────────────────────────────────────────────────────────
Section "Laintas CLI (required)" SecCore
    SectionIn RO
    SetOutPath "$INSTDIR"
    File "${PRODUCT_EXE}"
    WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Add to PATH" SecPath
    Call AddToPath
SectionEnd

Section "Desktop shortcut" SecShortcut
    CreateShortCut "$DESKTOP\Laintas CLI.lnk" "$INSTDIR\${PRODUCT_EXE}"
SectionEnd

Section "Start Menu folder" SecStartMenu
    CreateDirectory "$SMPROGRAMS\Laintas CLI"
    CreateShortCut "$SMPROGRAMS\Laintas CLI\Laintas CLI.lnk" "$INSTDIR\${PRODUCT_EXE}"
    CreateShortCut "$SMPROGRAMS\Laintas CLI\Uninstall.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

;──────────────────────────────────────────────────────────────────────────
; Component descriptions
;──────────────────────────────────────────────────────────────────────────
LangString DESC_SecCore     ${LANG_ENGLISH} "Core Laintas CLI program (required)."
LangString DESC_SecPath     ${LANG_ENGLISH} "Add laintas_cli to the system PATH so you can run it from any terminal."
LangString DESC_SecShortcut ${LANG_ENGLISH} "Create a shortcut on the desktop."
LangString DESC_SecStartMenu ${LANG_ENGLISH} "Create a Start Menu folder with shortcuts."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecCore}     $(DESC_SecCore)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecPath}     $(DESC_SecPath)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecShortcut} $(DESC_SecShortcut)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecStartMenu} $(DESC_SecStartMenu)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

;──────────────────────────────────────────────────────────────────────────
; Defaults
;──────────────────────────────────────────────────────────────────────────
Function .onInit
    SectionSetFlags ${SecPath} ${SF_SELECTED}
FunctionEnd

;──────────────────────────────────────────────────────────────────────────
; Post-install: create workspace
;──────────────────────────────────────────────────────────────────────────
Section -PostInstall
    SetShellVarContext current
    CreateDirectory "$PROFILE\laintas_workspace"
    FileOpen $0 "$PROFILE\laintas_workspace\README.txt" w
    FileWrite $0 "Laintas CLI Workspace$\r$\n"
    FileWrite $0 "──────────────────────────────────────────$\r$\n"
    FileWrite $0 "Run 'laintas_cli' in any terminal to start.$\r$\n"
    FileClose $0
SectionEnd

;──────────────────────────────────────────────────────────────────────────
; Registry — Add/Remove Programs
;──────────────────────────────────────────────────────────────────────────
Section -Registry
    WriteRegStr HKLM "${REG_KEY}" "DisplayName" "${PRODUCT_NAME}"
    WriteRegStr HKLM "${REG_KEY}" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "${REG_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
    WriteRegStr HKLM "${REG_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
    WriteRegStr HKLM "${REG_KEY}" "URLInfoAbout" "${PRODUCT_WEB_SITE}"
    WriteRegStr HKLM "${REG_KEY}" "NoModify" 1
    WriteRegStr HKLM "${REG_KEY}" "NoRepair" 1
SectionEnd

;──────────────────────────────────────────────────────────────────────────
; Uninstall
;──────────────────────────────────────────────────────────────────────────
Section "Uninstall"
    Call un.RemoveFromPath

    Delete "$INSTDIR\${PRODUCT_EXE}"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    Delete "$DESKTOP\Laintas CLI.lnk"
    RMDir /r "$SMPROGRAMS\Laintas CLI"

    DeleteRegKey HKLM "${REG_KEY}"
SectionEnd
