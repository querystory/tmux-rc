---
title: "Live Mode routing: gates first, a controller behind a flag"
---

# Design: Live Mode routing

Status: **proposed**. Weighs where a voice request's send decision should be made, after
an afternoon of voice use in which six of fifteen relays reached a wrong or stale pane.
Builds on [Live Mode](live-mode.md) and [the control session](control-session-routing.md);
it links to them rather than restating them.

## What went wrong, by cause

Every wrong send that afternoon can be explained without a single recognition error, so
this is a routing problem, not an audio one. The causes fall into four kinds:

- **Fan-out.** A fleet-wide question ("what's the status of everything today?") was
  relayed to four panes, because the prompt says to ask the pane that would know. Each
  receiving agent then took "ask all the panes" literally and broadcast its own
  roll-call. One spoken sentence became hundreds of agent-to-agent messages, reaching
  panes idle for days. The answer was already in the pane list the model held.
- **A stale target.** With no name given, the model matched on topic and picked a pane
  idle for ten days whose history happened to be about the same subject. The prompt's
  ladder already says a long-idle pane needs confirming; at low reasoning effort the
  model skipped that rung.
- **A bare "yes" with nothing to answer.** "Go for it" was typed into a pane with no
  pending question. Text mode has a rule for this (a bare yes only answers a question this
  conversation asked); the voice prompt did not, and the correction that followed went to
  the same wrong pane.
- **The model's words under the user's label.** In GPT-Live, the voice front end hands
  off to a backend model that only sees the handoff. It wrote its own briefs and sent them
  as "From the user (via voice)", so the receiving agent could not tell instruction from
  paraphrase.

The backend was a small model at low effort. That makes "use a bigger model" the obvious
fix, and it is part of the answer, but three of the four causes are not intelligence
failures: they are missing rules and missing checks that a bigger model would also
sometimes skip.

## The options

### (a) Gates only

Keep the current path (voice front end, backend model, `type_in_pane`) and add checks
where a send leaves the daemon:

- one pane per user request; a second distinct target in the same turn is refused with a
  reason the model can say aloud;
- a send to a pane idle for more than about an hour goes through the approval card text
  mode already has, instead of typing straight in;
- the bare-"yes" rule moves into the shared prompt, so voice and text say the same thing;
- a fleet question is answered from the pane list, and no relay asks an agent to contact
  other panes;
- text labelled as the user's must be the user's: the daemon compares it with the input
  transcript, and anything else is labelled as the assistant's.

**Fit with the rule that the model owns meaning.** This is the reason (a) is safe to ship
first. Every check is plumbing or an evidence gate: counting targets per turn, reading a
pane's idle age, comparing a label against a transcript. None judges what the user meant.
The two that are about meaning (what a bare yes answers, when to relay a question) go in
the prompt, where the model owns them. The approval card is reused as is, so the only new
UI is a card appearing in voice for a stale pane.

**Latency:** none added on the common path; a stale-pane send waits for a tap, which is
the point. **Cost:** none. **Failure mode:** a gate refuses a send the user wanted; the
model says why and the user names the pane. That is recoverable; a misplaced instruction
in a live agent is not. **What it leaves open:** the backend can still pick the wrong
fresh pane, and it still cannot run a multi-step dispatch (open a window, start an agent,
hand off). Gates remove the worst sends; they do not make routing smart.

### (b) A stronger model for the send decision, inside the daemon

Before a send leaves, the daemon asks a stronger model (Opus through the provider seam
that text mode already uses for Claude) whether this target is right, with the fleet
directory and the request.

**Latency:** one extra model call per send, a few seconds with a cached prompt, which is
noticeable in a voice turn but tolerable for an action. **Cost:** small per send, since
the fleet prompt caches. **Failure mode:** the API is slow or down; the send falls back to
the approval card, which is a fine default.

The problem is the shape. The verifier sees one send, not the conversation: it does not
know the user corrected the target two turns ago, or that "it" means the PR just
discussed. So it is a second judge of meaning with less context than the first, and
when the two disagree there is no principled tiebreak except asking the user, which is
what (a)'s card already does. It also cannot repair the deeper limit, that the GPT-Live
backend is chosen from OpenAI's own models: Opus can review a send but cannot become the
model holding the conversation. The cheaper version of this idea is to raise the existing
backend's reasoning effort or pick a larger backend model, which the daemon already allows
through configuration. That is worth measuring on the replay suite before writing any
code.

### (c) A dedicated controller pane

Every voice request goes to one long-lived agent pane (a Claude Code session, opt-in per
user), which reads the directory and dispatches. This is the control session already
proposed in [control-session-routing](control-session-routing.md), with voice as its
front end. It reuses a pattern that already works by hand: on the bad afternoon, the two
sends the user addressed to their own coordinator pane were the two that landed right
first time, because that agent had the context to know where work lived.

