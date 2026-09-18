"""Record a real laintas-cli session from a PTY into a timed cast (JSONL).

Line 0 is a header {"cols","rows","version"}; every other line is
[elapsed_seconds, "output chunk"] — the asciinema v2 shape, so the raw file
stays inspectable with standard tools.

The harness answers the terminal's cursor-position request (CPR) the way a
real terminal does; without it prompt_toolkit prints a warning that belongs to
the recording rig, not to the product.
"""
import json, os, re, sys, time, pexpect

out_path, cwd, duration = sys.argv[1], sys.argv[2], float(sys.argv[3])
sends = sys.argv[4:]

# A turn that wants to write a file stops on the approval dialog, and that
# dialog is part of what the recording is for — so the rig answers it the way
# a watching user would. `y` approves the one change on screen; it never
# reaches for "always", which would grant more than this turn.
APPROVAL = (b"approve", b"deny")
COLS, ROWS = 100, 34

env = dict(os.environ)
env.update(TERM="xterm-256color", COLUMNS=str(COLS), LINES=str(ROWS))
child = pexpect.spawn("/usr/local/bin/laintas-cli", [], cwd=cwd, dimensions=(ROWS, COLS),
                      encoding=None, timeout=1, env=env)

cast = open(out_path, "w", encoding="utf-8")
cast.write(json.dumps({"version": 2, "width": COLS, "height": ROWS}) + "\n")
t0 = time.time()
pending = b""
sent = 0
answered = 0.0
prompt_seen = 0.0
CPR = re.compile(rb"\x1b\[6n")
while time.time() - t0 < duration:
    try:
        data = child.read_nonblocking(size=16384, timeout=0.05)
    except pexpect.TIMEOUT:
        data = b""
    except Exception:
        break
    if data:
        cast.write(json.dumps([round(time.time() - t0, 3),
                               data.decode("utf-8", "replace")]) + "\n")
        cast.flush()
        pending = (pending + data)[-4000:]
        if CPR.search(data):
            child.send(b"\x1b[%d;%dR" % (ROWS, 1))
        if b"\xe2\x94\x82 " in data and b"\xe2\x80\xba" in data and not prompt_seen:
            prompt_seen = time.time()
        if all(token in pending for token in APPROVAL) and time.time() - answered > 8:
            time.sleep(1.2)          # let the dialog finish painting
            child.send(b"y")
            answered = time.time()
            pending = b""
    # Send the next line once the prompt has been idle for a beat.
    if sent < len(sends) and prompt_seen and time.time() - prompt_seen > 2.0:
        child.send(sends[sent].encode() + b"\r")
        sent += 1
        prompt_seen = 0.0
cast.close()
child.kill(9)
