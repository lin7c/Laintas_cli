---
name: windows-machine
description: Driving the Windows machine itself -- reading windows and the screen, clicking real controls -- through a connected Helpwo kernel.
version: 1.1.0
requires_tool: win.snapshot
triggers:
  - open the browser
  - open an app
  - launch
  - window
  - screen
  - desktop
  - computer
  - click a button
  - desktop app
  - screenshot the screen
  - read the screen
  - automate an application
  - accessibility tree
  - wechat
  - qq
  - clipboard
  - what is on my screen
  - control my computer
---

# Driving the Windows Machine

The rest of this CLI reaches Windows *files and programs* from the Linux side.
This is the other mechanism: reaching **running applications** -- clicking a
button in a desktop app, reading what is on screen. It exists only while
`helpwo-kernel.exe` is running on the Windows side with a machine tier
switched on, which is why this skill is only offered to you when it is.

Load it before your first `win.*` call in a session, not after.

## What you are actually inside of

Every other tool here is bounded by a folder. These are not. The machine
tiers exist precisely because "read every window" and "drive every
application" cannot be described by a path, and the kernel's own window says
so in a block headed BEYOND THE WORKSPACE.

So: the user switched this on deliberately, for a task. It is not a general
licence, and the sections below are how to hold up your end of that.

## Opening a browser or an application

When the kernel is connected, "open the browser", "open Word", "open WeChat"
mean a window on the desktop the user is looking at. `browser.*` is not that:
it is a headless Chrome inside WSL, invisible to the user and signed in to
nothing of theirs. Use it for background page work (scraping, testing a
site) or when the user names it, not as "the browser".

There is no `win.launch` -- starting programs stays on the guarded shell, on
purpose. From this WSL shell, use `explorer.exe`: it hands the request to
Windows and returns at once, and the command guard lets it through.

```bash
# a URL in the user's default browser (their profile, their logins)
explorer.exe "https://example.com"

# an installed program: find its Start-menu shortcut, then open it
find "/mnt/c/ProgramData/Microsoft/Windows/Start Menu/Programs" \
     /mnt/c/Users/*/AppData/Roaming/Microsoft/Windows/Start\ Menu/Programs \
     -maxdepth 3 -iname '*chrome*.lnk' 2>/dev/null
explorer.exe "$(wslpath -w "/mnt/c/ProgramData/Microsoft/Windows/Start Menu/Programs/Google Chrome.lnk")"

# a Windows built-in on PATH
explorer.exe notepad.exe
```

- `explorer.exe` exits with status 1 even when it worked. Judge success by
  the window appearing, not by the exit code.
- Shortcut names are in the user's language, not the product's English
  name; list the folders and pick, rather than guessing.
  Start-menu folders nest (`Programs/<Vendor>/<App>.lnk`); `find` walks
  them. With no match, drop the `-iname` and read the list.
- Do **not** run a GUI `.exe` directly (`notepad.exe`, `/mnt/c/.../app.exe`):
  WSL waits for it to exit, so the command hangs until the idle timeout, and
  the program is killed when the command ends.
- `powershell.exe -Command 'Start-Process ...'` and `cmd.exe /c start ...`
  also work, but the guard can stop each one for the user's approval (always
  under an enforcing policy or a remote channel). Keep them
  for what explorer cannot do, e.g. `Get-StartApps` to find a Store app's
  AppID, then `explorer.exe "shell:AppsFolder\<AppID>"`.
- Launch with the URL rather than opening a blank browser and typing into it:
  it is one step instead of four and never races the address bar.

Starting is asynchronous. Poll `win.windows` (every second or so, for up to
~20 s) until a window from that process appears, then work on it by
`handle`. A program that was already running may just come to the front
instead of opening a new window -- look for it by title rather than
assuming a new one.

**Inside a browser window**, the tree covers the browser's own controls
(tabs, address bar, buttons) and usually the page too. The page part can be
sparse on the first snapshot while the browser switches its accessibility
support on; snapshot once more before concluding it is empty. To go somewhere
new in an open window, `win.set_value` the address bar control (Chrome and
Edge label it "Address and search bar") and `win.key` `enter`; only if that
control is missing, `win.key` `ctrl+l`, `win.type` the URL, `win.key` `enter`.
Remember whose browser it is: every tab, saved password and signed-in site
in it belongs to the user, and the rules below apply to all of them.

## Read the tree before you look at pixels

`win.snapshot` and `win.screenshot` are not two ways to do the same thing.

- **`win.snapshot`** reads the accessibility tree and hands back **named,
  addressable controls**. Acting on one with `win.invoke` calls the control's
  own method: no pointer moves, no focus is taken, and the user can keep
  typing in another window while it happens. One cheap round trip.
- **`win.screenshot`** gives you a picture, which costs an `image.describe` or
  `image.to_text` call to read, and acting on it means `win.click` at
  coordinates -- the real mouse, taken away from whoever is sitting there.
  **Every look is a separate model call and a separate charge.** A twenty-step
  task done this way is twenty of them.

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

## The pointer is shared

`win.click`, `win.type` and `win.key` use the one real mouse and keyboard. If
the user is at the machine, they will see it happen and their own typing will
land in the wrong place. Prefer the tree; when you cannot, keep the run short
and say what you are about to do.

## What you will see that you did not ask for

This is the part with no technical control behind it, because there cannot be
one: a window list, an accessibility tree, a screenshot and the clipboard all
return whatever happens to be there. On a real desktop that routinely
includes a password manager, a banking tab, a private conversation, a
colleague's email, a 2FA code.

Four rules, and they are about **your** behaviour, not the tool's:

1. **Narrow the read.** Snapshot the window you need by title. Prefer
   `win.snapshot` of one window over `win.screenshot` of the whole desktop,
   and prefer either over reading every window in the list "for context".
2. **Do not repeat what you saw in passing.** Content from an unrelated
   window is not part of the task. Do not summarise it, quote it, or use it
   to answer a later question. If you must say that you saw something --
   because it blocks the task -- name the window, not the contents.
3. **A secret you read is a secret you do not write down.** Never put a
   password, token, recovery code or 2FA code into a file, a commit, a shell
   command, a chat message, or a network request. `win.clipboard.get` is the
   sharpest edge here: people keep passwords on the clipboard for seconds at
   a time, and you may read one while reaching for something else. If that
   happens, say so and move on -- do not echo it back.
4. **Never authenticate as the user on your own initiative.** Typing a
   password you found, approving a prompt, clicking through a consent dialog
   or completing a 2FA challenge is the user's decision every time, even when
   it is obviously what would unblock the task. Ask.

## What the shell can still reach

The machine tiers are about windows. The guarded shell (`ai-exec`) is a
separate surface with its own boundary, and on Windows its working directory
is the user's real profile -- which is where Chrome keeps its cookie
database, where DPAPI keeps its master keys, and where the credential manager
lives.

The guard already asks before any command that names credential material, and
refuses outright a command that reads such a file and sends it off the
machine in the same line. Do not treat that prompt as an obstacle to route
around: reading a cookie store to "check if the user is logged in" is the
wrong tool for that question, and there is nearly always a direct one -- ask
the user, or look at the application itself.
