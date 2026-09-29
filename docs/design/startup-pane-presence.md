# Publish Pane Presence Before Classification

## Why

Pane existence comes from tmux, not the language model. Previously the first watcher
tick waited for every capture, classification, and scrollback bootstrap before
publishing any state. A restart therefore made an existing fleet disappear for
40 seconds or more, with latency increasing with pane count and model delays.
Deep-linked mobile panes could even look deleted during that interval.

## Contract

The rule is about a pane the clients have never seen, not about startup. Startup is
simply the case where every pane is one: the first successful tmux discovery publishes
the complete ordered inventory before any pane parsing. Each placeholder has its pane
ID, tmux labels/title, session, window index/name, and focus flags. Tool and activity
are `unknown`: presence is known, but the watcher must not invent agent status or
readiness for input.

`booted` now means **inventory published**, not **every classification finished**.
Before successful discovery it stays false, including when discovery raises.
A successful empty discovery (or no tmux server) publishes an empty, booted state.
Clients can therefore distinguish loading from an authoritative empty inventory.

Parsing remains sequential in the existing worker thread. As each startup pane
finishes, its result replaces only that placeholder and publishes a fresh snapshot.
The existing state version and thread-safe long-poll notification expose progress;
no new endpoint, background worker, classifier, or concurrent model calls are needed.
All identities remain visible while another pane or scrollback bootstrap is slow.

Placeholders never enter the classification/fingerprint caches, so they cannot
suppress a real first parse or contaminate parser context. Published dictionaries
and the list are copied to keep subsequent startup replacements from changing the
already-visible snapshot before its version notification.

## Mid-Session Panes

A window opened while the app is running deserves the same answer, and for a sharper
reason: the dock's "+" navigates to the new pane the instant the endpoint returns the
id, so the client is asking about a pane the daemon has not published yet. Waiting for
classification there is not a slow start, it is a card that does not exist — which the
client can only read as "gone".

So the pre-publish fires for any tick whose inventory holds a pane not seen before, and
"before" is keyed on the PID rather than the id, because tmux hands a closed pane's id
to the next one. A recycled id is therefore a new pane, which is what stops it
pre-publishing the dead occupant's card.

The cost of generalizing is that the pre-publish now lands on decks the client already
has, so it must not undo them: an existing pane carries its last published state into
this snapshot rather than reverting to a placeholder. That state comes from the parse
cache, which holds the same dictionaries the deck was built from — so the fields merged
in later in a tick (the activity-log counter above all, which a client reads a change in
as "refetch this pane's history") travel with it.

What this does not promise is immediacy. The endpoint wakes the watcher, but the wake
only lets the loop begin another tick; one already in flight runs to completion first,
and since classification is sequential that can be several model timeouts away. The
clients therefore hold a just-created pane on screen for a window sized to outlast such
a tick rather than for the measured idle case. Making discovery preempt classification
would turn this into a real bound, and is the obvious next step — it is a change to how
the loop is scheduled rather than to what it publishes, which is why it is not here.

## Boundaries

Per-pane error isolation, natural tmux ordering, and final bootstrap enrichment remain
unchanged. A pane
closed after discovery may remain until the next inventory refresh, as before.
This change removes classification latency from initial visibility, not from the
time it takes all summaries to become available. Discovery itself still depends on
tmux responding; there is no disk-backed state restoration.

The implementation is in `openbus/watcher.py`.

## Verification

Tests block the first and second parsers independently, inspect inventory while the
worker remains blocked, and verify long-poll wakeups as results arrive. They also
cover empty/failed discovery, target filtering, per-pane failures, unchanged later
ticks, cache isolation, focus/order preservation, and publication before bootstrap.
For the mid-session case they hold a tick open on one pane and inspect the deck from
another thread, which is the only moment a carried-over card is observable: a new pane
appears with identity and no activity, an already-classified one keeps both its state
and its activity-log counter, and a recycled id shows the new occupant rather than the
old. Each was confirmed to fail against an implementation without the behavior it
describes, since a test of a publish this transient can otherwise pass by accident.
