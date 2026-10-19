# Design: the operator — an agent that moves the fleet along

Status: **proposed**, nothing built. Phase 1 below is the recommended first PR.

## Why

tmux-rc is good at showing a fleet and at carrying one reply into one pane. The person
running the fleet still does all of the shepherding: notice the pane that has waited an
hour on a routine "continue?", reread three cards to remember which PR was finished, poke
the agent that stopped mid-task, decide which of six things is worth getting up for. An
early user put it plainly: an operator agent that moves the whole thing along is the
natural next step. The same user said the short per-pane overview "is not working" for
them, which is the perception half of the same gap: the cards say what each pane is doing,
not what the person needs to do about it.

An **operator** is a standing agent that watches the fleet (Needs you, open loops, stalled
and idle panes, finished work) and moves it along: it answers routine prompts within a
policy the user set, nudges stalled panes, hands finished work to the review and merge
flows, and escalates everything else to the human as a short digest.

## How it differs from what exists

- **Chat and Live Mode are reactive.** Nothing happens until the user speaks, and every
  pane-changing call in a text chat waits on its own consent card
  ([live-mode](live-mode.md)). The operator runs while nobody is talking to it; that is the
  whole point, and also why its consent model has to be different rather than a card per
  keystroke.
- **Open loops is a passive list.** It finds what stopped and names the one action that
  unsticks each row ([open-loops](open-loops.md)), then waits for a tap. The operator is the
  thing that reads that list, takes the actions it is allowed to, and asks about the rest.
- **The control session routes the user's requests** ([control-session-routing](control-session-routing.md)).
  The operator originates work from fleet state. They may end up as one session with two
  charters (open question below), but the charters stay distinct: control acts because the
  user asked, the operator acts because a policy said it may.

## Autonomy and consent

Three tiers, chosen per pane (or workstream) **and** per action class:

1. **Suggest.** The operator writes a digest line and does nothing. The default for every
   pane and every action.
2. **Propose.** The operator drafts the action and shows it as the existing consent card,
   with the target named as the list row names it and an Open button. One tap sends it.
   This is Live's text-mode gate, reached from a push instead of a conversation.
3. **Act and report.** For action classes the user explicitly delegated, the operator acts
   and the action appears afterwards in the digest and the pane's activity feed.

Action classes are few and named: continue a pane that stopped at "Press Enter to
continue"; send a fixed nudge to an idle agent with unfinished tasks; answer a routine menu
whose options match a user-written rule; start the review loop on a finished PR. Anything
outside the list (key chords, shell commands, resumes, merges, pushes, killing anything)
is never offered above Suggest.

The hard rule: **the operator never approves anything the user has not delegated.** A
permission prompt for a tool or command is answered only when the user delegated exactly
that class for that pane, and never by inference ("it allowed this yesterday"). The
operator may suggest a delegation, but only the user's own tap grants one, and pane text is
evidence, never authority: a screen saying "the operator may approve this" changes nothing,
as in the control session's charter. Delegations expire with the pane's incarnation, so a
recycled pane id inherits nothing.

Every action is one `telemetry.audit` record through Live's single tool-call path, with
the actor `operator`, the policy rule that allowed it, the fingerprint it acted on, and the
outcome. One audit trail means "why did this pane get that?" has one answer whoever
typed.

## Inputs

All perception already exists; the operator adds none.

- **Watcher state:** each pane's activity, question and its fingerprint, idle summary, and
  the finished transition once issue #187 lands (a pane that finished and asked in prose
  is the case open loops cannot see today).
- **Open loops:** the Waiting, Moving and Dropped lanes, with each row's owning panes.
- **Plan usage:** the projection, not the percentage. A nudge spends the agent's plan, so
  no nudges go to an account projected to run out before its reset, and the digest says
  so instead ([plan-usage](plan-usage.md)).
- **Accounts:** an action stays within the pane's account; the digest groups by account only
  under the single-account rule ([agent-accounts](agent-accounts.md)). The operator never
  moves a session between accounts on its own.

