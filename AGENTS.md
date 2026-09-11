# AGENTS.md — working conventions for tmux-rc

Conventions for AI agents (and humans) working in this repo. The philosophy notes in the
harness/session prompt still apply (minimize LOC, DRY, KISS, why-focused docs); this file
captures repo-specific workflow that isn't obvious from the code.

## The repo root is the live deploy — and it is always the integration branch

The daemon is a systemd `--user` unit (`tmux-rc.service`, installed by `make install-units`)
whose `WorkingDirectory` is the repo root. The root is checked out **detached at
`origin/integration`**: `origin/main` plus **every open, non-draft PR** merged together.
That merge is what the phone runs, so several in-flight PRs get field-tested at once
without merging any of them prematurely, and two PRs that fight each other show up here
before either lands.

- **Never `git checkout` / `switch` / `reset` in the root to try one PR.** That silently
  drops every other PR under test. Try a single PR alone in its own worktree.
- **To make a PR (or a new push to one) live, rebuild integration:** in a worktree, reset
  the `integration` branch to `origin/main` and merge each open PR branch (`gh pr list`),
  most conflict-prone last. Its history is disposable — rebuild from main rather than piling
  merges on the old tip, or a PR that was dropped or reworked lingers in the merge. Resolve
  conflicts in the merge commit only, never on the PR branch. `make test`, force-push
  `integration`, then in the root `git checkout --detach origin/integration` and
  `systemctl --user restart tmux-rc` (the unit runs with reload off, so nothing applies
  until the restart). Leave a PR out only when its conflict is not mechanical, and say so —
  its author fixes it against main.
- **Integration is never merged to main.** PRs land one at a time, via review and the user's
  explicit go.
- **Never edit files in the root.** All changes go through worktrees cut from `origin/main`
  (below); the root's only writes are the detached checkout and `.env`.
- `.env` (gitignored) lives in the root. Provider API keys do **not**: they go in
  `~/.config/tmux-rc/openai.env`, outside every checkout, so no worktree or commit can
  carry one. Logs: `journalctl --user -fu tmux-rc`. `make dev` (StatReload) is for iterating
  in a worktree, never the root.
- The live instance is reached through the tunnel client (`tmux-rc-tunnel.service`). A
  momentary "no tunnel connected" is the relay's ~1h connection cap; the client reconnects
  within ~1min.

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

Any change to `openbus/parser_prompt.txt` or the classifier logic (`openbus/classify.py`)
MUST be validated with the prompt-eval harness and MUST add or update a case in
`research/eval/samples/` covering the behavior being fixed or changed — a prompt/classifier
fix without a matching eval case is incomplete. Run `python -m research.eval` (all samples
must pass); for a prompt edit, A/B it with `python -m research.eval --prompt <candidate>`
to confirm the fix without regressing other cases. See `research/eval/README.md` for the
scoring model, adding a sample, and `--model`/`--prompt`.
