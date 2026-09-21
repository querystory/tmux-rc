"""The per-pane activity-log cache: append-and-cap behavior (docs/design/activity-log.md).
In-memory only; tmux is the state. This pins the bounded-append invariant the watcher
and the bootstrap seed both rely on."""

from openbus.watcher import EVENTS_LOG_MAX, _append_events


def test_append_stamps_ts_and_preserves_order():
    log = []
    _append_events(log, [{"text": "a"}, {"text": "b"}], 100.0)
    assert [e["text"] for e in log] == ["a", "b"]
    assert all(e["ts"] == 100.0 for e in log)


def test_append_keeps_event_fields():
    log = []
    _append_events(log, [{"text": "edit", "file": {"path": "x", "added": 3}}], 1.0)
    assert log[0]["file"] == {"path": "x", "added": 3}


def test_cap_drops_oldest_keeps_newest():
    log = []
    for i in range(EVENTS_LOG_MAX + 50):
        _append_events(log, [{"text": str(i)}], float(i))
    assert len(log) == EVENTS_LOG_MAX
    assert log[-1]["text"] == str(EVENTS_LOG_MAX + 49)  # newest survives
    assert log[0]["text"] == str(50)  # oldest 50 dropped
