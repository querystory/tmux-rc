# termiphone

Watch and control terminal AI agents (Claude Code, Codex, Gemini CLI — or any
program) from your phone. A small local service reads a `tmux` pane, figures out
what's happening, and shows a phone-native dashboard: status at a glance, alerts when
an agent is blocked on a question, tappable answers, and a snapshot timeline.

**Why not `/remote-control`?** Claude Code's remote control is locked to the Anthropic
API (no Bedrock/Vertex) and only drives Claude Code. termiphone observes the *terminal*,
so it's vendor-agnostic on both axes — any agent, any model provider for the
summarization pass. See [`docs/PRD.md`](docs/PRD.md) and [`docs/DESIGN.md`](docs/DESIGN.md).

> **Status: proof of concept.** Milestone 1 (single pane) works end to end:
> watch → classify → phone card → detect a waiting prompt → tap → answer round-trips
> into the pane. Milestone 2 (all panes) and the non-goals in the PRD are next.

## Run

Prereqs: `tmux`, Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and Google
Application Default Credentials on the host (for the Gemini Flash Lite classification
pass via Vertex).

```bash
uv sync
cp .env.example .env          # set GOOGLE_CLOUD_PROJECT (ADC must be available)

# In another terminal, start a tmux session and run an agent in it:
tmux new -s work
#   ... run claude / codex / gemini / anything ...

# Start termiphone (reads env from your shell):
GOOGLE_CLOUD_PROJECT=<project> uv run python -m termiphone.server
```

Open `http://<machine-lan-ip>:8080` on your phone and add it to your home screen.
For access off your LAN, front it with a tunnel you control (e.g. `cloudflared`,
`tailscale`) — the PoC has **no auth**, so never expose it on an untrusted network.

### Config (env)

| var | default | meaning |
| --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project for Vertex (required for the LLM pass) |
| `VERTEX_AI_REGION_GEMINI` | `global` | Vertex region |
| `TERMIPHONE_TARGET` | first pane | pane id (`%3`) or `session:window` to watch |
| `TERMIPHONE_HOST` / `TERMIPHONE_PORT` | `0.0.0.0` / `8080` | HTTP bind |
| `TERMIPHONE_NO_LLM` | unset | set `1` to run heuristics-only (no Vertex calls) |

## How it works

- **`tmux.py`** — non-disruptive `capture-pane` reads + `send-keys` writes.
- **`classify.py`** — heuristics first (tool, activity, context %, y/n & menu prompts);
  a lazy Gemini Flash Lite pass fills gaps only when the pane changed and heuristics
  are weak (empty status, unparsed prompt, or a box-drawing TUI frame).
- **`watcher.py`** — polls every 1.5s, tracks idle time, keeps a snapshot ring buffer.
- **`server.py`** — FastAPI: `/api/state`, snapshot endpoints, `/api/panes/{id}/send`.
- **`web/`** — installable vanilla-JS PWA.