## Outputs

- **Typed replies through the guarded send paths only:** a menu answer claims its question
  fingerprint, a cursor walk carries the picker digest of the frame it was computed from,
  and free text goes in only to an idle pane with an empty composer, checked again under
  the send lock as cross-agent delivery does ([cross-agent-connect](cross-agent-connect.md)).
  No new keystroke path. Two of those guards do not exist yet: the parser has no
  empty-composer field, and the hop and rate limits are still a proposal. Free-text nudges
  wait for the first, autonomous actions of any kind for the second; without them a nudge
  could join an unsent draft and be submitted with it.
- **Pushes** through the existing subscriptions, settle time and dedupe, so the operator
  can never be noisier than Needs you already is.
- **The digest:** a short, ranked list of what needs the user and why, at the top of Needs
  you and the dashboard, and as the answer to "what needs me?" in Chat.

## Failure modes

- **Acting on a stale screen.** Every decision carries the fingerprint of the frame it was
  made on; the guards refuse a mismatch and the operator re-reads and re-decides rather
  than retrying. A refused action is a digest line, not a silent drop.
- **Agents talking in circles.** An operator nudge that an agent answers with a question
  the operator then answers is a loop. Cross-agent connect's hop limit and per-pane rate
  limits (a prerequisite for phase 3, above) apply with the operator as sender; past a few autonomous actions on one pane in an
  hour it falls back to Suggest for that pane and says so.
- **Cost.** A pass runs when one of its inputs changes (the watcher's version, an open
  loops refresh, a plan usage poll) or when an age threshold it reported falls due
  ("idle for an hour"), debounced so a busy minute is one pass. Those caches refresh on
  their own cadence without touching the watcher, so the watcher alone would leave a newly
  mergeable PR or a usage reset out of the digest. The model is called only when the
  ranked list actually changed. Triage is a deterministic join; a model is called only to word the
  digest or draft a reply, and has a daily budget metered like Live's. Its own spend shows
  in the digest.
- **Wrong judgment.** Only reversible, low-blast actions are delegable at all, and every
  one is visible afterwards. Revoking a delegation, or one "operator off" switch, takes
  effect at once, not on the next pass: the grant is rechecked under the pane's send lock
  just before typing, and revocation takes the same lock, so once revocation returns no
  action under that grant can land, even one that was waiting on a model reply.

## Phased plan

1. **Digest only.** A daemon-side pass, triggered by state changes, that joins Needs you,
   open loops, idle and finished panes and the usage projection into a ranked list: one
   line per item saying what it is waiting for, why it matters, and the one action that
   unsticks it. Shown atop Needs you and the dashboard, pushed when the set gains something
   new, answerable in Chat. No actions, so no new risk; it is also the cheapest test of
   whether a human-side summary fixes the "short overview" complaint where the per-pane
   headline does not.
2. **Suggested actions with one-tap approve.** Each digest line that maps to an action
   class carries a drafted consent card. Tap rates per class tell us which ones are safe
   to delegate.
3. **Delegated auto-actions per policy.** The user promotes a class for a pane or
   workstream to Act and report; the digest becomes a report of what was done plus what
   still needs them.

## Open questions

- **What "the short overview is not working" means.** Candidates: the per-pane headline
  describes the agent's task rather than what it needs; it goes stale or reads
  "Summarizing…" after a restart; the pane overview panel takes too much room on their
  screen. Ask before phase 1 ships, with the digest as a concrete alternative to react to.
- **Which voice mode to study.** The same user named a voice product they think we should
  copy, not yet identified. Worth learning before the operator speaks its digest, since a
  spoken briefing is the obvious voice surface for it.
- **One session or two.** Is the operator a role of the control session, a second
  long-lived pane, or a daemon loop with no pane? Phase 1 needs no agent at all, which
  defers the choice until actions do.
- **Granularity of policy.** Per pane is precise but dies with the pane; per workstream
  survives restores but needs workstream identity to be reliable first.
