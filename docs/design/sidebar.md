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
  there, since it is where you go most. Under it, "··· More" gives way to the other three
  rows, and a small dim caret on the last of them (Show less, its own button) folds them back
  into More; it stays as you left it. A chevron on the Dashboard row was tried first, and
  read as ambiguous (one row that both opened a place and unfolded others), and so did a More
  row that stayed above the rows it had unfolded. There is no split of some places outside More and the rest behind it:
  one place left outside read as arbitrary.
- **Settings** are things you set once: Accounts, Notifications, Theme, Plan usage, the
  phone's sort order, and Docs. They left the ☰ dropdown and the foot for one Settings, the
  same content on a phone (the header's gear, a sheet) and on desktop.
- **The list** has its own block: a "Sessions" title followed by its counts (panes ·
  sub-agents, muted, so the green is the +'s alone) and, fixed at the right, the green + (new
  window), then search with the filter button beside it. Search and
  filter share a row because both narrow the list. The list's own line (State / Session ·
  Sub-agents) is gone: grouping is a rarely changed choice, so it joined the filter menu as
  "Group by", and the sub-agents count, which also switches their lines under each row (its eye is open while
  they show, slashed while not), sits
  in the title row.

Between the header and the usage strip everything scrolls as one, so the places scroll out
of the way as you page through panes, and the Sessions block (its title and +, search and
the filter, the "Filtered:" chips) sticks under the header with a hairline once it has. The
header, with the collapse, stays put: scrolling it away hid the way to fold the sidebar. The
usage strip stays at the foot.

The header is just the logo and the name, with Live as the logo's dot (green, or amber
while it reconnects or the watcher stalls) and a dim collapse at the right, as on
claude.ai. A status word in the foot cost a row to say what a dot says.

## The filter opens beside the list

The filter is the shipped one (#402): Show (All, Running, Recent, Needs you) and, with two
or more accounts, Panes from, and Group by (State or Session). It is a bordered square button, unmistakably clickable, that
counts the filters that are on. Its menu opens to the right of the sidebar, never over the
list, so the list narrows in view as you pick. Everything that narrows the list reads as one
"Filtered:" row of chips under the search, the state, the grouping when it is by session,
the account and the keyword, each with its ×, and the button counts the same choices. The
grouping stays a per-browser preference rather than joining the URL: it says how you like
to read the list, not what a link should show. The state lives in the URL (`filter=`, `acct=`, `q=`) as before, so a link or
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
