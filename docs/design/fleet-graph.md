# Design: the Fleet graph

Status: **phase 1 implemented, with the observatory look** (`web/m/graph.js`, the
`worktree` field on `/api/state`). **Experiment:** real flows and a hub toggle (below).
The Files lens, Lineage, conflict links and replay are deferred (below).

## What it is

The fleet as a graph you can move around in. Every pane is a node, its running sub-agents
are satellites, and hubs group the panes by a lens:

- **Session**, the default: the tmux sessions, as every other surface already groups them.
- **Account**: the agent account each pane draws on. With one account this lens says
  nothing, so it is hidden until there are several.
- **Repo / dir**: the repository a pane works in, then the directory inside it.
- **Worktree**: the git checkout a pane sits in. A checkout with two agents busy in it is
  ringed in amber, since that is where two agents' edits collide.
- **Tool / model**: the agent, then the model it runs.

You pan by dragging (one finger on a phone), zoom with the wheel or a pinch, and Fit puts
the whole graph back in view. Tapping a pane opens a popover with its state, model,
account, tokens when known, what it is doing now, how long it has been at it and where it
works, plus Open pane. Tapping a hub folds it into a single counted ring, and tapping it
again unfolds it.

Fit wears a crosshair rather than the four corners it first had: on a phone those corners
read as "fill the screen", and a tester pressed it expecting exactly that. That button now
exists beside it. Full screen pins the view over the page with CSS, inside the safe area;
Esc or the button leaves it, and it refits both ways. The toggle heads the control column in
the top-right corner and stays there in both states, its icon swapped, so the way out is
exactly where the way in was. A phone once opened the view full screen by default, with
that button closing the view; in use it lost the app's title bar and back button, and a
close button where the full-screen toggle belonged sent people hunting, so the view now
opens inline like every other drill-in and full screen is one tap away. The Fullscreen API was passed over because an iPhone grants it to
video only, so it would add a code path that does nothing on the device that wanted this.

## The look

The first version read as a diagram: thin grey lines, small icon circles, plain labels. A
mock put five looks over the demo fleet, each one the real graph code with its renderer
swapped, so pan, pinch and the popover behaved as they would ship:

- **A, Observatory**: a fixed navy canvas, a glowing hub, curved edges carrying particle
  streams, a monospace HUD, in a warm palette (gold running, coral needs you).
- **B, Theme-aware**: the same craft on the app's own colours, in light and dark.
- **C, Observatory + Pulse**: A, plus a spark up the edges and an event feed on every state
  change.
- **D and E**: A and B with a detail layer (below).

**E shipped.** A made the prettier screenshot, but its gold-for-running contradicts the
rest of tmux-rc, where amber means *needs you*, and a navy slab inside a light app jars on a
phone in daylight. E keeps the state colours identical on every surface and works in both
themes; A survives only as the **dark canvas** toggle (the moon), which recolours E's ground
rather than its states, per device. C's sparks and feed were dropped: the most motion and
code for the least information, and a pane's events already have a home in its timeline.

The detail layer is what answers "show me more" without clutter: a node's size and the arc
round it are the context it has used, hot past 80%; a badge counts sub-agents, every one not
finished or idle, each satellite in its state's colour (the sidebar lists only the busy
ones, but a pane waiting on six background agents should show six); a needs-you
pane pulses and says how long it has waited; idle panes fade with age, so stale ones
recede; a dashed ring marks the account once there are several; running edges carry the
current verb and its elapsed time; hubs sum their running, needs-you and cost; the HUD
counts sessions, running, needs you, sub-agents, tokens a second (the sparks' rate), cost and the oldest
pane; and two callouts, Longest waiting and Most expensive, glide to the pane and open it.
More words arrive as you zoom in: the headline or the question, then model, context,
tokens, cost and account. The hub sparkline the mock invented was left out until a
per-session history query exists to feed it, rather than shipped as decoration.

Glow is pre-rendered sprites stamped onto the canvas, never canvas blur, and sparks are
stateless (a position is a hash of the index and the time), so nothing is allocated per
frame.

## Every moving mark is something that happened (experiment)

The look shipped with particle streams on every edge leading to work. They were pretty and
meant nothing: their density was a constant per kind of edge, and they flowed along the
spokes of a "tmux" hub that nothing actually flows through. A tester asked the right
question: does stuff really flow in and out of tmux? It does not. So the streams are gone,
and what moves now is only what the daemon already knows happened:

- **Tokens being written** throw sparks off a pane's rim (a sub-agent's run up its line to
  its pane), one for every 20 tokens a second. The rate is the rise in the pane's token
  counter over the last 20 seconds, read from the polls the graph already gets; a smaller
  count is a new turn's, and a parse that showed no counter is no reading rather than a
  reset. A pane that stops writing goes still within the window. Sparks scale with a number
  instead of a constant, so a busy fleet looks busy and a quiet one is still.
