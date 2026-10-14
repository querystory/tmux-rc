# Design: plan usage — Claude and Codex limits in /m

Status: **implemented** (`openbus/plan_usage.py`, `web/m/usage.js`, `web/m/usage-view.js`).

## Why

A fleet of agents burns subscription limits much faster than one person typing. The
moment that matters is not "you are at 100%", it is "at this pace you hit 100% an hour
before the window resets": that is when you pause a sweep, move work to another account,
or let a pane idle. So the UI shows each window's percentage all the time (a pinned strip
on the phone's list, the sidebar foot on desktop; the collapsed rail leaves it out, as it
leaves out every card's text), and on desktop a trend with a dashed projection to
the reset (colours below).

The first version showed a bare "68%" and "59m", and that read either way: remaining or used, time left or time gone. So each meter now says "used", the time says "resets", and the bar carries direction itself. The fill is what is used. A faint stretch past it is where the fitted pace ends by the reset. A thin tick marks the even pace (the share of the window already gone), so a fill past the tick is ahead of it. Colour follows the projection, not the current value: amber when it ends at 90% or more, red when it runs out before the reset. That case adds a "full in" line, in the meter's colour, above the reset countdown rather than in place of it: the reset is still when the room comes back, and a forecast without it left no way to plan around the stall. All meters share one flat grid, two to a row, instead of a block per account under a header. The windows differ by plan (Claude has 5h, 7d and a per-model weekly limit, "7d Fable"; a weekly-only Codex plan has one), so per-account rows with fixed 5h and 7d columns left holes and spent a whole row on each header. Packed, Claude's three and Codex's one make a 2×2. Each label carries its provider's icon instead of a header, and the order (provider, then 5h, 7d, per-model) keeps an account's meters together. In the sidebar's narrowest cells the word "used" drops out so the window name and percentage still fit; the trend line already shows direction there.

### When the phone shows it

Even packed, the strip takes two rows above the list, and most of the time nothing in it
asks for anything. So the phone's "…" menu has a Usage item that cycles Auto, On and Off,
remembered per browser like the theme. On is the full strip and Off hides it. Auto, the
default, shows only the meters that call for a decision and hides the strip when none do.
A meter qualifies when the projection has it full before the reset (the same `limit_at`
that draws "full in", so phone and server cannot disagree), or when it is at 70% used or
more. The threshold is needed because the fit lags a sudden burst (see Projection): a
sweep started late in a window can burn the last 30% before the line catches up, and 70%
is where that remainder gets small enough to matter. Lower and a steady week's ordinary
pace would keep the strip up most of the time, which is what Auto exists to avoid. An
account that can't be read drops out under Auto too, because there is nothing to act on.
The desktop sidebar has room to spare, so it keeps every meter, and the same setting is
one switch in its ☰ menu, "Plan usage", that is On (Auto and On, since every meter shows) or
Off. It is the one setting rather than a second, desktop-only one, so a browser that crosses
the breakpoint never shows a strip its other layout was told to hide; someone paying per
token, with no plan limits at all, turns it off once.

The strip's top edge is a seam, as the fleet chart's is: dragged taller, each trend takes
the height and, a row per hundred pixels, several accounts' strip stacks more rows of
meters while still swiping sideways, so a tall strip shows more meters rather than just
bigger ones. The height is per browser, like the sidebar's width, and a double-click
restores the natural one. Dragged under one row it closes, which sets the switch Off with
an Undo in the notice, rather than snapping back with a hint. Snapping back fights a
gesture that plainly meant "away", and a zero height would hide the strip somewhere the
menu does not show, so the switch is the only place hidden can live.

The meters wait for the accounts before they first draw. Drawn before them, several
accounts' meters had no strip to fold into yet and stood for a moment as a tall grid
reaching up the sidebar, then collapsed into their row.

### The dashboard panel

A phone's bar says where a window stands but not how it got there, and the sidebar's trend
is a thumbnail. So the dashboard has a Plan usage panel, right under the activity chart:
every meter, whatever the Usage setting, as a trend tall enough to read. It sits under the
chart, not above it, because the activity chart is what the dashboard is for; above it, the
panel pushed the page's main subject below the fold on a phone. Tapping the strip, on the phone or in the
sidebar, opens the dashboard at that panel rather than a sheet of its own, so there is one
larger view to maintain and it sits beside the fleet history it is usually read against.
Tapping the panel opens the usage view, below.

### The usage view: a sawtooth over time

