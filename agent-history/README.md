# agent-history

A small, greppable index of coding-agent sessions, so a person or a router such as
Live Mode can find past work across projects and resume it. It is independent of the
tmux-rc daemon: it indexes sessions that never ran in a pane (subagents, IDE sessions,
headless runs), and runs whether or not the daemon is up.

Status: Claude Code only. Codex and OpenCode readers and summaries follow.

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
`source`, `source_missing`, `cwd`, `branches`, `entrypoint` (`cli` is interactive,
`sdk-cli` is headless), `title` (the user's name for the session over the generated one),
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

## Resolving a request

`agent-history resolve [-json] [-harness claude] [-all] <query>` answers "where does
this belong?" for a request like "fix live mode": the likeliest repos and, in each, the
sessions to resume. It reads only the index, takes milliseconds, and calls no model.

Scoring is deliberately simple. Each query word and adjacent word pair is weighted by
how rare it is across sessions, so "fix" barely counts and "live mode" decides; the
session's title, branches and PR links count more than passing mentions; hits saturate
so one long session can't bury the rest; and weight halves every two weeks of
inactivity. A repo ranks by its best three sessions, not its volume, and git worktrees
fold into their main repo. Headless runs and subagents are left out unless `-all`.
Paraphrase ("the voice thing" for Live Mode) is out of reach by design until real misses
justify aliases or embeddings.
