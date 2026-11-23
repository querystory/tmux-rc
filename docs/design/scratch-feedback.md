# Scratch feedback: a quote for an address, and a maker that can be woken

Status: implemented. How to use it: [Scratch](../scratch.md#answering-the-maker).

## Why

A scratch page's footer already messages the pane that made it, with a screenshot. Two
things kept that from being enough.

Feedback pointed at a page, not a sentence. Most review remarks are about one line, and
on a phone the slow part of making one was describing where: "the third bullet under
Risks". The screenshot shows the agent the page; it does not show which line you mean.

Feedback needed a living pane. Pages outlive the panes that write them: a report is read
the next morning, after its window was closed. The footer then offered only "Choose a
pane", which hands the remark to an agent that has never seen the page.

Both ideas come from comparing scratch with Markdown Brain, a hosted shared-Markdown
product whose comments are anchored to text and whose mentions start a session.

## Decisions

**The quote is the address.** Selecting text on the page offers a Quote chip; a tap puts
the selected words, in quotation marks, at the caret of the message. Nothing else is
recorded: no offsets, no anchor ids, no thread. An agent finds a sentence in its own page
by its words more reliably than by a position that moved with its last edit, and the
pane's conversation is already the thread, so a store of comments would be a second copy
of it that has to be kept in step.

**The chip sits in the app, the selection is reported by the page.** A scratch page is
sandboxed in a frame the app cannot read. The daemon already appends a few lines of script
to every HTML page it serves, a rendered Markdown page included, to report scrolling;
the selection travels the same way, so Markdown and hand-written HTML are covered by one
mechanism and no page has to opt in. The report waits for the selection to settle,
because dragging a selection handle changes it many times a second, and because a tap on
the chip can clear the selection in the frame before the tap lands.

**A reported selection is untrusted, and only ever a draft.** The page could post any
text and call it a selection. That gives it nothing it did not have: a page can already
offer a draft, and like a draft, a quote is only placed in the box, in front of the user,
who sends it or not. What the app adds is hygiene before the text nears a terminal: one
line, no control or invisible characters, cut at a few hundred characters.

**Resuming reuses the resume the daemon already trusts.** The conversation is the one
scratch already records for a folder to name its author (`agent_session`, found through
agent-history). It is reopened by the function Live Mode's "resume that session" uses:
everything that reaches tmux comes from the index, a conversation already running is
found and never started twice, and its account is the one its transcript lives under.
The wait for the agent's prompt is the one reboot restore uses. The page is then moved to
the new pane by the same binding the To chip writes. No part of this is new machinery;
the feature is a join of three existing ones.

**Resume, then write.** The tap reopens the conversation and the message box appears when
the agent is ready; the message is sent as any other. Sending in the same tap would mean
holding a draft for a pane that does not exist yet and typing it into an agent still
loading its transcript, where a lost or mangled message is silent.

**Only on the user's tap.** Nothing resumes by itself, and a page cannot ask for it: the
route is the app's, out of a sandboxed page's reach.

## Tradeoffs

- A quote that appears twice on a page is ambiguous. The remark after it and the
  screenshot usually settle it; an exact anchor would cost the storage this design avoids.
- The reboot-restore snapshot is not the record used, though it also binds panes to
  conversations: it holds the panes open now, and the pane in question is the one that
  closed. The folder's own record survives it.
- A page whose conversation was never found (no agent-history, or made by a script) gets
  no Resume, only Choose a pane.
- Without a parse model the daemon cannot see the agent reach its prompt, so the wait
  runs to its limit, a minute, before the box appears.
- Touch selection inside the frame is the browser's own. It was exercised in headless
  Chromium at phone width, not on an iPhone.

## Alternatives rejected

- **Stored comment threads with text anchors**, as the product that inspired this has. It
  suits a document several people edit; here there is one reader and the maker's
  conversation already holds the exchange.
- **Offsets or DOM paths as the address.** They break on the agent's next edit, which is
  the edit the feedback asks for.
- **A selection menu drawn inside the page.** The page is the agent's; chrome injected
  into it would fight its layout and the phone's own selection callout, and a button
  inside the sandbox could be imitated by the page.
- **Starting a fresh agent on the page** when its maker is gone. That is a job runner,
  and the new agent lacks the context that made the page; "Choose a pane" already covers
  handing it to another agent deliberately.
- **A second resume path** typed into a shell as restore does for a rebuilt layout. It
  would duplicate the running check and the account choice for no gain: there is no
  layout to rebuild, only one window to open.

## Follow-ups not built

Per-type notification switches (a question, a turn that ended, a page that is ready), the
other idea worth borrowing from the same comparison, is separate work on the settings
surface. Hosting documents, agent-declared status as the only signal, triggers and
schedules, organisations and roles, and plain-text notifications were considered there
and rejected for tmux-rc.
