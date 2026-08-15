# Design: prompt caching — where the classifier bill actually goes, and how to cut it

Status: **measured + direction chosen**, implementation pending. Companion to
[parse-cadence.md](parse-cadence.md), which decides *when* we call the LLM; this doc
decides *what each call should cost*. Everything here is grounded in live probes run
against the production model (Vertex `gemini-3.1-flash-lite`, 2026-08-15) and the fleet
OTel dataset — not provider marketing.

## The finding that motivates everything else

Every classify call re-sends the same ~6k-token parser prompt, and we earn **zero
cache discount for it**. Probes replaying the byte-exact production system prompt with
fresh pane content — the daemon's exact shape — hit Vertex's implicit cache **0 times
in 14 attempts**, even while the live fleet was hammering that prefix. The sister
project's caching experiments (qs-app `gemini-caching.md`) found the same thing:
Gemini's "prefix caching" does not, in practice, match a shared prefix followed by
divergent content. It matches *request extension* — the new request must begin with the
previous request, byte for byte.

The call layout itself is not the problem. The stable prefix (system prompt + a
one-line pane header) is ~6,070 tokens before the first churning byte, fleet-global,
and clears every provider's cache minimum. On Claude (one `cache_control` breakpoint)
or OpenAI (automatic prefix matching) this exact shape would cache today. Vertex is the
odd one out, and we happen to be on Vertex.

Scale of the miss: the system prompt is ~53% of the median call (10.5k tokens in), and
the classifier runs ~$5/day fleet-wide at real prices. A cached prefix bills at 10% of
list, so the theoretical recovery is **40–45% of the classify bill** — real money at
fleet scale, and the fraction grows if the fleet does.

(A second, embarrassing finding, fixed separately: the daemon's cost constants were
still 2.5-flash-lite's prices after the move to 3.1 — all reported costs were ~2.5×
under reality, and `cached_content_token_count` was discarded, so hit rate wasn't even
observable. The instrumentation fix is its own PR; this doc assumes it.)

## Options considered

### 1. Explicit CachedContent holding the parser prompt — **chosen**

Vertex's explicit cache API is the one mode with a *guaranteed* discount, and probes
confirmed it: create a `CachedContent` holding the system prompt, pass `cached_content`
instead of `system_instruction`, and every call reports exactly the prompt's 6,055
tokens cached — 3/3 hits, deterministic, latency unchanged. Storage is ~$0.15/day per
host at a 1h TTL; the read discount is 90%. Expected net: **−40–45% of the classify
bill**, with the model seeing byte-identical tokens — no prompt change, no behavior
change, no eval-harness burden. Implementation is ~25 lines: find-or-create keyed on
(model, prompt hash), recreate on NOT_FOUND, behind an env flag so it can be A/B'd
against the flag-off fleet in telemetry.

One trust caveat, hence the flag and the A/B: there is an unresolved mid-2026 report of
this exact model reporting cached tokens in usage metadata while billing full price.
The scoreboard for the A/B is the **GCP billing export**, not usage metadata.

### 2. Conversation-shaped calls (append-only transcript per pane) — deferred

The intuition: keep every prior turn identical and append only what's new, so each
request extends the last and implicit caching finally matches. Probes confirm the
mechanism works (extension requests hit 4/5 times, caching ~90% of the prior request),
and the "must call often enough to stay warm" worry dissolves on the data: 96% of a
pane's parses come within 60 seconds of its previous one — busy panes keep themselves
warm, and the 4% that go cold are idle panes whose parses are rare and cheap anyway.

Deferred because the economics only *tie* option 1 while carrying real risk. Cached
tokens cost 10% — a transcript that grows ~2k tokens per call re-buys its own history
at a discount until a periodic compaction (itself a full-price, cache-busting write)
resets it; net saving models out to ~−47% at a 20k-token cap, indistinguishable from
option 1's guaranteed −40–45%. Hit rates are stochastic where option 1's are certain —
including a reproducible implicit-cache dead zone right in our 9–17k operating range.
And it's the largest change on the table: a per-pane transcript state machine,
compaction policy, re-validated prompts (the transcript subsumes today's prior-frames
and recent-events feedback — a genuine continuity upside, but also adjacent to a known
repetition-degeneration failure mode the watcher already defends against). The hybrid —
explicit-cached system prompt *under* an implicit-cached transcript, ~−60% — is the
natural second step if the bill ever justifies it. Bank the guaranteed cut first.

### 3. Provider swap (the seam already exists) — measure, don't assume

The env-swappable model and the per-call telemetry were built for exactly this
comparison. Current-generation small OpenAI models list ~5× cheaper than flash-lite
*before* caching, with automatic prefix caching that our existing call shape would hit
without modification. Whether classification quality holds is unknown and is the whole
question — the eval harness gates it. This is a measurement to run, not a plan to
commit to.

### 4. Incremental summary maintained on the classify hot path — **rejected**

Proposed as: feed the current session summary into each tick and let the model emit a
delta when something major changes, replacing the periodic deep-read refresh. Rejected
on arithmetic and on correctness. The call it would replace costs ~$0.17/day
fleet-wide; the hot path it would fatten costs ~$5/day and is TTFT-bound — the phone's
perceived responsiveness. A summary field adds output tokens (and hundreds of ms)
precisely on busy ticks, on a model class that already needs trailing-garbage salvage
for its JSON. And a summary maintained only by deltas over 200-line windows drifts — a
game of telephone with no re-baseline — so the scrollback deep read can't be deleted
anyway; the proposal adds a second writer of the same field rather than removing a
call. The staleness it targets has a cheaper fix already sketched in parse-cadence.md:
trigger the deep-read refresh on the work→idle *transition* (where the idle-burst
summary already fires), keeping the cadence as a floor.

## Direction

1. Land the instrumentation truth fix (real prices + cached-token column) and let a day
   of fleet data confirm the ~0% baseline.
2. Explicit CachedContent behind an env flag; A/B a day each way; judge on the billing
   export.
3. Transition-triggered summary refresh (parse-cadence.md follow-up, ~10 lines).
4. Revisit the transcript hybrid and/or a provider A/B only with cached-token telemetry
   in hand and a bill that justifies the complexity.
