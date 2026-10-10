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

The daemon binds to loopback and has no built-in login. Put an authenticating front end
in front of it before making it reachable remotely. Follow the [remote access guides](deploy/_index.md)
for Tailscale, Cloudflare Tunnel with Access, and other options.

On iPhone, follow [iPhone setup](iphone-setup.md) to install the PWA, enable push
notifications, and configure Live Mode. [Agent setup](agent-setup.md) covers optional
agent settings that improve the dashboard's observations.

## What this website serves

This is the public project website and documentation. Your live dashboard and terminal
controls are served by your own daemon at `/m`; no running agent sessions are hosted here.
