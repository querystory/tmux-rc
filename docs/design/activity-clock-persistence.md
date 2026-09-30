# Activity Clocks That Survive a Restart

Status: implemented, extended from the two clocks to the whole card (see "Restoring
the card" below).

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
- A pane whose screen differs from when the daemon went down should look recent. We can't
  know exactly when it changed, and erring toward "recent" there is fine.
- Scrollback moving at the top edge is not activity.
- Losing or corrupting the stored state must never make things worse than today.

## Design

### Save what the watcher already knows, keyed by a pane identity that survives restarts

The daemon already keeps a SQLite database for pane history (see
[pane-history.md](pane-history.md)). It already builds a pane key that is stable
across daemon restarts: tmux server id (boot id plus server pid), pane id, and the
pane's process id. The
process id matters. tmux reuses pane ids like `%40` after its own server restarts, and
a key on the bare id would hand an old pane's timestamp to an unrelated new one. With
the pid in the key, a reused id is simply a new row.

We add one small table to that database: one row per pane, holding a hash of the
pane's current fingerprint, the time that fingerprint first appeared, and the time the
pane went idle (empty while it isn't idle). A row records facts about the pane, not
what some daemon noticed, so writes are conditional: a row is written only when the
stored hash differs from the current one or the pane has entered or left idle. A write
replaces the whole row with the clocks as the watcher holds them, so after a mismatch
it records the `window_activity` seeds, not the startup time or a stale idle time.
That is a few writes a minute across the fleet, not one per tick.

The condition matters even with one daemon. The watcher treats a pane's first tick
after startup as a screen change, so an unconditional write-on-change would stamp
every row with "now" on each restart: the bug again.

Memory stays the runtime source of truth. SQLite is a write-through checkpoint, read
once in bulk at startup into memory and never per pane or per tick, so a slow or
locked database costs at most one timeout and restart seeding. A crash between
a change and its write is harmless: the hash mismatches and seeding falls back to
`window_activity`, which is accurate for a pane that just changed.

### Seeding

On a pane's first sighting after startup, the watcher looks up its preloaded row. If
the stored hash matches the current screen, the stored times replace `window_activity` wherever
the code now seeds from it: `last_activity_at`, and `state_since` on a pane that is
already idle. If the hash differs, or there is no row, or the database can't be read,
the watcher falls back to `window_activity` exactly as today.

The two clocks need separate stored times. An idle pane's screen can change without
leaving idle (a shell command finishing between polls), and `state_since` deliberately
holds through that. Seeding it from the last content change would move it forward and
put an old idle pane back in Recent. Only idle panes are seeded, so the stored state
time needs no question text.

The stored value is a hash, not text. The history database's "no terminal text" rule
still holds in spirit. A hash of a low-entropy screen could in principle be matched
by guessing, but the file is already private to the user (0600 in a 0700 directory),
so this adds no meaningful exposure.

### Restoring the card

The clocks were the visible symptom, but a restart also threw away every card. Each
pane came back as "No recent activity", and the daemon re-paid one LLM parse plus one
scrollback deep read per pane (around forty of each) just to redraw screens that had
not changed. So the same row also carries the card: the last successful parse, the
tail of the activity log, and the bootstrap's summary and name.

On a hash match the card goes back into memory as though it had just been parsed and
bootstrapped, so the unchanged screen costs no LLM call and the summary refresh waits
its normal cadence. On a mismatch nothing is restored and the pane is read as today.
A card is only stored alongside the hash it was parsed from. After a failed parse the
watcher shows the previous card over a newer screen, so that row stores the clocks but
no card, and a restart re-reads the screen. Writes also happen when a new parse,
bootstrap summary or idle summary lands, which is still only a handful a minute.

This reverses the earlier rejection below of persisting pane state. The
concern there was size and terminal text. The stored card is the parser's output plus
a capped log tail, not the snapshot ring. Parser changes are covered by the hash: a
card is restored only for a screen whose normalized text is byte-identical.

### Fingerprint the visible screen, not the scrollback window

The fingerprint should cover only the pane's visible rows. Everything that decides
activity, questions, or state is on screen. The scrollback rows above it matter to
the parser as context, but a change in which rows fall inside a fixed-size window is
not a change in the pane. This also keeps stored hashes valid across a restart. A
screen that sits still while scrollback shifts still hashes the same, so a restart
can still reuse its stored time.

The parser still receives the full capture. Only the change-detection signature
narrows.

### Multiple daemons

Two tmux-rc copies on one host share the database by default, with no instance lock,
and copies watching the same tmux server compute identical pane keys. The conditional
write makes this safe: the first copy to see a change records it, the other's write is
a no-op, and they converge.

- Copies running different fingerprint code hash the same screen differently, so each
  restart of one copy mismatches the other's rows, falls back to `window_activity`,
  and re-stamps them. That is today's behavior, confined to a mixed-version setup.
  Accepted, as with versioning below.
- Two copies racing on one change can leave the older observation in the row. The
  loser's next tick sees the new screen and rewrites it, so the error lasts one poll.
- The existing inventory history has the same two-writer exposure. An instance lock or
  a per-instance database is the real fix and belongs in its own change.

### Pruning

After the first successful listing of every pane on the tmux server, we delete the
preloaded rows for that server whose panes are not in it. Limiting the delete to the
preload means a row another daemon inserted meanwhile can't be pruned. Preloaded rows
from other tmux servers go once nobody has written them for 30 days. Proving a
server gone (a different boot id, a dead pid) would be exact, but only the watching
daemon can see a live server's panes change, and a month of silence is a simpler test
that errs toward keeping rows. The listing must be the full one, not the
watch list, which `TMUXRC_TARGET` narrows to one pane. If tmux can't be listed,
nothing is pruned. Expiring rows by age instead would drop a long-idle live pane back
to `window_activity`, which the footer redraws above keep fresh: the very bug this
fixes.

## Alternatives considered

**Using SQLite as the live store.** Reading clocks from the database each tick would
put disk latency and lock contention on the path that feeds the UI, for no gain: memory
already holds the truth while the daemon runs. Rejected.

**A separate JSON state file.** Simplest to write, but it would need its own private-
directory checks, atomic replace, and concurrent-reader story. The history database
already solves all of those and already has the restart-stable pane key. A second
store means two sources of truth for pane identity. Rejected.

**Storing the timestamp in the history inventory payloads.** The history table saves
space by writing a new payload only when the fleet's structure changes. A
per-pane timestamp would make almost every tick a new payload and defeat that. The
checkpoint is current state, not history, so it gets its own table. Rejected.

**Persisting the whole in-memory pane state, or the snapshot ring.** Originally
rejected as more than the clock bug needed. The card half is now done (see "Restoring
the card"); the snapshot ring and the parser's per-pane bookkeeping stay in memory,
since they are raw terminal text and a restart rebuilds them within a tick.

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
  A failed write is not retried; the row stays stale until the pane's next real
  change, and a restart before then falls back to `window_activity`. Writes back off
  for a minute after a failure, as history recording does, so a failing disk can't
  slow the watcher.
- **Screen changed while down:** hash mismatch, fall back to `window_activity`, and
  the pane shows as recent. That's correct.
- **Screen changed and changed back while down** (a command run, then cleared): the
  hash matches and the pane keeps its old times. Nothing we can observe separates this
  from footer redraws, so we accept it.
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