Every meter climbs toward 100% and drops to zero at its reset, so over weeks it is a
sawtooth, and the questions the view answers are about that shape: am I using more than
last week, how often do I hit the 5h wall, when does this window run out. So the view
(#view=usage) draws one chart per window type (5-hour, Weekly, Weekly <model>), stacked
to fill the page, with each account a series in its chip's colour. Codex's week sits in
Weekly beside Claude's, since it is the same question. Past windows are the earlier teeth,
muted; the current one is bold and runs dashed on to its reset at the fitted pace. A red
triangle marks each time a window hit the limit, and a hollow one where the current window
will at this pace. Each chart's heading keeps the numbers the strip shows: the chip, the
current %, any "full at", and the reset's countdown and clock time.

**One range control, not arrows per chart.** The first version stepped each meter back
one window at a time with arrows in its corner. A week's trend at a time cannot show a
month's pattern, a dozen arrows were a dozen controls for one question ("how far back am
I looking?"), and nothing said what time a chart showed. So there is one segmented control
at the card's top right (1d, 7d, 30d, All; the Activity chart's control and look) and,
beside it, the range in words: "May 17 – Jun 21 · last 30 days". The charts share one time
axis and one zoom, so a wheel zoom or a drag on any of them moves all three and the words
follow ("8 days shown"). Each range also runs on past now by a fifth of itself (at least
six hours), so the forecasts show without a short range spending its width on the future.

