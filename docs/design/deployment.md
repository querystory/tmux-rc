# Deployment: always-on tmux-rc

Status: **proposed**. Companion to [durable-vertex-auth.md](durable-vertex-auth.md) (which
closed the credential half of "runs unattended"); this closes the process half.

## Problem

tmux-rc is three long-running processes — the **daemon** (watcher + API), the
**tunnel-client** (connects the phone through the IAP relay), and historically a **local
otel receiver** — and today every one of them lives or dies by whether a particular
terminal happens to stay open. This has produced a steady drip of real incidents, all
with the same root cause and different costumes:

- The daemon ran from a worktree that was later deleted under it (PWA 404s, "Not Found").
- The daemon was an orphaned `setsid` process nothing would restart; killing it required
  archaeology to prove nothing owned it.
- The tunnel-client died silently twice in one day ("no tunnel connected"), once because
  its binary lived in `/tmp` — which is wiped on reboot — and once for no logged reason
  at all. Recovery each time was a human noticing the phone was dead.
- Which code the daemon runs depends on which checkout a shell happened to `cd` into.

None of these are bugs in the programs. They are the predictable cost of running services
as shell jobs. The system's own design docs already reached this conclusion twice (the
tunnel moved to SA credentials for the same reason; durable-vertex-auth.md flags "systemd
unit" as its step 6) — this doc runs it to ground.

## What should tmux-rc watch? (architecture before deployment)

The deployment shape depends on the monitoring scope, so settle that first.

**Multiple tmux *sessions*: already supported, by construction.** The watcher enumerates
`list-panes -a` — every pane of every session and window on the server. A user with 14
sessions is watching 14 sessions today. No work here.

**Multiple tmux *servers* (sockets): explicitly out of scope.** `tmux -L work` creates a
second server invisible to the daemon. Supporting it would mean keying the watcher's
per-pane stores and the HTTP API by (socket, pane_id) — bare `%3` exists on every server —
a cross-cutting refactor for a configuration almost no one runs. The telemetry identity
(`pane_uid = boot_id:server_pid:pane_id`) already disambiguates servers, so this door
stays open without any code paid for now. If a real multi-server need appears, that is
the moment to pay.

**Start at boot, idle until tmux appears: yes.** The daemon already degrades gracefully
when no tmux server exists (empty pane list, keeps polling), so boot-start needs no
architectural change — the daemon simply becomes ambient infrastructure, like the shell
itself. Two latent bugs block this today and are prerequisites:

1. **`server_uid()` caches failure.** It is `@cache`d; started before tmux, it would
   stamp `boot_id:0` on every record forever. Same class of bug if the tmux server
   restarts mid-daemon: the cached pid goes stale and new panes inherit a dead server's
   identity. Fix: cache only successful reads, and re-derive when tmux's server pid
   changes. (Cheap: the watcher already calls tmux every tick; noticing a pid change is
   one comparison.)
2. **Binaries in `/tmp`.** The tunnel-client currently runs from `/tmp/tunnel-client`,
   erased on reboot. Anything a unit starts must live in a real path (`~/.local/bin`).

## Deployment shape

### Chosen: `systemd --user` units + linger

Two units — `tmux-rc.service` (daemon) and `tmux-rc-tunnel.service` (tunnel-client) —
grouped under a `tmux-rc.target`, with `loginctl enable-linger` so they start at boot
without a login session.

Why this shape:

- **Restart policy is the whole point.** `Restart=always` with backoff converts every
  failure mode we've hit (silent death, OOM, kill, reboot) from "phone is dead until a
  human notices" into a seconds-long blip. The tunnel-client already survives the relay's
  hourly Cloud Run WebSocket timeout by reconnecting; systemd extends that resilience to
  the process itself.
