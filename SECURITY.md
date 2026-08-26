# Security

## Reporting a vulnerability

Report privately via [GitHub Security Advisories](https://github.com/querystory/tmux-rc/security/advisories/new).
Please don't open a public issue for a vulnerability.

## Known by design: the daemon has no authentication

tmux-rc **authenticates nobody**. `POST /api/panes/{id}/send` types into a real terminal,
so anyone who can reach the port controls your machine. It binds `127.0.0.1:18030` and is
meant for **single-user machines only** — loopback is not a permission check, and on a
shared host every other account can reach that port.

This is a documented design constraint, not a vulnerability. Whatever you put in front of
the daemon *is* the access-control system — see [docs/deploy/](docs/deploy/). Reports that
amount to "the API is unauthenticated" will be closed as working-as-documented; reports
that it can be reached in a way the docs say is safe are very much in scope.

## Telemetry

`TMUXRC_QSDEBUG=1` sends raw pane text and model output to the configured OTLP endpoint.
It is off unless you set it. Terminal contents can contain secrets — don't enable it
against a sink you don't control.
