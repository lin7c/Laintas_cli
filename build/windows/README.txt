Laintas CLI for Windows
=======================

Requirements:
- 64-bit Windows 10 version 2004 or newer, or Windows 11
- WSL 2 enabled

Install:
1. Double-click install.cmd.
2. Open a new PowerShell or Command Prompt window.
3. Run: laintas-cli

The launcher uses the private WSL distribution named "Laintas-CLI". It does
not change the user's default WSL distribution and does not use wsl.exe during
normal CLI startup.

Uninstall the launcher but preserve Linux data:
  powershell -ExecutionPolicy Bypass -File uninstall.ps1

Permanently remove the launcher and the private Linux filesystem:
  powershell -ExecutionPolicy Bypass -File uninstall.ps1 -DeleteLinuxData
