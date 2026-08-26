---
weight: 2
title: "Cloudflare Tunnel"
---

# Exposing tmux-rc with Cloudflare Tunnel + Access

> ### ⚠️ Create the Access policy BEFORE you start the tunnel
>
> The instant `cloudflared` connects, your hostname serves the daemon to the whole
> internet — no warm-up, no grace period. Without Access in front, anyone who learns the
> hostname can `POST /api/panes/{id}/send` and type into your terminal. We measured
> exactly that while writing this guide.
>
> Unsure whether the gate is live? **Stop the tunnel, then check.**

For reaching the daemon from a phone over the internet, with a login in front of it.
Only needed if you want a public hostname — on a LAN you need none of this, and if you
can put a VPN client on the phone, a private mesh such as `tailscale serve` (see [Deploying](../)) is safer because
there is no public surface to misconfigure.


## The cheat sheet

Seven commands. **Do them in this order** — step 4 is what stops the daemon being
published to the internet without a login, and step 6 is how you prove it worked.

```bash
# 1. one-time: authorize cloudflared for your domain (opens a browser)
cloudflared tunnel login

# 2. create the tunnel and point a hostname at it
cloudflared tunnel create tmux-rc
cloudflared tunnel route dns tmux-rc tmux.example.com

# 3. write the ingress config (see below) then check it WITHOUT connecting
cloudflared tunnel --config ~/.cloudflared/tmux-rc.yml ingress validate
```

```yaml
# ~/.cloudflared/tmux-rc.yml
tunnel: <UUID from step 2>
credentials-file: /home/you/.cloudflared/<UUID>.json
ingress:
  - hostname: tmux.example.com
    service: http://localhost:18030
  - service: http_status:404
```

**4. Create the Access application — before anything is running.** In the dashboard:
Zero Trust → Access → Applications → Create new application → Self-hosted, hostname
`tmux.example.com`, then a policy: *Allow* / *Emails* / your address. This is the login.

**5. Confirm the gate is live, still with nothing running:**

```bash
curl -s -o /dev/null -w '%{http_code}
' https://tmux.example.com/
# 302 → gate is live, continue.  530 → go back to step 4.  200 → STOP, something is open.
```

**6. Start it, then immediately verify:**

```bash
systemctl --user start tmux-rc-tunnel     # see step 6 below for the unit
curl -s -o /dev/null -w '%{http_code}
' -X POST   https://tmux.example.com/api/panes/%251/send   -H 'content-type: application/json' -d '{"keys":"echo probe"}'
# 302 = correct (challenged).  200 or 404 = the request REACHED the daemon: stop the
# tunnel now and fix the policy.
```

**7. Open `https://tmux.example.com/` on your phone**, sign in, and add to home screen.

Everything below is the same seven steps with the reasoning, the exact systemd unit, and
what to do when a step misbehaves. Read it if something breaks or you want to know why.

## The long version

The same steps, with the why.
## Read this first: the daemon has no authentication

**The tmux-rc daemon does not authenticate anything.** There is no login, no API key, no
session. It binds `127.0.0.1:18030` precisely because localhost is the only access
control it has.

That matters more than the usual "don't expose your dev server" advice, because of what
the API does:

```
POST /api/panes/{id}/send      # injects keystrokes into one of your terminals
```

Anyone who can reach that endpoint can type into your shells — as you, on your machine,
with your credentials and your SSH agent. `GET /api/panes/{id}/snapshots/{snap_id}`
hands them the scrollback (secrets, tokens, whatever was on screen) on the way, and
`GET /api/panes/{id}/live` long-polls a live view of the screen. If voice Live Mode is
enabled (`TMUXRC_LIVE_MODE=1`, off by default), the `/api/live-mode` WebSocket lets a
voice session drive panes too.

So: **a tunnel without an authentication layer publishes a remote-code-execution API to
the internet.** Not "a risk"; the literal function of the endpoint. Obscure hostnames do
not help — `*.trycloudflare.com` names and Certificate Transparency logs for your own
domain are both crawled continuously, and an unauthenticated hit is all it takes.

