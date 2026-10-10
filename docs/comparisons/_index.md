---
title: Why tmux-rc
weight: 15
---

We built tmux-rc for a workflow we already had: lots of coding agents running in
multiple tmux sessions, across projects, with work continuing while we stepped away.
We wanted to drop a small service onto that host and manage the whole fleet remotely.

We looked at other agent managers and terminal tools. Several solve parts of this
well, but we did not find the combination we wanted: existing-session discovery,
an overview of what needs us, and text/voice control across a mixed fleet, with little
change to how we already launch and run agents.

## The workflow we wanted to keep

Our starting point is a running tmux server. Sessions, windows, and panes already
contain agents, shells, experiments, and long-running work. Some agents were started
by hand; others were started by another agent. Projects have their own branches,
worktrees, scripts, and conventions.

The manager should fit around that work:

- **See what is already running.** Pick up existing panes across multiple tmux
  sessions without importing each conversation or relaunching every agent.
- **Keep our launch habits.** An agent started in a terminal should join the overview
  alongside agents started from the dashboard.
- **Make a large fleet manageable.** Show what needs an answer, what is working,
  what just finished, and what delegated subagents are doing.
- **Let us intervene from anywhere.** Use a desktop or phone to answer, steer, inspect
  a terminal, or talk to the fleet through text and voice.
- **Help keep work moving.** Show subscription limits, usage trends, and projected
  exhaustion so we can decide where to spend the remaining allowance.
- **Keep a mixed fleet together.** Support agents from different vendors and ordinary
  terminal work on the same tmux server; use harness-specific detail where available.

These are the requirements that shaped tmux-rc. The terminal sessions continue to
belong to tmux, and our existing Git and agent workflows remain the starting point.

## What the alternatives showed us

The question we asked was how each tool would fit this existing fleet.

| Alternative | What attracted us | Why we still wanted tmux-rc |
| --- | --- | --- |
| [Conductor](conductor.md) | Parallel workspaces, agent chats, cloud execution, and team collaboration | Its workspace lifecycle is a different starting point from our already-running tmux sessions |
| [T3 Code](t3-code.md) | Remote conversations, provider switching, account quotas, and recovery after limits | It manages provider-backed threads; importing history does not attach to the live terminal fleet |
| [MuxFlow](muxflow.md) | Direct tmux/SSH integration, native terminals, notifications, and voice replies | A close fit for terminal access; we wanted the fleet overview and conversational supervision that shaped tmux-rc |
| [cmux](landscape.md#cmux) | Native terminal workspaces and beta remote tmux mirroring | A useful desktop surface for the same fleet; our focus also includes browser access and fleet-level intervention |
| [Superset and agent-deck](landscape.md) | Parallel agent workspaces and conversational orchestration | Their documented launch/session models differ from automatically observing arbitrary existing panes |
| [Native Claude/Codex controls](landscape.md#first-party-remote-controls) | Deep integration with their own harnesses | We wanted one overview across vendors and terminal work |

We chose the combination that fits our workflow. Other tools also have subagents,
remote clients, usage tracking, and voice features.
MuxFlow and cmux can even provide another view of the same tmux sessions. The
combination and the starting workflow are what mattered to us.

## Drop it onto the host that already runs the work

Run the daemon on your tmux host, open the browser dashboard, and it observes the
configured server's panes. Existing sessions stay in place. The dashboard adds
summaries, attention states, structured replies, subagent activity, a spatial session
atlas, usage history and limit projections, and text/voice controls. Recognition and
available detail vary by harness.

The setup is a small service plus a web client; tmux, your agents, and your execution
host are still prerequisites. Remote access needs an explicit access-control boundary:
**the daemon has no built-in authentication**. Every client allowed to reach it can
read and control terminals. Use an authenticated proxy or identity-controlled tailnet
ACLs that restrict access to the intended users.

See [the architecture](../design/architecture.md),
[agent configuration](../agent-setup.md),
[remote setup](../deploy/_index.md), and
[usage-limit tracking](../design/plan-usage.md).

## The research behind the decision

The detailed pages explain session ownership, remote access, voice, quotas, subagents,
and how to use tools together:

- [Conductor](conductor.md)
- [T3 Code](t3-code.md)
- [MuxFlow](muxflow.md)
- [Broader landscape](landscape.md)

**Research checked: October 10, 2026.** Sources are official docs and public code,
not hands-on trials of every product. “Not established” means the reviewed sources
left a question open. Beta and early-access features are identified separately;
availability and subscription requirements can change.
