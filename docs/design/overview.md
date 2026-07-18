---
title: Design Overview
---

# tmux-rc — Design

This documents the *why* behind the architecture and the decisions that were weighed
and rejected. For how the code is laid out, read the source under `tmux-rc/`.

## Guiding constraint: the terminal is the interface, not the agent

Every design choice flows from one decision: **observe the terminal, not the agent.**
We do not integrate with Claude Code's, Codex's, or Gemini CLI's internal protocols.
We read the rendered text of a tmux pane and send keystrokes back. This is what makes
tmux-rc vendor-agnostic — a raw `psql` prompt and a Claude Code permission dialog
are the same kind of thing to us: text on a screen, possibly waiting for input.

The cost of this choice is that classification is inherently fuzzy — we're parsing
human-facing UI, not a structured API. We accept that and lean on heuristics plus a
cheap LLM to absorb the fuzziness. The benefit is enormous reach with near-zero
per-tool integration work: supporting a new agent is usually just adding a prompt
pattern, sometimes nothing at all.

## Why tmux (rejected: raw pty, screen, integrating each agent)

tmux is already how the target user runs long sessions, and it solves the two hard
problems for free:

- **Read access without disruption.** `capture-pane` returns the current screen
  contents for any pane at any time, from an unrelated process, without attaching or
  interfering. A raw pty wrapper would require us to sit in the I/O path and re-render
  the terminal ourselves — far more code and a source of bugs.
- **Bidirectional multiplexing.** Multiple clients can attach to one tmux session
  simultaneously, and `send-keys` injects input from outside. So the user can stay
  attached on their laptop while tmux-rc (and the phone) also watch and act on the
  same session — no client is exclusive. This is exactly the "both the app and a human
  connected at once" property we want, and tmux gives it natively.

Rejected alternatives: hand-rolling a pty multiplexer (reimplementing tmux, badly);
`screen` (weaker scripting surface than tmux's `capture-pane`/`list-panes`);
integrating each agent's own remote/IPC hooks (defeats vendor-agnosticism, N× work).

## Why polling (rejected: tmux control-mode for the PoC)

The watcher polls `capture-pane` on a short interval (~1.5s) and diffs against the
previous capture. tmux *does* offer control-mode (`tmux -CC`) for push-based event
streams, which would be lower-latency and lower-overhead. We deferred it because:

- The timeline feature needs periodic snapshots anyway, so we're sampling regardless.
- Polling is trivial to reason about and debug; control-mode parsing is a project of
  its own.
- ~1.5s latency is imperceptible for "is my agent done / blocked" — this is a
  glance-and-unblock tool, not a live terminal.

Control-mode is the natural post-PoC upgrade; nothing in the state model or the HTTP
API changes when we swap the watcher's input source.

## Why heuristics first, LLM lazily (rejected: LLM on every tick)

Classification is layered deliberately:

1. **Heuristics (free, instant, always run).** Tool detection from the running command
   and known banners; activity from tail patterns (a shell prompt at the bottom with
   unchanged text ⇒ idle; a known question pattern ⇒ waiting; otherwise running);
   Claude Code's context% and cost from its predictable status line via regex.
2. **LLM (Gemini Flash Lite, lazy).** Only invoked when heuristics can't produce a
   clean one-line status *and* the pane actually changed, or when a prompt is detected
   but its options don't parse cleanly. It returns structured JSON: a status phrase,
   an activity guess, and any question+options.

Running the LLM on every pane every tick was rejected on cost and latency: it would be
constant spend and constant lag even when a pane is idle or unchanged, for output a
regex already nails (a shell prompt, a Claude status line). The lazy design keeps the
common case free and reserves the model for genuinely ambiguous screens — which is
also where a model's fuzziness-tolerance actually earns its keep.

## Why Gemini Flash Lite via Vertex (rejected: Anthropic, local model)

- **Flash Lite** is cheap and fast, and Gemini models are strong at reading both
  terminal text and screenshots — which matters because the timeline gives us images
  essentially for free, and a multimodal model can read a rendered screen directly.
- **Vertex** because a Google project (`qs-backend-dev`) is available and reaching it is
  one client construction with `vertexai=True`. (Auth has since moved from developer ADC
  to a long-lived service-account key so the unattended daemon doesn't wedge on reauth —
  see [design/durable-vertex-auth.md](durable-vertex-auth.md).)
- Rejected Anthropic: the entire premise is not being locked to one provider, and the
  user's inference already runs through Google. Rejected a local model (Ollama): slower
  and more variable at structured extraction for no PoC benefit when Vertex is a free
  API call away.

Crucially we **reimplement** the ~30-line Vertex call rather than importing qs-app's
`backend.genai.llm`. That library is excellent but pulls in FastAPI/SQLAlchemy/WorkOS
and a config-loading lifecycle we don't want in a standalone tool. Copying the small,
stable slice (client construction, inline image parts, `response_mime_type=json`) keeps
tmux-rc independent and lets the watcher be rewritten in another language later.

## Why Python (rejected: Go, C)

The tmux side is just subprocess calls — no hot loop, no pty wrangling, no low-latency
requirement — so it's trivial in any language, which means the language should be
chosen for the *rest* of the system. Python wins there: FastAPI + the Vertex client are
a few lines each, and it keeps the watcher small enough to port to Go later (for a
single static binary) without touching the PWA. C was rejected outright — hand-rolling
subprocess and HTTP for a glorified screen-scraper is pure downside.

## State model and API shape

The unit is a `PaneState`: identity (pane/window/session), classified `tool`,
`activity` (running/idle/waiting) with `idle_seconds`, a short `status_line`, optional
`context_pct`, an optional `question` (prompt + options), a snapshot reference, and a
timestamp. The HTTP API is list-shaped from day one — `GET /api/state` returns a list
even in Milestone 1's single-pane mode — so the jump to all-panes is a watcher change,
not an API or UI redesign. Answers post to `/api/panes/{id}/send`, which is a thin
wrapper over `send-keys`.

## Build order

Milestone 1 proves the entire vertical slice on **one** pane: watch → classify →
phone card → detect waiting → tap → `send-keys` → snapshot timeline. Only once that
loop demonstrably works do we fan out to all panes in Milestone 2, which introduces no
new architecture. This ordering front-loads the risky, end-to-end integration (does
the answer round-trip actually advance a real agent's prompt?) instead of the easy
breadth (more rows).

## Security posture (PoC)

The service exposes read access to terminal contents and the ability to inject
keystrokes — powerful and dangerous. For the PoC it relies entirely on network
isolation: bind to the LAN, or front it with a tunnel the user controls. No auth,
no TLS termination of our own. This is acceptable only because it's a single-user
dogfood tool on a trusted network; any real deployment must add authentication before
exposure. This is called out as a non-goal, not an oversight.
