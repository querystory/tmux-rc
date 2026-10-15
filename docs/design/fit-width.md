# Fit Width

Status: implemented (`openbus/fit.py`, the live view's `cols`, and "Fit to my screen" in a
pane's ⋯ menu).

## Why

The phone's terminal view is not a tmux client. It renders `capture-pane` output, so every
line arrives already wrapped at the window's width. A window last sized by a 200-column
desktop terminal reaches a 45-column phone as lines four screens wide: you either pan
sideways on every line or shrink the font until it is unreadable.

The fix is to make tmux wrap where the phone does. While a viewer looks at a pane it
reports how many columns it can show at its current font, and the daemon resizes the
window to that width. Only the width: the window's height stays as it is, because the view
scrolls through capture history natively and never needs to send scroll events to tmux.

## Decisions

**Automatic only when nobody is at a real terminal.** A window belongs to every client of
its session. If a desktop terminal is attached, shrinking the window shrinks it there too,
which is a surprise nobody at that desk asked for. So when the session's attached count is
zero, fitting just happens; when a terminal is attached, it waits for the viewer to opt in
through "Fit to my screen", which is off by default and only appears in that case. The
choice is remembered per browser, because it describes the viewer's habit, not the pane.

**Lone panes only.** Resizing a window that is split squeezes every pane in it, including
ones the viewer is not looking at. A single-pane window, or a pane that is zoomed (and so
fills its window), is the only case where the window width is the viewed pane's width.
Anything else is left alone.

**A floor of 40 columns.** Below that most TUIs stop laying out usefully, and a phone in
portrait at a readable font is already above it.

**Several viewers: the narrowest wins.** Two phones on one pane would otherwise take turns
resizing it every poll. The narrowest width serves both: the wider one sees short lines,
not wrapped ones.

**Always put it back.** The resize sets the window's `window-size` to manual, and
`resize-window -A` alone leaves it manual, so restoring is the pair: size the window to its
clients again, then unset the window's own `window-size` so it inherits the session or
global value once more. That happens when the viewer leaves the pane (or its terminal view,
or hides the page), when its heartbeat stops for three minutes (each live round is one, so
a closed laptop or a dropped tunnel counts), and at daemon startup.

**The mark lives on the window.** Which windows tmux-rc narrowed is stored as a window
option on the window itself, holding the width it was given. It needs no file, cannot drift
from reality, and dies with the window. A daemon that crashed while a window was narrow
finds the mark on its first sweep and restores it.

**Never fight the user.** If the window's width is no longer the width we set, someone
resized it by hand: the mark is dropped and the window is theirs, with no restore. A window
whose `window-size` the user set locally before we ever touched it is never fitted at all.

## Alternatives rejected

**Reflow on the client.** The view could rewrap long lines itself. That works for a shell's
plain output and breaks everything else: a TUI lays itself out at the pane's width, with
boxes, columns and status lines placed by cursor position, and rewrapping those rows
produces garbage. Only the program can lay itself out narrower, and it only does so when
its terminal really is narrower.

**A control-mode client with `window-size latest`.** Attaching a `tmux -C` client sized to
the phone would make tmux do the sizing itself. But "latest" means the client that was most
recently active, and tmux-rc types through `send-keys`, which is not activity from any
client, so the size would follow whichever terminal last saw a keystroke rather than the
screen being looked at. It would also be a long-lived attached client per viewer, visible
to every tool that counts clients, including our own attached check above.
