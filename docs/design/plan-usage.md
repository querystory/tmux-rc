# Design: plan usage — Claude and Codex limits in /m

Status: **implemented** (`openbus/plan_usage.py`, `web/m/usage.js`).

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
The desktop sidebar has no "…" menu and room to spare, so it keeps every meter.

### The dashboard panel

A phone's bar says where a window stands but not how it got there, and the sidebar's trend
is a thumbnail. So the dashboard leads with a Plan usage panel: every meter, whatever the
Usage setting, as a trend tall enough to read. Tapping the strip, on the phone or in the
sidebar, opens the dashboard at that panel rather than a sheet of its own, so there is one
larger view to maintain and it sits beside the fleet history it is usually read against.
Tapping the panel opens it up: wider trends (one to a row on a phone, two on desktop), the clock times behind each
countdown, and the pace's forecast as a number. Those come from the same response the strip
draws, so the detail costs no request. A longer range was considered and left out: samples
are pruned to eight days (see Polling and storage), enough for the current weekly window and
no more, so a range past the last reset would mean keeping history nothing else reads.

The sidebar strip stays. It is the only place the limits show while a pane is open, and both
draw from one renderer (`renderUsage`), so the panel is a size and a flag, not a copy.

## Accounts, not providers

One person often runs several Claude or Codex logins side by side, each with its own plan.
Claude Code picks its config dir from `CLAUDE_CONFIG_DIR` (else `~/.claude`), Codex from
`CODEX_HOME` (else `~/.codex`). So the daemon finds, for each Claude or Codex pane, the
agent process under the pane and reads just those variables (plus `HOME`) from its
`/proc/<pid>/environ`; every other variable is discarded unread. The agent process, not
the pane's shell, because an inline `CLAUDE_CONFIG_DIR=… claude` sets it only there. The
daemon user's own defaults are always included, so the strip works with no panes open.

Accounts are deduplicated before anything is fetched, so ten panes on one login cost one
request:

- **Claude** is keyed by `oauthAccount.accountUuid` from the config's `.claude.json`
  (inside an overridden config dir, else `~/.claude.json`), and labelled by the email's
  local part. Two config dirs logged into one account collapse to one row.
- **Codex** is keyed by its home directory and labelled by the directory's name. Its
  account id lives only inside `auth.json`'s tokens, and keeping the Codex path entirely
  credential-free was worth more than merging the rare case of two homes on one login.

A meter shows just its provider's icon while there is one account per provider; with
several, each meter label and each pane's heading carries the short account name, so you
can tell which limit a pane is drawing on. The name follows the window, so a narrow cell
truncates the name rather than the window.

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
`utilization` and `resets_at`, treats a null window as 0% (not yet opened), and any
failure shows as "unavailable" rather than stale numbers. Per-model weekly limits come
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
as the fleet chart. Unlike pane history, it is pruned to eight days (the longest window
plus a day): only a current window is ever drawn, and a minute-level poll per account
would otherwise grow without use. The UI fetches `/api/usage` once a minute; it is a separate endpoint,
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

The sparkline is a few lines of inline SVG rather than the ECharts fleet chart: that chart
is built around stacked state bars, and four 24px trends do not justify loading it on
screens that otherwise would not.
