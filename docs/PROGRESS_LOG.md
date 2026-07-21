# tmux-rc — Progress Log

Running log of what happened, newest at top. Paired with `REQUIREMENTS.md` (what to
build) — this is the "what I did / what broke" narrative. Committed as we go.

## 2026-07-14 (dogfooding session — building tmux-rc from inside tmux-rc)

- **Docs discipline restored.** User (rightly) called out that I was churning on code
  without keeping `REQUIREMENTS.md` + this log current and without committing. Fixing
  now: write docs → commit → then fix bugs, committing each fix.
- **Model: switched Gemini 2.5 Flash Lite → 3.1 Flash Lite** (`gemini-3.1-flash-lite`,
  env override `TMUXRC_GEMINI_MODEL`). 2.5 was stale; I'd blindly copied it from
  another project's config.
- **LLM trace logging added.** Every classify call appends IN (tail sent) + OUT
  (parsed JSON) to `/tmp/tmux-rc-llm.log` (override `TMUXRC_LLM_LOG`). Grep it
  to see what the model actually saw/returned.
- **Auto-reload fixed & confirmed.** `make dev` runs uvicorn with reload ON by default
  (`TMUXRC_RELOAD=0` to disable). Verified: editing a source file logs
  "StatReload detected changes… Reloading" and spawns a fresh worker. Earlier confusion
  was because the running instance had been started by hand without reload.
- **Conservative question detection (heuristic).** A scrolling transcript with prose
  ending "?" or containing "1./2." lists was being read as a live prompt. Rewrote
  `_detect_question`: numbered menu must reach the bottom and have >=2 items; y/N only
  on the last line; removed the bare-"?" branch. STILL SEEING false positives — the
  LLM also hallucinates questions from bulleted reasoning (open, see requirements).
- **Rich status extraction via LLM.** Instead of regex-picking one line (which grabbed
  the vim `-- INSERT --` footer), the LLM now returns structured JSON: status_line,
  activity, model, context_pct, cost, mode, working_verb, elapsed, tokens. Noise filter
  strips vim/tmux chrome from the heuristic path too.
- **Working indicator captured**: verb ("Cultivating"/"Orbiting"), elapsed, tokens.

- **False-positive questions fixed (both paths).** Heuristic: menu must reach bottom
  OR carry a `❯` cursor; y/N only on last line; bare-"?" removed. LLM: prompt now
  forbids inventing a question from prose/lists. Regression test added (8 tests pass).
- **Shell-awareness + task-summary** added to LLM prompt (describe running command /
  summarize what's worked on, not just the spinner verb).
- **Logo root cause was the service worker** caching stale app.js. SW now caches
  nothing + drops old caches; real claude.png used; /api/version drives client auto-
  reload on asset change. May need one hard refresh to evict the old SW.
- **Tool oscillation fixed**: agent signatures keep a pane 'claude' when it shells out.
- **Label** uses tmux window name, not "0:0".

### KNOWN BUGS (open)

1. **Tool oscillates claude ⇄ shell.** When Claude Code shells out, the pane's
   foreground command becomes bash/git/grep → `_detect_tool` returns "shell". Attempted
   fix: agent signatures in transcript + (still needed) sticky per-pane tool in the
   watcher so it never downgrades a known agent. NOT yet verified working.
2. **Claude logo still not showing** the real mark. Tried an emoji cloud, then a bezier
   path (rendered as a blob), then rotated-rects sunburst. User points to
   https://claude.ai/favicon.ico as the real asset — should just use that.
3. **LLM invents questions** from transcript lists/bullets → false "waiting".
4. **status_line echoes the verb, not the task.** Should summarize WHAT is being
   worked on ("Building tmux-rc…"), not just "running Orbiting…".

### Prior (overnight / M1)

- Milestone 1 built and verified end-to-end: watch pane → classify → PWA card → detect
  waiting prompt → tap option → send-keys round-trips → agent proceeds. Committed with
  PRD + DESIGN docs and 6 classifier tests. See git log.
