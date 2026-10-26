# Tab icons and titles

Status: implemented, strategy B without host colours. Scratch pages have their own icon and
the placeholder a spinner (`web/scratch.svg`, `openbus/scratch_pending.html`); every app view
draws its own icon live (`web/tab-icon.js`, `tabIconKey` in `web/m/pane-model.js`).

## Why

Every tmux-rc tab looks the same today. The app, the dashboard, a pane, a rendered scratch
page and a placeholder all show one favicon, and all the app's views share one fixed title.
Someone who keeps a dozen tabs open, with GitHub pull requests, other apps and tabs from
Claude's browser control, can't find a scratch page by sight. A narrow tab shows the icon
and about five characters. Then "In pr…" and "tmux-…" say nothing, and a page still being
made looks the same as one that has landed.

The same problem grows once tmux-rc runs on more than one machine. Two "Sessions" tabs from
two hosts look identical; the title tells them apart (below).

GitHub's tabs show what a good answer looks like: one clear glyph plus a state colour (a
red cross for failing CI, an orange ring while it runs, a green check). You can read a whole
strip of them at a glance.

## The icon: one glyph on the brand tile (strategy B)

Every tmux-rc icon keeps the app icon's dark rounded tile. The glyph in the middle names the
surface: a terminal for a pane, a page for scratch, the tmux mark for the sessions home. The
green status bar along the bottom is the family cue, and it is always the brand green. A
state dot sits top-right.

The mock compared three strategies in a rebuilt Chrome strip, at real size, among realistic
neighbours:

- **A: the full mark with a corner badge.** It keeps the brand whole, but at 16 pixels the
  light badge and the mark split the space between them, and neither reads. In a narrow
  strip it is the least legible of the three. Rejected.
- **B: a glyph on the brand tile.** The surface glyph gets most of the 16 pixels, and the
  tile still says "tmux-rc" next to other sites. Chosen.
- **C: a tile in the host's colour.** It gives the loudest host signal, but blue, purple
  and teal tiles sit among other sites' blue, purple and teal icons and stop looking like
  tmux-rc. It also leaves no colour free for state.

The glyphs are Lucide's, the same set the app's chrome uses: the tmux-rc mark itself for the
sessions home, terminal for a pane, layout-dashboard, activity, gauge, list-todo, network and
history for the dashboard and its views, files for the scratch gallery and file-text for a
scratch page.

Host colours were dropped after the mock. A hashed colour means nothing until you learn which
host it stands for, it spends the one colour slot the state dot leans on, and a palette that
avoids the state colours runs out of distinct hues after a few hosts. The title names the host
in words instead, so the bar stays the brand green and the family cue stays constant.

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

The app redraws its icon on every render, the same moment it sets the title, so navigating or
a pane changing state updates the tab at once. The state poll keeps running while the tab is hidden,
since a background tab is exactly when its icon is read; it is one long poll answered only on
change, and the heavier history and meter fetches still wait for the tab to be seen. Each icon is the brand tile with the glyph and
dot spliced in as SVG text, turned into a data URL, kept per glyph-and-state key, and only
written to the page when the key changes. One small module draws both these and the
placeholder's spinner; the sandboxed placeholder gets it inlined, since it cannot load a
script the way the app does. The tiles themselves stay the two SVG files the static pages
already use, so there is one drawing of each.

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
3. The host, last. It is the first thing to be cut, which is fine: a narrow strip is usually
   one machine's tabs, and a wider tab shows it.

Dialogs (chat, voice, settings, export) don't change the tab: they sit over a view, and the
tab keeps that view's icon and title. Voice needs no icon of its own, because Chrome already
marks a tab using the microphone.

## Several machines

The host's name comes from the machine's hostname: its first label, with no setup. An
environment variable can override it. The icon carries no host signal (see above). A
single-host user also sees the host in the
title. That costs a few characters at the end of the title, and it is already right the day
they add a second machine.

## Pinned tabs and phones

A pinned tab shows only its icon, which is the strongest case for putting the surface glyph
and the state in the icon rather than only in the title. A home-screen app keeps the base
tmux-rc mark from the manifest: it is the app's identity on a home screen, not a view of it.
Its count belongs in the platform's app badge, not the icon. Phone tab switchers show the title above
a thumbnail, so the title rule does the work there.

## Agent-written pages

The daemon renders the gallery, Markdown pages and the placeholder, so it can give them the
scratch icon. It does not touch a page an agent wrote as HTML. To give such a page a default
icon, the daemon would have to rewrite its bytes in flight, which changes the body length,
breaks byte ranges, and risks clashing with an icon the page declares itself. That is a lot
of risk on arbitrary content for a cosmetic default. A `Link` response header would avoid the
rewrite, but Chrome ignores icons declared that way. Such a page keeps its own icon, or
falls back to the app's.
