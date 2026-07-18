# Design: the live view — watching a pane in real time, in color

Status: **implemented (v1)** — this documents the shipped design and the paths not taken.

## The want

When a raw pane is on screen — the peek behind the card, and the full-screen ⤢ view —
it should behave like a terminal you're looking at, not a screenshot you re-request:
updates land within a few hundred ms of the pane changing, and the text carries the
terminal's **colors**. Color isn't decoration: TUIs use it to separate drafts from
output, errors from noise, spinners from content (the same reason the parser needed
dim-awareness). Before this, the peek re-rendered on the watcher's snapshot cadence, the
full-screen view was a frozen one-shot, and all color was stripped server-side.

## Constraints that shaped it

1. **The tunnel does not stream.** The relay↔tunnel-client protocol serializes whole
   HTTP request/response pairs and bounds any held request under ~60s; there is no
   pass-through for a phone-originated WebSocket or SSE today. True end-to-end streaming
   is a future tunnel-protocol change (bundled with the tunnel reliability work).
2. **The watcher's cadence is for the LLM, not eyes.** Live freshness must not couple to
   parse ticks and must never add an LLM call. The live path is raw `capture-pane` only.
3. **No vendored terminal emulator.** A raw pty stream (tmux `pipe-pane`) would need an
   xterm.js-class client to interpret cursor-movement — against the no-framework rule,
   and unnecessary: tmux already composes the screen; `capture-pane -e` hands us finished
   frames with colors intact. tmux stays the only emulator in the system.
4. **Phone economics.** Cellular data / battery care about bytes and radio wake-ups; an
   idle pane must cost ~nothing.

## The design: long-poll frames, decoupled from the tick

`GET /api/panes/{id}/live?frame=<hash>`:

- The daemon **holds the request**, re-capturing every 250ms (a worker thread; never the
  LLM) and hashing the frame. First mismatch against the client's declared hash →
  respond immediately with the full colored frame. Nothing changed by ~25s → a tiny
  `{frame}` "you're current" reply; the client re-holds at once. Worst-case staleness is
  the 250ms check interval; a busy pane streams responses back-to-back; an idle pane
  costs one request per ~25s.
- `frame` is an **ETag-style content hash** (full MD5 — a truncated one could alias a
  changed frame onto the client's and silently stall the stream). It is how we answer
  "does the client already have this screen?" without server-side per-client state and
  the "no change" reply — nothing to do with deltas.
- **Full colored frames, always.** v1 deliberately does not diff on the wire: a resize,
  reflow, or alt-screen flip reflows every line, so any delta scheme degenerates to a
  full frame exactly when the screen is most confusing. Full frames make those a
  non-event by construction. Frames are ~13KB raw but gzip to ~2.8KB (GZipMiddleware),
  so the bandwidth argument for deltas is weak at this size.
- **Spinners spin.** The change hash is over the raw frame, so every visible change —
  including a spinner tick or ticking timer — is a new frame. Live means live.

### Client (web/terminal.js — the one capture→HTML path)

- `renderCapture(text, {color})`: SGR → styled spans (256-color + faint/bold/italic/
  underline) and link anchoring (OSC-8 markdown, bare, and cross-line wrapped URLs) in a
  single module. This replaced a bolted-on `linkifyCapture` that had started to drift
  from the color path; app.js is now a native ES module importing it.
- A shared `liveStream()` drives both surfaces: the long-poll loop, an `AbortController`
  to stop on close / pane-switch, and the stale look. Late frames that resolve after a
  pane switch are ignored (the callback captures its pane id).
- **Color = live, gray = stale.** A colored frame is fresh; when the *connection* goes
  unhealthy the surface desaturates (a CSS grayscale filter, so `<a>` tags and text
  survive; the next good response re-colors). Crucially, stale is about the CONNECTION,
  not screen activity — an idle pane produces no new frames for long stretches yet is
  perfectly live, so any server response (a fresh frame OR the ~25s "no change" reply,
  which is proof-of-life) counts as live; gray only on a fetch error or a >one-hold
  watchdog with no response at all.
- **Instant stale-on-mount.** Switching to a pane paints its last-known frame gray
  immediately from a per-pane cache, and the stream colors over it — no blank flash. On
  a same-pane re-render (the deck rebuilds every poll) the mount reflects the stream's
  real liveness rather than blanket-graying, which is what removed both the idle-gray and
  the per-poll repaint flicker (they were the same bug).

## Rejected alternatives

- **Flat client polling at 500ms.** Simplest, but 2 req/s per viewer through the tunnel
  forever, the radio never idles, and latency is no better than the long-poll's 250ms
  server-side checks.
- **SSE / WebSocket now.** Same tunnel-streaming blocker; deferred to the tunnel-protocol
  work, where the long-poll stays as the fallback transport.
- **tmux `pipe-pane` raw stream.** True terminal deltas, but cursor-movement soup needing
  a client-side emulator (constraint 3); also one-per-pane and it mutates pane config,
  crossing the observer line.
- **On-the-wire line deltas.** The client already sends its frame hash, so the anchor for
  a delta is there — but the edge cases (resize/reflow/alt-screen) all live on the delta
  side, and at 2.8KB gzipped the payoff is marginal. Specced as a post-MVP option, not
  shipped.
- **Volatile-strip the change hash** (ignore spinner/timer churn, as the watcher does for
  its parse trigger). Tried — it froze the spinner in live mode, which is the opposite of
  the point. Reverted: live mode shows the animation; flicker was a repaint-method
  problem, fixed by reflecting real liveness on remount rather than by dropping frames.

## Post-MVP

- **WebSocket transport** + optional **line deltas**, bundled with the qsi-automation
  tunnel-protocol change (the tunnel can't stream today).
- **Telemetry** (bandwidth, live-time per user, a per-pane "has-live-viewer" presence
  signal to let the daemon throttle LLM cadence when nobody's watching) — see
  `docs/design/live-telemetry.md`.

## Open questions

- Peek vs. full-screen: both stream today (one at a time). If two-viewer or
  battery pressure ever bites, share one 250ms capture loop per pane across waiters.
- The 250ms floor is a server constant, not a client knob — clients shouldn't bid the
  capture rate up.
