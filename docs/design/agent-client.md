# Design: the agent client — a coding agent on the bus

Status: **draft / thinking** — no code yet, but the thing it describes already ran.

The [openbus narrative](openbus-narrative.md) names four clients on the bus and counts
them off: "the phone happens to be the first client. Live Mode is the second.
Agent-to-agent handoffs are the third. Memory is the fourth." This is the third one,
written up because it happened by accident before anyone built it.

A Claude Code session orchestrated three sibling Claude Code sessions in adjacent tmux
windows and drove two PRs to review-ready plus a terraform branch. It worked. Every way
it failed was the same way, and every one of those failures maps to a primitive openbus
already computes and the orchestrating session could not reach.

## What ran

One session — call it the master, because that is what it was doing, not because the
word is good — spawned three others in sibling windows of the same tmux session, handed
each a task, and steered them. The slaves did the work: real branches, real commits, two
PRs review-ready, a terraform change. The master's own context stayed small, because it
never read the files the slaves read.

That is the narrative's thesis running in the wild. The master was "acting like a person
at the keyboard, talking to the agents running there": typing a sentence into a pane,
reading the reply, nudging one that had drifted. It is the only mode in which one session
can run three others without drowning in their output.

It was also doing it with `tmux send-keys` and `capture-pane`, which is exactly the mode
the same document criticises — the terminal as a bad RPC channel, fired blind and
scraped. A person gets away with that because a person is *looking at the screen the
whole time*. The master looked only when it remembered to.

## Every failure was silent

This is the finding. Not that things went wrong — things go wrong — but that not one of
them raised an error anywhere. No exception, no non-zero exit, no warning. The master's
report of its own progress was confident and wrong, and the human found out by walking
the windows himself.

Each failure below is first-hand from that session, and each one is followed by the
primitive that already exists inside the daemon.

**The Enter got dropped.** `send-keys` with the text and `Enter` as one call frequently
delivered the text and lost the Enter. Prompts sat in input boxes, composed and unsent.
The human went hunting through windows and found steering messages parked in composers;
the master had already reported that work as dispatched. — The daemon's own send path
happens to do the right thing here for unrelated reasons: `tmux.send_keys` writes the
literal text with `-l` and then issues the Return as a *separate, ordered* `send-keys`
(the split exists because tmux caps a message at 16KB, but the ordering guarantee is the
same one this failure needs). More importantly it does not treat the send as finished:
every `/api/panes/{id}/send` forces an immediate re-parse, because input changes the
screen and the daemon refuses to believe its own stale view of it.

**The verification was a false negative by construction.** The master did check. It
grepped the whole pane for the text it had just sent — which matches whether or not the
send worked, because a *sent* message is echoed into the transcript and stays in
scrollback. The discriminator is not whether the text is present but *where* it is:
still in the input box means unsent, scrolled up into the transcript means sent. A
substring search over a flattened capture cannot tell those apart. — The classifier
already reads the pane as a structured screen rather than a bag of characters; telling
"the agent is asking this question" from "the agent answered that question" is the job
`parser_prompt.txt` exists to do.

**A slave sat on the trust prompt for ten minutes.** A freshly spawned session stopped
on "do you trust the files in this folder" and waited. The master assumed it was working
and moved on. — This is the single most-exercised path in the product: a pane blocked on
a question the human must answer is what the deck headlines, what `waiting_on: "user"`
means, and what the phone turns into a tappable answer that round-trips into the pane.
The daemon would have had it classified within a tick or two. Nobody asked it.

**tmux renamed the windows out from under the labels.** Windows auto-renamed to the
running command, destroying the names the master was addressing them by — precisely the
failure [agent-setup.md](../agent-setup.md) already warns about and already defends
against: `Pane.label` rejects command-shaped names (`claude`, `codex`, shells, runtimes)
exactly so a fleet does not read as eight identical rows.

**Window indices renumbered, and the master typed into the wrong window.** Indices are
positional and shift when a window closes. — Pane ids (`%3`) are stable for the life of
the pane, which is why every daemon endpoint is keyed on one and why agent-setup calls a
pane id "the one that is always unambiguous." Addressing a long-lived fleet by index is
the bug, not the symptom.

**Expiring gcloud credentials took out the master and a slave at the same moment.** The
slaves are separate processes with separate context windows, but they share a machine,
an ambient credential store, and a clock. Isolation in the way that matters for
reliability is not something sibling panes provide. — Not a bus problem, and openbus
does not fix it; it belongs here because it is more evidence for the same shape. Neither
process announced that it had lost auth in any way the other could see.

**Nothing signalled completion.** The master polled panes on a timer and read prose to
decide whether work had finished. — The daemon already tracks `activity`,
`idle_seconds`, and `state_since` per pane, writes an LLM one-line summary of each
activity burst once a pane goes quiet, appends to a per-pane event log with a monotonic
`events_seq`, and — through `/api/state?v=<version>` — will *hold a request open* until
any of that changes. The master hand-rolled a worse version of a change-triggered
structured feed that was running on localhost the entire time.

