# tmux-rc

Watch and control terminal AI agents (Claude Code, Codex, Gemini CLI — or any
program) from your phone. A small local service reads a `tmux` pane, figures out
what's happening, and shows a phone-native dashboard: status at a glance, alerts when
an agent is blocked on a question, tappable answers, and a snapshot timeline.

**Why not `/remote-control`?** Claude Code's remote control is locked to the Anthropic
API (no Bedrock/Vertex) and only drives Claude Code. tmux-rc observes the *terminal*,
so it's vendor-agnostic on both axes — any agent, any model provider for the
summarization pass. See [`docs/PRD.md`](docs/PRD.md) and [`docs/design/overview.md`](docs/design/overview.md).

> **Status: proof of concept.** Milestone 1 (single pane) works end to end:
> watch → classify → phone card → detect a waiting prompt → tap → answer round-trips
> into the pane. Milestone 2 (all panes) and the non-goals in the PRD are next.

## Run

Prereqs: `tmux`, Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and Google Cloud
credentials for Vertex (the Gemini Flash Lite classification pass). Because the daemon
runs unattended for long stretches, it authenticates with a long-lived **service-account
key** rather than developer ADC (which expires and needs a browser reauth — see
[docs/design/durable-vertex-auth.md](docs/design/durable-vertex-auth.md)). Point
`GOOGLE_APPLICATION_CREDENTIALS` at the key file — an **absolute** path, since google-auth
does not expand `~` (`.env.example` has the mint command).

```bash
uv sync
cp .env.example .env          # then edit .env: GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS

# In another terminal, start a tmux session and run an agent in it:
tmux new -s work
#   ... run claude / codex / gemini / anything ...

# Start tmux-rc (config is loaded from .env automatically):
uv run python -m daemon.server
```

The daemon binds `127.0.0.1:18030` by default. To browse it from a phone on the same
network, set `TMUXRC_HOST=0.0.0.0` and open `http://<machine-lan-ip>:18030` — but note
the API is **unauthenticated** and `/send` injects keystrokes into your terminals, so
never do that on an untrusted network. For access off your LAN, front it with a tunnel
you control (e.g. `cloudflared`, `tailscale`).

### Run it as a service

Long term you don't want the daemon living in a terminal that might close — it should
survive reboots and restart itself if it dies. `systemd --user` units for the daemon and
the tunnel client ship in [`deploy/systemd/`](deploy/systemd/):

```bash
make install-units                        # copy units, enable + start them, enable-linger
journalctl --user -fu tmux-rc             # daemon logs (replaces watching a pane)
systemctl --user restart tmux-rc          # after a git pull or a .env change
systemctl --user restart tmux-rc.target   # both halves (daemon + tunnel)
```

`make install-units` stamps the unit's `WorkingDirectory` with the checkout you run it
from, so it works wherever you cloned. On a host with an encrypted `$HOME`, install with
`make install-units LINGER=0`: lingering units would crash-loop before you log in.

**Exposing it off-LAN** is optional and yours to choose — this repo doesn't ship a tunnel.
`tmux-rc-tunnel.service` is a slot for whichever reverse-tunnel client you use
(cloudflared, tailscale funnel, frp, something in-house): point its `ExecStart` at the
binary, put the client's config in `~/.config/tmux-rc/tunnel.env` (see
[`deploy/tunnel.env.example`](deploy/tunnel.env.example)), then
`systemctl --user start tmux-rc-tunnel`. The unit stays inactive until that file exists,
so you can ignore it entirely on a LAN-only setup. Whatever you pick should
**authenticate** — the daemon itself has no auth.

The checkout *is* the deploy — the unit runs this directory and loads its `.env`, so
upgrading is `git pull` + `restart`. For iterating on the daemon itself, stop the unit
and run `make dev` in a pane as usual; the two modes share the same command and config.
See [docs/design/deployment.md](docs/design/deployment.md) for why user units + linger
(and not containers, system units, or a supervising parent).

### Run without cloning

`uv` installs straight from the (private) git repo — no manual clone or checkout to
manage. The wheel bundles the phone UI, so this is fully self-contained (`tmux-rc` is a
console script; reload defaults off when installed). You still need `tmux`, a running
agent, a Vertex service-account key file **on the machine**, and the env vars pointing at
it — `uvx` fetches the code, not your credentials.

```bash
uvx --from "git+ssh://git@github.com/querystory/tmux-rc.git" tmux-rc
# or pin a branch/tag/commit:
uvx --from "git+ssh://git@github.com/querystory/tmux-rc.git@main" tmux-rc
```

Requires SSH access to the `querystory` org. The `docs/` site is checkout-only (not
bundled); the phone dashboard and API work without it.

**Setting the env vars.** There's no repo-root `.env` here, so use one of (real shell env
vars always win — `.env` never overrides them):

