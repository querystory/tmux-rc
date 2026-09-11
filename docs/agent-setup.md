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
2. **If the pane doesn't say who it is, the phone can't either.** A card's heading comes
   from the pane's own identity, not from the agent. A fleet of agents that all
   self-identify as "claude" is a column of identical rows.

## Name your windows

This is the single highest-value thing to do, and it costs nothing per tick.

tmux names a window after the command that launched it, so eight agents become eight rows
headed `claude`. tmux-rc treats those auto-names as the non-information they are — the
generic set in `openbus/tmux.py` includes the shells, `node`, `python`, and the agent CLIs
themselves (`claude`, `codex`, `gemini`, `aider`) — and falls back to
`<session-or-cwd>:<window-index>`, which at least points at a real window. A name you
chose beats both:

    tmux rename-window 'Resolve PR 38'

A window name wins outright because it is per-window. Session names and cwd basenames are
shared by every window in the session, which is why tmux-rc qualifies those with the
window index instead of using them bare.

Codex can keep that heading current by itself: its `status_line` accepts a
`"thread-title"` entry, so the pane text carries the thread's own subject and the
classifier has something specific to summarize even when the window name is stale.

If your agent renames the window as it works, let it — `automatic-rename` fighting the
agent for the title just restores the wall of `claude`.

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
on progress is pure cost.** Elapsed timers, token counters, cost readouts and single-cell
spinners are already stripped from the fingerprint, so those are safe to leave on.

## Codex: don't use the alternate screen

    [tui]
    alternate_screen = "never"

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
real and it multiplies by every tick.

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

    TMUXRC_TARGET=%3

See the `TMUXRC_TARGET` row in the [README](https://github.com/querystory/tmux-rc#configuration)
for the accepted forms — a pane id is the one that is always unambiguous. Note that this
restricts watching to a *single* pane, so it is a blunt instrument: today it is the
right answer when you have one agent you care about, and the wrong one when you have a
fleet plus a stray `top`. Closing the `top` pane is often the better fix.

## What this all adds up to

Name your windows so the phone can tell your agents apart; turn off decoration that moves
on a timer; keep agents off the alternate screen so their history is readable; and don't
point the watcher at something that repaints forever. The first is about whether tmux-rc
is *usable* from a phone. The rest are about what it costs you per day, and the numbers
above are what that bill looks like when nobody is paying attention.
