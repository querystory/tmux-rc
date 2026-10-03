# agent-history

A small, greppable index of coding-agent sessions, so a person or a router such as
Live Mode can find past work across projects and resume it. It is independent of the
tmux-rc daemon: it indexes sessions that never ran in a pane (subagents, IDE sessions,
headless runs), and runs whether or not the daemon is up.

Status: Claude Code, Codex and omp. An OpenCode reader and summaries follow.

## Why an index and not a copy

Harness transcripts are mostly tool output: in a measured sample, what the human typed
was about 1% of the bytes and assistant prose under 3%. Search over the raw files is
already fast (`rg` over every transcript here takes tens of milliseconds); what it lacks
is identity, ranking and a record that survives the harness. So the index keeps the
small part that matters — where the work happened, what the human asked and decided,
PR links, how to resume — and points at the transcript for everything else. Nothing is
copied, and harness directories are only ever read.

The index is plain files rather than a database so the same `rg` searches it and the
transcripts, and a person or agent can read an entry without tooling.

## Memory is pulled, never pushed

Nothing here injects context into a session. The hook writes only to the index and
prints nothing. The index is read deliberately — by you, or by tmux-rc when routing a
request — because stale or wrong history silently fed to an agent is worse than none.

## Index format (the contract with consumers)

`~/agent-history/index/<harness>/<session>.md`, with subagents at
`<harness>/<parent-session>/<agent>.md` (`$AGENT_HISTORY_DIR` overrides the root).

