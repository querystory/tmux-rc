---
title: Documentation
toc: false
---

Setup guides, architecture, API reference, and engineering notes for tmux-rc.
Search the documentation or choose a topic below.

## Setup and use

{{< cards >}}
  {{< card link="/client-setup/" title="Install the client" subtitle="Optional web app installation on iOS, Android, and desktop." >}}
  {{< card link="/getting-started/" title="Get started" subtitle="Run the service against your existing tmux sessions." >}}
  {{< card link="/iphone-setup/" title="iPhone setup" subtitle="Install the web app, enable notifications, and use Live Mode." >}}
  {{< card link="/agent-setup/" title="Configure your agents" subtitle="Settings that make your agents easier to read and cheaper to watch." >}}
  {{< card link="/deploy/" title="Remote access" subtitle="Reach your machine through an authenticating tunnel or private network." >}}
{{< /cards >}}

## Documentation

The product requirements, architecture, engineering notes, and progress log are built
directly from the Markdown in the repository. Use the search box to find a topic.

{{< cards >}}
  {{< card link="/design/background-and-motivation/" title="Background & motivation" subtitle="New here? What tmux is, and why tmux-rc exists." >}}
  {{< card link="/design/architecture/" title="How it all works" subtitle="An end-to-end tour of the running system, with diagrams." >}}
  {{< card link="/deploy/" title="Reaching it from outside localhost" subtitle="The daemon has no auth — read this before exposing it to anything." >}}
  {{< card link="/agent-setup/" title="Configuring your agents" subtitle="Settings that make your agents readable on the phone — and cheaper to watch." >}}
  {{< card link="/agent-orchestration/" title="Orchestrating agents from an agent" subtitle="Driving agent sessions in other panes — and which of those mechanics fail silently." >}}
  {{< card link="/design/thin-llm-ui-layer/" title="The thin LLM UI layer" subtitle="Affordances that can't be built generically, decided per screen by a cheap model." >}}
  {{< card link="/prd/" title="Product Requirements" subtitle="What we're building and why." >}}
  {{< card link="/requirements/" title="Requirements" subtitle="Source-of-truth checklist of what was asked for." >}}
  {{< card link="/design/" title="Design Notes" subtitle="Architecture and design decisions, with the why behind them." >}}
  {{< card link="/benchmarks/" title="Benchmarks" subtitle="Hot-path classifier latency, cost, and model head-to-heads." >}}
  {{< card link="/hint/" title="Telemetry Hints" subtitle="Guidance for querying tmux-rc telemetry." >}}
  {{< card link="/progress/" title="Progress Log" subtitle="What changed, newest first." >}}
  {{< card link="/api-reference/" title="API Reference" subtitle="HTTP endpoints and the interactive reference on your running daemon." >}}
{{< /cards >}}
