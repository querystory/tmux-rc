# Fit Width

Status: implemented (`openbus/fit.py`, `POST /api/panes/{id}/fit`, and "Fit to my
screen" in a pane's ⋯ menu).

## Why

The phone's terminal view is not a tmux client. It renders `capture-pane` output, and the
programs it shows, agent TUIs above all, lay themselves out at their window's width: they
break lines, draw boxes and place status rows for the terminal they think they are in. A
window last sized by a 200-column desktop terminal reaches a 45-column phone as rows four
screens wide: you either pan sideways on every line or shrink the font until it is
unreadable. (Plain text that tmux itself wrapped is no help either: the capture joins it
back into one long line.)

The fix is to give the window the phone's width. When the phone opens a pane's terminal
it reports how many columns it can show at its current font, and again on zoom, rotation
or the toggle; the daemon resizes the window to that width. Only the width: the window's
height stays as it is, because the view scrolls through capture history natively and never
needs to send scroll events to tmux.

## Decisions

**Automatic only when nobody is at a real terminal.** A window belongs to every client of
every session that contains it. If a desktop terminal is attached, shrinking the window
shrinks it there too, which is a surprise nobody at that desk asked for. So when no client
is attached to any of those sessions, fitting just happens; otherwise it waits for the
viewer to opt in through "Fit to my screen", which is off by default and only appears in
that case. The choice is remembered per browser, because it describes the viewer's habit,
not the pane.

**Lone panes only.** Resizing a window that is split squeezes every pane in it, including
ones the viewer is not looking at. A single-pane window, or the zoomed pane of a window
(which fills it), is the only case where the window width is the viewed pane's width.
Anything else is left alone.

**A floor of 40 columns.** Below that most TUIs stop laying out usefully, and a phone in
portrait at a readable font is already above it.

**No restore when the viewer leaves.** A fitted window keeps the phone's width. Nobody is
looking at a window nobody is attached to, so its width costs nothing, and it is the
right width the next time the phone opens it. Putting it back on leave would cost more
than it saves: every restore makes the app redraw at the old width, so a quiet pane is
resized twice per glance. Restoring also needs a way to tell when the viewer has gone
(heartbeats, idle timers, a startup sweep for the crash case), all for a window that
nobody sees in the meantime.

**Given back only when it matters.** Two cases, and only these. A real terminal attaches
to a session holding a window we fitted on our own: the watcher sees it on its regular
poll (no new timer) and hands the window back. Or the viewer unticks "Fit to my screen".
Handing back is `resize-window -A` and then unsetting the window's `window-size`, because
on tmux 3.7 `-A` leaves the option at `manual`; without the unset the window would stop
following its clients for good. A fit the viewer forced while a terminal was attached is
marked as forced, and the watcher leaves it alone.

**The mark lives on the window.** A fitted window carries the width it was given as a
window option, so the mark needs no file, cannot drift from reality, and dies with the
window. That mark is also how the watcher knows which windows are ours to give back,
read from the pane listing it already makes every tick.

**Never fight the user.** If the window's width is no longer the width we set, someone
resized it by hand: the mark is dropped and the window is theirs, with no restore. A
window whose `window-size` the user set before we ever touched it is never fitted at all.

**Our resize is not activity.** Measured on an isolated tmux server with an app that lays
itself out at the terminal's width: each resize made an idle pane look busy. The app
redraws, so the capture changes, which reset the pane's idle clock, stamped it as just
active (moving it to the top of the list), took a snapshot and spent a parse, an LLM call
in production. A timer alone cannot tell a reflow from news, so the watcher uses both time
and content. For a second after a resize that really changed the window's size, every
change is taken as the redraw, since a look can catch it half done. This matters most when
the watcher itself gives a window back, because it captures the pane milliseconds later.
The first look after that second compares the settled screen with the one last read,
ignoring what a reflow changes (whitespace, and box-drawing rules redrawn to the new
width). The same content means it was only the reflow: no activity, no parse. Anything
new, such as a question that arrived during the redraw, is the app's own and is read as
usual. So are a screen not yet parsed and a pending question, whose answer is checked
against the rows on screen. An app that truncates rather than wraps (an ellipsis at the
new width) shows up as new content and costs one parse; that is the safe way to be wrong.

**Reports are POSTs, never part of the live GET.** Folding the report into the live long
poll would have been free, but a GET that resizes a real terminal breaks the rule the
preview sandbox relies on: that a page able to load our URLs still cannot change anything.

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

**Restore on leave, with a heartbeat.** The first version of this feature did it: restore
when the view left, after a few quiet minutes, and at startup for whatever a crash left
narrow. It worked, but it was most of the code, it doubled the redraws, and it solved a
problem nobody has (see "No restore when the viewer leaves").