- **You** are a node at the foot of the canvas. A pulse runs from you to a pane when
  tmux-rc sends it input (a message, an answer, keys: `input_at`, stamped where every input
  already bumps the pane's input generation), and from a pane to you, amber, when it comes
  to need you. Panes you typed into in the last ten minutes keep a fading line to you.
- **Scratch pages** hang off the pane whose meta.json names it, as small sheets, with a
  turning arc while one is being made (`GET /api/scratch/panes`, the gallery's own listing
  without its transcript search). Publishing sends a pulse from the pane to the sheet; a
  page that had been promised then sends a green pulse to you, the Ready push.
- **Sub-agents** starting and handing back their work pulse between the pane and the
  satellite, from the parser's roster changing between polls.

Every one of these is a difference between two polls, so opening the graph replays
nothing: the first poll is the baseline. That keeps the code free of history queries, at
the price of an empty-looking graph for the first seconds, which is honest. Push
notifications themselves were not drawn, because whether one was delivered depends on
devices and presence; what is drawn is the ask or the landing that would prompt one.

The hub became an option (the network button) rather than the frame. With it, its spokes
group panes and are drawn still. Without it, each group of the current lens is a disc of
its panes, sunflower-packed with those that have satellites on the rim, and the discs are
laid in rows shaped like the room; the only lines left are ones that carry something. A
force layout was passed over: it never settles to the same place twice, and a graph that
re-shuffles on every poll defeats the glide that keeps identity across updates. Without
the hub, a lens shows only its first level (repository, not directory), which costs the
Worktree lens its two-agents-one-checkout ring.

What is still missing: a running pane whose agent shows no token counter (omp today)
writes no sparks even while busy, and input sent at the desk rather than through tmux-rc
draws no line from you, since the daemon cannot tell it apart from the agent's own output.

## Delegated work is work

A pane can sit idle at its own prompt while the background agents it started do the work.
The first version drew that pane as idle and its sub-agents as specks. On a phone, a
session with two agents busy read as "2 idle" and the HUD said nothing was running while
hundreds of thousands of tokens were in flight. Now a pane whose sub-agents are at work
counts as waiting, which is what it is doing: it is coloured, ringed and counted that way
everywhere in the graph. A working sub-agent is drawn as a small pane of its own, about six
tenths of its parent's size, glowing in its state's colour, with its job and elapsed time
beside it where there is room. Finished or idle ones stay small. The parent's own words move
out past its satellites rather than covering them.

For the HUD, we chose counting the parent pane as running over bringing back a separate
sub-agents count. "Running" is the question people actually ask the HUD ("is anything
happening?"), and a pane that is waiting on its agents answers yes. The phone's row of
counts also has no room for a sixth figure. The exact sub-agent number stays in each hub's
sum, on the badge and in the popover, which is where the detail is wanted.

## Words that give way

On a phone at fit zoom there is room for perhaps a dozen labels, and labels at the screen
edge were being cut off. Rather than hide names by zoom level alone, every label is queued
with a rank and drawn after the marks, most wanted first: the selected pane and panes that
need you (always), hub names, hub sums, working panes, then the rest. A label that would
land on one already drawn, or under the HUD or its controls, is dropped; a pane's label
that runs out of room on its side flips to the other side of the node; one near an edge slides inward
while its node is on screen, and one wider than the screen is cut. A hub whose full sum has
no room says only its most urgent part ("2 need you"). The result is that the fitted phone
view names every pane that needs you and most of the working ones, and nothing overlaps or
falls off the edge, without a layout solver: a greedy pass over a handful of rectangles is
cheap enough to run every frame.

The layout also takes the shape of its room. The ring of panes is an ellipse stretched to
the space Fit frames it in, so a tall phone screen fills top to bottom instead of leaving a
band empty above and below a circle sized to its width.

## Why it is a drill-in, not the dashboard

The prototype weighed a graph as the dashboard's main picture and rejected it. At card size
the graph mostly repeats the session cards and the list: you can see that a session is busy,
but not which pane needs you, and nothing in it can be read without zooming. What the graph
adds is regrouping. The same panes can be viewed by repository, checkout, account or model,
and no list can show that.

So the Dashboard gets a compact card (the Session lens with no pane labels, still live), and
the card opens the full view, as Running by hour and Open loops already do. On a phone, ☰
lists it beside the Dashboard. A view of its own also gives the graph the whole screen,
which pan and pinch need, and keeps the dashboard's scroll free of a canvas that would
capture it.

## Why a hand-drawn canvas

The app already ships ECharts, whose graph series has pan, zoom and touch built in. It would
fight the one thing the graph is for, though: switching lens glides every node from its old
place to its new one, and folding a hub collapses its subtree into it. Those transitions
need each node to keep its identity across layouts, and that is easy to own in a small
canvas and awkward to bend a chart library into. A canvas is invisible to the keyboard and
to screen readers, so every node is mirrored as an offscreen button: focusing one selects it
on the canvas, and Enter does what a tap does. The cost is code with no JS test harness
behind it, so the interactions are checked in Playwright against the demo fleet (mouse,
wheel, CDP touch pinch and pan, tap, fold, lens) and by hand on a phone.

## Live without jank

The graph updates from the same `/api/state` poll as the rest of the app. A poll that
changes nothing structural (the same panes, groups and satellites) only recolours the
nodes. Nothing moves and the view keeps its pan and zoom. When a pane or sub-agent appears
or leaves, the existing nodes glide to their new slots and the new ones ease in from their
hub, again without re-framing the view. Only a lens change re-fits. Sparks and pulses stop under
prefers-reduced-motion, and frames stop altogether while nothing is writing, travelling or
asking, or the canvas is off screen.

## Where Repo and Worktree come from

Open loops already scans every worktree of every repository a pane sits in, on its own
cadence and off the request path. The state endpoint now joins each pane's cwd to that scan
(the innermost worktree holding it) and adds the repository name, worktree path and branch.
That reuses work that already runs, and adds no git process per poll. The tradeoff is
freshness. A pane that moves into a repository the scan has not seen yet sits under "no
repository" until the next scan, at most a quarter of an hour later. Running a separate git
lookup per pane on every poll was rejected: it would put subprocesses on the hot path for a
view that is opened now and then.

## Deferred

- **Files lens.** This lens would show panes and the files they touched, with two writers
  of one file marked. It needs new collection: the changed paths from the worktree scan's
  `git status` (today only their count is kept), and edits attributed from each agent's
  transcript, for two agents in one checkout.
- **Conflict links.** These are red links between panes that write the same file. They build
  on the same collection, and the warnings belong on the cards and rows first, where they
  are seen without opening a graph.
- **Lineage.** This lens would show a conversation's history: resumed, copied to another
  account, forked or restored. It needs a parent link per session, which is not recorded yet.
- **Replay.** This would be a scrubber that recolours the graph from history. History keeps
  states but not titles, tools or files, so a replayed popover would mostly say "not
  recorded". A scrubber on the Activity chart answers "what was running then" more cheaply.
