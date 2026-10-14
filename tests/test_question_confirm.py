"""A new user wait is published only once a second capture still shows it (Watcher._confirmed):
Codex flashes its own approval review for a moment, and that flash must never reach the card
or a push. The parse keeps the read, so a real question shows on the next look at it."""

import pytest

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
    monkeypatch.setattr(W, "classify",  # as classify() does, the card names its pane
                        lambda pane, text, **_kw: {**take(parses), "pane_id": pane.id})
    return Watcher(target=None, use_llm=False)


def _tick(w):
    """One pane tick, its card published as the deck (what _confirmed compares with)."""
    w._forced_this_tick = set()
    w.states = [w._tick_pane(_Pane())]
    return w.states[0]


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


def test_output_streaming_above_a_menu_does_not_hold_it(monkeypatch):
    # The recapture is compared on the menu's own rows, not the whole screen.
    menu = "{}\n────────\nDo you want to proceed?\n\n  make\n\n  1. Yes\n  2. No"
    ask = {**ASK, "question": {**ASK["question"], "answer_style": "menu", "options": ["Yes", "No"]}}
    w = _harness(monkeypatch, ["working", menu.format("out 1"), menu.format("out 2")], [WORK, ask])
    _tick(w)
    assert _tick(w)["question"] == ask["question"]


def test_the_recapture_waits_out_the_interval_from_the_first(monkeypatch):
    monkeypatch.setattr(W, "CONFIRM_SECONDS", 1.2)
    slept = []
    monkeypatch.setattr(W.time, "sleep", slept.append)
    w = _harness(monkeypatch, ["working", "menu"], [WORK, ASK])
    _tick(w)
    _tick(w)
    assert len(slept) == 1 and 0 < slept[0] <= 1.2


def test_a_wait_on_others_and_a_shown_question_take_no_second_capture(monkeypatch):
    external = {**WORK, "activity": "waiting", "waiting_on": "external"}
    w = _harness(monkeypatch, ["a", "b", "b", "c"], [external, ASK])
    never = lambda s: (_ for _ in ()).throw(AssertionError(s))  # noqa: E731
    monkeypatch.setattr(W.time, "sleep", never)
    assert _tick(w)["waiting_on"] == "external"
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    assert _tick(w)["question"] == ASK["question"]  # "b", confirmed by its recapture
    monkeypatch.setattr(W.time, "sleep", never)
    assert _tick(w)["question"] == ASK["question"]  # "c": the deck already shows this ask


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
    later = {**ask, "question": {**ask["question"], "context": "sleep 20s"}}
    w = _harness(monkeypatch, ["working", menu.format("10s"), menu.format("20s")],
                 [WORK, ask, later])
    _tick(w)
    assert _tick(w)["question"] is None
    card = _tick(w)  # the 20s menu hashes like the 10s one, but is read, not served from cache
    assert card["question"] == later["question"] and card["frame"] != w.frame_fp(
        menu.format("10s"), ask["question"])


def test_a_failed_parse_carrying_a_held_ask_confirms_nothing(monkeypatch):
    # The cache keeps the held read; a failed parse copies it forward, unread.
    failed = {"activity": "unknown", "events": [], "tool": "codex", "parse_ok": False}
    w = _harness(monkeypatch, ["working", "flash", "moved", "moved again"],
                 [WORK, ASK, failed])
    _tick(w)
    assert _tick(w)["question"] is None
    assert _tick(w)["question"] is None  # "moved" fails to parse; its recapture moved on


def test_a_failed_recapture_leaves_the_screen_unread(monkeypatch):
    w = _harness(monkeypatch, ["working"], [WORK, ASK])
    _tick(w)
    screens = iter(["menu", None, "menu", "menu"])

    def capture(pid, mark_dim=False):
        if (text := next(screens)) is None:
            raise W.subprocess.CalledProcessError(1, "capture-pane")  # the pane went away
        return text

    monkeypatch.setattr(W.tmux, "capture_pane", capture)
    with pytest.raises(W.subprocess.CalledProcessError):
        _tick(w)
    parses = []
    monkeypatch.setattr(W, "classify",
                        lambda pane, text, **_kw: parses.append(1) or {**ASK, "pane_id": pane.id})
    assert _tick(w)["question"] == ASK["question"]  # read again, then confirmed
    assert parses == [1]
