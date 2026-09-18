# canvas

Whiteboards in the terminal, and for the agent.

A board is an ordinary `.excalidraw` file, so it opens in Helpwo's editor, in
Excalidraw itself, or in anything else that reads the format. This extension
adds the terminal half: a full-screen viewer you can pan, zoom and draw on, and
four tools the model uses to put diagrams on a board you then review.

```
/canvas                   open a canvas (makes a board if there is no empty one)
/canvas <path>            view that board (created if missing)
/canvas list              boards under this directory
/canvas text <path>       what is on it, as text — ids, labels, arrows
/canvas new <path>        create a named board and open it
/canvas open [path]       open it in Helpwo, where you draw with a mouse
```

In the viewer: `w` turns drawing on (`r` rect, `o` ellipse, `d` diamond,
`a` arrow, `l` line, `p` pencil, `t` text), `c` colour, `f` fill, `1`-`3`
stroke width, `x` delete, `u` undo, `b` another board, `q` close.

## Tools

| Tool | What it does |
| --- | --- |
| `canvas.list` | Boards under the working directory |
| `canvas.read` | Ids, labels and what each arrow connects |
| `canvas.draw` | Add shapes and arrows in one write |
| `canvas.update` | Relabel, move or erase elements already there |

What the model draws is marked as its turn, so a person can review or undo it
in the editor instead of finding a diagram that changed under them.

## Why it is an extension

Whiteboards used to be built into the CLI. Nothing else in the product depends
on them — `infinite_canvas` borrows `workflow_viz` for its glyphs and that is
the only edge — so they are a feature rather than infrastructure, and they cost
every copy of the agent four tool schemas in front of the model whether or not
anyone draws.

The command still uses the CLI for the parts the CLI owns: argument splitting,
the Helpwo sub-terminal launcher, the console. That is the normal arrangement —
an extension runs in-process with the same permissions, and the host's context
is documented as a convenience rather than a boundary.

## Files

```
main.py             the /canvas command and the four tools
canvas.py           the .excalidraw format: read, write, describe, review turns
canvas_edit.py      the editor: draw, label, move, erase, concurrency guard
canvas_view.py      the full-screen viewer (prompt_toolkit)
infinite_canvas.py  the pan/zoom scene the viewer renders
```
