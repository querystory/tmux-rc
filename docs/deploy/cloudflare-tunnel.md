# Exposing tmux-rc with Cloudflare Tunnel + Access

A runbook for reaching your daemon from a phone on the open internet, with
authentication in front of it. Cloudflare Tunnel is one of several ways to fill the
`tmux-rc-tunnel.service` slot (see [Run it as a service](../../README.md#run-it-as-a-service));
this doc covers it end to end because it is the option most people can stand up in an
afternoon with no public IP, no open ports, and no paid plan.

If you only ever use tmux-rc on your LAN, you do not need any of this. Skip it.

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
hands them the scrollback (secrets, tokens, whatever was on screen) on the way.
The WebSocket at `/api/live-mode` streams your terminals live.

So: **a tunnel without an authentication layer publishes a remote-code-execution API to
the internet.** Not "a risk"; the literal function of the endpoint. Obscure hostnames do
not help — `*.trycloudflare.com` names and Certificate Transparency logs for your own
domain are both crawled continuously, and an unauthenticated hit is all it takes.

The tunnel is not the security boundary. **Cloudflare Access is.** Everything below
exists to put Access in front of the tunnel, and the [verification](#7-verify-that-auth-actually-works)
section exists so you can prove it is really there before you trust it.

> **Quick tunnels (`--url`, `*.trycloudflare.com`) cannot be protected by Access.**
> Access applications are defined against a hostname in a Cloudflare zone you control;
> `trycloudflare.com` is not your zone, so there is nowhere to attach a policy. Cloudflare
> documents quick tunnels as "intended for testing and development only," with no
> authentication, a 200 in-flight request cap, and no Server-Sent Events. **Never point a
> quick tunnel at tmux-rc.** Every step below uses a *named* tunnel on your own domain,
> which is the only shape Access can protect.

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
authentication. Use `--overwrite-dns` only if you know an old record is in the way.

Confirm what exists (both are read-only):

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

Smoke-test it in the foreground before wiring up systemd:

```bash
cloudflared tunnel --config ~/.cloudflared/tmux-rc.yml run tmux-rc
```

Load `https://tmux.example.com` — you should get the PWA. **At this moment the daemon is
publicly reachable with no authentication.** Do not walk away from this step; go
straight to section 6, or Ctrl-C until you are ready to.

## 5. Run it from the repo's systemd unit

This repo ships [`deploy/systemd/tmux-rc-tunnel.service`](../../deploy/systemd/tmux-rc-tunnel.service)
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
exec "$(command -v cloudflared)" --no-autoupdate \
  tunnel --config "$HOME/.cloudflared/tmux-rc.yml" run
EOF
chmod +x ~/.local/bin/tunnel-client
```

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

## 6. Put Cloudflare Access in front of it

This is the step that makes the whole thing safe. Everything before it just made your
terminals reachable.

### Enable an identity method

In the Cloudflare dashboard: **Zero Trust → Settings → Authentication → Login methods**.

New Zero Trust organizations get the **Cloudflare identity provider** as the default
login method. For a personal single-user install, **One-time PIN** is the simplest
option: no external IdP, no app registration. Access emails a code to the address the
visitor types; the PIN expires 10 minutes after it is requested and is single-use.
OTP is no longer added automatically to new organizations, so add it explicitly under
**Login methods → Add new → One-time PIN** if you want it.

Google, GitHub, Microsoft Entra, Okta, and the rest of the OIDC/SAML catalogue are also
available on the free plan. Social/OIDC login (e.g. GitHub) is worth the extra five
minutes if you have it — it re-authenticates in one tap rather than an email round-trip,
which is the difference between pleasant and irritating on a phone.

### Create the application

**Zero Trust → Access controls → Applications → Add an application → Self-hosted and
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

- **Cloudflare Zero Trust free plan: up to 50 users, no credit card, no time limit.**
  Access applications, policies, One-time PIN, and the standard OIDC/SAML identity
  providers are all included.
- **Pay-as-you-go is $7/user/month**, which removes the 50-seat cap and adds an uptime
  SLA and 30-day log retention (free is 24-hour retention).
- The relevant cliff is the **50-seat limit**: past 50 active seats, additional users are
  blocked from authenticating until you upgrade. For a personal or small-team tmux-rc
  install this is not a constraint you will ever touch.
- Cloudflare's Service-Specific Terms also set an average of 150,000 DNS queries per seat
  per month; likewise irrelevant at this scale, but it exists.

So the honest summary: **there is no paywall in front of the authentication you need
here.** If a guide tells you otherwise, it is out of date. Pricing does change — confirm
against [Cloudflare's plans page](https://www.cloudflare.com/plans/zero-trust-services/)
before you build a budget on it.

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

A 302/401/403 is correct. A 200 means an unauthenticated stranger just ran a command on
your machine — and you should tear the DNS record down before debugging.

### 7.2 A service token must succeed

```bash
curl -sS https://tmux.example.com/api/version \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"
```

Expect the daemon's JSON. If you get the login HTML instead, your policy action is
`Allow` rather than `Service Auth`, or the token is not selected in that policy.

A useful negative control — a *wrong* secret must fail:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://tmux.example.com/api/version \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: definitely-not-the-secret"
```

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

The Live Mode socket is the piece most likely to be silently broken, so test it
explicitly rather than assuming. With a service token:

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

tmux-rc's Live Mode and terminal streaming need `wss://` to survive the tunnel *and*
Access. It does, on the free plan, and the reason is worth understanding:

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

Cloudflare closes WebSockets that are idle in both directions. tmux-rc's live stream is
chatty enough that this rarely bites, but a pane that produces no output for a long
stretch can drop; the client reconnects.

<a id="session-length-and-phone-re-auth"></a>
## Session length and phone re-auth

You will check this from a phone all day, so re-auth friction is a real design decision,
not a detail. Access has three nested durations:

| Duration | What it controls | Default | Range |
|---|---|---|---|
| **Global session** | How often you must log in to the *identity provider* | 24 hours | 15 min – 1 month |
| **Application session** | How long this app's token is good for | 24 hours | immediate – 1 month |
| **Policy session** | Per-policy override of the application default | inherits app | immediate – 1 month |

Two cookies implement it: a **global** session token on your `<team>.cloudflareaccess.com`
domain, and a per-application **`CF_Authorization`** cookie on the protected hostname.
When the application token expires, Access silently issues a new one as long as the
global token is still valid and you still match the policy — no prompt. You only see a
login screen when the *global* session has also expired.

Practical settings for a phone:

- **Application session: 1 week or 1 month.** This is the number that decides how often
  the PWA bounces you to a login screen.
- **Global session: 1 month** if you accept that a stolen unlocked phone means a stolen
  session; **24 hours** (the default) if you would rather re-auth daily. This is a real
  tradeoff, and there is no universally right answer — it comes down to whether your
  phone's own lock screen is a boundary you trust.
- Do **not** set "immediate timeout" and then wonder why Live Mode reconnects constantly.
- Enable **binding cookie** (`CF_Binding`) under the application's cookie settings for
  defence in depth: it ties `CF_Authorization` to that application so a stolen cookie
  alone cannot be replayed.

An expired session is also the most confusing failure mode in the PWA: the page is
already loaded, so instead of a visible login screen you see API calls quietly failing
and Live Mode refusing to connect. A hard reload surfaces the Access redirect.

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

## Where this leaves you

Access is authenticating *at the edge* — requests are rejected before they reach your
machine, which is strictly better than an application-level check the daemon does not
have anyway. What Access does **not** give you is per-user authorization inside tmux-rc:
everyone who passes the policy gets the full API, including `/send`. Keep the allow-list
to people you would hand an unlocked laptop.

The daemon still has no authentication of its own. If you later move off Cloudflare, that
fact does not change — whatever replaces this must authenticate too. That is why
`tmux-rc-tunnel.service` is a slot with a warning attached rather than a shipped default.

For defence in depth, the daemon receives Cloudflare's signed `Cf-Access-Jwt-Assertion`
header on every authenticated request, and could verify it to reject anything arriving by
another path. tmux-rc does not do this today — worth knowing the hook exists if you want
belt and braces.

## References

- [Cloudflare Tunnel — create a locally-managed tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-local-tunnel/)
- [Tunnel ingress rules](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/local-management/ingress/)
- [TryCloudflare (quick tunnel) limitations](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- [Add a self-hosted public application](https://developers.cloudflare.com/cloudflare-one/applications/configure-apps/self-hosted-public-app/)
- [Access policies and actions](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)
- [Service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)
- [One-time PIN](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/)
- [Session management](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/)
- [Authorization cookie](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/)
- [WebSockets](https://developers.cloudflare.com/network/websockets/)
- [Zero Trust plans and pricing](https://www.cloudflare.com/plans/zero-trust-services/)
