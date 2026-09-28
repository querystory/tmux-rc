# agent-history

A small, greppable index of coding-agent sessions, so a person or a router such as
Live Mode can find past work across projects and resume it. It is independent of the
tmux-rc daemon: it indexes sessions that never ran in a pane (subagents, IDE sessions,
headless runs), and runs whether or not the daemon is up.

Status: Claude Code and Codex sessions are indexed. OpenCode readers, summaries and
`resolve` follow.

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
`started`, `last_active`, `prs`, `resume`, `messages`. Empty fields are omitted. The body
is the human's messages, one `## <timestamp> · <how it was sent>` section each; a
subagent's body is the task its parent gave it.

`prs` is an array of canonical GitHub pull URLs. Each URL contains the repository and
number without requiring GitHub access, and the shape leaves room for a later dashboard
to fetch live review/merge state. A session can list several PRs; the same PR can appear
in several sessions. Claude's explicit `pr-link` records are authoritative. Codex tracks
pull URLs in its rollout (including `gh pr create` output), plus explicit human shorthand
such as `PR 4955` when the session's local `origin` identifies a GitHub repository.

Entries are derived: deleting the index and running `reconcile` rebuilds it from any
transcripts that still exist. When a harness deletes a transcript, its entry is kept and
marked `source_missing: true` — it can no longer be grepped for detail or resumed, but
what the human said is not lost. Retention is otherwise the harness's setting.

## Running it

`go build -o ~/.local/bin/agent-history .` then register `agent-history hook` for each
harness. For Claude Code, use its `Stop`, `SessionEnd` and `SubagentStop` hooks. For Codex,
add a `Stop` command hook to `~/.codex/hooks.json` (or the equivalent `config.toml`):

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "~/.local/bin/agent-history hook"}]}]
  }
}
```

Codex requires reviewing and trusting a new local hook from `/hooks`; see the official
[Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).

Both harnesses send `transcript_path` on standard input. The hook hands that transcript
to a detached child and returns in milliseconds. `agent-history reconcile` repairs
whatever hooks missed (hard reboot, killed session, hooks not yet installed); hooks run
one themselves when the last is more than six hours old, so no timer is needed.
