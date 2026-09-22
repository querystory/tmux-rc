# Persistent pane history

The atlas reads `/api/history?window=1h|24h|7d|all`. The daemon records structural
inventories in SQLite whenever identity, session, tool, or displayed state changes,
and at least once per minute while healthy. Removing a pane or closing the whole
fleet therefore has an explicit observation. Browser visits do not affect collection.

The database defaults to `$XDG_STATE_HOME/tmux-rc/history.sqlite3` (normally
`~/.local/state/tmux-rc/history.sqlite3`); `TMUXRC_HISTORY_DB` overrides it. Keep it
outside checkouts, in a private directory (mode `0700`); shared override directories
are rejected rather than chmodded. The database and WAL/SHM sidecars use `0600`. It is local to one host, contains no terminal text or summaries,
and uses WAL with short transactions. All history is retained; copy the database
using SQLite's backup API, or stop its writer before copying the database and WAL.
A second collector should use its own database if it observes a different fleet.

The query returns at most roughly 361 buckets. Each bucket represents the latest
inventory at its end, rather than counting parse events or adding every pane seen
throughout the bucket. Exact observations carry for at most 120 seconds; longer
collection outages are gaps, not idle time. The newest bucket is provisional.
Smaller time ranges use smaller buckets. State folding matches the mobile UI:
external waiting and compacting count as running, user waiting as needs-you.

## Backfilling older local logs

Older `/tmp/tmux-rc-llm.log` entries contain state but omit pane identity. The systemd
journal contains some watcher messages with both a pane ID and the exact first 80
characters of a re-emitted event. The importer joins those records within two seconds,
accepting only a unique match. It rejects other hosts, boots, services, and records
before the supplied tmux server start. **It cannot reconstruct every pane or state
change.** It never treats the model's `session` title as a tmux session name.

Export the live service's journal for the current tmux server lifetime:

```sh
journalctl --user -u tmux-rc --since '@<server-start-epoch>' -o json --no-pager > /tmp/tmux-journal.jsonl
uv run python -m scripts.backfill_history --trace /tmp/tmux-rc-llm.log \
  --journal /tmp/tmux-journal.jsonl --server-uid '<boot-id>:<tmux-server-pid>' \
  --since <server-start-epoch> --lifetimes /tmp/pane-lifetimes.json --dry-run
```

Verify the report, then omit `--dry-run` to import; rerunning is idempotent. The server
PID comes from `tmux display-message -p '#{pid}'`; its process creation time and
`/proc/sys/kernel/random/boot_id` identify the correct lifetime. Don't use the daemon's
PID or start time: restarting the daemon does not restart tmux.

The lifetime JSON is an array of `{server, pane_id, birth, start, end}` objects, with
Unix-second start/end bounds and a unique birth discriminator (PID plus process
start time). Supply independently verified lifecycle evidence, such as the creation
time of a still-running pane process. An observation must fall wholly within exactly
one interval. A server lifetime alone does not prove which generation occupied a pane
ID; missing or overlapping intervals are rejected. Do not infer lifetimes from the
state logs themselves. Closed panes without lifecycle evidence cannot be backfilled.

Reconstructed counts carry each matched state for at most four hours and never beyond
its verified lifetime end. They are partial estimates, shown as lighter bars with an
explanation. They have tool identity but **unknown historical tmux session**; selecting
a real session excludes these records. Importing never overwrites live snapshots,
and estimated states never fill an outage after exact collection has begun. The
SQLite backfill stores only time, pane birth identity, tool, folded state, and the
verified lifetime end. Legacy imports without lifetime evidence remain on disk but
are excluded from queries; re-import them with verified intervals to restore coverage.
