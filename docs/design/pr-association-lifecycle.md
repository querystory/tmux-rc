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

Keep `{repo.lower(), number}` as the association key, matching the existing accumulator.
Normalize before lookup, deduplication, capacity accounting, and conflict detection;
repository casing cannot create a second association. Add:

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
                 "context": "Review finished; findings summarized",
                 "status": "done"}]}
```

Omission means unchanged. An empty update list, model failure, truncated capture, or
malformed update never clears an association. Code validates references, statuses,
field lengths, and batch size before applying updates to this pane's session only.
Accept at most eight updates per response; reject an oversized batch in full. Context
must be nonempty plain text of at most 240 Unicode code points after trimming; reject
an oversized/invalid item rather than truncating its meaning. Retain the existing
256-character repository bound and positive safe-integer PR-number validation.
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
review feedback. Only an unambiguous active match may be automatically routed. Done
records are clarification candidates, never automatic fallback targets; an explicit
request to resume a particular completed session authorizes that target. Removed
records are never routing candidates. A user can independently select that session
and start new work, which may reactivate the association. Ambiguity still requires a
question, including two active sessions with different roles on the same PR.

Publish one canonical association state to API consumers. Derive visible links,
search matches, and the Live routing digest from it so consumers cannot accidentally
keep using the old accumulated union. Metadata updates must trigger publication even
when the pane's activity status remains idle. This is not a PR dashboard project.

## Restart and session boundaries

Removal cannot be reliable if a daemon restart forgets it and bootstrap reconstructs
the old association from scrollback. Persist bounded association records, including
removed tombstones, as watcher-owned state; do not put this in agent-history.

Persist a composite identity: tmux server generation, pane ID, pane process start
identity, and a verified agent conversation ID. Neither a title nor cwd is an identity.
A changed component starts a new association scope, including a replacement
conversation inside the same shell. If a conversation ID is unavailable or temporarily
unverifiable, quarantine existing records: do not restore, publish, route, or mutate
them until the identity is verified. This trades tracking availability for isolation;
never fall back to pane ID alone. Establishing this identity signal is an implementation
prerequisite, not an assumption that today's watcher already supplies it.

Restore state before bootstrap. With restored records, bootstrap fills narrative
context but cannot overwrite lifecycle decisions using old history. Without saved
state, bootstrap must reconstruct the latest relationship from the full available
timeline, not union every historical work mention. If storage is unavailable, expose
degraded restart persistence rather than silently promising durable removals.

On restore, capture the current terminal as a baseline before permitting any mutation
of restored records. Every context/status change requires supporting content first
observed after that baseline, including active → done and context-only edits. Advance
the record's evidence boundary on each accepted mutation; subsequent mutations require
new supporting evidence beyond that boundary. Reactivation requires renewed work;
done → removed instead requires handoff or progression beyond the work. Bootstrap
cannot change restored records. Supply new evidence separately from historical context.
A redraw, resize, or shifted
scrollback window is not new work; if continuity cannot be established, rebaseline and
require subsequent evidence. Apply this same rule to records created during normal
operation. The model judges whether the new evidence supports the proposed transition,
not just a fresh quotation of an old task. Wall-clock `updated_at` is display
metadata, never proof that undated terminal text is newer. This conservative rule can
miss work begun while the daemon was down until new evidence arrives; it must not
resurrect an association on the first ordinary tick after restart.

Keep at most 64 association records total per session, counting removed tombstones.
At capacity, accept updates to existing keys but reject new keys and expose a capacity
warning; never evict a retirement decision or active work to admit another reference.
This avoids resurrecting an evicted tombstone from scrollback and bounds model input.
Prune state only for verified ended session lifetimes, not a transient pane-list error.
History retains the latest disposition, not an unbounded event log. Define atomic writes
and recovery tests in implementation; no database/schema expansion is prescribed here.

## Migration and acceptance tests

The shipped baseline stores associations only in memory. The first upgrade therefore
starts without saved associations and reconstructs best-effort state from available
scrollback under the bootstrap rules above. It cannot promise to preserve every old
link or reconstruct work no longer in the capture. Do not import an unverified union
from a stale client, or invent roles for old references. There is no fourth pending
status: bootstrap emits validated active/done/removed records or leaves a reference
unassociated. Subsequent restarts restore the new persisted records. Quarantine is a
session-level identity gate, not a PR status, and its records count toward the same cap.

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
