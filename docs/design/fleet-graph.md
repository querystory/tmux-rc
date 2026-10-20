# Design: the Fleet graph

Status: **phase 1 implemented** (`web/m/graph.js`, the `worktree` field on `/api/state`).
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
canvas and awkward to bend a chart library into. The cost is code with no JS test harness
behind it, so the interactions are checked in Playwright against the demo fleet (mouse,
wheel, CDP touch pinch and pan, tap, fold, lens) and by hand on a phone.

## Live without jank

The graph updates from the same `/api/state` poll as the rest of the app. A poll that
changes nothing structural (the same panes, groups and satellites) only recolours the
nodes. Nothing moves and the view keeps its pan and zoom. When a pane or sub-agent appears
or leaves, the existing nodes glide to their new slots and the new ones ease in from their
hub, again without re-framing the view. Only a lens change re-fits. Particles flow only along
edges that lead to work in flight. They stop under prefers-reduced-motion, and frames stop
altogether while the canvas is off screen.

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
