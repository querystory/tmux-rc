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

| Record | Fields and purpose |
| --- | --- |
| Conversation | UUID, owner identity, editable title, created/updated times, archive state, optional parent conversation and fork point. Stable across calls. |
| Call | UUID, conversation ID, start/end times, end reason, selected provider/model, transcript recording mode, exact-action-payload opt-in (separate persisted fields), heartbeat, daemon-generation/lease owner. One explicit Start-to-End interaction. |
| Connection | UUID, call ID, provider connection ID when available, model actually used, timestamps, reconnect reason. Transport provenance only; references a separate accounting scope. |
| Turn | UUID, conversation sequence, call/connection IDs, optional fresh request ID foreign key, role, text, start/end times, partial/final/interrupted state, optional provider item ID. |
| User request | Server-issued UUID, authenticated owner, call ID, user-turn ID, allowed tool/pane scope, creation and revocation times. Unique (call_id, request_id); immutable call/owner association. Imported/history turns cannot create one. |
| Action | UUID (daemon-issued action key), call ID, fresh user-request ID, turn ID when known, provider tool-call ID for provenance, verb, stable pane identity and label snapshot, argument summary, outcome, submitted flag. Unique (call_id, action_key). Never imply a sent command completed its underlying task. |
| Accounting scope | UUID, owning call, provider scope key when available, counter semantics, start/end, completeness. Stable across transport reconnects that preserve provider counters. |
| Usage | Accounting-scope ID, connection ID for provenance, source event ID or local sequence, cumulative/delta semantics, input/output tokens split by text/audio/cache when reported, audio duration when reported, final/provisional/completeness flags. |
| Price snapshot | Provider/model, currency, effective time, units and rates actually used, source/version. Immutable UUID plus the rate snapshot used for each estimate; Estimate references it by foreign key. |
| Estimate | Immutable UUID/revision, accounting-scope ID, usage revision, price-snapshot ID, currency, integer micro-unit amount, completeness and optional superseded estimate ID. Append a revision when usage changes; never update historical estimates or use current rates implicitly. |
| Share snapshot | UUID, owner, selected conversation range, redacted export payload, creation/expiry/revocation metadata and access policy. Only if sharing is enabled. |

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

Each adapter declares whether its usage messages are cumulative snapshots or deltas,
and the exact reset scope. For cumulative messages, replace the latest snapshot for
that accounting scope; do not sum every turn's snapshot. Order samples by a documented
provider revision/sequence within the scope, not arrival time. Ignore older revisions;
conflicting equal revisions make the estimate incomplete. Without a reliable ordering,
retain per-dimension monotonic maxima for documented cumulative counters and mark
unexplained decreases/resets incomplete; never overwrite with smaller delayed samples.
Keep provider corrections separate when their revision semantics cannot be verified.
Test delayed pre-reconnect samples arriving after newer counters. For deltas, deduplicate event IDs
and sum once using a unique (accounting_scope_id, event_namespace, source_event_id)
key. Adapters declare whether IDs are scope-wide or connection-local; use connection
ID as the namespace unless scope-wide uniqueness is documented. A connection-local
ID cannot prove replay identity across reconnects; mark ambiguous totals incomplete
rather than claiming exact deduplication. Test reused IDs on new connections. Where only
local receive sequencing exists, retain (connection_id, receive_sequence) provenance
and mark uncertain cross-connection replay as incomplete, never silently counted twice.
A reconnect that resumes the same provider accounting scope retains its
scope key; a genuinely new scope gets a new key. Counter resets without a reliable
scope signal are flagged as incomplete instead of guessed.

An accounting scope belongs to exactly one call in phase one. Explicit Continue
starts a fresh provider scope; do not reuse a native billing scope across calls.
Conversation totals sum disjoint accounting scopes, not call summaries plus their
turns. A resumed call contributes new usage only. Preserve unknown dimensions as null,
not zero. Prices are estimates calculated with the saved rate snapshot; changing
configuration tomorrow must not rewrite yesterday's estimate. Do not count cached
input twice. Keep voice-service charges, model charges, and optional backend charges
separate until their billing boundaries are known to avoid adding an inclusive price
to its own components. Use integer micro-units or decimal amounts, never binary float
as the authoritative persisted monetary amount.

