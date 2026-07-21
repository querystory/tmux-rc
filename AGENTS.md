# AGENTS.md — working conventions for tmux-rc

Conventions for AI agents (and humans) working in this repo. The philosophy notes in the
harness/session prompt still apply (minimize LOC, DRY, KISS, why-focused docs); this file
captures repo-specific workflow that isn't obvious from the code.

## Testing changes on the live dev instance

**The dev daemon always runs `make dev` from the REPO ROOT (`~/src/qs/termiphone`), in
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
- The live instance is reached through a tunnel/reverse proxy of your choice (see
  SETUP.md) — the client runs in the `2:tunnel` window. A momentary "no tunnel connected"
  is the relay's connection cap; the client auto-reconnects within ~1min.

## Develop in worktrees, off main

Each change gets a clean worktree cut from `origin/main`
(`git worktree add -b <branch> .claude/worktrees/<name> origin/main`). Stacking PRs on a
non-main base has repeatedly caused merge pain (features silently reverted when the base
squash-merges) — prefer branching off main and merging main in, over stacking.

## Review before merge

No PR merges with unaddressed review comments. Drive Copilot review to clean (resolve
every thread, re-request, repeat) before merging. Merge only on the user's explicit word.

## Tests

`make test` runs `pytest -q tests/`. There is no JS test harness — browser-side logic is
verified by hand against real DOM shapes and by live testing on the phone.
