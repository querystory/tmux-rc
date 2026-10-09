---
weight: 3
title: "Previewing work"
---

# Previewing work over the tunnel

Mocks, reports and built sites need to be opened on the phone, often while an agent is
still producing them. The daemon can serve them itself, behind the front door you already
have, so nothing new needs to be authenticated, supervised or remembered.

## What `/scratch/` is

Point `TMUXRC_SCRATCH_DIR` at a directory and the daemon serves its files, read-only, at
`https://<your-host>/scratch/`. A folder `report/` in it is `https://<your-host>/scratch/report/`
(its `index.html`, if it has one). It sits behind the same tunnel and login as the app, so
whoever can open tmux-rc can open it, and nobody else.

It is for anything that is a pile of static files, not just tmux-rc work:

- an HTML mock or design study an agent just wrote;
- the build output of a static site (a docs site, a landing page, a single-page app's
  `dist/`) for another project;
- a generated report, a chart, a screenshot set, a PDF.

## Enabling it

Set the variable in the `.env` the daemon loads (the repo root's, for the systemd unit)
and restart:

    TMUXRC_SCRATCH_DIR=/home/you/.local/share/tmux-rc/scratch

then `systemctl --user restart tmux-rc`. The directory must exist when the daemon starts;
files added afterwards show up without another restart. Keep it outside every checkout, so
no commit can pick up a preview and no worktree cleanup can delete one. Each preview gets
its own subfolder, and deleting the folder unpublishes it.

## Telling agents about it

Agents only use scratch if they know it exists. Set `TMUXRC_SCRATCH_ADVERTISE=1` (and
`TMUXRC_SCRATCH_URL` to the public base, such as `https://your-host/scratch`, since the
daemon cannot know the hostname your tunnel gives it) and the daemon exports both variables
to the tmux server's global environment at startup. Every pane opened after that inherits
them; shells that were already running do not. Each restart re-applies the setting, so a
variable that is no longer configured is removed rather than left pointing at nothing.
Turning the flag off stops the writes but leaves the last values in place until tmux
restarts or you run `tmux set-environment -gu` on them.

It is off by default because it writes to your tmux server's environment, which you may
manage yourself. The variables only say where scratch is. To have agents use it, add a line
like this to your `CLAUDE.md` or `AGENTS.md`:

    If TMUXRC_SCRATCH_DIR is set, put previews for the user in a subfolder there and
    share $TMUXRC_SCRATCH_URL/<folder>/ (or the folder's path, if that URL is unset).
    Never put secrets or private data there. Make pages self-contained: their scripts
    can't fetch data or post forms.

## What it exposes, and what it won't run

**Everything in the directory is published to everyone the tunnel admits.** That is the
whole access model: there is no per-file permission and no listing to hide behind a
guessable name. Never put secrets, credentials or private data there. For the same reason
there is no default directory: an unset variable means nothing is served, so nothing gets
published because someone saved a file in the wrong place.

The mount does not list directories, and requests cannot climb out of it through `..` or
symlinks.

**Pages are sandboxed.** A preview lives on the daemon's own hostname, where a script
could otherwise call the daemon's API, which types into your terminals, using your
session. So every `/scratch/` response carries a Content-Security-Policy that:

- runs the page's scripts in an opaque origin, so the daemon's API is as foreign to them as
  it is to any other website, and the page has no cookies or `localStorage`;
- sets `connect-src 'none'`: scripts get no `fetch`, XHR, WebSocket or EventSource, to
  anywhere;
- sets `form-action 'none'`: no form submissions. A request whose answer the page cannot
  read would still carry your session, so blind writes are refused too.

Ordinary resource loads are left alone: `<img>`, `<link>` and `<script src>` still fetch,
from this host or another, because that is how a static bundle loads its own files, and
the daemon's `GET` routes only read. Those reads go out without the page being able to
see the answer.

The consequence is simple: **a self-contained static bundle works; anything that needs to
call a backend or fetch remote data from script does not.** Plain HTML, CSS, images, PDFs
and classic scripts with their data inlined are fine. A page that fetches JSON, calls an API, or loads module
scripts (which are fetched as CORS requests) will not work. Build those with their data
baked in, or give them their own hostname (below).

## Why it is not a proxy

`/scratch/` serves files; it does not forward to a dev server on another port. A proxy
would put an arbitrary local app, with its own unauthenticated API, on the daemon's origin
and behind its login, where none of the sandboxing above can apply. Dev servers also tend
to need WebSockets for hot reload and absolute paths at the site root, so a path-prefixed
proxy would half-work in confusing ways. Build the site and drop the output in scratch, or
use a separate tunnel client.

## Mocks worth keeping are committed instead

A mock that explains a PR should outlive the scratch dir. Put it under
`docs-site/static/mocks/`: the docs build copies `static/` verbatim, so it is served at
`/docs/mocks/<name>.html` after `make docs`, travels with the PR that it explains, and
survives every rebuild, under the same sandbox as scratch: a mock written by an agent does
not gain the daemon's API by being committed. Copying a file straight into the built docs
dir looks like it works, then vanishes on the next `make docs`.

## If a preview needs its own hostname

Sometimes a preview really needs more than scratch allows: a whole site with absolute
links, one that needs a backend, or one shared without the daemon. Then it needs its own
tunnel: a separate client instance with **its own configuration** (its own hostname and
local port), so it cannot end up starting a second copy of the daemon's tunnel. How you
give it a name and a port depends on your client; for Cloudflare that is a second named
tunnel, not a second run of the [daemon's wrapper](cloudflare-tunnel.md). Gate the new
hostname with the same kind of login as the daemon's: nothing else protects it.

Whatever the client, give it **non-interactive credentials**, the same kind the tunnel
service uses (a service account, a tunnel token), not your interactive login. A client that
falls back to developer login credentials works for a day and then dies on the
organization's reauthentication policy, and an unattended preview has nobody to notice.
Run it under the user systemd manager so it is restarted and logged:

    systemd-run --user --unit=preview-<name> -p Restart=always \
      -p EnvironmentFile=<that preview's own env file> <the client command for it>

Stop it with `systemctl --user stop preview-<name>` when the review is done. The
[design note](../design/scratch-previews.md) has the reasoning behind these choices.
