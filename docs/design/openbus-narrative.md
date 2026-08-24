# openbus: the narrative

**OpenRouter is one endpoint for every model. openbus is one bus for every agent.**

This is a definition, not a vision statement. tmux-rc is the bones — the perception
loop, the keystroke plane, Live Mode, the phone client all run today. This document
names what they add up to so we can finish building it. Unbuilt pieces are listed as
work, not dreams.

## Day one: a real problem, solved

OpenRouter won because it solved a real problem the day it launched: getting
inference. One API key, every model, no waitlists, no per-vendor contracts. The
platform ambitions came later; the wedge was concrete.

openbus's wedge is the same shape: **remote control of any agent harness, without
vendor lock-in.**

You run coding agents in terminals. You leave your desk and they keep working — then
one blocks on a question and sits idle for an hour. The vendor answer to this is
Claude Code's `/remote-control`, and it defines the problem by what it refuses to do:
locked to the Anthropic API (no Bedrock, no Vertex), and it drives exactly one
harness. Your Codex pane, your Gemini pane, your OpenCode pane — invisible. Your
Claude-on-Bedrock pane — invisible. The vendors will each ship their own remote
control, each locked to their own harness and their own billing, because that's what
vendors do.

openbus works one layer down, where lock-in can't reach: it reads the *terminal*.
Any program in a pane — any harness, any provider, anything that prints text — shows
up on your phone: status at a glance, an alert when an agent is blocked, tappable
answers that round-trip back into the pane. That works today, end to end. It is the
whole reason to install openbus on day one, and it doesn't require believing anything
else in this document.

But notice what the wedge quietly required: a daemon that watches every pane, parses
every screen into structured state, and can type into any of them. The remote control
is just the **first app on that bus** — the phone happens to be the first client.
Live Mode is the second. Agent-to-agent handoffs are the third. Memory is the fourth.
The product isn't the app; it's the bus underneath, and every new thing built on it
makes the bus worth more. OpenRouter ran the same inversion: the router was the
day-one app, the platform became the company.

## The insight the wedge uncovers

Watching one pane taught us the design principle for everything else: **openbus acts
like a person at the keyboard.**

The industry's answer to "give agents hands" is computer use — screenshots, pixel
coordinates, synthetic clicks. It works and it's miserable: slow, expensive, brittle
against every re-render. The terminal is the same capability without the misery —
text in, text out, no vision model in the loop. The terminal *is* computer use minus
the finicky UI.

But not the way harnesses use it. A harness types a one-shot
`grep | awk | xargs` incantation, fires it blind, and scrapes the output — the
terminal as a bad RPC channel, its weakest mode. A *person* at a keyboard does
something else: watches the screen, and mostly **talks to the agents running there.**
Types a sentence into a pane. Reads the reply. Notices one is blocked and answers it.
Nudges one that's drifted. Conversation with running processes, through a keyboard,
with the screen as shared state.

That's what openbus does — first on behalf of your thumb on a phone, then on behalf
of intent ("roll back window 3 to before the refactor"), then on behalf of the agents
themselves. It is a multiplexer for harnesses and the bus between them.

## This already works

The bus is not hypothetical. Today, a pane-management Claude session sits on the tmux
server, and Live Mode directs it in natural language — *"create a new pane for X and
tell window 4 to hand this off to it"* — and it does: opens the pane, starts the
agent, types the handoff into the other window. Agents talk to each other over tmux
right now, because an agent with a keyboard and eyes on the screen can do anything a
person at that keyboard can.

That's the existence proof. The work is making it first-class instead of
prompt-shaped: addressable instead of described, cheap instead of ceremonial,
observable instead of ad hoc.

## Why a bus and not an orchestrator

Run five agents and you have five islands. They can't see each other, can't hand work
off, can't ask what a sibling learned. The human alt-tabbing between panes is the
slowest component in the system.

The reflex fix is a supervisor: one orchestrator that decomposes, assigns, collects.
That reflex fails the way it fails in an engineering org — a manager who routes every
message is the bottleneck and the single point of failure. Good organizations run on
direct peer contact, with hierarchy for direction and escalation. Not everything
transits the boss.

So openbus carries both kinds of traffic and has no opinion about which you use:

**Top-down (C2).** You state intent; openbus plans concrete keystrokes against the
live parse of the screen, confirms at a risk-appropriate tier (auto-advance known-safe
steps, one-tap for real effects, typed confirm for destructive), and executes. That
flow is specified in the control-plane design and not yet coded — today the phone
client relays taps and answers; the planner is the distance between that and one
person commanding ten agents without typing ten times.

