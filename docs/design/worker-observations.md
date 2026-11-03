# Worker status interface for custom harnesses

Set `TMUXRC_WORKER_SNAPSHOT` to a private local JSON file to include any custom harness's
complete worker inventory in activity charts. Unset, no file is read and pane collection
uses its existing behavior. The source owns the full roster of each registered controller;
its workers replace that controller's screen-derived subagent counts.

The writer must atomically replace the file on lifecycle changes and every ten seconds
after successfully checking current state. A heartbeat is an affirmation of the roster,
not evidence that every worker is running. Example (all names are invented):

```json
{
  "version": 1,
  "source_id": "garden",
  "run_id": "run-1",
  "sequence": 1,
  "observed_at": 1793707200,
  "controllers": [{"pane_id": "%1", "birth": "123", "server": "boot:server"}],
  "workers": [{"id": "prune", "controller_pane_id": "%1", "tool": "garden-runner",
               "activity": "waiting", "waiting_on": "external"}]
}
```

`observed_at` is Unix seconds. Increase `sequence` for every replacement, including
heartbeats. Source and run IDs distinguish producer incarnations. Controller bindings
must use the current tmux server identity and pane birth discriminator; a pane number
alone is insufficient. The daemon checks these against its inventory before attribution.
A worker may provide `pane` with the same `{pane_id, birth, server}` shape when that
worker actually occupies its own foreground pane. This explicit local producer assertion
must not be copied from an inherited `TMUX_PANE`: headless children inherit their parent's
pane. A matching binding prevents counting the worker twice. A controller's session is
used for grouping; the worker's `tool` supplies its harness group.

Activities are `running`, `idle`, `waiting`, `compacting`, `quota_blocked`, `unknown`,
and `done`. Waiting requires `waiting_on` equal to `user` or `external`. Done workers
are omitted from current counts. A fresh complete roster can omit finished workers.
A valid empty roster records zero workers. Reports older than thirty seconds become
Unknown. Missing/malformed files retain the last accepted roster with its original
observation time; the source is unavailable until a valid read, and stale once that
observation expires. Unknown workers remain until a fresh roster removes them, their
controller lifetime ends, or the source is disabled. Restarting the daemon does not
refresh timestamps from disk. Before any complete snapshot has been accepted, combined
fleet history is unmeasured (a gap), while pane-only history and independently observed
session groups remain available. A missing initial file never erases another session's
parsed worker counts. Once a controller scope is known, expired empty inventories make
only those sessions' combined history unmeasured, including tool filters with no matching
workers. A stale roster consisting solely of workers already counted in panes likewise
cannot establish zero background workers. Known stale background workers retain their
Unknown states. Fresh empty snapshots restore measured zero; a daemon restart preserves
the timestamp-based expiry.

Snapshots over one MiB, duplicate identities, conflicting pane bindings, invalid states,
future timestamps beyond five seconds, and regressing sequences are rejected as a whole.
The last accepted snapshot is held in memory, not copied into parser caches. The source
status is separate from worker state: a transient file error can show unavailable while
its last accepted observations are still within their freshness window.

Screen parsing and terminal actions remain independent. Fresh reported workers survive
an unread controller screen. Panes-only charts retain their existing semantics; combined
charts add workers without verified foreground panes. This measures observed activity,
not CPU, tokens, or monetary usage. Existing history is not retroactively classified.

## Producer contract

The interface is a versioned JSON file. A producer can be written in any language and
needs no tmux-rc package, provider SDK, or registered harness plugin. The harness owns
translation from its internal lifecycle to the activity values above; tmux-rc owns
validation, freshness, counting, and presentation.

| Field | Contract |
| --- | --- |
| `version` | `1` for this format. |
| `source_id` | Nonempty string naming this producer. |
| `run_id` | Nonempty string identifying this producer incarnation; use a new value when it restarts. |
| `sequence` | Nonnegative integer, increasing for every replacement within an incarnation. |
| `observed_at` | Unix seconds when the complete roster was checked, including fractional seconds if desired. |
| `controllers` | Complete list of controller bindings, each with string `pane_id`, `birth`, and `server`. |
| `workers` | Complete current roster; each row needs a unique nonempty string `id`, registered `controller_pane_id`, nonempty string `tool`, and `activity`. |
| Worker `waiting_on` | Required for `waiting`: `user` or `external`. |
| Worker `pane` | Optional explicit foreground binding with the same shape as a controller binding. |

`tool` is an open identifier: `garden-runner` in the example is an invented custom
harness, not a built-in agent. Custom identifiers are counted and offered as chart
filters without adding them to tmux-rc's terminal-parser tool list. Prefer a stable
identifier; `unknown` and `other` are reserved display buckets without their own filters.
A controller may be a plain shell pane. This interface reports its workers independently
of how its screen is classified. One configured file can report several controllers;
its writer must combine their rosters before replacing the file.

## Binding a controller

The current binding uses Linux's boot ID, the tmux server PID, and the pane's PID.
Read these from the same tmux server that tmux-rc observes:

```sh
cat /proc/sys/kernel/random/boot_id
tmux display-message -p '#{pid}'
tmux display-message -p -t '%3' '#{pane_id} #{pane_pid}'
```

Replace `%3` with the explicit controller pane. Set `server` to the boot ID followed
by `:` and the server PID, `pane_id` to the returned pane ID, and `birth` to the pane
PID as a string. For another tmux socket, use the same explicit `-S` socket path on
every command. Fail the read if any identity is unavailable. Check the identity before
each publication; stop reporting under that binding when the controller disappears or
its identity changes. Do not automatically attach old workers to a reused pane ID.

A producer can run separately from its controller, under a process supervisor. In this
version, workers need a valid controller pane for attribution; fully pane-free harnesses
are outside the interface's current scope.

## Publishing and enabling

On each lifecycle change and at least every ten seconds, read the authoritative complete
roster, map its states, and create a snapshot. Do not advance `observed_at` after a failed
read or merely because the producer process is alive. Update `sequence` even for a
heartbeat whose roster did not change. A successful empty inventory writes `workers: []`;
a failed read leaves the last snapshot untouched.

Write JSON to a temporary file in the destination directory with mode `0600`, flush it,
and atomically rename it over the configured path. Use a private directory outside the
repository. Readers must never see a partially written file. Include only structural
identity and activity; terminal text, prompts, credentials, and task descriptions are
unnecessary for this interface.

Set the daemon environment to the producer's output path and restart tmux-rc when ready:

```sh
TMUXRC_WORKER_SNAPSHOT=/private/path/workers.json
```

The dashboard reports source freshness. Verify a running worker appears in the combined
chart, an explicit completion removes it, and stopping the producer makes its observations
stale after thirty seconds. Panes-only counts should remain the same throughout. Unset
the setting and restart to disable collection. Nothing launches or controls the producer
on behalf of the harness.

History databases gain a schema version marker for worker inventories. Older readers
refuse the newer database rather than misreading it. Keep a database backup before
upgrading if rollback to an older release is needed.
