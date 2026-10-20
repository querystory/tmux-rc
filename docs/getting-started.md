---
title: Get started
weight: 1
---

tmux-rc runs on the machine where your tmux sessions live. Open its web app on your
desktop or phone to see what your agents are doing and answer them through chat or
direct dashboard controls.

## Try it without a model account

Install tmux, Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).
Keep an existing tmux session running, or start one and launch your preferred agent.
Then run:

```bash
TMUXRC_NO_LLM=1 uvx --from "git+https://github.com/querystory/tmux-rc.git" tmux-rc
```

Open `http://127.0.0.1:18030/m` on that machine. This mode uses heuristics for basic
activity and prompt detection; richer summaries require the model pass.

## Enable summaries

The default classifier uses Gemini through Vertex AI. Configure a Google Cloud project
and durable credentials as described in [Vertex authentication](design/durable-vertex-auth.md).
For an installed service, supply these environment variables:

```bash
GOOGLE_CLOUD_PROJECT=your-project \
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json \
  uvx --from "git+https://github.com/querystory/tmux-rc.git" tmux-rc
```

Pane content is sent to the classifier provider. Classification calls are metered;
the dashboard reports usage. `TMUXRC_NO_LLM=1` disables that pass.

For configuration, service installation, and development from a checkout, see the
[repository README](https://github.com/querystory/tmux-rc#run).

## Connect your phone

The daemon binds to loopback and has no built-in login, so run it only on a machine you
alone use: every other account on the host can reach a loopback port. Put an authenticating front end
in front of it before making it reachable remotely. Follow the [remote access guides](deploy/_index.md)
for Tailscale, Cloudflare Tunnel with Access, and other options.

On iPhone, follow [iPhone setup](iphone-setup.md) to install the PWA, enable push
notifications, and configure Live Mode. [Agent setup](agent-setup.md) covers optional
agent settings that improve the dashboard's observations.

## Move to another machine

`tmux-rc export` writes one `.tgz` with everything another machine needs: the history
database (pane history, plan usage, and the tmux layout, which the new machine then offers
back as a workspace to restore), accounts, mocks, scratch pages, and every `~/.claude*`
and `~/.codex*` dir, so agents can resume their conversations. It runs while the daemon
does. On the new machine, clone the checkout, then:

1. Copy the archive over, and stop tmux-rc there if it runs: `systemctl --user stop tmux-rc`.
2. `uv run tmux-rc import tmux-rc-export-<host>-<date>.tgz`. Whatever it replaces is kept
   beside it as `*.pre-import-<time>`, and paths under the old `$HOME` are rewritten.
3. `make install-units`, and `make docs` if you serve the docs.

Credentials stay out unless asked for: the agents' logins, `~/.claude.json`, the push keys,
`~/.config/tmux-rc` and the checkout's `.env`. Without them, sign in again on the new
machine. `--with-secrets` adds them, and the file then holds every sign-in and key, so keep
it private, or move it in one pipe that never leaves a file behind: `uv run tmux-rc export
--with-secrets -o - | ssh newhost 'cd tmux-rc && uv run tmux-rc import -'`. `--no-scratch`
and `--no-agents` make the archive smaller.

Settings › **Export all…** makes the same archive from the app: tick the parts to include,
each shown with its size before compression, and download.

## What this website serves

This is the public project website and documentation. Your live dashboard and terminal
controls are served by your own daemon at `/m`; no running agent sessions are hosted here.
