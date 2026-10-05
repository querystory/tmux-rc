---
weight: 3
title: "Previewing work"
---

# Previewing work over the tunnel

Mocks, design studies and built sites under review need to be opened on the phone. The
tempting move is a second tunnel per preview: a quick static server plus another tunnel
client. Don't — serve them through the daemon's own front door, which is already
authenticated, already supervised, and already on the phone.

## Two places, by lifetime

**Mocks that inform a PR are committed** under `docs-site/static/mocks/`. The docs build
copies `static/` verbatim, so a mock there is served at `/docs/mocks/<name>.html` once
`make docs` runs, travels with the PR that it explains, and survives every rebuild. (Older
studies under `static/design/` work the same way.) Copying a file straight into the built
docs dir instead looks like it works, then vanishes on the next `make docs`.

**Throwaway previews go in a scratch dir** named by `TMUXRC_SCRATCH_DIR`; the daemon serves
it read-only at `/scratch/`. Use it for anything that is not this repo's to publish (a
landing page for another project, a work-in-progress build) or that nobody will want next
week. Keep it outside every checkout so no commit can pick it up and no worktree cleanup
can delete it — e.g. `~/.local/share/tmux-rc/scratch`.

Why scratch is opt-in, with no default path: everything in it is served to whoever the
front door admits. An unset variable means nothing is published by accident. The mount is
a plain static mount — no directory listings, and lookups cannot climb out of the directory
— and the directory is checked at startup, so create it before restarting the daemon.
Files added later appear without a restart.

Scratch pages are also sandboxed into an opaque origin. They sit on the daemon's own
hostname, so without that a script in a preview could call the daemon's API, which types
into your terminals, with your session. Forms and script requests are blocked too: a
request whose answer the page can't read still carries your session. The cost is that a
preview cannot use `localStorage`, make requests from script, submit forms, or load
module scripts. Static pages and classic scripts work fine. Anything that needs more
belongs in a committed mock or on its own hostname.

## If you do run a second tunnel client

Sometimes a preview really needs its own hostname (it is a whole site with absolute links,
or it must be shared without the daemon). Then give the extra client **the same
credentials the tunnel service uses**, not your interactive login. A client that falls
back to developer login credentials works for a day and then dies on the organization's
reauthentication policy — and an unattended preview has nobody to notice. The service's
env file already holds a self-refreshing credential, so point the extra client at that
file and override only what differs (its name and port), run under the user systemd
manager so it is restarted and logged:

    systemd-run --user --unit=preview-<name> -p Restart=always \
      -p EnvironmentFile=$HOME/.config/tmux-rc/tunnel.env \
      ~/.local/bin/tunnel-client --slug <name> --port <port>

Stop it with `systemctl --user stop preview-<name>` when the review is done.
