# Durable Live Mode conversations

Status: proposal, not an implemented feature. This design is separate from the
conversation-thread UI and mobile audio lifecycle fixes.

## What the user should be able to do

Open a history of Live Mode conversations, read what was said and which pane actions
were taken, see duration and estimated cost, and continue an earlier conversation.
A conversation can span multiple calls and provider connections. Ending a call must
not erase its transcript. Sharing should create an explicit, reviewable snapshot,
not expose the daemon or silently publish every future turn.

SQLite is a good fit for the local, single-daemon writer and indexed history reads.
Use the existing private `history.sqlite3` database, introducing versioned migrations, with
separate tables from structural pane history. That changes the database's sensitivity:
currently pane history deliberately contains no terminal text or summaries. The new
feature adds conversation content, so its recording controls, export behavior, and
retention must be explicit. No cloud database is needed for local history.

## What exists today

`openbus/history.py` persists structural pane observations in SQLite, outside the
checkout, with WAL and private file permissions. `openbus/live.py` has a per-call
UUID, `_Meter`, cumulative usage accounting, a bounded transcript tail, and per-turn
and final telemetry emission. These are useful collection points, but the transcript
tail is not a durable conversation archive. `web/m/live.js` renders a bounded,
in-memory stream of user/model fragments and typed-action notices.

The provider work in PR #170 introduces additional adapters and billing dimensions;
implementation must reconcile with that PR's eventual main version. This proposal
makes no assumption that every provider exposes the same usage, resume tokens, or
turn boundaries. Existing telemetry is not automatically imported as complete history.

## Record these things

Four records. This feature answers *what happened*; it does not decide what Live Mode
is allowed to do, so nothing here gates dispatch, mints authority, or changes a code
path that runs today. Everything that was only bookkeeping became a column.

| Record | Fields and purpose |
| --- | --- |
| Conversation | UUID, owner, editable title, created/updated times, archive state, optional parent conversation and fork point. Stable across calls. |
| Call | UUID, conversation ID, start/end times, end reason, provider/model, recording mode, heartbeat, daemon generation. One Start-to-End interaction. Transport details (provider connection ID, model actually used, reconnect reason) are columns here: a reconnect rewrites them and keeps appending to the same call, because a new socket is not a new thing to reason about. |
| Turn | UUID, conversation sequence, call ID, role, text, start/end times, partial/final/interrupted state, optional provider item ID. Usage lives here as columns: tokens split by text/audio/cache and audio duration as the provider reports them, each tagged cumulative-or-delta with a provider revision for ordering. |
| Action | UUID, call ID, turn ID when known, provider tool-call ID, verb, stable pane identity and label snapshot, argument summary, outcome, submitted flag. A descriptive record of what Live Mode typed, written after the fact. It imposes no uniqueness constraint on dispatch and is never consulted before sending input. Never imply a sent command completed its task. |

Store UTC timestamps for display and durable ordering; assign a monotonically
increasing sequence per conversation for stable pagination. A provider ID supplements
our own IDs rather than replacing them. A pane reference includes the tmux server and
pane lifetime so a reused `%12` cannot point an old action at a new pane.

Do not store raw microphone/audio buffers, credentials, provider resume secrets,
full terminal screens, or repeated ambient pane snapshots by default. Transcript
text and commands can still contain secrets: private filesystem permissions are
necessary but do not make the content safe to publish. First delivery saves a safe
argument summary for actions; exact typed payloads require an explicit recording option.

## Write path and recovery

Persist at the daemon's provider event boundary, before sending a finalized event to
the browser. Browser disconnection must not discard records already received by the
daemon. Normalize provider events once, then feed persistence, telemetry, and the live
UI from that representation. Reuse `_Meter`'s accounting semantics after auditing its
reconnect handling; do not build a second independent cost calculator in the browser.

Coalesce streaming text in a bounded buffer. Checkpoint incomplete turns at most once
per second and flush on turn completion, interruption, and call end. The UI can show
live deltas immediately; only acknowledged durable records are described as saved.
Mark the possible last unflushed fragment loss after a crash rather than promising
exact transcripts. Upsert a stable turn/item ID when a provider revises a transcript;
do not append the replacement as another utterance. Persist an event deduplication key
where the provider supplies one; otherwise use connection + receive sequence and do
not claim deduplication across arbitrary provider replays.

