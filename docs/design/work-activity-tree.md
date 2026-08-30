# Design: the work-activity tree

Status: **draft / thinking** — no code yet.

## What this is (and isn't)

tmux-rc should show **work done**, structured as a tree — not "Claude usage
stats". The unit is a *chunk of work*, and chunks nest and link:

```
tmux-rc build
├─ phase 1: initial prototype        (28m · $2.10 · +1.2k LOC)
│  ├─ scaffold repo + PRD/design      (6m · +300)
│  ├─ tmux wrappers + classifier      (14m · +600)
│  └─ local testing                   (8m)
├─ pivot to LLM passthrough           (41m · $6.80 · +700/-440)
│  ├─ research: text vs image          (12m · 9 LLM probes)
│  └─ raw-JSON pipe rewrite            (22m)
└─ …
```

Each node carries: a name, start/end (duration), cost, tokens, LOC delta, a one-line
summary of what was accomplished, and links to children and to the underlying raw
captures (drill-down, per the viewing doc). It is **tool-agnostic** — a node might
represent an agent working, but also `make test`, a `psql` session, a manual git rebase,
editing in vim. The tree describes the *work*, whatever produced it.

This generalizes the timeline: a flat timeline is a tree of depth 1. The live card is
the currently-open leaf node.

## How the tree is built — LLM auto-segmentation

Decision: **the LLM segments automatically** (no required user effort). The system
watches the parse stream (we already parse on change/heartbeat) and an LLM groups
activity into named phases, detecting boundaries semantically: "the focus shifted from
X to Y — start a new sibling", "this is a sub-task of the current phase — nest it",
"this phase wrapped up — close it". The prompt frames it as *narrating work into a
tree*, using the same signals we already extract (headline, tasks done, notable, tool,
idle transitions) plus git/LOC deltas.

Because auto-segmentation is fuzzy, design for correction later (rename/merge/split a
node) — but v1 is zero-effort. Boundaries don't need to be perfect; a roughly-right
tree that reads like the session's story is the goal.

### Segmentation mechanics (to work out)
- **Incremental, not batch.** Maintain the tree as we go: each parse either extends
  the open leaf, opens a sibling (focus changed), opens a child (sub-task started), or
  closes nodes (work finished / went idle). Re-summarizing the whole session every
  time is too costly; fold each delta in.
- **Stability.** Reuse the multi-frame-context trick so the segmenter sees recent
  history and doesn't thrash boundaries. A node's name/summary can be refined as more
  happens within it, but shouldn't flip-flop.
- **Rollups.** Time = wall-clock within the node. Cost/tokens = summed from the parses
  attributed to it. LOC = git diff delta across the node's span (needs git sampling —
  see below). These roll up from children to parents.
- **Node close.** A node closes on a clear focus change, a long idle, or an explicit
  signal (commit, "phase done"). Closed nodes are immutable history; the open leaf is
  live.

## Data it needs
- The parse stream (have it) — headline/tasks/notable/tool/activity per tick.
- **LOC / git deltas** — sample `git diff --shortstat` (and branch/commit) periodically
  per pane's cwd, so nodes can show +/- lines and link work to commits. New capture,
  cheap.
- Cost/tokens per parse (have it from the LLM usage metadata).
- Raw captures retained (viewing doc's substrate) so any node drills down to reality.

## Relationship to other docs
- **Substrate**: the tree is a rollup over the persisted parse/capture log (see
  viewing-and-timeline.md → Postgres/SQLite option). Store the atoms; the tree is a
  view/materialization over them. This is the strongest reason to persist.
- **Viewing**: each node links to its raw captures for drill-down; a node's "see what
  happened" is the viewing feature scoped to that node's time span.

## Open questions
- Attribution when multiple panes/tools interleave — is the tree per-pane, per-session,
  or a unified cross-pane project tree? (The example above is cross-pane/project.)
- How much does the LLM decide the hierarchy vs. anchoring on hard signals (commits as
  natural phase boundaries)? Auto is the call, but commits/tasks are strong hints.
- Cost of continuous segmentation for panes nobody's watching (same question as the
  timeline — probably a low background rate so history is ready when you open the app).
- Editing/correction UX (deferred past v1).
