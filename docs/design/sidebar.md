# The desktop sidebar

Status: implemented (`web/m/sidebar.js`, `web/m/app.js`, `web/m/index.html`).

The wide layout's sidebar was a header of icons (☰, Dashboard, Chat, the mic, the bell), a
"New window" row, the search box, a filter dropdown on the list's own line, and a foot with
Live, the theme and Docs. Every control was one click away, and none of them said what it
was. The redesign came out of a mock loop with the user; these are the decisions it
settled and why.

## Places, settings, and the list

The sidebar holds three kinds of thing, and each now has one home.

- **Places** you go: Dashboard, Chat, Voice and Settings. Dashboard is a row that is always
  there, since it is where you go most. A small chevron at its end unfolds the other three
  under it, and picking one folds them away. There is no "More" with some places outside
  it: a split of one place outside and the rest behind a menu read as arbitrary, and four
  rows always fit, so either all show or the chevron holds the three.
- **Settings** are things you set once: Accounts, Notifications, Theme, Plan usage, the
  phone's sort order, and Docs. They left the ☰ dropdown and the foot for one Settings, the
  same content on a phone (the header's gear, a sheet) and on desktop.
- **The list** has its own block: a "Sessions" title with the green + (new window), then
  search with the filter button beside it, then the list's State / Session · Sub-agents
  line. Search and filter share a row because both narrow the list.

The header is just the logo and the name, with Live as the logo's dot (green, or amber
while it reconnects or the watcher stalls) and a dim collapse at the right, as on
claude.ai. A status word in the foot cost a row to say what a dot says.

## The filter opens beside the list

The filter is the shipped one (#402): Show (All, Running, Recent, Needs you) and, with two
or more accounts, Panes from. It is a bordered square button, unmistakably clickable, that
counts the filters that are on. Its menu opens to the right of the sidebar, never over the
list, so the list narrows in view as you pick. Everything that narrows the list reads as one
"Filtered:" row of chips under the search, the state, the account and the keyword, each
with its ×. The state lives in the URL (`filter=`, `acct=`, `q=`) as before, so a link or
Back restores it.

## Panels dock beside the sidebar

Settings and New window open as a panel docked to the sidebar's right edge, over the left of
the main column, with a sticky title and ×. The right of the screen is usually busy (the chat
column, the pane's overview), and the terminal in the middle matters least while you are
choosing something; docking also keeps the panel next to the control that opened it. The
panel is non-modal, so the list stays live beside it; Escape or a click elsewhere closes it.
One helper does this for both, and a phone keeps its bottom sheet. A centred modal was
rejected: New window had grown taller than the screen and scrolled its own title away.

New window is compact for the same reason: the session on one line, the accounts as a
two-column grid of small chips with "+ Add account" as the next cell, the default for the
session on one line (the full explanation in its tooltip), and each tool a 44px row naming
the account it will start on. Three accounts and five tools fit at 700px tall.

## The collapsed rail keeps working

Folded to a 60px rail, the sidebar used to show its head and the live panes. A rail with
nothing but "expand" made folding a trap, so the rail now keeps everything that works as an
icon, top down: expand, the green +, search, the filter, Dashboard, Chat, Voice, every pane
the list shows in its order, and Settings at the foot. The + opens New window beside the
rail without expanding it, and the filter's menu opens beside it too with its count. Search
expands the sidebar and focuses the box, since a query needs the list. Each pane icon has a
state dot (amber needs you, green running) and, with several accounts, an account dot in
the opposite corner; hovering one shows the list's hover card, with the account's chip. The
pane icons scroll between the places and Settings.

The idle-count button ("+12") is gone: the rail lists every pane, as the list does.

## The phone

The phone's places were already on screen (Dashboard in the tab bar, Chat and the mic in the
header), so its ☰ became a gear that opens the same Settings. Its tab bar is still the state
filter and its account chips stay under search, so the list's chips there leave the state
out. Nothing else on the phone changed.
