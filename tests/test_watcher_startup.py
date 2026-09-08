"""Startup visibility is independent of slow parsers. No real tmux or model calls."""

import asyncio
import threading

import pytest

from daemon import watcher as W


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