Use short transactions through one serialized writer, off the audio receive loop,
with a bounded queue. Existing `History` migrations are ad hoc: introduce a shared
schema-version table and ordered transactional migrations before adding these tables.
Test upgrades from each supported version, interrupted upgrades and preservation of
pane history. Refuse a database newer than the running binary; roll back via a verified
backup rather than destructive automatic downgrades.

Enable `PRAGMA foreign_keys=ON` on every reader and writer connection before starting
transactions; verify it is enabled and test orphan rejection and cascade deletion.
Define deletion relationships explicitly rather than relying on unenforced REFERENCES;

indexes cover owner/created-at/ID and conversation/sequence. Retain the existing private
DB/WAL/SHM permissions. A backup must use SQLite's backup API or a stopped writer.

On startup, persist a new daemon generation and invalidate all active leases owned
by prior generations, regardless of heartbeat age, before accepting attachments. Mark
those calls interrupted; heartbeat staleness handles abandoned calls during normal
operation, not crash recovery. Test a crash immediately after a heartbeat. Finalize only usage
actually observed, and leave incomplete turns labeled partial. Disk-full or writer
failure must surface “History not saving” in the live UI; keep audio usable, retain a
bounded pending buffer, and retry with backoff. Never silently report a complete saved
conversation when events were dropped. Commands must not be replayed to repair a
missing history row. Deletion of an active call first ends its recording/call so queued
writes cannot recreate deleted data.

## Usage and cost without double counting

Record what the provider reported; compute money when someone looks. No price-snapshot
table, no estimate-revision chain, no accounting-scope entity: this is a cost readout,
not a billing ledger, and a stored pre-aggregated estimate is what would create the
double-counting that machinery then has to prevent. Rates live in configuration beside
the model list, versioned with the code; a displayed cost names the rate version it used.

Each adapter declares whether its usage messages are cumulative snapshots or deltas, and
the reset scope. For cumulative messages, replace the latest snapshot for that call; do
not sum every turn's snapshot. Order samples by the provider's revision/sequence, not
arrival time. Ignore older revisions; conflicting equal revisions mark usage incomplete.
Without reliable ordering, show usage as provisional rather than inventing a total.

A call with independently billed components — voice seconds plus backend model tokens —
records each under its own provider/model key on the turn. They are summed for display
and never folded into one another, so an inclusive voice charge and a token charge
cannot be mistaken for the same money. A component with no known rate shows as unpriced
rather than estimated at zero.

## Browsing and continuing

Add a History entry to Live Mode with paginated conversations, search scoped to the
current owner, and model/date filters. List pages sort by immutable (created_at, UUID),
not mutable updated-at. For search or other mutable filters, materialize the matching
conversation IDs in a bounded, expiring owner-scoped pagination snapshot at the first
request; later title/turn changes cannot change its membership. Recheck ownership and
deletion on each page (deleted items disappear), and require a fresh search after cursor
expiry. Sign an owner/filter-bound cursor with the snapshot ID and initial creation
watermark and last key, and exclude later inserts until refresh. Show latest activity
as a separate field. Deleted rows can disappear; existing rows cannot move between
pages. Turn pages use immutable conversation sequence, updating partial turns in place.
Opening one shows the message thread, action records,
partial/interrupted markers, and a compact usage summary. Reuse the live
conversation component so historical and active turns have the same presentation.
Suggested API contracts (not endpoints that exist today):

- `GET /api/live-conversations?before=…&limit=…` — bounded owner-scoped list.
- `GET /api/live-conversations/{id}/turns?after=…&limit=…` — stable sequence pagination.
- `POST /api/live-conversations/{id}/continue` — create a new call, optionally a fork.
- `PATCH` / `DELETE /api/live-conversations/{id}` — rename/archive or delete.
- `POST /api/live-conversations/{id}/export` — preview and produce a selected export.

