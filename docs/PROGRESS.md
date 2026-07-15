# tmux-rc — Progress Log

Running log of what changed, newest first. Commit after meaningful changes.

## 2026-07-14 (session: tmux-rc-dev)

- Added Rewind history picker (Esc-Esc): scrollable entry list on the card, ❯-selected
  highlight, +N/-N diff-stat notes (green/red), ↑/↓ move cursor + Enter restores.
  Label now prefers user-chosen window/session name, falls back to cwd basename
  (rejects tmux defaults incl. numeric "0"). Attach images via clipboard+Ctrl-V (needs
  wl-clipboard on Wayland). Fixed input-clearing on re-render, no-store headers + SW
  self-destruct so the phone stops serving stale assets.
- **PAUSED — architecture reckoning.** The classifier is now a pile of regexes that
  hard-code Claude Code's exact UI text. This is fragile (breaks on any UI/status-line
  change or a different agent) and defeats the reason we chose an LLM. Next step is a
  redesign: make the LLM the parser (structured JSON from pane text/screenshot),
  regexes only as a hot-loop fast-path. See the ARCHITECTURE block in REQUIREMENTS.md.

- **Fixed card oscillation on static multiple-choice prompts** (options ⇄ blank input).
  Debugged with data: LLM is deterministic on identical text (5/5); the real cause was
  the watcher reclassifying every tick because a "static" Claude screen still drifts
  its status line (cost/counters), and the heuristic can't parse boxed prompts while
  the LLM can → alternation. Fix: `_fingerprint()` ignores volatile metrics; reuse
  cached PaneState when fingerprint unchanged; idle LLM pass gated on fingerprint (≤1
  per screen). Verified by watcher sim (8 ticks → 1 call) + user on live UI.
- Attach-images (phone→agent) requirement captured; NOT built yet (paused to fix bug).

- Card redesign (single-pane quality): status_line leads as a wrapping task summary;
  working subline (verb·elapsed·tokens); metadata chip row (model · ctx% · cost ·
  mode badge · agent count). Added PaneState.agents. VERIFIED live: tool sticky as
  claude, real task summary, model/ctx/cost/mode all populated. User confirmed logo
  fixed + oscillation stopped.

- Wrote REQUIREMENTS.md + this PROGRESS.md after user (rightly) called out that I'd
  been churning without capturing requirements or committing. Process fix: update both
  files and commit after each meaningful change from here on.
- Set Gemini model to **gemini-3.1-flash-lite** (was 2.5-flash-lite — outdated).
- Added LLM trace logging → `/tmp/tmux-rc-llm.log` (grep to debug misclassification).
- `_detect_tool` now sniffs agent signatures (✳/context left/bypass/shift+tab) so a
  pane shelling out doesn't immediately read as a subprocess — stickiness in watcher
  still TODO. UNVERIFIED.
- Tightened `_detect_question` heuristic to reduce false "waiting" on transcript prose.
  LLM prompt still needs tightening. UNVERIFIED.
- Added rich status extraction (model/ctx/cost/mode/working verb/elapsed/tokens) via
  the LLM pass; proactive pass on idle screens. `PaneState` extended.
- Added always-on raw input row + special keys (Enter/Esc/arrows/Ctrl-C) to every card.
- Claude logo: TWO attempts (bezier, then rotated-rect sunburst) — STILL not rendering
  correctly per user. Needs definitive fix + browser verification.

### Milestone 1 (earlier) — DONE + verified
- tmux wrappers, heuristics+lazy-LLM classifier, watcher, FastAPI server, PWA.
- End-to-end verified: real Claude Code model picker detected as waiting, tapped an
  option on the phone, agent proceeded.
