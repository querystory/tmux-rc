"""Parse cadence: the LLM runs on a real content change (vs. the last PARSE) or on a
forced reparse — never on a timer. An unchanged screen must cost zero LLM calls no
matter how long it sits, and a slowly-drifting screen must still re-parse on content
alone (docs note at watcher.HEARTBEAT's old definition; the ~58%-duplicate finding)."""

import daemon.watcher as W
from daemon.watcher import Watcher


class _Pane:
    def __init__(self, pid="%1", label="work"):
        self.id = pid
        self.label = label
        self.display_title = label


def _harness(monkeypatch, frame_holder):
    """A Watcher whose capture returns frame_holder[0] and whose classify is a counter."""
    calls = {"n": 0}
    monkeypatch.setattr(W.tmux, "capture_pane", lambda pid, mark_dim=False: frame_holder[0])
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "srv:1:%1")

    def fake_classify(pane, text, llm_fn=None, prior=None, recent_events=None):
        calls["n"] += 1  # one call == one LLM parse
        return {"activity": "idle", "events": [], "label": pane.label, "tool": "shell"}

    monkeypatch.setattr(W, "classify", fake_classify)
    return Watcher(target=None), calls


def test_unchanged_screen_parses_once(monkeypatch):
    frame = ["$ idle prompt"]
    w, calls = _harness(monkeypatch, frame)
    pane = _Pane()
    for _ in range(50):  # 50 ticks, identical frame — a pane sitting idle
        w._forced_this_tick = set()
        w._tick_pane(pane)
    assert calls["n"] == 1, "an unchanged screen must never re-parse (no heartbeat)"


def test_content_change_reparses(monkeypatch):
    frame = ["$ idle prompt"]
    w, calls = _harness(monkeypatch, frame)
    pane = _Pane()
    w._forced_this_tick = set(); w._tick_pane(pane)      # parse 1
    for _ in range(10):
        w._forced_this_tick = set(); w._tick_pane(pane)  # no change
    frame[0] = "$ ls\nfile.txt"                          # real content change
    w._forced_this_tick = set(); w._tick_pane(pane)      # parse 2
    assert calls["n"] == 2


def test_slow_drift_reparses_against_last_parse(monkeypatch):
    # Each tick differs from the PRIOR one only slightly, but every frame differs from
    # what we last parsed — so `changed` (vs _prev_fp, written only on parse) stays true
    # and we re-parse on content. This is the case the old heartbeat existed to catch.
    frame = ["line 0"]
    w, calls = _harness(monkeypatch, frame)
    pane = _Pane()
    for i in range(5):
        frame[0] = f"line {i}"
        w._forced_this_tick = set(); w._tick_pane(pane)
    assert calls["n"] == 5


def test_forced_reparse_ignores_unchanged(monkeypatch):
    # An answered question: screen hasn't changed yet, but the phone sent input, so the
    # card must refresh promptly. Forced parses fire even with no content change.
    frame = ["question? > "]
    w, calls = _harness(monkeypatch, frame)
    pane = _Pane()
    w._forced_this_tick = set(); w._tick_pane(pane)              # parse 1
    w._forced_this_tick = {pane.id}; w._tick_pane(pane)          # forced -> parse 2
    assert calls["n"] == 2
