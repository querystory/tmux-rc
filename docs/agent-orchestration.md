# Orchestrating agents from an agent

Status: **guidance for users** — the mechanics of driving agent sessions in other panes
from an agent session, and which of those mechanics fail without telling you.

[Configuring your agents](agent-setup.md) covers making a pane *readable* — settings
that let tmux-rc name and watch your agents well. This page covers the other direction:
one agent session that **drives** others in sibling panes with `send-keys` and
`capture-pane`. It is a how-to, not an argument; the argument for doing this on a bus
instead is [the agent client](design/agent-client.md).

## The pattern, and why people reach for it

One session — the orchestrator — spawns others in sibling windows, hands each a task,
and steers them by typing into their panes. It works, and it beats in-process background
workers on four counts: the fleet stays visible to a human (and to the phone), each
session can be corrected mid-flight, each has its own context budget so the orchestrator
never pays to read what they read, and they outlive the orchestrator — it can be cleared,
compacted or killed while the work continues.

What you give up is the return path, and that is what the rest of this page is about.

## The governing property: the failures are silent

Nothing here raises an error. Not an exception, not a non-zero exit, not a warning. Work
looks dispatched when it was never received, so an orchestrator reports progress that is
not happening and a human finds out by walking the windows.

That single property should drive how you write orchestration prompts and scripts:
**every step needs positive confirmation, and the absence of an error confirms nothing.**
Every item below is an instance of it.

## Submitting a prompt is two steps, not one

`tmux send-keys -t <pane> 'message' Enter` as a single call frequently delivers the text
and loses the Enter. The TUI has not finished consuming the paste when the Return
arrives, so the message sits in the input box, composed and unsent, looking for all the
world like an agent that is thinking about it.

Send the literal text, **pause**, then send `Enter` as its own call. The pause is the
part that does the work: tmux delivers back-to-back `send-keys` in order, but ordering
only guarantees the Return arrives after the paste, not after the TUI has finished
reading it — a second `send-keys` issued immediately drops the Enter about as often as
the single call does. Two calls make the delay expressible; the delay is what makes it
land. The daemon settles deliberately for this reason (`TMUXRC_ENTER_SETTLE_S`, 0.3s by
default) on top of the chunking it already does under tmux's 16KB message cap, and
re-checks the pane's identity across the wait, which is why input from the phone does
not exhibit this.

Retrying the Enter is safe: a bare Return on an empty input box is a no-op in every agent
TUI worth driving, so a retry loop cannot double-submit. It can only fail to notice that
the first one worked, which brings us to the next part.

## Confirming submission: only the input line is authoritative, and it lies too

The tempting check — grep the pane for the text you just sent — **cannot fail**, which
is not the same as working. An *unsent* message is sitting in the composer; a *sent* one
is echoed into the transcript and stays in scrollback. Both match, so the check reports
success either way: on a dropped Enter it is a straight false positive, and the rest of
the time it tells you nothing you did not already know. The question is never whether the
text is present; it is *where* it is. Still in the input box means unsent; scrolled up
into the transcript means sent.

So read the input line and check that it is empty. That is the right idea, and it trades
a check that always says yes for one that can say no when the answer is yes. Four ways it
does that, all worth knowing before you trust it:

- **The input glyph is not unique on screen.** At least one popular agent TUI renders
  transcript messages with the *same* glyph its input line uses, so a capture routinely
  contains two or more matching lines. Take the **last** match, not the first — the first
  is usually a message you sent earlier, which reads as "still unsent" forever.
- **An empty input box is not an empty string.** The same TUI pads its empty input line
  with U+00A0, a non-breaking space, which POSIX `[[:space:]]` does not match in most
  locales. Strip the glyph and test for emptiness naively and the box never reads empty,
  so a perfectly good send reports as failed.
- **A greyed placeholder is not typed input.** Agent TUIs draw hint text after the glyph
  when the composer is empty ("Press up to edit queued messages" and the like). A
  plain-text capture keeps the characters and loses the styling that made them grey, so
  the check reads a hint as a draft and reports a good send as failed. This one is not
  fixable at this layer: telling grey from typed requires capturing the escape sequences
  too, which is what the daemon does before marking that run `⟪placeholder⟫`.
- **The screen is stale until the TUI repaints.** There is no event to wait for, only a
  sleep long enough that you probably saw the redraw. Too short and you get a false
  negative; too long and every steer costs seconds.

