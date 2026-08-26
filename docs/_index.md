---
title: tmux-rc docs
toc: false
---

Vendor-agnostic phone control plane for terminal AI agents running in tmux.

The engineering docs — product requirements, architecture and design notes, how to
reach the daemon safely from outside localhost, and the running progress log.

{{< cards >}}
  {{< card link="design/background-and-motivation/" title="Background & motivation" subtitle="New here? What tmux is, and why tmux-rc exists." >}}
  {{< card link="design/architecture/" title="How it all works" subtitle="An end-to-end tour of the running system, with diagrams." >}}
  {{< card link="deploy/" title="Reaching it from outside localhost" subtitle="The daemon has no auth — read this before exposing it to anything." >}}
  {{< card link="design/thin-llm-ui-layer/" title="The thin LLM UI layer" subtitle="Affordances that can't be built generically, decided per screen by a cheap model." >}}
  {{< card link="prd/" title="Product Requirements" subtitle="What we're building and why." >}}
  {{< card link="requirements/" title="Requirements" subtitle="Source-of-truth checklist of what was asked for." >}}
  {{< card link="design/" title="Design Notes" subtitle="Architecture and design decisions, with the why behind them." >}}
  {{< card link="benchmarks/" title="Benchmarks" subtitle="Hot-path classifier latency measurements." >}}
  {{< card link="hint/" title="Telemetry Hints" subtitle="Guidance for querying tmux-rc telemetry." >}}
  {{< card link="progress/" title="Progress Log" subtitle="What changed, newest first." >}}
  {{< card link="/apidocs" title="API Reference" subtitle="Live Swagger UI for the daemon's HTTP API." >}}
{{< /cards >}}
