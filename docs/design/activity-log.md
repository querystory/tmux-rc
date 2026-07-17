# Design: the activity log (surviving a reload) & scrolling back further

Status: **draft / thinking** — an MVP is scoped here; the post-MVP section is
speculative. Captures where the feed's history lives, why, and how much of it we can
honestly reconstruct.

## The problem

The card's activity feed — the bullet lines of "what the thing did" — is the running
story of a session. Today it's assembled **in the browser**: each `/api/state` poll
delivers only the 1-2 events currently on screen, and the client appends them to an
in-memory array (`eventLog`), deduping by text. Scrollback over the last several
minutes is therefore a property of *one browser tab's lifetime*.

That breaks in an ordinary way: **reload the page and the feed is empty.** Everything
the parser observed over the last hour was only ever in JS heap. You reload (phone
slept, PWA evicted, you hard-refreshed after a deploy) and the card falls back to a
near-empty feed until new activity trickles in. Session bootstrap (see below) softened
the *cold daemon start* case, but a plain reload against a long-running daemon still
starts the feed from zero.

The narrower framing: **the feed is the one piece of card state that the daemon
observes over time but does not keep.** Everything else on a card is either
recomputed every tick from the live screen (headline, activity, question) or already
held server-side (the snapshot ring buffer, the bootstrap summary). The event stream
is the exception, and only because of an early "cheap version, no backend" shortcut
whose own comment called a server-side log "the future feature."

## The load-bearing constraint: tmux is the state

tmux-rc's spine is *the terminal is the interface* (see DESIGN.md). Nothing we build
here may pretend to be a system of record. So two rules bound every option:

1. **No persistence to disk.** State lives in tmux. Anything we hold is a bounded
   in-memory cache of *observations*, lost on daemon restart, and that loss must
   degrade gracefully — not corrupt, not require recovery, just thin out.
2. **The recovery path is re-observation, not a saved file.** When the cache is empty
   (fresh daemon), we rebuild it by reading tmux again — which is exactly what session
   bootstrap already does (deep-read scrollback → reconstruct milestones).

The subtle point that justifies caching at all: **scrollback is not a faithful record
of what we observed.** TUI agents (Claude Code especially) redraw their screen in
place. A question asked and answered an hour ago, a transient menu, a spinner state —
the parser *saw* these live, but tmux's scrollback no longer contains them; the
in-place redraws overwrote the cells. So the observations the feed is made of are
information tmux has already discarded. This is the same reason the **snapshot ring
buffer** exists server-side: it holds screens tmux has scrolled or redrawn away. The
activity log is the same category of thing — a bounded cache of observations, one the
daemon already has precedent for keeping.

## MVP: move the log into the daemon (in-memory)

Keep the per-pane activity log in the watcher process instead of the browser, and
serve it. Concretely:

- The watcher keeps `events_log[pane_id]` — a bounded list (`~300` entries) of the
  events it has emitted for that pane, each already carrying `{text, file?, meta?,
  ts}`, plus the `historical` flag for bootstrap-reconstructed ones.
- Session bootstrap seeds the **front** of this list (its milestones predate anything
  observed live), and also feeds them into the existing "already reported" dedup list
  so live parses don't restate them.
- A new read-only endpoint, `GET /api/panes/{id}/events`, returns the list. Each
  `/api/state` entry advertises `events_len` so the client refetches a pane's log only
  when it has grown — cheap, and no streaming needed.
- The client stops accumulating; it renders whatever the endpoint returns.

**What this costs:** one bounded dict in the daemon (same shape and lifecycle as
`snapshots`), garbage-collected with the other per-pane stores when a pane closes.
Worst case ~45KB/pane. No new LLM calls, no disk, no schema.

