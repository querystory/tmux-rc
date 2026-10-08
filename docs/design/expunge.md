# Expunge a Session

Status: implemented (`openbus/expunge.py`, the pane's ⋯ menu).

## Why

"Kill window" ends a process but leaves everything the agent wrote behind: the
transcript, its subagents, file snapshots, the prompt history, and agent-history's
index of what the human typed. Sometimes a session should not survive at all, such as
one that saw a secret or one the user simply wants gone. Expunge is that action: it kills
the window, then deletes that one session's local files.

## The caveat, stated up front

Expunge is local. A session's content may also exist where this machine cannot reach:
spans and logs already exported through OpenTelemetry, and whatever Anthropic or OpenAI
keep on their side under their own retention. The confirmation says so in plain words,
because a button called "Expunge" otherwise implies a guarantee it cannot give.

## Exactly one session, found by its id

The design rule is that Expunge only deletes what is named by the session's id. It never
deletes by date, project directory, or a broad glob. Deleting a project directory would
take every other session in that repo with it, and a mistake here cannot be undone.
So the hard part is the identity, not the deletion.

- **Claude Code** registers each running session as `sessions/<pid>.json` under its
  config dir. The daemon walks the pane's process tree and takes the agent whose
  registration matches its pid *and* kernel start time, so a stale file left by a
  reused pid never counts. A registration that can't be read refuses the whole
  expunge, since it could be this pane's own session. The config dir comes from that
  process's own `CLAUDE_CONFIG_DIR`, not the daemon's, so a pane running a second
  profile is handled correctly.
- **Codex** keeps no registry, and its shared app-server, not the terminal client,
  holds the rollout open, so no process proves which thread a pane shows. The one
  reliable signal is the thread id on the status line, which is the same evidence the
  Live resume path already trusts (`live._codex_pane`). A UUID-shaped segment counts
  only if a rollout under `CODEX_HOME` is named for it, so another segment that happens
  to look like an id is never taken for the thread. Without `session-id` on the status
  line, Expunge refuses and says to add it.

The walk stops at the first agent down each branch, so an agent's own subprocesses
(a headless `claude -p` it ran) are not a second session. Two agents side by side, no
agent, a Codex screen showing zero or two thread ids, or an id that isn't a UUID: each
refuses with the reason. None of them falls back to anything broader. The UUID check
also means an id can never smuggle a path separator or glob character into a pattern.

The confirmation is bound to the id the preview showed. If the pane is running a
different session by the time the user confirms, the request is refused.

## What is deleted

Claude, under the agent's config dir: the `projects/*/<id>.jsonl` transcript and its
sibling `<id>/` (subagents, tool results), `file-history/<id>`, `session-env/<id>`,
`tasks/<id>`, `todos/<id>-*` and `debug/<id>.txt`, plus the session's lines in
`history.jsonl`.

Codex, under `CODEX_HOME`: the `sessions/…/rollout-*-<id>[_<segment>].jsonl` files (a
resumed thread has several), `archived_sessions/`, `shell_snapshots/<id>.*`, the thread's
and lines in `history.jsonl` and `session_index.jsonl`.

agent-history: the session's index entry and its subagents' entries, under the agent's own
`AGENT_HISTORY_DIR` (or the default). That index exists to outlive the harness's own
retention, so leaving it would defeat the point.

tmux-rc itself: the pane's checkpoint row (its card, summary and recent events), which
would otherwise stay on disk until the next restart prunes it. A tombstone stops
any tick in flight from writing it back. If the database is busy, the deletion is
retried every tick, and the response reports the failure instead of claiming success.
The structural pane history (counts and states per minute) holds no session content and
is left alone.

## Order and safety

1. Identify the session and resolve every target while the agent is still alive.
   If any path, through a symlink, resolves outside its root, refuse before
   anything is killed.
2. Kill the window, using the same path as "Kill window", but only while the pane
   still belongs to the process that was identified. tmux reuses pane ids, and a
   recycled `%N` must never take a newer window down with it. The tmux server makes
   that check and the kill in one command (`if-shell -F` on `pane_pid`), so the
   id can't change hands between the two.
3. Wait for the agent process to exit. If it has not exited after a few seconds, delete
   nothing, since a live agent would only write the files again.
4. Resolve the targets again and delete them. Shared logs are rewritten through a temp
   file and an atomic rename, keeping their mode. A line that names the id but can't be
   parsed, such as one a crash cut short, is dropped as well. It is already corrupt, and
   keeping it could keep this session's words. The result reports counts and
   basenames for the UI, never contents.

The audit line records the pane and the session id, and nothing else.

## Not covered

- Other panes in the same window are killed with it, as with "Kill window". Their
  sessions are not expunged.
- omp, Gemini and other harnesses: there is no reliable pane-to-session mapping yet,
  so they refuse rather than guess.
- Codex threads the session spawned have their own ids and are left in place. Only
  their agent-history entries go, since those sit under the parent.
- Codex's SQLite stores (`state_*.sqlite`, `thread_history_*.sqlite`, `logs_*.sqlite`)
  keep their own copy of the thread's items, and the confirmation says so for Codex.
  tmux-rc deliberately doesn't write to them. They belong to Codex's app-server, which
  outlives the client and has a schema that changes between versions, and
  `codex-native-context.md` already rules out a second writable owner of that state.
  Removing those rows is Codex's job, through its own interface.
- The harnesses append to their shared logs without a lock. Expunges are serialized
  within the daemon, and a rewrite the log outgrew while it was being written is thrown
  away and redone, so a concurrent append survives. One gap remains: a writer that keeps
  the old file open, rather than opening it for each append, would write past the rename.
- Claude's hook-driven agent-history indexer can race the deletion and rewrite the
  entry. Deleting the transcript first and the index entry last keeps that window small.
- Codex's shared app-server, not the closed client, holds the thread. If it writes to
  the thread again after the deletion, a fragment can reappear.
- OpenTelemetry exports, provider-side retention, backups, and copies in shell
  scrollback or other tools are out of reach.
