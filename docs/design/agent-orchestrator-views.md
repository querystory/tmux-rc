---
title: "Agent View / Orchestrator View"
---

# Design: Agent View and Orchestrator View — two modes for one phone

Status: **draft / proposed** — no code yet. Frontend-only cut-off fix decided; tmux
resize/reflow explicitly rejected (see below). Splits the phone UI into two named,
view-wide modes and fixes the specific pain points in each.

## The want

The phone is used in two genuinely different postures, and today's single UI serves
neither cleanly:

- **Hands-on, low-latency.** Sitting with the phone, driving Claude Code / Codex
  directly — reading exactly what the terminal shows and responding as if attached
  natively. This wants maximum information density and the raw terminal.
- **Asynchronous, on the go.** Firing off a command and not returning for 20 minutes,
  then asking (often by voice) "what needs input from me right now?" This wants the
  high-level summary, the pending decisions surfaced, and voice orchestration — not a
  wall of terminal text.

Two concrete complaints motivate the split:

1. **The terminal is cut off** on the right and bottom. Wide frames overflow the phone
   and Claude's own input line (where the "❯ 1 / 2 / 3" prompt lives) is slid off-screen.
2. **The summary is awkward.** Collapse/expand is a fiddly chevron; the deep summary can
   be up to five minutes stale; and the numbered choices, though modeled, are buried
   mid-card instead of surfaced immediately.

## The insight: the two modes already half-exist

Nothing here is built from zero. The pieces are present but unnamed and un-toggleable:

- **Agent View** ≈ the live-terminal *peek* (`bgTerm`, `app.js:2304`) and the fullscreen
  overlay (`openScreen`, `app.js:3506`), both streaming the real colored frame from
  `GET /api/panes/{id}/live` (`daemon/server.py:442`) at a ~250ms cadence
  (`LIVE_CHECK_SECONDS`, `server.py:439`). This is already near-native latency.
- **Orchestrator View** ≈ the expanded card (`applyCard`, `app.js:1993`): the deep
  `session_summary`, the per-tick `headline`, the pending `question` buttons
  (`buildQuestion`/`keyFor`, `app.js:3360`), the event log, and voice Live Mode
  (`daemon/live.py`).

The only switch between them today is the view-wide `cardsCollapsed` flag (`app.js:356`),
flipped by a small chevron and the `#expand-btn`. **This work promotes that implicit
collapse into an explicit, named two-mode toggle, and fixes each mode's rough edges.**
It is consolidation and polish, not new architecture.

## The two modes

### Agent View — hands-on, native, maximum density

- The card collapses to a **single tap-to-expand status line** (tool icon · `headline` ·
  running/idle badge). The live terminal fills the rest of the deck.
- The terminal container becomes **`overflow:auto` on both axes**, so the right edge and
  the bottom are always reachable by scroll; pinch-zoom (`pinchZoom`, `app.js:3580`) is
  retained on top.
- **Fit-to-width autoscaling.** The font size is derived from the frame's column count
  against the container width (`frameCols × ch-width` vs viewport), clamped to a legible
  floor (~7–8px). At or above the floor everything fits horizontally with no scroll;
  below it (very wide frames) we fall back to horizontal scroll + pinch rather than
  shrinking to an unreadable size.
- **The agent's own input line stays visible.** Today `tuckChrome` (`app.js:2137`)
  deliberately slides the agent's composer / status rows off the bottom with a negative
  margin. Agent View **skips the tuck** so Claude's input box and its "❯ 1 / 2 / 3"
  prompt are on screen, while tail-pinning keeps new output autoscrolled to the bottom.
- The keystroke bar (`#bar-keys`: Enter / Esc / ↑ ↓ / Ctrl-O / Ctrl-B / Ctrl-C /
  Ctrl-D) and composer stay visible and target the shown pane — the existing `send-keys`
  path (`server.py:542`). This is the native interaction surface.
- Agent View **subsumes the fullscreen overlay** (`openScreen`): the deck itself is now
  the full-bleed terminal, so the separate modal is retired. Its **"Sun mode"**
  high-contrast toggle (for outdoors) is kept as an Agent-View control.

### Orchestrator View — asynchronous, voice-driven

- The card body is shown; the live terminal peek is hidden/minimized.
- A **fleet-level "Needs input" roll-up** is pinned at the very top: every waiting pane
  with its tappable choices, tap to jump to that pane. This is the visual twin of the
  voice question "what needs input from me right now?" The active pane's `question`
  (`applyQuestion`, `app.js:3360`) is elevated above the summary.
- **Fresher summary.** The deep `session_summary` refresh (today a fixed 300s cadence,
  `SUMMARY_REFRESH_SECONDS`, `watcher.py:52`) becomes **(b) event-triggered** on
  significant new events **plus (c) refresh-on-demand** when a pane enters Orchestrator
  View or when voice asks about it. The always-current `headline` (per-tick parser)
  remains the top line regardless.