Authenticate and authorize every read/write/export using server-established identity;
never trust an owner ID supplied in the browser. Local unauthenticated mode needs an
explicit single-owner policy and must not enable remote sharing implicitly. Existing
relay identity must be validated against the deployment's trust boundary first.
Missing or unverified identity fails closed for every history endpoint, including
listing, continuation, deletion and export; it must never fall back to another owner.
`_trusted_user` currently returns None for direct/LAN callers, so implement this gate
before exposing history. A separately configured single-owner mode must bind to
loopback or require its own authenticated local credential; a LAN request does not
become the local owner merely because it reached the daemon.

“Continue” starts a new provider connection and new metering scope. Load a bounded
summary plus recent finalized turns as conversation context, then refresh the current
pane inventory. Tell the user that this is a new call with prior context; do not claim
the old live audio stream is restored. Incomplete turns remain visible but are marked
as such in context. Prior tool calls are inert historical data, never executable
instructions. Serialize transcripts, summaries and action arguments inside a dedicated
quoted, untrusted-history block, explicitly fenced by the provider system prompt.
Use structured JSON serialization (including escaping delimiter characters) or a
length-delimited provider data field; never interpolate raw content into instruction
or protocol syntax. Treat encoding as structural protection, not a guarantee against
semantic prompt injection; tool authorization remains server-side. Include delimiter-
breaking transcripts, summaries and action arguments in malicious-history tests.
Never replay provider tool-call messages, IDs or results as active protocol messages.
Historical content must never be able to cause an action. Loading history is a read:
prior tool calls are replayed to the model as inert transcript text, never as protocol
tool-call messages, tool results or IDs, so the provider cannot treat them as pending
work to resume. Imported or shared conversations are context only and are marked as
such. If a provider emits a tool call immediately after history is loaded and it
corresponds to a historical action rather than anything the user just said, that is a
history bug: the fix is in how context is serialized, not a new check before dispatch.
Test history-only tool calls with a fabricated ID, malicious transcripts that imitate
protocol syntax, and delimiter-breaking summaries and action arguments.

Provider-native resumption, if supported, is a separate optimization with its own
expiry and credentials handling; it is not the durable history contract. Never store
resume secrets in exports. A user may fork an old point or change models; record the
parent and selected model explicitly. Permit only one active call per conversation
initially; reject a second with a clear “already active” response or offer a fork.

Make Start/Continue/Fork idempotent with a client request UUID scoped to the verified
owner and a stored request digest, so a retried tap resumes the original call instead of
opening a second one. Create the call and its active-conversation lease in one
transaction. A browser transport reconnect attaches to that existing call by ID; it does
not create a new conversation or call. Recording policy comes from the stored call, not
from reconnect parameters, and Continue inherits it unless the owner changes it. On
daemon restart, a prior-generation lease marks its call interrupted and continuing
starts a new call — history has to show the seam rather than pretend the old call
resumed.

Out of scope, deliberately. Live Mode's existing authorization, socket takeover and
tool-dispatch behavior are unchanged by this feature: today `openbus/live.py` dispatches
provider tool calls to `tmux.send_keys` without binding them to a fresh user turn, and
this design neither adds that binding nor relies on it. Recording an action is not
permission to take one, and no record defined here is consulted before input is sent to
a pane. Tightening that path is a behavior change to Live Mode and belongs in its own
change, against its own tests; see issue #236. Keeping it out means this PR can
be judged on whether it stores the right things.

## Recording, retention, and sharing

Proposed default: local transcript recording on, visibly labeled before starting;
provide “Don’t save this conversation” and “Metadata only” choices. This default is a
product decision to approve before implementation. The choice applies server-side
throughout the call and continuation; telemetry must honor it too, not just SQLite.
Exact action arguments and typed payloads require a separate explicit per-call opt-in,
independent of ordinary transcript recording. By default all persistence, logs and
telemetry receive only redacted action summaries, never raw `fc.args` or `keys`.
Apply this gate as well as the call recording policy before serialization; QSDEBUG
cannot enable raw payloads. Test transcript-on/payload-off and all-recording-off modes.
Carry both policies through every content-bearing emitter:
`_Meter.note`/`emit_live_turn`, tool-handler `telemetry.emit_action` raw arguments and
typed text, derived summaries, local diagnostic logs and exporters. Process-wide
`QSDEBUG` must not override a call's opt-out. Enforce suppression before serialization,
not merely before inserting SQLite rows, and test each path with QSDEBUG enabled.
An opted-out transcript must not leak through `_Meter`'s telemetry tail. Continuing a
non-recorded call cannot reconstruct missing turns and should say so.

