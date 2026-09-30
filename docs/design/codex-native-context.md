# Codex native context for terminal observation

Status: research and proposed design; no adapter implemented or deployed.
Read-only investigation: September 30, 2026, Codex CLI and local daemon 0.159.2.

## Decision

Use the Codex app-server as an optional source of structured context for the existing
terminal observer. Keep the terminal authoritative for what is visible and actionable
in a pane **now**. Native metadata can explain a conversation, corroborate a scrape,
and supply history that is no longer visible; it must not silently override the screen
or authorize input.

This extends the [OpenBus narrative](openbus-narrative.md), rather than replacing its
universal observation path. Codex, Claude Code, OpenCode, shells, and future agents
should expose evidence through the same bus even when their native protocols differ.
The terminal path continues to work without any native adapter.

The attractive first use is better context, not another remote-control implementation:
join a pane to its Codex thread, fetch a bounded metadata snapshot, and let the existing
semantic pass use that alongside the capture. Native search and history can independently
improve session discovery. Actions remain on the existing, guarded control path.

## What was actually inspected

The investigation used the installed executable's help and generated experimental JSON
schemas, read-only SQLite queries, and read-only RPCs over the existing daemon's Unix
WebSocket. No user thread was resumed, no turn started, no menu answered, no approval
resolved, and no daemon restarted or remote-control service enabled.

The distinction between a verified read and a possible feature matters:

| Surface | Observed locally | What it establishes |
| --- | --- | --- |
| `codex agents` / command-center UI | CLI help identifies it as browsing sessions on the shared local app-server daemon | The UI has a native local session inventory; it is not merely a tmux listing |
| `app-server daemon version` | Existing managed daemon and CLI both reported 0.159.2 | A versioned native service is already present on this host |
| Local socket + `initialize` | Direct Unix WebSocket handshake succeeded | A separate, read-only client can inspect this daemon without driving the TUI |
| `thread/loaded/list` | Eight loaded IDs in the sampled snapshot | Runtime inventory is available, separately from persisted history |
| `thread/list` | Default DB-only listing returned 25 records; an all-source DB-only listing returned 87 | Inventory filters and loaded inventory are not interchangeable |
| `thread/read`, without turns | Successfully read metadata and runtime status, including four loaded IDs absent from the all-source listing | Read-by-ID is useful for reconciliation; neither list alone is sufficient |
| `thread/turns/list` | Completed and in-progress turns with timestamps and duration | Structured turn state can supplement visible output |
| `thread/items/list` | Paginated items with turn IDs and item timestamps | History can be fetched selectively instead of scraping an entire rollout |
| `thread/searchOccurrences` | Returned item IDs, snippets, match ranges, and turn references | Within-thread history search is available |
| `thread/search` | Returned matching threads and snippets | Native discovery may supplement the cross-agent resolver |
| `thread/queue/list` | Successful empty response | Read support verified; queue behavior was not exercised |
| `thread/backgroundTerminals/list` | Returned a command/process entry | Native subprocess metadata exists; the inspection command itself may appear here |
| `thread/attachment/list` | Empty response; sampled attachment table also empty | No evidence that this installation uses attachments to represent PR ownership |
| `model/list` | Eight model entries with reasoning-effort metadata | A catalog is available; account access and successful model selection were not tested |
| `project/list` | Empty response; sampled threads had null project IDs | Grouping by working directory in the TUI does not prove native project objects exist |
| Passive notifications | No turn/item/status events during a 12-second observation after reads | Event delivery to this observer is unproven, not proven absent |

These are dated observations, not compatibility guarantees or performance benchmarks.
Names, transcripts, account information, and full thread IDs from the user's sessions
are intentionally omitted from this document.

### Two important completeness surprises

The all-source DB-only list still omitted four of the eight loaded IDs. All four could
be read directly; they reported non-ephemeral threads and paginated history. Three had
parent thread IDs and one did not. The cause was not established. It would be wrong to
explain this away as just an ephemeral-thread filter or assume all missing records are
subagents. Reconcile the union of listed and loaded IDs, subject to scope and limits.

The observer received initialization-related account and remote-control notifications,
but no turn/item/status events during the short passive window. A read is not evidence
of a subscription. The installed request schema includes unsubscribe, but did not expose
a standalone subscribe method. Do not resume a user's thread merely to acquire events.
Start with snapshots; investigate event delivery independently with disposable sessions.

## How the command center fits the local storage

This version has more than a directory of transcript files:

