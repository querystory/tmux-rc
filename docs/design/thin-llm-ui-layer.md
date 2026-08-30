# The thin LLM layer on top of the UI

Status: **concept, with one shipped instance.** The idea generalizes past tmux-rc, so
it's written as its own note rather than buried in a feature doc. The worked example is
`copyables` (the card's one-tap copy rows) — see [architecture](architecture.md) for
where the parse pass sits in the system.

## The claim

There is a class of UI problem that is **impossible to solve generically and trivial to
solve semantically**. Traditional software has to solve it generically, because code has
to work for all inputs. So it either ships a bad generic answer, or — far more often —
ships nothing and leaves the work to the user.

An LLM already sitting in the render path can just *decide*, per screen, and be right.
Not by generating the interface, and not by answering questions about it: by making the
small judgment call the UI needs in order to offer the right affordance, on this screen,
right now.

That is a thin layer. It doesn't own the interaction; the interface is still an
interface, still deterministic, still fast. The model contributes one decision the code
could not have made.

## The worked example: getting text OUT of a terminal

Copying text out of a terminal on a phone is miserable, and the reasons are structural
(see [#97](https://github.com/querystory/tmux-rc/issues/97)): a pane is a fixed-width
character grid, so a selection drags column padding, box-drawing borders and gutter
glyphs along with it; long lines are hard-wrapped, so a command pastes with newlines
mid-token; and on a touch surface a drag pans the viewport instead of selecting. The
same problem exists on the desktop the moment an agent prints content inside an indented
or quoted block.

Every generic fix is bad, and each fails differently:

- **Copy the whole frame.** Now the user has the borders, the status line, and the
  neighbouring output — they must edit it down by hand, which was the original problem.
- **Detect structure with rules.** Fenced blocks, indentation runs, box-drawing
  rectangles. This is a losing arms race against every TUI's chrome, and it cannot tell
  a code block the user wants from an ASCII table it should leave alone.
- **Improve raw selection.** Genuinely worth doing, and orthogonal — but it still asks
  the user to aim precisely at a wrapped grid on a phone.
- **Copy buttons in the source tools.** Requires every agent CLI to cooperate. tmux-rc's
  whole premise is that it never integrates with the tools it watches.

The semantic version is one sentence: *"what on this screen exists to be pasted
somewhere else?"* The model already reads every frame to classify the pane, so answering
it costs one more field in a parse that was happening anyway. It returns a verbatim,
paste-ready payload — wrapped lines rejoined, borders and gutters stripped — plus a
one-line summary of what the text *is*. The card renders a labeled copy button.

And critically: the hard part isn't extraction, it's **judgment about intent**. The rule
that makes the feature good is *destination, not copyability*: a command the agent is
asking permission to run is NOT a copyable, because the user's job there is to approve
it, not carry it elsewhere. A copy button next to a live permission prompt competes with
the real affordance. No regex expresses that distinction. A sentence of prompt does.

## Why this is a different move than "AI in the product"

Most LLM features in UIs are one of three shapes, and this is none of them:

- **A chat box bolted on.** The model is a destination the user visits, and the burden
  is on the user to ask. Here the user asks nothing; the affordance is simply *there*
  when it applies.
- **Generative UI.** The model emits the interface. That trades determinism for
  flexibility, and the result is slow, unpredictable, and hard to make accessible. Here
  the interface is hand-built and fixed; only a *decision* comes from the model.
- **An agent that acts.** Tool-calling with side effects, requiring confirmation flows
  and a trust model. Here the model's output is inert data that a deterministic renderer
  displays. The blast radius of a wrong answer is one unhelpful button.

The distinguishing property: **the model's output is a small structured field consumed by
ordinary code.** That's what keeps the layer thin, cheap, and safe.

## The economics matter, and they're better than expected

This works on a *small, cheap, fast* model — the same `gemini-flash-lite` class pass
that already classifies the pane. It got the extraction right on the first real call:
box borders stripped, a wrapped commit body rejoined into one paragraph, a
backslash-continued `gcloud` command turned into one runnable line, three sensible
labels.

That is the load-bearing economic fact. The judgment needed here is *linguistic and
contextual*, not deep reasoning — exactly what small models are now good at. So the
pattern doesn't require frontier spend on every frame; it rides a pass that already
exists, on a model chosen for latency.

Two properties follow, and they're why this generalizes:

- **Marginal cost per decision is near zero** when the parse is already happening. New
  affordances are new *fields*, not new pipelines.
- **New affordances cost prompt, not architecture.** `copyables` shipped as a prompt
  section, a schema field, a validation guard, and a render function. No new service, no
  new model, no new user-facing concept.

## What guardrails this needs

A thin layer is only safe if the code around it stays suspicious. The rules that fell out
of building this one (see `openbus/classify.py` and `web/app.js`):

- **The model's output is untrusted input, always.** It derives from pane content an
  attacker could control. Labels are bidi-stripped, length-capped, and rendered as
  `textContent`, never markup — the same treatment link labels get.
- **Bound the payload in code, not in prose.** These fields ride every state poll for
  every client. Cap count and size server-side; *drop* malformed entries rather than
  repair or truncate them (a silently clipped paste is worse than no paste).
- **Validate before capping.** The prompt asks for the most-relevant entries first, so
  slicing before filtering lets one malformed early entry burn a slot and drop a good
  one.
- **Enforce cross-field rules where the fields are comparable.** The prompt said "a URL
  belongs in `links`, not `copyables`" and the model still duplicated one, producing a
  copy row stacked under a link chip offering the same string. Prose guidance is a
  preference; the deduplication belongs in code, where both fields are in hand.
- **Regression-test the judgment, not just the plumbing.** The prompt-eval corpus
  (`research/eval/`) compares by *presence* — did the model notice there was something
  paste-worthy, or correctly notice there wasn't. It caught the permission-prompt
  over-fire before it shipped. Blessing exact payload text would be brittle; blessing the
  decision is not.

## Where else this shape applies

The test for a candidate: *is the decision easy for a human reading the screen, hard to
express as a rule, and consumed as a small structured field?* Within tmux-rc, the
existing parse already answers several — `links` (which URLs is the screen *offering*,
versus decorative ones in a log), `question` (is this a live affordance or the agent's
own rhetorical question — the distinction [#85](https://github.com/querystory/tmux-rc/issues/85)
was about), `tables`, and the headline itself. `copyables` is the first that creates an
*action* rather than describing state, which is why it feels different in the hand.

The broader bet: **any interface over content it doesn't control** has a backlog of
affordances it never shipped because they couldn't be done generically. A thin semantic
pass is how those become cheap.

## Why this was found by using it

Worth recording, because it's the actual method: this feature came from doing real work
from a phone — drafting a message to paste into a conference app with no API, running a
`gcloud auth` command on a remote box — and hitting the wall repeatedly. Not from a
feature review.

The refinements came the same way, in minutes, from looking at the thing on a real
phone with real panes:

1. The first version showed **only the label**. "API token" doesn't say *which* token, so
   deciding whether to copy meant copying and pasting somewhere to look — the terminal
   problem again, one step removed. Fixed by adding a one-line monospace preview of the
   payload.
2. The first version **duplicated links**, stacking a copy row under a chip offering the
   same URL.

Neither is visible in a spec, a test, or a screenshot of synthetic data. Both are obvious
within seconds of real use. The tight loop — build, deploy to the live daemon, look at it
on the actual device, fix — is what turned a plausible feature into a good one, and it's
the argument for keeping that loop short: **the product improves at the rate you can
close it.**
