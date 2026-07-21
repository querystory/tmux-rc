# SETUP — self-hosting tmux-rc safely

tmux-rc runs a small local daemon that **reads your tmux panes and injects keystrokes
into them** (`/send`, `/image`, `/select`). That is exactly as powerful as sitting at your
keyboard: anything that can reach the daemon can type commands into your terminals.

> **The one rule: never expose the daemon to the internet without authentication in front
> of it.** An unauthenticated public endpoint is remote code execution on your machine for
> anyone who finds the URL. Every internet-facing option below puts an auth gate *ahead* of
> the daemon; the daemon itself still has no built-in auth, so the gate is not optional.

The options below are a ladder from **safest** to **most convenient**. Pick the lowest rung
that reaches your phone. They are examples of tools that work well, not endorsements — any
equivalent authenticating proxy is fine.

Everything here assumes the daemon is bound where you want it. The relevant env vars live
in [`.env.example`](.env.example):

| var | meaning |
| --- | --- |
| `TMUXRC_HOST` | bind address — `127.0.0.1` (localhost only) or `0.0.0.0` (all interfaces) |
| `TMUXRC_PORT` | bind port (default `8080`) |

---

## 1. Localhost-only (default, safest)

Bind the daemon to loopback so nothing off the machine can reach it directly, then bridge
to it over a channel you already trust.

In `.env`:

```dotenv
TMUXRC_HOST=127.0.0.1
TMUXRC_PORT=8080
```

Reach it either:

- **On the same machine** — open `http://127.0.0.1:8080`.
- **From another device over SSH** — forward the port through an SSH session you already
  have:

  ```bash
  # on your laptop/phone-with-a-shell:
  ssh -L 8080:localhost:8080 you@your-dev-box
  # now http://localhost:8080 on the laptop reaches the daemon, tunneled over SSH
  ```

There is **no public exposure at all**: SSH provides both the transport and the
authentication. This is the recommended default. If SSH port-forwarding reaches your phone
(e.g. via an SSH client app), you may not need anything below.

---

## 2. Cloudflare Tunnel (`cloudflared`) + Cloudflare Access — recommended for phone-anywhere

> Public hostname. **Must** sit behind Cloudflare Access, or you have published an
> unauthenticated RCE endpoint.

`cloudflared` dials *outbound* from your machine to Cloudflare and exposes the local daemon
at a hostname you control — no inbound port, no router config. **Cloudflare Access** (part of
Cloudflare's Zero Trust product) is the auth gate: it authenticates every request at
Cloudflare's edge *before* it reaches the tunnel, using an identity provider you choose.
Access supports **Google, GitHub, and one-time email PIN (OTP)** as identity providers,
among others.

Rough shape:

1. **Run the tunnel** pointing at the local daemon:

   ```bash
   cloudflared tunnel --url http://localhost:8080
   # or a named tunnel bound to a hostname you own, e.g. tmux.example.com
   ```

2. **Create an Access application** in the Cloudflare Zero Trust dashboard: *Access >
   Applications > Add an application > Self-hosted*, on the tunnel's hostname
   (e.g. `tmux.example.com`).

3. **Add an allow policy** scoped to *you* — e.g. "Emails: `you@example.com`", or "GitHub
   org: your-org", or an OTP policy to your email. Anything not matching the policy is
   rejected at the edge and never reaches the daemon.

The result mirrors the two-gate design in [`docs/design/architecture.md`](docs/design/architecture.md):
an auth gate at the edge, an outbound-only tunnel, no open inbound port. This is the
recommended "reach it from my phone on cellular" setup.

---

## 3. ngrok + OAuth — quickest to try

> Public hostname. **Must** have OAuth enabled, or the URL is an unauthenticated RCE
> endpoint.

ngrok also dials outbound and gives you a public URL, and it can put **Google or GitHub
sign-in** in front of the tunnel. ngrok's current model configures auth via a **Traffic
Policy** with an `oauth` action (the older single `--oauth google` flag has been folded into
Traffic Policy):

```yaml
# oauth.yml
on_http_request:
  - actions:
      - type: oauth
        config:
          provider: google        # or: github
```

```bash
ngrok http 8080 --traffic-policy-file oauth.yml
```

You can further restrict to specific emails/domains inside the policy. Verify the exact
flag/policy syntax against ngrok's current docs, since ngrok iterates on this
([OAuth traffic policy](https://ngrok.com/docs/traffic-policy/examples/oauth-protection)).

Caveats: ngrok's free tier hands out **ephemeral URLs** that change each run (a reserved
domain needs a paid plan), and it routes your traffic through ngrok's edge. Great for a
quick trial; for a stable phone bookmark, option 2 or a reserved ngrok domain is nicer.

---

## 4. Google IAP (Identity-Aware Proxy) — for a Cloud Run / GCLB deployment

> Behind IAP; IAP is the auth gate. Do not expose the backend without it.

If you deploy the exposure layer on Google Cloud (a Cloud Run service or a GCLB backend that
fronts an outbound tunnel relay), put **Identity-Aware Proxy** in front. IAP authenticates
callers with Google accounts and forwards the verified identity as a header, and the backend
is locked to internal ingress so it can only be reached *through* IAP. This is heavier to
stand up (load balancer, IAP brand/client, ingress config) and mainly makes sense if you
already live in GCP or want a multi-user hosted relay. It is the pattern the project's own
internal deployment uses; the vendor-neutral shape is just "Google-account auth gate in front
of an internal-only backend."

---

## 5. Tailscale — no public endpoint, reachable from your own devices

> Not internet-exposed at all — reachable only from devices on your tailnet.

If you only need to reach the daemon from **your own devices**, put the machine on a
[Tailscale](https://tailscale.com) tailnet and bind the daemon to the Tailscale interface (or
keep it on `127.0.0.1` and use Tailscale Serve). Access is gated by **device authentication**
into your tailnet — there is no public URL to leak. Your phone reaches
`http://<machine-tailscale-name>:8080` over the encrypted mesh. This is a strong middle
ground: no edge provider sees your traffic, nothing is on the public internet, and setup is
minimal. The tradeoff is that every viewing device must be enrolled in the tailnet.

---

## Choosing

- **Just you, at your machine or over SSH?** Option 1. Done.
- **Your phone, anywhere, stable bookmark?** Option 2 (Cloudflare Tunnel + Access).
- **Kick the tires in 30 seconds?** Option 3 (ngrok + OAuth), mind the ephemeral URL.
- **Already on GCP / want a hosted multi-user relay?** Option 4 (IAP).
- **Only your own devices, no public exposure at all?** Option 5 (Tailscale).

Whatever you pick: the daemon has no auth of its own, so the gate in front is the whole of
your security. If you can reach the daemon without signing in, so can everyone else.

> **Production / internal deployment note.** The project's own production deployment runbook
> (systemd units, durable service-account auth, the tunnel relay and its infrastructure)
> lives in a separate **internal ops repo**, not here — it is specific to one organization's
> cloud. This guide is the vendor-neutral, self-hosting equivalent.
