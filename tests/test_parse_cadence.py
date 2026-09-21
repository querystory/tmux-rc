"""Parse cadence: the LLM runs on a real content change (vs. the last PARSE) or on a
forced reparse — never on a timer. An unchanged screen must cost zero classify/parse
calls no matter how long it sits, and a slowly-drifting screen must still re-parse on content
alone (the parse-cadence note atop openbus/watcher.py; the ~58%-duplicate finding)."""

import openbus.watcher as W
from openbus.watcher import Watcher


class _Pane:
    def __init__(self, pid="%1", label="work", current_command="bash"):
        self.id = pid
        self.current_command = current_command  # real classify() anchors tool on this
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

    def fake_classify(pane, text, llm_fn=None, prior=None, recent_events=None, prev_activity=None):
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


def test_failed_parse_retries_the_same_screen_instead_of_retiring_it(monkeypatch):
    """The permanent-"Running"-badge bug. A parse failure (a 429 returning None) used to
    advance the pane's fingerprint anyway, which marks the screen read — and since an
    unchanged screen is never re-parsed, the pane kept whatever the failed parse guessed.
    A finished agent's screen never changes again, so that guess was final. The failure
    must leave the screen unread so the next tick picks it up."""
    frame = ["agent finished · done 10:28 PM"]
    w, calls = _harness(monkeypatch, frame)
    outcomes = [None, None, {"activity": "idle", "events": [], "tool": "claude"}]

    def flaky(pane, text, llm_fn=None, prior=None, recent_events=None, prev_activity=None):
        calls["n"] += 1
        got = outcomes.pop(0) if outcomes else {"activity": "idle", "events": [], "tool": "claude"}
        if got is None:  # what classify() returns when the model call failed
            return {"activity": prev_activity or "unknown", "tool": "unknown",
                    "events": [], "parse_ok": False}
        return dict(got, label=pane.label)

    monkeypatch.setattr(W, "classify", flaky)
    pane = _Pane()
    for _ in range(3):
        w._forced_this_tick = set()
        state = w._tick_pane(pane)
    assert calls["n"] == 3, "each failure must leave the screen unread for the next tick"
    assert state["activity"] == "idle", "the parse that finally succeeded must win"
    # And once it HAS been read, the screen is retired again: no heartbeat re-parse.
    for _ in range(10):
        w._forced_this_tick = set()
        w._tick_pane(pane)
    assert calls["n"] == 3
    assert "parse_ok" not in state, "internal flag must not reach the UI"


def test_repeated_failures_dont_restart_the_pane_clocks(monkeypatch):
    """Copilot, #210: the retry must not make a STILL pane look busy. `changed` used to
    answer two questions at once — "is this screen new to us?" and "did the screen
    move?" — and leaving the fingerprint unset on failure told both yes. So a sustained
    LLM outage re-recorded a snapshot every tick and pinned idle_seconds at 0, and the
    pane never aged out of "Recent" for as long as it lasted."""
    frame = ["agent finished · done 10:28 PM"]  # an agent TUI: no bare shell prompt
    w, calls = _harness(monkeypatch, frame)

    def always_fails(pane, text, llm_fn=None, prior=None, recent_events=None, prev_activity=None):
        calls["n"] += 1
        return {"activity": prev_activity or "unknown", "tool": "unknown",
                "events": [], "parse_ok": False}

    monkeypatch.setattr(W, "classify", always_fails)
    pane = _Pane()
    for _ in range(6):
        w._forced_this_tick = set()
        w._tick_pane(pane)
    assert calls["n"] == 6, "an unread screen must still be retried every tick"
    assert len(w.snapshots["%1"]) == 1, "a screen that never moved is ONE snapshot"


def test_no_llm_shell_prompt_retires_the_screen(monkeypatch):
    """Copilot, #210: with TMUXRC_NO_LLM=1 every parse takes the fallback, so marking the
    recognized shell prompt a failure would leave the fingerprint permanently unset —
    every tick a "change", the snapshot ring filling with one identical screen. The bare
    prompt is a real read (it is what _obvious_idle is FOR), so it retires the screen."""
    frame = ["user@host:~$ "]
    # The real classify(), reached with no llm_fn — exactly what use_llm=False does.
    monkeypatch.setattr(W.tmux, "capture_pane", lambda pid, mark_dim=False: frame[0])
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "srv:1:%1")
    w = Watcher(target=None, use_llm=False)
    pane = _Pane()
    for _ in range(5):
        w._forced_this_tick = set()
        state = w._tick_pane(pane)
    assert state["activity"] == "idle"
    assert len(w.snapshots["%1"]) == 1, "an unchanged shell prompt is ONE snapshot"


