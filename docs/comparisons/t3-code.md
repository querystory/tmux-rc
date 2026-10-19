---
title: T3 Code
weight: 20
---

**Checked October 10, 2026.** [T3 Code](https://t3.codes/) is an open-source agent
control plane with desktop, web, and mobile clients. This comparison uses its public
docs and implementation, not a local installation.
[Repository](https://github.com/pingdotgg/t3code).

## Session ownership

Environment servers own provider processes, terminals, files, and Git operations.
Clients control them over authenticated RPC. Structured provider adapters normalize
harness events into T3 thread state; this differs from observing the screen of an
existing terminal. The server owns its terminal PTYs. We found no application
integration that enumerates and controls an existing tmux fleet.
[Architecture](https://github.com/pingdotgg/t3code/blob/main/docs/internals/overview.md),
[terminal runtime](https://github.com/pingdotgg/t3code/blob/main/docs/internals/terminal-runtime.md).

## Where it overlaps with tmux-rc

| Capability | T3's documented behavior |
| --- | --- |
| Remote clients | Desktop, browser, iOS, Android; T3 Connect, private-network pairing, Tailscale, and SSH |
| Parallel tasks | Multi-model prompt fanout into separate threads/worktrees |
| Subagents | Delegated-agent inspection; approvals/questions route through the parent |
| Usage | Token history, cache savings, API-equivalent cost, quotas pooled across accounts/environments |
| Limit recovery | Optional continuation when a provider-reported reset arrives |
| External orchestration | OAuth-authenticated MCP access to read and operate T3 threads |

Sources: [remote access](https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md),
[threads and recovery](https://github.com/pingdotgg/t3code/blob/main/docs/user/thread-sidebar.md),
[usage](https://github.com/pingdotgg/t3code/blob/main/docs/user/usage.md),
[outside agents](https://github.com/pingdotgg/t3code/blob/main/docs/user/outside-agents.md).

Usage estimates are not subscription bills. Account pooling describes the available
allowance; it does not by itself establish automatic routing to whichever account has
quota left. Reset recovery needs a reported reset time and a running environment.
These distinctions matter when comparing ways to keep agents working.

## Voice: dictation versus conversation

The documented voice implementation transcribes locally on supported iOS devices,
inserts text into a draft, and waits for normal message submission. That provides
voice input; it does not establish spoken fleet conversation or proactive spoken updates.
tmux-rc's voice interface can discuss state and take actions across its managed panes.
[T3 voice implementation](https://github.com/pingdotgg/t3code/blob/main/docs/internals/voice-input.md).

## Import and switching

T3 onboarding discovers Claude and Codex projects and imports recent conversations
for continuation. Visible imported history is best effort, retaining up to 200 messages
and omitting attachments and tool activity. Its importer records native session
references and resume cursors. This is more than copying a transcript into a new chat,
but it does not attach to the original running tmux process.
[Onboarding](https://github.com/pingdotgg/t3code/blob/main/docs/user/welcome-wizard.md),
[importer](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/project/AgentSessionImporter.ts).

Switching providers inside T3 transfers a budgeted selection of conversation context.
It does not transfer outgoing reasoning, tool-call state, or attachments. Omitted saved
history can be retrieved through T3's thread-reading tool.
[Portable handoffs](https://github.com/pingdotgg/t3code/blob/main/docs/user/portable-handoffs.md).

**Why we built tmux-rc:** T3 covers managed conversations, account quotas, and remote
clients well. We wanted the manager to discover work already running across our tmux
sessions and let us supervise it in place. That requirement led to fleet summaries,
attention states, spatial views, and conversational control built around panes.
Subagent visibility and quota tracking are capabilities both products offer.

To try both, give T3 a separate task/worktree first. For an imported conversation,
hand ownership over between turns and verify continuation before retiring the original
session. No bidirectional live synchronization with tmux-rc has been established.
