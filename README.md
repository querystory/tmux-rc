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
key** rather than developer ADC (which expires and needs a browser reauth). Point
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

Open `http://<machine-lan-ip>:8080` on your phone and add it to your home screen.
The daemon injects keystrokes into your terminals and has **no built-in auth**, so never
expose it unauthenticated. For access beyond localhost/LAN, put it behind an
authenticated tunnel or proxy — see [SETUP.md](SETUP.md) for concrete options
(SSH port-forward, Cloudflare Tunnel + Access, ngrok OAuth, Google IAP, Tailscale).

### Config (env)

Loaded from `.env` at startup (real shell env vars still override). See `.env.example`.

| var | default | meaning |
| --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project for Vertex (required for the LLM pass) |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | absolute path to the Vertex service-account key (durable auth; see `.env.example`) |
| `VERTEX_AI_REGION_GEMINI` | `global` | Vertex region |
| `TMUXRC_TARGET` | first pane | pane id (`%3`) or `session:window` to watch |
| `TMUXRC_HOST` / `TMUXRC_PORT` | `0.0.0.0` / `8080` | HTTP bind |
| `TMUXRC_NO_LLM` | unset | set `1` to run heuristics-only (no Vertex calls) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/gRPC receiver for per-parse benchmark telemetry; unset = telemetry off |
| `OTEL_EXPORTER_OTLP_HEADERS` | — | e.g. `authorization=Bearer <token>` for the receiver |
| `TMUXRC_QSDEBUG` | unset | set `1` to also send raw pane text + model output JSON (privacy: content leaves the host) |

## API

The daemon serves the phone's PWA and a small HTTP API on `:8080` — usable by anything
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
