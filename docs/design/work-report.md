# Design: work report — what the fleet did this week, and what is waiting on you

Status: **proposal, nothing built.** It builds on [open loops](open-loops.md) (issue #366),
[plan usage](plan-usage.md) and the pane history in `history.sqlite3`, and it is that
issue's last build step, the model-written summary, grown into a page of its own.

## The problem

Open loops answers "what has stopped moving right now". It does not answer the question a
person running a fleet asks at the end of a week, or first thing on a Monday: what did all
of this add up to? Which pieces of work moved, which ones are one click from done, which
ones quietly ate a week of agent time and have nothing merged to show for it.

Today that answer comes from asking an agent to build it. A hand-built report of exactly
this kind was made recently, and it was useful enough to want every week: totals per
repository, a "Waiting on you" section, a timeline of every PR grouped by piece of work,
and agent hours. It also showed what is wrong with building it that way. It took a long
agent session, every run regroups and rewords from scratch so two weeks are not
comparable, its numbers cannot be checked against anything, and its private content ends
up in whatever directory the agent chose. A mock built from tmux-rc's own data then showed
that most of the report is already sitting in tmux-rc, and named precisely what is not.

## What the report is for

The report exists to produce actions, and the review of the past is in service of that.
So its order is the order of attention:

1. **Waiting on you.** The things only the user can unstick: approved PRs ready to merge,
   reviews asked of the user, PRs with no reviewer, PRs that are red or conflicting,
   drafts to close or keep. This is open loops' Waiting lane plus its Dropped lane,
   regrouped by the action they need rather than by lane, so "five PRs need a reviewer
   picked" reads as one chore. It comes first because it is the only section whose
   reading changes what happens next.
2. **The week in numbers.** PRs opened, merged and closed unmerged per repository, merges
   per day, lines changed. A glance, not an analysis.
3. **Workstreams.** Each piece of work as a heading, a one-line account of where it stands,
   and its PRs on a timeline (opened, merged, still open, abandoned).
4. **Agent hours.** Where the fleet's time went: per day, per workstream, and the panes
   that ran longest.

## Where each section's data comes from, and what is missing

**Waiting on you** is open loops' cached GitHub join, unchanged. One gap: an approval does
not name its approver. The GraphQL query already fetches the latest reviews (it uses them
to tell humans from bots) but drops the author before the row is built. "Approved by a
teammate" versus "approved by the bot" matters to whether the click is safe, so the row
should carry the name. This also improves the existing open loops view.

**The numbers and the timeline** need a week of PRs, and open loops looks at much less:
merged PRs from the last 24 hours and open ones from the last fourteen days, with no
closed-unmerged search at all, and no line counts. Two ways to close that gap were weighed.

- *Widen the open loops poller* to seven days of merges plus closed PRs plus additions and
  deletions. Rejected: that poller runs every fifteen minutes so the lanes stay fresh, and
  a week's history does not change on that cadence. The searches that already hit
  GitHub's timeout on a busy account would get heavier for a section read once a week.
- *One search when the report is built*: the user's PRs updated in the window, with the
  same fields open loops fetches plus `createdAt`, `closedAt` and line counts, paged the
  same way. **Chosen**, because its cost is paid once per report and reuses the existing
  query fragments rather than adding a second definition of a PR.

Issue #366's daily snapshots would also give a week, but only for weeks after they started
recording, and they record lane state rather than every PR touched. They stay the right
home for "Moving as a diff"; the report does not depend on them.

**Agent hours** come from the pane history that already feeds the activity chart:
`snapshot_intervals` joined to `inventory_payloads` gives each agent pane's state over
time, cut at local midnight. This is time a pane was seen running on screen, not time
between transcript messages, which is what the hand-built report counted. The two
disagree by a lot and neither is wrong: screen time is what the fleet looked like, and it
covers every agent tmux-rc classifies; transcript time needs the optional
`agent-history` index and only covers the harnesses it reads. The report uses screen time
because it is always there, labels it as such, and shows the hours the daemon was down as
uncovered rather than as idle.

Two gaps here:

- **A closed pane loses its title.** The history payload keeps a pane's uid, session and
  tool, and the title lives in `pane_checkpoints`, which deletes a pane's row when it
  closes. So the panes that did the most work last week, long since closed, show as bare
  ids. The fix is to record the title in history: either in the payload itself or in a
  small uid-to-title table written when a title changes. The table is preferred, because
  payloads are deduplicated whole and a title that changes every few minutes would defeat
  that.
- **A closed pane loses its PRs too.** Hours per workstream sum the panes whose PR
  associations fall in the workstream, but history records no associations, the watcher
  retires a reference once its PR merges or closes, and a closed pane's remaining ones go
  with it. So the work that finished last week, which is most of what a weekly report is
  about, would have no PRs to attribute its hours to. History needs a durable uid-to-PR
  record, written when an association first appears and never retired; hours from before
  it existed are shown unattributed rather than guessed. A pane associated with many PRs
  (a session that coordinates others) cannot be split honestly; it is attributed to its
  most frequent workstream and the report says so.

**Plan usage** already stores each account's weekly percentage, which gives a cost line
for free ("this week used most of two accounts' weekly limits"). Optional; it costs no
new data.

## What the model does, and what code does

The project's rule is that judgement belongs to the model and evidence belongs to code
([thin LLM UI layer](thin-llm-ui-layer.md)). The report has exactly two judgements in it.

**Grouping PRs into workstreams by topic.** A design doc, its five implementation PRs, a
follow-up fix opened a week later in another repository and the session that drove them
are one piece of work, and nothing mechanical says so. Open loops groups today by stack,
then a `workstream:` label, then the `TMUXRC_WORKSTREAMS` keyword map. In practice
almost nobody labels, so most "workstreams" are a single PR, and the keyword map is the
substring rule this project keeps deleting: it grows a word per miss and is wrong in ways
nobody notices. The proposal is that the model groups, given PR titles, branch names,
pane titles and session summaries (`/api/digest`), and that the keyword map goes away,
both here and in open loops. The stack and an explicit label stay as facts the model is
given, and code keeps its answer consistent with them (a stack is never split across
workstreams), the same way code keeps an offer consistent with the screen. Whether a
label should bind the model or only inform it is an open question below.

**One line per workstream**: where it stands and what is next ("design approved, three of
five implementation PRs merged, the last two wait on review"). Optionally one line atop
Waiting on you.

Everything else is plumbing, and stays in code: every count, date, hour and line total,
every PR's state, which lane a PR falls in. The model never states a number; the prose may
only name PRs it was given, and code drops any reference it cannot resolve. This is what
issue #366 meant by "shipping prose first would hide gaps in the data behind confident
sentences": the rows came first and are now trusted, so the prose can sit on top of them
without being the source of anything.

The grouping and narrative get eval samples like the classifier does, invented from
scratch: a stack, a doc with its implementation, two unrelated fixes with similar words in
their titles.

## Cadence and cost

**One model call per report.** The input is a week of PR titles and states, pane titles
and the latest session summaries: a few thousand to a few tens of thousands of tokens on a
busy week, a rounding error against the classifier's continuous bill. Because it runs once
a week, not every tick, it can afford a stronger model than the classifier uses.

The report is built on a schedule (weekly, at a configurable local time) and on demand,
and the result is stored: the grouping and prose are frozen with the time they were
written, while Waiting on you is read live from open loops each time the page opens, so
the first section is never a week stale. Freezing a report does not by itself make two
weeks comparable: a workstream that started last week would be regrouped, and maybe
renamed, once its early PRs leave the window. So each stored report keeps its PR-to-
workstream assignment, and the next grouping call is given the previous assignments as
input, so a follow-up PR joins the workstream it continues and the name carries over;
the model may still split or close a workstream, and the report says when it did. The
stored reports are what a "last week" link reads; keeping the last dozen is small.

A daily variant is the same report over a one-day window. It is not a second feature, but
whether a day is worth a model call, rather than just the numbers, is an open question.

## Where it shows

A **"This week" page of the dashboard** (`#view=week`), next to the open loops page and in
its style: reached from the open loops card, not as a new card on the dashboard, which
already holds the fleet chart and usage. Rows carry the same Open buttons as open loops,
so a PR in Waiting on you lands in the pane that owns it.

An **optional morning push**, which is issue #366's planned morning push, not a second one:
on the report's day it links the report and carries the Waiting on you count; on other
days it is the open loops brief. Two pushes on one morning would be the noise the push
design warns about.

## Privacy

Everything stays on the machine. The report is stored in the local history database and
served only by the daemon. The model call sends PR titles, pane titles and session
summaries to the same provider that already reads pane content for the classifier, so it
adds no new recipient. There is no share link and no export by default: the hand-built
report's contents (colleagues' names, unreleased work, other repositories) are exactly
what should not leave by accident. Sharing, if wanted, is a deliberate user action.

## Alternatives considered

**Keep asking an agent for it.** This is how the first report was made, and it proved the
shape. As the standing mechanism it fails on everything a report is for: it costs a long
agent session each time, the grouping is redone from nothing so weeks cannot be compared,
the numbers come from an agent's ad-hoc queries and cannot be checked, and the output lands
wherever the agent puts it. Issue #366 rejected a standing agent for the brief on the same
grounds; here the deterministic join is even larger, and only the grouping and prose need a
model.

**A static export**, an HTML file written nightly. Cheap, and it would make sharing easy,
but it is stale the moment it is written, it cannot carry Open buttons into live panes, and
a file of private work sitting on disk is the leak the privacy section is avoiding. If
sharing becomes a need, an export can be added later from the stored report.

**GitHub's own insights.** Pulse and contribution graphs are per repository or per
profile, know nothing of sessions, panes or agent time, and cannot say which PR is waiting
on the user because of a pane that went idle. They answer "what happened in this repo",
not "what did my fleet do and what is waiting on me", which is the join only tmux-rc has.

**Keyword grouping, improved.** A better map, or embeddings with a similarity threshold.
Rejected for the reason above: it is a rule standing in for a judgement, and every
version of it so far needed a fix per counter-example.

## Open questions for the user

- **Weekly, daily or both?** And which day and time the weekly one is built.
- **Does a `workstream:` label bind the model's grouping**, or only inform it? Binding is
  predictable; informing lets the model fix a stale label.
- **Should the model's grouping replace open loops' keyword grouping too**, or only the
  report's? Replacing it means the open loops view also costs a model call when its PR set
  changes.
- **Agent hours: screen time only**, or also transcript time when `agent-history` is
  installed, shown side by side?
- **Which repositories** count: everything the user authored in, or a configured list?
- **Is a cost line from plan usage wanted**, or is that a different page's question?

## Build order

1. Plumbing that fixes gaps open loops also has: approver names on rows, and pane titles
   and PR associations kept in history after a pane closes or its PR merges.
2. The report-time week search and the "This week" page with numbers, Waiting on you and
   agent hours, with stack-only grouping. Useful with no model at all.
3. The model call for grouping and narratives, with eval samples, and the stored report.
4. The weekly push, folded into issue #366's morning push.
