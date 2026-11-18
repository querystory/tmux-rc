# Design: the daily and weekly digest

Status: **implemented** as a script on a timer (`scripts/digest.py`, `make digest`,
`make install-digest`), with an eval (`scripts/digest_eval.py`, `make digest-eval`).

## What it is for

Someone running a fleet of agents wakes up to dozens of changes in several repositories.
The digest answers three questions on a phone: what got done, what state it is in, and
what they must do. It answers them by **theme of work**, in words and pictures. Change
numbers, repository names and line counts stay behind a tap: nobody plans a day around a
number.

## Why a model, and why only for the words

Deciding that eleven changes are one piece of work, and saying what is now true because of
them, is a reading of meaning. A rule over titles cannot do it, so one model call does:
it places each change in a theme, names the themes, and writes each theme's outcome line,
the few words of each ask, and the two verdicts. It is a model that writes well rather
than the parser's fastest one, whose digest read as filler; a run a day can afford it.

Everything a reader could act on wrongly is a rule in code instead. A change's stage,
whether it needs the user and which button that earns, each theme's state word and share
of effort, every link, and the list of worktrees safe to remove are computed from GitHub,
git and the transcripts. The model is handed those as given and never produces a number.
Cleanup advice in particular has no path from the model to the page: it is a list built
by gates (clean, pushed, merged, no pane, idle for a day), because a wrong "safe to
delete" costs work. A model sentence that names a change number or a repository is
dropped before the page is written.

## Why themes keep their identity

A page that reshuffles its cards every morning cannot be skimmed. Themes live in a small
registry beside the daemon's other state. Each morning the model sees the registry and
only the work that has no theme yet, so a change, once placed, stays put. A rename keeps
the theme's id, a new theme needs two changes, and a theme with nothing in it for two
weeks retires. An empty registry takes two calls on its first morning, one to form the
themes and one to write about them; every later run is one.

## Why effort is joined by tool calls

Agents work in a worktree through `git -C` while their session sits at the repository
root. The session's working directory therefore never names the worktree, and its git
branch is the root's: trusting it once pinned a week of unrelated effort on one change.
The worktree most of a run's tool calls name is the reliable signal, then the one change
a session links. About two thirds of effort joins to a change this way. Effort that
belongs to no worktree is coordination and is drawn as its own outlined block rather than
forced into a theme. Effort is priced at list rates only so themes can be compared; plans
are subscriptions, so it is never shown as money.

## Why it runs outside the daemon

A digest is one slow request a day. In the daemon it would share the parser's timeout,
backoff and restart cycle; as a timer unit it can take minutes, fail loudly in the
journal, and be rerun by hand. It reads the daemon over its local API and writes two
scratch pages, which the daemon already serves and announces.

## Alternatives rejected

- **Direction B for the daily, the story of the day.** A timeline of what happened reads
  well but puts the picture of where everything stands second. Direction A, picture
  first, is built; B remains the alternative.
- **Letting the model write the whole page.** An earlier Markdown digest did, and stated
  counts and cleanup advice that the facts did not hold.
- **Grouping by repository or label.** Themes cross repositories, and labels are not kept.
- **Calendar weeks.** The week is the last seven days, so the page is as useful on a
  Wednesday as on a Monday.

## Scoring a model against a hand-written digest

The eval runs the model on frozen facts and compares it with a digest a person wrote from
the same facts. Advice that could lose work fails the run outright: merging what is not
approved and green, or pruning or closing work that is still open. Grouping, state words,
the weekly verdict's largest theme, asks left out, leaked numbers, the theme count and
stability on a rerun lose points. A person still reads the outcome lines, which the eval
prints beside the baseline's. The committed baseline is invented; a real one stays on the
machine it describes.

## What the dashboard reads

Each run also writes `summary.json` beside the daily page: the verdict, up to three asks
with their links, each theme's name, state and share, and the plan's pace. It is cut from
the digest the page is drawn from, so the dashboard's card and the page cannot disagree.
