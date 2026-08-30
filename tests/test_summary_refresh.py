"""session_summary must track a busy session, not freeze at daemon start (#127).
The bootstrap deep read is the ONLY producer of session_summary, and it used to run
once per pane lifetime — so on a busy pane the card's italic summary narrated work
from hours earlier while headline/events stayed current. Pins the refresh contract:
new events since the last read + cadence elapsed ⇒ re-read replaces the summary,
WITHOUT re-seeding events (a re-seed would duplicate the log); an idle pane never
re-reads."""

import openbus.watcher as W
from openbus.watcher import SUMMARY_REFRESH_SECONDS, Watcher, _append_events


class _Pane:
    id = "%1"
    label = "work"


def _watcher(monkeypatch, reads):
    """A Watcher whose deep read pops the next canned {summary, name, events}."""
    monkeypatch.setattr(W, "backing_off", lambda: False)
    monkeypatch.setattr(
        W.tmux, "capture_pane", lambda pid, lines=None, mark_dim=False: "txt"
    )
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "srv:1:%1")
    calls = {"n": 0}

    def fake_bootstrap(pane, text, llm_fn):
        calls["n"] += 1
        return reads.pop(0)

    monkeypatch.setattr(W, "bootstrap", fake_bootstrap)
    return Watcher(target=None, use_llm=True), calls


def test_busy_pane_summary_refreshes(monkeypatch):
    w, calls = _watcher(
        monkeypatch,
        [
            {"summary": "old story", "name": None, "events": []},
            {"summary": "new story", "name": None, "events": []},
        ],
    )
    p = _Pane()
    w._maybe_bootstrap([p])
    assert w._boot["%1"]["summary"] == "old story"
    # Live activity lands, but within the cadence nothing re-reads...
    w._events_seq["%1"] = w._events_seq.get("%1", 0) + 3
    w._maybe_bootstrap([p])
    assert calls["n"] == 1
    # ...cadence elapsed -> the deep read re-runs and the summary follows the session.
    # This is the bug: any pane already in _boot was skipped FOREVER.
    w._boot["%1"]["ts"] -= SUMMARY_REFRESH_SECONDS + 1
    w._maybe_bootstrap([p])
    assert calls["n"] == 2
    assert w._boot["%1"]["summary"] == "new story"


def test_idle_pane_never_rereads(monkeypatch):
    w, calls = _watcher(monkeypatch, [{"summary": "s", "name": None, "events": []}])
    p = _Pane()
    w._maybe_bootstrap([p])
    # No new events since the read -> no re-read, no matter how much time passes.
    w._boot["%1"]["ts"] -= SUMMARY_REFRESH_SECONDS * 10
    w._maybe_bootstrap([p])
    assert calls["n"] == 1


def test_refresh_does_not_reseed_events(monkeypatch):
    seeded = [{"text": "boot event", "historical": True}]
    w, _ = _watcher(
        monkeypatch,
        [
            {"summary": "a", "name": "boot-name", "events": seeded},
            {"summary": "b", "name": None, "events": seeded},
        ],
    )
    p = _Pane()
    w._maybe_bootstrap([p])
    assert [e["text"] for e in w.events_log["%1"]] == ["boot event"]
    # Live activity via the real append path, then an elapsed cadence.
    _append_events(w.events_log["%1"], [{"text": "live"}], 2.0)
    w._events_seq["%1"] += 1
    w._boot["%1"]["ts"] -= SUMMARY_REFRESH_SECONDS + 1
    w._maybe_bootstrap([p])
    # The refresh took the narration only: no duplicate seed in the log, no seq bump
    # (a bump would make the phone refetch an unchanged feed), and the name survives
    # a read that dropped it.
    assert [e["text"] for e in w.events_log["%1"]] == ["boot event", "live"]
    assert w._events_seq["%1"] == 2
    assert {"summary": "b", "name": "boot-name"}.items() <= w._boot["%1"].items()
