# Save conversations and pane input history

Proposal only. Let someone reopen a previous Live Mode conversation, read the message
thread, and see roughly how long it ran and what it cost. Use the existing private
SQLite database. Also keep a continuous history of inputs sent to each pane. This
does not change how Live Mode or the composer sends commands to panes. The main
interaction goal is one assistant conversation that can move between voice and text.

## Two tables

| Table | Columns |
| --- | --- |
| `conversations` | UUID, originating machine UUID, local creation number for pagination, owner, kind (`live` or `pane`), optional pane lifetime key, title, started/ended timestamps, status (`active`, `ended`, `interrupted`), next entry number, usage totals JSON, history-incomplete flag. |
| `conversation_entries` | Conversation UUID, entry number, timestamp, kind (`user`, `assistant`, `input`, `action`, `notice`), content JSON (ordered text/image parts), optional metadata JSON. |

A conversation is the thread the user sees: either a Live Mode conversation or the
ongoing input history of a pane. These share storage, not lifecycle or recording policy. There is no separate Call, Turn, Action,
Connection, or Usage entity. A spoken message, an action notice, and “connection lost”
are simply different kinds of entries in that thread.

Generate a UUID for each conversation. The global entry key is
`(conversation_uuid, entry_number)`, with entry numbers 1, 2, 3, and so on allocated
from the parent counter in the insert transaction. No independent entry UUID is needed
for this single-writer design. Exports, origin links and cross-machine analysis preserve
these keys; local SQLite row numbers are never external identities. Keep a separate
AUTOINCREMENT creation number only for local pagination.

A persisted machine UUID identifies the originating installation across daemon/OS
restarts; hostnames and boot IDs are not machine identity. New installations get a new
machine UUID. Imports preserve the originating IDs rather than relabeling old records.
The conversation has one authoritative writer; cross-machine analysis can combine copies
by their global keys, but concurrent editing of one copied thread is not a replication
feature promised here. A separately writable copy gets a new conversation UUID.
Foreign keys with ON DELETE CASCADE attach entries to their conversation; enable
foreign-key enforcement on each connection. IDs do not grant access.

Provider/model information goes in metadata where useful. Action entries contain the
server-known operation, pane label and result, without copying raw command arguments
or typed secrets. They describe what was sent, not whether the command finished.
Provider connection IDs and transcript-fragment bookkeeping stay in the live handler;
they do not need their own durable records.

## Keep chatting when voice is inconvenient

Put a text composer in the Live Mode thread. The user can switch to **Text mode** in a
crowded room or meeting: stop microphone capture, stop current playback and suppress
new spoken replies, while keeping the assistant conversation open. Responses remain
visible as text. Switching back to Voice explicitly reacquires the microphone and
enables spoken replies. Closing the keyboard or sending text never unmutes audio.

Typing here sends a user message to the assistant, not directly to the selected pane.
The assistant can act on panes through the existing tools and show action notices in
the thread. The existing pane composer remains a distinct “send to pane” action.

This needs no new tables or conversation type. Voice transcripts and typed messages
are both `user` entries, with `input_mode: voice|text` in metadata; replies are
`assistant` entries regardless of whether they were spoken. Switching modes does not
end the conversation, reset usage, or create a Call. Text mode is a UI/audio preference,
not another durable lifecycle. A reopened thread starts with audio off until requested.

Route submitted text into the active provider conversation. Confirm each adapter can
accept text and produce visible replies while local audio is disabled; do not recreate
the provider session just to hide audio controls. If a provider requires a new session,
say it is reconnecting and restore bounded context explicitly rather than silently
starting from scratch. Never present an outgoing local text bubble as acknowledged
until accepted; show pending/failed sends and make retry reuse that message entry.
Mode switches neither resend inputs nor replay tool calls. If a reply is interrupted,
keep the partial text labeled as such; already-sent pane actions are not undone.

Prioritize this within an active conversation before restart-time Continue, sharing or
pane-history imports. Saving the thread should make the same conversation available
later, but a seamless voice/text switch must work without durable recording enabled.
Test voice→text during playback, no microphone/audio in Text mode, one typed message
per send/retry, preserved assistant context, visible tool actions, and explicit return
to Voice. Resuming after a disconnected provider remains the separate Continue work.

## Continuous pane history

