"""Turn recorded frames into one self-contained animated SVG.

A GitHub README is static HTML: no JavaScript, and images are fetched through
a proxy and drawn with <img>. Scripts inside an <img>-loaded SVG never run —
but CSS animation does, so the whole session can ship as one file with no
player, no video and no GIF banding on 13px text.

The screen is drawn the same way the web player draws it: every run is placed
at its own column, so the layout survives whatever monospace font the reader's
machine happens to use. Each row's successive contents become overlapping
<text> elements, and a keyframes rule makes exactly one of them visible at a
time. Nothing is re-wrapped and no text is edited; this is the recording.

    python3 scripts/frames_to_svg.py public/demo/session-en.json out.svg [--speed 1.5] [--end 60] [--hold 6]
"""
import html
import json
import sys

NAMED = {
    "black": "#0d1117", "red": "#f85149", "green": "#3fb950", "brown": "#e3b341",
    "blue": "#58a6ff", "magenta": "#a78bfa", "cyan": "#39c5cf", "white": "#e6edf3",
    "brightblack": "#6e7681", "brightred": "#ff7b72", "brightgreen": "#56d364",
    "brightbrown": "#f2cc60", "brightblue": "#79c0ff", "brightmagenta": "#c9a2ff",
    "brightcyan": "#56d4dd", "brightwhite": "#f0f6fc",
}
FG = "#c9d1d9"
BG = "#0b0f0c"
CW = 8.0          # column width in px
FONT = 13.4       # font-size; CW/FONT matches a monospace advance of ~0.6
LH = 19.0         # line height
PAD_X, PAD_Y = 16.0, 14.0
FONT_STACK = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
              "'DejaVu Sans Mono', 'Liberation Mono', monospace")


def color(value, fallback):
    if not value or value == "default":
        return fallback
    return NAMED.get(value, f"#{value}")


def main():
    src, dst = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]

    def opt(name, default):
        return float(args[args.index(name) + 1]) if name in args else default

    speed = opt("--speed", 1.0)
    hold = opt("--hold", 6.0)
    session = json.load(open(src, encoding="utf-8"))
    cols, rows = session["cols"], session["rows"]
    end = opt("--end", session["duration"])
    frames = [f for f in session["frames"] if f[0] <= end]
    total = (end + hold) / speed

    # ── Row timelines ────────────────────────────────────────────────────
    # For each row, the list of (start, stop, runs) it displayed. A row that
    # never shows anything contributes nothing to the file.
    open_at = {}
    intervals = []
    for t, delta in frames:
        for key, runs in delta.items():
            y = int(key)
            if y in open_at:
                start, previous = open_at.pop(y)
                if previous:
                    intervals.append((start, t / speed, y, previous))
            open_at[y] = (t / speed, runs)
    for y, (start, runs) in open_at.items():
        if runs:
            intervals.append((start, total, y, runs))

    width = cols * CW + 2 * PAD_X
    height = rows * LH + 2 * PAD_Y

    # One keyframes rule per distinct visible window, shared by every row that
    # happens to use it.
    windows = {}
    for start, stop, _y, _runs in intervals:
        windows.setdefault((round(start / total * 100, 3),
                            round(min(stop, total) / total * 100, 3)), None)
    for index, key in enumerate(windows):
        windows[key] = f"w{index}"

    css = [
        f"svg{{background:{BG}}}",
        f"text{{font-family:{FONT_STACK};font-size:{FONT}px;"
        "white-space:pre;dominant-baseline:middle}",
        "g{visibility:hidden}",
    ]
    for (a, b), name in windows.items():
        # step-end keeps every switch instant: no cross-fade, no interpolation
        # of something that was never a gradual change on the real screen.
        if b >= 99.999:
            body = f"0%,{a}%{{visibility:hidden}}{a}%,100%{{visibility:visible}}"
        else:
            body = (f"0%,{a}%{{visibility:hidden}}{a}%,{b}%{{visibility:visible}}"
                    f"{b}%,100%{{visibility:hidden}}")
        css.append(f".{name}{{animation:{name} {total:.2f}s infinite step-end}}"
                   f"@keyframes {name}{{{body}}}")

    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" '
        f'height="{height:.0f}" font-size="{FONT}">',
        "<style>" + "".join(css) + "</style>",
        f'<rect width="100%" height="100%" rx="10" fill="{BG}"/>',
    ]
    for start, stop, y, runs in intervals:
        name = windows[(round(start / total * 100, 3),
                        round(min(stop, total) / total * 100, 3))]
        baseline = PAD_Y + y * LH + LH / 2
        spans = []
        column = 0
        for text, fg, bg, flags, cells in runs:
            x = PAD_X + column * CW
            column += cells
            if text.strip():
                weight = ' font-weight="600"' if flags & 1 else ""
                fill = color(bg, BG) if flags & 2 else color(fg, FG)
                spans.append(f'<tspan x="{x:.1f}" fill="{fill}"{weight}>'
                             f'{html.escape(text)}</tspan>')
            if (bg and bg != "default") or flags & 2:
                paint = color(fg, FG) if flags & 2 else color(bg, BG)
                out.append(f'<g class="{name}"><rect x="{x:.1f}" '
                           f'y="{baseline - LH / 2:.1f}" width="{cells * CW:.1f}" '
                           f'height="{LH:.1f}" fill="{paint}"/></g>')
        if spans:
            out.append(f'<g class="{name}"><text y="{baseline:.1f}" '
                       f'xml:space="preserve">{"".join(spans)}</text></g>')
    out.append("</svg>")

    open(dst, "w", encoding="utf-8").write("".join(out))
    print(f"{len(intervals)} row states, {total:.1f}s loop, "
          f"{len(open(dst, 'rb').read()) / 1024:.0f} KB")


main()
