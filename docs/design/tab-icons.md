# Tab icons and titles

Status: proposed. The first piece is implemented: scratch pages get their own icon, and the
in-progress placeholder gets a spinner (`web/scratch.svg`, `openbus/scratch_pending.html`).
The rest waits on a pick between the strategies below.

## Why

Every tmux-rc tab looks the same today. The app, the dashboard, a pane, a rendered scratch
page and a placeholder all show one favicon, and all the app's views share one fixed title.
Someone who keeps a dozen tabs open, with GitHub pull requests, other apps and tabs from
Claude's browser control, can't find a scratch page by sight. A narrow tab shows the icon
and about five characters. Then "In pr…" and "tmux-…" say nothing, and a page still being
made looks the same as one that has landed.

The same problem grows once tmux-rc runs on more than one machine. Two "Sessions" tabs from
two hosts look identical.

GitHub's tabs show what a good answer looks like: one clear glyph plus a state colour (a
red cross for failing CI, an orange ring while it runs, a green check). You can read a whole
strip of them at a glance.

## The icon: one glyph on the brand tile (strategy B)

Every tmux-rc icon keeps the app icon's dark rounded tile. The glyph in the middle names the
surface: a terminal for a pane, a page for scratch, the tmux mark for the sessions home. The
green status bar along the bottom is the family cue. Once hosts are distinguished, it takes
the host's colour. A state dot sits top-right.

The mock compared three strategies in a rebuilt Chrome strip, at real size, among realistic
neighbours:

- **A: the full mark with a corner badge.** It keeps the brand whole, but at 16 pixels the
  light badge and the mark split the space between them, and neither reads. In a narrow
  strip it is the least legible of the three. Rejected.
- **B: a glyph on the brand tile.** The surface glyph gets most of the 16 pixels, the tile
  still says "tmux-rc" next to other sites, and the host shows even on a pinned tab.
  Recommended.
- **C: a tile in the host's colour.** It gives the loudest host signal, but blue, purple
  and teal tiles sit among other sites' blue, purple and teal icons and stop looking like
  tmux-rc. It also leaves no colour free for state.

A host monogram (a letter in the icon) was rejected because a legible letter needs about
seven of the sixteen pixels, which leaves no room for the surface glyph or the state dot.

## State in the icon

The icon carries four states, each a colour in the same top-right slot: needs you (red),
working (amber), finished while you were away (green, with a check), and stopped (grey). A
fifth, in progress, is motion rather than colour: an arc running round the tile's edge.
That is the one state where you are waiting for something to happen, and motion is what
says "wait" in a tab strip.

The dot shows the state of the tab's own subject. A pane tab shows that pane's state. The
sessions home shows a dot while anything needs you. Views with nothing of their own to
report show none. "Finished" clears when you look at the tab, since it exists only to say
that something happened while you were away. The other states clear when the state itself
changes.

Chrome does not animate SVG favicons, so the placeholder steps through eight frames with a
small inline script. A background tab's timers slow to about once a second. The arc then
moves once a second, which still reads as working. When the agent stops without
publishing, the arc gives way to the grey dot. When the page lands, the placeholder's own
reload replaces it with the page, so the spinner has no stop logic.

There was no Needs-you badge on the favicon before this. The count goes in the title (next
section), because a number is illegible in a 16-pixel icon and Chrome already marks a
pinned tab whose title changes.

## The title

The title rule is: **Needs-you count, subject, host.** For example: "(2) deploy fix ·
kestrel". A narrow tab truncates from the right, so the order follows what matters most
when only a few characters show:

1. The count, Gmail-style, on app tabs only. Scratch pages are sandboxed and can't ask the
   daemon.
2. The most specific subject: the pane's name, the view, or the page's title. For the
   placeholder, the state comes first ("In progress · …", "Stopped · …"), because that is
   the one thing its tab has to say.
3. The host, last. It is the first thing to be cut, which is fine, since the icon carries it.

Dialogs (chat, voice, settings, export) don't change the tab: they sit over a view, and the
tab keeps that view's icon and title. Voice needs no icon of its own, because Chrome already
marks a tab using the microphone.

## Several machines

The host's name comes from the machine's hostname: its first label, with no setup. Its
colour is hashed from that name into a small palette that avoids the state colours. An
environment variable can override either. A single-host user also sees the host in the
title. That costs a few characters at the end of the title, and it is already right the day
they add a second machine.

## Pinned tabs and phones

A pinned tab shows only its icon, which is the strongest case for putting the surface glyph,
the host colour and the state in the icon rather than only in the title. A home-screen app
uses the touch icon from the manifest, which the daemon can colour per host. Its count
belongs in the platform's app badge, not the icon. Phone tab switchers show the title above
a thumbnail, so the title rule does the work there.

## Agent-written pages

The daemon renders the gallery, Markdown pages and the placeholder, so it can give them the
scratch icon. It does not touch a page an agent wrote as HTML. To give such a page a default
icon, the daemon would have to rewrite its bytes in flight, which changes the body length,
breaks byte ranges, and risks clashing with an icon the page declares itself. That is a lot
of risk on arbitrary content for a cosmetic default. A `Link` response header would avoid the
rewrite, but Chrome ignores icons declared that way. Such a page keeps its own icon, or
falls back to the app's.
