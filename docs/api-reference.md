---
title: API reference
---

The HTTP API runs on your own tmux-rc daemon. Its default address is
`http://127.0.0.1:18030`; use your authenticated tunnel address when connecting remotely.

## Machine-readable reference

`GET /openapi.json` describes the exact version you are running. The interactive pages
(`/apidocs` Swagger UI, `/apiredoc` ReDoc) are off by default because FastAPI's pages
load JavaScript from a third-party CDN on the origin that controls your terminals; set
`TMUXRC_API_DOCS=1` in the daemon's environment to serve them. The public documentation
website does not proxy your daemon's API.

## Endpoints

Every pane endpoint takes a tmux pane id (`%3`); `send` also accepts `session:window.pane`
or the pane's label. Agents and scripts should start with `/api/digest`; `/api/state` is
shaped for the phone and carries only the events new since the last parse.

### Read the fleet

| Endpoint | Purpose |
| --- | --- |
| `GET /api/digest` | Per pane: headline, activity, idle time, pending question, idle summary, recent events. The one call that gives the whole picture |
| `GET /api/state` | The phone's deck; `?v=<version>` long-polls until something changes (~25s) |
| `GET /api/panes/{id}/events` | A pane's activity log ([design](design/activity-log.md)) |
| `GET /api/panes/{id}/snapshots`, `/snapshots/{snap_id}` | Saved screen captures; the second returns one as text |
| `GET /api/panes/{id}/live` | Current colored frame; long-polls with `?frame=` |
| `GET /api/panes/{id}/history` | Colored scrollback rows above a live frame (`?before=&lines=`) |
| `GET /api/history` | Fleet activity over time; `?before=<epoch ms>` pages back |
| `GET /api/usage` | Each Claude and Codex account's plan windows, trend and projection ([design](design/plan-usage.md)) |
| `GET /api/open-loops` | Waiting on you / Moving / Dropped, by workstream ([design](design/open-loops.md)) |
| `GET /api/recent` | Panes and scratch pages last opened |
| `GET /api/version` | Web-asset hash plus feature flags the client gates on (`live_enabled`, `docs`, `scratch`) |

### Act on a pane

| Endpoint | Purpose |
| --- | --- |
| `POST /api/panes/{id}/send` | Type text or keys, including an answer to the pending question |
| `POST /api/panes/{id}/compose` | Send a draft of text and files (any type, 50MB each); a file goes in as a path the agent opens |
| `POST /api/panes/{id}/image` | Attach one image (clipboard paste, falling back to a path) |
| `POST /api/panes/{id}/click`, `/wheel` | Mouse click and scroll for apps that take them |
| `POST /api/panes/{id}/fit` | Fit the pane's window width to the phone ([fit width](design/fit-width.md)) |
| `POST /api/panes/{id}/select` | Focus the pane in tmux on the host |
| `POST /api/panes/{id}/rename` | Rename the pane, its session, or both |
| `POST /api/panes/{id}/close` | Kill the window that contains the pane |
| `GET`/`POST /api/panes/{id}/expunge` | Preview, then kill the window and delete the agent session's local files ([design](design/expunge.md)) |
| `POST /api/panes/{id}/move`, `/exit` | Resume the agent session on another account; type `/exit` into the pane left behind ([design](design/agent-accounts.md)) |

### Sessions, accounts, workspace

| Endpoint | Purpose |
| --- | --- |
| `GET /api/launchers` | The **+** menu's entries (labels only; commands stay in the daemon, `TMUXRC_LAUNCHERS`) |
| `POST /api/windows`, `POST /api/sessions`, `GET /api/sessions/dirs` | Open a window or session running a configured launcher; directory suggestions |
| `GET`/`POST`/`PUT /api/accounts`, `/accounts/login`, `/accounts/default` | Agent accounts and their sign-in windows |
| `GET`/`POST /api/restore`, `POST /api/restore/dismiss` | Offer and restore the workspace of a dead tmux server ([design](design/reboot-restore.md)) |
| `GET /api/export`, `/api/export/parts` | Everything the daemon knows, as a `.tgz` (also `python -m openbus.server export`) |
| `PUT /api/history/goal` | The goal line shown on the dashboard |

### Previews and push

| Endpoint | Purpose |
| --- | --- |
| `GET /scratch/`, `POST /api/scratch`, `GET`/`PUT /api/pins`, `GET /api/panes/{id}/scratch`, `GET /api/scratch/{name}/pane` | The [scratch gallery](scratch.md): listing, archive/delete, pins, and which pane made a page |
| `GET /api/push/config`, `POST /api/push/subscribe`, `/unsubscribe`, `/revoke-all`, `/presence`, `/answer` | Web push ([design](design/push-notifications.md)) |
| `WS /api/live-mode` | Live Mode's voice and chat socket ([design](design/live-mode.md)) |
| `POST /api/client-error` | Browser-side failure reports, forwarded to telemetry |

The web app is served at `/m/`; its assets also live under `/v/{version}/`, an immutable
prefix so a deploy never runs new code against cached old modules.

For agent-driven coordination, start with [orchestrating agents](agent-orchestration.md).
The [architecture](design/architecture.md) explains how observation and input reach tmux.

## Access

The daemon itself has no authentication. API access can control real terminals, so keep
it on a single-user machine and use an authenticating front end for remote access.
See [remote access](deploy/_index.md). Every state-changing call is audited to the journal
and to telemetry (see [telemetry hints](hint.md)).
