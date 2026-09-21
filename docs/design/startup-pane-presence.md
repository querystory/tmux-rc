# Publish Pane Presence Before Classification

## Why

Pane existence comes from tmux, not the language model. Previously the first watcher
tick waited for every capture, classification, and scrollback bootstrap before
publishing any state. A restart therefore made an existing fleet disappear for
40 seconds or more, with latency increasing with pane count and model delays.
Deep-linked mobile panes could even look deleted during that interval.

## Startup Contract

The first successful tmux discovery publishes the complete ordered inventory before
any pane parsing. Each placeholder has its pane ID, tmux labels/title, session,
window index/name, and focus flags. Tool and activity are `unknown`: presence is
known, but the watcher must not invent agent status or readiness for input.

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

## Boundaries

Later ticks retain the existing complete-deck publication behavior rather than
flashing parsed cards back to placeholders. Per-pane error isolation, natural tmux
ordering, ID recycling, and final bootstrap enrichment remain unchanged. A pane
closed after discovery may remain until the next inventory refresh, as before.
This change removes classification latency from initial visibility, not from the
time it takes all summaries to become available. Discovery itself still depends on
tmux responding; there is no disk-backed state restoration.

The implementation is in `daemon/watcher.py` on main; the independent package-rename
PR moves it to `openbus/watcher.py` without changing this contract.

## Verification

Tests block the first and second parsers independently, inspect inventory while the
worker remains blocked, and verify long-poll wakeups as results arrive. They also
cover empty/failed discovery, target filtering, per-pane failures, unchanged later
ticks, cache isolation, focus/order preservation, and publication before bootstrap.
