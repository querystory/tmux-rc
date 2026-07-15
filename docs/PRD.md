# tmux-rc — Product Requirements

## Problem

AI coding agents (Claude Code, Codex, Gemini CLI) and long-running shell tasks run in
a terminal on a dev machine. When you step away, you lose all visibility and control:
you can't see that a 40-minute test run finished, that an agent hit an error, or —
most painfully — that an agent has been sitting for 20 minutes waiting for you to
answer a yes/no permission prompt or pick from a multiple-choice question.

Claude Code ships `/remote-control`, which gives a phone UI for exactly this. But it
has two disqualifying limitations for a heterogeneous terminal workflow:

1. **Locked to the Anthropic API.** It does not work with Amazon Bedrock, Google
   Vertex, Microsoft Foundry, or any custom LLM gateway. If your inference runs
   through Bedrock/Vertex, remote control is simply unavailable.
2. **Single vendor.** It controls Claude Code and nothing else. Codex, Gemini CLI,
   `make test`, a `psql` session, a raw shell — all invisible to it.

The terminal is the universal substrate. A phone control plane should treat it that
way: watch whatever is in the terminal, regardless of which agent (or no agent) is
running, and regardless of which model provider does the summarization.

## Goal

A small local service that watches a `tmux` session and pushes a **phone-native**
view of what's happening — not a terminal emulator on a touchscreen (miserable), but
a high-level, glanceable UI:

- **Status at a glance:** per pane — which tool is running, one-line "what's
  happening" summary, whether it's actively churning / idle (and for how long) /
  blocked waiting for input, and (when detectable) Claude-Code-style context-window %.
- **Actionable alerts:** when a pane is waiting for input, surface the actual question
  and its options as tappable buttons; tapping sends the answer back to the terminal.
- **Timeline:** a scrubbable history of recent terminal snapshots per pane, so you can
  catch up on what happened while you were away before you respond.

## Users

Primarily the author (dogfooding). Anyone running terminal AI agents who wants to
monitor and unblock them from a phone without SSH-ing into a shell on a touchscreen.

## Requirements

### Must have (Milestone 1 — single pane, this PoC)

- Watch one tmux pane and report its state to a phone over LAN (or a tunnel the user
  sets up).
- Classify activity: running vs idle (with idle duration) vs waiting-for-input.
- Detect a waiting prompt, extract the question + options, present tappable answers,
  and round-trip the answer back into the pane via `tmux send-keys`.
- Installable PWA (add-to-home-screen), auto-refreshing.
- Vendor-agnostic model backend for the summarization pass; PoC uses **Gemini Flash
  Lite via Vertex** (cheap, fast, strong at reading terminal text/images).
- Heuristics do the cheap work; the LLM is a lazy fallback, not on the hot path.

### Must have (Milestone 2 — all panes)

- Fan out to every pane/window in the session; dashboard with one row per pane,
  waiting panes floated to the top with an alert style.

### Nice to have (post-PoC)

- Push notifications instead of polling.
- Rendered PNG snapshots (vs. raw captured text) for the timeline.
- Multi-host / multiple tmux servers.
- Auth beyond network isolation.
- tmux control-mode (`-CC`) for push-based updates instead of polling.

## Explicit non-goals

- **Not** a terminal emulator on the phone. We never render a raw interactive terminal
  for the user to type into character-by-character. The whole point is abstraction.
- **Not** tied to any one agent vendor or any one model provider.
- **Not** a hosted/cloud service in the PoC — it runs locally next to tmux.

## Success criteria (PoC)

From a phone, while an agent runs in a tmux pane on the dev machine: I can see it's
working, see when it finishes or blocks, read the question it's asking, tap the answer,
and watch the agent proceed — without opening a terminal.
