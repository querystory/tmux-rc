# Agent accounts: several Claude or Codex logins on one machine

Status: implemented (`openbus/accounts.py`, `web/m/accounts.js`). Approved from the
playable prototype in the scratch previews (`accounts/`).

## Why

The motivating case is one person with a personal Claude plan for open-source work and a
company plan for company work, running both at once, never logging out and back in.
Claude Code and Codex already support this: each config dir (`CLAUDE_CONFIG_DIR`, default
`~/.claude`; `CODEX_HOME`, default `~/.codex`) is its own login, settings and transcripts.
A pane's account is fixed by the environment its agent started with, so nothing ever
"switches". tmux-rc already read that variable from each agent process, and meters,
transcripts and Expunge followed it. What was missing was naming the accounts, choosing
one for a new pane, and adding a second one without leaving the phone.

## The model: a profile is a provider plus a config dir

Profiles are discovered, not configured: `~/.claude`, every `~/.claude-<name>`,
`~/.codex*`, any dir a live pane uses, and any dir added through the UI. A dir is listed
once someone is signed in there, or once it was added here (then it shows as "not signed
in" until the sign-in finishes).
A pane running on another HOME's default dir is not one of them: that is someone else's
setup, whose name tmux-rc could not read (its `.claude.json` sits beside the dir, under that
HOME) and which no launch could pin with the config variable alone, so it is left out
rather than listed half-working.

A profile is named by the user's label, else by the non-secret `oauthAccount` fields in
the dir's `.claude.json`: the organisation name, with a personal plan's
"<email>'s Organization" shortened to the email, then the display name, then the email. Two
unlabelled accounts that would read the same (two people of one organisation) add the
display name or the email's local part ("QSI · sam"), and the full email only if that
clashes too.
The credentials file is never opened by this code: Codex keeps no non-secret identity
file, so only the presence of its login file is checked. The old meter labels (the email's
local part, or the full email on a clash) are gone: for one person with two plans they put
the same full email on every meter.

The store (`$XDG_STATE_HOME/tmux-rc/accounts.json`) holds only what discovery cannot
know: labels, a colour index per dir, and forgotten dirs. Colours are handed out on first
sight (the lowest unused) and kept, so adding a third account never recolours the first
two.

## The single-account rule

Account UI appears only once a provider has two or more signed-in accounts, and the test
is per provider. With one Claude and one Codex account nothing changes from before: no
badges, no picker, no filter, no grouping in Plan usage, no account on the consent card.
With two Claude accounts and one Codex, Claude panes get badges and Codex panes do not,
because the Codex icon already says which account it is. Most people have one account
each, and for them account chrome would only be noise and something to misread.

The one addition everyone sees is **Accounts…** in the ☰ menu, because adding a second
account has to start somewhere. On a wide screen that menu now sits in the sidebar head;
the wide layout had no menu before.

## Showing it

- **Badges** ("● Work") on the phone's rows, the sidebar's rows and the pane header, in
  the account's colour. The pane header puts it first on a phone, which ellipsizes the
  end. Every place that names an account draws this one chip, so the same account never
  looks two ways on one screen; a long name truncates inside it.
- **Session cards** on the dashboard carry the chip of the session's own default (the
  dir its tmux environment sets), and nothing when it sets none: a session that starts
  new windows on another account is worth seeing at a glance, the standard dir is not.
- **Usage strip:** with several accounts the meters become one row that swipes sideways
  and snaps per meter, most urgent first (full soonest, then the highest pace at the
  reset). Two meters fit on a phone with a third of the next peeking in under a fade, so
  the row reads as one that scrolls; a two-meter split looked complete and hid the swipe.
  Seven meters in a two-column grid would push the list off a phone's screen, and what
  needs attention must never sit behind a swipe. Each meter names its account with the
  same chip as the rows, in its bottom corner across from the reset, and its title keeps
  just the window: a name in the title was truncated first, read as part of the window
  ("7d QSI"), and left a one-account provider's meter looking unnamed beside it.
- **Dashboard Plan usage:** wide, one row of 180px meters grouped per account, each group
  labelled with its chip and the details (who, plan, dir, panes) in its tooltip; it wraps
  only when the groups do not fit. The label names the account once over its meters, so
  they carry no chip: repeating it in every 180px meter is noise. A phone's dashboard
  uses the strip, chips included.