Seven failures, seven primitives, zero reachable from inside the session that needed
them.

## The diagnosis: action without perception

The narrative puts it in one line: "A system that only sends keystrokes is a macro
recorder; one that sees is a control plane." The master was a macro recorder.

It is worth being precise about why that is worse for agent panes than for shell
commands. A blind `grep | awk` at least returns an exit status, a stderr, and a
terminating process — three signals that something happened. An agent pane returns none
of those. It never exits, it never sets a status, and its only output is prose on a
screen that also contains the last hour of prose. Fire-and-scrape degrades from unreliable
to *unfalsifiable* the moment the thing on the other end is an agent rather than a
command.

So the master needed the perception half, and the perception half exists. The daemon
watches every pane on a 1.5s tick, fingerprints it to decide whether the screen actually
changed, classifies changed screens into structured state, tracks the transitions, and
round-trips input back into any pane. The phone consumes exactly that and is a good
client of it. The master needed the same three things the phone needs and had to
hand-roll all three out of `capture-pane` and string matching.

## The right size: a third client, not a second control plane

The tempting conclusion from a session that went this wrong is that orchestration needs
its own machinery — a control plane by which tmux-rc talks to master sessions,
dispatches work, and tracks it. That is the wrong size, and the reason is the same
reason the narrative gives for the bus existing at all.

**A second plane would be duplicate perception.** The expensive, hard, interesting part
of this system is the classified view of the screen, and it is already computed for
every pane on every tick and billed once. A second plane either re-runs that
classification — paying twice for the same pixels, and the per-pane classifier bill is
not theoretical; agent-setup documents $168 in a day when the cadence misfires — or it
reads the daemon's classification, at which point it is a client of the daemon and
calling it a plane is just naming.

**Two writers into one pane is a split brain.** The daemon forces a re-parse after every
send specifically so its state cannot drift from the screen it just changed. A second
sender bypassing that makes the daemon's view stale in a way it has no way to detect.
Everything downstream assumes one keystroke path: the audit log that records every send
with its actor, the tiered-confirmation model in the
[control-plane design](agentic-control-plane.md), and build item 2's consent rules for
agents typing at each other. Each of those gets substantially harder with two paths and
is nearly free with one.

**The symmetry is the evidence.** Ask what the master actually wanted and the list is:
what is on that screen, send this and tell me it landed, wake me when something changes.
That is the phone's API. If orchestration wanted a fundamentally different *shape* of
data, that would be a real argument for separate machinery — but it wants the same data
with different ergonomics, and "same data, different ergonomics" is the definition of a
client. Live Mode already proved the pattern: it consumes the same state the deck does,
presents it as a voice conversation instead of cards, and nobody proposed a second plane
for it.

The honest counter-argument is that an agent is not a thumb. It addresses panes by role
rather than by tapping one, it wants several at once, it would rather block on an event
than watch a deck, and it has no attention budget to protect — the whole
attention-economics problem (build item 4) inverts, because an agent *wants* the raw
feed the human must be shielded from. Those differences are real. They are differences
in filtering and delivery over one perception loop, which is a client concern, and the
rest of this note is about sizing that client honestly.

## What is actually missing: three verbs

An agent-facing interface to what the phone already consumes. Three verbs, and only one
of them is genuinely new work.

**Read pane state** — mostly exists. `/api/digest` is already documented in the code as
"the endpoint for agents/scripts", and returns per pane: label, self-published title,
window index, activity, idle seconds, headline, pending question, the idle summary, and
recent timestamped history. The gap is addressing and filtering convenience, not
structure.

**Send input, with delivery confirmation** — the real gap. `/api/panes/{id}/send`
returns `{"ok": true}` once tmux accepted the keystrokes, which is a true statement about
tmux and not the claim the caller needs. A confirmed send makes the stronger claim, out
of work the endpoint already does: send, force the re-parse that already fires, and
answer with what the screen says afterwards — is the input line clear, did the pane leave
the state it was in, is the same question still up. That is the correct check from the
false-negative failure above, computed once in the daemon by the component that already
understands screen structure, instead of re-derived badly by every caller.

The limit is worth stating: "landed" is not "understood" and not "accepted." Confirmed
delivery only closes the first of those three, and it is the only one that was failing
silently.

**Subscribe to state changes** — exists as `/api/state?v=<version>`, shaped for a deck:
it wakes on *any* change to *any* pane, because a deck redraws wholesale. An agent wants
a narrower predicate — this pane stopped working, this pane is asking something, this
pane has been idle past N seconds. Same feed, an agent-shaped filter on top, and the
long-poll discipline (hold ~25s, return the new version, re-hold) already survives the
tunnel.

