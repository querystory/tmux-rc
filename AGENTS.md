# AGENTS.md — working conventions for tmux-rc

Conventions for AI agents (and humans) working in this repo. The philosophy notes in the
harness/session prompt still apply (minimize LOC, DRY, KISS, why-focused docs); this file
captures repo-specific workflow that isn't obvious from the code.

## Testing changes on the live dev instance

**The dev daemon always runs `make dev` from the REPO ROOT, in
tmux pane `%8`. To test a branch on the live phone instance, `git checkout <branch>` in
the root and restart the daemon — never `cd` the daemon into a worktree.**

- The daemon loads `web/`, prompts, etc. relative to its cwd. Running from the stable root
  means a later `git worktree remove` can never delete the cwd out from under it (that
  strands the process → `FileNotFoundError` on `parser_prompt.txt`, site returns
  `{"detail":"Not Found"}`, and even `cd`/`make` fail with "getcwd: No such file").
- **To make branch X live:** from root, `git checkout X`, then in pane %8:
  `C-c`, then `make dev > out.log 2>&1`. StatReload restarts on `.py` edits; for `web/`
  changes just reload the page (assets are served no-store).
- A branch checked out in a `.claude/worktrees/*` worktree can't also be checked out in
  root — `git worktree remove` it first (after pushing), then checkout in root.
- The live instance is at `<slug>.<your-tunnel-domain>` (via the tunnel client in the
  `2:tunnel` window). A momentary "no tunnel connected" is the relay's ~1h connection
  cap; the client auto-reconnects within ~1min.

## Develop in worktrees, off main

Each change gets a clean worktree cut from `origin/main`
(`git worktree add -b <branch> .claude/worktrees/<name> origin/main`). Stacking PRs on a
non-main base has repeatedly caused merge pain (features silently reverted when the base
squash-merges) — prefer branching off main and merging main in, over stacking.

## Review before merge

No PR merges with unaddressed review comments. Drive Copilot review to clean (resolve
every thread, re-request, repeat) before merging. Merge only on the user's explicit word.

## UI iconography: no emoji — inline Lucide icons

Never use emoji glyphs (🎙 ⌨ 📎 ☀ …) as UI chrome (buttons, badges, menus, toggles).
Emoji render as platform-colored bitmaps: they ignore `currentColor` so they can't
theme (glaring since light mode), they clash with the chrome, and they look different
on every device.

Instead, use the inline Lucide icons in `web/app.js`: the `LUCIDE` path map +
`licon(name, size)` helper emit stroke-`currentColor` SVGs that theme for free and
render identically everywhere (same approach as the ⤢ fullscreen button). To add an
icon, copy its path data from lucide.dev into the map — **inline only, never a CDN or
external fetch** (the app stays self-contained behind IAP). Static buttons ship empty
in `index.html` and get their icon injected at boot.

Emoji in *content* (terminal captures, transcripts, pane text) is data, not chrome —
pass it through untouched.

## Tests

`make test` runs `pytest -q tests/`. There is no JS test harness — browser-side logic is
verified by hand against real DOM shapes and by live testing on the phone.

## Classifier / prompt changes

Any change to `daemon/parser_prompt.txt` or the classifier logic (`daemon/classify.py`)
MUST be validated with the prompt-eval harness and MUST add or update a case in
`research/eval/samples/` covering the behavior being fixed or changed — a prompt/classifier
fix without a matching eval case is incomplete. Run `python -m research.eval` (all samples
must pass); for a prompt edit, A/B it with `python -m research.eval --prompt <candidate>`
to confirm the fix without regressing other cases. See `research/eval/README.md` for the
scoring model, adding a sample, and `--model`/`--prompt`.
