---
title: Agent manager comparisons
weight: 15
---

Choose an agent manager by how it fits your running work: where agents execute,
which sessions it can see, how you intervene remotely, and how much workflow it owns.
File editors and diff viewers are useful to some users, but they do not decide whether
a product is a good fleet manager.

**Research checked: October 10, 2026.** These comparisons use official documentation
and public source, rather than hands-on trials of every product. “Not established”
means the reviewed sources did not answer the question; it does not mean unsupported.
Features, availability, and subscription requirements can change.

## Start with session ownership

| Product | Unit of work | Existing tmux fleet | Remote access |
| --- | --- | --- | --- |
| tmux-rc | Running panes and their agent activity | Observes and controls the configured tmux server | Browser/PWA behind an authenticated proxy or identity-controlled tailnet ACLs |
| [MuxFlow](muxflow.md) | tmux sessions, windows, panes, and agent hooks | Direct tmux integration; attention tracking needs supported hooks | Native clients and SSH hosts |
| [T3 Code](t3-code.md) | Provider-backed conversations on environment servers | No fleet discovery found; can import Claude/Codex conversation history | Desktop, web, iOS, Android; relay, pairing, Tailscale, SSH |
| [Conductor](conductor.md) | Local worktrees or cloud workspaces with agent chats | Adoption of running tmux panes not established | Mac app; iOS for cloud workspaces; cloud API |

Sources and qualifications are on each product's page. See the
[broader landscape](landscape.md) for Superset, cmux, agent-deck, and Vibe Kanban.

tmux-rc has no built-in authentication. Network privacy alone is not an access
boundary: every client allowed to reach the daemon can read and control its terminals.
Use an authenticated proxy or tailnet identity/ACL rules that restrict access to the
intended users. See [deployment guidance](../deploy/_index.md).

## Which workflow fits?

These are our assessments, not vendor claims:

- **Keep a running terminal fleet:** start with tmux-rc, MuxFlow, and agent-deck.
  Check whether the tool observes existing sessions or expects to launch new ones.
- **Start tasks in a managed conversation UI:** compare T3 Code, Conductor, and
  Superset. Worktree setup, structured approvals, queues, and PR lifecycle matter here.
- **Use a terminal as your main workspace:** compare cmux and MuxFlow alongside
  tmux-rc. A terminal workspace and a fleet control plane can serve different needs.
- **Run work while your laptop is off:** distinguish a daemon on your own always-on
  host from a provider's managed cloud execution. A phone client alone supplies neither.

tmux-rc's current capabilities include pane summaries and attention states,
structured replies, subagent activity, a spatial session atlas, usage history and
subscription-limit projections, and text/voice control. Agent recognition and detail
vary by harness. tmux remains responsible for the underlying terminal sessions.
See [the architecture](../design/architecture.md),
[agent configuration](../agent-setup.md), and
[plan usage](../design/plan-usage.md).

Subagent visibility, remote clients, and usage tracking also exist in competing
products. Do not describe them as exclusive to tmux-rc. The stronger distinction is
the combination of existing fleet access, attention management, and conversational
control without requiring every task to begin inside a new workspace manager.

## Use two tools or hand work over

Using separate tools for separate tasks is straightforward. Use separate worktrees
when agents may edit concurrently. Sharing credentials or a repository does not imply
that two managers synchronize approvals, queues, or live provider state.

MuxFlow and tmux-rc can address the same tmux server, so they can provide different
views of the same panes. Session deletion, renames, and replies affect both views;
avoid submitting duplicate instructions from both clients.

T3's conversation import is a continuation path, not attachment to the running tmux
process. Conductor's workspace model likewise does not establish a general tmux
handoff. For a trial, finish or stop the current turn, keep the original session
available, and verify which manager owns the next turn before sending it.

## Keep the comparisons current

When updating a page, record the research date and link the exact feature docs or
implementation. Recheck phone/cloud availability, supported harnesses, session import,
voice behavior, and subscription limits. Separate announced or early-access features
from generally available ones. Keep popularity signals in the landscape page:
GitHub stars, vendor-reported users, downloads, and active users measure different things.
