"""A new user wait is published only once a second capture still shows it (Watcher._confirmed):
Codex flashes its own approval review for a moment, and that flash must never reach the card
or a push. The parse keeps the read, so a real question shows on the next look at it."""

import openbus.watcher as W
from openbus.watcher import Watcher
from tests.test_parse_cadence import _Pane

ASK = {"activity": "waiting", "waiting_on": "user", "events": [], "tool": "codex",
       "question": {"prompt": "Would you like to run the following command?"}}
WORK = {"activity": "running", "events": [], "tool": "codex"}


def _harness(monkeypatch, captures, parses):
    """Captures and parses are consumed in order; the last one repeats."""
    def take(items):
        return items.pop(0) if len(items) > 1 else items[0]
    monkeypatch.setattr(W.tmux, "capture_pane", lambda pid, mark_dim=False: take(captures))
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "srv:1:%1")
    monkeypatch.setattr(W, "classify", lambda pane, text, **_kw: dict(take(parses)))
    return Watcher(target=None, use_llm=False)


def _tick(w):
    w._forced_this_tick = set()
    return w._tick_pane(_Pane())


def test_a_flash_gone_on_the_second_capture_never_needs_you(monkeypatch):
    w = _harness(monkeypatch, ["working", "approval flash", "working again"], [WORK, ASK, WORK])
    assert _tick(w)["activity"] == "running"
    card = _tick(w)  # read the flash; the recapture has moved on
    assert (card["activity"], card["question"], card["waiting_on"]) == ("running", None, None)
    assert _tick(w)["activity"] == "running"  # and the next parse never shows it at all


def test_a_question_still_on_the_second_capture_needs_you_at_once(monkeypatch):
    w = _harness(monkeypatch, ["working", "menu"], [WORK, ASK])
    _tick(w)
    card = _tick(w)
    assert card["activity"] == "waiting" and card["question"] == ASK["question"]


def test_a_held_question_shows_on_the_next_capture_of_it(monkeypatch):
    # The recapture caught a redraw, but the menu is still there on the next tick.
    w = _harness(monkeypatch, ["working", "menu", "redraw", "menu"], [WORK, ASK])
    _tick(w)
    assert _tick(w)["question"] is None
    assert _tick(w)["question"] == ASK["question"]  # an unchanged screen: the cached read


def test_a_question_reread_on_a_changed_screen_is_confirmed_by_the_earlier_read(monkeypatch):
    # Output streams above a held menu: every recapture differs, the ask does not.
    w = _harness(monkeypatch, ["working", "menu 1", "menu 2", "menu 3"], [WORK, ASK])
    _tick(w)
    assert _tick(w)["question"] is None
    assert _tick(w)["question"] == ASK["question"]


def test_the_recapture_waits_out_the_interval_from_the_first(monkeypatch):
    monkeypatch.setattr(W, "CONFIRM_SECONDS", 1.2)
    slept = []
    monkeypatch.setattr(W.time, "sleep", slept.append)
    w = _harness(monkeypatch, ["working", "menu"], [WORK, ASK])
    _tick(w)
    _tick(w)
    assert len(slept) == 1 and 0 < slept[0] <= 1.2


def test_a_wait_on_others_and_a_standing_question_take_no_second_capture(monkeypatch):
    external = {**WORK, "activity": "waiting", "waiting_on": "external"}
    w = _harness(monkeypatch, ["a", "b", "c"], [external, ASK, ASK])
    monkeypatch.setattr(W.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(s)))
    assert _tick(w)["waiting_on"] == "external"
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    assert _tick(w)["question"] is None  # "b": a new user wait; its recapture moved on
    monkeypatch.setattr(W.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(s)))
    assert _tick(w)["question"] == ASK["question"]  # "c": the same ask, already read


def test_a_wait_on_others_turning_into_a_user_wait_is_confirmed(monkeypatch):
    external = {**WORK, "activity": "waiting", "waiting_on": "external"}
    user = {**WORK, "activity": "waiting", "waiting_on": "user"}  # the same key, no question
    w = _harness(monkeypatch, ["a", "b", "c"], [external, user])
    _tick(w)
    assert _tick(w)["activity"] == "running"  # "b": its recapture moved on


def test_a_rejected_flash_keeps_the_running_clock(monkeypatch):
    w = _harness(monkeypatch, ["working", "approval flash", "working again"], [WORK, ASK, WORK])
    since = _tick(w)["state_since"]
    assert _tick(w)["state_since"] == since  # the held card
    assert _tick(w)["state_since"] == since  # and the next parse of the work


def test_a_new_menu_under_the_same_screen_is_not_the_one_read(monkeypatch):
    # "sleep 10s" → "sleep 20s" normalizes alike; the question's own rows tell them apart.
    menu = "Would you like to run the following command?\n\n  sleep {}\n\n  1. Yes\n  2. No"
    ask = {**ASK, "question": {**ASK["question"], "answer_style": "menu", "options": ["Yes", "No"]}}
    w = _harness(monkeypatch, ["working", menu.format("10s"), menu.format("20s")], [WORK, ask])
    _tick(w)
    assert _tick(w)["question"] is None
