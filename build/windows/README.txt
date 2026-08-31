Laintas CLI for Windows
=======================

Requirements:
- 64-bit Windows 10 version 2004 or newer, or Windows 11
- WSL 2 enabled

Install:
1. Double-click laintas-cli_windows_amd64_setup.exe.
2. Open a new PowerShell or Command Prompt window.
3. Run: laintas-cli

The launcher uses the private WSL distribution named "Laintas-CLI". It does
not change the user's default WSL distribution and does not use wsl.exe during
normal CLI startup.

Uninstall from Windows Settings > Apps. The private Linux filesystem and
~/.laintas data are preserved so a reinstall or upgrade can reuse them.

To permanently remove the private Linux filesystem too:
  wsl.exe --unregister Laintas-CLI
