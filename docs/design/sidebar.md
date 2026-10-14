# The desktop sidebar

Status: implemented (`web/m/sidebar.js`, `web/m/app.js`, `web/m/index.html`).

The wide layout's sidebar was a header of icons (☰, Dashboard, Chat, the mic, the bell), a
"New window" row, the search box, a filter dropdown on the list's own line, and a foot with
Live, the theme and Docs. Every control was one click away, and none of them said what it
was. The redesign came out of a mock loop with the user; these are the decisions it
settled and why.

## Places, settings, and the list

The sidebar holds three kinds of thing, and each now has one home.

- **Places** you go are two rows under the header, always there: Dashboard and Settings.
  Chat and Voice are not places but toggles (a panel that opens beside you, a mic that stays
  on), so they are small icons in the header, between the name and the collapse, and being on
  shows as a tint on the icon. As rows they read as selected navigation: an open Chat was a
  big green rectangle in the list of places. That left two places, which need no folding, so
  the "··· More" row that once gave way to the rest is gone; it, and a chevron on the Dashboard
  row before it, were ways to hide rows that no longer need hiding.
- **Settings** are things you set once: Accounts, Notifications, Theme, Plan usage, the
  phone's sort order, and Docs. They left the ☰ dropdown and the foot for one Settings, the
  same content on a phone (the header's gear, a sheet) and on desktop.
- **The list** has its own block: a "Sessions" title with its pane count beside it, then at the
  right the sub-agents count (a button, muted, so the green is the +'s alone) and, fixed at the
  far right, the green + (new window), then search with the filter button beside it. Search and
  filter share a row because both narrow the list. The list's own line (State / Session ·
  Sub-agents) is gone: grouping is a rarely changed choice, so it joined the filter menu as
  "Group by", and the sub-agents count, which also switches their lines under each row (its eye is open while
  they show, slashed while not), sits
  in the title row.

Between the header and the usage strip everything scrolls as one, so the places scroll out
of the way as you page through panes, and the Sessions block (its title and +, search and
the filter, the filter chips) sticks under the header with a hairline once it has. The
header, with the collapse, stays put: scrolling it away hid the way to fold the sidebar. The
usage strip stays at the foot.

The header is just the logo and the name, with Live as the logo's dot (green, or amber
while it reconnects or the watcher stalls) and a dim collapse at the right, as on
claude.ai. A status word in the foot cost a row to say what a dot says.

## The filter opens beside the list

