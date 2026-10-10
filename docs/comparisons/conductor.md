---
title: Conductor
weight: 10
---

**Checked October 10, 2026.** This page covers
[Conductor by Melty Labs](https://www.conductor.build/), not unrelated projects
with the same name. Research uses official docs; we have not run the app for this comparison.

## What it manages

Conductor organizes tasks into workspaces with chats, terminals, branches, and a
review path. Its documented agents are Claude Code, Codex, Cursor, and OpenCode.
Local workspaces use Git worktrees; cloud workspaces run in isolated Linux sandboxes.
[Introduction](https://www.conductor.build/docs),
[worktrees](https://www.conductor.build/docs/concepts/git-worktrees),
[cloud model](https://www.conductor.build/docs/cloud).

Its local model shares the repository's Git backing while giving each workspace
separate files. This is development isolation, not a security sandbox. Cloud execution
has a different boundary, and must not be conflated with the local permissions model.
[Worktree boundaries](https://www.conductor.build/docs/concepts/git-worktrees).

## Remote access is now a real part of the product

Conductor released iOS on October 2, 2026: chat with agents and merge PRs from a
phone, **for cloud workspaces**. That announcement does not establish phone attachment
to an arbitrary local Mac session. Cloud work continues independently of the Mac,
subject to the sandbox's lifecycle.
[iOS release](https://www.conductor.build/changelog/0.90.0-conductor-for-ios),
[local versus cloud](https://www.conductor.build/docs/cloud).

Its cloud API can create workspaces, send prompts, and read responses. An MCP interface
also exposes cloud workspace operations. This makes integration with another agent
manager plausible; it is not evidence of tmux session discovery.
[API documentation](https://www.conductor.build/docs/api).

## Parallel agents and attention

Conductor explicitly supports both independent workspaces and multiple agents sharing
one workspace. Separate workspaces suit independently landing branches; shared
workspaces suit implementation and review of the same files. Shared files can still
receive conflicting edits.
[Parallel-agent workflow](https://www.conductor.build/docs/concepts/parallel-agents).

Controls differ by harness: the documentation distinguishes plan mode, reasoning,
checkpoints, skills, and Codex goals. Claude's experimental agent teams can also be
enabled. Multiple chat tabs, harness subagents, and teams are related capabilities,
but should not be counted as identical fleet views.
[Agent modes](https://www.conductor.build/docs/concepts/agent-modes),
[agent-team configuration](https://www.conductor.build/docs/faq).

## Compared with tmux-rc

| Decision | Conductor | tmux-rc |
| --- | --- | --- |
| Start a task | Create or select a managed workspace/chat | Use a tmux pane; observe work already running |
| Execute remotely | Managed cloud workspace | Your configured tmux host |
| Review and ship | Workspace, diff, checks, and PR workflow | Keep your existing terminal and Git workflow |
| Inspect existing tmux panes | Not established in reviewed docs | Core operation |
| Conversational fleet voice / pooled quota forecasting | Not established by this research | Text/voice controls and plan-usage projections are implemented |

**Why we built tmux-rc:** Conductor offers a strong workspace lifecycle, cloud
execution, and team collaboration. Our starting point was a busy tmux server with
multiple sessions already running. We wanted to add remote supervision around that
fleet, while keeping our existing launch and terminal habits. Conductor also has
remote access; the difference that matters to us is how work enters the manager.

## Try alongside tmux-rc

Run a new task in a separate Conductor workspace and leave existing tmux agents under
tmux-rc. Git changes can travel through ordinary branches and PRs; live conversations,
approvals, and queues should be treated as belonging to their original manager.
We have not established a supported import or export path for a running tmux agent.

Conductor's site reports over 100,000 builders. That is a dated vendor adoption claim,
not an independently verified active-user ranking. See the
[landscape](landscape.md#popularity-and-coverage) for how we choose comparison coverage.
