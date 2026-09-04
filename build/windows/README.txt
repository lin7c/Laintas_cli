Laintas CLI for Windows
=======================

Requirements:
- 64-bit Windows 10 version 2004 or newer, or Windows 11
- WSL 2 enabled

Install:
1. Double-click laintas-cli_windows_amd64_setup.exe.
2. Keep the default directory or choose another local NTFS drive.
3. On the final page, choose whether to start Laintas CLI immediately.

The selected directory contains bin, the private WSL virtual disk, and the
uninstaller. Upgrades remember and reuse the same directory.

The launcher uses the private WSL distribution named "Laintas-CLI". It does
not change the user's default WSL distribution and does not use wsl.exe during
normal CLI startup.

Terminal
--------
The CLI opens in Windows Terminal. If one is already installed it is used,
through a profile named "Laintas CLI"; if not, the copy bundled in the
installation directory is used instead, in portable mode, so it never reads
or writes your own Windows Terminal settings.

The bundled copy exists because the legacy console (conhost, what cmd.exe
opens) reports mouse input only as Windows console events and never as the
escape sequences a Linux program can read. A WSL application running there
cannot receive a mouse click at all, whatever it does, and Windows 10 ships
no other terminal. Set LAINTAS_NO_WT=1 to stay in whatever console started
the launcher.

Selecting text
--------------
The CLI turns on mouse reporting so the status slots on the prompt can be
clicked. While that is on, the terminal hands clicks to the CLI instead of
selecting text: hold Shift and drag to select as usual. This is how mouse
reporting works in every terminal, not a Windows limitation. Run
"/config enable_mouse false" inside the CLI to turn clicking off and get
plain drag-to-select back.

If the installation fails
-------------------------
The message box names the real reason and points at the full log:

  %TEMP%\laintas-cli-install.log

The three causes seen most often, and what each one needs:

- "The Windows Subsystem for Linux is not enabled" - open PowerShell as
  Administrator, run "wsl --install --no-distribution", restart Windows, then
  run this installer again.
- "The WSL 2 kernel is missing or out of date" - run "wsl --update" as
  Administrator and try again.
- "Hardware virtualisation is off" - turn on Intel VT-x / AMD-V in the
  BIOS/UEFI and enable the "Virtual Machine Platform" Windows feature.

If the message says the installation directory already contains files while no
Laintas-CLI distribution is registered, a previous "wsl --unregister" left the
folder behind: delete <install directory>\WSL, or install elsewhere.

Deleting ext4.vhdx (the multi-GB file under <install directory>\WSL) to free
disk space does not uninstall anything: Windows keeps the distribution
registered, and everything that runs inside it then fails. The installer now
detects this and rebuilds the distribution. To clear it by hand:
  wsl.exe --unregister Laintas-CLI
and then install again. Note that the file is the whole Linux filesystem,
including ~/.laintas -- deleting it discards that data.

Uninstall from Windows Settings > Apps. The private Linux filesystem and
~/.laintas data are preserved so a reinstall or upgrade can reuse them.

To permanently remove the private Linux filesystem too:
  wsl.exe --unregister Laintas-CLI
