# Background & motivation

Status: **background / primer**. Start here if you're new to the project. The [PRD](../PRD.md) has
the crisp requirements; this is the longer *why* — the problem that started it, and why watching a
`tmux` session turned out to be the right shape for the answer. It also explains what tmux is along
the way, so no prior knowledge is assumed.

## The problem: the agents live elsewhere, and reaching them is the pain

The whole thing starts from one decision: **I don't want to run Claude Code on my local machine.**
Coding agents want a beefy, always-on box — one that keeps grinding while my laptop sleeps, moves
between networks, or runs out of battery. So the agents run on a remote dev box, working away
whether or not I'm at my desk.

That solves *where the work happens* and immediately creates the real problem: **how do I reach it?**
An agent that's been sitting for twenty minutes waiting on a yes/no permission prompt is worse than
useless — it's blocked *and* I don't know it. The value is in noticing and unblocking quickly, and
that requires a live line to a machine I'm not sitting in front of.

The obvious line is SSH: connect in, attach to the terminal, look, type an answer. In practice it's
miserable exactly when you need it most:

- **From a phone it barely happens.** An SSH client on a touchscreen, attaching to a raw terminal,
  pinch-zooming through a wall of scrollback, thumbing a `send-keys` answer character by character —
  nobody actually does this while they're out. So you don't check in at all, and the agent stalls.
- **Even from a PC it's friction.** Find the right host, SSH in, attach, hunt for the pane that's
  waiting, squint at what it's asking. Every glance is a small production.

Moving the agents to a remote box was supposed to free me from my local machine — but SSH drags me
right back to being tethered to *some* terminal on *some* machine just to see what's going on.
**That tether is the thing tmux-rc removes.** Everything below is a reason this is the right way to
remove it; this is why it exists at all.

## The near-miss: Claude Code's own remote control

Anthropic already ships `/remote-control` for Claude Code — a phone UI to watch a session and answer
its prompts from your pocket. It's genuinely good, and it's the direct inspiration here: the idea is
exactly right. But in daily use it's opinionated in three ways that disqualify it for how I actually
work:

- **One vendor.** It drives Claude Code and nothing else. But Codex and Gemini CLI are in the same
  workflow — different models win at different things, and the interesting work increasingly *mixes*
  them. A single-vendor remote is blind to most of the fleet.
- **One billing path.** It's locked to the first-party Anthropic API — no Bedrock, Vertex, or
  Foundry. That has a real economic bite: running Claude through **Bedrock spends AWS credits**, but
  remote control forces first-party billing, so you can run out of quota mid-session and be stuck
  paying *real dollars* while sitting on a pile of cloud credits you can't apply.
- **Someone else's environment.** It runs the agent in a managed sandbox with its constraints. Half
  the point of a remote dev box is that it's *mine* — my tools, my filesystem, my network, no limits
  I didn't choose.

The pattern is the tell: remote control abstracts over the *agent* but stays opinionated about the
*vendor, the billing, and the environment*. The fix is to abstract one level lower — over the thing
all of these share.

## The insight: the terminal is the universal surface

Every one of those agents — Claude Code, Codex, Gemini CLI — and every non-agent task too (`make
test`, a `psql` shell, a plain prompt) has exactly one thing in common: **it runs in a terminal.**
The terminal is the universal substrate. If you watch and drive *that*, you're automatically neutral
on vendor, on billing, and on environment, because the terminal doesn't care which of those produced
the text on it.

So the shape of the answer falls out: don't integrate with any agent's API or parse any vendor's log
format. Observe the terminal, turn what's on it into a glanceable phone view, and send answers back
into it. The only requirement is a terminal that's *durable* (survives disconnects) and *observable
from the outside* (readable without disturbing it). That is precisely — and only — what tmux is.

## Why tmux is the perfect substrate

`tmux` (terminal multiplexer) sits between your terminal and the shells inside it, and two of its
properties are exactly the two we need.

**It persists.** Normally, closing a terminal — or dropping an SSH connection, or sleeping the laptop
— kills everything running in it. tmux breaks that link: a long-lived **server** process owns the
shells, and your terminal is just a *client* that attaches to it. Close the window and the work keeps
running; `tmux attach` from anywhere later drops you back in, same processes, same scrollback. That's
the durability the remote-box story depends on.

**It's structured and observable from outside.** A tmux server holds **sessions**, each with
**windows** (tabs) split into **panes** (side-by-side shells), and every one is addressable through a
command interface you can drive from a *separate* program:

- `tmux list-panes` — enumerate what exists.
- `tmux capture-pane` — read a pane's current contents **without disturbing it**. This is the load-
  bearing primitive: you observe an agent while a human stays attached to the same session, neither
  stepping on the other.
