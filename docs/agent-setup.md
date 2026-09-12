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
   information you already had. See [parse cadence](design/parse-cadence.md) for why the
   trigger is change-based and what the fingerprint already strips.
2. **If the pane doesn't say who it is, the phone can't either.** A card's heading is the
   agent's own session title, read off the screen — so an agent that never puts its title
   on screen leaves the phone nothing to show but a coordinate.

## Let the agent name itself

You do not have to name anything. The classifier reads the agent's **own** session title
off the screen and uses it as the card's heading — that is what the `session` field in the
parser prompt is for. Claude Code prints its session name just above its status line;
Codex puts its thread title first on its status bar. Both are picked up automatically, so
a pane reads as *Review 4745* or *airbyte-value-population* rather than as its command.

Two settings make that work better, and both are worth having:

- **Codex: include `"thread-title"` in `status_line`.** Without it the title is not on
  screen at all, so there is nothing to read and the pane falls back to a coordinate.
- **Leave `automatic-rename` alone** where an agent sets the window title itself. Fighting
  the agent for it just throws away the better name.

The fallback only matters when there is no agent title to find. `Pane.label` in
`daemon/tmux.py` then takes the first identity that looks user-chosen: the window name,
then the session name, then the cwd basename, and only if all three are unusable
`session:window-index`. "Unusable" is a deliberately small set — empty, a bare number, or
one of tmux's command auto-names (`bash`, `node`, `python`, `ssh`, …) — because guessing
wrong throws away a name the user meant.

Two things follow that are worth knowing before you rely on it. Agent binaries are *not*
in that set, so eight windows tmux auto-named `claude` are eight rows headed `claude`.
And session names and cwd basenames are shared by every window in a session, so those
collide too — the label is not qualified with the window index. Only the last resort is
unique, and you rarely reach it.

That is the whole argument for naming a window yourself when an agent publishes no title:
a window name is per-window and tmux-side, so it is the one identity that cannot collide.
Useful for a pane that is not an agent at all:

    tmux rename-window 'db migration'

But for an agent pane it is not the fix for anything; it is a manual override of a name
the agent already publishes.

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

**tmux-rc already defends itself against this** — `_fingerprint` in `daemon/watcher.py`
flattens the band of lines the animation plays over, so you are not exposed if you skip
this. Turn it off anyway if you can: suppressing decoration at the source is strictly
better than pattern-matching it downstream, and the same reasoning applies to whatever
animated chrome your agent adds next, which tmux-rc will not know about.

The general rule this is an instance of: **anything that repaints on a timer rather than
on progress is pure cost.** Elapsed timers, token counters, cost readouts and single-cell
spinners are already stripped from the fingerprint, so those are safe to leave on.

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
real, and you pay it again every time the pane is classified. (Not every tick: an
unchanged screen is never re-read, so width taxes activity, not mere existence.)

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

It has to reach the daemon's own environment, so export it (or put it in the `.env` the
daemon loads, or prefix the launch command) — a bare assignment in your shell is invisible
to the process you start next, and the daemon goes on watching everything.

See the `TMUXRC_TARGET` row in the [README](https://github.com/querystory/tmux-rc#config-env)
for the accepted forms — a pane id is the one that is always unambiguous. Note that this
restricts watching to a *single* pane, so it is a blunt instrument: today it is the
right answer when you have one agent you care about, and the wrong one when you have a
fleet plus a stray `top`. Closing the `top` pane is often the better fix.

## What this all adds up to

Let your agent put its title on screen (and name the window yourself when it can't) so the
phone can tell your agents apart; turn off decoration that moves
on a timer; keep agents off the alternate screen so their history is readable; and don't
point the watcher at something that repaints forever. The first is about whether tmux-rc
is *usable* from a phone. The rest are about what it costs you per day, and the numbers
above are what that bill looks like when nobody is paying attention.
