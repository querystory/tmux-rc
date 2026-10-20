---
weight: 3
title: "Previewing work"
---

# Previewing work through tmux-rc

Mocks, reports and built sites need to be opened on the phone, often while an agent is
still producing them. The daemon can serve them itself, behind whatever front door you
already use to reach it (Tailscale, a tunnel, a reverse proxy, or just localhost), so
nothing new needs to be authenticated, supervised or remembered.

## Quick start

Create a directory, name it in the `.env` the daemon loads (the one in the checkout the
systemd unit runs), and restart:

    mkdir -p ~/.local/share/tmux-rc/scratch
    echo "TMUXRC_SCRATCH_DIR=$HOME/.local/share/tmux-rc/scratch" >> .env
    systemctl --user restart tmux-rc

Drop a folder in, say `report/` with an `index.html` or an `index.md`, and open
`<your tmux-rc URL>/scratch/report/`. **Your scratch URL is simply the base URL you
already open tmux-rc at, plus `/scratch/`**: nothing about it depends on how you reach the
daemon. Files added later appear without a restart; deleting a folder unpublishes it.

Keep the directory outside every checkout, so no commit can pick up a preview and no
worktree cleanup can delete one. It must exist when the daemon starts, because that is the
one time it is checked.

## The gallery

`<your tmux-rc URL>/scratch/` lists every folder as a card with a thumbnail, a description
and who made it. See [Scratch gallery](scratch-gallery.md).

## What `/scratch/` is for

Anything that is a pile of static files, not just tmux-rc work:

- an HTML mock or design study an agent just wrote;
- a Markdown document, such as a plan or a design note with Mermaid diagrams, which opens
  rendered ([Markdown pages](scratch-gallery.md#markdown-pages));
- the build output of a static site (a docs site, a landing page, a single-page app's
  `dist/`) for another project;
- a generated report, a chart, a screenshot set, a PDF.

## Your scratch URL, by access method

`TMUXRC_SCRATCH_URL` is only needed to [tell agents](#telling-agents-about-it) where
scratch lives; the daemon cannot learn the hostname your access method gives it. It is the
same base as the app, plus `/scratch`. Whoever can open that base URL can open every
preview, so the security of scratch is exactly the security of your access method.

- **Local only:** `http://localhost:18030/scratch` (or your `TMUXRC_PORT`). Only this
  machine sees it.
- **Tailscale** (the [recommended](other-tunnels.md) way): with the daemon left on
  loopback, `tailscale serve --bg 18030` publishes it over HTTPS at
  `https://<machine>.<tailnet>.ts.net/`, so the scratch URL is
  `https://<machine>.<tailnet>.ts.net/scratch`. Everyone on your tailnet who can reach this
  machine can see it: check your tailnet ACLs, especially if you share devices or nodes
  with others. Reaching `http://<machine>.<tailnet>.ts.net:18030` directly needs
  `TMUXRC_HOST=0.0.0.0`, which also opens the port to your LAN; prefer Serve. Never use
  `tailscale funnel`: it publishes to the internet with no login.
- **Cloudflare Tunnel, or another authenticating tunnel:**
  `https://<your-hostname>/scratch`. The [Access policy](cloudflare-tunnel.md) in front of
  the hostname covers `/scratch/` with no extra rule, so everyone that policy admits can
  see previews.
- **A reverse proxy (nginx, Caddy):** `https://<your-host>/scratch`. The proxy must
  forward `/scratch/` to the daemon unchanged, like every other path, and must not remove
  or replace the `Content-Security-Policy` header the daemon sets on it (a site-wide
  `header Content-Security-Policy ...` in Caddy, or `proxy_hide_header` in nginx, would).
  That header is what sandboxes the pages. Whatever login the proxy enforces is the only
  thing guarding scratch.
- **LAN** (`TMUXRC_HOST=0.0.0.0`): `http://<lan-ip>:18030/scratch`, visible to everyone on
  the network, like the app itself.

Whatever the method, the [sandbox](#what-it-exposes-and-what-it-wont-run) protects the
daemon's API from preview pages in the same way: it is a header the daemon sends, not a
property of the network path.

## Telling agents about it

Agents only use scratch if they know it exists. Add to `.env`:

    TMUXRC_SCRATCH_ADVERTISE=1
    TMUXRC_SCRATCH_URL=<your base URL>/scratch

and restart. The daemon then exports three variables to the tmux server's global
environment: `TMUXRC_SCRATCH_DIR`, `TMUXRC_SCRATCH_URL`, and `TMUXRC_SCRATCH_LOCAL_URL`
(the daemon's own bind address and port, `http://127.0.0.1:18030/scratch` by default). The local one exists because the public URL sits
behind your login: an agent that requests it gets a login redirect and cannot tell whether
its page works, so it checks against the daemon directly. **Only panes opened after that
inherit them**: shells and agents already running keep the environment they started with.
In a new pane, `env | grep SCRATCH` shows them. Each restart re-applies the setting, so a variable that is
no longer configured is removed rather than left pointing at nothing. Turning the flag off
stops the writes but leaves the last values in place until tmux restarts or you run
`tmux set-environment -gu` on them.

It is off by default because it writes to your tmux server's environment, which you may
manage yourself. The variables only say where scratch is; agents also need to be told what
to do with it. Paste [the agent snippet](scratch-gallery.md#the-agent-snippet) into your
agent instructions.

### One instruction file for every agent

Each agent CLI reads its own global instructions file, so the snippet would otherwise be
pasted, and kept in sync, in five places. Keep it in one file instead and point every tool
at it: tools that read a plain markdown file get a symlink, and Claude Code, which reads
`CLAUDE.md` rather than `AGENTS.md` but supports `@path` imports, gets one import line.
`ln -s` refuses to overwrite, so if a tool already has a global file, move its contents
into the shared one first.

    mkdir -p ~/.config/agents "${CODEX_HOME:-$HOME/.codex}" ~/.config/opencode ~/.omp/agent \
      ~/.gemini ~/.claude
    $EDITOR ~/.config/agents/AGENTS.md            # paste the snippet
    ln -s ~/.config/agents/AGENTS.md "${CODEX_HOME:-$HOME/.codex}/AGENTS.md"
    ln -s ~/.config/agents/AGENTS.md ~/.config/opencode/AGENTS.md
    ln -s ~/.config/agents/AGENTS.md ~/.omp/agent/AGENTS.md
    ln -s ~/.config/agents/AGENTS.md ~/.gemini/GEMINI.md
    echo '@~/.config/agents/AGENTS.md' >> ~/.claude/CLAUDE.md

| Tool | Global file it reads | Check |
| --- | --- | --- |
| Codex | `$CODEX_HOME/AGENTS.md` (default `~/.codex`) | `readlink -f ~/.codex/AGENTS.md` |
| opencode | `~/.config/opencode/AGENTS.md` | `readlink -f ~/.config/opencode/AGENTS.md` |
| omp | `~/.omp/agent/AGENTS.md` (a named profile: `~/.omp/profiles/<name>/agent/`) | `readlink -f ~/.omp/agent/AGENTS.md` |
| Gemini CLI | `~/.gemini/GEMINI.md` (the name follows the `context.fileName` setting) | `readlink -f ~/.gemini/GEMINI.md` |
| Claude Code | `~/.claude/CLAUDE.md`, plus what it imports | `/memory` in a session lists the imported file |

These paths come from each tool's source as installed here (Codex 0.162, opencode 1.18,
omp 18.4, Gemini CLI 0.63) and Claude Code's memory documentation for `@~/` imports; none
was exercised end to end. opencode also falls back to `~/.claude/CLAUDE.md` when it has no
global `AGENTS.md`, which the symlink makes moot.

## What it exposes, and what it won't run

**Everything in the directory is published to everyone who can reach tmux-rc.** That is the
whole access model: there is no per-file permission, and `/scratch/` itself lists every
folder, so not even an unguessable name hides one. Never put secrets, credentials or private data there. For the same reason
there is no default directory: an unset variable means nothing is served, so nothing gets
published because someone saved a file in the wrong place.

Inside a folder the mount lists nothing, and requests cannot climb out of scratch through
`..` or symlinks; the gallery skips anything that would.

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
and classic scripts with their data inlined are fine. A page that fetches JSON, calls an
API, or loads module scripts (which are fetched as CORS requests) will not work. Build those with their data
baked in, or give them their own hostname (below).

## Troubleshooting

- **404 at `/scratch/...`**: `TMUXRC_SCRATCH_DIR` is unset, names a directory that did not
  exist at startup, or was set without restarting the daemon. A folder with neither
  `index.html` nor `index.md` also 404s at its bare URL, since a folder has no listing; link the file (the
  gallery links such a folder's newest file).
- **The page loads but its data doesn't**: a `fetch` or XHR failing (look for a CSP error
  in the browser console) is the sandbox doing its job. Inline the data into the page or
  bundle it as a classic script.
- **`env | grep SCRATCH` is empty in a pane**: the pane predates the restart that
  advertised it. Open a new pane, or check that `TMUXRC_SCRATCH_ADVERTISE=1` is set.
- **A redirect to `http://` behind HTTPS**: already handled. The daemon makes its
  trailing-slash redirects path-only, so a TLS-terminating proxy or tunnel keeps its
  scheme.

## Why it is not a proxy

`/scratch/` serves files; it does not forward to a dev server on another port. A proxy
would put an arbitrary local app, with its own unauthenticated API, on the daemon's origin
and behind its login, where none of the sandboxing above can apply. Dev servers also tend
to need WebSockets for hot reload and absolute paths at the site root, so a path-prefixed
proxy would half-work in confusing ways. Build the site and drop the output in scratch, or
use a separate tunnel client.

## Mocks

A UI decision in tmux-rc is usually made by looking at two or three versions of a screen
side by side. Hand-built mock pages kept re-inventing the same frame (snapshotting the app,
A/B slots, viewport presets, a phone mode, a theme toggle, swiping) and kept growing the
same bugs: desktop frames squeezed into half-width slots that showed the narrow layout, a
phone showing two cramped slots, controls covering the app, snapshots gone stale. So there
is one harness, and a mock is only what differs.

**A mock is variants of the real app.** Each variant is `/m` over the [demo
fleet](https://github.com/querystory/tmux-rc/blob/main/scripts/demo_fleet.py) plus a
small script that patches its DOM and CSS, so every mock gets the same fictional data and
the app's real stylesheet and markup instead of a hand-rolled copy that drifts. A mock's
source is a folder outside every checkout, by default
`~/.local/share/tmux-rc/mocks/<name>/`, holding `variants.json` and the files it names:

- `title`, and `variants`: each a `name`, a `label`, an optional `short` label for the
  phone, a one-line `description`, and the `script` that patches the app. The script runs
  last, with `MOCK` set to the variant, view, theme and width being shown.
- `views`: the app states to show, each a `name`, `label`, URL `hash`, selectors to `click`
  and one to `wait` for, or a `chat` message for the canned chat (snapshots only). One
  plain view if left out.
- `widths`: the desktop widths to offer (default 1280, 1440 and 1920; 390 is always there).
- `states`: switches the design itself has, such as one account or two: each a `name`, a
  `label` and `options` as value/label pairs, plus an optional `default` value (else the
  first option). The harness shows each as a menu under its label and hands the current
  values to the script as `MOCK.state`.
- `fragments`: single elements to capture once, such as a populated New session sheet,
  given as the same steps as a view plus a `selector`. The script can drop them into any
  frame (`MOCK.fragments`).
- `note`: the working design note, in Markdown, shown in the harness's Note panel.
- `doc`: the name of the decision record in `docs/design/`, shown under the note.

**What gets committed is the decision, not the mock.** When a design is chosen, write
`docs/design/<doc>.md` with the final design and why, plus at most one line per rejected
option. When it changes later, edit the same page, so its history is the history of the
decision. The variants and their pages are throwaway, which is why they live outside the
repo.

**Two ways to run one.** `make mock NAME=<name>` boots the demo server, opens each view at
each width in both themes, keeps the finished DOM with its stylesheet and images inlined,
stops the server, and writes the harness to `$TMUXRC_SCRATCH_DIR/<name>/`. Scratch pages
cannot fetch, so that snapshot is the only way to put a mock on the phone, and it is
reproducible. `make mock-live NAME=<name>` instead serves the harness from the demo server
itself on `http://127.0.0.1:18039/mock/live/`, over the running app, and re-reads the
variant scripts on every reload: that is the loop for writing a variant, on this machine.

**The harness follows a few rules**, the ones the hand-built mocks kept breaking:

- Every choice (option, view, each state, width) is one menu under its own small label,
  ruled off from the next. A row of buttons per choice spread a mock with eight states
  over three rows of unlabelled chips where nothing said which belonged together. Each
  menu is as wide as its current value, never cut short: reading every choice at a
  glance matters more than one line, so a busy mock wraps, a whole choice at a time.
  The width menu and the theme, Note, Hide and A|B toggles stay small at the right end.
  Option text need not repeat its label ("All", not "Show: All").

- On a desktop, a frame is the app at exactly the chosen width, at 100%, scrolling
  sideways if the window is narrower. Two options sit side by side only when both fit at
  that width; otherwise ← and → flip between them.
- On a phone (narrower than 700px) the app fills the screen as the phone would show it,
  and the controls start folded into the pill. Unfolded, they are one row of menus
  (option, view, each state) with no width choices, and a swipe moves between options. A
  mock with one variant shows no option menu, and each variant's description sits in the
  Note panel.
- `h`, or the Hide button, folds the controls into a small pill, remembered per page, so
  what is left is the app alone. The light/dark toggle swaps in that theme's snapshot.
- A prototype can be played rather than only looked at: a frame posts
  `{ mock: { view, theme, state, once } }` to its parent to move to another view, flip the
  theme or change a state, and `once` carries state for the next frame only, such as a
  toast.
- The state is in the URL hash (`v`, `b`, `view`, `w`, `ab`, `theme`, `hide`, `note`), so
  a link opens the same comparison.

**Check every mock the same way.** `make mock-check NAME=<name>` shoots the published
harness at 1920×1080 and as an iPhone (390×844, mobile, touch), served with the same
sandbox policy as `/scratch/`, into the mock's `shots/` folder. It fails on a page
error or on a frame that is not the width it claims, which is the squeezed-slot bug.

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
