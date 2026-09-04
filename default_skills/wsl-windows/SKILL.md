---
name: wsl-windows
description: Working on a Windows machine from inside the private WSL distribution the Windows build runs in.
version: 1.2.0
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
  - wechat
  - qq
  - desktop app
  - click a button
  - screenshot
  - automate an application
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

## What is approval-gated

Approval is a runtime mechanism. When one of these is called, the runtime
stops the call and puts the decision to the user; it does not need you to
introduce it, and it cannot act on a sentence in your reply. Make the call.
Announcing the command you are about to run and waiting for agreement leaves
the user with nothing to approve, and the work not done. Batch what belongs
together into one call -- for the prompt count, not because a prompt is a
failure.

The gated set, by design and not a malfunction to retry differently:

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

## Two shells in one command line

A PowerShell payload written on a bash command line is a bash string first,
and bash performs its expansions before PowerShell is started. Anything of
the form `$name` in double quotes or unquoted belongs to bash: `$_`, `$env:X`,
`$null` and the rest of PowerShell's automatic variables are substituted away,
and what reaches PowerShell is a different script that is still syntactically
valid. The result is not an error but wrong output, usually blamed on the
cmdlet.

Single-quote the payload -- `powershell.exe -Command '...'` -- and bash passes
it through untouched. Where the payload itself needs single quotes, write it
to a `.ps1` file and run that with `-File`. The runtime rejects the ambiguous
form rather than running it, and names the variable it found.

## The distribution is private

`Laintas-CLI` is this product's own distribution. Installing packages in it
does not touch the user's Ubuntu, and does not change their default
distribution. That is what makes `apt-get install` a cheap answer here: the
blast radius is one disposable rootfs that the installer can rebuild.


## Driving the Windows machine itself

Everything above is about reaching Windows *files and programs* from this
Linux side. Reaching its **running applications** -- clicking a button in a
desktop app, reading what is on screen -- is a different mechanism entirely,
and it only exists when `helpwo-kernel.exe` is running on the Windows side
with the machine tiers switched on.

You can tell without asking: if the `win.*` tools are in your tool list, a
kernel is connected and the tier is on. **If they are not there, the
capability is off** -- say so and tell the user to start the kernel with
`--allow-machine-read` (to look) or `--allow-machine-write` (to act). Do not
try to substitute a PowerShell script that drives the UI; that is the thing
those switches exist to gate.

### Always try `win.snapshot` before `win.screenshot`

They are not two ways to do the same thing.

- `win.snapshot` reads the accessibility tree and hands back **named,
  addressable controls**. Acting on one with `win.invoke` calls the control's
  own method: no pointer moves, no focus is taken, and the user can keep
  typing in another window while it happens. It costs one cheap round trip.
- `win.screenshot` gives you a picture, which you then have to spend an
  `image.describe` or `image.to_text` call to read, and acting on it means
  `win.click` at coordinates -- the real mouse, taken away from whoever is
  sitting at the machine. **Every look is a separate model call and a
  separate charge.** A twenty-step task done this way is twenty of them.

So: snapshot, invoke, set_value. Fall to the pixel path only when snapshot
tells you to.

### When snapshot comes back empty

A result with `opaque: true` means the window draws its own interface -- its
buttons and text are pixels in video memory with no controls behind them, so
there is nothing for the tree to report. This is normal for some
applications, most visibly WeChat 4.x and QQ NT. It is **not** a failure to
retry, and taking another snapshot will return the same thing.

Two honest responses, in order:

1. Fall back to `win.screenshot` plus `win.click`, and tell the user you are
   doing it and why -- it is slower and costs more per step, and they should
   get to decide whether the task is worth that.
2. If the task is really "send a message" or "read my chats", say plainly
   that driving a messaging client by automation carries a real risk to the
   user's account, and let them choose. Never reach for a hooking library or
   a protocol client to get around it.

### The pointer is shared

`win.click`, `win.type` and `win.key` use the one real mouse and keyboard. If
the user is at the machine, they will see it happen and their own typing will
land in the wrong place. Prefer the tree; when you cannot, keep the run short
and say what you are about to do.