Proposed retention: content for 30 days, usage totals for 90 days, with explicit
“keep” and configurable policies. Run bounded deletion jobs that remove turns,
actions, derived summaries, search rows, and share snapshots consistently. Deleting
local data cannot retract downloaded exports or copies in separately retained backups;
explain that in the deletion UI. SQLite deletion is logical deletion, not a promise of
forensic erasure. Storage settings should show approximate size and allow clearing
history without clearing structural pane history.

Deletion must fence queued and in-flight writes. Persist a conversation tombstone and
epoch in the same serialized transaction as the deletion barrier, before enumerating
its calls. Every create/continue/fork/attachment transaction checks the parent/source
conversation epoch atomically; a deleted source cannot authorize a new call or fork.
Concurrent operations either commit before the barrier (and are included in its
scope) or fail afterward. A fork committed before deletion is an independent explicit
copy, disclosed alongside exports; it is not silently recreated by retries. Idempotent
retries recheck tombstones instead of returning deleted records. Test these races. On the serialized writer, atomically
mark each affected call ID tombstoned and advance its recording epoch before deleting
content. Producers stop accepting new events for those calls and discard queued
content; every write checks the persisted tombstone/epoch in its transaction. Events
carry the epoch captured when accepted, never the current epoch at dequeue time.
Writes committed before the barrier are deleted; later writes are rejected, including
retries after restart. Retain minimal ID/epoch tombstones without transcript content,
never reuse call IDs, and do not let event ingestion recreate a missing parent call.
The delete response waits for the barrier and deletion commit. Test a delayed writer,
in-flight checkpoint, producer race and daemon restart against this ordering.

Phase one sharing is a downloadable Markdown/JSON transcript with a preview: select
turns, remove pane labels, redact commands and identities, optionally include cost.
Version the export schema. Exporting does not grant terminal access or allow actions.

Authenticated links are a later phase. The current tunnel is not a public sharing
service; do not expose the daemon or SQLite to serve an anonymous link. Within an
existing authenticated deployment, enforce owner/recipient ACLs on an immutable
snapshot. A future external service requires a separate deployment/access design.
If bearer links are ever supported, use high-entropy tokens stored hashed, expiration,
revocation and bounded reads, with explicit disclosure that anyone holding the link
can read it. Revocation prevents future fetches, not previously downloaded copies.
An incoming shared conversation may be imported only as inert context into a new,
private conversation, never as an active session with inherited pane permissions.

## Delivery sequence and acceptance

1. Local schema and normalized event writer; transcript opt-out applies to telemetry;
   crash/disk-full recovery; no raw audio. Test migrations, idempotent events, partial
   turns, revisions, queue overflow, permissions and opt-out end to end.
2. History list/thread and estimated usage. Test pagination during writes, cross-owner
   denial, cost snapshots, cumulative/delta reconnect accounting, missing usage and
   retention deletion including derived/search data.
3. Continue/fork with bounded prior context and fresh pane inventory. Test provider
   changes, stale pane IDs, concurrent calls, interrupted turns, and no action replay.
4. Reviewed local exports; then separately approved authenticated sharing. Test field
   redaction, export opt-out, snapshot immutability, expiry/revocation and ACL denial.

Open decisions: recording default, retention periods, whether metadata-only calls
appear in history, whether exact typed payload recording is ever needed, and whether
sharing initially means downloads only or an authenticated deployment feature. No
historical backfill is promised: old bounded telemetry tails cannot establish complete
turn sequences or reliable cost scopes. Any importer must label partial provenance
and remain separate from live collection.
