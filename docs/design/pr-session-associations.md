# PR/session associations

## Goal

When a request names a pull request — for example, “review landed on qs-app PR 4955;
please address it” — tmux-rc should know which live pane already has that work's
context. The same data should later support a dashboard of PRs currently being worked.

This is pane/session tracking in the core watcher. It is independent of Live Mode and
does not belong in agent history. Live Mode is one consumer of the result.

## Meaning

An association means the pane's session actually worked on the PR: it implemented,
created, or updated the change; addressed review feedback; reviewed it; tested or
debugged it; or actively shepherded its checks or merge.

A PR does not become associated merely because its number or URL appeared. Open-PR
lists, searches, dashboards, notification summaries, triage, comparisons, examples,
related changes, release notes, and logs are not work on those PRs. In particular,
`gh pr list` can fill a screen without adding a single association.

False positives are costly because associations are sticky for the pane lifetime, so
the classifier is deliberately conservative: ambiguity means no association.

## Data flow

```text
tmux capture + recent frames + local repository context
                         │
                         ▼
           Flash-Lite semantic classifier
              `working_prs` evidence
                         │
                 validate and bound
                         │
                         ▼
           watcher pane-lifetime union `prs`
                 │          │          │
                 ▼          ▼          ▼
             /api/state   digest   Live prompt
                                      │
                                      ▼
                              future PR dashboard
```

The classifier emits `working_prs`, a list of `{repo, number}` objects supported by the
current frames. This is an evidence channel, not public accumulated state. The watcher
validates each reference, removes `working_prs`, unions it into the pane's accumulated
`prs`, and publishes only that accumulated list.

There is intentionally no regex or URL scraping. Code cannot tell whether `#4955` is
the change being fixed, one row among fifty open PRs, or an unrelated example. That
decision stays in the semantic classifier that already reads the pane trajectory.

## Repository identity

The watcher resolves the pane cwd's local `origin` with `git remote get-url origin` and
passes an exact GitHub `owner/name` into the classifier. This uses no network access and
is cached for the pane's current cwd and refreshed when it changes or the next semantic
read occurs after 60 seconds. Failures expire too, so transient Git errors do not
disable tracking for the rest of a pane's lifetime. Repository
lookup is skipped when LLM classification is disabled. The model
uses that value for bare PR numbers; it may name another
repository only when the screen gives the complete owner/name unambiguously. If no
GitHub origin is available, it must not guess an owner.

## Lifetime and restart behavior

Associations persist for one tmux pane lifetime, retaining up to 64 of the most
recently evidenced PRs (repeat evidence refreshes retention order). Repository strings
are limited to 256 characters; together these bounds prevent untrusted model output
from growing state and Live context without limit. Multiple PRs may be associated
with a pane, and multiple panes may be associated with the same PR. Closing
a pane clears its set. If tmux recycles a pane id with a different pid, the old set is
cleared before the new pane is observed.

The state is memory-only, matching the watcher's other live session state. After a
daemon restart, the existing semantic bootstrap reads up to 800 lines of scrollback and
can reconstruct associations whose work evidence remains there. This is useful restart
recovery, not a durable historical database. Durable cross-session PR history, if ever
needed, is a separate design rather than an accidental dependency on agent history.

## Consumers and routing

`/api/state` exposes each pane's accumulated `prs`. The digest includes the same field,
and the Live prompt receives a readable “PRs this pane has worked on” line. A request
naming a repository and PR should prefer panes with an exact association.

The `/m` session sidebar (on phone and desktop) searches accumulated `prs`, not just
the current headline. Search by `4955`, `#4955`, `qs-app#4955`, `qs-app PR 4955`, the
full `owner/repo#4955`, or a GitHub pull-request URL. All matching panes remain in
the results, including several sessions for the same PR. Existing status filters still
apply; select **All** to find an idle pane. Search does not create associations.

The pane overview's **Links** section also offers every tracked PR as a tappable
GitHub title with `owner/repo#number` beneath it, on mobile and desktop. Titles come
from `gh pr view --json title`, not the LLM. Two background workers fetch titles with
an eight-second timeout; a shared 512-entry cache refreshes successes after 15 minutes
and retries failures after one minute. Failed refreshes retain the last title. If gh,
authentication or network access is unavailable, the link falls back to the reference.
Title updates participate in the state change signal even while a pane is idle.
These links survive the URL
scrolling off-screen, open externally like existing links, and deduplicate matching
links from the current frame. Unrelated preview/auth links remain available.

When several panes match, routing considers their current titles, activity, cwd, and
screens. If those do not distinguish the intended pane, Live Mode asks the user rather
than silently choosing. This permits several sessions per PR without pretending there
is a one-to-one owner.

A future dashboard can invert the same live state into PR → panes, then enrich it with
GitHub review/check state. That enrichment is deliberately out of scope here: first the
system needs a trustworthy semantic association to join against.

## Failure behavior and validation

- Failed classifier calls add nothing and never erase prior associations.
- Malformed references, non-positive numbers, and non-`owner/name` repositories are
  dropped; one classifier response is capped to bound untrusted output.
- Bootstrap and live classification use the same validation and accumulation path.
- Prompt evals pin both sides of the semantic boundary: real review work associates a
  PR, while an open-PR list associates none.
- Unit tests cover reference validation, repository resolution, accumulation across
  frames, cleanup on pane death/reuse, state publication, and Live prompt context.