Each entry is front matter of `key: value` lines whose values are JSON, which keeps it
valid YAML and one greppable line per field: `harness`, `session_id`, `parent_session`,
`source`, `source_missing`, `cwd`, `branches`, `entrypoint` (the harness's own word:
Claude's `sdk-cli` and Codex's `exec` are headless), `title` (the user's name for the session over the generated one),
`started`, `last_active`, `prs`, `resume_argv` (the command to run in `cwd`, for programs,
which must never go through a shell), `resume` (the same as a quoted line to paste), `messages`. Empty fields are omitted. The body
is the human's messages, one `## <timestamp> · <how it was sent>` section each; a
subagent's body is the task its parent gave it.

Entries are derived: deleting the index and running `reconcile` rebuilds it from any
transcripts that still exist. When a harness deletes a transcript, its entry is kept and
marked `source_missing: true` — it can no longer be grepped for detail or resumed, but
what the human said is not lost. Retention is otherwise the harness's setting.

## Running it

`go build -o ~/.local/bin/agent-history .` then register `agent-history hook` for Claude
Code's `Stop`, `SessionEnd` and `SubagentStop` hooks. The hook hands the transcript to a
detached child and returns in milliseconds. `agent-history reconcile` repairs whatever
hooks missed (hard reboot, killed session, hooks not yet installed); hooks run one
themselves when the last is more than six hours old, so no timer is needed.

Codex and omp have no such hook here, so their sessions (Codex's `~/.codex/sessions`,
or `$CODEX_HOME`; omp's locations below)
arrive with reconcile, which `resolve` also starts in the background when one is due.
No search waits for it, so a search after a long idle period answers from the index as
it was and starts the refresh; a later search sees the result.

An entry is one Codex thread, not one file: resuming a thread continues it in a new
rollout file under the same ID, so its files are read together and the entry is fresh
while none is newer. Its title is the thread name Codex keeps in `session_index.jsonl`;
renaming an idle thread touches no rollout, so the rename's time dates the entry as
well. The source is the thread's latest file, the one Codex's retention deletes last.
Work a thread delegates (`thread_spawn`) is indexed under it like a Claude subagent,
named by its task's path, since Codex stores the task itself encrypted. Approval
reviews (`guardian`) are left out: their task is a copy of the parent's transcript.

omp's JSONL is authoritative; its databases, tool logs, images and result sidecars
are not human history. The reader keeps user messages except those marked synthetic
or agent-attributed, since `role: user` also carries injected notifications. Missing
attribution remains eligible for older sessions. Subagents use their first
`session_init.task`, not the system prompt or its duplicate agent-attributed message.
Artifact location identifies a subagent: `parentSession` alone also marks ordinary forks.
A sibling transcript supplies the canonical parent ID; timestamped artifact folders
also identify direct orphans. Registered custom-file/breadcrumb pointers supply
artifact-tree boundaries even when a non-timestamped parent is missing or truncated.
The explicit flat session root is also a boundary: direct files are main sessions,
while nested transcripts remain artifacts even without a readable parent.
Boundaries and candidates use resolved filesystem paths, including missing paths'
existing prefixes, so symlink aliases cannot hide artifacts from live FD checks.
Original pointer paths are retained separately for resume argv.
An artifact without a resolvable canonical immediate parent is skipped, rather than
inventing a parent or offering a main-session resume. Previously promoted cached entries are
rejected by `get` and skipped by `resolve`, including entries already marked missing
or lacking resume argv: artifact classification is independent of resumability.
Cached children also require their current canonical parent to match the recorded
parent: deleted/truncated parents and changed relationships invalidate the entry
even under `-all`; reconcile can rebuild a valid changed relationship.

The rewritten title slot is current, including an explicit cleared title; slot-less
files use the header title: title-change records are audit, not the current name.
Identity, cwd and start time also come from the header, never the lossy slug. All dated records and
title updates count as activity, but only kept messages supply PR links. omp does not
persist Git branches or print/RPC launch mode, so those fields are omitted rather than
invented; `-all` cannot distinguish its historical headless runs.

Discovery includes `~/.omp/agent/sessions`, named profiles under
`~/.omp/profiles/<name>/agent`, `$PI_CODING_AGENT_DIR`, and the flat
`$PI_CODING_AGENT_SESSION_DIR` used by `--session-dir`. `$PI_CONFIG_DIR` changes the
`.omp` directory name. Profiles (`--profile`, `$OMP_PROFILE`, then `$PI_PROFILE`)
isolate storage and ignore the agent-dir override. Existing omp roots under
`$XDG_DATA_HOME` / `$XDG_STATE_HOME` are also supported; merely setting XDG variables
does not migrate storage. Exact custom-file registry and terminal breadcrumb pointers
find relocated transcripts without searching unrelated files. Older omp versions
without that registry cannot rediscover an unknown `--session-dir` after its breadcrumb
is overwritten: set `PI_CODING_AGENT_SESSION_DIR` for reconcile in that case.
Managed session/artifact trees are scanned recursively, including nested agents.

Ordinary omp entries resume with `omp --resume <id>` in their recorded cwd. Relocated
entries use the absolute transcript path to bypass ID lookup; named-profile entries
also preserve `--profile`, so a resume does not silently change profile configuration.
An explicit `PI_CODING_AGENT_SESSION_DIR` with no root/registry profile marker inherits
the active `OMP_PROFILE`/`PI_PROFILE`. Paths tied to the default root or registry
explicitly select `--profile default` when reconcile runs under a named profile.
The effective profile schedules an immediate omp rebuild. Cached-entry reads also
resolve argv against the current profile, effective/legacy roots and registry pointers,
so placement changes for that cached source affect the first result. Each
resolve/reconcile shares one fresh, lazily loaded placement snapshot across its entries
and discovery, avoiding repeated profile/registry scans; `get` gets a fresh snapshot.
Ordinary reconcile checks placement and requires the cached source to match the
discovered source before trusting an unchanged transcript mtime. A move or registry
pointer relocation therefore rebuilds source/resume metadata even when the transcript
keeps its mtime. The check reads only the entry header rather than routinely rescanning
transcripts. Format 6 rebuilds current artifact classification; obsolete
per-entry profile-only context is absent, and consumer JSON has no new fields.

## Resolving a request

`agent-history resolve [-json] [-harness claude|codex|omp] [-all] <query>` answers "where does
this belong?" for a request like "fix live mode": the likeliest repos and, in each, the
sessions to resume. It ranks indexed metadata and human messages, checks current storage
placement and liveness, and calls no model.

Scoring is deliberately simple. Each query word and adjacent word pair is weighted by
how rare it is across sessions, so "fix" barely counts and "live mode" decides; the
session's title, branches and PR links count more than passing mentions; hits saturate
so one long session can't bury the rest; and weight halves every two weeks of
inactivity. A repo ranks by its best three sessions, not its volume, and git worktrees
fold into their main repo. Headless runs and subagents are left out unless `-all`.
Paraphrase ("the voice thing" for Live Mode) is out of reach by design until real misses
justify aliases or embeddings.

Each session also says whether it is `running` now, and in which tmux pane, so a caller
can send to the live agent instead of resuming a second copy onto the same transcript.
This comes from the registry Claude Code keeps of its live processes
(`~/.claude/sessions/<pid>.json`); an entry only counts while its pid is alive with the
start time it registered, since the files outlive crashes and pids get reused.

Codex keeps no registry, but a running Codex holds its thread's rollout open, so the
open files of the user's `codex` processes say which threads are live — proof that
needs no start time check. Only codex processes count: an editor or `tail -f` on a
rollout is not the session, and neither are the sandbox helpers that run tool commands
under the name `codex`. The process's `TMUX_PANE` says where it runs.

That pane is usually missing. New Codex sessions run through a shared app-server
daemon, which holds the rollout of every thread its terminal clients show, while the
clients hold none; the daemon has left every terminal, so it reports no pane. tmux-rc
fills the gap from the screen when it can: a Codex status bar configured to show
`session-id` (see docs/agent-setup.md) shows the thread ID, so Live treats the one watched Codex pane whose
status bar shows it as the thread's pane. Thread names aren't used for this, since two
live threads can share one. Otherwise the thread counts as running out of reach, and
Live still refuses to start a second copy.

omp uses a live current-user conversation process plus its terminal breadcrumb.
The breadcrumb follows session switches, unlike launch argv, and survives exit for
`--continue`, so it is never live evidence alone. Internal workers, maintenance commands,
and exited unreaped processes (zombies) do not count.
Positive open-transcript evidence also covers headless hosts,
but descriptors are lazy: without an authoritative breadcrumb, zero or multiple
main-session descriptors make omp liveness unknown and publish no candidates.
Ambiguous terminal ownership does the same. Subagents suppress breadcrumbs
and are not reported as independent live sessions.
Live classification uses the host's own effective profile/storage environment and
registered artifact roots, so a subagent fd cannot stand in for a missing main transcript.
Relative agent/session directory overrides resolve against the inspected host's cwd,
not the history command's cwd.
Bun hosts whose best-effort OS rename did not take effect are also detected, but
only for an omp CLI script (including the installed `omp` symlink), not unrelated
Bun programs. Node is not an omp runtime: its CLI requires Bun.

The process's CLI/environment profile selects the breadcrumb root.
CLI profile parsing stops at `--`: option-like text in the prompt does not select a
different profile's breadcrumb.
Later in-process `.env` relocation may be invisible in Linux's initial `/proc` environment; no alternate
profile's stale breadcrumb is guessed as a fallback. The process's `TMUX_PANE` is used
only while it still has a controlling terminal.
Breadcrumb environment fallbacks also require a controlling terminal. A TTY stdin
still wins directly; detached headless hosts ignore inherited terminal metadata.

If a harness can't tell what is running, only its own sessions are marked
`running_unknown`; the other harnesses' answers stand.

`agent-history get <session-id>` prints one session in the same JSON shape, for a caller
that already chose it; the ID must be a plain name, so it can't reach outside the index.
