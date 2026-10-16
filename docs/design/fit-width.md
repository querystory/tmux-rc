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
is attached to any of those sessions, fitting just happens, by the browser's default
(Settings, "Fit to my screen": Auto, Width only or Off). With a terminal attached it waits
for a choice made for that pane in its ⋯ menu (Width, Tall or Off), which overrides the
default. Both are remembered per browser, because they describe the viewer's habits; a
pane's choice is keyed by the pane's incarnation, so a recycled pane id doesn't inherit it.

**Tall, for apps that keep no history.** Fitting the width is enough for a shell or an
inline agent such as Codex: they write their output into tmux's history, which the view
scrolls (and loads earlier pages of). A full-screen app is different. Claude Code runs on
the terminal's alternate screen, and tmux keeps no history for the alternate screen, so
`#{history_size}` stays 0 however much it prints; all the phone can ever scroll is the
visible rows, about 40 on a desktop window. The fix is a tall window: width from the
phone, height a fixed number of rows (500 by default, a setting), and the app shows that
much of its own transcript. Auto picks it mechanically: tall when the pane is on the
alternate screen (`#{alternate_on}`), width only otherwise. We measured on an isolated
server: Claude Code sat on the alternate screen with history 0 even after printing its
help; Codex and a shell were not, and built history (Codex 9 rows at once, the shell as
soon as output passed one screen). The alternate-screen flag rather than "history is 0"
is the test because a fresh shell also has no history yet, and should not be made tall
for it. At 500 rows Claude Code anchors its input to the bottom, so the view opens
scrolled to the bottom; the capture already includes every visible row, however many.

A tall window is also why fitting stays opt-in with a terminal attached: a desktop
terminal attached to a forced tall window sees the top 40-odd rows of a 500-row window,
which is the opposite of what anyone at that desk wants.

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
poll (no new timer) and hands the window back. Or the viewer picks Off for the pane in its
menu (the default being Off changes nothing: it leaves windows as they are). Going from
tall back to width only gives the height back first.
Handing back is `resize-window -A` and then unsetting the window's `window-size`, because
on tmux 3.7 `-A` leaves the option at `manual`; without the unset the window would stop
following its clients for good. A fit the viewer forced while a terminal was attached is
marked as forced, and the watcher leaves it alone.

**The mark lives on the window.** A fitted window carries the width it was given as a
window option, so the mark needs no file, cannot drift from reality, and dies with the
window. That mark is also how the watcher knows which windows are ours to give back,
read from the pane listing it already makes every tick.

**Never fight the user.** If the window's width (or, for a tall fit, its height) is no
longer what we set, someone resized it by hand: the mark is dropped and the window is
theirs, with no restore. A
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
ignoring only what a reflow changes (whitespace and box-drawing rules redrawn to the new
width) and, as the watcher always does, timers and spinners. The same content means it was only the reflow: no activity, no parse. Anything
new, such as a question that arrived during the redraw, is the app's own and is read as
usual. So are a screen not yet parsed and a pending question, whose answer is checked
against the rows on screen. The comparison is exact on purpose. An app that truncates
rather than wraps (an ellipsis at the new width), or a reflow that pushes the oldest
scrollback out of the capture's row budget, shows up as new content and costs one parse.
Every looser rule tried (allowing trimmed scrollback, say) could also take a new question
for old text, and a card that never learns of its question is worse than a parse.

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
