# Mention Trigger

Status: implemented (`openbus/mentions.py`, opt-in via `TMUXRC_MENTIONS_DIR`).

## Why

Some people keep a folder of plain Markdown notes that they and their agents all read and
write. Asking an agent something about those notes meant opening a terminal, starting the
agent in the folder and typing the question, which is the one step a phone makes awkward.
The notes are already where the question is being written down.

So a line that addresses an agent is the request. In a Markdown file of the watched
folder, a line that begins `@claude`, followed by what is wanted, opens a tmux window with
Claude Code started in that folder on that request, told to answer in the file beneath the
line and commit. From that moment it is a pane like any other: tmux-rc reads it, notifies
when it needs the user, and lets them answer. There is no new screen, no new notification
and no new place to look, which is the reason this fits a pane watcher at all.

[Markdown Brain](https://markdownbrain.ai) is the public product that prompted the idea;
it wakes agents on mentions, file changes and schedules.

## Why this narrow form, when triggers were rejected

An earlier look at that product listed "triggers and schedules" among the ideas to leave
out of tmux-rc, and for the general form that still holds. A file-change trigger or a
schedule starts work nobody asked for at that moment: it needs a configuration language to
say which change wakes whom with what prompt, a way to see and edit those rules, and its
own defence against agents waking each other in circles. That is a job scheduler, and
tmux-rc is a view of agents a person is running.

A mention is different in the two ways that matter. A person typed it, addressed to a named
agent, so it is the same act as typing into a new session, moved to where the thought
already was. And it carries its own configuration: the agent's name picks the harness and
the rest of the line is the prompt, so there are no rules to store or show. What is added
is a way to start a session, which the "+" menu already is.

## Decisions

**Off unless a folder is named.** `TMUXRC_MENTIONS_DIR` unset means no task is started and
no file is ever looked at. Someone running one session never meets the feature. It is also
off in single-pane mode, which could not show the window it opens.

**The syntax is the line's first characters.** `@agent request`, with the `@` in the first
column. That is what a person types on a phone, and it makes the cheap exclusions fall out
of the rule with no Markdown parser: a quoted line begins with `>`, an indented one with a
space, a mention in running prose is not at the start, and a fenced block is skipped by
tracking its fence lines. A capitalised name and a comma or colon after it are accepted,
since a phone keyboard supplies both unasked. This is string handling on the user's own
file, so it lives outside the pane parser and uses no regular expression.

**The agents are the ones that take an opening prompt as an argument.** Claude Code, Codex,
Gemini and omp do, and stay interactive afterwards. The command comes from the launcher
configured for that agent (the first entry with its icon, else the agent's bare name), so
a fleet's own flags and wrappers apply, and the window is opened by the same code that resumes a past conversation.
A harness whose command line has no such argument would need its prompt typed in after it
loads, with the wait and the trust dialogs that brings; it is left out until someone needs
it.

**The request is passed as one argument, inside a sentence.** tmux runs the window's command
as a list, with no shell reading the request, and the launcher's command line receives it
as a single positional parameter. Because it arrives embedded in the prompt's wording, it
cannot be read as an option either, however it begins.

**Handled means counted, in tmux-rc's state.** The store holds, per watched folder, how
many copies of each mention have been started, and which files held them. A copy beyond
the count is new, and it is the one in a file that gained a copy, so editing the file of
an answered mention never takes a request away from a new one. The count only rises and is
saved before the window opens. That gives at-most-once through
everything that was worth worrying about: a restart rereads the same counts, an edit
elsewhere in the file leaves the line as it was, the agent's answer adds no mention, and a
file that is renamed, or emptied and rewritten by a sync or a checkout, holds the same
mentions it did. Asking the same thing again on a new line is one more copy, so it starts.
The first time a folder is read, everything in it is counted and nothing starts, so
turning the feature on over old notes, or losing the state file, is quiet.

Alternatives weighed:

- *Marking the line in the file* (a tick, a note beneath) keeps the record with the notes,
  but has the daemon writing into files the user and their agents are editing, leaves
  uncommitted changes in their repository, and races every editor and sync tool.
- *"Answered" judged from the text* (something follows the line) needs no state, but an
  agent that is still working has written nothing, so a restart in that window would start
  it again. Restarts repeating old things is the failure this must not have.
- *Git state* (a mention not yet committed is new) is also stateless, and wrong in both
  directions: anything that commits before the daemon looks hides a request, and every
  uncommitted draft by an agent is a candidate.
- *Keying by file path alone* re-fires every mention in a file when it is moved, and moving notes
  out of an inbox is exactly what such folders are for.

The cost of counting is stated under limits: delete a mention and type the identical line
again, and it is not new.

**A file rests for thirty seconds first.** Editors and sync tools save mid-sentence. Without
the wait, half a request starts an agent and the finished one starts another.

**At most five such windows are open at once.** Nothing reliable says whether a person or
an agent wrote a line: both edit the same files as the same user. So the bound does not
depend on knowing. The windows a mention opens are named `@agent file`; while five are
open, further mentions wait, and start when one closes. An agent that addresses an agent
that addresses an agent can therefore cost five sessions and then stops until the user
closes one, where a rate limit would only slow the loop down. The prompt also asks the
agent to name others without the `@`, which stops the accident; the cap is what holds if
it is ignored. tmux holds the count, so it survives a restart with nothing stored.

**Polling, on its own task.** Every five seconds the folder's Markdown files are listed and
their change times compared; only a changed file is read. For a notes folder that is a few
thousand `stat` calls, and it needs no dependency, no inotify limits to tune, and behaves
the same on every platform and filesystem. It runs beside the worktree and plan-usage
loops rather than in the watcher's tick, which is paced by model calls. A mention answered
five seconds sooner would change nothing: the agent takes minutes.

**Only the folder.** The setting is resolved once; the walk enters no linked folder and
skips linked files, dot folders and anything not ending `.md`. A folder that does not exist
yet is waited for, and counted quietly when it appears.

## Left out

- **A new file in an inbox as a trigger.** It does not fall out of the mechanism: a file
  names no agent and carries no request, so it would need the stored rules this design
  avoids. Put a mention in the file.
- **File-change triggers and schedules**, for the reasons above.
- **A hosted brain, roles and a permission model.** The folder is a local git repository
  and the agents run as the user.
- **A separate runner process.** The daemon already starts agents, already survives
  reboots, and already shows the result; a second supervisor would duplicate all three.

## Limits

- The count is this machine's. Notes pulled in from another machine, with mentions that
  were answered there, look new here; the five-window cap bounds what that can start.
- A mention deleted and typed again, letter for letter, is not new. Change a word.
- A start that fails (the agent is not installed, so its window closes at once) is not
  retried, the price of never starting twice. Write the line again with a word changed.
- The agent may stop to ask permission before it writes. That is by design: it appears
  under "Needs you" like any other pane.
