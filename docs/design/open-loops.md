# Design: open loops — what has stopped moving, by workstream

Status: **steps 1 and 2 implemented** (`openbus/open_loops.py`, `GET /api/open-loops`,
`web/m/loops.js`). Daily snapshots and the morning push are later steps of issue #366,
which holds the product case. This note records how the join is built and why.

## What step 1 is

One read-only endpoint that answers "which pieces of work have quietly stopped, and what
is the one action that unsticks each", in three lanes:

- **Waiting on you**: a pane blocked on you (today's Needs you, unchanged); your PR that
  is approved, green, mergeable and targets the default branch; your green PR in a
  repository that requires no review (the only step left is your click); a review that
  someone asked of you.
- **Moving**: what merged, what got new commits and what was reviewed in the last day.
  "New commits" is the head commit's date, not the push: GitHub keeps no push time on a
  PR, so an old commit pushed today does not count. Step 3's snapshots replace this with a
  diff of head commits, which does see it.
- **Dropped**: your PR that needs a review and has nobody asked; a red head; a stacked PR
  whose base merged or was deleted; a PR that conflicts with its base; a PR untouched for
  three days; a pane idle for a day over uncommitted changes in a worktree only it sits
  in; a worktree no pane sits in or owns (through the PR whose head is its branch), holding
  uncommitted changes or commits that exist on no
  remote.

Rows carry reason codes rather than prose, so the view can word them and a test can pin
them. Each PR row carries the panes that own it, so step 2's Open button has a target; a
row with no pane says so by having none.

## The three sources and how each is read

**GitHub, from a few cached GraphQL requests.** Your open PRs, review requests of you,
what merged in the last day that involved you, and the PRs the watcher's associations
name. The refresh runs every 15 minutes on a background task, the same shape as plan
usage's poller, and the endpoint only ever reads the cache, so opening the view never
touches the network and a burst of requests costs GitHub nothing. The issue suggests an
hourly cadence; a quarter hour was chosen because the cost does not grow with the number
of PRs, and the Moving lane is only as fresh as the cache. A pane that gains an association the cache has
not looked up triggers a refresh within a minute rather than waiting out the quarter
hour; at startup that is what picks up the associations the classifier restores a tick
after the panes appear. The response states when the
cache was filled and whether the last refresh failed, and a failed refresh keeps the
previous answer rather than blanking the lanes.

The first version asked for everything in one request. On a busy account that request
ran into GitHub's own timeout (an intermittent 502 after about ten seconds), because
mergeability and check state are computed per PR. So each search is fetched a page of
forty at a time, the associated PRs in chunks of twenty, and each request is retried
once: half a dozen small requests per refresh instead of one that fails whole. Per-PR
`gh pr view` calls, as the title lookup makes, were rejected for the opposite reason:
thirty PRs would be thirty processes and thirty rate-limit hits, when the review, check
and stack fields the lanes need come back with each search page.

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
Open PRs and worktrees idle past fourteen days are counted (`older_open_prs`,
`older_worktrees`), not listed, so nothing disappears silently. A worktree's last activity
is the later of its last commit or staging and the last edit to a file still uncommitted,
untracked files included, so an old branch with fresh edits is not mistaken for abandoned.

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

**A worktree is owned through its PR as well as by a pane sitting in it.** Most panes sit
in a repository's main checkout and their agents work in worktrees elsewhere, so a cwd
match alone would call nearly every worktree abandoned. When a pane is associated with the
PR whose head is the worktree's branch, that session owns the worktree, and the PR row
already carries it.

**Unpushed work is judged without GitHub.** Commits that exist on no remote branch are
drift. Two cases are excluded because they are almost never lost work: a branch whose
upstream was pushed and then deleted (the usual trace of a merged PR, whose squashed
commits live in main under other hashes), and a detached HEAD (integration merges,
bisects). A worktree whose branch is the head of a PR that merged is skipped unless it
still holds uncommitted changes.

**Grouping: the stack, then a label, then the user's keywords.** A PR whose base is
another PR's head joins that PR's workstream, transitively. It needs no input from anyone
and is right whenever it fires, so it runs first; a parent that no search returned (a
teammate's PR, or one of yours from before the horizon) is looked up by its head branch,
up to three levels toward the root, so the stack still finds its root's name and the other signals only name or merge
whole stacks. A `workstream:<name>` label on any PR in a stack names it, and stacks (in
any repository) with the same label become one workstream: this is how a design doc and
its implementation, or work across repositories, come together. Last, an optional
`TMUXRC_WORKSTREAMS` map from a name to words found in titles or branch names catches what
nobody labelled; it is substring matching and the least reliable, which is why it is last
and opt-in. Rows that join no PR (a pane waiting on a question, a branch with no PR) fall
into an ungrouped bucket rather than being guessed into one.

## The view

Open loops sit on the dashboard, below Needs you, rather than as a new screen: the
dashboard is already where both phone and desktop go to ask "what is the fleet doing",
and Needs you is the first lane's existing half, so the pane rows of Waiting on you are not
repeated there. Each lane folds, Moving starts folded because a busy day fills it, and a
workstream holding a single PR drops its heading, which would only repeat the title. A
row's pane buttons open the owning window the same way a sidebar row does; a row with no
owner says "no pane" in the warning colour, because that is the finding. The view polls
once a minute alongside plan usage, since both are server-side caches.

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
