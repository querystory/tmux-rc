# Activity Clocks That Survive a Restart

Status: proposed, not yet implemented.

## Problem

The phone uses two per-pane clocks. **Last updated** sorts by `last_activity_at`, the
last time the pane's screen really changed. The **Recent** filter uses `state_since`,
the time the pane entered its current state. Both live only in daemon memory.

After a restart the watcher has to guess each pane's clocks. The guess (added for the
"restart amnesia" bug, #129) is tmux's `#{window_activity}`: the last time any byte
was written to the window. It is better than "now", but it is not the same as a
content change. tmux updates it for output our fingerprint deliberately ignores:
spinners, timers, and above all status-bar metrics.

The worst case is Codex. Its footer shows the account-wide weekly usage limit
("weekly 42% left"). That number is shared by every Codex process on the account.
When it ticks, every idle Codex pane redraws its footer at the same moment, and tmux
stamps every one of those windows as active. The fingerprint strips that text, so
while the daemon runs nothing happens. The next restart then reads `window_activity`,
and days-old conversations jump to the top of Last updated and back into Recent.

We restart often. Every PR merged into the integration checkout restarts the
daemon. So in practice the ordering is wrong most of the time. One observed restart
seeded twelve Codex panes, idle for days, inside a 34-second window: the moment the
weekly limit ticked, three minutes before the restart.

A second, smaller leak shows up without a restart. The fingerprint covers the whole
capture: a fixed window of 200 scrollback rows plus the visible screen. When an agent
pushes rows into scrollback without adding visible content (Codex redrawing its inline
viewport is the observed case), the top of that window moves. The fingerprint then
changes and the pane is marked active with nothing new on screen. We saw this on an
idle pane whose only differences between two snapshots were 14 rows scrolling off the
top and the (ignored) weekly percentage.

## Goals

- A restart must not change any pane's clocks unless its content actually changed.
- A pane whose content changed while the daemon was down should look recent. We can't
  know exactly when it changed, and erring toward "recent" there is fine.
- Scrollback moving at the top edge is not activity.
- Losing or corrupting the stored state must never make things worse than today.

## Design

### Save what the watcher already knows, keyed by a pane identity that survives restarts

The daemon already keeps a SQLite database for pane history (see
[pane-history.md](pane-history.md)). It already builds a pane key that is stable
across daemon restarts: tmux server id, pane id, and the pane's process id. The
process id matters. tmux reuses pane ids like `%40` after its own server restarts, and
a key on the bare id would hand an old pane's timestamp to an unrelated new one. With
the pid in the key, a reused id is simply a new row.

We add one small table to that database: one row per pane, holding a hash of the
pane's current fingerprint and the time that fingerprint first appeared. The watcher
writes a row only when a pane's screen really changes, which is the same moment it
already updates `last_activity_at`. That is a few writes a minute across the fleet,
not one per tick.

### One seeding rule for both clocks

On a pane's first sighting after startup, the watcher looks up its row. If the stored
hash matches the current screen, nothing changed while the daemon was down, so the
stored time is the pane's real last activity. That value becomes the seed wherever
the code now uses `window_activity`: for `last_activity_at`, and for `state_since` on a
pane that is already idle. If the hash differs, or there is no row, or the database
can't be read, the watcher falls back to `window_activity` exactly as today.

One seed feeds both clocks on purpose. For an idle pane, the last content change is
effectively the moment it went idle: the turn ended and the screen stopped changing.
Storing a separate `state_since` would mean storing the activity/question key it's
derived from. That key includes question text, and the history database promises to
contain no terminal text.

The stored value is a hash, not text. The history database's "no terminal text" rule
still holds in spirit. A hash of a low-entropy screen could in principle be matched
by guessing, but the file is already private to the user (0600 in a 0700 directory),
so this adds no meaningful exposure.

### Fingerprint the visible screen, not the scrollback window

The fingerprint should cover only the pane's visible rows. Everything that decides
activity, questions, or state is on screen. The scrollback rows above it matter to
the parser as context, but a change in which rows fall inside a fixed-size window is
not a change in the pane. This also keeps stored hashes valid across a restart. A
screen that sits still while scrollback shifts still hashes the same, so a restart
can still reuse its stored time.

The parser still receives the full capture. Only the change-detection signature
narrows.

### Pruning

Rows for panes that no longer exist stop being updated. On startup we delete rows not
written in 30 days. That keeps the table small without tracking pane deaths, and a
pane idle for more than 30 days loses nothing: it falls back to `window_activity`,
which for such a pane is also old.

## Alternatives considered

**A separate JSON state file.** Simplest to write, but it would need its own private-
directory checks, atomic replace, and concurrent-reader story. The history database
already solves all of those and already has the restart-stable pane key. A second
store means two sources of truth for pane identity. Rejected.

**Storing the timestamp in the history inventory payloads.** The history table saves
space by writing a new payload only when the fleet's structure changes. A
per-pane timestamp would make almost every tick a new payload and defeat that. The
checkpoint is current state, not history, so it gets its own table. Rejected.

**Persisting the whole in-memory pane state, or the snapshot ring.** That would
restore more than the clocks: cached parses, events, summaries. But it's far more
data, it stores terminal text, and it changes what a restart means, including cache
invalidation of parses made by an older parser. Much more than this bug needs.
Rejected. It may be worth its own design later.

**Seeding unknown panes as "oldest" instead of `window_activity`.** Needs no storage.
But a pane that really was active just before a deploy would drop out of Recent after
every merge. That's the same bug in the opposite direction, and a worse one, since
losing a live pane is costlier than seeing a stale one. Rejected.

**Filtering footer redraws out of tmux's activity signal.** tmux only reports one
per-window timestamp; there's no way to ask it "when did non-status content last
change". We would have to reconstruct that from captures, which is what the
fingerprint already does. Rejected.

**Fingerprinting only the bottom N rows.** A fixed N covers the visible screen on
some panes and not others, and it breaks when a pane is resized. The pane's own
height is the natural boundary. Rejected in favor of the visible screen.

**Versioning the fingerprint hash.** A deploy that changes the fingerprint's
normalization makes every stored hash mismatch once, so that one restart falls back
to today's behavior. That's rare, self-healing, and never wrong in a harmful
direction, so a version column isn't worth it. Rejected.

## Failure modes

- **Database missing, locked, or corrupt:** seed from `window_activity`, as today.
  Writes use the history module's existing backoff, so a failing disk can't slow the
  watcher.
- **Screen changed while down:** hash mismatch, fall back to `window_activity`, and
  the pane shows as recent. That's correct.
- **Clock skew or a stored time in the future:** clamp to "now", as the current seed
  already does.
- **tmux server restarted:** every pid changes, so no rows match and seeding falls
  back to `window_activity`. That's right: every pane really is new.

## Testing

- **Restart with nothing changed:** the watcher tick tests simulate a restart against
  an unchanged screen whose tmux `window_activity` is newer than the stored time, and
  check that both clocks keep the stored value.
- **Changed screen or reused pane id:** the tests check that a different screen, and a
  recycled pane id with a new pid, fall back to `window_activity`.
- **Scrollback scrolling:** a fingerprint test checks that rows scrolling out of the
  top of the capture, with the visible screen unchanged, produce the same signature.
