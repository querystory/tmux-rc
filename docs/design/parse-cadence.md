# Design: parse cadence — when the daemon calls the LLM

Status: **implemented** (the heartbeat removal) + **draft** (the tailing-logs throttle,
a scoped followup). Captures why we parse a pane when we do, what a telemetry audit
revealed about waste, why the fix was a deletion rather than an addition, and the one
case that still parses more than we'd like.

## The problem

The daemon watches each pane and, when something meaningful changes, asks an LLM to
classify it (activity, headline, events). That classification is the single recurring
cost in the system — everything else is local string work. So the question "when do we
call the LLM?" is really "where does the money go?", and for a long time we answered it
with two triggers: parse when the screen's **content fingerprint changed**, or every
`HEARTBEAT_SECONDS` regardless, as a safety net.

A telemetry audit of the full OTEL dataset (keyed by `input_sha256`, the hash of the
exact text we sent the model) showed the safety net had become the main expense:

| Activity | Duplicate parse rate | Duplicate cost |
|---|---|---|
| idle | 84.2% | $76.94 |
| running | 73.6% | $38.78 |
| waiting | 86.1% | $7.83 |

**58% of the entire parser bill was re-classifying screens byte-for-byte identical to
ones we had already classified.** The shape is the tell: the *more static* a state, the
*higher* its duplicate rate. Idle and waiting panes — a shell prompt sitting still, an
agent blocked on a question — display a frozen screen, and the heartbeat dutifully
re-sent that frozen screen to the model every ten seconds forever.

## Why this was worse than just wasteful

Two problems, not one:

1. **Cost with no information gain.** Re-classifying an unchanged screen cannot tell us
   anything the previous classification didn't. Every one of those calls was pure loss.

2. **Instability.** The classifier is a non-deterministic LLM. Feeding it the *same*
   input repeatedly invites *different* answers — so the cards most likely to be
   re-rolled were exactly the ones that should have been most stable (idle, waiting). A
   pane's summary could change while nothing on the pane changed. That's a correctness
   bug wearing a cost bug's clothing.

## The fix: delete the heartbeat

The instinct on seeing "58% duplicate" is to add a deduplication layer — cache the last
`input_sha256` per pane and short-circuit on a match. That was the original proposal.
But it treats a symptom. The real question is why we were *generating* an identical
input to dedup in the first place, and the answer is: the heartbeat, by definition, fires
on time, not on change.

The heartbeat's stated justification was catching a screen that drifts *so slowly* each
individual tick's diff stays under the fingerprint's threshold. That justification turns
out to be already covered elsewhere. The change check compares the current fingerprint
against the fingerprint of the **last parse** — not the last tick — and that reference is
recorded only when we actually parse. So a screen drifting line by line still, tick over
tick, differs from *what we last classified*, trips the change check, and re-parses on
content alone. The heartbeat caught nothing the content check didn't already catch. It
only added the cost and the instability.

So the fix is a removal: parse on a real content change versus the last parse, or on a
**forced reparse** (the phone just sent input, so an answered question must clear from
the card promptly even before the screen visibly settles). No timer. An unchanged screen
is never re-parsed. This is strictly less code and strictly less spend.

Measured live after the change: duplicate rate on active panes fell from ~84% to
0–15%, and the residual is genuine content change, not repeats (see below).

## What the fingerprint deliberately ignores

The reason a *working* agent doesn't trip the change check on every tick is that the
fingerprint strips the pane's volatile chrome before comparing: elapsed timers, token
counters, spinner glyphs, and the agent status-line metrics that drift constantly (cost,
context %, prompt/tool counts, memory). Those values still reach the UI — the classifier
emits them as structured fields — but they don't count toward "is this a new screen?".

Worth stating plainly, because it surprised us during the audit: **tmux's own status bar
is not captured at all.** `capture-pane` returns pane content only, not the multiplexer
chrome, so tmux's clock never entered the picture. The volatile churn we strip is the
*agent's* in-pane footer, which is pane content.

## The case that still over-parses: tailing logs

The live measurement surfaced the one workload the content check can't help: a pane
**tailing a log** (or otherwise emitting a steady stream of genuinely new lines — an
active `uvicorn` access log, `tail -f`, a progress spew). Every tick the content really
is different, so every tick is a legitimate change, so every tick parses. This isn't
duplicate waste — each call sees new bytes — but it's still a poor trade: we don't need
an LLM re-read multiple times a second to know a pane is "tailing a log, nothing
actionable."

We are **not** fixing this now, on purpose — a good throttle needs its own design, and
conflating it with the heartbeat removal would muddy a clean deletion. Sketching the
space for the followup:

- **Rate-cap per pane.** A minimum interval between parses for a pane classified as
  high-churn/low-signal, so a log tail parses at most every N seconds even as content
  streams. Simple; risks lagging a pane that transitions from "spewing logs" to
  "waiting for input" right after a parse.
- **Change *magnitude*, not just change presence.** Parse when the diff is large or
  structurally interesting, coast when it's just another appended line of the same
  shape. Better targeted; needs a cheap local signal for "interesting", which is the
  hard part.
- **Classifier-driven backoff.** Let the classification itself carry a hint ("this is a
  log tail, back off") that widens the interval until the shape changes. Uses the signal
  we already pay for, but couples cadence to model output.

The rate-cap is the likely MVP; the other two are refinements. Tracked as issue #46
(followup to #44). Until then, a log-tailing pane parses on every change — correct, if
not cheap, and bounded by how fast the pane actually changes.
