# PR context and lifecycle within a session

Status: draft proposal; not implemented. Follow-up to [PR/session associations](pr-session-associations.md)
(#245) and GitHub titles (#248).

## Problem

Today the watcher accumulates PR references for a pane's lifetime. GitHub titles explain
what each PR is, but not why this session is associated with it. Work that finishes or
is handed off stays in Links, search, and routing indefinitely. A session coordinating
several agents can become an unhelpful list of everything it ever touched.

Track the session's relationship to a PR, not ownership of the PR or every mention.
Two sessions can independently implement and review the same PR. Their contexts and
lifecycles need not agree.

## Proposed record

Keep `{repo, number}` as the association key and add:

| Field | Meaning |
| --- | --- |
| `context` | Short, LLM-written explanation of this session's work, e.g. “Addressing review feedback” or “Reviewing another session's implementation.” |
| `status` | `active`, `done`, or `removed`, describing this session's relationship. |
| `updated_at` | Watcher-assigned time of the last actual context/status change, not every classification. |

Use free text for context rather than a growing role taxonomy. One concise sentence
also covers testing, coordinating, waiting for review, and handoff. The GitHub title
continues to come from GitHub; the LLM does not invent or replace it. GitHub's open,
closed, and merged states are separate from this relationship status.

| Status | Interpretation | Default presentation and routing |
| --- | --- | --- |
| `active` | This session still has responsibility or useful ongoing work on the PR. Waiting or being idle does not end it. | Visible link; eligible for current-work routing. |
| `done` | Its work is finished, but the PR remains meaningful as the session's latest result. | Visible, marked Done; searchable as completed work, not an active assignment. |
| `removed` | Handed off, superseded, associated by mistake, or no longer relevant after the conversation moved on. | Hidden from current links/search/routing; retained in association history. |

A merged PR may still be active here for follow-up verification. An open PR can be
done here after a review, or removed after handoff. Do not automatically delete every
merged PR, or preserve a PR solely because it is the session's only one.

## Semantic updates, applied by code

Use the existing core classifier on its normal cadence, whether Live Mode is on or off.
Give it the prior association records as labeled state alongside the pane evidence.
Have it return explicit updates, not a replacement list:

```json
{"pr_updates": [{"repo": "owner/repo", "number": 123,
                 "context": "Review finished; changes handed back to the author",
                 "status": "done"}]}
```

Omission means unchanged. An empty update list, model failure, truncated capture, or
malformed update never clears an association. Code validates references, statuses,
field lengths, and batch size before applying updates to this pane's session only.
Do not retain two contradictory updates for one key in a response: ignore that key.

The LLM decides whether the conversation demonstrates a transition:

- Actual work starts: create an active association with its context.
- Responsibility changes: update context, retaining active status where appropriate.
- Work finishes: mark done while the result is still relevant.
- Work is handed off or the session clearly moves beyond it: mark removed.
- New work on a done/removed PR begins: reactivate it with the new context.

Absence from the latest screen is not evidence of removal. Neither are elapsed time,
an unrelated aside, a fresh PR list, or a GitHub state change alone. Conversely, an
explicit “remove” command is not required: a clear semantic progression can retire old
work. A removed record must not reactivate merely because its earlier work remains in
scrollback. Reactivation requires evidence of renewed work after the retirement.

Keep the prompt change small: define the update fields and transition semantics once
in shared instructions. Put concrete timelines and UI examples in eval fixtures, not
new prompt appendices. No PR-number scraping or per-tool phrase rules for lifecycle.

## Links, search, and routing

Show active links first, then completed links. Each link displays the GitHub title,
`owner/repo#number`, and the session-specific context. Done gets a subdued text label.
Removed entries appear only in an expandable association-history section, with their
last context explaining why they were retired; do not append them to unrelated links.

Default sidebar PR search matches active and done records, with their disposition
visible in results. Removed matches require an explicit “include history” option.
Done records remain discoverable so a user can return to a completed session for new
review feedback. Routing prefers suitable active contexts, then considers done ones;
it does not silently send new work to a completed or removed assignment. An explicit
request to resume a completed session can select it. Ambiguity still requires a
question, including two active sessions with different roles on the same PR.

Publish one canonical association state to API consumers. Derive visible links,
search matches, and the Live routing digest from it so consumers cannot accidentally
keep using the old accumulated union. Metadata updates must trigger publication even
when the pane's activity status remains idle. This is not a PR dashboard project.

## Restart and session boundaries

Removal cannot be reliable if a daemon restart forgets it and bootstrap reconstructs
the old association from scrollback. Persist bounded association records, including
removed tombstones, as watcher-owned state; do not put this in agent-history.

Key persisted state by the same verified pane/session lifetime used by the watcher,
not a bare reusable tmux pane ID. Restoration must reject a changed pane process or
known agent-session identity. Implementers must verify that identity contract before
claiming restart-safe behavior; a replacement conversation in the same shell must not
inherit the previous conversation's PRs.

Restore state before bootstrap. With restored records, bootstrap fills narrative
context but cannot overwrite lifecycle decisions using old history. Without saved
state, bootstrap must reconstruct the latest relationship from the full available
timeline, not union every historical work mention. If storage is unavailable, expose
degraded restart persistence rather than silently promising durable removals.

Keep at most 64 current records and 64 retired records per session, with completed
records eligible for archival when current capacity is exhausted. Never silently
evict active work to admit a passing mention. Historical retention is bounded, not an
audit log. Prune state for ended session lifetimes. Define atomic writes and recovery
tests in implementation; no database/schema expansion is prescribed by this sketch.

## Migration and acceptance tests

Existing `{repo, number}` entries have no trustworthy role or lifecycle. Preserve
their links temporarily with no invented context and mark them internally as needing
reassessment. On the next successful semantic read, provide them as legacy evidence
for the LLM to classify; do not manufacture an “implementing” role or permanently bless
the existing union. Failed reassessment leaves them unchanged, not deleted.

Add multi-capture evals that check transitions and negative cases, plus deterministic
tests for update application, publication, persistence, and consumer filtering:

- Two sessions implement and review the same PR with distinct contexts.
- A PR list creates nothing; coordinating actual assigned work can create associations.
- Completion leaves a Done link; moving to unrelated work later retires it.
- Explicit handoff retires the source without automatically assigning the destination.
- A merged PR with remaining verification stays active; an open PR review can be done.
- A brief aside or offscreen PR does not retire work.
- Old scrollback cannot resurrect removed work; a new request can reactivate it.
- Restart preserves retirement; pane reuse and conversation replacement do not inherit it.
- Failed/partial model output preserves unaffected records; invalid updates do not clear state.
- Search distinguishes completed results; removed records cannot leak into default routing.

Deliver context, transitions, and all consumer filters together in the follow-up.
Titles alone do not solve this problem, and adding statuses without removing the old
union from consumers would leave it unsolved too.
