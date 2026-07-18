# Design: telemetry for the live view

Status: **draft / thinking** — an MVP is scoped here; the post-MVP section (billing
rollups, LLM-throttling wire-up, client render timing) is speculative. Captures what we
want to learn from live mode, why per-round is the natural unit, and how we turn a chain
of long-poll rounds into an attributable, summable "live-time" without a fragile
always-open session.

## The problem

The live view (docs/design/live-view.md) streams a pane's raw colored screen to the
phone over a long-poll endpoint — `GET /api/panes/{id}/live?frame=<hash>`. It is by far
the most expensive thing the daemon does per viewer: an idle parse is one small LLM call
every few seconds, but a *watched* pane ships a full ~13KB colored frame (≈2.8KB
gzipped) every time the screen changes, which for a busy agent is many times a second.
We shipped gzip (PR #35) on a hunch about the ratio. We are now flying blind on three
questions the product actually needs answered:

1. **What does live mode cost on the wire?** Real bytes per frame and in aggregate —
   raw *and* gzipped-sent, because the compression ratio is itself a number we tune
   against. "~13KB / ~2.8KB" is a guess from one log line; we should measure.
2. **How much live-time does each user spend?** Wall-clock seconds with a live view
   open, attributable and summable per user / session / pane. This is explicitly a
   **future billing signal** — "maybe we bill per live-hour" — so the number has to be
   defensible as a sum, not a vibe.
3. **How is live mode used at all?** How often, on which panes, under which tool
   (claude / codex / gemini / shell). Ordinary product-development debugging.

And one forward-looking want that shapes the design even though we won't fully build it:

4. **Adaptive daemon behavior.** The daemon should *know*, as first-class server state,
   whether a pane currently has a live viewer — so a later change can throttle the LLM
   parse cadence when nobody is watching. This must be real state the telemetry reads
   from, not something reconstructed from logs after the fact.

## The load-bearing constraints

Everything below is bounded by the existing telemetry contract (daemon/telemetry.py) —
we are extending it, not inventing a second one:

1. **Wholly optional, fail-closed.** No `OTEL_EXPORTER_OTLP_ENDPOINT` ⇒ every emit is a
   no-op. Telemetry must **never** break, slow, or fail the live request path. The live
   endpoint is a hot loop; the emit sits at the *end* of a round, off the critical path,
   wrapped so any error is swallowed.
2. **Privacy is client-side and fail-closed.** Default = numeric metrics + structural
   fields + hashes, **never pane text**. Live frames are pane content by definition, so
   the record carries *bytes and a frame hash*, never the frame itself — even under
   `TMUXRC_QSDEBUG` there is no reason to ship the screen (emit_parse already does that
   for accuracy diffing; live telemetry is about cost and time, not content).
3. **Own scope, own emit, reuse the plumbing.** Parse records are LLM benchmarks —
   every row has a model, latency, tokens. A live round has none of those. Folding live
   rows into `emit_parse` would poison every parse aggregate with null-model rows. So
   live telemetry gets its **own scope** (`tmux-rc.live`) and its **own function**
   (`emit_live`), but reuses `_emit_record`, the opt-in gate, the `otel_opt_in` resource
   attr, and the `pane_uid` / `pane_label` identity that already joins parse and
   lifecycle records. One receiver, one table, filtered by scope.

## The unit of emission: one record per completed round

A live "round" is one iteration of the long-poll handler: it holds open, captures the
pane every `LIVE_CHECK_SECONDS` (250ms), and returns the instant the frame hash changes
(a *change round*) or after `LIVE_HOLD_SECONDS` (~25s) of no change (an *idle round*).
The client re-holds immediately, so **a continuous viewer is a chain of back-to-back
rounds**. The candidate units:

- **Per 250ms capture** — absurd. That's 4 records/second/viewer of almost entirely
  "nothing changed"; it would dwarf the parse telemetry and tell us nothing a round
  boundary doesn't.
- **Per frame sent** — captures every byte, but says nothing about idle time (an idle
  round sends no frame yet is a real second of watching), so live-time would be
  undercounted exactly when the user is reading a still screen.
- **Periodic aggregate** (one rollup record every N seconds per viewer) — lowest volume,
  but needs a persistent per-viewer accumulator in the daemon that has to be flushed on
  disconnect, and disconnect on mobile is exactly the unreliable event we're trying to
  design around. It also blurs the change-vs-idle distinction.
- **Per completed round** — *chosen.* The round is the endpoint's natural choke point:
  the handler already computes everything the record needs (hash, changed-or-not, bytes)
  and already has a clean exit point. Volume is self-throttling: an idle viewer emits
  **one record / ~25s**; a busy viewer emits one per visible change, which is the
  fidelity we actually want (bytes track real change rate). No accumulator, no flush, no
  lifecycle to leak.

Each record carries: pane identity (`pane_uid`, `pane_label`), `tool`, a client
`session` id, the round's `hold_s` (wall-clock the round was open), a `changed` flag
(change round vs. idle timeout), and — on a change round — `raw_bytes` and `sent_bytes`
(pre/post gzip). Idle rounds carry no bytes (nothing was sent) so byte sums stay honest.

## Deriving live-time: sum round durations

Because rounds are back-to-back, **the sum of round hold-durations for a (session, pane)
approximates watch-time** — and it's a *robust floor*: every second a viewer was
connected is inside some round we emitted, so we can never over-count, and the only
under-count is the sub-second tail of the final, un-emitted round when the client
navigates away mid-hold (≤25s, usually far less). For a billing signal, an
undercount-only error that's bounded by one hold is the right bias — we'd rather bill
slightly less than a second we can't prove.

The alternative is an explicit **stream-start / stream-end event pair**: cleaner
"sessions", trivially summable. But the *end* event depends on the client firing a beacon
on `visibilitychange` / `unload`, which is notoriously unreliable on mobile (backgrounded
PWAs, killed tabs, dead networks) — the very platform this product targets. A missed end
event leaves a session open forever, which is worse for billing than a bounded
undercount. So:

- **v1: sum round durations.** No client cooperation required; the number degrades
  gracefully (a dropped connection just ends the round chain). This is the floor we can
  defend.
- **post-v1 (optional augment):** a best-effort start/end beacon can *refine* the
  boundaries (mark session open/close explicitly), but the round-sum stays the source of
  truth for billing. We design the record so a later beacon is additive, not a rewrite.

## Session identity: a client-generated id

There is no per-user auth identity inside the daemon. The only "who" it has is the
tunnel owner's email, forwarded as `X-Tunnel-User` and trusted **only from loopback**
(the same trust model as `_audit`, see server.py) — over the LAN it's an unverified
claim. That email answers "which account", but not "which viewing session": one user
opening the PWA twice, or leaving it open across days, is one email but many distinct
watch sessions, and billing/usage wants to tell those apart.

So we use **two attribution keys, layered:**

- **`session`** — a UUID the **client generates once per page load** and sends as
  `?session=<uuid>` on every live poll. It's the stable spine of one viewing session:
  it groups a round chain, survives the individual rounds, and costs nothing (no
  server state, no auth). It is *not* identity — it's a correlation key, deliberately
  anonymous, which also keeps it privacy-clean. The **invariant is that two different
  viewers never share a session id.** A round that arrives with no session (a caller
  that didn't send one — rare, since the client always mints one) is left
  **un-attributable**: we emit the round with the session key simply *absent*, so
  watch-time rollups EXCLUDE it. The tempting shortcut — bucket session-less rounds
  under a shared `"anon"` — is wrong precisely because it violates the invariant: it
  would sum unrelated viewers' hold-seconds into one phantom session and corrupt the
  per-session billing signal. Better to attribute to none than to the wrong one.
- **`actor`** — the loopback-trusted `X-Tunnel-User` email when present, recorded the
  same guarded way `_audit` records it (trusted only from loopback; a LAN claim is
  logged as a claim, never as the actor). This is the account key for "live-time per
  user" rollups. Absent (direct LAN use) ⇒ omitted, and the `session` still carries the
  usage story.

Recommendation: **client sends a per-page-load `session` UUID; the server derives
`actor` from the existing trusted header.** Live-time is summable per `session` always,
and per `actor` whenever the tunnel is in play — which is the case that matters for
billing.

## Presence: a first-class "has-live-viewer" flag on the watcher

The adaptive want (throttle parsing when nobody watches) needs the daemon to *know*, at
tick time, whether a pane has a live viewer — a server-side fact, not a log query. The
cheapest honest signal we already produce is the live poll itself: **a pane with a
recent live poll has a viewer.** So:

- The watcher gains a `live_seen: dict[pane_id → last-poll monotonic ts]`. The live
  handler stamps it on each round *after the first successful capture* — a viewer
  mid-hold is present, but a poll against a 404/wedged pane must NOT mark that pane
  watched (that would suppress future parse-throttling for a phantom viewer). This is
  one dict write per capture, no locking concern beyond the GIL (same pattern as the
  watcher's other per-pane dicts).
- A `has_live_viewer(pane_id)` predicate returns true if the last stamp is within a
  small window (a couple of hold-lengths, e.g. ~60s — long enough to bridge the instant
  between a round returning and the client re-holding, so it doesn't flicker false).
- **This is state, not events.** The round records still go to OTel for the after-the-
  fact questions; the flag is the *live* signal for in-process decisions. They share the
  same source (the poll) but serve different consumers.

**Why put it on the watcher rather than infer it from telemetry?** Because (a) telemetry
may be *disabled* (no OTel endpoint) and adaptive behavior must still work; (b) a log
round-trip to QueryStory and back is absurd for a decision the daemon makes every tick;
and (c) the whole point of item 4 is that presence is *server state*. Reconstructing it
from emitted logs would be the exact anti-pattern the goal names.

v1 **builds the flag and the stamp** (it's a few lines and it's the load-bearing piece of
the adaptive design) but **does not wire the LLM throttle** — that's a follow-up that
reads `has_live_viewer` in the watcher's parse-cadence decision. Shipping the signal now
makes that follow-up a small, isolated change instead of a cross-cutting one.

### Why not the alternatives (presence)

- **Reconstruct from OTel rounds.** Rejected above: breaks when telemetry is off, and
  puts a network round-trip in a per-tick decision.
- **An explicit open/close registry keyed on start/end beacons.** Same mobile-beacon
  unreliability as live-time; a missed close leaves a pane "watched" forever and would
  *suppress* parse throttling exactly wrong. The recency-window stamp self-heals — no
  poll for 60s ⇒ not watched, no cleanup needed.
- **Reference-count active requests.** Accurate, but needs increment/decrement around a
  request that can be cancelled mid-hold (client navigates away), and a leaked
  decrement pins the count > 0 forever. The timestamp is leak-proof by construction.

## MVP scope

- `emit_live(...)` in daemon/telemetry.py under a new `tmux-rc.live` scope, reusing
  `_emit_record` and the opt-in gate. Attrs: `session`, `pane_uid`, `pane_label`,
  `tool`, `hold_s`, `changed`; plus `raw_bytes` on change rounds (sent/ratio stay in the
  middleware log — see open questions); `actor` when the trusted header is present.
  Never the frame text.
- The `live_frame` handler emits exactly one `emit_live` per completed round, computing
  `hold_s` from a round-start monotonic clock and `raw_bytes` from the frame it's about
  to return (`len(data)`, encoding once and reusing it for the change hash).
- The watcher's `live_seen` stamp + `has_live_viewer(pane_id)` predicate, stamped from
  the handler after each successful capture.
- Client sends a per-page-load `session` UUID on every live poll (one line in
  `liveStream`).
- Tests mirroring tests/ style: `emit_live` builds the right attrs, is a no-op when
  disabled, and never sends frame text; `has_live_viewer` respects the recency window.

**What it deliberately does NOT do:** no billing rollups (that's a QueryStory-side query
over the summable rounds), no LLM-throttle wire-up (follow-up reading `has_live_viewer`),
no start/end beacons, no client-side render timing.

## Post-MVP

- **Billing rollups.** Sum `hold_s` per `actor` per day in QueryStory; the rows are
  already shaped for it. Decide the unit (live-hours) and the rounding there, not here.
- **LLM parse throttling.** The watcher reads `has_live_viewer` and drops parse cadence
  (or skips the LLM entirely) for panes nobody is watching — the payoff of the presence
  signal.
- **Start/end beacons** to tighten live-time boundaries, as a refinement over the round
  sum, never a replacement.
- **Client render timing** — time-to-first-frame, paint latency — would round out the
  cost picture but belongs to client telemetry, which doesn't exist yet.

## Open questions

- **Getting `sent_bytes` accurately.** The gzip happens in GZipMiddleware *after* the
  handler returns, so the handler doesn't see the compressed length directly. Options:
  (a) compress the frame ourselves in the handler just to measure (wasteful — double
  compression), (b) record `raw_bytes` + a sampled/estimated ratio and let the existing
  middleware log line (`/live -> N bytes`) carry the ground-truth sent size, or (c) read
  the ratio from a small rolling sample. Leaning (b) for v1: `raw_bytes` is exact and
  free, the middleware already logs true sent bytes, and the ratio is stable enough that
  we don't need per-frame gzip in the hot loop. Revisit if the ratio proves variable.
- **Presence window length.** ~60s bridges the re-hold gap without flapping, but if the
  parse-throttle follow-up wants faster "nobody's watching" detection, a shorter window
  trades responsiveness for flicker risk. Tune when the throttle lands, not before.
- **Idle-round volume for very long idle watches.** One record / 25s / viewer is fine
  for a handful of viewers; if live mode ever fans out widely, idle rounds could be
  coalesced (emit every Nth idle round, carrying a count). Not a v1 concern.