One pane thread per owner and pane lifetime, surviving browser visits, Live Mode calls,
renames and daemon restarts. Qualify the existing tmux server/pane identity with the machine UUID and confirmed
pane creation/removal boundaries; `%52` or a display label alone is not an identity.
If the lifetime cannot be established, start a new thread rather than merge unrelated
panes. A closed pane stays readable. Starting another agent inside the same pane adds a
boundary notice when detected; it does not silently erase or replace the pane history.

An `input` entry represents a logical send, not every keyboard event:

| Value | Where it lives |
| --- | --- |
| Submitted text and timestamp | Entry content and timestamp. |
| Source: composer, Live Mode, or API | Entry metadata, assigned by the server. |
| Delivery: sent, failed, or unknown; Enter submitted or not | Entry metadata. Sent means handed to tmux, not processed by the agent. |
| Context at send time | Metadata snapshot: pane/session/window label, working directory, tool/model, agent session ID and branch when known, plus observation time. Missing or stale information stays labeled as such. |
| Originating Live Mode message | Optional `(conversation_uuid, entry_number)` reference, only when that association is known and both threads have the same owner. |

For example: pane input #42 says “address the review comments,” sent from Live Mode at
14:32, to the Codex session in the repo directory on a particular branch. Its origin
link opens the voice discussion explaining which comments. An independently typed
composer message has the same shape without that link. Do not invent a causal link to
the latest voice message when the provider did not establish one.

Record at the logical dispatch boundary used by composer/API/Live Mode, not each low-level
`send_keys` call: one composer submission may paste several segments and press Enter.
Store the ordered text and image parts as one prompt, retaining recorded images as
described below; temporary upload paths are not durable attachment references. Key-only operations can be short notices.
Unsubmitted fragments stay marked unsubmitted; do not pretend they form a full prompt.
Never automatically resend input to repair a missing history row.

This initially covers inputs sent through tmux-rc. Direct typing in another tmux client
and complete agent replies need a reliable harness/event source later. Screen captures
are observations, not exact messages. The UI must call this input history, not imply a
complete two-way transcript. Imported events, if added later, must identify their source.

The ordered entries provide durable context. Current running/idle/waiting state still
comes from the existing watcher; an input is not evidence that work is running or done.
If useful later, a thread can have one derived summary and a `through_entry` number so
new inputs can be appended to its context. No extra state-machine or summary table is
needed. Reading history never replays commands; using it as agent context is a separate
explicit feature with the same safeguards as Continue.

Pane input recording has its own visible opt-in because exact submitted text may contain
secrets. Live-origin text is saved only when both pane-input recording and Live Mode
content recording allow it. With recording off, keep at most a content-free notice.
Keep exact input text in the pane entry, not duplicated in Live Mode action summaries.
Origin links are optional navigation, not permission to read: recheck ownership, and a
deleted source becomes unavailable without deleting the independently saved pane input.
Make that independence clear in deletion UI; users can delete either or both threads.

## Pasted images belong to the prompt

Entry content is an ordered list of parts: for example `[text, image, text, image]`.
A plain message has one text part. Each image part holds its content hash, MIME type,
byte length and optional display name. The hash (for example SHA-256) identifies the
bytes across machines; no separate image UUID or attachment table is required. Store
bytes once in the private history attachment directory, scoped to the owner, outside
temporary upload storage. The entry references the hash, never a temporary path.

When recording is enabled, atomically save the image before committing its entry
reference; a recording failure leaves an explicit unavailable-image marker and incomplete
history, not a broken reference described as saved. Input delivery need not fail because
history storage failed. Render saved thumbnails/full images in history in their original
prompt order. Recording disabled means no retained image copy; the same content policy
applies to images as text, including Live-origin pane inputs.

Exports intended to preserve images bundle their referenced bytes and part manifests;
text-only exports clearly mark omitted images. On import, verify bytes against the hash
and preserve entry identity. Hash deduplication is not authorization: enforce owner access
for image reads and never expose files through a public hash URL. Deleting history removes
unreferenced owner-scoped blobs through serialized cleanup, retaining blobs still referenced
by another saved entry. Orphan files from failed commits can be swept by the same cleanup.
Use existing upload size/type validation; do not fetch arbitrary remote image URLs.

## Saving and reading