**Peer-to-peer (A2A).** Any agent on the bus addresses any other directly, without
you in the path. The session that changed the auth module asks the session reviewing
it what it found. A stuck agent asks the sibling that solved the same problem an hour
ago. Work moves sideways at machine speed; you find out because you can watch, not
because you had to route it.

Both are necessary. Pure top-down makes your attention the fleet's clock rate. Pure
peer-to-peer is unsteerable. Real organizations run both channels at once — peers for
throughput, hierarchy for direction — and the substrate should make both cheap. Use
whichever gets the best result.

## What tmux is for

tmux is not the product. It's load-bearing three ways:

- **Zero adoption cost.** Every terminal agent already runs in a pane. No SDK, no
  integration, no cooperation from harness vendors — if it runs in a terminal, it's
  on the bus, including harnesses that don't exist yet.
- **Addressing for free.** Sessions → windows → panes is project → task → agent.
  Addresses turn a pile of processes into a network: *the agent in window 3 of the
  auth-refactor session.*
- **The only honest view.** Not the agent's self-report, not a status API a vendor
  chose to expose — the actual screen. Perception grounded in what's really there is
  what makes action safe.

If something replaces tmux, openbus survives. The name describes the layer, not the
multiplexer.

## Memory: the fourth client on the bus

Agents that can talk but can't remember repeat themselves forever. Every harness
writes transcripts — Claude Code in `~/.claude/projects`, Codex in
`~/.codex/sessions` — each in its own format, in its own hidden directory, indexed by
nobody. An enormous corpus of decisions and dead ends that no agent can consult,
including the agent that produced it.

Retrieval is solved and won't be rebuilt: `cass` already indexes two dozen harness
formats with hybrid search and cited, token-budgeted excerpts. What's missing is that
it isn't *on the bus* — no agent can ask it anything mid-task. Wiring it in as an
addressable participant is a thin shim.

The unclaimed piece is the one only the substrate can build. Every existing tool
indexes *messages*; none can answer "what did we decide about this" by reconciling a
Claude session with the Codex review of the same branch and the commit that came out
of it — because none of them know those three things are related. openbus is the only
thing positioned to know: it watches the work happen and already sees each pane's
working directory. Recording branch and transcript identity alongside (build item 3)
makes **provenance a byproduct of being the substrate** — and that's what turns
transcripts into institutional memory.

## Why this wins

Three properties compound, and each is hard to copy without the others:

1. **Lock-in-proof by construction.** openbus operates one layer below where any
   vendor controls anything. A harness would have to stop being a terminal program to
   opt out — and the more harnesses proliferate, the more the neutral layer is worth.
2. **Perception before action.** The screen is parsed into structured state first;
   actions are planned against that state, not fired blind. A system that only sends
   keystrokes is a macro recorder; one that sees is a control plane.
3. **The graph is free — to the substrate.** Who works on what, where, on which
   branch, and what they concluded: nobody has to instrument anything, because the
   substrate already watches it all. Recording it is build item 3, not a moat someone
   else can build first.

## What to build

The distance between today's prompt-driven bus and the first-class one, in order:

1. **Addressing and messaging as primitives.** "Tell window 4 to hand this off" is a
   paragraph of prompt today; make it one cheap, observable operation with a real
   address. The pane-management session proved the shape; now make it protocol.
2. **Consent on the peer channel.** An agent that can type into another's pane can
   derail it. Who may address whom, whether messages are announced, what a human can
   veto — extend the tiered-confirmation model (designed for human actions) to agents
   acting on each other.
3. **Memory on the bus.** The cass shim as an addressable participant, plus the
   provenance layer (pane ↔ worktree ↔ branch ↔ transcript) only openbus can record.
4. **Attention economics.** Ten agents emit more than a person can read. Live Mode
   must summarize and escalate, never stream, or the bottleneck returns as a
   notification feed. (#64, #83)
5. **Cost and cadence.** A perception pass per pane per interval, times many panes,
   is a real bill; throttling and parallel parses (#46, #55) become load-bearing at
   fleet scale.

## The bet

Terminal agents are proliferating and every vendor's coordination story stops at its
own walls. The layer between harnesses cannot be built inside one — a harness that
tried would be building its competitors' integration surface. It has to be built one
layer down, where every agent already runs: at a keyboard, in a terminal.

Day one, openbus is the remote control every harness refuses to be. That earns it the
seat at the keyboard. Everything after — Live Mode, handoffs, memory, whatever gets
built next — is another client on the same bus, and each one makes the bus more worth
plugging into. The apps come and go; the bus is the product. We're building it.
tmux-rc is the bones.
