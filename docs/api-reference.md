---
title: API reference
---

The HTTP API runs on your own tmux-rc daemon. Its default address is
`http://127.0.0.1:18030`; use your authenticated tunnel address when connecting remotely.

## Interactive reference

Open `/apidocs` on your running daemon for Swagger UI, `/apiredoc` for ReDoc, or
`/openapi.json` for the machine-readable schema. Those routes describe the exact version
you are running. The public documentation website does not proxy your daemon's API.

## Common endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/digest` | Per-pane headline, activity, pending question, summary, and recent events |
| `GET /api/state` | Dashboard state and classifier usage |
| `GET /api/panes/{id}/events` | A pane's observed activity log |
| `GET /api/panes/{id}/snapshots` | Saved terminal snapshots |
| `GET /api/panes/{id}/live` | Current terminal frame; supports long polling |
| `POST /api/panes/{id}/send` | Send text or keys, including structured prompt answers, into a real tmux pane |
| `POST /api/panes/{id}/select` | Focus the pane in the host's tmux session; accepts no answer payload |
| `POST /api/panes/{id}/image` | Send an image to a pane |
| `GET /api/history` | Historical fleet activity |
| `GET /api/usage` | Agent subscription usage |

For agent-driven coordination, start with [orchestrating agents](agent-orchestration.md).
The [architecture](design/architecture.md) explains how observation and input reach tmux.

## Access

The daemon itself has no authentication. API access can control real terminals, so keep
it on a single-user machine and use an authenticating front end for remote access.
See [remote access](deploy/_index.md).