- Voice Live Mode (`#lm-btn`, `daemon/live.py`) is the primary companion here.

## Why a view-wide toggle, in two places (rejected: per-pane; single location)

The mode is a *posture*, not a per-pane property — you are either heads-down on one agent
or scanning the fleet — so it is view-wide, matching today's `cardsCollapsed`. State
becomes `viewMode ∈ {'agent','orchestrator'}` in `app.js` (superseding `cardsCollapsed`),
persisted to `localStorage` so it survives reloads and pane swipes.

The control lives in **two** places: a two-segment control in the `#top` ribbon
(replacing `#expand-btn`), and a thumb-reachable quick-swap next to the bottom keys bar.
Agent View is one-handed hands-on work where the top ribbon is an awkward stretch; a
single top-only control was rejected for that reason. Icons are inline Lucide glyphs, not
emoji, per `AGENTS.md`.

## Why frontend-only for the cut-off (rejected: tmux resize / reflow)

The terminal's width is tmux's own PTY column count — whatever the *laptop* client set
(often 120+ cols). The tempting fix is to resize the watched window to the phone's
geometry so Claude Code reflows narrow and reads natively. **Rejected**, because tmux
sizes a window to its *smallest* attached client: forcing phone geometry would shrink the
laptop's view of that same window. That directly violates the founding property that the
laptop and the phone attach to the same session simultaneously without either disturbing
the other (see [overview.md](overview.md), "Bidirectional multiplexing").

So the cut-off is fixed **entirely on the client**: fit-to-width autoscaling guarantees
nothing is hidden, and scroll + pinch reach anything below the legible floor. The cost is
that a very wide frame renders small and needs a pinch to read comfortably — an accepted
tradeoff, and strictly better than today's hard clip. An opt-in per-pane reflow endpoint
(`resize-window` under `window-size manual`, gated on no other attached client) remains a
possible future addition but is out of scope here.

## Why 250ms is enough for "native" (rejected: control-mode now)

The live stream already checks for a changed frame every 250ms server-side
(`LIVE_CHECK_SECONDS`) and long-polls the whole colored frame. For hands-on work this is
imperceptibly close to native. tmux control-mode (`-CC`) or a tighter cadence would lower
latency further, but it is a project of its own (see [live-view.md](live-view.md) and the
control-mode notes) and is deferred until 250ms actually feels laggy in practice. Nothing
in this design changes if the stream's input source is swapped later.

## What stays put

- **List mode** (`listFilter`, `app.js:1382`) is orthogonal — it is the fleet index and
  is reachable from either view.
- The `/api/panes/{id}/live` and `/api/state` channels are unchanged; this is a
  presentation split over the same data.
- No classifier or parser-prompt changes, so no new eval samples are required
  (`AGENTS.md`).

## Phasing and file map

| Phase | Change | Files |
|---|---|---|
| 1 | `viewMode` state + dual toggle (top ribbon + bottom). Agent View: full-height scrollable terminal, fit-to-width autoscaling, skip `tuckChrome`, keep input line visible. Subsume the fullscreen overlay (keep Sun mode). | `web/app.js` (state ~356; toggle in `#top`; `bgTerm`/`paintTerm` `app.js:107,2304`; `tuckChrome` `app.js:2137`; `peekRO` `app.js:2409`; retire `openScreen` `app.js:3506`), `web/index.html` (`#top`, `.deck`, `.bg-term` CSS) |
| 2 | Orchestrator View: fleet "Needs input" roll-up + elevated active-pane `question`; hide the peek. | `web/app.js` (`applyCard` `app.js:1993`, `applyPaneBody` `app.js:1928`, `applyQuestion` `app.js:3360`) |
| 2 | Summary freshness: event-triggered refresh + on-demand refresh on entering Orchestrator View / voice ask. | `daemon/watcher.py` (`SUMMARY_REFRESH_SECONDS` `:52`, bootstrap refresh `:517`), `daemon/server.py` (on-demand refresh hook) |

Verified live on the phone (there is no JS test harness — `AGENTS.md`).

## Success criteria

- Toggle freely between Agent View and Orchestrator View; the choice persists across
  reloads and pane swipes.
- In Agent View: no terminal text is unreachable (right and bottom scroll/zoom to
  everything), the agent's input line and any numbered prompt are visible, and typing /
  keys round-trip as on a native attach.
- In Orchestrator View: the panes waiting on input — and their tappable choices — are the
  first thing seen, the summary reflects recent activity rather than a five-minute-old
  snapshot, and voice orchestration reads the same fleet state the roll-up shows.