| Store | Relevant contents | Limitation |
| --- | --- | --- |
| `~/.codex/state_5.sqlite` | Thread identity, names, cwd, model settings, archive/source metadata, rollout paths, project/section/spawn relationships | The sampled thread table has no live activity-status column |
| `~/.codex/thread_history_1.sqlite` | Structured turns and items, statuses, timestamps, item types, projection bookkeeping | Private, versioned storage schema; not an adapter contract |
| Rollout JSONL | Conversation events and session metadata | Large, unstructured for selective reads, and subject to format changes |
| `~/.codex/session_index.jsonl` | Append-only naming information used by the current history reader | A name index, not a runtime-state source |
| App-server runtime | Loaded threads and current status, accessed through RPC | Availability, event scope, and protocol version must be handled |

The state database contained 97 threads in the sampled snapshot: 80 with paginated
history and 17 legacy. Paginated did **not** mean that the existing JSONL reader had
lost the conversation. Two checked threads still had rollout files of roughly 152 MB
and 32 MB; the current reader's user-message counts matched the history database's
`userMessage` counts, 117 and 70 respectively.

There is no basis here for an emergency rewrite of `agent-history`. Structured native
reads are useful because they can paginate, select item types, and report runtime
metadata—not because the existing reader was shown to be broken.

Prefer the app-server protocol for a live adapter. Keep the existing file reader as
an independent, cross-version fallback for discovery. Direct database access is useful
for diagnosis, but should not become a second writable owner of Codex's state. Never
copy these databases into the repo or scrape authentication files for connectivity.

## Useful RPCs and their boundaries

