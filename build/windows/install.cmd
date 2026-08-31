@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "LAINTAS_EXIT=%ERRORLEVEL%"
if not "%LAINTAS_EXIT%"=="0" pause
exit /b %LAINTAS_EXIT%