The filter grew out of the shipped one (#402). Its menu has State and, with two or more
accounts, Account, both multi-select: a pane shows if it matches any
checked state and any checked account, and none checked is All. One-of was wrong for both:
"running or recently finished" and "these two accounts" are natural questions. The menu stays
open while you check, since one click is rarely the whole choice.

State's options are the list's sections, in their order and colours: Needs you, Working, Just
finished, Idle, each showing exactly that section's panes. They were Running, Recent and Needs
you, which named different sets from the sections beside them: Recent was any pane not idle,
so it also held the working ones and those that needed you. The phone's tabs use the same
words (All, Working, Just finished, Needs you), and the old `filter=running` and
`filter=recent` links read as Working and Just finished.

Then Group by (State or
Session) and Sort by (Last activity, or Session order: tmux's own session, window, pane order,
which reads best grouped by session).

The button is a bordered square, unmistakably clickable, and it opens the menu to the right
of the sidebar, never over the list, so the list narrows in view as you pick. Everything that
narrows the list reads as one row of chips under the search, one per state, per account and
for the keyword, each with its ×, grouped under small labels named as the menu names them
(State, Account, Search: chips of one look read as one kind of thing), and a Clear all once
two are on (the menu has one too); the button counts the same and its tooltip names them. The
chips are the rows' own account chips, small, with no "Filtered:" label: larger chips with a
label took two lines for three filters. Each state has its list colour (amber Needs you, green
Working, muted Just finished, neutral Idle) on its chip, its dot in the menu's State and its section's count, so the three
read as one thing. Grouping and
sorting are not filters: they change how the list reads, not what is in it, so they neither
count nor get a chip (a "1" on the button for grouping by session read as a hidden filter).
They are per-browser preferences rather than URL state, for the same reason, and the sort is
one setting with the phone's Settings. The filters live in the URL as comma lists
(`filter=working,finished`, `acct=`, `q=`), so a link or Back restores them and an old
single-value link still reads (as do the old names, `running` and `recent`).

Panes that need you are the one thing a filter must not hide: an account or search filter
once left "Nothing needs your attention" on screen while another account's pane was asking.
So whenever State is All or includes Needs you, every pane that needs you shows in its
section, and those the account or search filter leaves out are dimmed and marked "outside
filter", so the filter still reads as honest. Picking only Working (say) is an explicit
choice, so nothing is forced in then. Settings › Appearance can turn it off. An empty list
says what was filtered ("No Personal panes need you") and, if panes it hides need you, offers
them ("5 panes on other accounts need you · Show").

The phone keeps its tab bar for the state, one at a time (tabs are one-of by nature), and has
the same filter button beside its search, opening the menu as a bottom sheet with Account,
Sort by and Clear all. It used to have a sideways-scrolling row of every account under search,
which hid the one that was on: a list of one account's panes showed no sign of being
filtered. Now the active filters' labelled chips sit under search as on the wide list (under
the header on the dashboard), and a narrowed list ends with "8 Codex panes · Show all
accounts".

## Panels dock beside the sidebar

Settings and New window open as a panel docked to the sidebar's right edge, over the left of
the main column, with a sticky title and ×. The right of the screen is usually busy (the chat
column, the pane's overview), and the terminal in the middle matters least while you are
choosing something; docking also keeps the panel next to the control that opened it. The
panel is non-modal, so the list stays live beside it; Escape or a click elsewhere closes it.
One helper does this for both, and a phone keeps its bottom sheet. A centred modal was
rejected: New window had grown taller than the screen and scrolled its own title away.

New window is compact for the same reason, but not at the cost of saying how it works (a
first cut hid every explanation in tooltips, and read as cryptic): a one-line subtitle says
what it does; the accounts are a two-column grid of small chips, "+ Add account" the next
cell, with a hint naming the tool they apply to; the session's default is a sentence with a
checkbox ("Use Work for every new window in qs") and a muted line on what happens otherwise,
not a "Default for … Not set" select; and each tool is a 52px row with a muted line, the
account it starts on where it has several, then its model or what it is. Rows the account
does not apply to carry no chip. It fits unscrolled at 900px tall; shorter, the body scrolls under
the fixed title.

## The collapsed rail keeps working

Folded to a 60px rail, the sidebar used to show its head and the live panes. A rail with
nothing but "expand" made folding a trap, so the rail now keeps everything that works as an
icon, top down: expand, the green +, search, the filter, Dashboard, Chat, Voice, every pane
the list shows in its order, and Settings at the foot. The + opens New window beside the
rail without expanding it, and the filter's menu opens beside it too with its count. Search
expands the sidebar and focuses the box, since a query needs the list. Each pane icon has the
list's own single running ring on its logo, an amber dot when it needs you and, with several
accounts, an account dot in the opposite corner; hovering one shows the list's hover card,
with the account's chip. The open pane is a soft filled square: the rows' left bar and
border, squeezed round an icon, looked lopsided. The
pane icons scroll between the places and Settings.

The idle-count button ("+12") is gone: the rail lists every pane, as the list does.

## The phone

The phone's places were already on screen (Dashboard in the tab bar, Chat and the mic in the
header), so its ☰ became a gear that opens the same Settings. Its tab bar is still the state
filter, so the chips under its search leave the state out, and its filter button opens the
rest of the menu (above).
