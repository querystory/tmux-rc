"""Parse cadence: the LLM runs on a real content change (vs. the last PARSE) or on a
forced reparse — never on a timer. An unchanged screen must cost zero classify/parse
calls no matter how long it sits, and a slowly-drifting screen must still re-parse on content
alone (the parse-cadence note atop openbus/watcher.py; the ~58%-duplicate finding)."""

import openbus.watcher as W
from openbus.watcher import Watcher


class _Pane:
    def __init__(self, pid="%1", label="work"):
        self.id = pid
        self.label = label
        self.display_title = label
        self.session = "work"
        self.window_index = "0"
        self.window_name = label
        self.session_active = True


def _harness(monkeypatch, frame_holder):
    """A Watcher whose capture returns frame_holder[0] and whose classify is a counter."""
    calls = {"n": 0}
    monkeypatch.setattr(W.tmux, "capture_pane", lambda pid, mark_dim=False: frame_holder[0])
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "srv:1:%1")

    def fake_classify(pane, text, llm_fn=None, prior=None, recent_events=None):
        calls["n"] += 1  # one call == one LLM parse
        return {"activity": "idle", "events": [], "label": pane.label, "tool": "shell"}

    monkeypatch.setattr(W, "classify", fake_classify)
    # use_llm=False keeps the watcher fully off real LLM code (classify is stubbed above,
    # and the idle summarizer never reaches the network) so the test is CI-isolated.
    return Watcher(target=None, use_llm=False), calls


def test_states_carry_session_identity(monkeypatch):
    # Both tick paths must stamp the structural tmux identity (_stamp_identity) — the
    # phone groups windows under their session and follows per-session focus from it.
    frame = ["$ idle prompt"]
    w, _ = _harness(monkeypatch, frame)
    pane = _Pane()
    for _ in range(2):  # parse path first, then the cached (unchanged-screen) path
        w._forced_this_tick = set()
        s = w._tick_pane(pane)
        assert (s["session"], s["window_index"], s["window_name"], s["session_active"]) \
            == ("work", "0", "work", True)


def test_refined_label_survives_idle_ticks(monkeypatch):
    # classify may refine label to the agent's own session name; an unchanged-screen
    # tick must NOT revert it to the tmux label. A tmux-side RENAME still wins.
    frame = ["$ x"]
    w, _ = _harness(monkeypatch, frame)
    monkeypatch.setattr(W, "classify", lambda pane, text, **kw: {
        "activity": "idle", "events": [], "tool": "shell",
        "label": "agent-name", "tmux_label": pane.label})  # as classify.py emits
    pane = _Pane()
    for _ in range(2):  # parse tick, then a cached (unchanged) tick
        w._forced_this_tick = set()
        assert w._tick_pane(pane)["label"] == "agent-name"
    w._forced_this_tick = set()
    assert w._tick_pane(_Pane(label="renamed"))["label"] == "renamed"


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
    w._forced_this_tick = set()
    w._tick_pane(pane)  # parse 1
    for _ in range(10):
        w._forced_this_tick = set()
        w._tick_pane(pane)  # no change
    frame[0] = "$ ls\nfile.txt"                          # real content change
    w._forced_this_tick = set()
    w._tick_pane(pane)  # parse 2
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
        w._forced_this_tick = set()
        w._tick_pane(pane)
    assert calls["n"] == 5


def test_forced_reparse_ignores_unchanged(monkeypatch):
    # An answered question: screen hasn't changed yet, but the phone sent input, so the
    # card must refresh promptly. Forced parses fire even with no content change.
    frame = ["question? > "]
    w, calls = _harness(monkeypatch, frame)
    pane = _Pane()
    w._forced_this_tick = set()
    w._tick_pane(pane)  # parse 1
    w._forced_this_tick = {pane.id}
    w._tick_pane(pane)  # forced -> parse 2
    assert calls["n"] == 2
