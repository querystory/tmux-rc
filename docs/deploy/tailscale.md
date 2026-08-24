---
title: "Tailscale"
---

# Exposing tmux-rc with Tailscale

Status: **documented, not verified on this host** — the commands below come from
Tailscale's official docs (validated January 2026), not from a run against a live
tailnet. Treat exact output strings as illustrative and the verification steps in
["Prove the auth is real"](#prove-the-auth-is-real) as mandatory.

## Read this first: the daemon has no authentication

tmux-rc binds `127.0.0.1:18030` and **authenticates nobody**. There is no login, no
token, no allowlist. `POST /api/panes/{id}/send` types characters into a real terminal
on your machine, and `/api/live-mode` is a websocket that lets a voice session do the
same. Whatever you put in front of the daemon *is* the entire access-control system.

That reframes the choice below. The question is not "how do I get a URL for my phone",
it's "who is allowed to type into my shell". Tailscale answers it two very different
ways, and only one of them is a real answer.

## Two modes, and they are not equivalent

| | `tailscale serve` | `tailscale funnel` |
| --- | --- | --- |
| Who can reach it | devices in your tailnet only | **anyone on the internet** |
| Authentication | tailnet membership (WireGuard device identity) | **none** |
| Identity forwarded to the app | `Tailscale-User-Login` and friends | no identity headers |
| Public DNS/TLS | tailnet-internal `*.ts.net` name, cert issued for it | public `*.ts.net` name |
| Suitable for tmux-rc | **yes — this is the default** | no, see the warning |

`tailscale serve` is the recommendation for this application, and not as a hedge. Your
phone already runs the Tailscale app; joining it to the tailnet is the login. There is
no public listener to be found by a scanner, no password to leak, no session to steal
from a browser. The exposure is exactly "devices I have enrolled", which is the
smallest surface that still makes the phone dashboard work.

### Why Funnel is the wrong tool here

Funnel publishes the service to the open internet on port 443, 8443, or 10000. It has
**no authentication layer of its own** — Tailscale's own docs distinguish the two modes
precisely on this point: Serve traffic carries identity headers, "Funnel traffic, which
is publicly available, does not include identity headers." There is no
"require login before Funnel" toggle, no identity-aware proxy in front of Funnel as of
early 2026, and no per-request authorization hook. `tailscale funnel 18030` publishes
keystroke injection into your terminals to anyone who guesses or discovers the hostname
— and Funnel hostnames are not secret: they appear in Certificate Transparency logs the
moment the TLS certificate is issued.

So: **do not Funnel this daemon.** If you genuinely need public access (a device you
can't enroll in the tailnet), use a tunnel that can enforce authentication — see
[ngrok](ngrok.md) with a Traffic Policy, or a Cloudflare Tunnel behind Access. Funnel is
appropriate for a static site, not for a remote-control API.

## Setup with `tailscale serve`

**Prerequisites:** Tailscale installed and logged in on the machine running the daemon,
and the Tailscale app installed and logged into the *same tailnet* on your phone.
MagicDNS and HTTPS certificates must be enabled for the tailnet (admin console → DNS).
Serve requires HTTPS; if it isn't enabled the CLI prompts you with a consent page to
turn it on.

Leave the daemon on loopback. Do **not** set `TMUXRC_HOST=0.0.0.0` — Serve connects to
it from the same machine, and keeping the bind on `127.0.0.1` means that even a device
already on your tailnet cannot bypass Serve by hitting port 18030 directly.

```bash
# 1. Confirm the daemon is where you think it is.
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:18030/    # expect 200

# 2. Publish it to the tailnet on HTTPS 443, persistently.
tailscale serve --bg --https=443 http://localhost:18030

# 3. See what you published.
tailscale serve status
```

`--bg` is the flag that matters for an always-on dashboard: without it, `serve` runs in
the foreground and the mapping dies with your shell. With it, the configuration is
stored in `tailscaled`'s state and **automatically resumes after a reboot** or a
`tailscale down` / `tailscale up` cycle.

Your URL is the device's MagicDNS name — `https://<device>.<tailnet>.ts.net` — with a
real, publicly-trusted TLS certificate (Tailscale provisions one for the tailnet DNS
name, so no certificate warnings on the phone and the PWA installs cleanly). Bookmark
that on the phone; it is stable for the life of the device name.

To take it down:

```bash
tailscale serve --https=443 off     # remove this one mapping
tailscale serve reset               # remove every serve mapping on this device
```

### Does it need the repo's systemd tunnel slot? No.

This repo ships [`deploy/systemd/tmux-rc-tunnel.service`](https://github.com/querystory/tmux-rc/blob/main/deploy/systemd/tmux-rc-tunnel.service)
as a vendor-neutral slot for a long-running tunnel *client* process. Tailscale doesn't
need it: `tailscaled` is already its own system service with its own restart semantics,
and the serve mapping lives in that daemon's state rather than in a foreground process
you have to supervise. Adding a unit whose `ExecStart` is `tailscale serve` would be a
second, worse copy of a job `tailscaled` already does.

**So leave `~/.config/tmux-rc/tunnel.env` absent.** The unit has
`ConditionPathExists=%h/.config/tmux-rc/tunnel.env`, so with no env file it stays
cleanly *inactive* rather than failing — exactly the intended behavior for a setup that
doesn't use the slot. `systemctl --user restart tmux-rc.target` will keep working and
will simply skip the tunnel half.

The one thing worth doing is ordering: make sure `tailscaled` is enabled at boot
(`sudo systemctl enable --now tailscaled`) so the serve mapping comes back before you
reach for your phone.

### Restricting *which* tailnet devices can reach it

Tailnet membership is the authentication; ACLs are the authorization. Note that a new
tailnet ships with a **permissive starter policy** that lets every member reach every
device (`"src": ["autogroup:member"], "dst": ["*:*"]`) — the deny-by-default engine only
starts working for you once you narrow that. If your tailnet has devices you don't want
holding a remote control for your terminals (a family member's laptop, a shared server,
anything with a shared auth key), replace the wildcard with a grant scoped to this
service. In the admin console's Access Controls:

```json
{
  "grants": [
    {
      "src": ["you@example.com"],
      "dst": ["tag:tmux-rc-host"],
      "ip":  ["tcp:443"]
    }
  ]
}
```

Tag the daemon host accordingly (`tailscale up --advertise-tags=tag:tmux-rc-host`, after
declaring the tag's owner in the policy file). The effect is that only your own devices
can open the dashboard at all — a stolen bookmark on an enrolled-but-unauthorized device
gets a connection refused at the WireGuard layer, before any HTTP is spoken.

Two related knobs worth knowing about:

- **Device sharing** invites a device from another tailnet. Never share the daemon host;
  sharing hands the recipient the same unauthenticated API you're trying to protect.
- **Tailnet lock**, if you use it, prevents a compromised coordination server from
  injecting a new device into your tailnet. Since tailnet membership *is* the auth here,
  that's the threat model it addresses.

### Identity headers, and what tmux-rc does with them

Serve injects `Tailscale-User-Login`, `Tailscale-User-Name`, and
`Tailscale-User-Profile-Pic` into each proxied request, and — importantly — **strips any
copies of those headers arriving from the client**, so they can't be spoofed.

tmux-rc doesn't read those headers today; its audit trail keys off `X-Tunnel-User`,
which it honors [only from a loopback peer](https://github.com/querystory/tmux-rc/blob/main/daemon/server.py)
(see `_trusted_user`). Since Serve *does* connect from loopback, the audit log will
attribute actions to `local:127.0.0.1` rather than to a person. That's honest — it isn't
claiming an identity it can't prove — and it's fine for a single-user tailnet. If you
share the dashboard with more than one person and want names in the audit log, the small
change is to teach `_trusted_user` to also accept `Tailscale-User-Login` from loopback.

## Prove the auth is real

Don't trust the setup, test it. Three checks, in order of how much they tell you.

**1. The loopback port is still loopback.** From another machine on your LAN (not the
tailnet):

```bash
curl -sS --max-time 5 http://<lan-ip-of-daemon-host>:18030/
```

This must fail to connect. If it returns HTML, `TMUXRC_HOST` is set to `0.0.0.0` and you
have an unauthenticated dashboard on your LAN regardless of what Tailscale is doing.

**2. A non-tailnet client cannot reach the `.ts.net` name.** From a phone on cellular
with the Tailscale VPN toggled **off**, open `https://<device>.<tailnet>.ts.net`. It must
fail to resolve or fail to connect. A page loading here means you ran `funnel`, not
`serve` — run `tailscale funnel status` and turn it off.

**3. The dangerous endpoint specifically.** The read-only dashboard loading is not proof
the write path is protected; test the write path directly. With Tailscale off:

```bash
curl -sS --max-time 5 -X POST \
  https://<device>.<tailnet>.ts.net/api/panes/%1/send \
  -H 'content-type: application/json' -d '{"keys":"echo probe"}'
```

Expected: a connection/DNS failure — not a `200`, and not a `404` either (a `404` means
you reached *something*). Then turn Tailscale on and confirm the same request succeeds
against a pane you own. The pair of results is the proof: reachable with membership,
unreachable without.

**4. Websockets.** Live Mode and the live frame stream ride a websocket at
`/api/live-mode`. Serve is a standard HTTP reverse proxy and forwards the upgrade, but
Tailscale's docs don't call websockets out explicitly, so verify it rather than assume:

```bash
# From a tailnet device, with a websocket client of your choice; wscat shown.
wscat -c wss://<device>.<tailnet>.ts.net/api/live-mode
```

The practical check is simpler: open the dashboard on the phone, tap the Live button,
and confirm the status goes to `listening`. If the connection is refused at the upgrade
you'll see it stall at `connecting`.

## Troubleshooting

**`tailscale serve` says HTTPS isn't enabled.** Enable HTTPS certificates for the tailnet
in the admin console (DNS → HTTPS Certificates). MagicDNS must be on too; the certificate
is issued for the MagicDNS name.

**The mapping disappeared after a reboot.** You almost certainly omitted `--bg`. A
foreground `tailscale serve` ends with the shell that started it. Re-run with `--bg` and
confirm with `tailscale serve status`.

**`502` or connection refused through the `.ts.net` name.** Serve is up but the upstream
isn't: check `systemctl --user status tmux-rc` and
`curl http://localhost:18030/` on the host. Serve does not start your daemon for you.

**Certificate errors on the phone.** The device name changed (renaming a device
invalidates the old name's certificate) or the tailnet's HTTPS feature is off. Re-issue
by re-running the serve command and check `tailscale cert` output.

**Port conflict between Serve and Funnel.** The same port can't carry both at once. If a
`serve` command errors out about the port, run `tailscale funnel status` — you may have
a leftover Funnel on 443. Turn it off.

**Let's Encrypt rate limits.** Repeatedly toggling certificates can lock you out for
hours. Don't script `serve reset` / `serve` loops.

## Where this leaves you

For a personal, always-on phone dashboard over an unauthenticated
keystroke-injection API, `tailscale serve` is the strongest option documented in this
repo: no public listener exists at all, the credential is a device enrollment rather
than a password, and it survives reboots without a supervisor process. Its one real cost
is that every client device must run Tailscale — which for a phone you own is a
one-time install, and for a device you *don't* own is a feature, not a limitation.

See [ngrok](ngrok.md) for the case where you need a public URL and must therefore buy
authentication back with a proxy policy.
