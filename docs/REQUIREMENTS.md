# termiphone — Requirements

Source of truth for what Shapor asked for. `[x]` done+verified, `[ ]` open, `[~]` partial/unverified.

## Dev experience
- [x] `make dev` runs uvicorn with auto-reload (reload ON by default). Verified: edits take effect ~1s.
- [x] Makefile with dev/run/test/fmt.
- [x] LLM trace log to grep (`/tmp/termiphone-llm.log`, override `TERMIPHONE_LLM_LOG`).
- [ ] **Keep this REQUIREMENTS.md and PROGRESS.md current, and COMMIT to git as we go.** (Was dropped — user had to ask 3×.)

## Model / LLM
- [x] Use current Gemini flash-lite: **gemini-3.1-flash-lite** (was wrongly on 2.5-flash-lite). Override `TERMIPHONE_GEMINI_MODEL`.
- [ ] status_line must SUMMARIZE THE TASK ("Building termiphone remote control…"), optionally a subtask — not just echo the working verb.
- [ ] Shell panes: describe running command / finished / exit status, not just Claude Code.

## Classification correctness
- [x] **Tool oscillates claude→shell** — FIXED (c758f73). Prompt now decides tool from the whole picture; stickiness is time-bounded (bridge shell→agent only if agent seen <8s ago). Verified live: Claude pane stays "claude" while shelling out.
- [~] **False-positive questions**: heuristic tightened; LLM prompt improved (only real affordances = waiting). Better but not fully verified — architecture reckoning will subsume this.

## UI / presentation
- [ ] **Surface the agent's task/TODO list** on the card. Claude Code shows "N tasks
      (X done, Y open)" + the items in the terminal; extract and render them so you can
      see progress from the phone. (Noticed while building Rewind support.)
- [~] **Rewind history picker (Esc-Esc).** Extract the restore-history entries and let
      the user scroll through them + restore. ↑/↓ move the ❯ cursor (card reflects),
      Enter restores; visible + scroll to load more. WORKING on phone. TODO: the
      "N files changed +X -Y" diff-stat lines render as separate entries instead of
      attaching to their entry as a red/green colored sub-label. Fix the note parser.
- [x] **Label uses the agent's session name** (c758f73). Parser reads the title
      Claude Code prints (e.g. "termiphone-dev") and it wins over tmux/cwd label.
      Verified live.
- [ ] **Reconsider ↑/↓ buttons** — only clearly useful for navigating pickers like
      Rewind; keep them for that. Shell history recall from a phone is marginal.
- [x] Claude logo — use the real claude.ai icon (web/claude.png). Confirmed by user.
- [x] Mode as badge (plan / accept-edits / bypass), color-coded like Claude web.
- [x] Clean metadata chip row: model · ctx% bar · cost · mode · agent count.
- [x] Background-agent count per pane (PaneState.agents, extracted by LLM).
- [x] Lead with a human task summary (status_line), working subline verb·elapsed·tokens.
- [x] Preserve typed input across the 2s re-render (was clearing after ~1s).
- [x] **Attach images (phone → agent).** 📎 button (file/camera) uploads to
      /api/panes/{id}/image; server puts the bytes on the system clipboard (wl-copy on
      Wayland, xclip on X11) and sends Ctrl-V so Claude Code embeds the image inline —
      typing a path does NOT work. Verified E2E on the host (pasted the real image).
      HOST DEP: needs `wl-clipboard` on Wayland (sudo apt install wl-clipboard).
- [x] Special-key buttons: Enter/Esc/↑/↓/Ctrl-O/Ctrl-B/Ctrl-C.
- [x] **Persistent bottom input bar** (76a22ee). Single bar with special keys + text
      input + attach. Tap a card to target it (input goes to that pane). Tapping also
      focuses the pane in tmux (d61d4eb).
- [ ] **Turn-timeline view** — see Activity narrative section below.

## ⚠️ ARCHITECTURE — READ BEFORE ADDING FEATURES
The classifier has drifted into a pile of brittle regexes that hard-code the EXACT
text of Claude Code's UI (Rewind header, ❯ cursor, "No code changes", "N files
changed", status-line fields, spinner glyphs). This DEFEATS THE POINT of using an LLM:
it breaks the instant Anthropic changes a label, the user edits their status line, or
termiphone points at Codex/Gemini (which render completely differently).

**Correct architecture (to refactor toward): the LLM is the parser.** Feed the pane
text (and later the screenshot) to Flash Lite and get back structured JSON for whatever
it sees — status, question, rewind picker, task list, narrative — vendor-agnostic and
resilient to UI wording changes. Regexes should be at most a cheap fast-path for the
hot loop (change detection / obvious idle), NEVER the source of truth for parsing agent
UI. Before adding any more UI-specific detection: move parsing into the LLM schema.
PAUSED here (2026-07-14) to redesign around this before writing more scrapers.

## Activity narrative
- [x] **Accumulated activity log** (e7edfdd). Events accumulate client-side per-pane
      into a scrollable region (max 40vh, sticks to newest, scroll-back preserved).
      Model-side dedup: watcher feeds prior events back to parser so it only emits
      genuinely new ones. Verified live: distinct entries, no spam.
- [x] **Idle burst summary** (c758f73). After 60s idle with accumulated events, one
      LLM call summarizes the recent burst into a {from, to, text, count} span. UI
      collapses oldest events under an expandable summary line.
- [ ] **Turn-timeline view** — durable log of completed turns (verb, duration, tokens).
      Requires detecting turn boundaries in the watcher. (Moved from UI section.)

## Milestone 2 — all panes
- [x] Label = window NAME (done in tmux.py).
- [x] **Fan out to ALL panes** (bd56a84). Watcher loops over `tmux.list_panes()`,
      per-tick work extracted into `_tick_pane`. Cards sorted waiting > running > idle.
      Per-pane state independent (fingerprint, parse cadence, events, sticky tool).
      GC drops state for closed panes. Verified live with multiple panes.

## Non-goals for PoC
- Push notifications; auth (LAN/tunnel only, do NOT expose); PNG snapshots; tmux control-mode.
