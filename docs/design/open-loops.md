# Design: open loops — what has stopped moving, by workstream

Status: **step 1 implemented** (`openbus/open_loops.py`, `GET /api/open-loops`). The
`/m` view, label and keyword grouping, daily snapshots and the morning push are later
steps of issue #366, which holds the product case. This note records how the join is
built and why.

## What step 1 is

One read-only endpoint that answers "which pieces of work have quietly stopped, and what
is the one action that unsticks each", in three lanes:

- **Waiting on you**: a pane blocked on you (today's Needs you, unchanged); your PR that
  is approved, green, mergeable and targets the default branch; your green PR in a
  repository that requires no review (the only step left is your click); a review that
  someone asked of you.
- **Moving**: what merged, what was pushed and what was reviewed in the last day.
- **Dropped**: your PR that needs a review and has nobody asked; a red head; a stacked PR
  whose base merged or was deleted; a PR that conflicts with its base; a PR untouched for
  three days; a pane idle for a day over uncommitted changes in a worktree only it sits
  in; a worktree no pane is on, holding uncommitted changes or commits that exist on no
  remote.

Rows carry reason codes rather than prose, so the view can word them and a test can pin
them. Each PR row carries the panes that own it, so step 2's Open button has a target; a
row with no pane says so by having none.

## The three sources and how each is read

**GitHub, from one cached query.** Everything comes from a single GraphQL request: your
open PRs, review requests of you, what merged in the last day that involved you, and the
PRs the watcher's associations name. It runs every 15 minutes on a background task, the
same shape as plan usage's poller, and the endpoint only ever reads the cache, so opening
the view never touches the network and a burst of requests costs GitHub nothing. The
issue suggests an hourly cadence; a quarter hour was chosen because the cost is one
request whatever the number of PRs, and the Moving lane is only as fresh as the cache.
The response states when the cache was filled and whether the last refresh failed, and a
failed refresh keeps the previous answer rather than blanking the lanes.

Per-PR `gh pr view` calls, as the title lookup makes, were rejected: thirty PRs would be
thirty processes and thirty rate-limit hits per refresh, and the review, check and stack
fields the lanes need are all available in the one query.

**Panes, live.** Pane state comes straight from the watcher on each request, which is
free, so Needs you rows are as current as the cards. The pane's PR associations from the
classifier (`pr-session-associations.md`) are reused as-is to find which panes own a PR;
nothing is re-derived and no pane text is parsed.

**Worktrees, from local git, on the same cadence.** The repositories that matter are the
ones panes sit in; each one's own worktree list then finds every worktree wherever it
lives (under the repo, beside it, or in a temp directory). Every git call runs without
optional locks, so a status check can never take an index lock an agent then trips over.

## Decisions worth knowing

**A horizon, so the lanes hold loops rather than an archive.** A real fleet had several
times more open PRs than a person can be said to have in flight, most untouched for
months. Listing them all as "no activity" would bury the dozen that dropped this week.
Open PRs and worktrees idle past fourteen days are counted (`older_open_prs`), not listed,
so nothing disappears silently.

**"No reviewer requested" only where review is required.** A repository without branch
protection reports no review decision at all; there the missing reviewer is not a loop,
and a green PR is simply waiting for the author's merge. Bot reviewers (an automated
review request or comment) do not count as someone who will answer.

**Stacked PRs wait on their base, not on you.** A green PR whose base is another PR's
branch is not ready to merge, whatever its review state says, so it is never offered as
a click. It still shows up in its workstream when it moves or drops.

**Dirt belongs to a pane only when the pane is alone in the worktree.** Many panes sit in
a repository's main checkout while their agents work in worktrees elsewhere. Flagging
every idle pane there for the checkout's uncommitted files repeated one finding a dozen
times and blamed sessions that never touched the files.

**Unpushed work is judged without GitHub.** Commits that exist on no remote branch are
drift. Two cases are excluded because they are almost never lost work: a branch whose
upstream was pushed and then deleted (the usual trace of a merged PR, whose squashed
commits live in main under other hashes), and a detached HEAD (integration merges,
bisects). A worktree whose branch is the head of a PR that merged is skipped unless it
still holds uncommitted changes.

**Grouping is the stack only.** A PR whose base is another PR's head joins that PR's
workstream, transitively, and the group takes its root's title. It needs no input from
anyone and is right whenever it fires. It cannot join a design doc to its implementation
or work that spans repositories; that is what step 2's label and keyword signals are for.
Rows that join no PR (a pane waiting on a question, a branch with no PR) fall into an
ungrouped bucket rather than being guessed into one.

**Moving is a fixed day until snapshots exist.** The issue defines Moving as the diff
against the previous brief. Until step 3 stores briefs, "the last 24 hours" stands in.

## What it does not do yet

- A session that finished and asked a question in prose is invisible here: it is idle,
  not waiting. Catching it needs the finished transition from issue #187; this endpoint
  will pick it up from the classifier's output once that exists, not from pane text.
- A PR waiting on someone else's review is in no lane until it goes stale. "Waiting on
  others" is a real question but not one of the three lanes.
- Associations are retired once their PR merges, so a merge only appears under Moving
  when the search for merges that involved you finds it.
