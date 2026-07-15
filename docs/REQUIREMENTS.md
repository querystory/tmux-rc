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

## Classification correctness (TOP PRIORITY — actively broken)
- [ ] **Tool oscillates claude→shell** when Claude shells out (foreground becomes bash/git). Must be STICKY: once a pane is a known agent, keep it. Sticky state lives in the watcher; `_detect_tool` now sniffs agent signatures (✳/context left/bypass) but stickiness not yet wired in the watcher. UNVERIFIED.
- [ ] **False-positive questions**: transcript prose ending in "?" or with 1./2. bullets read as a live prompt, by heuristic AND LLM. Only real affordances (boxed prompt, ❯ on options, input line) = waiting. Heuristic tightened; LLM prompt NOT yet. UNVERIFIED.

## UI / presentation
- [ ] **Surface the agent's task/TODO list** on the card. Claude Code shows "N tasks
      (X done, Y open)" + the items in the terminal; extract and render them so you can
      see progress from the phone. (Noticed while building Rewind support.)
- [~] **Rewind history picker (Esc-Esc).** Extract the restore-history entries and let
      the user scroll through them + restore. ↑/↓ move the ❯ cursor (card reflects),
      Enter restores; visible + scroll to load more. WORKING on phone. TODO: the
      "N files changed +X -Y" diff-stat lines render as separate entries instead of
      attaching to their entry as a red/green colored sub-label. Fix the note parser.
- [ ] **Label should use the tmux SESSION name** (e.g. "termiphone-dev", shown in the
      tmux status bar) — currently falls back to cwd basename ("qs-app") for a
      generic-named window, ignoring the session name the user deliberately set.
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
- [ ] **Turn-timeline view** — a history of the agent's long turns, e.g.
      "✻ Churned for 12m13s", "✳ Cultivated for 8m02s" — one entry per turn with its
      verb, duration, and (if available) tokens. Distinct from the snapshot strip: this
      is a durable log of completed turns, so you can scroll back and see "it spent 12m
      on X, then 8m on Y." Requires detecting turn boundaries (working-line appears →
      disappears) in the watcher and recording {verb, start, end, tokens}.

## Milestone 2 — all panes
- [~] Label = window NAME (done in tmux.py); fan-out still single-pane.
- [ ] Fan out to ALL tmux windows/panes (user has ~11). API/state already list-shaped → watcher change only.

## Non-goals for PoC
- Push notifications; auth (LAN/tunnel only, do NOT expose); PNG snapshots; tmux control-mode.
