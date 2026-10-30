# tmux-rc — Requirements

Record of what the project owner asked for during the 2026-07 proof of concept, with the
boxes brought up to date on 2026-10-29. `[x]` done+verified, `[ ]` open, `[~]` partial/unverified.
Current behaviour is documented from [docs/_index.md](_index.md), not here.

## Dev experience
- [x] `make dev` runs uvicorn with auto-reload (reload ON by default). Verified: edits take effect ~1s.
- [x] Makefile with dev/run/test/fmt.
- [x] LLM trace log to grep (`/tmp/tmux-rc-llm.log`, override `TMUXRC_LLM_LOG`).
- [x] **Keep this REQUIREMENTS.md current, and commit as we go.** (The separate progress log was retired: git history is the log.)

## Model / LLM
- [x] Use current Gemini flash-lite: **gemini-3.1-flash-lite** (was wrongly on 2.5-flash-lite). Override `TMUXRC_GEMINI_MODEL`.
- [x] status_line must SUMMARIZE THE TASK ("Building tmux-rc remote control…"), optionally a subtask — not just echo the working verb. (Now the card's `headline`.)
- [x] Shell panes: describe running command / finished / exit status, not just Claude Code.

## Classification correctness
- [x] **Tool oscillates claude→shell** — FIXED (c758f73). Prompt now decides tool from the whole picture; stickiness is time-bounded (bridge shell→agent only if agent seen <8s ago). Verified live: Claude pane stays "claude" while shelling out.
- [x] **False-positive questions**: subsumed by making the LLM the parser (below); `waiting` is decided by the agent's final message, with `waiting_on` telling user waits from external ones.

## UI / presentation
- [x] **Surface the agent's task/TODO list** on the card (the parser's `tasks`, plus
      `subagents` for background workers).
- [x] **Rewind history picker (Esc-Esc).** Extract the restore-history entries and let
      the user scroll through them + restore. ↑/↓ move the ❯ cursor (card reflects),
      Enter restores; diff-stat lines attach to their entry as its `note`.
- [x] **Label uses the agent's session name** (c758f73). Parser reads the title
      Claude Code prints (e.g. "tmux-rc-dev") and it wins over tmux/cwd label.
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
      /api/panes/{id}/image; server normalizes to PNG, puts it on the clipboard
      (wl-copy/xclip) and sends Ctrl-V so the agent embeds it inline. ALWAYS png:
      paste handlers ask the clipboard for image/png, so an offer in the upload's own
      mime (a phone JPEG) reads as empty and silently no-ops — the original bug.
      When no clipboard tool works, falls back to TYPING the staged file's path into
      the pane (agents read files from disk) — degraded but working, never a silent
      200. Stagings live under /tmp/tmux-rc-images (0700, owned-dir enforced, day-pruned).
      Any other file (a PDF, say) attaches too: staged the same way under a generated
      name with the client's extension, and typed into the draft as its path.
- [x] Special-key buttons: Enter/Esc/↑/↓/Ctrl-O/Ctrl-B/Ctrl-C.
- [x] **Persistent bottom input bar** (76a22ee). Single bar with special keys + text
      input + attach. Tap a card to target it (input goes to that pane). Tapping also
      focuses the pane in tmux (d61d4eb).
- [ ] **Turn-timeline view** — see Activity narrative section below.

## ARCHITECTURE (resolved: the LLM is the parser — `openbus/parser_prompt.txt`)
At the time of writing the classifier had drifted into a pile of brittle regexes that hard-code the EXACT
text of Claude Code's UI (Rewind header, ❯ cursor, "No code changes", "N files
changed", status-line fields, spinner glyphs). This DEFEATS THE POINT of using an LLM:
it breaks the instant Anthropic changes a label, the user edits their status line, or
tmux-rc points at Codex/Gemini (which render completely differently).

**Correct architecture (to refactor toward): the LLM is the parser.** Feed the pane
text (and later the screenshot) to Flash Lite and get back structured JSON for whatever
it sees — status, question, rewind picker, task list, narrative — vendor-agnostic and
resilient to UI wording changes. Regexes should be at most a cheap fast-path for the
hot loop (change detection / obvious idle), NEVER the source of truth for parsing agent
UI. Before adding any more UI-specific detection: move parsing into the LLM schema.
Paused here on 2026-07-14 for that redesign; prompt changes now go through the eval
harness described in [AGENTS.md](https://github.com/querystory/tmux-rc/blob/main/AGENTS.md#classifier--prompt-changes).

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
- [x] Blocking push notifications for user-waits, with secure option replies where the
      platform supports actions and an iPhone deep link to the pending pane.

## Non-goals for PoC
- Milestone/completion pushes (after noise calibration); auth (LAN/tunnel only, do NOT
  expose); PNG snapshots; tmux control-mode.