- `tmux send-keys` — inject keystrokes into a pane, as if typed by the user.

Durable, structured, observable-without-interference — that's the entire wishlist, and it already
exists. This is the quiet strength of the approach: tmux has been battle-hardened for two decades and
is on every dev box. tmux-rc isn't a new terminal stack we have to keep alive; it's a **thin layer
above a mature, boring dependency.** The hard part isn't our code — it's someone else's, already
solved.

## The reader that makes it cheap: Gemini Flash Lite

Watching gives you raw pane text; the value is in *understanding* it — "this pane is blocked, here's
the question, here are the options." Heuristics handle the easy cases, but the fallback is a small,
high-volume classification job run every time a pane changes, and its economics decide whether
continuous watching is even viable.

**Gemini Flash Lite** — a model a lot of people sleep on — turns out to be the perfect reader for it:
strong enough to read messy terminal text and rendered images reliably, cheap and fast enough to run
on every tick without a second thought about the bill. That combination is the real unlock. It means
a **cheap, fast little model can babysit the crazy-expensive frontier agents** — you don't burn
Opus/Fable-tier tokens to notice an Opus-tier agent is stuck; you spend fractions of a cent to watch,
route, and unblock, and reserve the real money for the real reasoning. Keeping expensive agents fed
and unblocked is exactly where a cheap watching layer pays for itself many times over.

### The same reader can build the UI's affordances, not just describe state

Once a cheap model reads every frame, something bigger than classification falls out. There is a
class of UI problem that is **impossible to solve generically and trivial to solve semantically** —
so traditional software ships a bad generic answer, or ships nothing and leaves the work to the
user. A model already in the render path can just *decide*, per screen, and be right.

The shipped example is getting text OUT of a terminal. Copying from a pane is miserable: a
fixed-width grid drags borders and column padding into any selection, long lines are hard-wrapped,
and on a phone a drag pans instead of selects. Every generic fix is bad — copy-the-whole-frame hands
you the chrome, rule-based structure detection is an arms race against every TUI, and per-tool copy
buttons would require the very integration this project refuses. But *"what on this screen exists to
be pasted somewhere else?"* is one sentence, and the Flash Lite pass answering it was already
running. So the card grows labeled one-tap copy buttons holding clean, unwrapped, border-free text.

The hard part is not extraction — it's **judgment about intent**: a command the agent is asking
permission to run is deliberately NOT offered for copying, because there the user's job is to
approve it, not carry it elsewhere. No regex expresses that. A sentence of prompt does.

This generalizes well past tmux-rc, and it's the concept most worth stealing from this project — see
[the thin LLM layer on top of the UI](thin-llm-ui-layer.md).

## The bigger prize: an outer loop, not just thumbs

Answering prompts from a phone is the visible feature. The deeper reason is **orchestration** —
driving a whole fleet of sessions from an *outer-loop agent*, not only a human. Picture an
orchestrator that spins up several Claude Code / Codex / Gemini sessions, hands each a slice of work,
and coordinates: notices when one blocks, pipes one's output into another, decides what to launch
next. To reason about the fleet it needs a clean, live, structured read of **what each session is
doing right now** — the same thing the phone UI needs, just consumed by code instead of eyes.

Today the only window into a Claude Code session is its `~/.claude/projects/**/**.jsonl` transcript,
and as an orchestration substrate it's the wrong tool: the files **rotate** (so "the current session"
is a moving target), they're **slow to parse** (large append-only JSONL, no index), they're **not
semantically indexed** (raw event soup you re-derive meaning from every time), and **grepping logs is
the wrong interface** for an agent that needs to act on state, not spelunk for it. I built a
`/session-summary` skill that writes `.md` sidecars next to those files to make them searchable — it
helps, but it's **clunky**: a batch pass over static files, bolted onto the log substrate,
reconstructing state after the fact instead of observing it live.

Watching the terminal attacks the same need from the right side. The one surface every agent shares,
turned into live structured state, is exactly what both a thumb and an orchestrator want — so the
phone UI is simply the *first* consumer of the watcher, and an orchestration API over the same state
is the natural next one.

## In one line

Put the agents on a durable remote box, watch the one surface they all share — the terminal, made
persistent and observable by mature old tmux — read it cheaply with Flash Lite, and you get a
vendor-neutral, billing-neutral, environment-neutral control plane that a thumb can use today and an
orchestrator tomorrow. Not another single-vendor remote, and not another pile of logs to grep.

---

Next: [How it all works](architecture.md) for the end-to-end mechanics, the [PRD](../PRD.md) for
requirements, and the other [design notes](_index.md) for the *why* behind individual pieces.