`scripts/steer.sh` is a working helper that does all of this: literal text, pause,
separate Enter, retry, and an input-line check that handles the first two. It refuses to
type into a composer that is not already empty, because a draft left by an earlier
dropped Enter would otherwise be concatenated with your message and submitted as one, and
it takes a per-pane lock so that two steers of one pane cannot interleave — which an
orchestrator steering a fleet in parallel will otherwise do. The glyph it matches is
overridable (`STEER_GLYPH`) because it is harness-specific, which is itself a hint about
how far this approach generalises.

**Prefer the daemon's endpoint when it is running.** `POST /api/panes/<id>/send`
serialises sends, records who made them, and re-parses the pane afterwards; typing behind
the daemon's back leaves its view stale until the next capture and leaves no record at
all. The helper is for when there is no daemon — and as evidence for the argument below.

**It is still guesswork, and that is the point.** Three of those four were found by
measurement rather than by reading the screen carefully, and the fourth cannot be fixed
from a plain capture at all. Polling a terminal to find out whether a keystroke arrived
is inference about a repaint, not a delivery receipt. The script is best read as
evidence for [the agent client](design/agent-client.md): a daemon that already watches
every pane, fingerprints it for change, and re-parses it after every send it makes can
simply *report* delivery. Nobody has to poll for something another process is already
watching.

## A spawned session is not running until you confirm it

A newly launched agent commonly stops on a first-run prompt — "do you trust the files in
this folder" and its equivalents — and waits. Observed: one session sat on that prompt
for ten minutes while its orchestrator assumed the work was underway.

Capture the pane after every spawn and confirm the agent is at its input line before
sending it anything. Treat a spawn as a step that can block, not as a step that returns.

## Addressing: names get clobbered, indices move

Two separate ways to type into the wrong pane:

- **tmux auto-renames a window to the command running in it**, so windows you named turn
  back into `claude`, `node` and friends. Set `automatic-rename off` on the window
  *before* `rename-window`, or the name you set is temporary. (tmux-rc already defends
  its own labels against this — `Pane.label` rejects command-shaped names outright — but
  that does not help a script addressing panes by name.)
- **Window indices renumber when a window closes**, silently retargeting anything that
  addressed a window by number.

Address by **pane id** (`%3`). It is what every tmux-rc endpoint is keyed on and the
only form that is unambiguous while the pane lives — but note that bound: tmux
*recycles* pane ids, so an id you cached before a pane closed can later name a different
pane entirely. That is why the daemon pairs the id with the pane's pid. An orchestrator
holding ids across a long run should re-resolve them rather than assume they still mean
what they meant. See [agent-setup](agent-setup.md) for how labels are derived when you
want a pane to read well on the phone too.

## Sibling panes are not a sandbox

Separate processes with separate context windows still share a machine, a clock, an
environment, and an ambient credential store. An expiring cloud credential has been
observed taking out an orchestrator and one of its sessions in the same moment, and
neither announced the loss in a way the other could see.

Do not size the blast radius of a task by assuming pane isolation. And note the failure
shape repeating: a shared dependency dying is exactly the kind of event that produces no
signal anywhere.

## Interactive auth is a hard human boundary

A password prompt, an interactive cloud login, a hardware key tap — none of these can be
driven from another pane, and none should be. Plan for them as escalations to a human
rather than as steps an orchestrator retries.

## Nothing signals completion

There is no notification and no exit status. The orchestrator's only options are to poll
panes and interpret prose, which is expensive in its context and unreliable in its
conclusions.

Two things make this materially better without new machinery:

- **Ask for a machine-greppable status line.** Tell each session to end its work by
  printing a single agreed line. It turns "read the prose and decide" into a string
  match, and it costs one sentence in the spawn prompt.
- **Front-load the spawn brief.** The highest-leverage artifact in this whole pattern is
  the prompt that starts a session: state the constraints already discovered, and say
  what was ruled out and why. Otherwise each session rediscovers them separately, at full
  price, and reaches different conclusions.

Both are workarounds for a missing return path. The daemon already computes what the
return path needs — per-pane activity state, idle time, a one-line summary written when a
pane goes quiet, and a change-triggered feed that will hold a request open until
something happens. Exposing that to the orchestrator is the subject of
[the agent client](design/agent-client.md); until then, the status line is the cheapest
honest substitute.