```bash
# 1. Exported shell env vars (simplest, persists across runs in the shell):
export GOOGLE_CLOUD_PROJECT=your-gcp-project
export GOOGLE_APPLICATION_CREDENTIALS=/abs/path/to/sa-key.json   # absolute — no ~
uvx --from "git+ssh://git@github.com/querystory/tmux-rc.git" tmux-rc

# 2. Inline, for a one-off run:
GOOGLE_CLOUD_PROJECT=your-gcp-project \
GOOGLE_APPLICATION_CREDENTIALS=/abs/path/to/sa-key.json \
  uvx --from "git+ssh://git@github.com/querystory/tmux-rc.git" tmux-rc

# 3. A .env in (or above) the directory you launch from — the daemon searches upward
#    from the cwd when there's no repo-root .env:
mkdir -p ~/tmux-rc && cd ~/tmux-rc
# create .env with GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS (see the table
# below and `.env.example` in the repo)
uvx --from "git+ssh://git@github.com/querystory/tmux-rc.git" tmux-rc
```

> `GOOGLE_APPLICATION_CREDENTIALS` **must be an absolute path** — google-auth does not
> expand `~`. To run heuristics-only with no Vertex creds at all, set `TMUXRC_NO_LLM=1`
> (see the table below). All other `TMUXRC_*` vars work the same way.

### Config (env)

Loaded from `.env` at startup (real shell env vars still override). See `.env.example`.

| var | default | meaning |
| --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project for Vertex (required for the LLM pass) |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | absolute path to the Vertex service-account key (durable auth; see `.env.example`) |
| `VERTEX_AI_REGION_GEMINI` | `global` | Vertex region |
| `TMUXRC_TARGET` | first pane | pane id (`%3`) or `session:window` to watch |
| `TMUXRC_HOST` / `TMUXRC_PORT` | `127.0.0.1` / `18030` | HTTP bind |
| `TMUXRC_NO_LLM` | unset | set `1` to run heuristics-only (no Vertex calls) |
| `TMUXRC_LAUNCHERS` | Claude/Codex/Gemini | dock "+" menu entries — inline JSON or a path to a JSON file: `[{"label":"Claude (Fable)","command":"claude --model fable","icon":"claude"}, …]`; `icon` is a built-in logo name (claude/codex/gemini/shell) or an image URL |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/gRPC receiver for per-parse benchmark telemetry; unset = telemetry off |
| `OTEL_EXPORTER_OTLP_HEADERS` | — | e.g. `authorization=Bearer <token>` for the receiver |
| `TMUXRC_QSDEBUG` | unset | set `1` to also send raw pane text + model output JSON (privacy: content leaves the host) |

### What it costs

Measured, not estimated — from a month of per-call telemetry on a real fleet (45 panes
across 3 hosts, agents running most of the day), at Gemini 3.1 Flash Lite list prices
($0.25/M input, $1.50/M output):

- **~$5/day for the whole fleet** (~$33/week). A classify call is ~10.5k tokens in /
  ~200 out (≈$0.003); calls fire only when a pane's content actually changes, so cost
  scales with how busy your agents are, not with pane count — idle panes are free.
- Voice (Live Mode) is billed per session and has been immaterial next to the
  classifier (a few dollars per month of daily use).
- A lightly used single-pane setup runs pennies per day. `GET /api/state` reports
  running totals, and the OTLP telemetry (above) records per-call tokens/cost if you
  want the real queryable ledger.

## API

The daemon serves the phone's PWA and a small HTTP API on `:18030` — usable by anything
(curl, scripts, agents), not just the phone:

| Endpoint | What it gives you |
|---|---|
| `GET /api/digest` | **Start here for "what's going on":** per pane — headline, activity (`running`/`waiting`/`idle`), idle time, pending question, LLM idle-summary, recent timestamped event history |
| `GET /api/state` | The phone's live view: current cards, usage totals, `llm_error`. `events` carries only what's NEW since the last parse — history lives in `/api/digest` |
| `GET /api/panes/{id}/snapshots` (+ `/{snap_id}`) | Raw screen captures over time |
| `POST /api/panes/{id}/send`, `/select`, `/image` | Act on a pane (these exist for the phone's audited path; local consumers can equally use `tmux send-keys` directly) |

## How it works

- **`tmux.py`** — non-disruptive `capture-pane` reads + `send-keys` writes.
- **`classify.py`** — heuristics first (tool, activity, context %, y/n & menu prompts);
  a lazy Gemini Flash Lite pass fills gaps only when the pane changed and heuristics
  are weak (empty status, unparsed prompt, or a box-drawing TUI frame).
- **`watcher.py`** — polls every 1.5s, tracks idle time, keeps a snapshot ring buffer.
- **`server.py`** — FastAPI: `/api/state`, snapshot endpoints, `/api/panes/{id}/send`.
- **`web/`** — installable vanilla-JS PWA.
