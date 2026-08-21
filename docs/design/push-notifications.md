# Design: push notifications — "needs you" and "milestone" pushes, with reply

Status: **draft / thinking** — no code yet. Captures the design for issue #139.

## The problem

Every pane's output already flows through an LLM that decides what matters. But the
result only reaches you when you're *looking at the app*. An agent hits a permission
prompt at minute 2 of a 40-minute session and sits blocked for 38 minutes; the tests
you kicked off finish and nobody tells you. The phone should tap you on the shoulder —
when your attention is genuinely needed, or when something you care about completed —
and let you answer right from the notification.

The failure mode to design against is the opposite one: a push for every parse. A
notification channel that cries wolf gets muted within a day and the feature is dead.
Almost all of this design is about *not* sending notifications.

## What already exists (most of the intelligence)

The importance judgment is already in the pipeline; it just isn't routed anywhere:

- `parser_prompt.txt` instructs headlines to be written "like a mobile push
  notification the user will REACT to" — the copy already exists, per parse.
- `waiting_on: "user" | "external"` (#65) is precisely the "needs you" bit: an input
  affordance the human must fill, vs. background work the agent spawned.
- `question: {prompt, options}` is a ready-made notification body *plus* the reply
  affordance — options become action buttons.
- `POST /api/panes/{id}/send` is the reply path: audited, and it already forces a
  re-parse so the answered question clears promptly.
- The watcher already detects the relevant *transitions* (`_deck_fp`,
  `_question_prompt`) to wake the long-poll — the same change-detection a notifier
  needs.

So this is a **routing + transport** problem. No second classifier, no notification
LLM pass.

## Two kinds of notification, two different mechanisms

### Blocking: derived, not asked for

An agent blocked on the user is already fully described by existing fields:
`activity == "waiting"` and `waiting_on == "user"`. The daemon derives the
notification server-side — **zero prompt change, zero new schema** — with title from
the pane label, body from `question.prompt` (falling back to `headline`), actions
from `question.options`.

Not every user-wait carries a `question`: `classify()` also promotes a detected
`rewind` picker to `waiting_on: "user"` with no question object. Those still notify —
they're real user-waits — but body falls back to the headline and there are **no
action buttons**; a tap deep-links into the card. Actions exist only when there's a
structured question to answer.

*Alternative considered and rejected:* a dedicated `notify: {urgency, title, body}`
object emitted by the parser (as sketched in #139). Rejected because it duplicates
copy the model already writes (headline, question, events) — two phrasings of the
same thing that can disagree — and because for the blocking case the *decision* is
already structural. Asking the model to re-decide it just adds a place to be wrong.

*Also rejected:* a numeric `importance: 0–10` + threshold. LLM scores are poorly
calibrated, drift across prompt edits, and there's no ground truth to tune a
threshold against. A score also can't express *why* — buckets defined by example can.

### Milestone: one boolean on events

"Major task done" genuinely needs fuzzy judgment — tests passing after a long red
streak matters; the 14th file edit doesn't. The one schema addition is an optional
flag on the existing event object: `"milestone": true` on an event worth interrupting
a person for (a build/test run finishing with a verdict, a PR merged, a deploy
completing, an error the agent can't recover from). The prompt defines the bucket by
example and — like `copyables` — says explicitly that *omitting it is the correct
answer far more often than not*.

This rides the existing event machinery for free: events are already deduplicated at
the source (the model sees what it already reported), already written as standalone
notification-feed lines, and already timestamped in the burst ring. The notification
body *is* the event text — single-sourced, no drift.

The dedup interaction is deliberate: the notifier consumes events **at append time**,
so the flag only matters on the parse that first reports the event. If the model
re-emits the same text later *with* the flag, the text-keyed duplicate guard drops it
before the notifier sees it — there is no "upgrade to milestone" path. Marking
happens when the event is written or not at all, which is also the only behavior the
prompt asks for.

## Cadence: the actual hard problem

The watcher re-parses on every real content change. Suppression lives server-side in
a small notifier that observes state transitions — never in the LLM, and never in the
service worker (by the time a push arrives, the decision must already be made).

**1. Someone watching is already notified.** If any client has the app *foregrounded*,
the app is the notification surface (the dock badges the waiting pane). Two presence
inputs:

- A **per-client presence lease**, refreshed by `/api/state` polls that carry a
  visibility flag and by a `sendBeacon` fired on `visibilitychange`. Per-client
  (keyed by a client id, one TTL entry each — suppress while *any* visible lease is
  live), because a hidden tab's hide-beacon must not clear a different device's
  visible lease. And a lease, not a latch, because the raw poll signal goes stale in
  both directions: the poll loop never pauses when hidden (a hidden desktop tab
  keeps polling), and one long-poll can be in flight — or the mobile OS can suspend
  the page outright — right as the user backgrounds the app, leaving the server
  believing "visible" for the rest of the hold. The TTL must exceed the ~25s
  long-poll hold with margin (~40s): a foregrounded client's next visible signal can
  legitimately be a full hold away, and a shorter TTL would read ordinary
  foreground use as absence and push at a watching user. The hide-beacon delivers
  the background transition promptly, so the longer TTL costs nothing in the common
  case; it only bounds how long a *missed* beacon (suspended page) can keep
  suppressing. Expiry fails in the safe direction: no fresh visible signal ⇒ notify.
- tmux itself knows when the user is at the desk: `client_activity` from
  `list-clients`. Keystrokes in any attached tmux client within the last ~30s mean
  the user is *at the terminal*, likely mid-answer — pushing to their phone then is
  noise.

Suppression here is correct even cross-device: if you're watching on the desktop, the
phone staying silent is the right behavior.

**2. Once per question, not once per tick.** A blocked pane stays blocked across
dozens of parses. The notifier keys on a fingerprint of **(pane incarnation, full
answer contract)** — the pane's birth pid (the watcher already tracks it, because
tmux recycles `%N` ids) plus a hash of `question.prompt`, `options`, and
`answer_style` (or the headline, for questionless waits like rewind). The full
contract matters for the reply path below: options can reorder or the style can flip
while the prompt text stays identical, and an action minted against the old contract
must not validate against the new one. The notifier pushes **at most once per
fingerprint**. This is deliberately
*level-triggered with a notified-set*, not edge-triggered: a question that appears
while you're watching (suppressed by rule 1) and is still unanswered when you
background the app *should* then fire. Pure edge-triggering misses that case; the
notified-set makes "fire once, whenever conditions first allow" fall out naturally.
Fingerprints are forgotten when the question clears, so a *re*-blocked pane can
notify again.

**3. Milestones coalesce; blocking never waits.** A blocking push goes out
immediately (modulo a short settle delay, below). Milestone events buffer ~60s and
merge: three panes finishing inside a minute is one "3 tasks finished" push, not
three buzzes. Same shape as `_maybe_summarize`'s burst collapsing.

**4. Settle before pushing.** Agent screens flap — a menu flashes, redraws, and the
next parse sees it differently (#64 lists the same traps for Live Mode's completion
detection). A blocking push waits ~5s of the fingerprint holding stable before
sending. This also absorbs the answered-instantly case: user was at the terminal,
answered within seconds, no push.

**5. Rate cap as runaway insurance.** At most one milestone push per pane per
5 minutes — and blocking gets its own emergency ceiling (first push immediate,
then at most ~3 per pane per 15 minutes). Blocking can't be exempt from all caps:
a prompt regression that rephrases a persistent question on every parse mints a
fresh fingerprint each time, and the notified-set would wave every one through. No
single pane may be able to buzz a pocket continuously, whatever the model does.

## Transport: Web Push, sent by the daemon

The app is already a proper installable PWA behind IAP (`manifest` fetched with
`use-credentials`, apple metas) — the precondition for iOS Web Push (16.4+, installed
to home screen) is cleared. The daemon sends via `pywebpush` with a VAPID keypair.
No third-party service holds content beyond the platform push relays themselves
(payloads are encrypted end-to-end to the browser; FCM/APNs see ciphertext).

*Alternative considered:* ntfy / Pushover — one plain HTTP POST from the daemon, no
service worker at all. Simpler, but: content transits a third party in cleartext
(pane text includes code and secrets-adjacent material), replies land in *their* app
instead of ours, and there's no deep link back to the pane card. Kept as the
**fallback transport** — the notifier's decision layer is transport-agnostic, and if
iOS Web Push proves too fragile (see Risks), swapping the sender is a small, isolated
change.

### The service-worker wrinkle

`web/sw.js` is currently a **self-destructing tombstone** and `app.js` unregisters
all service workers at boot — deliberately, because an old *caching* SW once served
stale `app.js` and hid new UI. Push requires a live SW, so the tombstone gets
replaced by a real one that handles `push` and `notificationclick` and registers
**no fetch handler at all**. No fetch handler ⇒ every asset request still goes
straight to the network ⇒ the property the tombstone protected (never serve stale
assets) is preserved by construction. The boot-time unregister purge is replaced by
an explicit `navigator.serviceWorker.register()` — no registration call exists in
the app today (nothing registers a SW implicitly), and the returned registration is
also where `pushManager.subscribe()` hangs off. The SW file itself is served
no-store like everything else, so browser update checks always see the current
version. This constraint gets a loud comment in `sw.js`, or someone will
"helpfully" add caching back.

### Subscriptions and keys

Push subscriptions must survive a daemon restart — push exists precisely for when no
client is open to re-register, so "re-POST on next app load" can't be the only copy.
This is a narrow, justified exception to the no-persistence rule (see the
activity-log design): a subscription is a **client credential**, config-like (peer of
`.env`), not observed pane state — losing it doesn't thin out history, it severs the
channel. One small JSON file next to `.env` holds the VAPID keypair (generated on
first use; it must stay stable, since subscriptions bind to the public key), the
`sub` contact claim VAPID authentication requires (a `mailto:` — default it from the
tunnel owner or a `.env` value), and the subscription list. Clients still re-POST their subscription at boot as self-healing;
the store prunes endpoints on `410 Gone`.

`POST /api/push/subscribe` is guarded and audited like `/send`. It's single-user,
multi-device: every stored subscription gets every push (per-device mute can come
later if it ever matters).

### Egress

Push endpoints (`fcm.googleapis.com`, `web.push.apple.com`) would hit the same
broken-IPv6 serial-connect hang the Gemini websocket did — but the daemon's
`socket.getaddrinfo` IPv4-first sort is process-wide, so `pywebpush`/`requests`
inherit it for free. That sort doesn't bound a request that connects and then
stalls, though: the sender is a synchronous `requests` call, so it runs **off the
watcher and request hot paths** (its own worker thread, fed by a queue) with
explicit connect/read timeouts. An FCM/APNs outage must degrade to dropped or late
pushes — never to stalled parsing or presence updates (blocked-event-loop slowness
is already a known failure class in this daemon). Blocking pushes go with `Urgency: high` and a short TTL
(~10 min — a stale question has probably been answered from another surface);
milestones with normal urgency and ~1h TTL. Tags are **namespaced by kind**:
`block:<pane_id>` (a newer question on the same pane replaces the older one) and a
single rolling `milestones` tag (a coalesced push spans panes, so it has no single
pane to tag — and it must never be able to replace an unanswered, action-bearing
blocking notification).

## Replying from the notification

- **Options** (`question.options` present): the first two **renderable** options
  become action buttons (Web Push caps actions at two on most platforms; tapping the
  body opens the card for the rest). "Renderable" means the same pseudo-option
  filtering the card already applies ("Other", "type something…") — the push must
  never offer an action the card wouldn't, and indices are minted against that same
  filtered list, which is also what the answer-contract fingerprint hashes, so
  filtering can't skew index→keystroke mapping. **iOS does not render action buttons
  at all** — there, every tap deep-links into the app. Accept the asymmetry rather
  than fighting it.
- **Free text**: deep link with the composer focused — on every platform. (The Web
  Notifications API has no inline text-input field; Android's RemoteInput is a
  native-notification feature that `showNotification` cannot express, so there is
  no web-push inline reply to offer.)
- **Deep link**: `/?pane=<id>` handled at boot in `app.js` (selects that pane's
  card). Small, and independently useful.

An action tap is **send-keys from the lock screen**, so the reply path is narrower
than `/send`: the SW posts `{nonce, option_index}` to a dedicated
`/api/push/answer`. Three server-side gates before anything is typed:

- **The nonce is one-shot.** Each action-bearing push carries a random nonce the
  daemon minted and remembers; answering consumes it atomically. Fingerprint
  validation alone can't prevent a double-tap or replayed push from injecting the
  option twice — both taps can arrive before the forced re-parse clears the
  question. (The card UI has the same hazard and disables its option buttons once
  an answer is in flight; the nonce is the server-side equivalent.)
- **The fingerprint must still match.** The nonce binds to the full answer-contract
  fingerprint above; if the pane's current question has changed in prompt, options,
  order, or style — or the pane id was recycled to a new pane — the tap is rejected
  rather than typed into whatever is there now.
- **The daemon owns the option→keystroke mapping.** `answer_style` matters:
  `"menu"` options are on-screen widgets answered with a keystroke (digit, y/n),
  not their label text — typing the label into a numbered menu selects nothing or
  the wrong thing. The card does this mapping in `app.js` today; `/api/push/answer`
  mirrors those exact semantics server-side (a later cleanup can point the card at
  the same endpoint so the mapping has one home). The SW never sends strings at
  all — only the nonce and an index.

The endpoint audits with a distinguishable actor (`push-action`) through the same
`_audit` path as `/send`.

## Build order

1. **Signal only.** Add the `milestone` flag to the prompt; log every would-be
   notification decision (blocking derivations included) into the digest/telemetry.
   Send nothing. Live with it for a day or two and read the log against what you'd
   actually have wanted buzzing your pocket — this is the only honest way to
   calibrate the fuzzy bucket, and it's free. The prompt change ships with eval
   cases: milestone-positive and milestone-negative samples in the existing
   prompt-eval corpus (`research/eval/samples/`), and a full eval run green — that's
   a requirement of this step, not a someday-before-step-4.
2. **Blocking pushes.** Real SW, subscribe endpoint, VAPID sender, presence +
   fingerprint + settle suppression. If only this ships, most of the value is
   captured: "an agent is waiting on *you*" is the high-signal, low-noise case.
3. **Replies.** Action buttons → `/api/push/answer`; deep link.
4. **Milestones with coalescing.** Last, because it's where the noise lives — tune
   it with step 1's data.

## Risks

- **iOS Web Push is the fragile leg.** Permission is only grantable inside the
  installed PWA from a user gesture; the subscription silently dies if the user
  removes/reinstalls the app icon; SW registration behind IAP can fail in obscure
  ways. Time-box it: if step 2 fights for more than a day, switch the sender to the
  ntfy/Pushover fallback and revisit. Steps 1's decision layer carries over
  unchanged either way.
- **Calibration is the whole game.** Step 1 exists so the milestone bucket is tuned
  on real data, not vibes. Don't skip it.
- **Prompt regressions become pocket buzzes.** A parser-prompt edit that inflates
  `waiting_on: "user"` or milestone flags now has a physical blast radius. The rate
  caps (including the blocking ceiling) bound the damage; the eval cases required in
  step 1 catch it before it ships.

Related: #139 (the ask), #65 (`waiting_on` — the "needs you" signal), #64 (Live
Mode's completion notify — same settle/never-completes traps, different consumer),
#46 (high-churn panes — the same panes that would spam milestones).