def test_failed_parse_keeps_the_whole_card_not_just_the_activity(monkeypatch):
    """Copilot, #210 round 2: an unread screen must not REDACT the card either.
    classify()'s fallback can only carry `activity` forward, so a waiting pane came back
    without its `question` — and the phone gates the answer controls on that field
    (`show("question", !!pane.question && needsYou(pane))`). The card kept its "Needs
    you" badge while the buttons to answer it silently vanished, which is worse than a
    stale card: the user is told to act and given nothing to act with."""
    frame = ["? Do you want to proceed?  1. Yes  2. No"]
    w, calls = _harness(monkeypatch, frame)
    good = {"activity": "waiting", "waiting_on": "user", "tool": "claude", "events": [],
            "headline": "Asking to proceed",
            "question": {"answer_style": "menu", "prompt": "Proceed?", "options": ["Yes", "No"]}}
    seq = [good]

    def then_fails(pane, text, llm_fn=None, prior=None, recent_events=None, prev_activity=None):
        calls["n"] += 1
        if seq:
            return dict(seq.pop(0))
        return {"activity": prev_activity or "unknown", "tool": "unknown",
                "events": [], "parse_ok": False}

    monkeypatch.setattr(W, "classify", then_fails)
    pane = _Pane(current_command="node")
    w._forced_this_tick = set()
    w._tick_pane(pane)
    # A REAL content change (trailing whitespace is stripped by the fingerprint), so the
    # pane re-parses — and that parse fails.
    frame[0] = "? Do you want to proceed?  1. Yes  2. No  3. Later"
    w._forced_this_tick = set()
    state = w._tick_pane(pane)
    assert state["question"] == good["question"], "the answer affordance must survive"
    assert state["waiting_on"] == "user" and state["activity"] == "waiting"
    assert state["headline"] == good["headline"] and state["tool"] == "claude"
    # Still live fields, re-stamped from tmux rather than frozen with the old card.
    assert state["snapshot_id"] is not None
    assert "parse_ok" not in state


def test_failed_forced_reparse_still_advances_parsed_at(monkeypatch):
    """The phone stops spinning an answered control when `parsed_at` advances. A forced
    reparse that FAILS still has to move it, or the control spins forever."""
    frame = ["? Proceed?"]
    w, calls = _harness(monkeypatch, frame)
    seq = [{"activity": "waiting", "waiting_on": "user", "tool": "claude", "events": [],
            "question": {"prompt": "Proceed?"}}]

    def then_fails(pane, text, llm_fn=None, prior=None, recent_events=None, prev_activity=None):
        calls["n"] += 1
        if seq:
            return dict(seq.pop(0))
        return {"activity": prev_activity or "unknown", "tool": "unknown",
                "events": [], "parse_ok": False}

    monkeypatch.setattr(W, "classify", then_fails)
    pane = _Pane(current_command="node")
    w._forced_this_tick = set()
    first = w._tick_pane(pane)["parsed_at"]
    w._forced_this_tick = {"%1"}  # phone sent input; screen unchanged
    assert w._tick_pane(pane)["parsed_at"] > first


def test_failed_forced_reparse_still_retries(monkeypatch):
    """Copilot, #210 round 3 — and they were right where I argued otherwise. A forced
    reparse runs on an UNCHANGED screen (the phone just answered a question), so
    _prev_fp already matches this text from the earlier successful parse. Declining to
    SET it on failure was not enough: the stale matching value kept `changed` False, so
    the retry never came and the answered question sat on the card until the screen
    moved on its own. That is the same stuck-card failure this PR exists to fix, reached
    by the path the user actually notices. The failure has to CLEAR the mark."""
    frame = ["? Proceed?"]
    w, calls = _harness(monkeypatch, frame)
    seq = [{"activity": "waiting", "waiting_on": "user", "tool": "claude", "events": [],
            "question": {"prompt": "Proceed?"}}]

    def then_fails(pane, text, llm_fn=None, prior=None, recent_events=None, prev_activity=None):
        calls["n"] += 1
        if seq:
            return dict(seq.pop(0))
        return {"activity": prev_activity or "unknown", "tool": "unknown",
                "events": [], "parse_ok": False}

    monkeypatch.setattr(W, "classify", then_fails)
    pane = _Pane(current_command="node")
    w._forced_this_tick = set()
    w._tick_pane(pane)                      # 1: succeeds, retires the screen
    w._forced_this_tick = {"%1"}
    w._tick_pane(pane)                      # 2: forced reparse on the SAME screen, fails
    assert calls["n"] == 2
    w._forced_this_tick = set()             # 3: an ordinary tick must pick it back up
    state = w._tick_pane(pane)
    assert calls["n"] == 3, "a failed forced reparse must leave the screen unread"
    assert state["question"] == {"prompt": "Proceed?"}, "and the card stays answerable"
