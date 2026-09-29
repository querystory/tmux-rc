# Design: the control session and routing

Status: **proposed**. Replaces the old "chat with your tmux" note. Builds on the
[openbus narrative](openbus-narrative.md), the [agent client](agent-client.md) and the
[control-plane design](agentic-control-plane.md); it links to them rather than
restating them.

## The problem

The fleet is many agents doing independent, collaborative work, and requests to it now
arrive from voice (Live Mode), from a coming text chat, and from agents themselves.
Each request raises the same question: which pane should this go to, and what should
happen there? Today Live Mode answers it by letting a voice model type into panes
directly. That fails for anything longer than a sentence: the voice model has a thin
view of each pane, no memory of which stream lives where, and no way to open a pane,
start an agent and hand work over in one sequence. The narrative's existence proof
("This already works") is an agent on the tmux server doing exactly that when asked.

## Shape

Two roles, one bus, and no hub.

- **The control session.** tmux-rc creates a `control` tmux session, at startup or on
  first use, running an ordinary harness in an ordinary pane: Claude by default,
  swappable for any other. It is attachable at the desk and visible on the phone like
  any other pane. It reaches the fleet through bus verbs the daemon exposes (below).
- **Front ends.** Live Mode and the text chat. They know control exists and route to
  it, but nothing forces them through it, and nothing forces anything else through it
  either: agents keep addressing each other directly, and you keep tapping into any
  pane yourself. The narrative's argument against a supervisor ("Why a bus and not an
  orchestrator") is the constraint here. Control is a peer with the most context, not
  a master.

## Routing: correct by default, fast when it is sure

Every request has one correct-by-default destination: control. Anything ambiguous,
multi-step, about an unknown stream, or that creates or hands off panes goes there, and
control works it out with the full directory in hand, asking the user only if it truly
cannot tell. The front end never shows a "which one?" picker of its own: one place owns
clarification, so the question is asked once, with the best context.

The fast path is an optimization over that default, for latency and cost: GPT-Live's
fast-front/slow-back split (`openbus/gpt_live.py`) with control as the backend. A small
fast model reading the directory sends direct only when exactly one target matches on
workstream identity (below), not on a self-reported confidence score; the target is an
agent pane, not a shell, where text executes; and the message is plain text (Live Mode's
`type_in_pane`) asking for nothing destructive or outward-facing, such as a push or a
delete ("tell the qs-app review pane to add a test for that"). Key presses, window
creation, resumes and anything doubtful go to control, which asks the user for risky
actions per the control plane's risk tiers.

Why: a send into a live agent cannot be undone, so an extra hop to control is cheap and
a misplaced instruction is not. Restricting the fast path this way makes consent a
property of routing rather than a separate gate: the only unconfirmed action is the one
a person would take without thinking, saying a sentence to an agent. The residual risk
is false confidence, so each direct send names its target as it goes (Live Mode's
overlay already logs typed actions) and echoes the directory's incarnation, so it is
rejected if the pane id has been recycled.

## Crossed streams

Routing classifies against **workstream identity**, not pane ids: the PR a pane is
tracking ([PR/session associations](pr-session-associations.md)), its branch and
working directory, its self-published title, its topic, and the conversation threads
proposed in #232. Pane ids are how the daemon addresses; they are recycled, and they
are not what a person means by "the auth review."

The norm is **one workstream per pane**. When a new stream would land in a pane that is
already busy with a different one, control proposes (or with consent, opens) a new pane
instead of stacking the second stream onto the first agent's context. Pane creation is
already bounded to configured launchers, so this adds no arbitrary-command surface.

When a request does go astray, #232's origin links point each pane input back to the
message that caused it, so "why did this agent get that?" has an answer.

## The control session's charter

Control is **one long-lived session** with a narrow, standing charter: dispatch,
routing, pane and window management, and keeping streams from crossing. It never does
project work. Anything that edits code, reviews a PR or runs a build goes to a project
pane, which control opens if none fits.

Why so narrow:

- **Small context.** Routing quality depends on the directory being the bulk of what
  control holds. Every file it reads for project work crowds that out.
- **No drift.** A router that starts doing the work becomes a busy worker, and a busy
  worker routes badly: it is mid-task when the next request arrives, and it starts
  answering from its own task's context instead of the fleet's.
- **Predictable cost.** A session that only reads summaries and emits short sends has a
  bounded bill; one that picks up project work inherits that work's bill.