**A chart per window type, not per account.** A per-account chart overlays its 5h teeth
on the weekly line they feed, which reads well for one account and badly for three; and
the comparison people make is across accounts on the same limit ("which plan has room
for this sweep?"). Grouping by window keeps every y axis meaning the same thing.

**The tooltip reads the samples, not echarts' nearest point.** Every series has its own
sample times, so echarts' per-series nearest point would show different instants side by
side. The tooltip interpolates each meter at the pointer's time instead.

**A phone shows one chart at a time.** Three stacked charts at 390px are three slivers.
The range sits on top, the window tabs under it (a sideways swipe on the heading moves
between them too), and until a range is picked it follows the chart: a day of 5h teeth,
a month of weekly ones.

**Data.** `/api/usage?past=<days>` adds, per window, the windows that ended in those days,
each with its samples thinned to the same 120 points as a current window. The view asks
for 30 days (All asks for ten years), so 1d, 7d and 30d switch without a fetch; a month
of three accounts' meters is about 150 KB. Only the view asks: the strip polls the same
endpoint every minute without it. The read is bounded by time, which is what the range
control speaks in, rather than by a count of windows. Windows are still told apart by
their reset time rather than stored as records: a reset that moves by an hour or less
between reads is the same window, since no two windows reset closer than five hours apart.

The charts are the app's vendored ECharts, styled as the Activity chart is, and load only
with the view. Hand-drawn SVG was tried first, as small sparklines, and had no axes, no
zoom and no exact values; those are what a chart library is for.

The sidebar strip stays. It is the only place the limits show while a pane is open, and both
draw from one renderer (`renderUsage`), so the panel is a size and a flag, not a copy.

## Accounts, not providers

One person often runs several Claude or Codex logins side by side, each with its own plan.
Claude Code picks its config dir from `CLAUDE_CONFIG_DIR` (else `~/.claude`), Codex from
`CODEX_HOME` (else `~/.codex`). So the daemon finds, for each Claude or Codex pane, the
agent process under the pane and reads just those variables (plus `HOME`) from its
`/proc/<pid>/environ`; every other variable is discarded unread. The agent process, not
the pane's shell, because an inline `CLAUDE_CONFIG_DIR=… claude` sets it only there. The
signed-in accounts tmux-rc lists ([agent accounts](agent-accounts.md)) are always included,
so the strip works with no panes open; the daemon's own environment no longer adds one.

Accounts are deduplicated before anything is fetched, so ten panes on one login cost one
request:

- **Claude** is keyed by `oauthAccount.accountUuid` from the config's `.claude.json`
  (inside an overridden config dir, else `~/.claude.json`). Two config dirs logged into
  one account collapse to one row.
- **Codex** is keyed by its home directory. Its
  account id lives only inside `auth.json`'s tokens, and keeping the Codex path entirely
  credential-free was worth more than merging the rare case of two homes on one login.

Each row reports the config dirs it was read from, and the UI names it after the account
those dirs belong to. A meter shows just its provider's icon while there is one account
per provider; with several, each meter carries the account's chip in its corner, and the
strip becomes one swipeable row ordered by urgency (see [agent accounts](agent-accounts.md)).

## Sources, and why each

**Codex: its own session logs.** Every `token_count` event in
`<home>/sessions/YYYY/MM/DD/*.jsonl` carries `rate_limits` with `used_percent`,
`window_minutes` and `resets_at`. No credential, no network, and the numbers are exactly
what Codex itself was told. Fragile, undocumented points: the slot names lie (a
weekly-only plan reports its week as `primary`, with no `secondary`), so windows are named
by `window_minutes`; older builds sent `resets_in_seconds` instead of `resets_at`; and a
`limit_id` other than `codex` is a different meter and is skipped. Logs reach hundreds of
MB, so only the last MiB of a log is read. Concurrent sessions each write their own log,
so the most recently touched file need not hold the newest numbers: logs are scanned
newest-touched first and the newest event wins, stopping at the first log last written
before that event (it cannot hold a newer one). A sample is
stamped with its event's time, so re-reading the same event adds nothing.

**Claude: the OAuth usage endpoint.** Claude Code does hand `rate_limits.five_hour` and
`seven_day` to statusline commands, but only on stdin to a script the user configures,
per running session; using it would mean asking every user to edit their statusline to tee
a file. Nothing else on disk holds the numbers. So the daemon calls
`GET https://api.anthropic.com/api/oauth/usage` (`anthropic-beta: oauth-2025-04-20`) with
the account's access token from `.credentials.json`. This is undocumented and may change
shape or disappear; the parser reads only `five_hour` / `seven_day` →
`utilization` and `resets_at`, and treats a null window as 0% (not yet opened). A failed
read keeps showing the last reading, marked "as of 6m ago", while it is at most three polls
(15 minutes) old: the endpoint drops the odd request, and a meter that blanks for five
minutes over one dropped request is worse than one a few points behind. Past that the
account reads "unavailable", since older numbers would mislead. A network error (no
HTTP status) is retried once after 2s; an HTTP status is not, as a 401 or 429 will not
change in that time. Per-model weekly limits come
from the `limits` list: each `weekly_scoped` entry naming a model becomes "7d <model>",
with its `percent` and `resets_at`. The response also has `seven_day_opus`-style keys,
but they are null on current plans while `limits` carries the number Claude Code's own
status line shows, so `limits` is the one read; any model it names appears, none is
hardcoded.

The token is read in-process per request and never logged, returned, or put in argv;
failures log only the exception type, since an HTTP error's text could echo request
details. An expired token is not refreshed — Claude Code owns that file, and a second
writer racing it could log the user out — it just reads "unavailable" until Claude Code
refreshes it. macOS keeps these credentials in the Keychain, not a file, so there Claude
reads unavailable; the daemon is deployed on Linux.

## Polling and storage

A background loop runs every minute: discovery and Codex logs are local and cheap. The
Claude endpoint is called at most every five minutes per account, which is gentle for an
undocumented endpoint and fine for windows measured in hours. Samples go into the existing
history database (a `plan_usage` table keyed by provider, account, window and time) rather
than a new store, so the trend survives restarts under the same permissions and migrations
as the fleet chart. Raw samples are kept for eight days (the longest window plus a day), so
a current window's trend and projection see every poll. Past that they are thinned to the
last sample of each hour, per window, rather than deleted: an hour is fine enough to see
how a past window climbed and when it hit the limit, and keeps a year to about eleven
thousand rows per meter (8,500 hourly ones, plus eight days of five-minute ones). Old
samples of a window not yet opened are dropped: they say only "0%", and no past window can
be drawn from them. The hour is split by window as well, so the final reading of a
window that reset mid-hour survives, and its first sample at the limit is kept rather than
its last, so "hit the limit" keeps the time it was hit. Thinning scans every old row, so it runs once an hour,
not on every poll. The long history is also the base for correlating burn with running
agents (#377). The UI fetches `/api/usage` once a minute; it is a separate endpoint,
not part of the `/api/state` long-poll, because that one returns on every pane change.

## Projection

A least-squares line over the current window's samples, plus a 0% anchor at the window's
start (`resets_at − length`; both windows restart from zero), extended from now to the
reset. The latest value is held until now: Codex writes an event on every turn, so silence
means no use, and without that point a quiet afternoon would leave a "full in" forecast
sitting in the past. The slope is clamped at zero since usage never falls inside a window.
The anchor keeps a single sample meaningful (it becomes the window's average pace) and
stops one noisy reading from swinging the line. A fit over the whole window lags a sudden
burst, which we accept: it is meant to answer "at this rate", not to forecast. The server
computes it, so it is tested in Python and phone and desktop agree. Only ~120 thinned
samples per window are sent, the sparkline's width; the fit uses all of them.

The sidebar's and dashboard's sparklines are a few lines of inline SVG rather than ECharts:
four 24px trends do not justify loading a chart library on screens that otherwise would
not. The usage view, which is about the charts, loads it.
