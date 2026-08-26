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

{{< cards >}}
  {{< card link="other-tunnels/" title="Other tunnels" subtitle="Tailscale (prefer this) and ngrok — what to know before you pick one." >}}
  {{< card link="cloudflare-tunnel/" title="Cloudflare Tunnel + Access" subtitle="A public hostname with a real login in front of it. Step order is the security control." >}}
{{< /cards >}}