### Why MCP, and what was rejected

**MCP over the existing daemon** is probably right, for one reason that has nothing to do
with protocol quality: it lands the verbs in the harness's tool list, where the master
will actually see them. The master in the evidence session *could* have curled
`/api/digest` on localhost at any point. It did not, and not out of ignorance — it failed
by omission while busy, which is the failure mode documentation does not fix and a
prompted tool list does. MCP also adds no perception: it is transport over endpoints that
already exist, which is exactly the property that keeps this a client.

Rejected, with reasons:

- **Nothing — document the HTTP API and tell masters to curl it.** Cheapest possible
  answer and it half-works today. Rejected as the whole answer: it gives read, not
  confirmed send and not subscribe, and it loses on discoverability above. Worth doing
  anyway as the layer everything else sits on.
- **A CLI wrapper** (`openbus send %3 "…"` that verifies before returning). Genuinely
  attractive: no protocol, works in any harness including ones with no MCP support, and
  composes in shell. Rejected as the primary interface because subscribe has no good CLI
  shape — a blocking `openbus wait` occupies a tool call for minutes and most harnesses
  will not hold one open. Keep it as a thin fallback; over these endpoints it is a few
  lines.
- **A harness-specific integration** — a Claude Code plugin or a bespoke subagent type.
  Rejected on the founding rule: tmux-rc integrates with no agent in particular, and a
  Claude-only orchestration path is the same lock-in the wedge exists to route around.
  The second master will not be Claude Code.
- **A new agent-to-agent socket protocol.** Rejected as the second control plane wearing
  a different hat, and it needs cooperation from harness vendors — the dependency the
  whole approach refuses to take.

## tmux slaves vs. in-process background workers

The same orchestration can be done with in-process background agents, and the comparison
matters because it determines what to build next.

**What tmux slaves have.** They are *visible* — to the human at the desk and on the
phone, which is how the unsent-prompt bug was caught at all; a background worker's
mistake is invisible until it returns, if it returns. They are *steerable mid-flight*:
you can type a correction into a running slave, and a launched worker takes no further
input. They have *independent context budgets*, which is the big one — three slaves
reading three codebases cost the master nothing, and that is what made one session
driving three viable. And they *outlive the master*: it can be compacted, cleared, or
killed and the work continues, because the work is not owned by a conversation.

**What background workers have.** A structured result and a completion notification —
precisely the two things missing above. No addressing problem, because there is nothing
to address.

**What each lacks.** Workers cannot be steered after launch, spend the master's context
on the way back, are invisible while running, and die with their parent. Slaves return
prose on a screen, signal nothing on completion, and — per the gcloud failure — are
isolated far more shallowly than "separate process" suggests: same machine, same
credentials, same working tree if you are careless. Panes are not sandboxes and should
not be sold as such.

**The conclusion.** These two models differ on exactly one axis that matters, and it is
not autonomy — the slave model already has *more* autonomy than the worker model, which
is part of why it failed the way it did. What it lacks is the **return path**. So the
move is to give the tmux model the worker model's status channel, not to move
orchestration in-process and give up visibility, steerability, and independent context to
get it.

And the status channel is not a feature to invent. Activity transitions, idle time, and a
one-line summary per burst are already computed for every pane; the completion signal is
a feed to expose, not a mechanism to build.

One caveat, in keeping with the rest of openbus: a status channel derived from the screen
is *inferred*, not reported. "Idle for ninety seconds, summarized as opened PR #191" is
strong evidence of completion, not proof of it. That is the same trade the whole system
makes — the screen is the only honest view — and it is strictly better than a master
reading prose, but it is not an exit code and should not be written down as one.

## What this forces into the open

The agent client is the first concrete consumer of the narrative's build items 1 and 2,
and it turns both from design questions into shipping questions.

**Addressing (item 1).** Two of the seven failures were addressing failures. Pane ids
solve them and are what the daemon already uses, but a master thinks in roles — "the one
reviewing the auth PR" — not in `%7`. Some stable naming that survives tmux renaming
windows is the first thing to get right, and agent-setup's advice (let the agent publish
its own title) is most of the answer already.

**Consent (item 2).** A master that can type into a sibling's pane can derail it, and
this is no longer hypothetical. Who may address whom; whether a message announces that a
machine typed it, so a human reading over the pane can tell; whether the human can veto
mid-flight. Related: two agents that can both perceive and type at each other can
ping-pong, so some turn discipline belongs in the same design.

**A reply path, cheaply.** A slave today can only leave text on its own screen and hope
someone reads it. But if the master subscribes to state changes, then a slave writing one
status line to its own pane *is* a legible return channel with no new plumbing at all —
the daemon already sees it, classifies it, and can wake a subscriber on it. That may be
the entire agent-to-agent messaging layer for v0, and it is worth trying before anything
heavier.
