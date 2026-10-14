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
        self.pid = None  # no process: no agent transcript to read
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

    def fake_classify(pane, text, **_kw):
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

    # The same prompt over a different command (widget context) is a new ask too.
    clock[0] = 1150.0
    frame[0] = "Overwrite file? > (b)"
    state[0] = {**state[0], "question": {"prompt": "Overwrite file?", "context": "b.txt"}}
    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1150.0, "a new context restarts the clock"

    # As is the same "Do 1 now?" over a different row 1.
    clock[0] = 1200.0
    frame[0] = "Overwrite file? > (c)"
    state[0] = {**state[0], "question": {**state[0]["question"], "items": ["1. c.txt"]}}
    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1200.0, "new rows restart the clock"

    # And the same rows under a different decision.
    clock[0] = 1250.0
    frame[0] = "Overwrite file? > (d)"
    state[0] = {**state[0], "question": {**state[0]["question"], "subject": "Backups:"}}
    w._forced_this_tick = set()
    assert w._tick_pane(pane)["state_since"] == 1250.0, "a new subject restarts the clock"


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
              "1", "1", "4000", "1", ""]
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


def test_a_parse_read_before_an_answer_cannot_put_its_question_back(monkeypatch):
    # /send drops an erased picker's question at once (drop_question); a tick that captured
    # the dialog before that, finishing after it, must not republish the answered ask.
    frame = ["trust dialog"]
    asking = {"activity": "waiting", "waiting_on": "user", "events": [], "tool": "claude",
              "question": {"prompt": "Trust this folder?"}}
    state, clock = [asking], [1000.0]
    w = _harness(monkeypatch, frame, state, clock)
    pane = _Pane()
    w._forced_this_tick = set()
    w._tick_pane(pane)
    w.drop_question(pane.id, "Another prompt?")  # not this card's ask: untouched
    assert w._state[pane.id]["question"]
    clock[0] = 1001.0
    w.drop_question(pane.id, "Trust this folder?")
    assert w._state[pane.id]["question"] is None
    assert w._state[pane.id]["activity"] == "running"
    assert w._state[pane.id]["state_since"] == 1001.0  # running since the answer
    w.drop_question(pane.id, "Trust this folder?")  # reported twice: the guard stands
    clock[0] = 1000.5  # this parse's capture predates the answer
    w._forced_this_tick = {pane.id}
    stale = w._tick_pane(pane)
    assert stale["question"] is None and stale["state_since"] == 1001.0
    # The same prompt over another command is a new ask, even off an early capture.
    state[0] = {**asking, "question": {"prompt": "Trust this folder?", "context": "rm -rf x"}}
    frame[0] = "another dialog"
    w._forced_this_tick = {pane.id}
    assert w._tick_pane(pane)["question"]["context"] == "rm -rf x"
    state[0] = asking
    clock[0], frame[0] = 1002.0, "a new ask"  # read after it: the parse stands
    w._forced_this_tick = {pane.id}
    assert w._tick_pane(pane)["question"] == {"prompt": "Trust this folder?"}


def test_input_generations_never_repeat_across_runs():
    """A menu answer token names its input generation; a restart must not reissue one an
    earlier run handed out, or a token held across it could approve an identical ask."""
    first = Watcher(target=None, use_llm=False)
    first.invalidate_input_actions("%1")
    second = Watcher(target=None, use_llm=False)
    assert second.pane_input_generation("%1") not in {
        first.pane_input_generation("%1") - 1, first.pane_input_generation("%1")}


def test_a_reparse_before_the_answer_is_drawn_reissues_no_token(monkeypatch):
    """send-keys only queues the digit: the forced reparse right after an answer can read
    the old menu, and must keep its consumed generation, or a second tap's digit queues
    behind the first and lands on the next prompt. Output streaming above the menu keeps
    it consumed too; the menu going away ends it."""
    menu = f"{'▔' * 40}\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"  # a widget edge
    frame, clock = [f"● Bash(mkdir s1)\n{menu}"], [1000.0]
    question = {"prompt": "Do you want to proceed?", "answer_style": "menu",
                "options": ["Yes", "No"]}
    state = [{"activity": "waiting", "events": [], "tool": "claude", "question": question}]
    w = _harness(monkeypatch, frame, state, clock)
    pane = _Pane()
    w._forced_this_tick = set()
    held = w._tick_pane(pane)
    issued = held["input_generation"]
    w.consume_answer("%1", w.frame_fp(frame[0]), held["frame"])  # claim_question's call
    for screen in (frame[0], f"● Bash(mkdir s1)\n⎿ streamed\n{menu}"):
        frame[0] = screen
        w._forced_this_tick = {"%1"}
        assert w._tick_pane(pane)["input_generation"] == issued  # the consumed one
    frame[0], state[0] = "● Done.\n> ", {"activity": "idle", "events": [], "tool": "claude"}
    w._forced_this_tick = set()
    assert w._tick_pane(pane)["input_generation"] == w.pane_input_generation("%1")
    assert w._answered == {}


def test_a_claim_during_a_parse_outlives_that_parse(monkeypatch):
    """A tick that captured before a claim must not clear the claim's marker: its screen
    can't show the answer yet, and the next parse would reissue the token."""
    frame, clock = ["$ idle"], [1000.0]
    w = _harness(monkeypatch, frame, [{"activity": "idle", "events": [], "tool": "shell"}],
                 clock)
    parse = W.classify

    def claimed_mid_parse(*a, **kw):
        w.consume_answer("%1", "the claimed screen", "its frame")  # a /send thread
        return parse(*a, **kw)

    monkeypatch.setattr(W, "classify", claimed_mid_parse)
    w._forced_this_tick = set()
    w._tick_pane(_Pane())
    assert w._answered["%1"][1:] == ("the claimed screen", "its frame")
