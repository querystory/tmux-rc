---
title: "Deploying"
---

# Reaching the daemon from outside localhost

The tmux-rc daemon binds `127.0.0.1:18030` and **authenticates nobody** — loopback is the
only access control it has, and `POST /api/panes/{id}/send` types into a real terminal on
your machine. Anything you put in front of it *is* the access-control system.

These pages cover the ways to do that safely.

{{< cards >}}
  {{< card link="other-tunnels/" title="Other tunnels" subtitle="Tailscale (prefer this) and ngrok — what to know before you pick one." >}}
{{< /cards >}}
