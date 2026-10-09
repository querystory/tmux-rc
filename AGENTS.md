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
- To show the user a preview (mock, build, report), put it in a subfolder of the
  `/scratch/` dir rather than starting a server or tunnel; see `docs/deploy/previews.md`.

## Develop in worktrees, off main

Each change gets a clean worktree cut from `origin/main`
(`git worktree add -b <branch> .claude/worktrees/<name> origin/main`). Stacking PRs on a
non-main base has repeatedly caused merge pain (features silently reverted when the base
squash-merges) — prefer branching off main and merging main in, over stacking.

## Never kill the user's tmux server

Agents run inside the user's tmux, so every `tmux` command inherits `$TMUX` and targets
the user's live server. `$TMUX` beats `TMUX_TMPDIR`, so setting `TMUX_TMPDIR` alone
isolates nothing. On 2026-09-30 an agent cleaning up a throwaway demo ran
`TMUX_TMPDIR=... tmux kill-server`; it hit the user's main server and 47 panes (Claude,
Codex, shells) had to be restored from transcripts.

- Never run `tmux kill-server` or `tmux kill-session`, for any reason. Close a throwaway
  window or pane only by the exact id you created.
- A test server isolates every command, cleanup included, with `env -u TMUX tmux -L <unique-socket>`
  or an explicit `-S <path>` (as `tests/test_capture_viewport.py` and `tests/test_live_history.py` do).
- A user-level Claude Code hook blocks the unsafe forms, but the rule holds where it isn't installed.

## Review before merge

No PR merges with unaddressed review comments. Drive Copilot review to clean (resolve
every thread, re-request, repeat) before merging. Merge only on the user's explicit word.

## UI iconography: no emoji — inline Lucide icons

Never use emoji glyphs (🎙 ⌨ 📎 ☀ …) as UI chrome (buttons, badges, menus, toggles).
Emoji render as platform-colored bitmaps: they ignore `currentColor` so they can't
theme (glaring since light mode), they clash with the chrome, and they look different
on every device.

Instead, use the inline Lucide icons in `web/m/app.js`: the `LUCIDE` path map +
`licon(name, size)` helper emit stroke-`currentColor` SVGs that theme for free and
render identically everywhere. To add an
icon, copy its path data from lucide.dev into the map — **inline only, never a CDN or
external fetch** (the app stays self-contained behind IAP). Static buttons ship empty
in `web/m/index.html` and get their icon injected at boot.

Emoji in *content* (terminal captures, transcripts, pane text) is data, not chrome —
pass it through untouched.

## UI screenshots: the demo fleet, never the live one

Screenshots of the live daemon show real session names, paths, PR numbers and costs, and
the README carried exactly that until it was regenerated. Every screenshot that leaves
this machine — README, docs, a PR body — comes from the demo fleet instead
(`scripts/demo_fleet.py`): an invented organisation with every pane state the UI draws, a
seeded month of chart history, and a browser clock started at one fixed instant.

It is served by `scripts/demo_server.py`, which is the daemon's own FastAPI app with the
watcher, history database and tmux seam swapped out, rather than a separate mock of the
API. That is deliberate: a hand-written mock drifts from the real routes and silently
stops showing what users see, while this one breaks loudly when a route changes shape.
Mutations answer `{"ok": true}` without running, and tmux is unplugged, so it can never
reach a real pane.

- `make screenshots` shoots the set in `scripts/screenshots.mjs` into `.screenshots/` and
  refreshes the README images in `docs/img/`. Only those few are committed; the rest exist
  to be diffed, and committing them would make every UI PR churn binary files.
- `make screenshots-diff` (default `BASE=origin/main`) shoots the base and this tree with
  the same fleet and writes per-shot diff images plus `summary.md`, whose image table
  links the PNGs relative to itself. A pixel counts as changed only if no pixel next to it
  in the other shot explains it, so antialiasing jitter doesn't register; a shot under
  0.05% is marked as noise and counts as unchanged.
- **CI posts that diff on every PR touching `web/`** (`.github/workflows/screenshots.yml`):
  one comment, updated in place on each push, with the changed-% table and base/head/diff
  images of the shots that moved. The images live on the orphan `ci-screenshots` branch,
  linked by commit; fork PRs get the table and the artifact but no comment, since their
  token cannot push. A changed shot never fails the build, a shot that fails to render does.
  The comment is not the review: read it, and **say in the PR body which changes are
  intended**, so an unintended one stands out to the reviewer.
- Same tree, same machine gives the same bytes. Across machines fonts differ, so compare
  shots from one machine; that is why the diff tool, and CI, shoot both sides in one run.
- `tests/test_demo_fleet.py` fails on anything in the fleet that looks private (home
  paths, real project names, emails, tokens). Add new demo data freely, but keep it
  fictional and keep URLs under `example-org`.

## Tests

`make test` runs `pytest -q tests/`. There is no JS test harness — browser-side logic is
verified by hand against real DOM shapes and by live testing on the phone.

## Python lint

`make lint` runs ruff over the whole tree; `make fmt` is the same rules with the safe
fixes applied. Both must be clean before a PR goes up. The ruleset lives in
`pyproject.toml` and is shared with our other Python codebase so one style covers both.

It is `select = ["ALL"]` plus a curated ignore list, rather than a short opt-in list, so
that a rule ruff adds later shows up and gets an explicit decision instead of silently
never running. Every entry in that list carries its reason on the same line; if you turn
one off, say why there.

Two conventions worth knowing before you hit them:

- **No mid-file imports in `openbus/`** (PLC0415). An import inside a function is a real
  decision — deferring a slow or optional dependency — so it needs a `# noqa: PLC0415`,
  and the reason has to be findable: either on the noqa itself or in the surrounding
  comment or docstring. The three that exist today defer `google.genai` (~0.9s to import,
  measured — that would otherwise land on daemon startup), the optional opentelemetry
  stack, and `uvicorn` in `main()`. An import that is merely far from the top of the file
  is a bug: hoist it. Test and script files are exempt, so a single case can keep its
  import next to the code that needs it.
- **`ruff format` is not used**, and `make fmt` does not run it. The hand-aligned constant
  tables and short guard ladders in this repo are deliberate, and reflowing them would
  bury real diffs under whitespace churn.

Complexity rules (C901, PLR0911/0912/0915) are currently off: seven functions exceed them
today — the two watcher ticks, `render_png`, `tmux._mark_dim`, two in `live.py`, and the
docs-site link checker — and that refactor belongs in its own PR rather than inside a lint
change. Issue #205 has the full inventory; turn the rules back on when it lands.

## Classifier / prompt changes

Any change to `openbus/parser_prompt.txt` or the classifier logic (`openbus/classify.py`)
MUST be validated with the prompt-eval harness and MUST add or update a case in
`research/eval/samples/` covering the behavior being fixed or changed — a prompt/classifier
fix without a matching eval case is incomplete. Run `python -m research.eval` (all samples
must pass); for a prompt edit, A/B it with `python -m research.eval --prompt <candidate>`
to confirm the fix without regressing other cases. See `research/eval/README.md` for the
scoring model, adding a sample, and `--model`/`--prompt`.