Initial UI: total calls, active duration, turns, actions, estimated cost, and explicit
“partial usage” badges. Later: daily cost by provider/model and conversation. Per-turn
cost is available only when reporting boundaries support it; otherwise show a call
estimate rather than inventing attribution.

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
Opening one shows the message thread, action
receipts, partial/interrupted markers, and a compact usage summary. Reuse the live
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
Only a new user request can authorize actions; historical content cannot authorize
action replay. Persist a server-issued request ID for each fresh authenticated user
turn and associate the provider response with that request on the server. Bind every
action receipt to that call/request ID; never accept a provider-supplied request ID as
authority. Historical/imported turns cannot mint these IDs. Reject or require explicit
user confirmation for tool calls with no fresh request association, including unsolicited
calls after loading history. Recheck the request's call/owner, allowed tool/pane scope
and revocation before dispatch. Test history-only tool calls with a fabricated ID and
replayed requests from another call. This is a required change to the current automatic
Live tool execution path, not an existing guarantee. Require new tool-call validation and current pane-lifetime matching.
Test malicious transcript/argument instructions with current tools enabled.

Provider-native resumption, if supported, is a separate optimization with its own
expiry and credentials handling; it is not the durable history contract. Never store
resume secrets in exports. A user may fork an old point or change models; record the
parent and selected model explicitly. Permit only one active call per conversation
initially; reject a second with a clear “already active” response or offer a fork.

Make Start/Continue/Fork idempotent with a client request UUID scoped to the verified
owner and a stored request digest. A unique constraint returns the original call on
retry; reusing the key with different arguments returns 409. Create the call and its
active-conversation lease in one transaction. A browser transport reconnect attaches
to that existing authorized call via its ID; it does not create a new conversation or
call. Reject cross-owner attachments and supersede the old socket generation so only
one capture stream can drive the call. Fence every inbound audio/control frame and
provider send with the current lease/socket generation, using the same per-call
serialized execution path as takeover. Recheck after awaits before any side effect;
discard stale queued audio, stop/mute/configuration messages and stale socket cleanup.
An old handler exiting cannot close or stop the replacement call. Test delayed audio
and stop frames, plus old-socket finalization, after takeover. A prior-generation lease after daemon restart ends the
old call as interrupted; continuing creates a new call and billing scope. Recording
policy comes from the stored call, not reconnect parameters. Serialize socket takeover
and action dispatch through the same per-call execution lock. Check the persisted
lease and socket generation immediately before receipt reservation and immediately
before terminal dispatch while holding that lock; stale in-flight actions must fail.
A takeover waits for an already-dispatched action to finish recording its outcome,
and cannot authorize queued actions from the previous generation. Test a paused old
receiver that resumes after takeover. Policy changes and call termination use the
same fence. Both transcript and exact-payload policies come from their separate stored call fields;
reconnect restores both, and Continue inherits both unless explicitly changed by the owner. The `/api/live-mode`
WebSocket handshake must establish the same verified owner as the history APIs before
accepting Start or attachment. The current client-supplied `session` and logging-only
`_actor` are not authorization. Fail closed on missing/unverified identity; look up
requested calls under that owner and reject unauthorized IDs before opening a provider
connection. Apply the explicit single-owner policy to WebSockets too. Test absent,
forged and cross-owner identities on both initial connection and reconnect.

For tool execution, reserve a unique (call ID, daemon-issued action key) receipt before
sending input to tmux and record its outcome afterward. Provider tool-call IDs are
provenance only, not reliable idempotency keys across reconnects. Bind each action key
to its normalized arguments and pane lifetime; reject reuse with different arguments.
After a provider reconnect, if an action cannot be tied to an existing action key or a
new user request, require explicit user confirmation before execution, even if the
provider supplied a new tool-call ID. Do not deduplicate by argument equality alone:
the user may intentionally repeat an action. Test replay under both unchanged and
changed provider IDs, and intentional repetition. A crash between send and acknowledgment has
an uncertain outcome: expose that uncertainty and never automatically resend. SQLite
and tmux are not an atomic transaction, so do not promise exactly-once side effects.

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
