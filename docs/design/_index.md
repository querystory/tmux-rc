---
title: Design Notes
---

Architecture and design decisions for tmux-rc — the *why* behind the code, with
tradeoffs weighed and alternatives considered. Some are implemented; many are drafts
capturing the design space before code exists (each notes its status at the top).

New here? Start with the [Design Overview](overview.md) for the guiding constraints,
then [How it all works](architecture.md) for the end-to-end mechanics. The individual
notes below go deep on each piece.

Self-hosting? See [SETUP.md](../../SETUP.md) for how to expose the daemon safely
(localhost/SSH, authenticated tunnels, IAP, Tailscale). Production/internal deployment
runbooks (systemd units, durable service-account auth, the tunnel relay and its
infrastructure) live in a separate internal ops repo, not here — they are specific to one
organization's cloud.