**What it buys.** The best model available, with the whole conversation, the fleet
directory, and the ability to run a sequence (find, open, start, hand off) instead of a
single keystroke. Clarifying questions come from one place with the most context.

**Latency:** a full agent turn, tens of seconds when the controller is idle. In a voice
conversation that is long enough that the front end must say "handed to the controller"
and move on, then report the result when it lands. **Cost:** an agent turn per request,
far more than a single routing call, though on a subscription plan it is plan usage rather
than API spend.

**Failure mode, and the reason it is not the whole answer.** A controller is a single
point of failure that is often not idle. It can be mid-turn on the last request, waiting
on its own permission prompt, compacting, or crashed. A request typed into a busy agent
queues behind its current work with no deadline; one typed into a wedged agent goes
nowhere, and the voice user hears nothing. So the daemon has to watch the controller's
state (the watcher already knows when a pane is working, idle or asking) and refuse to
hand off to one that is not ready, falling back to the gated direct path or to asking.
The controller also sends to other panes, so (a)'s gates must apply to its sends too;
otherwise the fan-out failure simply moves one hop down.

**Fit with the rule.** Good: the controller is a model making the meaning call, and the
daemon only delivers, gates and watches. The return path (how the controller's answer gets
back to the voice) is the open problem: the controller-routing design expects
conversation threads for it, which do not exist yet.

### (d) Hybrid: voice answers, the controller acts

The voice path keeps what it is good at: answering fleet questions from the pane list,
and the one confident send ("tell the review pane to add a test") to a named or active
agent pane, gated as in (a). Anything multi-step, ambiguous, involving more than one pane,
or creating or resuming panes goes to the controller, with an immediate spoken
acknowledgement.

This is the controller-routing design's fast path made concrete, and it pays (c)'s
latency and cost only on the requests that need its intelligence. When the controller is
busy or wedged, the hybrid degrades to (a), which is no worse than today. The cost is two
paths to understand and test, which is why the replay suite has to cover both before it
ships.

### (e) A Claude chat loop behind the voice

A variant worth naming: instead of OpenAI's managed backend, the voice front end's only
tool is a handoff to the daemon's own text-mode loop, which can already run Claude with
the same prompt, tools and approval cards. This puts a strong model on the whole
conversation at a few seconds per step, without a separate pane. It would replace the
managed delegation that GPT-Live is built around, so it is a larger change than it looks,
and it still cannot run long multi-step work. It is the fallback if the controller's
latency proves too slow for voice.

## Tradeoffs at a glance

| | Latency added to a send | Cost | When it breaks | Model owns meaning? | Reuses |
|---|---|---|---|---|---|
| (a) Gates | none; a tap for stale panes | none | refuses a wanted send; user names the pane | yes, gates are evidence only | approval card, shared prompt |
| (b) Verifier | a few seconds | small | API down: card | splits meaning across two models | provider seam |
| (c) Controller | tens of seconds | an agent turn per request | busy or wedged controller: nothing happens | yes | coordinator pattern, control-session design |
| (d) Hybrid | none for answers; controller time for actions | agent turns for actions only | falls back to (a) | yes | all of the above |
| (e) Claude loop | a few seconds per step | small to moderate | API down: no backend | yes | text mode's loop and cards |

## Recommendation

Ship (a) now: it addresses all four causes, adds no latency, and fits the meaning rule
without exceptions. Measure it with the conversation-replay suite, built from invented
conversations with the same shapes as the bad afternoon.

Then, in order:

1. **Turn the existing knob.** Rerun the replay suite with the backend at higher effort
   and with a larger backend model. No code; it tells us how much of the remaining error
   is the model.
2. **The hybrid (d), behind a per-user setting, off by default.** The controller is the
   control session from the controller-routing design, with a narrow charter and the
   daemon's gates on its sends. The daemon hands off only to a controller that is idle,
   and says so aloud when it cannot.
3. **Everything through the controller (c)** only if the replay suite and real use show
   the gated direct path still sends to wrong panes after step 1.

(b) is parked: its value over (a)'s approval card is small, and it judges meaning with
less context than the model already holding the conversation. (e) is the alternative to
step 2 if a controller turn proves too slow for a voice conversation.

## Alternatives rejected

- **Code that decides targets** (keyword matching on titles, refusing sends by topic).
  It judges meaning with word matching, which is the model's job; text mode tried it
  and dropped it for that reason.
- **A mandatory controller for every user.** It makes the simple one-session setup pay
  for a second agent and its failure modes; casual users would get slower voice for
  nothing.
- **Dropping direct sends entirely.** The one confident send to the pane you are looking
  at is most of voice's value; routing it through a controller trades seconds of latency
  for no gain in correctness.

## Open questions

- How the controller's result reaches the voice session before conversation threads
  exist: a watcher event on the controller pane's next idle summary may be enough.
- Whether "idle for about an hour" is the right staleness line, or whether it should be
  the pane's last activity relative to the conversation's start.
- How the replay suite scores a hand-off to the controller, whose outcome is not a single
  send.
