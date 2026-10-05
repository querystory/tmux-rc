# Security

## Reporting a vulnerability

Report privately via [GitHub Security Advisories](https://github.com/querystory/tmux-rc/security/advisories/new).
Please don't open a public issue for a vulnerability.

## Known by design: the daemon has no authentication

tmux-rc **authenticates nobody**. `POST /api/panes/{id}/send` types into a real terminal,
so anyone who can reach the port controls your machine. It binds `127.0.0.1:18030` by
default — `TMUXRC_HOST` / `TMUXRC_PORT` override that, and setting `TMUXRC_HOST=0.0.0.0`
hands the same control to everyone who can route to the box. It is meant for
**single-user machines only** — loopback is not a permission check, and on a shared host
every other account can reach that port.

This is a documented design constraint, not a vulnerability. Whatever you put in front of
the daemon *is* the access-control system — see [docs/deploy/](docs/deploy/). Reports that
amount to "the API is unauthenticated" will be closed as working-as-documented; reports
that it can be reached in a way the docs say is safe are very much in scope.

## Tunnel identity (`X-Tunnel-User`)

The tunnel relay authenticates the phone through IAP and the tunnel client forwards the
email as `X-Tunnel-User`. That email is what the audit trail and usage telemetry record
as the actor. The daemon only sees a loopback TCP peer, though, and every local process
is a loopback peer. So by default any of them (a coding agent running in one of your
panes, say) can set the header and have its keystrokes recorded as yours. It gains no
access it didn't already have, because the API is unauthenticated (above). What it
breaks is the answer to "who did this", which is the whole point of the trail.

`TMUXRC_TUNNEL_SECRET_FILE` closes the naive version of that. When it is set, the daemon
believes `X-Tunnel-User` only if the same request carries the file's contents in
`X-Tunnel-Secret`, compared in constant time. A claim without the secret, or with the
wrong one, is still served (as local) and is logged as `local:127.0.0.1 claiming
'<email>' without a valid tunnel secret`, so attempts show up in the trail. Requests
without the header, like your own `curl localhost:18030`, behave exactly as before.

It is opt-in because the tunnel client has to send the header. Turning it on before the
client does would make every phone request anonymous. To enable it:

1. Set `TMUXRC_TUNNEL_SECRET_FILE=~/.config/tmux-rc/tunnel-secret` in `.env` and restart
   the daemon. It creates the file (mode 600) if it is missing.
2. Have the tunnel client read that file at startup and **set** (overwrite, never append)
   `X-Tunnel-Secret` on every request it makes to the daemon, HTTP and WebSocket alike,
   after copying the relay's headers. Overwriting matters: it means a copy of the header
   sent from the browser can never get through.

**Limits.** The secret is a file owned by your own account, and anything running as you
can read it, agents included. It stops a process that simply sets the header. It does
not stop one that goes looking for the file. Fixing that properly needs a boundary the
operating system enforces: run the tunnel client as a separate OS user that owns the
secret, or have it reach the daemon over a unix socket and check the caller's UID with
`SO_PEERCRED`, so the identity comes from the kernel instead of a header. Both are
follow-up work.

## Telemetry

`TMUXRC_QSDEBUG=1` sends raw pane text and model output to the configured OTLP endpoint.
It is off unless you set it. Terminal contents can contain secrets — don't enable it
against a sink you don't control.
