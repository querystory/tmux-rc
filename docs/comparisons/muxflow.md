---
title: MuxFlow
weight: 30
---

**Checked October 10, 2026.** [MuxFlow](https://gal064.github.io/muxflow/) describes
itself as a remote-first tmux IDE. Its tmux and remote-agent features make it relevant
even if you do not use its file editor. Research uses public docs and source;
we have not installed it for this comparison.

## It really does introspect tmux

Sessions and processes remain on the host. The app maps tmux sessions to workspaces
and windows to tabs; session/window renames flow both ways. This makes it closer to
tmux-rc's existing-terminal model than a manager that owns provider-backed chats.
[Project overview](https://github.com/gal064/muxflow).

Remote access uses the system OpenSSH client and a managed host helper over a private
socket, rather than a public TCP listener. OpenSSH retains authentication and
host-verification responsibility. Restarting the helper does not restart tmux.
[Remote-host architecture](https://github.com/gal064/muxflow/blob/main/docs/remote-host.md).

## Agent state and subagents

Codex and Claude Code have lifecycle adapters. Hooks are the primary state source;
process detection alone does not establish working, blocked, or idle. An agent can
appear with unknown state when its hooks are absent. Codex transcript monitoring
repairs specified missing lifecycle transitions. Its hook implementation also tracks
child activity, so subagent awareness is not exclusive to tmux-rc.
[Agent hooks](https://github.com/gal064/muxflow/blob/main/docs/agent-hooks.md).

tmux-rc uses terminal capture/classification alongside available harness history and
agent detail. This can cover terminal work without an equivalent MuxFlow lifecycle
adapter, but inferred state can be ambiguous. Hooks and terminal interpretation have
different failure modes; neither should be described as universally more accurate.
[tmux-rc architecture](../design/architecture.md).

## Remote and voice workflow

MuxFlow documents macOS, Linux, and Android clients, with iOS forthcoming. It supports
remote screenshot/file paste and port forwarding. Android voice is described as
hold-to-talk with spoken agent replies. tmux-rc instead provides a browser/PWA and
text/voice fleet controls. Both have voice features; compare interaction style and
scope rather than simply marking voice present or absent.
[Features and platform availability](https://github.com/gal064/muxflow).

## Use both on the same fleet

**Our assessment:** this is the most natural pairing of the three detailed comparisons.
Both can operate on the same host's tmux sessions. MuxFlow can supply native terminal
and SSH workspace tools, while tmux-rc supplies its attention overview and fleet controls.
This is an architectural compatibility assessment, not a tested interoperability promise.

There is no required conversation migration if both clients address the same panes.
Replies, renames, and pane closure affect the underlying shared sessions. Use one client
to submit a given instruction, then confirm it arrived.

Review MuxFlow's hook setup before enabling it on an existing host. Its documented
Codex compatibility setup can change `CODEX_EXEC_SERVER_URL` in the tmux environment
and a marked daemon setting in Codex configuration; existing panes may need a new
Codex launch for status tracking. This is an agent-configuration change, not merely
another terminal view.
[Hook setup and Codex compatibility](https://github.com/gal064/muxflow/blob/main/docs/agent-hooks.md).

Pooled subscription quotas and usage forecasting were not established by the sources
reviewed here. Keep that question open when refreshing the comparison.