- **User units, not system units.** The daemon must run *as the user*: it reads the
  user's tmux socket, the user's SA key, and pastes into the user's clipboard
  (wl-copy/xclip need the user's Wayland/X session). A system unit would have to
  impersonate all of that; a user unit gets it for free. Linger covers the at-boot
  requirement.
- **Configuration stays where it is.** The daemon already loads the repo-root `.env`
  itself, so the unit is thin: `WorkingDirectory=` the checkout, run the venv's python,
  `TMUXRC_RELOAD=0`. No second copy of the config to drift. The checkout *is* the deploy
  — `git pull` + `systemctl --user restart tmux-rc` is the whole upgrade story, which
  matches how this repo actually develops (branch checkouts on root for live testing).
- **Logs go to journald** (`journalctl --user -fu tmux-rc`). This deliberately replaces
  "watch the pane." The pane workflow stays available — stop the unit, run the daemon
  manually in a pane while iterating, start the unit again — the two modes share the
  same command and env, so there is no behavioral fork. The single-line operational
  logging and AUDIT lines were designed for exactly this: readable in a journal, not
  just a scrollback.

### Rejected alternatives

- **Keep `setsid`/pane jobs (status quo).** This doc's problem statement is the rejection.
- **System-level systemd units.** Wrong user context (tmux socket, clipboard, SA key);
  gains nothing over user units with linger.
- **Docker/containers.** The daemon's entire job is touching host state — the tmux
  socket, the clipboard, the display session. A container would need all of those
  mounted through, making it strictly more fragile than a user unit for zero isolation
  benefit; and the tool is inherently single-host, single-user.
- **Supervising from within (daemon spawns/monitors the tunnel-client).** Tempting DRY —
  one unit instead of two — but couples two programs with independent lifecycles (a
  tunnel binary upgrade shouldn't bounce the watcher and reset its buffers) and
  reimplements restart logic systemd already does better.
- **cron `@reboot` + respawn loops.** Strictly worse systemd: no dependency ordering, no
  journal, no `systemctl status`.

### The tunnel-client half

The client binary is built from `qsi-automation/tunnel` but *deployed here*, on the
workstation. Division of responsibility:

- **This repo** owns the unit file and documents the install path (`~/.local/bin/
  tunnel-client`) and flags (`--slug`, `--port`, `--owner`).
- **qsi-automation** owns the relay (Cloud Run) and the client's code, including two
  known improvements that are relay/client-side, not deployment-side: forwarding the
  IAP identity for the audit log (its PR #525), and optionally make-before-break
  reconnects to hide the hourly Cloud Run WS timeout (today: a ~2s blip per hour, which
  the UI can dampen client-side by tolerating a couple of failed polls before declaring
  the backend down).

### The local otel receiver: retire it

The third process solves itself: the daemon now points at the Cloud Run receiver, which
is durable, SA-authenticated, and shared with Claude Code telemetry. The local bench
receiver (and its SA key) should be shut down once the per-source prefix routing lands
in qsi-automation — no unit needed for a process that shouldn't exist.

## Failure modes after this change

| Failure | Today | With units |
|---|---|---|
| Process dies silently | Dead until a human notices the phone is stale | Restarted in seconds, visible in `systemctl status` |
| Host reboots | Everything gone; binaries in /tmp erased | Everything back up at boot, no login needed |
| tmux not running yet | (same) daemon idles correctly | (same), pending the `server_uid` cache fix |
| Relay's hourly WS timeout | ~2s blip, client reconnects | Unchanged (client-level concern) |
| Bad deploy (broken code on the checkout) | Daemon crashes in the pane | Unit flaps; `systemctl status` shows it; roll back the checkout |

## Implementation sketch

1. Fix `server_uid()` caching (cache success only; re-derive on tmux server pid change).
2. Install the tunnel-client binary to `~/.local/bin` (built from qsi-automation).
3. Add `deploy/systemd/tmux-rc.service`, `tmux-rc-tunnel.service`, `tmux-rc.target` to
   this repo + a short `make install-units` (copy to `~/.config/systemd/user/`,
   `daemon-reload`, `enable --now`, `loginctl enable-linger`).
4. Retire the local otel receiver + its SA key once collector routing lands.
5. README: replace the "run it in a terminal" instructions with the unit workflow, keep
   the manual-pane mode documented for development.
