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
- [ ] **Claude logo STILL not showing** — user asked 3×. Use the real mark. Reference: https://claude.ai/favicon.ico. My inline-SVG attempts failed. NEEDS a definitive fix + visual verification in the browser.
- [ ] Mode as icon/badge (plan / accept-edits / bypass / normal) like Claude web.
- [ ] Clean metadata row: model · ctx% bar · cost · mode (not a run-on string).
- [ ] Background-agent count per pane; collapsible.
- [ ] Don't waste horizontal space; lead with a human summary.

## Milestone 2 — all panes
- [ ] Fan out to ALL tmux windows/panes (user has ~11). API/state already list-shaped → watcher change only.
- [ ] Label = window NAME (e.g. "Resolve PR 38"), not "work:0".

## Non-goals for PoC
- Push notifications; auth (LAN/tunnel only, do NOT expose); PNG snapshots; tmux control-mode.
