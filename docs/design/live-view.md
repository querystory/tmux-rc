# Design: the live view — watching a pane in real time, in color

Status: **implemented (v1)** — this documents the shipped design and the paths not taken.

## The want

When a raw pane is on screen — the Terminal view —
it should behave like a terminal you're looking at, not a screenshot you re-request:
updates land within a few hundred ms of the pane changing, and the text carries the
terminal's **colors**. Color isn't decoration: TUIs use it to separate drafts from
output, errors from noise, spinners from content (the same reason the parser needed
dim-awareness). Before this, the terminal re-rendered on the watcher's snapshot cadence
and all color was stripped server-side.

## Constraints that shaped it

1. **The tunnel does not stream.** The relay↔tunnel-client protocol serializes whole
   HTTP request/response pairs and bounds any held request under ~60s; there is no
   pass-through for a phone-originated WebSocket or SSE today. True end-to-end streaming
   is a future tunnel-protocol change (bundled with the tunnel reliability work).
2. **The watcher's cadence is for the LLM, not eyes.** Live freshness must not couple to
   parse ticks and must never add an LLM call. The live path is raw `capture-pane` only.
3. **tmux is the only terminal emulator in the system.** We consume *composed frames*
   (`capture-pane -e` — finished screens with colors), never a raw pty byte stream, so
   nothing here has to interpret cursor movement. That's a frames-*source* decision; how
   we *render* those frames (hand-rolled SGR vs. a library like xterm.js) is a separate
   call weighed under [Rendering](#rendering-hand-rolled-sgr-vs-xtermjs).
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
  from the color path; the app is a native ES module importing it.
- One long-poll loop drives the terminal: an `AbortController`
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
  a same-pane re-render (the list re-renders every poll) the mount reflects the stream's
  real liveness rather than blanket-graying, which is what removed both the idle-gray and
  the per-poll repaint flicker (they were the same bug).

## Rendering: hand-rolled SGR vs. xterm.js

Given composed frames from `capture-pane`, we still choose how to turn them into pixels.
Two options, and this is a real tradeoff, not a foregone conclusion — `web/terminal.js`
is ~170 lines re-implementing a slice of what xterm.js does, so the choice deserves to
be earned.

**What we do (hand-rolled).** `terminal.js` parses the SGR runs `capture-pane -e` emits
— 16-color, 256-color, 24-bit, bold/faint/italic/underline — into styled `<span>`s, and
anchors links (OSC-8 markdown, bare, wrapped). It is the *one* capture→HTML path, shared
by every terminal view.

Why this, for now:
- **No build step, no framework, no vendored bundle.** The whole PWA is hand-served
  vanilla JS with a strict "no framework" rule; xterm.js is ~100–200KB min+gz and expects
  a bundler/module setup we don't have. Adding it is the first real dependency.
- **We don't need an emulator — tmux already is one.** xterm.js's core value is
  interpreting a *raw pty stream* (cursor moves, scroll regions, alt-screen, wrapping).
  We never see that stream; tmux composed it away. Rendering an already-composed frame is
  just SGR→span, which is the small, boring part of what xterm.js contains — so we'd be
  vendoring a large emulator to use ~5% of it.
- **Full control of the theme + our bolt-ons.** The palette is tuned for the dark terminal
  background, the link anchoring reuses the same OSC-8 materialization as the static views,
  and the "decolor when stale" is a CSS filter over our own spans. Bending xterm.js to all
  three (custom theme, our link handling, a stale filter) is possible but not obviously
  less code than the ~170 lines we wrote.

What we give up by not using it — and the **triggers that would flip the decision**:
- **Fidelity edge cases.** Our renderer covers the SGR subset that agent TUIs actually
  emit; it does *not* implement the full vocabulary (blink, conceal, exotic underline
  styles, some 256/truecolor combining). If a pane shows visibly wrong colors/styles that
  we can't cheaply patch, that's a switch signal — xterm.js is battle-tested here.
- **A genuinely interactive terminal.** The day live view stops being read-mostly and
  wants a real cursor, text selection, reflow-on-resize, or local echo, we'd be building
  an emulator by hand — exactly xterm.js's job. That's the strongest trigger.
- **Raw-stream mode.** If we ever move off composed frames to a `pipe-pane`/PTY stream
  (e.g. for sub-frame latency), we need an emulator client-side and should adopt xterm.js
  rather than hand-roll one.

So the rule of thumb: **keep hand-rolled while live view is a read-only, composed-frame
viewer of a known SGR subset; adopt xterm.js the moment we need true interactivity, a
raw stream, or find ourselves chasing emulator-grade fidelity bugs.** Until one of those,
the 170 lines are cheaper than the dependency.

## Scrolling past the top: into the app's own history

The live frame is the screen plus up to 200 lines of tmux scrollback. That is all of an
inline app's history (Codex, omp, a shell write theirs into tmux), but none of a fullscreen
agent's: Claude Code with `tui: fullscreen`, OpenCode, and Gemini's alternate buffer keep
the transcript inside the app, so the view's top is just the top of the screen.

So scrolling on past the top, after some resistance, sends scroll-wheel reports into the
pane, and the app scrolls its own transcript. The live stream picks up the new screen like
any other change. Scrolling down past the bottom brings the app back notch for notch. It
does not stop when the notch count reaches zero, because apps accelerate the wheel: a burst
of 30 moves Claude Code about 4 lines a notch, and a lone notch moves it 1. Counting notches
back to zero once left the app well short of its bottom. Leaving the pane or pressing
Latest output sends it home with every notch that went up, erring high. Without that, the watcher would keep parsing a
screen scrolled back to old output.

Why the wheel and nothing else, from driving each agent in a private tmux server:

- A wheel report scrolls the transcript in Claude Code and OpenCode. Codex, omp, and a shell
  ignore it, because they never asked for mouse reports.
- Up recalls a previous prompt in every agent tested. Shift+Up raises Codex's reasoning
  effort, which is probably the "thinking level" surprise you get when a terminal turns
  the wheel into arrow keys. Keys are never a stand-in for the wheel.
- PgUp scrolls Claude Code and OpenCode, but they take the wheel anyway, and PgUp does
  nothing in Codex or omp.
- tmux copy mode would reach the inline apps' history, but the view already shows that
  history, and copy mode would freeze the real pane until someone exits it.

So the daemon sends the wheel only when the app is on the alternate screen and has turned on
mouse tracking with SGR reports, and tmux is not in copy mode over it. That is the app's own
statement that it handles the wheel, so it covers an unknown agent correctly and refuses an
inline Claude Code. The client shows the gesture only for the tools whose fullscreen mode
does this (claude, opencode, gemini). After the daemon refuses once, it stops asking for
that pane. The resistance (160px) means a fling that only reaches the top never touches the
pane. Touch uses the same path, because on a phone a fullscreen agent's view is one screen
and nothing else reaches older output.

## Loading earlier history

The frame carries 200 rows of scrollback because it is resent on every screen change, but
tmux keeps far more (50,000 rows here). Scrolling up used to stop at the frame's top. Now
the view fetches older rows in chunks of 500 as you come within a screen of the top and
puts them above the frame, until it reaches tmux's oldest row and says "Start of history".
It loads only on the way up: off the tail, or, when the frame is too short to scroll (a
few long wrapped lines), one request per upward gesture at the top. Nothing loads while a
text selection holds the frame back, because rows would be placed against a frame that
isn't the one on screen. A short frame would
otherwise sit "near the top" forever and pull in all of history by itself.

**Rows are addressed by absolute index from the oldest row tmux keeps.** tmux's own row
numbers count from the screen, so the same number names a different row after every line
of output, and a cached chunk would drift. Counting from the oldest row instead, new
output only adds rows below, so chunks already fetched stay valid with no refetching and
no reconciling. Each frame states the absolute index of its first row, and the client asks
for "the 500 rows before row N". That leaves two windows to get right:

- *Between computing a range and capturing it, output can arrive.* The translation from
  absolute to tmux rows happens inside tmux (`run-shell -C` expands the history size in the
  same command that captures, and reports the size in the same queue), so no output can
  land in between. Doing the arithmetic in Python would need a retry loop on busy panes.
- *Some events do renumber rows*: trimming at the history limit, clear-history, a reflow
  on a width change, and switching to or from the alternate screen. Every frame and chunk
  carries a screen key: the width, whether the alternate screen is up, and a hash of the
  oldest 64 rows tmux keeps. Trimming replaces those rows, and it can refill to the same
  size between two looks, so a size alone can't show it. tmux keeps no count of trims, so
  this is the best signal available. It misses a trim only when the rows the trim exposes
  repeat the ones it dropped, row for row, a tenth of the limit apart. The client drops everything it
  holds when the key changes or the frame's first row moves back, and throws away a chunk
  numbered under another key. The key and first row are part of the frame's identity hash,
  so a reflow that leaves the joined text unchanged still reaches the client.

**The gap.** While you read above the tail, output moves the frame's first row down, past
rows the client never fetched. The new frame waits while the client fetches that gap. Then
the gap rows go in at the end of the held rows, exactly where the old frame showed them,
and the new frame is painted in the same step, so even a row you were reading inside the
frame never moves, not even for the length of a request. A gap is split at the painted
frame's first row: rows below it are on screen in that frame and must land with the next
one, while rows above it are on screen nowhere (left while you followed the tail, or
skipped by a frame painted after a failed fetch), so they land at once, held above the
frame if you are reading it. Rows waiting for a frame that a text selection holds back are
dropped and fetched again, rather than shown twice. If output outruns the requests (still
more than a request's worth behind after one), the waiting rows land and the newest frame
paints anyway, and the rows it passed become a gap above it. A failed fetch paints the frame anyway
and pauses loading for a few seconds before retrying; frames paint normally meanwhile. A chunk asked for by a split that a
newly painted frame has since moved is thrown away and asked for again. While you follow the tail the gap is left for later, so a streaming pane costs
no extra requests. It is filled as soon as you scroll up. The same applies before any history has loaded: once you leave the
tail, rows that output scrolls out of the frame come back as a gap. Only a painted frame
moves the edge (one held back for a text selection is not on screen yet), a frame that
arrives while the first chunk is in flight leaves the empty cache's edge alone, and a frame
whose first row moves back drops the cache however far behind its edge is. The browser's own scroll
anchoring is off for the terminal: Chrome would pin a frame row and undo that placement,
and older Safari has none, so the client holds the view itself, by measuring how far the content
moved rather than predicting it.

**A line tmux wrapped across a chunk's edge** comes back in two pieces, because `-J` joins
wrapped rows only within one capture. Each chunk says whether its last row wraps on (a
two-row `-J` probe at the edge, in the same tmux queue), and the client renders the two
pieces again as one line, so a long URL stays one link. A held piece inside a text
selection is left split rather than replaced under the selection. One cost is accepted:
when such a line straddles the frame's first row, rejoining it can move the frame's rows
by one line as its gap lands. Avoiding that would mean mapping joined lines back to
physical rows, which the client cannot do without tmux's wrap flags. Snapping chunk edges to whole lines
instead would need per-row wrap flags that tmux does not expose. The frame's own first row
can still start mid-line, as it always could.

**On the alternate screen the frame reports nothing earlier.** The history above a
fullscreen app belongs to whatever ran before it; scrolling up there should reach the app's
transcript (the wheel, above). Inline apps get tmux history first, and the wheel only once
there is nothing earlier to load.

Rejected: a bigger frame (resent on every change, and still a cutoff), tmux copy mode
(freezes the real pane), and counting rows from the bottom with client-side bookkeeping
of how much output arrived (it needs the same history size, read less atomically).

## Rejected alternatives

- **Flat client polling at 500ms.** Simplest, but 2 req/s per viewer through the tunnel
  forever, the radio never idles, and latency is no better than the long-poll's 250ms
  server-side checks.
- **SSE / WebSocket now.** Same tunnel-streaming blocker; deferred to the tunnel-protocol
  work, where the long-poll stays as the fallback transport.
- **tmux `pipe-pane` raw stream.** True terminal deltas, but cursor-movement soup needing
  a client-side emulator (which would then justify xterm.js — see
  [Rendering](#rendering-hand-rolled-sgr-vs-xtermjs)); also one-per-pane and it mutates
  pane config, crossing the observer line.
- **On-the-wire line deltas.** The client already sends its frame hash, so the anchor for
  a delta is there — but the edge cases (resize/reflow/alt-screen) all live on the delta
  side, and at 2.8KB gzipped the payoff is marginal. Specced as a post-MVP option, not
  shipped.
- **Volatile-strip the change hash** (ignore spinner/timer churn, as the watcher does for
  its parse trigger). Tried — it froze the spinner in live mode, which is the opposite of
  the point. Reverted: live mode shows the animation; flicker was a repaint-method
  problem, fixed by reflecting real liveness on remount rather than by dropping frames.

## Post-MVP

- **WebSocket transport** + optional **line deltas**, bundled with the tunnel-protocol change (the tunnel can't stream today).
- **Telemetry** (bandwidth, live-time per user, a per-pane "has-live-viewer" presence
  signal to let the daemon throttle LLM cadence when nobody's watching) — see
  `docs/design/live-telemetry.md`.

## Open questions

- If two-viewer or
  battery pressure ever bites, share one 250ms capture loop per pane across waiters.
- The 250ms floor is a server constant, not a client knob — clients shouldn't bid the
  capture rate up.