The tunnel is not the security boundary. **Cloudflare Access is.** Everything below
exists to put Access in front of the tunnel, and the [verification](#7-verify-that-auth-actually-works)
section exists so you can prove it is really there before you trust it.

> **Quick tunnels (`--url`, `*.trycloudflare.com`) cannot be protected by Access.**
> A self-hosted Access application can only be created for a domain that "must belong to
> an active zone in your Cloudflare account" — and `trycloudflare.com` is Cloudflare's
> zone, not yours. There is simply nowhere to attach a policy, which is *why* a quick
> tunnel is unauthenticated: it is a consequence of the zone rule, not a setting you
> forgot. Cloudflare documents quick tunnels as "intended for testing and development
> only," with a 200 in-flight request cap (`429` beyond it), no Server-Sent Events, and no
> uptime guarantee. **Never point a quick tunnel at tmux-rc.** Every step below uses a
> *named* tunnel on your own domain, which is the only shape Access can protect.

## What you need

- A domain on a Cloudflare account (the free plan is fine), with Cloudflare as its
  authoritative DNS — i.e. an active zone.
- Cloudflare Zero Trust enabled on that account. **The free tier includes up to 50
  users** and is what this runbook assumes; see [the free-tier reality](#free-tier-reality).
- `cloudflared` installed on the machine running the daemon.
- The daemon already working locally at `http://localhost:18030`.

Every command below was checked against **cloudflared 2026.3.0**. Check yours with
`cloudflared --version`; the `tunnel`/`access` subcommand surface has been stable for
years, but flags do drift.

## 1. Install cloudflared

Cloudflare publishes `.deb`/`.rpm` packages and a static binary; use your platform's
package instructions from the [downloads page](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/).
On Debian/Ubuntu:

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
cloudflared --version
```

Cloudflare rotated the signing key for this repository on 2025-10-30. If you set the
keyring up before then, re-run the `curl … | sudo tee` line above — otherwise `apt update`
stops seeing new versions, quietly. The canonical instructions live at
[pkg.cloudflare.com](https://pkg.cloudflare.com/).

Whatever you do, **install to a real path** — `/usr/local/bin` or `~/.local/bin`, never
`/tmp`, which a reboot erases. That failure has already bitten this project once, which
is why `deploy/systemd/tmux-rc-tunnel.service` calls it out.

## 2. Authenticate cloudflared to your zone

```bash
cloudflared tunnel login
```

This opens a browser, asks which zone to authorize, and writes `~/.cloudflared/cert.pem`.
That file is the *origin certificate*: it authorizes this machine to **create tunnels and
DNS records** in your zone. It is not what serves traffic, and it is not needed at
runtime if you run the tunnel by UUID — keep it `chmod 600` and treat it like an API key.

On a headless box, run `cloudflared tunnel login` on your laptop and copy `cert.pem`
over; the flow needs a browser exactly once.

## 3. Create the tunnel and route DNS

```bash
cloudflared tunnel create tmux-rc
```

Two things land: a tunnel UUID (printed) and a credentials file at
`~/.cloudflared/<UUID>.json`. **That JSON is the tunnel's actual secret** — anything
holding it can serve traffic for the tunnel. It is written `0400`; leave it that way.

Now point a hostname at it:

```bash
cloudflared tunnel route dns tmux-rc tmux.example.com
```

This creates a proxied CNAME (`<UUID>.cfargotunnel.com`) in your zone. The record
**must stay proxied** (orange cloud). A grey-cloud/DNS-only record bypasses Cloudflare
entirely — which means it bypasses Access, which means the daemon is naked on the
internet. If you ever "fix" a problem by unproxying this record, you have removed all
authentication.

**Then check the CNAME actually points at the tunnel you just created.** We have seen
`route dns` bind the hostname to a *different*, pre-existing tunnel in the same account,
and `--overwrite-dns` decline to correct it afterwards. That failure is quiet and it
matters: your Access application would be guarding a hostname whose traffic is served by
somebody else's connector, and your own tunnel would look mysteriously dead.

```bash
cloudflared tunnel list                 # note the UUID of `tmux-rc`
dig +short CNAME tmux.example.com       # must be <that UUID>.cfargotunnel.com
```

If the UUID does not match, fix the record **in the Cloudflare dashboard** (DNS → the
CNAME → point it at `<your-UUID>.cfargotunnel.com`, proxied) rather than re-running
`route dns`. Confirm the tunnel's own view too — both of these are read-only:

```bash
cloudflared tunnel list
cloudflared tunnel info tmux-rc
```

## 4. Point the tunnel at the daemon

Write `~/.cloudflared/tmux-rc.yml`:

```yaml
tunnel: tmux-rc
credentials-file: /home/YOUR_USER/.cloudflared/YOUR_TUNNEL_UUID.json

ingress:
  - hostname: tmux.example.com
    service: http://localhost:18030
  - service: http_status:404
```

Notes that are easy to get wrong:

- **The catch-all `http_status:404` rule is mandatory.** cloudflared refuses to start
  without a final rule that has no `hostname`.
- `service:` is plain `http://` on purpose. The daemon speaks HTTP on loopback; TLS is
  terminated at Cloudflare's edge, so the phone still gets `https://`/`wss://`. Do not
  add `noTLSVerify` — there is no TLS on this hop to verify.
- Use a **dedicated config file** rather than the default `~/.cloudflared/config.yml` if
  this machine runs other tunnels. `cloudflared tunnel run` reads `config.yml` by default;
  pass `--config` to select another. Keeping tmux-rc's ingress in its own file means a
  later unrelated tunnel edit cannot silently re-route your terminals.
- **You do not need `originRequest: websocket: true`.** WebSockets are proxied by default;
  that setting exists for other purposes. See [WebSockets](#websockets-live-mode) below.

**Do not start the tunnel yet.** The obvious next move — run it in the foreground to
"see if the config works" — is the exact mistake this doc is ordered to prevent: it
publishes the daemon with no authentication in front of it. Validate the file without
serving any traffic instead:

```bash
cloudflared tunnel --config ~/.cloudflared/tmux-rc.yml ingress validate
cloudflared tunnel --config ~/.cloudflared/tmux-rc.yml ingress rule https://tmux.example.com/api/state
```

`ingress validate` parses the rules and reports errors; `ingress rule` shows which rule a
given URL would match. Neither connects to Cloudflare and neither serves anything. That
is all the confidence you need before step 5 — the end-to-end test happens in step 7,
*after* Access is in place.

## 5. Put Cloudflare Access in front of it — BEFORE you run the tunnel

This is the step that makes the whole thing safe. Everything before it just made your
terminals reachable.

### Enable an identity method

In the Cloudflare dashboard: **Zero Trust → Integrations → Identity providers**.

New Zero Trust organizations get the **Cloudflare identity provider** as the default
login method. For a personal single-user install, **One-time PIN** is the simplest
option: no external IdP, no app registration. Access emails a code to the address the
visitor types; the PIN expires 10 minutes after it is requested and is single-use.
OTP is no longer added automatically to new organizations, so add it explicitly:
**Add new identity provider → One-time PIN**.

Google, GitHub, Microsoft Entra, Okta, and the rest of the OIDC/SAML catalogue are also
available on the free plan. Social/OIDC login (e.g. GitHub) is worth the extra five
minutes if you have it — it re-authenticates in one tap rather than an email round-trip,
which is the difference between pleasant and irritating on a phone.

### Create the application

**Zero Trust → Access controls → Applications → Create new application → Self-hosted and
private → Add public hostname.**

- **Application name**: `tmux-rc`
- **Public hostname**: subdomain `tmux`, domain `example.com`, path empty.
  Leave the path empty — the whole origin must be protected, including
  `/api/*` and the `/api/live-mode` WebSocket. Protecting only `/` would leave the
  keystroke-injection API wide open, which is the entire failure mode this doc exists to
  prevent.
- **Session duration**: see [session length](#session-length-and-phone-re-auth) below.
- **Identity providers**: select the ones you enabled.

### Add policies

Access applications are **deny by default** — an application with no matching Allow
policy rejects everyone. You need two policies.

**Policy 1 — you, from a browser:**

- Action: **Allow**
- Selector: **Emails** → your address. (Or **Emails ending in** → `@example.com` for a
  small team. Prefer the explicit list: "emails ending in `@gmail.com`" is not an
  authentication policy, it is an invitation.)

**Policy 2 — scripts and monitoring (optional but useful):**

- Action: **Service Auth**
- Selector: **Service Token** → the token you create below.

The action **must** be `Service Auth`. On an `Allow` policy Access ignores the token
headers and redirects to the IdP login page — the single most common reason "my curl
still gets an HTML login page." Service Auth exists precisely for flows with no browser.

The application also has a **"401 Response for Service Auth policies"** option. Turn it
on if scripts talk to this hostname: a non-browser client handles a clean `401` far
better than a `302` into an HTML login page, and it makes a failed check unmistakable
rather than something that merely "returned some HTML".

### Create a service token

**Zero Trust → Access controls → Service credentials → Service Tokens → Create Service
Token.** Name it, pick a duration (you choose it at creation; `8760h` = one year is a
common pick).

You get a Client ID and a Client Secret. **The secret is shown exactly once.** Store it
in a password manager or a `chmod 600` file — not in the repo, not in the unit, not on a
command line where `ps` can read it.

Clients authenticate with two headers:

```
CF-Access-Client-Id: <CLIENT_ID>
CF-Access-Client-Secret: <CLIENT_SECRET>
```

<a id="free-tier-reality"></a>
### Free-tier reality

The gating question for this whole document — *does basic authentication cost money?* —
answers cleanly: **no.**

- **Cloudflare Zero Trust is free for up to 50 users.** Access applications, policies,
  One-time PIN, and the standard OIDC/SAML identity providers are all included.
- Paid tiers (around **$7/user/month** last time Cloudflare published a figure) remove the
  50-seat cap and lengthen log retention. Cloudflare's live pricing page renders its plan
  table in JavaScript, so this number is harder to cite than it used to be — treat it as
  indicative and check before budgeting.
- The relevant cliff is the **50-seat limit**: once the seats are consumed, *additional*
  users who try to log in are blocked until you upgrade (already-authenticated users are
  unaffected). For a personal or small-team tmux-rc install you will never touch this.
- Cloudflare's Service-Specific Terms also set an average of 150,000 DNS queries per seat
  per month — but that applies to **Gateway DNS**, which an Access-plus-Tunnel setup like
  this one does not use. Mentioned only so you recognise it as irrelevant here.

So the honest summary: **there is no paywall in front of the authentication you need
here.** If a guide tells you otherwise, it is out of date. Pricing does change — confirm
against [Cloudflare's plans page](https://www.cloudflare.com/plans/zero-trust-services/)
before you build a budget on it.

## 6. Run it from the repo's systemd unit

> **Do not start the tunnel until step 5's Access policy exists and you have
> confirmed it in the dashboard.** The moment `cloudflared` connects, the hostname
> serves your daemon to anyone who knows it. There is no grace period and no
> "it's only up for a minute" — a request that arrives in that window reaches
> `/api/panes/{id}/send`, which types into your terminal. Access must be in place
> first, and step 7 exists to prove it is.
>
> This is not hypothetical: while validating this guide we started the tunnel
> before creating the Access application and measured a plain `200 OK` with the
> origin's content served to the open internet. The origin was a throwaway static
> page, not the daemon — deliberately — but the sequencing mistake is easy to make
> and the consequence with a real daemon behind it is remote code execution.

**Pre-flight — run this before you start anything.** With the tunnel still stopped,
the hostname should already be answering with an Access challenge, because the DNS
record and the Access application both exist by now:

Test the API path, not just `/` — a path-scoped application protects `/` and leaves
`/api` open, and that is the failure you most need to catch *before* the daemon is
behind it:

```bash
for p in / /api/state; do
  printf '%s -> ' "$p"
  curl -s -o /dev/null -w '%{http_code}\n' "https://tmux.example.com$p"
done
```

- **302 for both** — the gate is live. Cloudflare is challenging anonymous requests even
  with no origin behind them. Safe to proceed.
- **530 / 1033** — DNS points at a tunnel that isn't running, and Access is NOT
  confirmed. Go back to step 5; do not start the tunnel to "see if it works".
- **302 for `/` but not for `/api/state`** — your application has a path scope. Remove it
  so it covers the whole hostname, then re-run this.
- **200, or anything else** — stop. Something is answering without authentication.
  Find out what before you connect a daemon to it.


This repo ships [`deploy/systemd/tmux-rc-tunnel.service`](https://github.com/querystory/tmux-rc/blob/main/deploy/systemd/tmux-rc-tunnel.service)
as a deliberately vendor-neutral slot:

```ini
ExecStart=%h/.local/bin/tunnel-client
EnvironmentFile=%h/.config/tmux-rc/tunnel.env
ConditionPathExists=%h/.config/tmux-rc/tunnel.env
```

The unit stays *inactive* (not failed) until that env file exists, so a LAN-only install
ignores it entirely. Fitting cloudflared into that shape — rather than inventing a
parallel unit — keeps `systemctl --user restart tmux-rc.target` working as documented.

### Option A: make the slot point at cloudflared (recommended)

Keep the unit untouched and make `~/.local/bin/tunnel-client` a two-line wrapper. This is
what the slot is for, it survives `git pull`, and there is nothing to reconcile:

```bash
mkdir -p ~/.local/bin ~/.config/tmux-rc
cat > ~/.local/bin/tunnel-client <<'EOF'
#!/bin/sh
set -e
CF=$(command -v cloudflared) || {
  echo "tunnel-client: cloudflared not found on PATH ($PATH)" >&2
  exit 127
}
exec "$CF" --no-autoupdate \
  tunnel --config "$HOME/.cloudflared/tmux-rc.yml" run
EOF
chmod +x ~/.local/bin/tunnel-client
```

The `command -v` guard is worth the three lines: a systemd user unit gets a minimal PATH,
not your login shell's, so a `cloudflared` you installed to `~/.local/bin` may be invisible
here. Failing with a named error beats `exec ""`. If it does trip, either hard-code the
absolute path or add `Environment=PATH=...` to the unit.

`--no-autoupdate` matters under systemd: cloudflared's self-updater restarts the process
on its own, which fights `Restart=always` and makes the version running unpredictable.
Let your package manager own upgrades. The tunnel name comes from the config's `tunnel:`
key, so `run` needs no argument.

The env file is what arms `ConditionPathExists`. cloudflared takes its config from the
YAML, so this file is mostly a marker — but it is the right place for anything secret,
since the unit is in git and flags are visible in `ps`:

```bash
cat > ~/.config/tmux-rc/tunnel.env <<'EOF'
# Marker file: arms tmux-rc-tunnel.service (ConditionPathExists).
# cloudflared reads ~/.cloudflared/tmux-rc.yml; put secrets here, not in flags.
TUNNEL_METRICS=localhost:20241
EOF
chmod 600 ~/.config/tmux-rc/tunnel.env
```

### Option B: a drop-in override

If you would rather not have a wrapper script, override `ExecStart` with a drop-in
(`systemctl --user edit tmux-rc-tunnel`), which writes
`~/.config/systemd/user/tmux-rc-tunnel.service.d/override.conf`:

```ini
[Service]
ExecStart=
# The .deb installs to /usr/bin, the static binary usually to /usr/local/bin —
# `command -v cloudflared` to check yours, and set the absolute path here.
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel --config %h/.cloudflared/tmux-rc.yml run
```

The empty `ExecStart=` is required — without it systemd appends a second command instead
of replacing the first. The tradeoff versus Option A: the drop-in lives outside the repo
and outside `~/.local/bin`, so it is one more place to remember. Both work; pick one.

### Start and verify

```bash
systemctl --user daemon-reload
systemctl --user start tmux-rc-tunnel
systemctl --user status tmux-rc-tunnel
journalctl --user -fu tmux-rc-tunnel
```

Healthy startup logs four `Registered tunnel connection` lines (Cloudflare establishes
redundant edge connections). If the unit reports `condition failed` and stays inactive,
`~/.config/tmux-rc/tunnel.env` is missing — that is the unit working as designed.

Do **not** use `cloudflared service install`: it installs a *system* service under root,
which double-runs the tunnel alongside the user unit and takes it outside
`tmux-rc.target`.

## 7. Verify that auth actually works

Do not skip this. "The login page appeared once" is not proof; the API paths and the
WebSocket are what matter, and it is entirely possible to protect `/` and leave `/api`
open (a path-scoped application does exactly that).

### 7.1 An unauthenticated request must be challenged

```bash
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://tmux.example.com/api/state
```

Expect a **302** to `https://<your-team>.cloudflareaccess.com/cdn-cgi/access/login/...`
(or a 401/403). If you get **200 and JSON, stop** — your terminals are exposed. The
usual causes: the DNS record is grey-clouded, the Access application hostname does not
match, or the application has a path scope that misses `/api`.

Test the dangerous endpoint by name, not just a read-only one:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST https://tmux.example.com/api/panes/%251/send \
  -H 'content-type: application/json' -d '{"keys":"echo access-check"}'
```

(`%251` is the pane id `%1` URL-encoded — a literal `%` in a path confuses many
clients and proxies. Use a pane id that exists on your machine; `tmux list-panes -a`
prints them.)

A 302/401/403 is correct — the request never reached the daemon.

**A `404` is not a pass.** It means Access let the request through and the *daemon*
answered, rejecting it only because that pane id does not exist. Your terminals are
exposed; a valid pane id would have run the command. Treat 404 exactly like 200: tear the
DNS record down, then debug. This is precisely why you should run the test with a pane id
that really exists — a 404 from a typo'd id looks reassuring and is the opposite.

A `200` means an unauthenticated stranger just ran a command on your machine.

### 7.2 A service token must succeed

```bash
curl -sS https://tmux.example.com/api/version \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"
```

Expect the daemon's JSON. If you get the login HTML instead, your policy action is
`Allow` rather than `Service Auth`, or the token is not selected in that policy.

**This test alone proves nothing.** JSON also comes back when Access is missing entirely —
a wide-open hostname passes it perfectly. It is only meaningful next to 7.1 (no
credentials → challenged) and the negative control below. Run all three, or you have
learned nothing about whether the token is what let you in:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://tmux.example.com/api/version \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: definitely-not-the-secret"
```

A *wrong* secret must be rejected (302/401/403). If a deliberately bogus secret still
returns the daemon's JSON, Access is not in the path at all — treat that exactly like a
200 in 7.1 and take the DNS record down.

### 7.3 Browser / phone

Open `https://tmux.example.com` on the phone. You should get the Access login screen
first — email + PIN, or your IdP — and the PWA only after. Install to the home screen
*after* the first successful login so the PWA starts from an authenticated origin.

For interactive CLI use against the same app, `cloudflared` can carry your user identity
instead of a service token:

```bash
cloudflared access login https://tmux.example.com     # browser round-trip, caches a JWT
cloudflared access curl https://tmux.example.com/api/state
cloudflared access token -app=https://tmux.example.com  # print the JWT
```

### 7.4 WebSocket

Only relevant if you run voice Live Mode: `/api/live-mode` exists **only when the daemon
was started with `TMUXRC_LIVE_MODE=1`** (it is off by default). With Live Mode off the
route is absent, so this test returns 404 *through* a correctly configured Access — which
is a pass for Access and a non-answer about WebSockets. Check `curl -s
localhost:18030/api/version` on the host: `live_enabled` tells you which case you are in.
The everyday screen view uses `GET /api/panes/{id}/live`, an ordinary HTTP long-poll that
needs no WebSocket support at all.

If Live Mode is on, test the socket explicitly rather than assuming. With a service token:

```bash
python3 - <<'EOF'
import os, websockets, asyncio     # pip install websockets
url = "wss://tmux.example.com/api/live-mode"
hdr = {"CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"],
       "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"]}
async def main():
    async with websockets.connect(url, additional_headers=hdr) as ws:
        print("connected:", ws.response.status_code if hasattr(ws, "response") else "ok")
asyncio.run(main())
EOF
```

A successful handshake (HTTP 101) proves Access passed the upgrade through. A 302 to the
login URL means the request was not authenticated; a 403 means it was authenticated and
the policy rejected it — different bugs.

From the phone, the real test is the app: open a pane in Live Mode and confirm output
streams. In DevTools (or Safari's remote inspector) the `/api/live-mode` request should
show status **101 Switching Protocols**.

<a id="websockets-live-mode"></a>
## WebSockets (Live Mode)

Only voice Live Mode (`/api/live-mode`, opt-in via `TMUXRC_LIVE_MODE=1`) uses a
WebSocket; the screen views are HTTP long-polls. If you enable it, `wss://` has to
survive the tunnel *and* Access. It does, on the free plan, and the reason is worth
understanding:

- **WebSockets are supported on all Cloudflare plans, free included.** Verify the
  zone-level toggle is on under **Network → WebSockets** in the dashboard.
- **cloudflared proxies WebSocket upgrades with no extra ingress configuration.** You do
  not need `originRequest: websocket: true` for this. TLS is terminated at the edge, so
  the browser gets `wss://` even though the origin hop is plain HTTP on loopback.
- **Access authenticates the upgrade request like any other HTTP request.** The browser
  sends the `CF_Authorization` cookie on the WebSocket handshake automatically, because
  the handshake *is* an HTTP request to the same origin. This is why an authenticated PWA
  gets a working socket with no special handling.

One consequence worth internalizing: **browser JavaScript cannot set headers on a
WebSocket.** The `WebSocket(url, protocols)` constructor takes no headers — a hard
Web Platform limitation, not a Cloudflare one. So service tokens can authenticate a
WebSocket from `curl`/Python/Go, but **never from the browser**. The browser path is
cookie-based, always. If you find yourself trying to feed `CF-Access-Client-Id` to a
browser socket, the design is wrong: log in normally and let the cookie do it.

Cloudflare closes a WebSocket when no data moves in *either* direction for a while. The
duration is not documented (only Enterprise can configure it), so do not design against a
specific number — Cloudflare's own advice is a client-side ping/pong heartbeat. A voice
session that goes quiet for a long stretch can therefore drop; the client reconnects.
Note also that Argo Smart Routing is not compatible with WebSockets, and Cloudflare's own
code releases terminate open connections.

<a id="session-length-and-phone-re-auth"></a>
## Troubleshooting

**`curl` returns HTML with a Cloudflare login form instead of JSON.**
Working as designed — you are unauthenticated. For scripts, use a service token against a
**Service Auth** policy (an `Allow` policy will ignore the headers), or
`cloudflared access curl`.

**Service token gets 403, browser login works.**
The policy action is `Allow`, not `Service Auth`; or the token is not selected in that
policy; or the secret was truncated when copied. Remember the secret is displayed once —
if in doubt, rotate it.

**`cloudflared` exits immediately: "ingress rules were invalid".**
Almost always the missing catch-all. The last ingress rule must have no `hostname`:
`- service: http_status:404`.

**"Cannot determine default origin certificate path" / "cert.pem not found".**
`cloudflared tunnel login` was never run as the user running the service, or `cert.pem`
lives elsewhere. Pass `--origincert`, or set `TUNNEL_ORIGIN_CERT` in
`~/.config/tmux-rc/tunnel.env`. Under systemd, `%h` is the *service* user's home — if you
ran `login` as your user but the unit runs as another, the paths differ.

**"tunnel credentials file not found."**
The `credentials-file:` path in the YAML must be **absolute**; `~` is not expanded.

**Error 1033 / "Argo Tunnel error."**
The edge has the DNS record but no connector is registered — cloudflared is down or
cannot dial out. `systemctl --user status tmux-rc-tunnel` and
`journalctl --user -u tmux-rc-tunnel`.

**502 from the edge, tunnel connected.**
cloudflared is up but nothing answers on `http://localhost:18030`. Check
`systemctl --user status tmux-rc` and `curl -s localhost:18030/api/version` on the host.
If the daemon was started with `TMUXRC_HOST` set to something other than loopback,
`localhost` may not be where it is listening.

**The site loads but Live Mode never connects.**
Check the `/api/live-mode` request status. **101** = working. **302** = the WebSocket was
not authenticated (usually an expired session; hard-reload). **403** = authenticated but
policy-rejected. **1006/immediate close** with everything else healthy = check the
zone's **Network → WebSockets** toggle.

**Everything works but you are asked to log in constantly.**
Application session duration is too short, or the global session expired. See
[session length](#session-length-and-phone-re-auth).

**You unproxied the DNS record to "fix" something.**
Re-proxy it immediately. A grey-cloud record bypasses Cloudflare, and therefore bypasses
Access, and therefore publishes an unauthenticated keystroke-injection API. Then re-run
the [7.1 check](#71-an-unauthenticated-request-must-be-challenged).

**Two tunnels fighting over one hostname.**
`cloudflared tunnel info <name>` shows registered connectors. A stray foreground
`cloudflared` from step 4, or a root-level `cloudflared service install`, will
round-robin against the systemd unit and produce intermittent 502s. Kill the duplicate.

## References

- [Cloudflare Tunnel — create a locally-managed tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/)
- [Tunnel ingress rules](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)
- [TryCloudflare (quick tunnel) limitations](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- [Add a self-hosted public application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Access policies and actions](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)
- [Service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)
- [One-time PIN](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/)
- [Session management](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/)
- [Authorization cookie](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/)
- [WebSockets](https://developers.cloudflare.com/network/websockets/)
- [Zero Trust plans and pricing](https://www.cloudflare.com/plans/zero-trust-services/)
