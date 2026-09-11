"""state_since: the timestamp a pane ENTERED its current state, which the client ticks
into a live idle/waiting duration. It must reset only when the activity value or the
pending-question identity changes — NOT on cosmetic screen churn — and it must PERSIST
across unchanged re-parses so the clock keeps climbing (the frozen-idle_seconds bug in
#88). Time and tmux are mocked; nothing touches the real daemon or an LLM."""

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


def _harness(monkeypatch, frame_holder, state_holder, clock):
    """A Watcher whose capture returns frame_holder[0], whose classify returns
    state_holder[0], and whose time.time() reads clock[0] — all caller-controlled."""
    monkeypatch.setattr(W.tmux, "capture_pane", lambda pid, mark_dim=False: frame_holder[0])
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "srv:1:%1")
    monkeypatch.setattr(W.time, "time", lambda: clock[0])

    def fake_classify(pane, text, llm_fn=None, prior=None, recent_events=None):
        s = dict(state_holder[0])  # fresh dict per parse (as the real classify returns)
        s["label"] = pane.label
        return s

    monkeypatch.setattr(W, "classify", fake_classify)
    return Watcher(target=None, use_llm=False)


def test_state_since_persists_across_unchanged_reparses(monkeypatch):
    # A pane that parses idle once and then sits still: state_since must stay put, so the
    # client-computed duration (now - state_since) keeps growing — the frozen-snapshot fix.
    frame = ["$ idle prompt"]
    state = [{"activity": "idle", "events": [], "tool": "shell"}]
    clock = [1000.0]
    w = _harness(monkeypatch, frame, state, clock)
    pane = _Pane()

    w._forced_this_tick = set()
    first = w._tick_pane(pane)
    entered = first["state_since"]
    assert entered == 1000.0

    clock[0] = 1300.0  # 5 minutes later, screen never changed → cached fast-path
    w._forced_this_tick = set()
    later = w._tick_pane(pane)
    assert later["state_since"] == entered, "state_since must not drift on an unchanged pane"
    # The daemon does NOT bake in the elapsed seconds; the client derives them live.
    assert clock[0] - later["state_since"] == 300.0


def test_state_since_resets_on_activity_change(monkeypatch):
    # idle → running is a real state transition: the clock restarts at the new time.
    frame = ["$ idle prompt"]
    state = [{"activity": "idle", "events": [], "tool": "shell"}]
    clock = [1000.0]
    w = _harness(monkeypatch, frame, state, clock)
    pane = _Pane()

    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1000.0

    clock[0] = 1200.0
    frame[0] = "$ make\nbuilding..."          # real content change → re-parse
    state[0] = {"activity": "running", "events": [], "tool": "shell"}
    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1200.0, "activity change must reset the clock"


def test_state_since_resets_on_new_question_but_persists_on_same(monkeypatch):
    # A new question restarts the waiting clock; the SAME question persisting keeps it
    # counting even across a content-churn re-parse (a spinner tick, say).
    frame = ["Proceed? > "]
    q1 = {"activity": "waiting", "question": {"prompt": "Proceed?"}, "events": [], "tool": "claude"}
    state = [q1]
    clock = [1000.0]
    w = _harness(monkeypatch, frame, state, clock)
    pane = _Pane()

    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1000.0

    # Same question, screen churned (re-parse) 60s on: clock must keep climbing.
    clock[0] = 1060.0
    frame[0] = "Proceed? > _"                 # cursor blink churn → re-parse, same question
    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1000.0, "same question keeps counting"

    # A different question replaces it: waiting clock restarts.
    clock[0] = 1100.0
    frame[0] = "Overwrite file? > "
    state[0] = {"activity": "waiting", "question": {"prompt": "Overwrite file?"},
                "events": [], "tool": "claude"}
    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1100.0, "a new question restarts the clock"


def test_restart_seeds_idle_clock_from_tmux_activity(monkeypatch):
    """A fresh daemon seeing an already-idle pane must seed state_since from tmux's
    #{window_activity}, not 'now' — restart used to flash the whole fleet as recent
    for PARKED_IDLE_SECS (#129). A pane first seen BUSY still stamps now, and a real
    transition observed later still resets to now."""
    frame, state, clock = ["idle screen"], [{"activity": "idle"}], [10_000.0]
    w = _harness(monkeypatch, frame, state, clock)
    p = _Pane()
    p.window_activity = "4000"  # tmux: last activity 6000s ago
    first = w._tick_pane(p)
    assert first["state_since"] == 4000.0

    # busy pane, same fresh daemon: its state genuinely starts now
    state[0] = {"activity": "running"}
    frame[0] = "busy screen"
    q = _Pane(pid="%2")
    q.window_activity = "4000"
    assert w._tick_pane(q)["state_since"] == 10_000.0

    # observed transition later: reset to now, tmux's stale stamp must NOT win
    clock[0] = 11_000.0
    state[0] = {"activity": "running"}
    frame[0] = "now busy"
    assert w._tick_pane(p)["state_since"] == 11_000.0


def test_list_panes_populates_window_activity(monkeypatch):
    """The seeding above is inert unless #{window_activity} actually rides in
    _PANE_FMT: a missing column would leave Pane.window_activity at its "" default
    (and an 11-field line would be dropped by the length guard entirely)."""
    tmux = W.tmux
    fields = ["work", "0", "work", "0", "%1", "bash", "title", "/home/u", "42",
              "1", "1", "4000"]
    monkeypatch.setattr(tmux, "_run", lambda args: "\t".join(fields) + "\n")
    (pane,) = tmux.list_panes()
    assert pane.window_activity == "4000"
    assert (pane.id, pane.pane_active) == ("%1", "1")  # order intact around the new column


def test_last_activity_survives_refresh_and_restart(monkeypatch):
    frame, state, clock = ["idle screen"], [{"activity": "idle"}], [10_000.0]
    w = _harness(monkeypatch, frame, state, clock)
    pane = _Pane()
    pane.window_activity = "4000"
    first = dict(w._tick_pane(pane))
    assert first["last_activity_at"] == 4000.0
    clock[0] = 11_000.0
    cached = w._tick_pane(pane)
    assert cached["updated_at"] == 11_000.0
    assert cached["last_activity_at"] == 4000.0
    w._forced_this_tick = {pane.id}
    assert w._tick_pane(pane)["last_activity_at"] == 4000.0
    frame[0] = "new output, same idle classification"
    clock[0] = 12_000.0
    changed = w._tick_pane(pane)
    assert changed["last_activity_at"] == 12_000.0
    assert changed["state_since"] == first["state_since"]
    assert Watcher._deck_fp([first]) != Watcher._deck_fp([{**first, "last_activity_at": 12_000.0}])
