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


def test_fingerprint_distinguishes_none_from_literal_none():
    # A field flipping between None and the literal string "None" IS a change and must
    # bump — the fingerprint must not coerce them together (the f-string-join bug).
    w = Watcher(target=None)
    w._bump_state_if_changed(_states({**A, "title": None}))    # version 1
    w._bump_state_if_changed(_states({**A, "title": "None"}))  # real change → bump
    assert w.state_version() == 2


def test_fast_active_check_ignores_none_focus(monkeypatch):
    # A transient tmux error makes active_pane_id() return None. The fast check must NOT
    # treat that as "no pane focused" and clear tmux_active on every card (which would
    # bump the version and drop the UI's active selection) — it should bail unchanged.
    from daemon import watcher as watcher_mod

    w = Watcher(target=None)
    w.states = _states({**A, "tmux_active": True}, {**B, "tmux_active": False})
    w._bump_state_if_changed(w.states)
    v = w.state_version()
    monkeypatch.setattr(watcher_mod.tmux, "active_pane_id", lambda: None)
    w._check_active_fast()
    assert w.state_version() == v  # no bump
    assert w.states[0]["tmux_active"] is True  # selection preserved


def test_first_empty_tick_bumps_off_zero():
    # _state_fp starts as None (not ""), so even a first tick to an EMPTY deck (tmux down /
    # no panes) bumps to version 1 — otherwise version stays 0 through a prolonged empty
    # state and the long-poll (which only engages at version > 0) never kicks in.
    w = Watcher(target=None)
    assert w.state_version() == 0
    w._bump_state_if_changed([])
    assert w.state_version() == 1


def test_reparse_fields_bump_the_version():
    # The phone's reparsing spinner settles on the question prompt changing or parsed_at
    # advancing; both must be in the deck fingerprint so a forced reparse wakes the hold.
    w = Watcher(target=None)
    w._bump_state_if_changed(_states({**A, "parsed_at": 100}))  # version 1
    w._bump_state_if_changed(_states({**A, "parsed_at": 200}))  # fresh parse → bump
    assert w.state_version() == 2
    w._bump_state_if_changed(_states({**A, "parsed_at": 200, "question": {"prompt": "y/n?"}}))
    assert w.state_version() == 3  # question appeared → bump
    w._bump_state_if_changed(_states({**A, "parsed_at": 200}))  # question cleared → bump
    assert w.state_version() == 4


def test_non_dict_question_does_not_raise():
    # classify() pipes raw model JSON through unvalidated, so "question" could arrive as a
    # non-dict (e.g. a bare string) from a misbehaving LLM. The deck fingerprint must
    # tolerate that without raising AttributeError (which would stall the watcher loop).
    w = Watcher(target=None)
    w._bump_state_if_changed(_states({**A, "question": "just a string"}))  # must not raise
    assert w.state_version() == 1
    # A bare-string question fingerprints as None (no prompt), same as no question → the
    # transition to an actual {prompt} object is still a real change and bumps.
    w._bump_state_if_changed(_states({**A, "question": {"prompt": "y/n?"}}))
    assert w.state_version() == 2


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


def test_booted_flips_after_first_tick():
    # booted() is False until a _tick COMPLETES, so an empty deck reads as "loading"
    # (spinner) not "no panes". It keys off the completion flag, NOT _last_tick — which
    # _loop stamps even on a tick that raised before producing state.
    w = Watcher(target=None)
    assert w.booted() is False
    w._last_tick = 123.0  # a tick that raised still stamps this — must NOT flip booted
    assert w.booted() is False
    w._booted = True  # what _loop sets after a tick returns normally
    assert w.booted() is True
