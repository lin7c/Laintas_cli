"""Turn a recorded cast into replay frames for the download page.

A cast is raw terminal bytes, so the only honest way to read it is to run a
terminal: pyte replays the same escape sequences the user's terminal saw and
we photograph its screen. Each frame carries only the rows that changed since
the previous one, as runs of (text, colour, bold, reverse).
"""
import json, sys
import pyte

src, dst = sys.argv[1], sys.argv[2]
meta = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
SAMPLE_MS = 40          # screenshots per 40ms of session time
raw = open(src, encoding="utf-8").read().splitlines()
header = json.loads(raw[0])
cols, rows = header["width"], header["height"]
events = [json.loads(line) for line in raw[1:]]

screen = pyte.Screen(cols, rows)
stream = pyte.Stream(screen)
# Only a full-screen erase counts. `ESC[J` on its own erases to the end of the
# screen and prompt_toolkit writes one on nearly every repaint, so matching it
# would put the start of playback at the last keystroke of the session.
CLEAR_RE = __import__("re").compile(r"\x1b\[[23]J")
cleared_at = 0.0


def row_runs(y):
    """One screen row as merged style runs; trailing blanks dropped."""
    line = screen.buffer[y]
    runs = []
    for x in range(cols):
        ch = line[x]
        style = (ch.fg, ch.bg, ch.bold, ch.reverse)
        # Plain ASCII merges into runs; anything else (CJK, box drawing, the
        # status dots) gets a run of its own so the page can pin that single
        # glyph to the exact columns the terminal gave it. Fallback fonts
        # disagree about the width of those glyphs, and a merged run would let
        # the disagreement accumulate across the line.
        exotic = not (ch.data and " " <= ch.data <= "~")
        if runs and runs[-1][0] == style and not exotic and not runs[-1][2]:
            runs[-1][1].append(ch.data)
        else:
            runs.append((style, [ch.data], exotic))
    out = []
    for (fg, bg, bold, reverse), chars, _exotic in runs:
        # `cells` is how many terminal columns the run occupies — a CJK glyph
        # owns two of them, and the page pins each run to that width so wide
        # text can never drift out of column alignment.
        out.append(["".join(chars), fg, bg,
                    (1 if bold else 0) | (2 if reverse else 0), len(chars)])
    while out and out[-1][0].strip() == "" and out[-1][2] == "default" \
            and out[-1][3] == 0:
        out.pop()
    return out


def snapshot():
    return [row_runs(y) for y in range(rows)]


frames = []
blank_frames = []
prev = [[] for _ in range(rows)]
last_shot = -1.0


def shoot(t):
    global prev
    now = snapshot()
    delta = {str(y): now[y] for y in range(rows) if now[y] != prev[y]}
    if delta:
        if not any(now):
            blank_frames.append(len(frames))
        frames.append([round(t, 3), delta])
    prev = now


for t, chunk in events:
    # The first one: the session's own opening `clear`. A later full erase
    # would belong to whatever the agent ran, and is part of the show.
    if not cleared_at and CLEAR_RE.search(chunk):
        cleared_at = t
    stream.feed(chunk)
    if last_shot < 0 or (t - last_shot) * 1000 >= SAMPLE_MS:
        shoot(t)
        last_shot = t
shoot(events[-1][0] if events else 0.0)

# ── Start at the cleared screen ───────────────────────────────────────────
# A recording opens with sign-in, the banner and the project files the CLI
# creates on its first run in a folder — the part nobody watches. The recorded
# session therefore begins by typing `clear`, a real command running through
# the real PTY, and playback starts at the blank screen it produced: the last
# frame where nothing is on screen at all. Everything before it is dropped and
# the clock is rebased, so the page opens on the prompt.
# The screen is wiped by the escape the `clear` command writes. Prefer that
# moment over looking for an all-blank screenshot: the prompt is usually
# repainted inside the same sampling interval, so a blank frame may never be
# photographed at all.
start = 0
if cleared_at:
    start = next((i for i, frame in enumerate(frames) if frame[0] >= cleared_at),
                 0)
elif blank_frames:
    start = blank_frames[-1]
if start:
    offset = frames[start][0]
    frames = [[round(t - offset, 3), delta] for t, delta in frames[start:]]
    print(f"start trimmed to the cleared screen at {offset:.1f}s "
          f"({start} frames dropped)")

# ── Jump cuts ─────────────────────────────────────────────────────────────
# A real turn spends most of its wall clock on one animating status row. The
# recording keeps every frame of it, but a page that replays 40 idle seconds
# in full never reaches the answer, so a wait longer than KEEP_HEAD + KEEP_TAIL
# is cut in the middle like a video edit: nothing is rewritten, whole frames
# are dropped and the ones after them move earlier. The elapsed counter on the
# status row visibly jumps at each cut, which is the point — it reads as a
# fast-forward rather than pretending the model was quick.
import re as _re

WAIT_RE = _re.compile(r"^\s*L[·›»]\s+(Thinking|Writing|Running|Checking|Connecting)")
KEEP_HEAD, KEEP_TAIL = 2.5, 1.5


def is_wait(delta):
    seen_status = False
    for runs in delta.values():
        text = "".join(run[0] for run in runs)
        if not text.strip():
            continue
        if WAIT_RE.match(text):
            seen_status = True
            continue
        return False
    return seen_status


cut_seconds = 0.0
cut_count = 0
kept = []
index = 0
shift = 0.0
while index < len(frames):
    if not is_wait(frames[index][1]):
        kept.append([round(frames[index][0] - shift, 3), frames[index][1]])
        index += 1
        continue
    run_start = index
    while index < len(frames) and is_wait(frames[index][1]):
        index += 1
    start_t, end_t = frames[run_start][0], frames[index - 1][0]
    if end_t - start_t <= KEEP_HEAD + KEEP_TAIL:
        for frame in frames[run_start:index]:
            kept.append([round(frame[0] - shift, 3), frame[1]])
        continue
    head = [f for f in frames[run_start:index] if f[0] <= start_t + KEEP_HEAD]
    tail = [f for f in frames[run_start:index] if f[0] >= end_t - KEEP_TAIL]
    gap = (tail[0][0] - head[-1][0]) - 0.2
    for frame in head:
        kept.append([round(frame[0] - shift, 3), frame[1]])
    shift += gap
    cut_seconds += gap
    cut_count += 1
    for frame in tail:
        kept.append([round(frame[0] - shift, 3), frame[1]])
frames = kept
meta = dict(meta, cuts=cut_count, cut_seconds=round(cut_seconds, 1),
            real_duration=round(events[-1][0], 1) if events else 0)
print(f"{cut_count} jump cuts, {cut_seconds:.1f}s of waiting removed")

json.dump({"cols": cols, "rows": rows, "meta": meta,
           "duration": frames[-1][0] if frames else 0, "frames": frames},
          open(dst, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print(f"{len(frames)} frames, {frames[-1][0] if frames else 0:.1f}s")
