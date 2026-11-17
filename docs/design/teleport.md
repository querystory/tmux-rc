# Teleport: continue a claude.ai/code session in a local pane

Status: implemented (`openbus/teleport.py`, `POST /api/teleport`, the New session sheet).

## Why

A Claude Code session started on claude.ai/code (from a phone, say) runs in a cloud
sandbox. It cannot reach what only this machine has: local credentials, the other
checkouts, the rest of the fleet. Claude Code can bring such a session down
(`claude --teleport <id>`), but by hand that takes several steps that are easy to get
wrong, each found by doing it:

- Teleport refuses to run anywhere but a checkout of the session's first repository.
- It checks the session's branch out in the directory it runs in, so running it in a
  checkout someone is working in moves that checkout to another branch.
- It only tries `git checkout`: a branch the cloud session pushed after the last local
  fetch is not found, and the session resumes "without branch".
- The session's title stays behind, so the pane arrives unnamed.

## What it does

The New session sheet has one more row: paste a session's link or id, press Teleport.
With several Claude accounts the account step comes first, as for a launcher, because a
cloud session belongs to one account. The daemon then:

1. Reads the session id out of what was pasted.
2. Asks the sessions API for the session, with the chosen account's own token: the same
   in-process read the usage meter does (`plan_usage.token`), never refreshed or written.
   The reply names the title, the repositories and the branch.
3. Finds the main checkout of the first repository among the directories panes and past
   agent sessions ran in, by its `origin`.
4. Opens a window there running, in order, `git fetch origin <branch>` and
   `claude --teleport <id> --name <title> --worktree <name>`.

The fetch runs in the pane, not in the daemon: it needs the user's git credentials, and a
failure there is visible and harmless. `--worktree` gives the session a fresh worktree,
so no existing checkout changes branch. As with launchers, the request never carries a
command: it names an id, and the argv is built around the fixed `claude --teleport`.

Each refusal says what to do: not a session link, no such session on this account, the
account's sign-in unusable, or no checkout of the repository on this machine.

## What it leaves alone

- **The cloud session keeps running.** Teleport copies the conversation; the original
  stays on claude.ai with whatever it was subscribed to (a pull request's events, a
  scheduled check-in). Nothing here stops it. Archive it on claude.ai once the local one
  has taken over, or both may act on the same event.
- **Only the first repository comes down.** A session working in several is checked out
  in the first; the others are named in a notice. Its transcript still refers to the
  sandbox's paths, which the agent has to find again locally.
- **Paste only.** The API can also list an account's sessions, which would make this a
  picker. Left out until the paste flow has been used: a list of every cloud session's
  title is more of the account's data than this needs to read.
- **An existing tmux session only.** The row hides for "New session…": a teleport picks
  its own directory, which is the one thing that mode asks for.
