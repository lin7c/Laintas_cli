---
name: wsl-windows
description: Working on a Windows machine from inside the private WSL distribution the Windows build runs in.
version: 1.0.0
triggers:
  - windows
  - wsl
  - powershell
  - drive letter
  - C drive
  - install node
  - install python
  - dev server
  - hot reload
  - file watcher
  - npm install
  - path translation
  - explorer
  - clipboard
---

# Windows, Through WSL

On the Windows build this CLI is a Linux program in a private WSL 2
distribution named `Laintas-CLI`. Everything below follows from that one fact:
the shell is Linux, the machine is not.

Read this before installing a toolchain, starting a dev server, moving a
project between filesystems, or handing a path to a Windows program. For
anything else, ordinary Linux practice applies.

## Two filesystems, and which one a file is on

- `/home/laintas`, `/tmp`, `/opt` -- the distribution's own ext4. Fast, real
  inotify, real POSIX permissions. Nothing here is visible from Windows
  Explorer without going through `\\wsl$`, and the user does not think of it
  as "their disk".
- `/mnt/c/...`, `/mnt/d/...` -- the Windows disks, mounted as DrvFs. This is
  where the user's documents, repositories and Desktop actually are, and where
  the working directory usually starts (`%USERPROFILE%`).

Four consequences, all of which present as something else:

1. **DrvFs is slow.** Every file operation crosses a 9P/virtio boundary.
   `npm install`, a full-tree grep, `git status` on a large repository -- each
   is roughly an order of magnitude slower than the same work on `/home`. A
   build that "hangs" on Windows is usually this, not a deadlock.
2. **inotify does not fire on DrvFs.** Watch-mode builds, `--watch` test
   runners and dev-server hot reload silently never reload. The process looks
   healthy and simply does nothing when a file changes. Say so rather than
   debugging the tool; the fixes are polling (`CHOKIDAR_USEPOLLING=1`,
   `webpack watchOptions.poll`, `vite server.watch.usePolling`) or moving the
   project.
3. **This is the slow filesystem the recursion rule is about.** `shell-linux`
   says to bound a recursive command or know the scale first; `/mnt` is where
   ignoring that stops being slow and becomes a hang. A drive root here holds
   a whole machine's files, package stores included, and every entry costs a
   round trip. It has already killed a session: `fs.glob **/laintas-cli` over
   a drive root never returned.
4. **Permissions are synthesised.** The mount is `metadata,umask=022,
   fmask=011`, so `chmod` on `/mnt` mostly does not mean what it says and a
   git `core.filemode` diff there is noise.

**Do not move the user's project to `/home` on your own initiative.** It is
their file, in the place they can find it, backed up by whatever backs up
their Windows profile. Moving it hides it from Explorer and from their
Windows editor. Explain the trade-off and let them choose; a clone into
`/home` for a one-off heavy operation is a different, smaller thing and is
usually fine.

## The toolchain question

The distribution ships bash, git, curl, ssh, ripgrep, and the standard
coreutils. **No node, no python, no compilers, no package managers beyond
apt.**

`appendWindowsPath=true`, so the entire Windows PATH is visible here. That
means `python`, `node`, `npm` and `git` may resolve to a Windows `.exe` --
which is the single most expensive mistake available on this host:

- A Win32 process cannot open `/mnt/c/Users/me/proj`. It fails with a path
  error that names the path but not the reason.
- A Windows `node` writes `node_modules` with Windows path separators and
  CRLF, and binaries built by it will not run under Linux.
- `git.exe` and Linux `git` disagree about line endings and file modes in the
  same repository.

So, before running an interpreter or package manager: check what you actually
have. `command -v node` returning something under `/mnt/c/` is a Windows
binary, not a Linux one.

- To work **in Linux** (the default, and right for anything this CLI builds or
  tests): install into the distribution. `sudo apt-get install -y <pkg>` needs
  no password, persists across restarts, and is invisible to Windows and to
  the user's other WSL distributions. For node and python specifically, prefer
  the usual version managers (`nvm`, `uv`, `pyenv`) over Debian's aged
  packages -- state which you are installing before you install it.
- To deliberately drive a **Windows** program: translate the path first.
  `wslpath -w /mnt/c/Users/me/proj` gives `C:\Users\me\proj`; `wslpath -u`
  goes the other way. Every Windows binary invocation needs the user's
  approval, so batch the work into one call rather than a sequence of prompts.

## Interop worth knowing

- `explorer.exe .` opens the current directory in Windows Explorer. This is
  the fastest way to hand the user something they can see.
- `clip.exe` puts stdin on the Windows clipboard; `powershell.exe Get-Clipboard`
  reads it back (and needs approval).
- A server bound to `0.0.0.0` (not `127.0.0.1`) inside the distribution is
  reachable from the Windows browser at `localhost:<port>`. Binding to
  loopback only is the usual reason "the dev server started but the page will
  not load".
- Opening a URL in the user's browser is already handled by the runtime. Do
  not shell out to PowerShell to do it.
- `systemd` is off in this distribution. `systemctl` will not work; run
  services in the foreground or with `nohup`, and never write a unit file
  expecting it to start.

## What will ask for approval, and why not to route around it

By design, and not a malfunction to retry differently:

- `powershell.exe`, `pwsh`, `cmd.exe /c`, `wsl.exe`, and the Windows
  management binaries (`sc`, `net`, `netsh`, `schtasks`, `reg`, `winget`,
  `msiexec`, ...).
- Writes to paths outside the working directory, which on this host includes
  most of `/mnt` -- those are the user's real documents.
- `format`, `diskpart`, `bcdedit`, `vssadmin delete shadows`,
  `wsl --unregister` and disabling Defender are refused outright, in any mode.

Wrapping a blocked command in an encoded PowerShell payload, a `cmd /c`, or a
different quoting does not change the answer -- the payload is unwrapped
before the decision -- and attempting it is a much worse outcome for the user
than asking them.

## The distribution is private

`Laintas-CLI` is this product's own distribution. Installing packages in it
does not touch the user's Ubuntu, and does not change their default
distribution. That is what makes `apt-get install` a cheap answer here: the
blast radius is one disposable rootfs that the installer can rebuild.
