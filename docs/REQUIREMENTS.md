# termiphone — Requirements & Backlog

Living capture of requirements gathered while dogfooding (building termiphone from
inside termiphone). Grouped by area; `[x]` done, `[ ]` open, `[~]` partial. This is
the source of truth for "what did Shapor ask for" — keep it current as we iterate.

## Core loop (Milestone 1) — DONE

- [x] Watch a tmux pane, classify state, serve a phone PWA over LAN.
- [x] Detect a waiting prompt, show options as tappable buttons, round-trip the
      answer via `send-keys`. Verified against a real Claude Code model picker.
- [x] Snapshot timeline (scrollable strip of recent captures).
- [x] Installable PWA (manifest + service worker).

## Dev experience — DONE

- [x] `make dev` runs the server with **uvicorn auto-reload** (reload ON by default;
      `TERMIPHONE_RELOAD=0` to disable). Edits take effect in ~1s. Reset of in-memory
      state on reload is safe — tmux is the source of truth, state rebuilds in a tick.
- [x] Makefile targets: `dev`, `run`, `test`, `fmt`.

## Classification quality — IN PROGRESS

- [x] Rich status from the agent status line via the LLM (model, context %, cost,
      session stats) instead of regex-picking one line.
- [x] Extract the "working" indicator: whimsical verb ("Cultivating"), elapsed
      ("11m46s"), tokens streamed ("13.3k").
- [x] Ignore editor/multiplexer chrome: vim `-- INSERT --`, tmux footer
      (`shift+tab to cycle`) — NOT the agent's state.
- [x] **Stop false-positive questions.** Heuristic now requires a menu at the bottom
      OR a `❯` selection cursor (prose never has one); y/N only on the last line;
      bare-"?" branch removed. LLM prompt tightened to omit `question` unless a genuine
      interactive affordance is present. Regression test added.
- [~] **status_line should summarize the TASK, not just echo the verb.** LLM prompt now
      asks to summarize WHAT is worked on over echoing the spinner word. Needs live
      confirmation it actually produces task summaries (may need transcript context,
      not just the status line, fed to it).
- [x] **Shell-command awareness.** LLM prompt now covers plain-shell panes (running/
      last command, finished, exit).
- [~] Proactive LLM pass when a screen is idle/stable a few seconds (so status is
      populated even when heuristics look "fine"). Implemented; needs the prompt
      tightening above so it doesn't invent questions.

## UI / presentation — IN PROGRESS

- [ ] **Claude icon must be the actual Claude/Anthropic mark**, not a cloud. STOP
      hand-drawing SVGs. Use the real asset: https://claude.ai/favicon.ico (fetch &
      bundle it, or reference it). Tool icons should be real logos where possible.
- [ ] **Mode as an icon/badge** like Claude web: plan mode / accept-edits / bypass
      permissions / normal. Model extracts `mode`; UI should show it compactly.
- [ ] **Metadata row**: model · context% bar · cost · mode, laid out cleanly (today
      it's a run-on status string).
- [ ] **Background-agent count** per pane — how many sub-agents are running;
      collapsible/expandable detail.
- [ ] Don't waste space: the card should lead with a human summary of the work.

## Milestone 2 — all panes

- [ ] Fan out from single target pane to ALL panes/windows (`list_panes`). Dashboard
      = one row per pane; waiting floated to top. API + state model already
      list-shaped, so this is a watcher change only. (User has ~11 windows — this is
      the real use case.)
- [ ] Use the window NAME as the label (e.g. "Resolve PR 38", "output-fixes"), not
      just "work:0" — far more useful with many windows.

## Later / non-goals for PoC

- [ ] Push notifications (currently poll every 2s).
- [ ] Auth (currently LAN/tunnel only — no auth; do NOT expose publicly).
- [ ] Rendered PNG snapshots (currently raw text timeline).
- [ ] tmux control-mode instead of polling.