**What it buys:** the feed survives reloads, multiple clients see the same feed, and
the client shrinks (delete the accumulate-and-dedup logic — dedup already happens at
the source, in the parser's feedback loop).

**What it deliberately does NOT do:** persist across daemon restarts. A restart drops
every `events_log`; bootstrap then reconstructs an approximation from scrollback on
first sight of each pane. That is the correct behavior under the tmux-is-state rule —
the log is a cache, and its miss path is re-observation.

### Why not the alternatives

- **Keep client accumulation, just re-seed from bootstrap on reload.** Smaller change,
  but it only restores the *reconstructed* history; every event observed live between
  the last bootstrap and the reload still vanishes. Half-fixes the reported problem.
- **Re-derive on demand (no stored log anywhere).** Purest "tmux is the state": when a
  client wants history, re-read scrollback through the bootstrap prompt (cached by
  scrollback hash). But it costs an LLM call and seconds of latency per *view*, and —
  the crux above — it's lossy for TUI panes regardless, because the in-place redraws
  are already gone from scrollback. You pay more to get less than the live log had.
- **Persist to disk / Postgres.** Violates rule 1 for the open-source/tunnel mode. Note
  the *hosted* mode (cloud-architecture.md) already persists state in Postgres for
  historical timelines — so durable history is a **commercial-tier** feature by design,
  not something the local daemon should grow. The MVP here is explicitly the in-memory,
  open-source floor; the hosted tier is where "history across restarts" legitimately
  lives.
- **Do nothing.** Defensible now that bootstrap exists — the session summary carries
  orientation even with a thin feed. Rejected because the feed is the primary
  "what's been happening" surface and a blank-on-reload feed reads as broken, but this
  is the honest fallback if the in-memory log ever feels like too much machinery.

## Post-MVP: scrolling back further than the live screen

The MVP log only contains what the parser happened to observe while watching. Two
distinct "further back" wants remain, in increasing boldness:

### 1. Read-only: mine deeper scrollback (no input to the pane)

tmux retains scrollback beyond the visible screen (`history-limit`, often thousands of
lines). We already read a slice of it once, at bootstrap. A read-only "load earlier"
could re-read a *larger* slice on demand and summarize the older span into more
historical events — still pure observation, still no keystrokes sent, just a bigger
`capture-pane -S`. This extends history as far as tmux's own retained buffer goes, and
no further. Cheap, safe, and it's really just "bootstrap with a deeper window,
triggered by the user." Good candidate for the immediate follow-up.

Its ceiling is tmux's `history-limit` and, again, the TUI-redraw problem: for a
Claude Code pane, even the full retained scrollback is mostly the *current* rendered
frame's lineage, not a transcript. Which motivates the bolder option.

### 2. Active: probe the agent's OWN history UI (input required — flag-gated)

The richest history for an agent pane lives inside the agent, reachable only by
*driving its UI*: Claude Code's Esc-Esc **Rewind** picker enumerates past user
messages; scrolling a pager, paging a `git log`, or asking the agent "summarize what
we've done" all surface real history that plain observation cannot. tmux-rc can already
send keys — so it could **probe**: send the keystrokes that reveal history, capture the
frames that result, summarize them, then restore the pane to where it was.

This is powerful and genuinely different in kind, because **it sends input to the
user's live session.** That crosses the line from *observer* to *actor*, and it must be
explicitly consented to, not a silent default. Proposed shape:

- A per-session (or per-pane) flag — *"tmux-rc may drive my tabs to explore history"* —
  vs. the default *"review only, never touch my panes for history."* Off by default.
- When on, a "reconstruct history" action performs a **bounded, reversible probe**:
  e.g. Esc-Esc to open Rewind, arrow through the entries capturing each, Esc to close —
  leaving the pane exactly as found. The reversibility is the safety property; a probe
  that can't cleanly restore state must not run.
- This is a special case of the **agentic control plane** (agentic-control-plane.md):
  "intent → correct keystroke sequence." History-probing is one intent among many the
  control plane will drive, so it should be built on that machinery when it exists, not
  as a bespoke hack. It also generalizes past Claude Code — the same "drive the UI to
  reveal more, then restore" pattern works for pagers, `git log`, REPL history, any
  program with a history affordance — which is why it belongs behind the general
  control plane rather than hardcoded to Rewind.

Ordering rationale: read-only deeper-scrollback (option 1) is a small, safe extension
of bootstrap and can ship soon after the MVP. UI-probing (option 2) waits on both the
consent-flag UX and, ideally, the control-plane foundation — it should not be the thing
that first teaches tmux-rc to send unsolicited keystrokes.

## Open questions

- **Log cap vs. bootstrap seed.** Bootstrap emits up to ~12 milestones; the live log
  grows to ~300. When both exist, do reconstructed events stay pinned at the front
  forever, or age out under the cap like any other entry? (Leaning: age out — once real
  observed history fills the window, the reconstruction has served its purpose.)
- **Multi-client divergence.** With the log server-side, two phones show the *same*
  feed — good. But a client that scrolled up to read old events shouldn't get yanked
  when the log grows; the existing scroll-preservation logic must key off log identity,
  not just length.
- **`events_len` as the refetch trigger** assumes append-only. Aging entries out of the
  front (see cap question) changes length without appending — the client needs a signal
  that distinguishes "grew" from "rotated," or it refetches on every rotation. A small
  monotonic counter or a "first entry ts" alongside the length resolves it.
