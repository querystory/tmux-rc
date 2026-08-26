---
title: "ngrok"
---

# Exposing tmux-rc with ngrok

Status: **documented, not verified on this host** — the configuration below comes from
ngrok's official docs and pricing page (2026), not from a run against a live account.
Pricing and tier limits are the part most likely to have moved; re-check
[ngrok.com/pricing](https://ngrok.com/pricing) before relying on a number here. The
verification steps in ["Prove the auth is real"](#prove-the-auth-is-real) are mandatory.

## Read this first: the daemon has no authentication

tmux-rc binds `127.0.0.1:18030` and **authenticates nobody**. There is no login, no
token, no allowlist. `POST /api/panes/{id}/send` types characters into a real terminal on
your machine. An ngrok endpoint is a **public internet URL** — the default is that
anybody who has it can drive your shell.

So unlike a tailnet-private setup, with ngrok the authentication is not a bonus you may
skip: **an ngrok endpoint for this daemon without a Traffic Policy that authenticates is
a remote shell published to the internet.** Configure the policy in the same breath as
the endpoint, never "for now" without it. ngrok URLs are not secret — the hostnames
appear in Certificate Transparency logs and ngrok's `*.ngrok-free.app` space is actively
scanned.

## What ngrok gives you, and what it costs

**Traffic Policy** is the current name for ngrok's request-enforcement layer: a YAML
document of rule phases (`on_http_request`, `on_http_response`) that runs at ngrok's edge
before traffic reaches your machine. The authentication actions that matter here:

- **`oauth`** — redirects unauthenticated browsers to an identity provider (Google,
  GitHub, Microsoft, GitLab, and others; the first several have ngrok-managed apps so you
  need no client credentials of your own). This is the right one for a phone dashboard:
  a real login, session cookies, and a CEL expression to allow only *your* email.
- **`basic-auth`** — HTTP Basic with up to 10 `user:password` pairs. Simple and it works
  for API clients, but it's a shared static secret and the browser UX is poor.
- **`restrict-ips`** — CIDR allow/deny. Useless on a phone (carrier IPs move constantly),
  useful as a second factor for a fixed office egress.

**The tier picture (2026), because it drives the decision:**

| | Free ($0) | Hobbyist (~$8/mo) | Pay-as-you-go ($20/mo + usage) |
| --- | --- | --- | --- |
| Traffic Policy | yes, capped at **5 rules per policy** | yes | yes, metered |
| OAuth/OIDC logins | **3 traffic identities (MAU)** | 5 MAU | 5 included, then per-user |
| Domains | **1 auto-assigned dev domain** | 10 ngrok-branded static domains | custom domains |
| HTTP requests | 20k/month, 4k/min | 100k/month | unlimited |
| Data transfer | 1 GB/month | 5 GB/month | 5 GB included |
| Browser interstitial | **yes** | no | no |

The headline: **authentication is available on the free tier.** A single-user setup fits
inside "3 traffic identities" with room to spare, and one OAuth action plus one deny rule
is 2 of your 5 permitted rules. Free is not a reason to run unauthenticated.

The free tier's real costs are elsewhere:

- **The interstitial page.** Free endpoints show a browser warning page before your app
  loads, dismissed by a cookie for 7 days. On an installed PWA and on the `fetch`/
  websocket calls the dashboard makes, this is not a cosmetic annoyance — it can break
  the app outright. The documented workarounds (send an `ngrok-skip-browser-warning`
  header, or a non-browser User-Agent) are not things you can make Mobile Safari do.
  This alone makes free-tier ngrok a poor fit for tmux-rc specifically.
- **Request quota.** 20k requests/month sounds like plenty until you remember the
  dashboard polls. An always-on phone dashboard can burn that in days, and hitting the
  cap takes the endpoint down.
- **URL churn is mostly solved.** Every account, free included, gets one static dev
  domain (e.g. `your-name.ngrok-free.app`), so your phone bookmark and PWA install
  survive agent restarts. Don't run without claiming it — an ephemeral URL changes on
  every restart, which for an installed PWA means reinstalling it.

**Websockets are supported with no configuration** — ngrok forwards `Connection: upgrade`
out of the box, so `wss://` to `/api/live-mode` works. Verify it anyway; see below.

## Setup

Leave the daemon on loopback. Do **not** set `TMUXRC_HOST=0.0.0.0`; the agent connects
from the same machine, and keeping the bind on `127.0.0.1` means nothing on your LAN can
sidestep the ngrok policy.

### 1. Install the agent and claim a static domain

Install the `ngrok` binary to a **real path that survives a reboot** — `~/.local/bin/`,
never `/tmp` (this repo has been bitten by a tunnel client in `/tmp` twice; see
[deployment.md](../design/deployment.md)). Then claim your account's free static domain
from the ngrok dashboard and note it: `your-name.ngrok-free.app`.

### 2. Write the endpoint config with the auth policy inline

`~/.config/ngrok/ngrok.yml` (confirm the path on your system with `ngrok config check`):

```yaml
version: 3

agent:
  # No authtoken here — it comes from the environment; see step 3.
  log: stdout
  log_level: info

endpoints:
  - name: tmux-rc
    url: https://your-name.ngrok-free.app
    upstream:
      url: 18030
    traffic_policy:
      on_http_request:
        # 1. Anything unauthenticated is bounced to Google to log in.
        - actions:
            - type: oauth
              config:
                provider: google
        # 2. Logged in is NOT the same as authorized: without this rule, any
        #    Google account on earth is now typing into your terminals.
        - expressions:
            - "!(actions.ngrok.oauth.identity.email in ['you@example.com'])"
          actions:
            - type: deny
              config:
                status_code: 403
```

Rule 2 is the one people leave out, and leaving it out is the whole vulnerability: the
`oauth` action proves *someone* logged in, not that it was you. Use an explicit email
allowlist for a personal setup; `...email.endsWith('@your-company.com')` is the team
variant. That's 2 rules against the free tier's limit of 5.

If you'd rather not involve an identity provider, swap the two rules for one
`basic-auth` action — but understand the tradeoff: a shared static password, typed into a
phone, with no revocation short of editing the policy.

```yaml
    traffic_policy:
      on_http_request:
        - actions:
            - type: basic-auth
              config:
                realm: tmux-rc
                credentials:
                  - "you:a-long-random-password"
```

### 3. Keep the authtoken out of the config and out of `ps`

Environment variables take precedence over the config file, and `NGROK_AUTHTOKEN` is the
supported way to supply the token. That fits this repo's tunnel-env pattern exactly.

`~/.config/tmux-rc/tunnel.env` — mode `0600`, and it is deliberately outside the repo so
it can never be committed:

```bash
NGROK_AUTHTOKEN=2abc...your-token
```

```bash
chmod 600 ~/.config/tmux-rc/tunnel.env
```

Putting it here rather than in `ngrok.yml` or on the command line means the secret is in
exactly one file, not in the unit (which is committed to git) and not in `ps` output
(which every local user can read).

### 4. Wire it into this repo's systemd slot

Unlike Tailscale, ngrok **does** need the tunnel unit: the agent is a foreground process
that must be started, restarted on crash, and brought up at boot. That is precisely what
`deploy/systemd/tmux-rc-tunnel.service`
is a slot for. The unit already reads `EnvironmentFile=%h/.config/tmux-rc/tunnel.env`,
already has `Restart=always`, and stays inactive until that file exists — so step 3
activated it.

The one edit is `ExecStart`, which defaults to the placeholder `%h/.local/bin/tunnel-client`.
Either symlink the binary into that name, or point the unit at ngrok directly. Keeping
the arguments in the unit (rather than the env file) is fine here because they carry no
secret — the token is in the env file:

```ini
ExecStart=%h/.local/bin/ngrok start --all --config %h/.config/ngrok/ngrok.yml
```

Then:

```bash
make install-units                          # or re-run it after editing the unit
systemctl --user start tmux-rc-tunnel
systemctl --user status tmux-rc-tunnel
journalctl --user -fu tmux-rc-tunnel        # watch it connect
```

The unit's `StartLimitIntervalSec=1h` / `StartLimitBurst=20` tolerate the slow reconnect
flapping any tunnel does, while still failing a genuine crash-loop — no change needed.

## Prove the auth is real

An ngrok endpoint is public by construction, so these checks are not optional
housekeeping; they are the only thing standing between your terminals and the internet.
Run them **from a device that is not logged in** — a private browser window, or better,
`curl` from anywhere.

**1. The dashboard is challenged, not served.**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://your-name.ngrok-free.app/
```

Expect `302` (OAuth redirect to the provider) or `401` (basic auth). A `200` with the
dashboard HTML means the policy did not attach — check `ngrok config check` and the agent
log for a policy parse error, because a malformed policy can start the endpoint *without*
it.

**2. The write endpoint specifically.** A redirect on the homepage does not prove the API
is covered. Test the dangerous route directly, unauthenticated:

```bash
curl -sS -i --max-time 10 -X POST \
  https://your-name.ngrok-free.app/api/panes/%251/send \
  -H 'content-type: application/json' -d '{"keys":"echo probe"}'
```

Expect `302`/`401`/`403`. Anything else — including a `404`, which means you reached the
daemon — is a failure. Then check the pane: **nothing should have been typed into it.**
That physical check is the real test; the HTTP status is just a hint.

**3. The allowlist actually excludes.** Log in through OAuth with a *different* Google
account (any second account will do). You must get the `403` from rule 2. If you get the
dashboard, your CEL expression is inverted — note the leading `!` in the example: the
expression describes who to **deny**.

**4. Websockets survive the policy.** From an authenticated browser, open the dashboard,
tap Live, and confirm the status reaches `listening` rather than stalling at
`connecting`. ngrok forwards websocket upgrades natively, but a Traffic Policy that
issues a redirect will kill the upgrade for an unauthenticated client — which is the
correct behavior, and the reason to test it while logged in.

**5. Watch the traffic.** `ngrok`'s local inspector at `http://localhost:4040` shows every
request with the policy decision applied. Leave it open during the tests above; it's the
fastest way to see *why* something was allowed.

## Troubleshooting

**The PWA shows a warning page / assets fail to load.** That's the free-tier interstitial.
It's not fixable from the app side for a mobile browser — the documented bypasses require
setting a request header or User-Agent. Upgrade to Hobbyist (which removes it) or use
[Tailscale](tailscale.md) instead.

**`ERR_NGROK_...` about endpoint limits.** Free allows 3 online endpoints and 3 concurrent
agents. A stale agent from a previous shell may still be holding one — check the ngrok
dashboard and stop it.

**Endpoint went down mid-month.** You hit the 20k request or 1 GB transfer quota. Check
usage in the dashboard. A polling dashboard on an always-on phone will do this.

**The policy isn't applying.** Run `ngrok config check`. Verify the `traffic_policy` block
is nested under the *endpoint*, not at the top level. Check the agent log at startup —
policy errors are reported there, and the endpoint may come up unprotected.

**"5 traffic policy rules" error.** Free tier limit. Each element of `on_http_request` is
a rule; consolidate, or upgrade.

**OAuth loops back to the login page forever.** Usually third-party cookie blocking in the
phone browser, or a `max_session_duration` set very low. Try the same URL in a desktop
browser to isolate it.

**Token leaked into git.** If the authtoken ever lands in `ngrok.yml` inside a repo,
rotate it in the ngrok dashboard immediately — it grants the ability to publish endpoints
under your account.

## Where this leaves you

ngrok is the right choice when you need a **public** URL: a device you can't enroll in a
private network, a colleague you're sharing a session with, a webhook. It buys that with
a real authentication layer at the edge, and the free tier can genuinely authenticate.

But for the specific job of a personal always-on phone dashboard, it's second-best. You
are creating a public listener and then defending it, where
[`tailscale serve`](tailscale.md) creates no public listener at all. Free-tier ngrok is
worse still for this app — the interstitial degrades the PWA and the request quota
doesn't suit continuous polling.

**Ranking, plainly.** For a personal phone dashboard in front of an unauthenticated
keystroke-injection API:

1. **`tailscale serve`** — no public surface, auth is device enrollment, survives reboots
   without a supervisor. The default.
2. **Paid ngrok (or a Cloudflare Tunnel behind an identity proxy)** with OAuth plus an
   email allowlist — a genuine public option, correctly defended.
3. **Free-tier ngrok** — can be secured, but the interstitial and quotas make it a poor
   fit here, and it is the easiest configuration to accidentally leave open.
4. **`tailscale funnel`** — do not. It is public with no authentication whatsoever, which
   for this daemon means publishing a remote shell.
