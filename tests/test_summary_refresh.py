"""session_summary must track a busy session, not freeze at daemon start (#127).
The bootstrap deep read is the ONLY producer of session_summary, and it used to run
once per pane lifetime — so on a busy pane the card's italic summary narrated work
from hours earlier while headline/events stayed current. Pins the refresh contract:
new events since the last read + rate floor elapsed ⇒ re-read replaces the summary,
WITHOUT re-seeding events (a re-seed would duplicate the log); an idle pane never
re-reads; and an on-demand request_summary refreshes under the floor (but still no-ops
with nothing new to narrate)."""

import daemon.watcher as W
from daemon.watcher import SUMMARY_MIN_INTERVAL, Watcher, _append_events


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
    w._boot["%1"]["ts"] -= SUMMARY_MIN_INTERVAL + 1
    w._maybe_bootstrap([p])
    assert calls["n"] == 2
    assert w._boot["%1"]["summary"] == "new story"


def test_idle_pane_never_rereads(monkeypatch):
    w, calls = _watcher(monkeypatch, [{"summary": "s", "name": None, "events": []}])
    p = _Pane()
    w._maybe_bootstrap([p])
    # No new events since the read -> no re-read, no matter how much time passes.
    w._boot["%1"]["ts"] -= SUMMARY_MIN_INTERVAL * 10
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
    w._boot["%1"]["ts"] -= SUMMARY_MIN_INTERVAL + 1
    w._maybe_bootstrap([p])
    # The refresh took the narration only: no duplicate seed in the log, no seq bump
    # (a bump would make the phone refetch an unchanged feed), and the name survives
    # a read that dropped it.
    assert [e["text"] for e in w.events_log["%1"]] == ["boot event", "live"]
    assert w._events_seq["%1"] == 2
    assert {"summary": "b", "name": "boot-name"}.items() <= w._boot["%1"].items()


def test_on_demand_refreshes_under_floor(monkeypatch):
    """request_summary (card opened in Orchestrator View) pulls a refresh forward past the
    rate floor — as long as there is new activity to narrate."""
    w, calls = _watcher(
        monkeypatch,
        [
            {"summary": "old", "name": None, "events": []},
            {"summary": "fresh", "name": None, "events": []},
        ],
    )
    p = _Pane()
    w._maybe_bootstrap([p])
    assert calls["n"] == 1
    # New events, but still well within the floor: an unforced tick would NOT re-read...
    w._events_seq["%1"] += 2
    w._maybe_bootstrap([p])
    assert calls["n"] == 1
    # ...an on-demand request re-reads now, without waiting out the floor.
    w._force_summary.add("%1")
    w._maybe_bootstrap([p])
    assert calls["n"] == 2
    assert w._boot["%1"]["summary"] == "fresh"
    assert "%1" not in w._force_summary  # request consumed


def test_on_demand_noops_without_new_events(monkeypatch):
    """A forced refresh with nothing new to narrate does not spend a call — and still
    consumes the request so it can't linger."""
    w, calls = _watcher(monkeypatch, [{"summary": "s", "name": None, "events": []}])
    p = _Pane()
    w._maybe_bootstrap([p])
    assert calls["n"] == 1
    w._force_summary.add("%1")  # no events landed since the read
    w._maybe_bootstrap([p])
    assert calls["n"] == 1
    assert "%1" not in w._force_summary