Control **tracks fleet state rather than looking things up**: it is continuously fed
the watcher's directory and its changes, plus #232 threads, instead of re-reading pane
screens. Perception runs once, in the daemon; control is its consumer (the agent
client's "third client, not a second control plane").

Keeping the charter over a long session is the real risk, because charters erode
quietly. Three design-level guards:

- **Standing instructions** state the charter and the refusal ("hand project work to
  a project pane"), and make the directory the source of truth for state, never for
  instructions: its text comes from panes, so it is routing evidence, and only a request
  from a front end or the user carries authority to act (Live Mode's rule for terminal
  updates).
- **Re-seed, don't remember.** When context grows stale, tmux-rc restarts control from
  a fresh launcher seeded with the charter plus the current directory, rather than
  trusting a harness's own compaction; restarting works for any harness, and a
  conversation's summary of itself is where drift accumulates.
- **Visibility.** It is an ordinary pane; if it starts doing project work, you see it
  on the phone like any agent going off course.

## The directory and the verbs

Both front ends and control read the same **fleet directory**: per pane, its title,
tool, activity, summary, tracked PRs and recent events. `/api/digest` already returns
most of this and is documented as the endpoint for agents; the work is shaping it for
routing (stable workstream identity, an incarnation guard against recycled ids, as the
agent client explains) rather than inventing it.

Control's **bus verbs** are the agent client's verbs plus the ones this job needs: list
and read panes, send (with delivery confirmation), press a key, open a window, find and
resume sessions, and hand off. They are exposed over the daemon's API as an MCP server
with a thin CLI fallback, for the reasons the agent client gives: tools the harness
actually sees, no harness-specific integration. Every send stays on the daemon's one
keystroke path, so the audit log covers control as it covers the phone, and whatever
consent rules narrative build item 2 settles on have one place to be enforced.

## Optional session leads

A tmux session may have a **lead**: a long-lived pane (say, a qs-app lead) that holds
that project's context and takes requests for its session. This is a convention, not a
tier. Routing goes to a session's lead if one exists, and to the leaf pane otherwise.

Leads are optional because they cost something real: an extra hop on every request, a
second context that has to be kept current, and the risk that the lead's picture of its
panes goes stale while the panes move on. They pay for themselves only where a project
has enough parallel streams that a local router beats control's fleet-wide view.

## The return path

Mostly exists: Needs you, activity transitions, idle summaries and events. What is new
is fan-out and a thread tying a request to each of its effects. Every delegated request
is an entry in the requester's #232 thread; control's answer or clarifying question
lands in that same thread with an origin link to the request, and the front end already
watches its own thread. No new reply channel, concurrent requests stay distinct by
entry, and the same trail debugs a crossed stream.

## Alternatives rejected

- **A mandatory master router.** Every request through one session makes it the
  bottleneck and the single point of failure, and breaks peer-to-peer traffic the
  narrative treats as first-class. Control is the default for hard requests, never a
  gate for everything.
- **A voice model driving panes directly.** Today's behavior for anything
  multi-step. It fails because the voice model has neither the context nor the
  sequence of actions; it stays only as the fast path's single confident send.
- **A fast model only, no escalation.** Cheap until the first ambiguous request, then
  it either guesses (crossed streams) or stalls. Without a slow path it has nowhere to
  send doubt.
- **A front-end "which one?" picker.** Asks with less context than control has, asks
  when control could have resolved it, and splits clarification across two places.
- **A brain built on one vendor's API** (for example, the Claude API called from the
  daemon). Locks the smart layer to one provider, against the narrative's founding
  rule. A harness in a pane can be any harness; Claude is only the default.
- **Mandatory session leads.** Imposes the hop and upkeep costs on every project,
  including the many that have one or two panes.

## Build order

1. **Directory endpoint**: shape `/api/digest` for routing.
2. **Bus verbs** as MCP and CLI, starting with confirmed send.
3. **Fast dispatcher** in chat and Live: send direct on one confident target, else
   delegate (until step 4, the front end asks, as Live Mode does today).
4. **The control session** as the delegation target, with its charter and re-seeding;
   it needs #232's threads for its replies.
5. **Leads**, only if real use shows control's fleet-wide view is not enough.

## Open questions

- How control is fed changes without interrupting its own turns: a subscribe verb it
  blocks on between tasks, or a harness hook that injects the latest directory each
  turn.
- Whether hand-off needs its own verb or is a composed open-window-then-send.
- How consent rules (narrative build item 2) apply to control specifically, given it
  types into more panes than anyone else.
