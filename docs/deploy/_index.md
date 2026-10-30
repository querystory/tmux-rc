---
title: "Deploying"
---

# Reaching the daemon from outside localhost

The tmux-rc daemon **has no authentication** — no login, no API key, no token. `POST
/api/panes/{id}/send` types into a real terminal on your machine, so anyone who can reach
its port can control your terminal. It binds `127.0.0.1:18030` to keep that "anyone" down
to your own machine — which is why tmux-rc is for **single-user machines only**: loopback
is not a permission check, and on a shared host every other account can reach that port.
The moment you put it on a network, whatever you place in front of it *is* the entire
access-control system.

These pages cover the ways to do that safely.

## Tell the daemon its public name

The daemon answers only requests whose `Host` it knows: `localhost`, `127.0.0.1`, `[::1]`
and a specific `TMUXRC_HOST` bind. Tunnels and reverse proxies forward the name the phone
typed, so until that name is in `TMUXRC_ALLOWED_HOSTS=tmux.example.com` (comma-separated,
no scheme) every request gets `400 Host is not allowed` and WebSockets close with code
1008. Browser writes also need the public origin in
`TMUXRC_ALLOWED_ORIGINS=https://tmux.example.com`. Put both in `.env` and restart the
daemon. They are DNS-rebinding and cross-site defences, not a login; see
[SECURITY.md](https://github.com/querystory/tmux-rc/blob/main/SECURITY.md). Let the proxy
pass the daemon's `Cache-Control` headers through untouched: everything is `no-store`
except the immutable assets under `/v/<version>/`, so a proxy with caching rules of its own
serves stale JS after an upgrade.

{{< cards >}}
  {{< card link="other-tunnels/" title="Other tunnels" subtitle="Tailscale (prefer this) and ngrok — what to know before you pick one." >}}
  {{< card link="cloudflare-tunnel/" title="Cloudflare Tunnel + Access" subtitle="A public hostname with a real login in front of it. Step order is the security control." >}}
  {{< card link="previews/" title="Previewing work" subtitle="Mocks and throwaway previews through the daemon, without a second tunnel." >}}
{{< /cards >}}