For Live Mode, create the conversation when saved listening starts. For pane history,
explicitly enabling recording creates or opens that pane thread. The existing live handler builds
each message from streaming fragments; save the finished message, not every fragment.
Use one bounded writer queue off the audio loop. Assign a message its entry number
once and reuse it on a database retry. Do not replay provider history on reconnect or
try to reconstruct missed fragments after a daemon crash.

A browser reconnect to a still-running handler keeps the same conversation. A daemon
restart marks previously active Live Mode conversations interrupted; a new Live Mode start
creates a new conversation. Starting history is explicit, not a side effect of an
incoming message: late writes may insert entries only under an existing active parent.

Flush accepted entries before marking a conversation ended. On a write failure or
queue overflow, show “History not saving” and leave its history incomplete. A crash
can lose the current message or an action sent just before its entry was saved. Show
that limitation on interrupted history; this is a saved conversation, not an audit log.

Add a History list and reuse the existing message-thread view. Read conversations by
local creation number and entries by entry number, with a bounded “load older” query. No search
snapshots, signed pagination tokens, archive tree, or fork graph in the first version.

## Duration, usage, and cost

Keep one usage totals JSON value on the conversation, updated from the existing
provider meter and flushed when it ends. It includes provider/model, available token
and audio-duration counters, and whether the totals are incomplete. Usage can be saved
even if nobody finishes a spoken message. Do not attach usage to individual messages.

The meter handles provider cumulative-versus-delta accounting; persistence replaces its
latest normalized totals instead of adding every snapshot. On provider reconnect, only
combine amounts the adapter can distinguish without double counting; otherwise mark
the estimate incomplete. Independently billed voice/model totals remain separate keys
in the JSON, and included charges are not added twice. No per-sample accounting ledger.

Display estimated cost using the configured rates, labeled with the rate version.
Unknown usage or prices display as unavailable, not zero. Estimates may change when
rates change; we are not promising invoice reconciliation or historical billing rates.

## Recording and deletion

Provide a visible Save conversation toggle before starting. When off, do not persist
transcript content or send it through content-bearing telemetry/logging, including
QSDEBUG paths. Do not save microphone audio, provider credentials, or exact action
payloads in Live Mode action entries. Exact text belongs only in opted-in pane input
entries as described above. The saved transcript itself may contain sensitive things the user said.

Use the same server-verified owner for recording and history access. Unverified clients
can use existing unsaved Live Mode but cannot create/read saved conversations. Check
ownership on every operation and protect browser mutations/recording handshakes with
same-origin checks. IDs are lookup keys, not permission to read a conversation.

Keep a conversation until the user deletes it in the first version. Delete its entries
and usage together; no separate retention periods, keep overrides, or tombstones.
Deletion stops that thread's recording and runs through the same serialized
writer as inserts. Writes queued afterward fail the active-parent check; no write path
recreates the parent. Test deletion racing a pending write and restart. Downloads or
backups already taken cannot be recalled by deleting the local conversation.

## Later, when the basic history is useful

- **Continue:** reopen the same thread and append new entries, with a notice marking
  the new listening period. Feed bounded prior text as context, never replay actions.
  Evaluate this with the separate dispatch-authorization work in #236 before shipping;
  merely quoting old text does not make it safe against prompt injection. No Call table
  is needed just to show where listening stopped and restarted.
- **Share:** start with a reviewed text/JSON download. No hosted links, recipient ACLs,
  imports, share snapshots, or fork objects in this proposal's implementation scope.
- Add automatic retention, transcript search or detailed usage breakdowns only when
  actual usage demonstrates a need. They are not prerequisites for saving a thread.

## First implementation

Add the two tables with a migration that preserves existing pane history, then wire
finished messages, action notices and meter totals into the writer. Add list/read/delete
UI. Pane input history can follow using the same tables and shared writer, with composer,
API and Live Mode logical sends covered together. Test message ordering/retries, reconnect without duplication, crash/write failure,
usage without messages, cross-machine identifiers, pane identity/restarts, send-time context, missing origin links,
ordered image pastes, missing blobs, export/import integrity and shared-blob deletion,
recording opt-out, owner isolation and deletion races. No
backfill and no changes to provider tool dispatch. Review that small implementation
before designing restart-time continuation or sharing. In parallel, add the Live Mode
text composer and explicit Voice/Text control using the existing assistant session;
that interaction should not wait on the pane-history implementation.
