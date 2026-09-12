# Configuring your agents for tmux-rc

Status: **guidance for users** — how to set up the coding agents in your panes so
tmux-rc reads them well and cheaply. Nothing here is required: tmux-rc's founding rule is
that it watches what a pane *renders* and integrates with no agent in particular, so an
unconfigured agent works. These settings make it work *better*, and two of them make it
work considerably cheaper.

## Why your agent's settings are tmux-rc's business at all

tmux-rc never asks an agent what it is doing — it looks at the pane, the same pixels you
would. Two consequences follow, and every recommendation below is one of them:

1. **Repainting costs money.** The daemon decides whether to re-run its LLM classifier by
   fingerprinting the pane text each 1.5s tick; a screen that differs from the one last
   classified gets re-read. Chrome that *moves on its own* therefore bills you for
   information you already had. See [parse cadence](../design/parse-cadence/) for why the
   trigger is change-based and what the fingerprint already strips.
2. **If the pane doesn't say who it is, the phone can't either.** A card is headed by
   whatever the agent published about itself. Publish nothing and the heading falls back
   to tmux-side names that were never chosen to tell two agents apart, so a fleet reads as
   a column of near-identical rows.

## Let the agent name itself

You do not have to name anything — *provided the agent says who it is on screen*. The
classifier reads the agent's own session title out of the captured text (that is what the
`session` field in the parser prompt is for) and it becomes the pane's **label**, so the
pane reads as *Review 4745* or *airbyte-value-population* rather than as its command.

That is the one setting worth changing for Codex: **include `"thread-title"` in
`status_line`.** The parser will also take the title from a `Thread renamed to <name>`
line, but that is a single line of transcript — it scrolls away and the pane loses its
name again. The status bar is the only place the title stays on screen to be read.

(Agents that set the *terminal* title get a second route, but only to the desktop card,
which renders `title || label` — `title` being the terminal title, or failing that a name
the bootstrap read generates from the pane's scrollback. The phone's card renders the
label alone. So a terminal title is a bonus on one client, never a substitute for a name
the label can carry.)

### When nothing is parsed off the screen

The label falls back to `Pane.label` (`openbus/tmux.py`): the window name if it looks
deliberate, otherwise the session name — or the cwd basename, when the session name looks
like a default too — qualified with the **window index**. That qualification is the point
of the form: a session name is shared by every window in it, and cwd basenames repeat
across every pane working in the same checkout, so either one bare turns a fleet into a
column of identical headings.

"Looks deliberate" is judged on the name itself, not on who set it. tmux names a window
after the command that launched it, so the rejected set names the shells and runtimes
*and the agent CLIs it knows about* (`claude`, `codex`, `gemini`, `aider`): eight agents
would otherwise be eight rows headed `claude`, and a qualified coordinate at least tells
them apart. It is a literal list, so a CLI that is not on it is taken at face value and
its auto-name collides as before — one more reason to rename the window yourself. The
flip side is that `tmux rename-window claude` is rejected too — the check cannot tell your
rename from tmux's.

So name the window something that is not a command name:

    tmux rename-window 'db migration'

Two limits. tmux lets two windows share a name, so distinctness is on you. And the label
stops at the window: split panes in one window share it, so where you need a handle that
*cannot* be ambiguous, address the pane by id (`%3`) or numeric `session:window.pane`.

## Codex: turn off the sparkle animation

Codex's TUI draws an ambient "sparkle" — scattered single-dot braille
(U+2801/02/04/08/10/20/40/80) on the rows around its input box, reshuffled every frame.
Unlike a spinner it is not one cell in a fixed spot; the dots *move*, so the surrounding
spaces shift with them and the whole band of lines signs differently every frame. Three
churning lines out of thirty made every 1.5s tick look like a brand-new screen, so idle
panes re-classified continuously. Measured here before it was understood: **42 classifier
calls per 10 ticks across six panes, and roughly $168 of Gemini spend in one day.**

In `~/.codex/config.toml`, under `[tui]`:

    [tui]
    animations = false
    whimsy = false

**tmux-rc already defends itself against this** — `_fingerprint` in `openbus/watcher.py`
flattens the band of lines the animation plays over, so you are not exposed if you skip
this. Turn it off anyway if you can: suppressing decoration at the source is strictly
better than pattern-matching it downstream, and the same reasoning applies to whatever
animated chrome your agent adds next, which tmux-rc will not know about.

