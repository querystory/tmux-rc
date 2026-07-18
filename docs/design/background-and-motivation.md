# Background: what tmux is, and why tmux-rc exists

Status: **background / primer**. Start here if you're new to the project. The [PRD](../../prd/)
has the crisp requirements; this doc is the longer *why* — what tmux is, and the full set of
frustrations that made building tmux-rc worth it.

## What is tmux?

`tmux` (terminal multiplexer) is a program that sits between your terminal and the shells
running inside it. Two things about it matter for this project:

**It persists.** Normally when your terminal window closes — or your SSH connection drops, or
your laptop sleeps — every process running in that terminal dies with it. tmux breaks that
link. It runs a long-lived **server** process that owns your shells; your terminal merely
*attaches* to it as a client. Close the window and the shells keep running on the server. Open
a new terminal, run `tmux attach`, and you're back exactly where you were — same processes,
same scrollback, still running. This is why people run long jobs and remote work inside tmux:
the session outlives any one connection to it.

**It's structured, and scriptable.** A tmux server holds **sessions**, each with **windows**
(like tabs), each split into **panes** (independent shells side by side). Every one of these is
addressable. And critically, tmux exposes all of it through a command interface you can drive
from *outside* the session:

- `tmux list-panes` — enumerate what exists.
- `tmux capture-pane` — read the current text content of any pane, **without disturbing it**.
  This is the key primitive: you can look at what an agent is doing without touching its input.
- `tmux send-keys` — inject keystrokes into any pane, as if the user typed them.

That combination — *durable*, *structured*, *observable-and-controllable from the outside* —
is exactly what a phone control plane needs. tmux is the universal substrate: whatever runs in
a terminal (an AI agent, a test run, a database shell, a plain bash prompt) becomes something a
separate program can watch and drive. tmux-rc is that separate program.

## The top-level reason: run the agents somewhere else, reach them from anywhere

Before any of the finer motivations below, there's the one that started it: **I don't want to run
Claude Code on my local machine.** Agents want a beefy, always-on, well-provisioned box — one that
keeps working while my laptop sleeps, moves, or runs out of battery. So the agents live on a
remote dev box. Which immediately creates the real problem: **reaching them.**

The obvious answer — SSH in and attach to tmux — is a genuine pain, and it's worst exactly when
you most need it:

- **From a phone it's miserable.** An SSH client on a touchscreen, attaching to a raw terminal,
  pinch-zooming to read a wall of scrollback, trying to type a `tmux send-keys` answer with thumbs
  — this is the thing nobody actually does when they're out. So you don't check on the agent at
  all, and it sits blocked for an hour.
- **Even from a PC it's friction.** Find the right machine, SSH in, `tmux attach`, hunt for the
  pane that's waiting, squint at what it's asking. Every check-in is a small production.

The whole point of moving the agents to a remote box is to be free of your local machine — but SSH
drags you right back into being tethered to *a* terminal on *some* machine to do anything. tmux-rc
removes that tether: the agents run remote, and you reach them through a **phone-native UI over
HTTPS**, no SSH, no terminal emulator, no thumbs-on-a-shell. Glance, see what's blocked, tap the
answer, done — from a phone on the go, or a browser tab on any PC. Everything below is a reason
this is better than the alternatives; *this* is the reason it exists at all.

## The seed: Claude Code's remote control, and its limits

Anthropic ships `/remote-control` for Claude Code — a phone UI that lets you watch a Claude Code
session and answer its prompts from your phone. It's genuinely great, and it's the direct
inspiration for tmux-rc. The idea is right: step away from your desk, keep a leash on the agent,
unblock it from your pocket.

But in daily use it hits a wall of limitations, and the limitations aren't small:

- **It only drives Claude Code.** Codex and Gemini CLI are part of the same workflow — different
  models are better at different things, and increasingly the interesting work *mixes* them. Remote
  control can't see any of that. The moment your workflow is heterogeneous, a single-vendor remote
  is blind to most of it.
- **It's locked to the Anthropic first-party API.** It does not work through Amazon Bedrock,
  Google Vertex, or Microsoft Foundry. That's not a checkbox — it has a real economic bite:
  running Claude through **Bedrock lets you spend AWS credits**, but remote control forces you
  onto first-party billing. Run out of first-party quota mid-session and you're stuck paying
  *real dollars* to continue, even while sitting on a pile of AWS credits you can't apply.
- **A restricted compute environment.** Remote control runs the agent in Anthropic's managed
  environment, with the constraints that come with someone else's sandbox. Part of the appeal of
  running agents on *your own* dev box is that it's *your* box — your tools, your filesystem, your
  network, no restrictions you didn't choose.

The throughline: remote control abstracts over the *agent* but is opinionated about the *vendor,
the billing, and the environment*. tmux-rc abstracts over the *terminal* instead — so it inherits
none of those opinions. Any agent, any model provider for the summarization pass, on your own
machine.

## The bigger reason: driving the whole thing from an outer loop

Answering prompts from a phone is the visible feature. The deeper motivation is **orchestration** —
being able to drive a fleet of agent sessions from an *outer-loop agent*, not just human thumbs.

Picture an orchestrator that spins up several Claude Code / Codex / Gemini sessions, hands each a
slice of work, and coordinates between them: notices when one is blocked, feeds another's output
into a third, decides what to kick off next. For that orchestrator to reason about the fleet, it
needs a clean, live, structured read of **what each session is actually doing right now** — and a
way to act on it.

Today the only window into a Claude Code session's history is its `~/.claude/projects/**/**.jsonl`
transcript, and as an orchestration substrate it's painful:

- **The files rotate**, so "the current session" is a moving target you have to keep rediscovering.
- **They're slow to parse** — large append-only JSONL you grep from the outside, with no index.
- **They're not AI-indexed** — no semantic "what happened, what's blocked, what changed" view;
  just raw event soup you have to reconstruct meaning from every single time.
- **Grepping JSONL is the wrong interface** for an agent that needs to make decisions from the
  state, not spelunk logs for it.

I already built a `/session-summary` skill that generates `.md` sidecar summaries next to those
`.jsonl` files to make them searchable. It helps, but it's **clunky**: it's a batch pass over
static files, not a live read; it's bolted onto the log-file substrate rather than replacing it;
and it's still reconstructing state after the fact instead of observing it as it happens.

tmux-rc points at the same need from a better angle. Instead of parsing each vendor's private log
format after the fact, it **observes the terminal itself** — the one surface every agent shares —
and turns it into live, structured state: which pane, which tool, running vs idle vs blocked, the
current question and its options. That state is exactly what both a human on a phone *and* an
outer-loop orchestrator want. The phone UI is the first consumer of it; an orchestration API over
the same watcher is the natural next one.

## Why this shape, in one line

Every terminal agent, whatever the vendor and whatever the model backend, shares one surface: the
terminal, made durable and observable by tmux. Watch *that*, and you get a vendor-neutral,
billing-neutral, environment-neutral control plane — usable by a thumb today and by an orchestrator
tomorrow — instead of one more single-vendor remote or one more pile of log files to grep.

For how that observation actually works end to end, read [How it all works](../architecture/). For
the requirements, the [PRD](../../prd/); for the design rationale, [DESIGN](../../design/) and the
other notes in this section.
