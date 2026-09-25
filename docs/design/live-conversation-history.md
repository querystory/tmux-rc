# Save Live Mode conversations

Proposal only. Let someone reopen a previous Live Mode conversation, read the message
thread, and see roughly how long it ran and what it cost. Use the existing private
SQLite database. This does not change how Live Mode sends commands to panes.

## Two tables

| Table | Columns |
| --- | --- |
| `live_conversations` | Integer ID, owner, title, started/ended timestamps, status (`active`, `ended`, `interrupted`), next entry number, usage totals JSON, history-incomplete flag. |
| `live_entries` | Conversation ID, entry number, timestamp, kind (`user`, `assistant`, `action`, `notice`), text, optional metadata JSON. |

A conversation is the thread the user sees. There is no separate Call, Turn, Action,
Connection, or Usage entity. A spoken message, an action notice, and “connection lost”
are simply different kinds of entries in that thread.

The entry key is `(conversation_id, entry_number)`: 1, 2, 3, and so on. No entry UUIDs.
Use SQLite AUTOINCREMENT for conversation IDs; allocate entry numbers from the parent
counter in the insert transaction. Foreign keys with ON DELETE CASCADE keep entries
attached to their conversation. Enable foreign-key enforcement on each connection.

Provider/model information goes in metadata where useful. Action entries contain the
server-known operation, pane label and result, without copying raw command arguments
or typed secrets. They describe what was sent, not whether the command finished.
Provider connection IDs and transcript-fragment bookkeeping stay in the live handler;
they do not need their own durable records.

## Saving and reading

Create the conversation when saved Live Mode starts. The existing live handler builds
each message from streaming fragments; save the finished message, not every fragment.
Use one bounded writer queue off the audio loop. Assign a message its entry number
once and reuse it on a database retry. Do not replay provider history on reconnect or
try to reconstruct missed fragments after a daemon crash.

A browser reconnect to a still-running handler keeps the same conversation. A daemon
restart marks previously active conversations interrupted; a new Live Mode start
creates a new conversation. Starting history is explicit, not a side effect of an
incoming message: late writes may insert entries only under an existing active parent.

Flush accepted entries before marking a conversation ended. On a write failure or
queue overflow, show “History not saving” and leave its history incomplete. A crash
can lose the current message or an action sent just before its entry was saved. Show
that limitation on interrupted history; this is a saved conversation, not an audit log.

Add a History list and reuse the existing message-thread view. Read conversations by
integer ID and entries by entry number, with a bounded “load older” query. No search
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
payloads. The saved transcript itself may contain sensitive things the user said.

Use the same server-verified owner for recording and history access. Unverified clients
can use existing unsaved Live Mode but cannot create/read saved conversations. Check
ownership on every operation and protect browser mutations/recording handshakes with
same-origin checks. IDs are lookup keys, not permission to read a conversation.

Keep a conversation until the user deletes it in the first version. Delete its entries
and usage together; no separate retention periods, keep overrides, or tombstones.
Deletion stops that conversation's recording and runs through the same serialized
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
UI. Test message ordering/retries, reconnect without duplication, crash/write failure,
usage without messages, recording opt-out, owner isolation and deletion races. No
backfill and no changes to provider tool dispatch. Review that small implementation
before designing continuation or sharing.