The [official app-server documentation](https://learn.chatgpt.com/docs/app-server)
describes reads separately from loading/resuming a thread. The installed generated
schemas provide the more precise, version-specific shapes used in this investigation.
An experimental method being present in a schema is not proof that every server version
implements it or that its effects are harmless.

### Identity and inventory

Use `thread/loaded/list` for loaded IDs, bounded `thread/list` pages for discovery, and
`thread/read` with `includeTurns: false` for the small record needed by a pane. In the
observed version, `useStateDbOnly: true` requests database-only listing; it avoids
turning routine observer polling into a rollout discovery/repair scan.

Runtime statuses observed were `active`, `idle`, `systemError`, and `notLoaded`.
Waiting-on-approval and waiting-on-user-input flags appear in the documented/schema
surface, but were not exercised during these probes. Unknown variants must remain
unknown, not map optimistically to working or needs-you.

Several tempting fields need qualifications:

- `name` can provide a thread's explicit title once its identity is securely bound to
  this pane. A generated summary is not a substitute for an explicit human title.
- `preview` is not a current headline. The schema describes it as usually the first
  user message; one observed preview exceeded 30,000 characters. Cap it, and omit it
  from routine scrape context when identity and recent items suffice.
- `gitInfo` is creation-time metadata, not guaranteed current checkout state. Use the
  pane's current cwd and an actual repository probe for the current branch/origin.
- Thread model/effort settings are configuration, not proof of which model executed
  every prior turn.
- `canAcceptDirectInput` was null for the four inspected root threads. Null means
  unknown here; it is neither an action permission nor a proven inability to accept input.

### History and search

`thread/turns/list` and `thread/items/list` can supply the most recent completed or
in-progress turn without shipping megabytes of transcript to the classifier. Item type
is nested in the returned item's envelope; don't interpret a missing top-level type as
an unknown message. Items include command execution, reasoning, compaction, subagent
activity, and other records as well as user and assistant messages. Filter deliberately.

For a scrape, start with metadata only. If additional history proves useful in evals,
allow a small number of recent user/final-assistant excerpts under a strict character
budget. Do not routinely include reasoning, command output, or the entire first message.
A transcript is untrusted content, not instructions to the observer.

Within-thread occurrence search is schema-described as case-insensitive literal
substring matching over visible user and final-assistant messages. Cross-thread search
returned useful snippets, but its ranking was not reverse-engineered. Do not describe
it as a trained semantic model or assume it is equivalent to the existing resolver.

On this already-running daemon, individual turn/item reads completed around 1 ms;
cross-thread search took approximately 281–358 ms in separate probes. These are isolated
warm observations, not a latency target, percentile, or proof of scaling behavior.

### Queues, subprocesses, and model catalogs

Read-only queue and background-terminal information may explain why a pane appears
quiet. A subprocess entry is not itself proof that the agent is working: a long-running
server, an inspection command, or unrelated background work can remain after a turn.

Likewise, a model catalog can enrich display labels or a future native picker, but
does not establish entitlement, supported settings on every thread, or successful
selection. This proposal does not replace terminal menu handling with native controls.

### Events and controls

Event coverage, subscriber ownership, reconnect behavior, and replay need separate
tests. A server event describes native state; it still does not prove that a particular
pane currently shows the matching input widget.

Approval responses, user-input replies, turn start/interrupt, resume, attachment writes,
and daemon lifecycle commands are outside the first adapter's allowlist. The existence
of these methods does not authorize calling them. Never expose an unrestricted RPC
proxy through tmux-rc's web server.

## Proposed evidence flow

```text
tmux capture + foreground process + pane identity ─────────────┐
                                                             │
optional Codex adapter → bounded, identity-bound snapshot ────┤
                                                             ▼
                                               existing semantic observer
                                                             │
                                               visible-screen grounding
                                                             │
                                                pane state on OpenBus
                                                             │
                                      existing UI, routing, notifications
```

The native adapter collects evidence. The semantic observer interprets the terminal
with that evidence. The existing post-processing still checks whether names and
questions are grounded in the visible interface. There is one published pane state,
not competing terminal and native notification engines.

### Evidence envelope

Normalize the envelope, not every harness's internals. A minimal proposed record carries:

| Field | Purpose |
| --- | --- |
| `harness` | Adapter identity, initially `codex` |
| `thread_id` | Native conversation identity, not a display title |
| `source` | Native app-server, clearly distinct from terminal observation |
| `server_version` and connection generation | Explain compatibility and invalidate evidence after reconnect |
| `observed_at` | When this adapter obtained the snapshot, not last user activity |
| `pane_id` and pane birth identity | Scope evidence to the current physical pane incarnation |
| `binding` | How this thread was matched to this pane and whether that match is ambiguous |
| `name`, configuration, native status | Optional typed metadata; unknown values remain unknown |
| Recent turn reference/timestamps | Optional bounded context, not an invented physical activity clock |
| `completeness` | Whether a list/page/history read was partial or unavailable |

Do not send all these bookkeeping fields to the LLM. Retain provenance internally and
inject only a small, useful subset. A proposed input budget is metadata plus at most
two short excerpts, with a total hard cap of a few thousand characters. Tune that budget
from evals, not by adding pages of instructions or copying failing screens into prompts.

Native text should be quoted as data in a clearly separate context block. One concise
instruction can state that it is supporting context and that current controls/state
come from the visible screen. Harness-specific interpretation belongs in the Codex
prompt component, not a growing shared prompt appendix.

### Binding a thread to a pane

The current code already has useful machinery: `live.py` can match a Codex thread ID
to classified pane status chrome, and `agent-history` avoids treating the shared
daemon's inherited tmux environment as proof of a pane association.

Reuse and test that identity logic rather than looking for an arbitrary UUID anywhere
in scrollback. Printed JSON, other-pane listings, and quoted transcripts can all contain
valid thread IDs that do not identify the host conversation.

Invalidate the binding when the pane dies, its birth identity changes, the foreground
harness changes, or a different current thread ID appears. Do not retain a prior thread's
context under a reused pane number. A thread can appear in more than one pane; that is
not permission to pick one arbitrarily for routing. Retain candidate bindings and use
current pane state to resolve the request, or report ambiguity.

No visible ID means no confident join. Continue terminal-only classification. A native
thread without a matching tmux pane is not a new pane; a future native-session view may
display it separately, with its own explicit control and access design.

### Resolving disagreement

| Situation | Treatment |
| --- | --- |
| Screen shows a live choice; native snapshot says idle | Screen remains actionable; native state is conflicting context, not grounds to hide the choice |
| Native snapshot says waiting; screen shows an ordinary empty input prompt | Do not resurrect a menu or send a notification solely from the native hint |
| Native name exists but screen identity is unbound | Do not assign the name to the pane |
| Bound native name and explicit current UI title disagree | Prefer the explicit current UI title; refresh the metadata and preserve the conflict for diagnosis |
| Native status changes while pane contents are unchanged | It may warrant a bounded re-scrape, not automatic publication of native activity |
| Native turn completes while an old picker is still visible | Re-observe; completion does not establish that this visible picker was answered |
| Adapter unavailable or snapshot stale | Exclude native context and continue the existing terminal path |

In particular, native waiting flags do not bypass `_ground_visible_fields` or the
question's visible-text checks. The adapter may help the LLM understand a capture,
but cannot manufacture a currently actionable question that is not on the screen.

## Integration points in tmux-rc

### Watcher and classifier

Implement the adapter as an optional worker/cache outside the watcher's hot capture
loop. Reuse one bounded connection per configured local Codex server; do not open a
socket or search history for every pane on every tick. Fetch metadata primarily for
bound threads. Cache discovery separately and refresh it less often than current-thread
snapshots. Event support can replace some polling later, after its semantics are verified.

`classify.py` already has a parser-context builder for foreground/repository information.
That is a natural seam for bounded native context, with freshness and binding checked
before inclusion. The classifier should receive a snapshot, not perform RPCs itself.

Keep the watcher fingerprint distinctions intact:

- `_seen_fp` measures physical capture changes for activity clocks.
- `_prev_fp` represents the successful semantic read.
- `parsed_at` acknowledges an actual classifier pass, not an adapter poll.
- `_deck_fp` includes state that wakes UI clients; a poll timestamp must not change it
  on every tick when the evidence is otherwise identical.

Fingerprint meaningful native values, excluding observation timestamps. If they change,
coalesce and rate-limit any extra semantic pass. Don't recreate the idle-pane flapping
problem by parsing on every inventory refresh. Don't backdate pane activity from thread
`updatedAt`, because a rename, metadata change, or work in another pane can affect it.

This work must also preserve the independent physical capture, semantic parse, and
conversation-progress clocks described in [pane history](pane-history.md). A daemon
restart or data gap does not create observed work during the gap.

### Discovery and the session resolver

Native search can be an additional Codex candidate source for `agent-history` or Live
Mode discovery, not a replacement for Claude and other harnesses' search. Deduplicate
by harness/thread identity, retain provenance, and continue to distinguish a historical
candidate from a currently bound pane.

Never resume a thread because a search result is absent from loaded inventory. Never
automatically feed an entire historical conversation to an agent. Search returns evidence
for choosing a session; routing and user-authorized resume remain separate operations.

The existing Go reader already handles Codex rollouts and explicit names, so a native
candidate source should be justified by measured quality/latency improvements. It should
not make that optional utility depend on a running daemon for its baseline behavior.

### Notifications and PR associations

Keep one existing push manager. Its pane identity, question-generation, and action
guards still apply. Native flags can trigger observation or supply context, not a second
push stream with a different definition of needs-you.

PR relationships remain semantic bus state. A native cwd, branch, attachment, search
hit, or PR mention is not proof that this conversation is implementing or reviewing it.
The [PR lifecycle proposal](pr-association-lifecycle.md) covers active/done/removed
relationships and session-specific context; native history can help interpretation, but
must not accumulate every PR from a listing or make associations permanent.

Keep GitHub's title/state separate from the session's reason for working on the PR.
No native attachment writes are needed for that design.

## Availability, privacy, and control boundaries

Connection is opt-in and local in the first implementation. Locate the server using
supported CLI/configuration metadata, not a hardcoded per-host `/tmp` socket target.
The observed stable socket path lives under `~/.codex/app-server-control/` and points
to a private temporary socket. Treat its layout as version-specific, validate owner and
permissions, and don't scan arbitrary sockets or expose it through the tunnel.

Do not automatically bootstrap, start, update, or restart a daemon just to observe it.
Those operations can affect user sessions and require separate authorization. A missing
daemon is an ordinary fallback condition, not a tmux-rc outage.

Use an explicit read allowlist, bounded request timeouts, bounded pagination, capped
response/excerpt sizes, and reconnect backoff. Drop unrelated account/auth notifications
without logging their payloads. Store no native transcripts by default. Diagnostic logs
should contain method names, timing, counts, version, and error categories—not messages,
tokens, account IDs, or whole responses.

Separate compatibility failures from empty results. Unknown methods, null capabilities,
unknown status variants, truncated history, and timeouts must remain visible as adapter
health, without changing the pane's state to idle or needs-you.

Server-to-client approval/input requests must not be answered by this observer. If a
future control adapter is built, it needs an independent authorization design binding
the native request ID, thread, pane generation, displayed question, and explicit user
action. Observing a request is not permission to satisfy it.

## Local, remote, and cloud are different scopes

The installed CLI supports remote transports and an experimental remote-control surface.
The [remote-connection documentation](https://learn.chatgpt.com/docs/remote-connections)
describes connecting clients to other machines. That does not make this host's local
socket a public service or justify tunneling it directly to a browser.

There are also [cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)
and a [new cloud-work surface](https://learn.chatgpt.com/docs/whats-new/devday-2026).
These are distinct from proving that a web UI can browse this machine's existing local
TUI threads. That local-to-web inventory/control path was not verified here. Don't
promise automatic cloud synchronization of local history based on a product announcement.

An OpenBus adapter may eventually normalize evidence from local, remote, and cloud
agents, but each needs an explicit host/account scope and identity. The same thread ID
on a different configured endpoint must not accidentally inherit this pane's binding.

## Rollout and verification

### Phase 1: shadow observation

Add the read-only worker, binding, cache, and diagnostic counters behind an opt-in flag.
Compare native snapshots with terminal observations without changing classifier input,
published activity, notifications, or controls. Measure API latency, cache age, binding
coverage, disagreement, and request volume. Leave no daemon-management side effects.

### Phase 2: bounded scrape context

Supply identity-bound metadata to the existing semantic pass. Add matching eval samples
and A/B terminal-only versus terminal-plus-context, with repeated runs for noisy cases.
Test both improvement and resistance to conflicting/stale native evidence. Follow the
repository's mandatory prompt/classifier eval workflow; minimize prompt changes.

### Phase 3: optional discovery and history

Evaluate native search as an additional candidate source and bounded recent items as
an opt-in context expansion. Confirm that paginated and legacy threads, disconnected
servers, and users without the daemon still work through existing readers.

### Later: events and native controls

Only after independent protocol tests establish observer subscription behavior should
events replace snapshot polling. Native controls are a separate design and review, not
an unnoticed expansion of the read-only context adapter.

Required adapter tests include:

- Handshake/version mismatch, unsupported methods, null capabilities, and unknown enums.
- Timeout/reconnect with no capture-loop blocking and no automatic daemon startup.
- Loaded IDs missing from inventory, partial pagination, and read-by-ID failures.
- Pane reuse, a changed thread in the same pane, quoted foreign IDs, and ambiguous bindings.
- Explicit title preference and native metadata belonging to a different conversation.
- Current menu versus stale native idle; empty prompt versus stale native waiting.
- Unchanged poll results causing no parse, activity-clock change, or client wakeup.
- Metadata change coalescing, stale context expiry, and terminal-only fallback.
- Oversized previews/items, malicious transcript instructions, and secret-free logging.
- No writes: a fake transport should fail the test on any method outside the read allowlist.

Real end-to-end tests should use disposable agent sessions in their own tmux panes.
Observe genuine turns and menus, verify the terminal path still wins conflicts, and
verify that merely attaching the observer neither resumes work nor answers a question.
Do not use a user's working pane as a protocol test fixture.

## Reproducing the read-only investigation

These commands inspect capabilities without managing the daemon:

```sh
codex --version
codex agents --help
codex app-server --help
codex app-server daemon version
codex app-server generate-json-schema --experimental --out /tmp/codex-protocol-inspect
```

The last command writes generated schema files to the chosen temporary directory.
It does not contact or change a user thread. Keep generated schemas and raw probe
output out of this repo unless there is an explicit, scrubbed fixture requirement.

For the observed Unix WebSocket transport, send an `initialize` request with client
information and experimental capabilities, then an `initialized` notification. A small
read sequence is `thread/loaded/list`, bounded `thread/list`, and `thread/read` with
`includeTurns: false` for a selected ID. The exact parameters and wire framing should
come from the installed version's schemas, not a permanent copy of this experiment.

A separate attempt through `app-server proxy` timed out waiting for initialization;
direct Unix WebSocket succeeded. The proxy cause was not diagnosed, so this is not
evidence that proxy transport is generally broken.

Useful repository entry points for implementation review:

- `openbus/watcher.py`: captures, semantic cadence, fingerprints, and published pane state.
- `openbus/classify.py`: parser context, composed prompts, visible-screen grounding.
- `openbus/live.py`: thread-to-pane matching and routing.
- `agent-history/codex.go`: independent rollout/name reader and running-session discovery.
- `openbus/agent_history.py`: optional resolver integration.
- `openbus/push.py`: existing notification/action ownership.
- `openbus/server.py`: authenticated application boundary, not a raw native RPC proxy.

## Questions still open

Why did DB-only all-source inventory omit readable loaded threads? What grants a truly
read-only observer event delivery, and can it subscribe without changing thread state?
How are events scoped when multiple TUIs and observers attach? Is replay available,
and how should a reconnect identify gaps? What is the cheapest refresh strategy on large
histories? Which experimental methods remain stable across upgrades? How do explicit
titles propagate between multiple attached clients? Does native search improve the
cross-harness resolver enough to warrant its dependency?

These uncertainties don't prevent the proposed first step. Small, provenance-bearing
snapshots can already supplement terminal observation. They do prevent treating the
native service as an authoritative replacement for the terminal or shipping a broad
remote-control adapter on the strength of a few successful reads.