- **Filter** in ☰ (All, then each account), because it is a mode you set and forget, not a
  control every screen needs. While on, a "Filtered: Work ✕" pill shows under the header
  (in the sidebar on wide) and ☰ carries a dot. It applies to the list, the strip and the
  dashboard, Open loops included: a loop shows only if a pane on the account holds it, so a
  PR or worktree with no pane, which no account owns, is hidden while filtered.
- **Consent card:** a chat action on a pane names the account the pane runs on, and says
  that sending never changes it.

## Choosing the account for a new pane

The mechanism is a default account per tmux session:
`tmux set-environment -t <session> CLAUDE_CONFIG_DIR <dir>`. tmux hands it to every window
created there afterwards, a typed `prefix c` and a resume included. It is what a user
would do by hand, and it stays visible in tmux. New window preselects the session's
default and always shows a "Default for this session" control. When nothing sets the
variable (unset, or removed with `-VAR`), it says so ("no account default; new windows use
Work, the standard dir") rather than implying a choice nobody made.

Launching on an account wraps the launcher in `env CLAUDE_CONFIG_DIR=<dir>` (or
`env -u CLAUDE_CONFIG_DIR` for the default dir), so the pick holds whatever the session's
default is. Unset rather than set for `~/.claude`: Claude Code reads `~/.claude.json`
beside the default dir, but `<dir>/.claude.json` inside an explicit one, so setting the
variable to `~/.claude` would look signed out. A resumed Claude session is pinned the same
way to the dir its transcript is in, since it resumes only under the account that owns
it; agent-history now indexes every `~/.claude*` dir so those sessions are offered at all.

## Adding an account

![New window with two Claude accounts: Personal and Work as cards, "+ Add account", and the session's default shown and settable](../img/accounts-add-1-new-window.png)

From ☰ → Accounts… → Add account, or "+ Add account" in New window's picker:

1. **Name, provider and dir.** The dir is suggested as `~/.<provider>-<name>` and stays
   visible and editable, because the dir is the account. One already listed stops the
   step. An existing dir is reused as it is; a new one is created with mode 700.
2. **Share your setup (optional).** Symlink individual files from the provider's default
   dir (`CLAUDE.md`, `skills/`, `hooks/`, `statusline.sh`) so there is one copy to edit.
   `settings.json` is off by default because it often carries work-only hooks, MCP servers
   or permissions. The login, `.claude.json` and `projects/` are listed as never linked.
3. **Sign in.** tmux-rc opens a normal new window running `CLAUDE_CONFIG_DIR=<dir> claude`
   (or `CODEX_HOME=<dir> codex login`), and the user runs the provider's own `/login`
   there. tmux-rc never shows a password field, never handles a token and never drives the
   login; it only polls the dir's non-secret account info until it appears. That keeps the
   credential boundary exactly where the provider put it.
4. **Done:** the identity read from that info, and a pre-ticked "make it the default for
   session X".

![The last step: "Demo is ready", signed in as Demo Studio on ~/.claude-demo, with the session default ticked](../img/accounts-add-6-done.png)

The whole sequence, each step cropped to its sheet, is the `accounts-add-*` set in
`scripts/screenshots.mjs`, shot against the demo fleet with a stub sign-in screen.

Edge cases: an abandoned sign-in (Cancel, or its window closed) adds nothing beyond the
listed "not signed in" dir, with Finish sign-in and Remove. Rename changes only the label.
Remove takes the account off tmux-rc's list; the dir, its login and its transcripts stay
on disk and running panes keep running. The default `~/.claude` and `~/.codex` cannot be
removed.

## Safety

- No credentials file is ever shared, copied or linked, and discovering, naming or adding
  an account never reads one. The Plan usage meter is the exception it always was: it
  reads each metered account's OAuth token in-process to fetch its limits
  ([plan usage](plan-usage.md)), now for every listed account rather than only those a
  pane uses.
- Nothing switches accounts automatically, and nothing routes work to "the plan with
  headroom". A full plan is shown, never worked around.
- Login and logout stay in the provider's own UI.

## Alternatives considered

- Guessing the account from the repo or cwd: rejected, a wrong guess silently bills the wrong plan.
- Linking whole dirs between accounts: rejected, it silently shares the login.
- A login form or token paste in tmux-rc: rejected, it moves the credential boundary into tmux-rc.
- Per-account rows in the strip: rejected, several accounts push the list off a phone.
- Copying a transcript to move a session between accounts: deferred, it forks the session and moves work content across accounts.
