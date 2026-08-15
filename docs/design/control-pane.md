# Design: the control pane — a resident agent that manages the fleet

Status: **draft / thinking** — no code yet.

## Problem: the voice model is doing two jobs, and paying audio prices for both

Live Mode's prompt keeps absorbing orchestration duties. It now carries the
targeting ladder (name > conversation > active > offered redirects > idle-age
weighting), the verbatim-relay contract, placeholder/dim marker semantics, the
report-back and holding-note protocol, and destructive-command hygiene — all so
that one model can simultaneously hold a spoken conversation AND manage a fleet
of terminal sessions.

That coupling has three costs:

1. **Capability floor.** Routing a prompt to the right one of ten panes, watching
   the work land, and knowing when to speak requires the most capable live model
   available — the judgment and the voice are welded together, so we can never
   use a cheaper model for the easy half.
2. **Audio-rate context.** Every pane digest and every `[tmux update]` streams
   into the voice session as text-in at Live-API prices, and the model's
   reasoning about them shares a context with the conversation itself. The
   orchestration workload inflates exactly the session that bills highest.
3. **Prompt fragility.** The tuning history is a whack-a-mole record: placeholder
   text read as instructions, prompts typed into the wrong pane, instructions
   arriving summarized, the session going silent on dispatched work. Each fix
   grows the prompt the voice model must honor while half its attention is on
   speech. There is a ceiling on how much policy a voice prompt can carry.

## Idea: move the judgment into a resident control pane

Run a normal tmux pane whose occupant is a text coding agent (a Claude session)
with a standing charter: **manage the other sessions.** It routes user prompts to
the session they're destined for, watches dispatched work, chases answers,
aggregates status, and reports back. The voice layer collapses to a thin pipe:
transcribe what the user said, relay it to the control pane verbatim, speak the
control pane's replies aloud.

The intelligence moves to where tokens are cheap and capability-per-dollar is
high; the voice session keeps only the conversation.

## Why a pane, and not daemon code

- **It's dogfood.** The control pane is just another pane: the daemon already
  types into panes, digests them, and shows them on the phone. The user can
  watch it think, scroll its history, interrupt it, and correct it — the same
  auditability every other session gets for free. An orchestrator buried in the
  daemon is invisible until it misbehaves.
- **It inherits a full toolkit.** A resident agent session already has git, gh,
  tmux (send-keys, new-window, kill-pane), and file access — the entire action
  vocabulary of `agentic-control-plane.md`'s intent→confirm→execute loop —
  without us rebuilding a tool-execution layer inside the daemon.
- **Its transcript IS the conversation state.** The live prompt currently
  approximates "the conversation you're already in" statelessly, per session.
  A control pane's scrollback holds the actual thread — what was asked, where it
  was routed, what came back — across voice sessions, reconnects, and even
  phone-vs-voice modality switches.

## What each layer keeps

- **Voice model** (simplified prompt, three duties): decide whether speech was
  addressed to it at all (mic noise discipline stays), relay the user's words to
  the control pane verbatim, and speak the control pane's replies in one or two
  spoken sentences. No pane digests, no routing, no report-back protocol.
- **Control pane** (charter prompt, versioned in-repo): the targeting ladder,
  verbatim onward delivery, watch-and-report, holding notes, destructive-command
  confirmation. It reads fleet state itself — it lives inside tmux — rather than
  having digests pushed at audio prices.
- **Daemon**: the pipe. Knows which pane is the control pane, types into it,
  streams its replies back to the voice session. No new orchestration logic.

## The management verb set — shipped as skills

The control pane doesn't just relay; it operates the fleet. Its verbs:

- **Spawn**: create a new tmux window/pane and launch a session in it — a Claude
  Code (or codex/gemini/shell) session **in a specific directory** ("spin up a
  claude in the qs-app repo" → new window, cd there, launch, hand it the opening
  prompt).
- **Dispatch**: deliver a prompt to an existing window — inter-window messaging
  is plain `tmux send-keys`, the same primitive the daemon itself types with.
  Nothing new to invent: window-to-window communication IS typing into a pane.
- **Watch / inspect**: capture a pane, read its state, decide whether work
  landed — the report-back duty from the live prompt, executed with
  `capture-pane` instead of pushed digests.
