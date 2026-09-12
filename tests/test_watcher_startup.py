"""Startup visibility is independent of slow parsers. No real tmux or model calls."""

import asyncio
import threading

import pytest

from openbus import watcher as W


@pytest.fixture
def inventory(monkeypatch):
    panes = [W.tmux.Pane("work", str(i), f"agent-{i}", "0", f"%{i}",
                        "node", f"Task {i}", pid=str(100 + i),
                        window_active="1", pane_active="1") for i in range(2)]
    monkeypatch.setattr(W.tmux, "server_running", lambda: True)
    monkeypatch.setattr(W.tmux, "list_panes", lambda: panes)
    monkeypatch.setattr(W.tmux, "active_pane_id", lambda: "%1")
    w = W.Watcher(None, use_llm=True)
    monkeypatch.setattr(w, "_pane_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(w, "_maybe_bootstrap", lambda panes: None)
    return w, panes


def parsed(pane):
    state = {"pane_id": pane.id, "activity": "idle", "tool": "codex"}
    W._stamp_identity(state, pane)
    return state


def test_presence_and_results_publish_before_slow_work_finishes(inventory, monkeypatch):
    w, panes = inventory
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    def classify(pane):
        index = panes.index(pane)
        entered[index].set()
        assert release[index].wait(5), "test did not release parser"
        return parsed(pane)

    def bootstrap(panes):
        assert [s["activity"] for s in w.states] == ["idle", "idle"]

    monkeypatch.setattr(w, "_tick_pane", classify)
    monkeypatch.setattr(w, "_maybe_bootstrap", bootstrap)

    async def scenario():
        w._evloop = asyncio.get_running_loop()
        tick = asyncio.create_task(asyncio.to_thread(w._tick))
        try:
            assert await asyncio.to_thread(entered[0].wait, 3)
            assert not tick.done()
            assert w.booted()
            assert [s["pane_id"] for s in w.states] == ["%0", "%1"]
            assert [s["label"] for s in w.states] == [p.label for p in panes]
            assert [s["session"] for s in w.states] == ["work", "work"]
            assert [s["window_index"] for s in w.states] == ["0", "1"]
            assert [s["tmux_active"] for s in w.states] == [False, True]
            assert all(s["activity"] == "unknown" for s in w.states)
            assert not w._state and not w._prev_fp
            presence = w.states
            version = w.state_version()
            assert version > 0
            waiter = asyncio.create_task(w.wait_for_state_change(version, timeout=3))
            release[0].set()
            assert await asyncio.to_thread(entered[1].wait, 3)
            assert await asyncio.wait_for(waiter, 3) > version
            assert not tick.done()
            assert [s["activity"] for s in w.states] == ["idle", "unknown"]
            assert all(s["activity"] == "unknown" for s in presence)
        finally:
            for event in release:
                event.set()
            await asyncio.wait_for(tick, 3)
        assert [s["activity"] for s in w.states] == ["idle", "idle"]

    asyncio.run(scenario())


@pytest.mark.parametrize("running", [True, False])
def test_empty_discovery_publishes_booted_state(inventory, monkeypatch, running):
    w, panes = inventory
    panes.clear()
    monkeypatch.setattr(W.tmux, "server_running", lambda: running)
    w._tick()
    assert w.booted() and w.states == [] and w.state_version() == 1
    w._tick()
    assert w.state_version() == 1


def test_failed_discovery_stays_loading_then_recovers(inventory, monkeypatch):
    w, panes = inventory
    with monkeypatch.context() as patch:
        patch.setattr(W.tmux, "list_panes", lambda: (_ for _ in ()).throw(RuntimeError("tmux failed")))
        with pytest.raises(RuntimeError, match="tmux failed"):
            w._tick()
    assert not w.booted() and w.states == [] and w.state_version() == 0
    monkeypatch.setattr(w, "_tick_pane", parsed)
    w._tick()
    assert w.booted() and len(w.states) == len(panes)


def test_target_discovery_does_not_publish_other_panes(inventory, monkeypatch):
    w, panes = inventory
    w.target = "%1"
    monkeypatch.setattr(W.tmux, "find_pane", lambda target: panes[1])
    def classify(pane):
        assert [s["pane_id"] for s in w.states] == ["%1"]
        return parsed(pane)
    monkeypatch.setattr(w, "_tick_pane", classify)
    w._tick()
    monkeypatch.setattr(W.tmux, "find_pane", lambda target: None)
    w._tick()
    assert w.booted() and w.states == []


def test_bad_pane_does_not_hide_other_results(inventory, monkeypatch):
    w, panes = inventory
    def classify(pane):
        if pane == panes[0]:
            raise RuntimeError("bad pane")
        return parsed(pane)
    monkeypatch.setattr(w, "_tick_pane", classify)
    w._tick()
    assert [s["activity"] for s in w.states] == ["unknown", "idle"]
    assert w.states[0]["label"] == panes[0].label


def test_later_ticks_keep_classified_state_until_replacement(inventory, monkeypatch):
    w, panes = inventory
    monkeypatch.setattr(w, "_tick_pane", parsed)
    w._tick()
    before = w.states
    version = w.state_version()
    def classify(pane):
        assert w.states is before
        assert all(s["activity"] == "idle" for s in w.states)
        return parsed(pane)
    monkeypatch.setattr(w, "_tick_pane", classify)
    w._tick()
    assert w.state_version() == version


def _tick_blocked_on(w, monkeypatch, pane_id, inspect):
    """Run one _tick with the classification of `pane_id` held open, run `inspect` while
    it is stuck there, then release it and let the tick finish. That window is where the
    pre-classification publish is observable — and the only place it can be asserted on,
    since by the time the tick returns the real states have overwritten it."""
    entered, release = threading.Event(), threading.Event()

    def classify(pane):
        if pane.id == pane_id:
            entered.set()
            assert release.wait(5), "test did not release parser"
        return parsed(pane)

    monkeypatch.setattr(w, "_tick_pane", classify)

    async def scenario():
        w._evloop = asyncio.get_running_loop()
        tick = asyncio.create_task(asyncio.to_thread(w._tick))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert not tick.done()  # still classifying
            inspect()
        finally:
            release.set()
            await tick

    asyncio.run(scenario())


def _classified(w, monkeypatch, panes):
    """Get the fixture past startup: every pane seen, classified, and published."""
    monkeypatch.setattr(w, "_tick_pane", parsed)
    w._tick()
    assert [s["activity"] for s in w.states] == ["idle"] * len(panes)


def test_new_pane_is_visible_before_it_is_classified(inventory, monkeypatch):
    """A window opened mid-session (the dock's "+") must show up as a known-but-
    unclassified card at once, not only once the model has finished with it — the same
    presence-before-parse guarantee startup gets."""
    w, panes = inventory
    _classified(w, monkeypatch, panes)
    panes.append(W.tmux.Pane("work", "2", "agent-2", "0", "%2", "node", "Task 2",
                             pid="102", window_active="0", pane_active="0"))

    def while_blocked():
        # The new pane is already published, with identity, awaiting classification.
        assert [s["pane_id"] for s in w.states] == ["%0", "%1", "%2"]
        new = w.states[-1]
        assert new["activity"] == "unknown" and new["label"] == panes[-1].label
        # ...and the panes already classified did NOT flicker back to unknown.
        assert [s["activity"] for s in w.states[:2]] == ["idle", "idle"]

    _tick_blocked_on(w, monkeypatch, "%2", while_blocked)
    assert [s["activity"] for s in w.states] == ["idle"] * 3


def test_recycled_pane_id_does_not_prepublish_the_old_occupant(inventory, monkeypatch):
    """tmux hands a closed pane's id to the next pane. The pre-publish keys "already
    known" off the PID, not the id, so a recycled id is treated as a brand-new pane: it
    must show the NEW pane's identity as unclassified, never the classified card of the
    tenant that just died — a card that would name the wrong window and, worse, look
    settled rather than pending."""
    w, panes = inventory
    _classified(w, monkeypatch, panes)
    panes[1] = W.tmux.Pane("work", "7", "agent-new", "0", "%1", "node", "Task new",
                           pid="999", window_active="0", pane_active="0")

    def while_blocked():
        recycled = w.states[1]
        assert recycled["pane_id"] == "%1"
        assert recycled["activity"] == "unknown"  # not the dead pane's "idle"
        assert recycled["label"] == panes[1].label and recycled["window_index"] == "7"
        assert w.states[0]["activity"] == "idle"  # the untouched pane is undisturbed

    _tick_blocked_on(w, monkeypatch, "%1", while_blocked)
    assert [s["activity"] for s in w.states] == ["idle", "idle"]
