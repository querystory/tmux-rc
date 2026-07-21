---
title: "Control-mode watcher (evaluation)"
---

# Design: a control-mode watcher — should observation move from polling to `tmux -C`?

Status: **draft / evaluation** — no code yet. This note weighs replacing the watcher's
poll loop with a persistent tmux **control-mode** client (`tmux -C`), which would let the
daemon *learn* of terminal changes by being pushed events instead of asking on a timer.
The goal is a decision — invest now, later, or not at all — not a spec.

## The problem today

tmux-rc observes by **polling**, on two independent cadences (see
[architecture](architecture.md#the-observation-loop) and [parse cadence](parse-cadence.md)):

- **The watcher tick (~1.5s).** Every `POLL_SECONDS` the loop `capture-pane`s every pane,
  fingerprints it (stripping volatile timer/spinner churn), and — only when the content
  changed versus the last parse — sends the screen to the LLM classifier. Between ticks
  it also `display-message`s for the focused pane id (`_check_active_fast`) and diffs
  `list-panes` to notice creation and destruction.
- **The live frame loop (0.25s).** `/api/panes/{id}/live` holds a request open and
  `capture-pane`s the watched pane every `LIVE_CHECK_SECONDS`, returning the moment the
  colored frame's hash changes. This never touches the LLM; it drives the raw terminal a
  phone is looking at.

Everything is a **one-shot subprocess**: `_run(["tmux", …])` spawns a fresh `tmux` client
per `capture-pane`, per `list-panes`, per `display-message`, per `send-keys`. Nothing
holds a connection. That statelessness is a genuine virtue — it is exactly why a human can
stay attached to the same session while the daemon watches, and why a wedged tmux surfaces
as a bounded `subprocess` timeout rather than a hung daemon.

But polling has two costs baked in:

- **A latency floor on everything.** The fastest tmux-rc can *notice* anything is one poll
  interval. Focus changes are caught within the 0.25s live cadence at best (or the ~1.5s
  tick for the card's `tmux_active` flag); content changes on an unwatched pane wait up to
  a full ~1.5s tick before they are even captured, let alone classified. The 0.25s live
  loop feels instant to a human eye, but it *is* a floor, and it is paid by re-capturing on
  a timer whether or not anything moved.
- **Change is inferred, never announced.** The daemon never *knows* something happened; it
  re-reads and compares. The fingerprint and the frame hash exist precisely because we
  captured redundantly and had to decide after the fact whether the capture was worth
  acting on. A quiet pane is captured just as often as a busy one and discarded.

Neither cost is *hurting* today — 0.25s live and a 1.5s classify cadence both feel prompt,
and the parse-cadence work already made the expensive half (LLM calls) fire on change
rather than on the clock. The question this note answers is whether an event-driven
observation layer is worth its complexity given that the polling approach already feels
instant.

## What control mode is

`tmux -C attach` (control mode) opens a **persistent client** that speaks a simple,
line-based text protocol on stdout instead of drawing a terminal. Crucially, it does not
just answer commands — it **pushes asynchronous notifications** the moment things happen in
the server. The ones that matter here:

- `%output <pane-id> <data>` — the live byte stream written to *every* pane, as it is
  written. This is the content firehose: everything a `capture-pane` would ever see,
  delivered as it happens rather than sampled.
- `%window-pane-changed`, `%session-changed`, `%session-window-changed` — focus moved.
  This is what `active_pane_id()` / `_check_active_fast` poll for, pushed instead.
- `%window-add`, `%unlinked-window-close`, `%window-close`, `%layout-change`,
  `%session-changed` — windows and panes created, destroyed, and rearranged. This is what
  `list-panes` diffing reconstructs.
- `%begin <ts> <n> <flags>` … `%end` / `%error` — command/response framing. Commands are
  still issued (write a line to stdin), and their output arrives between a `%begin`/`%end`
  pair tagged with a request number, so responses can be matched to commands even while
  async `%output` lines interleave.

One persistent attached connection, fed by the server, replaces N one-shot subprocesses
**and** the two poll loops. The daemon would stop asking "what does this pane look like
now?" on a timer and start being told "this pane just changed, here's what arrived."

## What it would change in the daemon

Three new pieces, one rethink, and a careful list of what deliberately stays the same.

**A persistent-connection subsystem.** Today there is no long-lived tmux process; the
daemon is stateless between subprocess calls. Control mode introduces a supervised child
process the daemon must spawn (`tmux -C attach -t …`), read continuously, and — this is the
load-bearing part — **treat as mortal**. The tmux server can restart, the client can be
killed, the session can be destroyed; the subsystem has to detect the death, tear down
cleanly, and reconnect with backoff, rebuilding pane state from a fresh `list-panes` on
reattach. This is a small always-on state machine where today there is none.

**A streaming protocol parser.** Control-mode output is a byte stream that has to be split
into lines, demultiplexed into async notifications versus command responses, and
un-escaped (`%output` data is octal-escaped for non-printables). The `%begin`/`%end`
framing must be tracked to route a command's reply to its caller. And the protocol has
**version quirks** — notification sets and escaping have shifted across tmux releases
(3.x), so the parser needs to tolerate lines it does not recognize rather than choke. This
is the most fragile new surface: a hand-rolled parser for someone else's evolving wire
format.

**A rethink of the watcher.** With events arriving, the tick loop as it exists dissolves:

- `%output` can replace `capture-pane` polling for **both** consumers. For the live frame
  stream, a pane's `%output` *is* the change signal — no 0.25s re-capture, no hash compare;
  a frame goes out when bytes arrive. For the classify trigger, `%output` on a pane marks
  it dirty, and the existing fingerprint/parse-on-change logic runs against a
  freshly-assembled screen. (See the throttling caveat below — `%output` is far more
  granular than a 1.5s capture, so this is not a free swap.)
- Focus notifications replace `_check_active_fast` and the per-tick `active_pane_id()`
  entirely — `tmux_active` becomes push-updated.
- `%window-add` / `%window-close` / pane-level lifecycle notifications replace `list-panes`
  diffing and much of the pid-based recycled-id detection: tmux tells us directly when a
  pane is born or dies, rather than us inferring it from set differences between ticks.

**What stays exactly the same.** Control mode changes only how the daemon *learns* of
changes — not what it does with them, and not how it talks to the phone:

- **Input injection is unchanged.** `send-keys` stays a one-shot subprocess (or could ride
  the control connection's stdin; no reason to move it). The
  [agentic control plane](agentic-control-plane.md) primitives are untouched.
- **The LLM classify itself is unchanged.** Same prompt, same fingerprint, same
  parse-on-content-change discipline. Control mode changes the *trigger's plumbing*, not
  the classifier.
- **The phone-facing side is unchanged.** `state_version`/long-poll toward the phone, the
  `/api/state` shape, the live long-poll transport over the tunnel — all of it stays.
  Control mode is upstream of the daemon's own state; the daemon still *serves* that state
  to the phone exactly as now. This is the same boundary the state long-poll already draws:
  how we learn is independent of how we tell.

## Tradeoffs, honestly

**Benefits.**

- **Event-driven, ~0 detection latency.** Focus and structure changes land immediately
  instead of within a poll interval. Content changes are known the instant tmux writes
  them, not on the next capture.
- **No polling, no redundant capture.** A quiet pane produces no `%output` and costs
  nothing; today it is captured every 0.25s (if watched) or every 1.5s regardless. The
  fingerprint and frame-hash machinery exist only to discard redundant captures — with a
  push stream, much of that reason to exist goes away.
- **One unified change stream.** Focus, content, lifecycle, and layout all arrive on one
  connection in order, replacing three separate poll mechanisms and their reconciliation.
- **Less subprocess churn.** One persistent client instead of a fork per capture per pane
  per tick.

**Costs.**

- **A stateful long-lived connection in a stateless daemon.** This is the big one. The
  current design's robustness comes from having nothing to keep alive; every failure is a
  bounded per-call timeout. A persistent client is a new class of bug — half-dead
  connections, missed reconnects, state that silently diverges from tmux after a dropped
  notification. `is_stale()` today catches a wedged loop; a control-mode daemon needs an
  equivalent liveness check on the connection and a trustworthy resync path.
- **Protocol-parser complexity and cross-version fragility.** We would own a parser for
  tmux's control protocol, including escaping and `%begin`/`%end` framing, and it must
  survive tmux version differences. `capture-pane` output, by contrast, is stable, obvious,
  and already handled. This is real surface area that tracks someone else's format.
- **Reconnect/lifecycle handling.** Server restart, session teardown, client death — each
  needs detection and a clean rebuild. None of this exists today because nothing persists.
- **One control client per session, coexisting with the human's client.** Control mode
  attaches to a session; watching all sessions means one control client each, and each is a
  real attached client sharing the session with the user's own attachment. This must not
  perturb what the human sees (size negotiation, `aggressive-resize`, hooks) — the
  "a human can stay attached at the same time" invariant is sacred, and a misbehaving
  control client threatens it in a way a passive `capture-pane` never could.
- **The `%output` firehose needs its own throttle.** A busy pane (a log tail, a progress
  spew) emits `%output` continuously — the exact workload
  [parse-cadence](parse-cadence.md#the-case-that-still-over-parses-tailing-logs) already
  flags as the case the content check *can't* help. Polling incidentally rate-limits this:
  a 1.5s tick coalesces a second of frantic output into one capture. Push delivery removes
  that free coalescing, so we would have to re-introduce debouncing/coalescing on the
  daemon side (assemble a screen, wait a beat, then fingerprint) — recreating a cadence we
  just deleted, now by hand. The live stream needs the same treatment or it floods the
  tunnel. This blunts the "no polling" benefit meaningfully.

**Rough size.** The poll code control mode would *replace* is modest: `_check_active_fast`
/ `active_pane_id`, the `list-panes` diffing and recycled-id logic, and the `capture-pane`
cadence in both the watcher tick and the live loop — on the order of a hundred-odd lines,
most of which is well-worn and low-bug. What it would *add* — persistent-process
supervision, reconnect state machine, a streaming control-protocol parser with escaping and
`%begin`/`%end` framing and version tolerance, `%output` reassembly into screens, and
per-pane debouncing to replace the coalescing polling gave us free — is plausibly **2–3×**
that, and concentrated in exactly the fragile, stateful, must-handle-every-edge category
(long-lived connections, someone else's evolving wire format) rather than the
straightforward category it removes. The migration is net *more* code, and more dangerous
code, for a latency win the current approach already makes hard to perceive.

## Relation to "chat with your tmux"

The [agentic control plane](agentic-control-plane.md) — state an intent, have tmux-rc plan
and execute the concrete keystroke sequence — leans on two things: the **LLM parse** for
grounding, and **verification between steps** ("did the Rewind picker actually open before
I arrow into it?"). Does an event stream help?

Mostly it is **orthogonal**, the same way the phone-facing state long-poll is orthogonal:
the control plane needs to know *the current parsed state of a pane* to plan and verify
against, and it gets that from the watcher's classification regardless of whether the
watcher learned of the change by poll or by push. A faster, event-driven watcher makes the
grounding *fresher* — the between-steps re-parse ("confirm the picker opened") could react
to the `%output` that drew the picker within milliseconds instead of waiting for the next
capture — which tightens the plan→verify→next-step loop and makes multi-step sequences feel
snappier and less racy. That is a real but **incremental** benefit, not an enabling one:
the control plane is fully buildable on today's poll loop, and its hard problems
(grounding, fuzzy targeting, destructive-intent confirmation, multi-pane routing) are LLM
and UX problems that an event stream does not touch. Control mode would be a nice-to-have
accelerant for it, never a prerequisite.

## Recommendation

**Not now.** The honest verdict is that this is a large, stateful, fragility-adding
migration in pursuit of latency the current design already makes imperceptible. The live
loop at 0.25s reads as instant to a human eye; the classify cadence at ~1.5s is gated on
LLM cost, not capture speed, and parse-cadence already fixed the expensive half by firing
on change. The single biggest benefit of control mode — no redundant capture of quiet panes
— is partly clawed back by having to hand-build the `%output` debouncing that polling gave
us for free, and the single biggest cost — trading a robustly stateless observer for a
persistent, version-fragile, reconnect-managing connection that shares the user's session —
lands squarely against the project's founding invariant that *observation stays passive and
a human can stay attached alongside it.*

If a future need makes it worthwhile, the phased path is clear and lets us buy the safe
benefits first:

1. **Structure and focus events only.** Add a control-mode client used *solely* for
   `%window-add`/`%window-close`/focus notifications, replacing `list-panes` diffing and
   `_check_active_fast`. Keep `capture-poll` for all content. This is the low-churn,
   high-value slice: it kills the two cheapest poll mechanisms, needs only the simplest
   notification parsing (no `%output` reassembly, no debouncing), and if the connection
   dies we fall back to a `list-panes` resync with no content-fidelity risk.
2. **Content via `%output`, with debouncing.** Only if phase 1 proves the connection
   subsystem reliable, move content off `capture-pane` — first for the live stream (where
   the latency win is most visible), then for the classify trigger — building the coalescing
   throttle as a first-class piece.
3. **All-in** (retire `capture-pane` polling entirely) only if phases 1–2 show the
   persistent connection is as trustworthy in practice as the stateless subprocess model it
   replaces.

The trigger to revisit: a concrete latency complaint the poll floor actually causes (not a
hypothetical one), or the [agentic control plane](agentic-control-plane.md)'s step
verification proving too laggy on capture cadence in real use. Absent that, the complexity
is not justified — the polling approach already feels instant, and "observe the terminal,
don't drive the agent" is better served by the observer that cannot perturb the session.
