# Design: several agent accounts on one machine

Status: **decided, not built.** Chosen from the agent accounts mock (the harness in
`scripts/mock/`, see [Previewing work](../deploy/previews.md#mocks)). This page holds the
final design and why; when it changes, edit it in place so the diff shows what moved.

## The problem

The aim is to run open-source work on a personal Claude plan and company work on the
company plan, both at once, without ever logging out of one to use the other. Today a
second account already works per pane, but tmux-rc can't name it, can't say which account
a new pane gets, and labels its meters by email. With one person's two plans the email's
local part clashes (sam@ and sam@), so every meter shows a full address.

## Accounts are config dirs, not a switch

Claude Code keeps its login, settings and transcripts in `CLAUDE_CONFIG_DIR`, and Codex in
`CODEX_HOME`. Two dirs are two independent logins, so a pane's account is fixed by the
environment it started with. Nothing is swapped back and forth, and each plan only ever
sees its own work. tmux-rc already resolves usage, transcripts and expunge per pane dir;
what it lacks is naming, mapping and showing.

- **A profile** is a provider plus a config dir, with a label and a colour, and nothing
  more: it matches what the agents themselves understand, and no secret enters tmux-rc's
  config. Profiles are discovered from `~/.claude*`, `~/.codex*` and the dirs live panes
  use, and named from the non-secret account fields beside the login (the organisation for
  a team plan, "Personal" for an individual one). The credentials file is never opened to
  name one.
- **Mapping is per tmux session**, through the session's environment. tmux applies it to
  every pane created in that session afterwards, so `prefix c`, tmux-rc's "+" and a resume
  all inherit it. It is what a user would do by hand, and tmux-rc only reads and writes it.
  A launcher may also carry a profile, for a session that mixes both kinds of work.

## What the UI shows (option A, refined)

- **A badge on each pane** names its account, only when that provider has more than one,
  so a single-account user sees nothing new.
- **The New window dialog has an account picker**, preselected from the session's default
  and shown only for a provider with several profiles. Picking the other one can also make
  it the session's default, so the next pane needs no choice.
- **Plan usage is one horizontally swipeable row ordered by urgency**, not meters stacked
  per account. The question the strip answers is "which limit do I hit first", and
  grouping by account buried the one full plan under the healthy ones; the row keeps every
  account's meters one swipe away without growing the page.
- **The account filter lives in the ☰ menu**, and while one is set a "Filtered: Personal ✕"
  pill says so and clears it. Filtering is occasional, so it does not earn header space,
  but a filtered view must never pass for the whole fleet.
- **The consent card names the account** a chat action lands on. Acting never changes it.

## Rules that hold whatever the UI

- Accounts are never switched automatically or "moved to the plan with headroom", and the
  active login is never rewritten. A full plan is shown, not routed around.
- Credentials are never shared between dirs, by symlink, copy or tmux-rc. Login and logout
  stay in the agent's own `/login`, run by the user.
- An existing session stays on the account it started on. Resume runs under the dir that
  owns the transcript rather than the target session's default; moving work across is a
  hand-off summary into a new pane, or an explicit copy with a warning, never automatic.
- Inferring the account from the repo or cwd is rejected: a wrong guess silently bills the
  wrong plan. The mapping is something the user set and can see.

## Alternatives considered

- **B, account chips beside the tool chips** filtering the whole dashboard: rejected, it
  adds header clutter for a filter used now and then (it survives as the ☰ menu filter).
- **C, an Accounts card comparing plans and burn** side by side: not needed yet; the
  urgency-ordered strip already answers which plan runs out first.
- **Today's email labels**: what the mock started from, and the clash it shows is the bug.

## Open questions

- Resume and recent directories index only the daemon's own config dir, so sessions under
  a second dir are not offered yet; every profile dir needs indexing, with its dir returned.
- The daemon always contributes its own default account to the meters, even with no pane
  on it. Probably fine; worth confirming once profiles exist.