- **Retire**: close a finished window / kill a wedged process — destructive, so
  it rides the intent→confirm→execute loop of `agentic-control-plane.md` (see
  "Blast radius" under Risks), never fires on inference.

These land as a **`fleet` skill versioned in this repo** — "skill" here meaning
a plain instruction document the resident session loads, not any vendor's
feature. It teaches the spawn/dispatch/watch/retire recipes — the exact tmux
incantations, the naming conventions the daemon expects (window titles the
parser reads), and the guardrails. The verbs are plain tmux, so the skill is
prose plus shell, not an integration: any agent that can read a file and drive
tmux can take the charter (a Claude Code session would load it from
`.claude/skills`; a codex or gemini session via its own mechanism — a packaging
detail, not the design). Designating a control pane is then just launching a
normal agent session with the skill available and the charter as its opening
prompt — no forked agent, no custom binary; any capable session can take the
job, and the skill's evolution is reviewed like any other code.

## Model economics

The voice session sheds its largest cost driver: pane context. Today every
`[tmux update]` bills as Live-API text-in and competes with speech for the
model's attention; under this design the voice context is conversation-only.
With judgment gone, a less capable live model becomes plausible — and the door
opens to half-duplex STT → text-model → TTS stacks, where the "voice model"
disappears entirely. The control pane runs continuously but idles free: an agent
session with no prompt in flight costs nothing but memory.

## Alternatives considered

1. **Keep tuning the live prompt** (status quo). Every behavior costs prompt
   space the voice model must honor mid-speech, at audio prices; the tuning
   record shows the marginal fix getting harder. Rejected as a ceiling, though
   it remains the fallback if the hop latency below proves unacceptable.
2. **Daemon-internal text-model orchestrator** (no pane). Same economics,
   lower latency than an agent turn — but it's invisible (no scrollback to
   audit), it needs a bespoke tool-execution layer, and it adds a third prompt
   surface to maintain. The control pane gets the same separation with tooling
   and auditability for free.
3. **Control pane** (this design). Costs: an extra hop of latency on every
   relay (an agent turn, seconds, against today's direct typing), and a
   resident session to keep alive. Both look bounded; neither is free.

## Risks and open questions

- **Hop latency.** "Press 3" or "yes" routed through an agent turn feels slow.
  Open: keep a small direct fast-path in the voice layer (single-token answers,
  press_key) and relay everything else? That reintroduces a sliver of routing
  judgment to the voice prompt — the tension of this design in miniature.
- **Self-reference.** The control pane appears in its own fleet view and in the
  phone app. It must know itself and never route work to itself; the daemon
  should mark it (and possibly the phone should render it distinctly).
- **Lifecycle.** Who starts it, restarts it after a crash, notices it wedged?
  Phase 1 punts (user designates an existing session); a real version wants
  daemon-managed spawn and a watchdog.
- **Blast radius.** A standing agent with tmux control is the scenario
  `agentic-control-plane.md` flags: its intent→confirm→execute posture applies —
  destructive actions get a confirmation loop back through the user, and the
  charter inherits the same spelled-out-only rule for destructive commands.
- **Double agency.** The user still types into sessions directly (phone and
  desktop). The control pane must treat the fleet as shared, not owned — read
  state fresh before acting, never assume its last dispatch is the pane's
  current state.

## Incremental path

1. **Designate, don't build**: an env var names an existing pane as the control
   pane; the live prompt's targeting section collapses to "relay to the control
   pane." Measure verbatim fidelity, hop latency, and how it feels.
2. **Charter + skills + lifecycle**: the charter prompt and the `fleet` skill
   (spawn/dispatch/watch/retire) versioned in-repo; daemon-managed spawn/restart;
   self-reference marking.
3. **Cheapen the voice layer**: try a smaller live model, then a half-duplex
   stack, against the simplified voice prompt. Keep whichever holds up.

## Relations

- `agentic-control-plane.md` — the action-planning capability this pane would
  embody; its autonomy model (intent → confirm → execute) carries over.
- `live-mode.md` — the current architecture this offloads.
- `chat-with-tmux.md` — the text-chat sibling; a control pane serves both
  modalities with one brain.
