# Recording the hero terminal

The session on the landing page is a real laintas-cli run, not a mockup, so it
is refreshed by recording a new one — never by editing the frames by hand.

```bash
pip install pexpect pyte

# 1. Record. The task is typed into the live REPL once the prompt appears,
#    so pick a project worth showing and a task that finishes in a minute or
#    two. Every AI call in the recording is billed to the signed-in account.
python3 scripts/record_session.py /tmp/zh.cast ~/work/some-project 190 \
    "clear" "跑一下测试，看看那个失败的用例是怎么回事"

# 2. Convert. Frames carry only the screen rows that changed, plus the version
#    the page states in its caption.
python3 scripts/cast_to_frames.py /tmp/zh.cast public/demo/session-zh.json \
    '{"version":"1.29.4","recorded":"2026-09-18","shell":"bash"}'
```

The leading `clear` is deliberate: it runs through the real PTY like any other
command, and the converter starts playback at the blank screen it leaves
behind, so the page opens on the prompt instead of on sign-in and the banner.
Keep it as the first thing typed.

`session-zh.json` and `session-en.json` are the two recordings the page plays
(`TerminalReplay.jsx` picks one by the site language). Both are replayed
verbatim; the converter only drops frames — everything before the cleared
screen, and the middle of long waits on the status row (reported, and recorded
in `meta.cuts`). It never rewrites what was on screen.

Things that bite when re-recording:

- **Record in a fresh project directory.** A folder that already held a
  session carries its memory into the next one, and the agent answers from
  what it remembers instead of going and looking.
- **A task that writes stops on the approval dialog.** The rig answers it with
  `y` (never `a`/always), so the recording shows the diff, the approval and
  the re-run. Leave the policy in enforce mode — the dialog is worth showing.
- **Keep the terminal at the size in `record_session.py` (100×34).** The CLI
  lays out for the size it is given; the page scales the finished screen
  rather than re-wrapping it, so a different size means a different layout.
- **Read the answer before shipping it.** It is a model's own words about a
  real repository, on a public page, and it is not always right: one take on
  the same project blamed a missing rounding step for what was a
  `Decimal(float)` bug. If the diagnosis is wrong, record again — never edit
  the frames to make it right.
