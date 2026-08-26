---
title: "Other tunnels"
---

# Other ways to reach the daemon: Tailscale and ngrok

**The daemon authenticates nobody.** It binds `127.0.0.1:18030` because loopback is the
only access control it has. `POST /api/panes/{id}/send` types characters into a real
terminal on your machine, and `GET /api/panes/{id}/snapshots/{snap_id}` hands over the
scrollback. So whatever you put in front of the daemon **is** the entire access-control
system — the question is not "how do I get a URL for my phone", it is "who is allowed to
type into my shell".

This page is a pointer, not a runbook: each vendor's own docs are the current truth, and
neither tool below is verified against this host. If you need a public hostname with a
real login in front of it, a separately-reviewed Cloudflare Tunnel + Access runbook
lands in `docs/deploy/cloudflare-tunnel.md` (querystory/tmux-rc#148); until that merges,
use [Cloudflare's own Tunnel docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
and put an Access policy in front of the hostname *before* you start the tunnel.

## Tailscale — prefer this

`tailscale serve` publishes the daemon **to your tailnet only**. There is no public
listener for a scanner to find, no password to leak, and no browser session to steal;
the credential is a device enrollment, and your phone already runs the Tailscale app.
For a personal phone dashboard over an unauthenticated keystroke-injection API, that is
the smallest surface that still works, and it is the option to reach for first.

Setup is a few minutes with [Tailscale's own docs for `serve`](https://tailscale.com/kb/1312/serve).
Leave the daemon on loopback (do **not** set `TMUXRC_HOST=0.0.0.0`) so that even an
enrolled device cannot sidestep Serve by hitting port 18030 directly.

> **Do not use `tailscale funnel` for this daemon.** Funnel publishes to the open
> internet and has **no authentication layer of its own** — Tailscale's docs draw the
> line exactly here: Serve forwards identity headers, while "Funnel traffic, which is
> publicly available, does not include identity headers"
> ([Funnel docs](https://tailscale.com/kb/1223/funnel)). There is no "require login"
> toggle in front of it. `tailscale funnel 18030` publishes keystroke injection into
> your terminals to anyone who finds the hostname — and Funnel hostnames are not secret;
> they appear in Certificate Transparency logs as soon as the certificate is issued.

Tailscale needs nothing from `deploy/systemd/tmux-rc-tunnel.service`: `tailscaled` is
already a supervised service and the serve mapping lives in its state. Leave
`~/.config/tmux-rc/tunnel.env` absent and the unit stays cleanly inactive.

## ngrok — a public hostname, so authentication is mandatory

ngrok gives you a public URL, which puts it in the same risk category as a Cloudflare
Tunnel: the endpoint is reachable by anyone who finds it, so the edge policy is the only
thing standing between the internet and your shell. Authentication **is** available, and
on the free tier — ngrok's [Traffic Policy](https://ngrok.com/docs/traffic-policy/) can
enforce OAuth/OIDC or basic auth at the edge. Configure it in the same breath as the
endpoint, never "for now" without it, and remember that a login action proves *someone*
authenticated, not that it was you: pair it with an explicit email allowlist.

Two practical caveats for this app specifically. Free endpoints serve a browser
interstitial before your app loads, which an installed PWA and its `fetch`/WebSocket
calls cannot dismiss the documented way. And free-tier URL churn breaks a bookmarked PWA
unless you claim your account's static domain. Check
[ngrok's docs and pricing](https://ngrok.com/pricing) for the current limits.

## Need a public hostname with a real login?

Cloudflare Tunnel + Access is the option this repo has actually verified end to end; its
runbook is in flight as querystory/tmux-rc#148 and lands at
`docs/deploy/cloudflare-tunnel.md`. The one thing to carry over even if you follow
Cloudflare's docs instead: **create the Access policy before you start the tunnel.** The
moment `cloudflared` connects, the hostname serves the daemon to anyone who finds it, and
there is no grace period.
