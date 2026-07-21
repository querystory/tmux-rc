"""State long-poll: the deck version bumps only when the phone-facing view changes, and
wait_for_state_change holds until it does. This is what makes /api/state return the
instant a pane switch / add / activity change happens instead of on a fixed interval."""

import asyncio

from daemon.watcher import Watcher


def _run(coro):
    return asyncio.run(coro)


def _states(*panes):
    return [dict(p) for p in panes]


A = {"pane_id": "%1", "tmux_active": True, "label": "a", "activity": "idle", "events_seq": 0}
B = {"pane_id": "%2", "tmux_active": False, "label": "b", "activity": "running", "events_seq": 0}


def test_version_bumps_only_on_deck_change():
    w = Watcher(target=None)
    assert w.state_version() == 0
    w._bump_state_if_changed(_states(A, B))
    assert w.state_version() == 1
    w._bump_state_if_changed(_states(A, B))  # same deck → no bump
    assert w.state_version() == 1
    # pane switch (active flips) → bump
    w._bump_state_if_changed(_states({**A, "tmux_active": False}, {**B, "tmux_active": True}))
    assert w.state_version() == 2
    # new activity on a pane → bump
    w._bump_state_if_changed(_states({**A, "tmux_active": False}, {**B, "tmux_active": True, "events_seq": 3}))
    assert w.state_version() == 3


def test_live_frame_churn_does_not_bump():
    # Fields not in the deck fingerprint (a frame hash, cost, etc.) must NOT bump the
    # version — that churn is /api/live's concern, not the deck hold's.
    w = Watcher(target=None)
    w._bump_state_if_changed(_states(A))
    v = w.state_version()
    w._bump_state_if_changed(_states({**A, "frame_hash": "abc", "cost": 1.23}))
    assert w.state_version() == v


def test_transition_to_empty_deck_bumps():
    # tmux-down / no-panes sets states=[] via _tick's early returns; that transition must
    # bump so the /api/state hold returns to show "no panes" instead of holding to timeout.
    w = Watcher(target=None)
    w._bump_state_if_changed(_states(A))  # version 1
    w._bump_state_if_changed([])          # populated → empty is a deck change
    assert w.state_version() == 2
    w._bump_state_if_changed([])          # already empty → no further bump
    assert w.state_version() == 2


def test_wait_returns_immediately_when_already_ahead():
    w = Watcher(target=None)
    w._bump_state_if_changed(_states(A))  # version 1
    got = _run(asyncio.wait_for(w.wait_for_state_change(0, timeout=5), timeout=1))
    assert got == 1  # client behind → return at once, no hold


def test_wait_wakes_on_bump():
    async def scenario():
        w = Watcher(target=None)
        w._evloop = asyncio.get_running_loop()  # so _bump can notify waiters
        w._bump_state_if_changed(_states(A))  # version 1; client caught up at 1
        waiter = asyncio.create_task(w.wait_for_state_change(1, timeout=5))
        await asyncio.sleep(0.05)  # let it start holding
        assert not waiter.done()
        w._bump_state_if_changed(_states({**A, "tmux_active": False}))  # version 2 → wake
        return await asyncio.wait_for(waiter, timeout=1)

    assert _run(scenario()) == 2


def test_wait_no_missed_wakeup_when_bump_lands_before_wait():
    # A bump that lands after the version bump but before the waiter actually awaits the
    # Event must NOT be lost: the Event stays set (it is never cleared by _bump), and the
    # waiter clears-then-checks, so it returns at once instead of blocking to timeout.
    async def scenario():
        w = Watcher(target=None)
        w._evloop = asyncio.get_running_loop()
        w._bump_state_if_changed(_states(A))  # version 1; client caught up at 1
        # Simulate the racy bump landing "early": version advances and the Event is set
        # before we ever start waiting.
        w._bump_state_if_changed(_states({**A, "tmux_active": False}))  # version 2
        await asyncio.sleep(0)  # let the call_soon_threadsafe(set) run
        # Even with a tiny timeout, we should return immediately with version 2.
        return await asyncio.wait_for(w.wait_for_state_change(1, timeout=0.2), timeout=1)

    assert _run(scenario()) == 2


def test_wait_times_out_when_nothing_changes():
    w = Watcher(target=None)
    w._bump_state_if_changed(_states(A))  # version 1
    got = _run(asyncio.wait_for(w.wait_for_state_change(1, timeout=0.2), timeout=1))
    assert got == 1  # timed out → current version unchanged
