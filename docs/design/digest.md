# Design: the daily and weekly digest

Status: **implemented** as a script on a timer (`scripts/digest.py`, `make digest`,
`make install-digest`), with an eval (`scripts/digest_eval.py`, `make digest-eval`).

## What it is for

Someone running a fleet of agents wakes up to dozens of changes in several repositories.
The digest tells them, on a phone, what got done, what state it is in and what they must
do, by **theme of work**, in words and pictures. Change numbers, repository names and
line counts stay behind a tap: nobody plans a day around a number.

## Why a model, and why only for the words

That eleven changes are one piece of work, and what is now true because of them, is a
reading of meaning, so one model call does it: it files each change under a theme and
writes each theme's outcome line, the few words of each ask, and the two verdicts. It is
a model that writes well, not the parser's fastest, whose digest read as filler.

Everything a reader could act on wrongly is a rule in code: a change's stage, whether it
needs the user and which button that earns, each theme's state and share, every link.
The model is handed those and never produces a number. Cleanup advice has no path from
the model to the page at all: it is a list built by gates, because a wrong "safe to
delete" costs work. A model sentence naming a change number or repository is dropped.

A theme's state is a count, so it is a rule too. It is "Live on main" only when nearly
all of its work is on main and none is held. Otherwise it takes the state most of its
outstanding work is in: "Stuck" (checks failing), "Live on your machine, not on main",
or "In progress", a tie going in that order. One red change among many stays a red dot.

## Why themes keep their identity, and why the file is yours

A page that reshuffles its cards every morning cannot be skimmed. Themes live in
`~/.local/share/tmux-rc/digest/themes.json`. The model sees that registry and only the
work with no theme yet, so a change, once placed, stays put. A new theme needs two
changes; one with nothing in it for two weeks retires. Until work has been placed a run
takes two calls, one to file and one to write; after that, one.

Left alone, a model splits work by technical area, and the reader thinks in their own
projects. So the registry is plain JSON to edit. A name in it stays: the model names a
theme once, when it creates it, and afterwards rewrites only its outcome line. A theme
written by hand, even as just a name, is filled by the model, and may carry a `holds`
line saying what belongs in it, which the model reads and never writes.

## Why the pages draw at most seven themes

Seven is what a phone shows without becoming a list to scroll, and a model asked for "four
to seven" still starts an eighth. So the limit is a rule on the page, not a plea in the
prompt. With more than seven themes at work, the pages draw the first six and fold the rest
into one card, "Everything else", whose outcome line is the folded themes' own lines and
whose state and share are the same rules over their combined work. The order is the page's
own: themes that need the user first, then by effort, so what is folded is the smallest
work that asks nothing.

Only the drawing is folded. The registry keeps every theme, and the model still sees and
writes each one, so a folded theme that grows comes back as its own card with its name and
members intact. Refusing the model's eighth theme was rejected: its work would sit in no
theme and so nowhere on the page. Merging themes in the registry was rejected too: that
file is the user's, and a merge cannot be undone when the small theme grows.

## Why effort is joined by tool calls

Agents work in a worktree through `git -C` while their session sits at the repository
root, so the session's directory never names the worktree and its git branch is the
root's: trusting it once pinned a week of unrelated effort on one change. The worktree
most of a run's tool calls name is the signal, then the one change a session links; about
two thirds of effort joins to a change this way. Effort in no worktree is coordination,
drawn as its own outlined block. Effort is priced at list rates only to compare themes;
plans are subscriptions, so it is never shown as money.

## Why a timer outside the daemon, and why one page

A digest is one slow request a day. In the daemon it would share the parser's timeout,
backoff and restarts; as a timer unit it can take minutes, fail loudly in the journal and
be rerun by hand. It reads the daemon's local API and writes scratch pages, which the
daemon already serves and announces.

The app's viewer runs a page's script only in the page it opened; a page its frame then
navigates to runs none. A link from the daily page to the weekly one landed on a page
with no theme and no taps, so each folder gets one page holding both views, switched in
place, taking its theme from the message the viewer posts.

Each run also writes `summary.json` beside the daily page for the dashboard's card: the
verdict, up to three asks with links, each theme's name, state and share, and the plan's
pace. It is cut from the digest the page is drawn from, so the two cannot disagree. The
card gives the verdict three lines: the model is told the length that fits, and the card
cuts a longer one there, because the daily page a tap away has it whole.

## Scoring a model against a hand-written digest

The eval runs the model on frozen facts and compares it with a digest a person wrote from
them. Advice that could lose work fails the run: merging what is not approved and green,
or pruning or closing open work. Grouping, state words, the weekly verdict's largest
theme, asks left out, leaked numbers, the theme count and stability on a rerun lose
points. A person still reads the outcome lines, printed beside the baseline's. The
committed baseline is invented; a real one stays on the machine it describes.

## Alternatives rejected

- **Direction B for the daily, the story of the day.** It reads well but puts the picture
  of where everything stands second. Direction A is built; B remains the alternative.
- **Letting the model write the whole page.** An earlier Markdown digest did, and stated
  counts and cleanup advice the facts did not hold.
- **Grouping by repository or label.** Themes cross repositories, and labels are not kept.
- **Calendar weeks.** The week is the last seven days, as useful on Wednesday as Monday.