The general rule this is an instance of: **anything that repaints on a timer rather than
on progress is pure cost.** Elapsed timers, token counters, cost and context readouts, and
an enumerated list of spinner glyphs (`_VOLATILE_RE`) are already stripped, so those are
safe to leave on. Note *enumerated*: a spinner drawn with a glyph nobody has added to that
list still signs differently every frame — which is the same argument again for
suppressing decoration at the source rather than relying on it being recognized.

## Codex: don't use the alternate screen

Add `alternate_screen = "never"` to the **same** `[tui]` table as above — TOML allows each
table to be declared only once per file, so pasting a second `[tui]` header stops Codex
from loading the config at all.

A full-screen TUI runs on the terminal's alternate screen, which has no scrollback —
`tmux capture-pane` can only return the rows currently visible. Measured on this machine:
asking for 200 lines of an alternate-screen pane returns 41, the visible height, while the
same request on a normal pane returns everything it has. The daemon's bootstrap read wants
800 lines of history to reconstruct the story so far, so on an alternate-screen pane it
gets a fraction of that and the card's summary starts thinner than it should.

Claude Code has the analogous setting as `"tui"` in `~/.claude/settings.json`, which
accepts `"fullscreen"`. I have confirmed the effect from the tmux side — Claude Code panes
here report `alternate_on=1` and truncate to the visible rows exactly as above — but **I
have not confirmed the value that disables it**, so I am deliberately not printing one
here rather than inventing a key. Check `/config` in Claude Code for the current option.

## Terminal width: narrower panes are cheaper

Capture is bounded in *rows*, not characters, so a wider pane sends a proportionally larger
payload to the classifier on every call. Measured across two live panes here: a 178-column
pane captured ~3.7k characters where a 238-column pane captured ~23.9k. Width is not the
only factor in that gap — the wide pane also held denser output — but the direction is
real, and you pay it again every time the pane is classified. Not every tick: the pane is
captured each tick either way, but on the normal path an unchanged screen is not sent to
the classifier, so width taxes activity rather than mere existence. (Sending input forces
a reparse regardless — a cost you asked for.)

The honest tradeoff: this is a reason to prefer a reasonable width, not to cripple your
terminal. A pane too narrow to render your agent's diffs is worse for you than the token
saving is good for your bill. Ordinary widths are fine; a maximized 24-inch pane running
an agent all day is worth a second thought.

## Don't watch panes that repaint forever

A pane running a constantly-repainting non-agent program is the worst case the change
detector has, because every tick is a *genuine* change and there is no decoration to
strip. `top`, `htop`, `watch`, and a live log tail all qualify. One `top` pane cost **$38
in a day** here.

The classifier learns nothing from the fourth re-read of `top` that it didn't know on the
first. If you keep such a pane around, scope what the daemon watches:

    export TMUXRC_TARGET=%3

The daemon reads this once, at startup, so it has to be in the daemon's own environment by
then — and how it gets there depends on how you start it. From a shell: `export` it, or
prefix the command. From the systemd user unit: put it in the repo `.env`, which the
daemon loads for itself at import — the unit sets no `TMUXRC_TARGET`, and a
`systemctl --user` service inherits nothing from your shell. Either way, a
daemon that is already running keeps its old setting until you restart it
(`systemctl --user restart tmux-rc`) — otherwise it goes on watching every pane.

See the `TMUXRC_TARGET` row in the [README](https://github.com/querystory/tmux-rc#config-env)
for the accepted forms — a pane id is the one that is always unambiguous. Note that this
restricts watching to a *single* pane, so it is a blunt instrument: today it is the
right answer when you have one agent you care about, and the wrong one when you have a
fleet plus a stray `top`. Closing the `top` pane is often the better fix.

## What this all adds up to

Let your agent put its title on screen (and name the window yourself when it can't) so the
phone can tell your agents apart; turn off decoration that moves on a timer; keep agents off the alternate screen so their history is readable; and don't
point the watcher at something that repaints forever. The first is about whether tmux-rc
is *usable* from a phone. The rest are about what it costs you per day, and the numbers
above are what that bill looks like when nobody is paying attention.
