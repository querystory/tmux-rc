---
title: Broader competitor landscape
weight: 40
---

**Checked October 10, 2026.** This shortlist covers agent managers, terminal
workspaces, and orchestration tools relevant to supervising multiple coding agents.
It is not a ranking of coding models or editors. Start with the detailed comparisons
of [Conductor](conductor.md), [T3 Code](t3-code.md), and [MuxFlow](muxflow.md).

## Popularity and coverage

Public attention helps choose what to research, but does not establish active users
or market share. Conductor reports **100k+ builders** on its website; the number is
vendor-reported and is not directly comparable with GitHub stars.
[Conductor site](https://www.conductor.build/).

The following GitHub API observations are a dated snapshot, not live counters:

| Project | Stars observed October 10, 2026 | Why include it |
| --- | ---: | --- |
| [Vibe Kanban](https://github.com/BloopAI/vibe-kanban) | 28,312 | Prominent planning/workspace project; company services are sunsetting |
| [cmux](https://github.com/manaflow-ai/cmux) | 28,076 | Terminal workspace with beta existing-tmux mirroring |
| [T3 Code](https://github.com/pingdotgg/t3code) | 26,680 | Managed conversations, remote clients, and account quotas |
| [Superset](https://github.com/superset-sh/superset) | 15,042 | Parallel CLI-agent workspaces and remote access |
| [Emdash](https://github.com/generalaction/emdash) | 5,948 | Additional parallel-agent workspace project to watch |
| [agent-deck](https://github.com/asheshgoplani/agent-deck) | 1,051 | tmux-backed manager with conversational supervision |
| [MuxFlow](https://github.com/gal064/muxflow) | 78 | Smaller project with direct tmux/SSH overlap |

Counts were collected during this research session; GitHub stars change continually.
No common active-user dataset was found that would justify declaring one product
the most popular. Conductor deserves a detailed page because of its reported reach
and product overlap. MuxFlow and agent-deck deserve attention despite smaller public
counts because their terminal-management model is particularly relevant.

## Superset

Superset builds a workspace workflow around parallel CLI agents. Its status hooks
only report agents launched through Superset; outside its terminals, the hooks do
not establish agent status. That is a concrete distinction from observing arbitrary
existing tmux panes.
[Status detection](https://docs.superset.sh/agent-status).

Remote access includes a standalone headless host option, so a desktop installation
is not required on every execution host. The documented iPhone/iPad relay access
requires Pro. Check current platform requirements when evaluating it.
[Remote access](https://docs.superset.sh/remote-access).

Usage includes Claude/Codex subscription windows, multiple accounts, and analytics.
The documented Usage page does **not aggregate remote hosts**; do not equate it with
T3's cross-environment account pool. The repository uses ELv2: source availability
does not imply an OSI open-source license.
[Usage](https://docs.superset.sh/usage),
[repository and license](https://github.com/superset-sh/superset).

**Our assessment:** evaluate it for worktree lifecycle and managed terminal workflows.
Try separate tasks first; status tracking does not promise adoption of your old fleet.

## cmux

cmux is a native macOS terminal workspace built on Ghostty, with notifications,
splits, an embedded browser, and automation. Its README describes Claude Teams
integration. Phone, voice, and cloud features are listed under Founders Edition
early access; do not count them as generally available capabilities.
[Project and availability](https://github.com/manaflow-ai/cmux).

Its opt-in **beta remote tmux mirroring** attaches over SSH using `tmux -CC` and
maps existing sessions/windows/panes to native workspaces/tabs/splits. Changes go
back to the real tmux server, which remains alive after detach. This is actual
tmux introspection, not merely a terminal in which a user could run tmux.
[Remote tmux mirroring](https://cmux.com/docs/remote-tmux).

**Our assessment:** cmux is a plausible companion to tmux-rc for a Mac user who wants
a native terminal view of the same remote fleet. No conversation migration is needed
in principle; interoperability has not been tested here. Check beta limitations,
especially paste behavior and mirror restoration, before relying on it for remote work.

## agent-deck

agent-deck provides a terminal UI and browser command center around tmux sessions.
Its FAQ says it creates its own `agentdeck_*` sessions and leaves existing sessions
alone. This establishes tmux backing, but not automatic adoption of arbitrary old panes.
[Repository and FAQ](https://github.com/asheshgoplani/agent-deck#faq).

Its persistent supervisor feature, named “conductor,” can monitor sessions,
answer according to policy, escalate decisions, and communicate through Telegram,
Slack, or Discord. This feature is unrelated to the Conductor product at conductor.build.
[Supervisor documentation](https://github.com/asheshgoplani/agent-deck/blob/main/docs/conductor/README.md).

Parent-linked child sessions and explicit completion events are documented. Voice
conversation and pooled quota forecasting were not established in this research.
[Child-session management](https://github.com/asheshgoplani/agent-deck/blob/main/skills/agent-deck/references/sub-agents.md).

**Our assessment:** this is a direct comparator for conversational supervision and
agent-driven orchestration, even though its public star count is smaller.

## Vibe Kanban: account for the sunset

Bloop's April 10, 2026 shutdown announcement says Vibe Kanban continues as a
community-maintained open-source project and local workspaces remain functional.
Company remote services were scheduled to end after 30 days. Do not recommend the
former hosted service as currently available without new evidence.
[Shutdown announcement](https://www.vibekanban.com/blog/shutdown).

Its repository remains a useful reference for planning tasks, agent workspaces,
review, previews, and pull requests. Its README also describes configuring your own
remote deployment. Self-hosting and the former company service are distinct options.
[Repository](https://github.com/BloopAI/vibe-kanban).

## First-party remote controls

Native Claude and Codex clients are also alternatives when your fleet mainly uses
one harness. They should be evaluated alongside independent managers, rather than
omitted because they come from the agent vendor.

Claude Code Remote Control can connect existing local sessions to browser and phone
clients, with execution remaining on the host. The current documentation covers
multiple sessions and worktrees, not only one terminal conversation. It uses outbound
connections and subscription authentication; it is not a general interface for
non-Claude agents.
[Claude Remote Control](https://code.claude.com/docs/en/remote-control).

Current OpenAI documentation describes Codex through ChatGPT desktop/mobile, with
parallel chats, worktrees, and connected-host control. Mobile controls include
starting or steering work, handling approvals, and reviewing results. Worktree
handoff is documented, but adoption of arbitrary tmux panes was not established.
[Desktop app](https://learn.chatgpt.com/docs/app),
[worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees),
[remote engineering workflow](https://developers.openai.com/blog/mastering-codex-remote-for-engineering).

**Our assessment:** these are worth trying for deep native interaction with their
respective harnesses. tmux-rc remains useful when the overview must include agents
from several vendors and ordinary terminal work on the same tmux server.

## What to investigate next

Emdash is a workspace-manager watchlist entry, not a completed feature comparison.
For any additional product, first establish whether it controls existing terminal
sessions, starts its own agent processes, or runs managed cloud tasks. Then check
remote intervention, subagent detail, voice actions, account limits, lifecycle, and
session handoff. These questions reveal workflow differences more reliably than a
long list of editors, file viewers, or model names.
