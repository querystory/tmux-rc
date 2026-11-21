"""Live Mode's routing, replayed: scripted conversations through the daemon's own socket
handler (research/eval/live), asserting which pane got what under which label, the consent
card, the reason a refused call hands back to the model, and what survives a reconnect.

The gates that do not exist yet are strict xfails, each written as the behaviour the gate
should have: it starts passing the day its gate lands, which fails the suite until the
marker comes off with it."""

import asyncio

import pytest

from research.eval.live import (
    DROP,
    RECONNECT,
    Fleet,
    Script,
    approve,
    call,
    cases,
    replay,
    score,
)


def _play(turns, steps, *, text=False):
    script, fleet = Script(turns), Fleet.load()
    return asyncio.run(replay(fleet, steps, script.connect, text=text)), script, fleet


def _relay(pane, words="rerun the flaky tests"):
    return call("type_in_pane", pane_id=pane, text=f"From the user (via voice): {words}")


def _statuses(trace):
    return [(c.name, p["status"], p.get("reason")) for c, p in trace.results]


def test_a_send_reaches_the_pane_it_names_under_that_panes_label():
    trace, script, fleet = _play([[_relay("%1"), "Sent."]], ["tell lantern search to rerun"])
    text = "From the user (via voice): rerun the flaky tests"
    assert trace.sends() == [("%1", "lantern search", text)]
    assert trace.keys == [("%1", "-l", text), ("%1", "Enter")]
    assert _statuses(trace) == [("type_in_pane", "sent", None)]
    assert fleet.reparsed == ["%1"]
    assert '"lantern search · claude in lantern" (id=%1) — running' in script.prompts[0]
    assert trace.reply(1) == "Sent." and not trace.of("propose")  # voice: no card


@pytest.mark.parametrize("ok", [True, False])
def test_a_text_session_shows_the_card_and_types_only_on_send(ok):
    trace, _, _ = _play([[_relay("%3")]], ["tell rate limits to rerun", approve(ok)], text=True)
    [card] = trace.of("propose")
    assert (card["pane_id"], card["enter"]) == ("%3", True)
    assert card["payload"] == "From the user (via voice): rerun the flaky tests"
    assert bool(trace.keys) is ok
    assert _statuses(trace) == [("type_in_pane", "sent", None) if ok
                                else ("type_in_pane", "declined", "the user declined")]


@pytest.mark.parametrize(("fc", "reason"), [
    (_relay("%99"), "unknown pane"),
    (call("type_in_pane", pane_id="%3", text="hi", press_enter="yes"), "malformed call"),
    (call("press_key", pane_id="%3", key="C-x"), "malformed call"),
    (call("open_pane", pane_id="%99"), "malformed call or unknown pane"),
    (call("rm_rf", pane_id="%3"), "unknown tool"),
])
def test_a_refused_call_goes_back_to_the_model_with_its_reason(fc, reason):
    trace, _, _ = _play([[fc]], ["do it"])
    assert [(p["status"], p["reason"]) for _, p in trace.results] == [("rejected", reason)]
    assert trace.keys == [] and trace.sends() == []


def test_a_tap_on_a_card_whose_pane_was_recycled_types_nothing():
    """The card named one process; by the tap, tmux has given its id to another."""
    def recycle(fleet):
        fleet.panes["%3"]["pid"] = "2000"
    trace, _, _ = _play([[_relay("%3")]], ["tell rate limits", recycle, approve()], text=True)
    assert trace.of("propose") and trace.keys == []
    assert _statuses(trace) == [("type_in_pane", "error", "pane did not accept input")]


def test_a_provider_drop_reconnects_with_a_fresh_prompt_and_replays_no_send():
    trace, script, _ = _play([[_relay("%3"), DROP], ["Nothing more to send."]],
                             ["tell rate limits", "anything else?"])
    assert len(trace.sends()) == 1 and len(trace.keys) == 2
    assert ("status", "reconnecting") in [(f["type"], f.get("status")) for f in trace.frames]
    assert len(script.prompts) == 2 and script.heard == ["tell rate limits", "anything else?"]
    assert trace.reply(2) == "Nothing more to send."


@pytest.mark.parametrize(("after", "sent", "ok"), [
    (approve(), True, True),            # Send on the card the phone still shows
    ("never mind", False, None),        # a new message supersedes it instead
])
def test_a_card_survives_the_phone_reconnecting_and_runs_at_most_once(after, sent, ok):
    trace, _, _ = _play([[_relay("%3")]], ["tell rate limits", RECONNECT, after], text=True)
    [card] = trace.of("propose")
    assert len(trace.keys) == (2 if sent else 0)
    assert {"type": "decided", "id": card["id"], "ok": ok} in [
        {k: v for k, v in f.items() if k != "_turn"} for f in trace.of("decided")]


def test_every_case_names_panes_of_the_fleet():
    panes = set(Fleet.load().panes)
    for case in cases():
        for turn in case["turns"]:
            assert set(turn.get("to", ())) <= panes, case["name"]


@pytest.mark.parametrize(("turn", "clean"), [
    ([_relay(p, "what PRs are you managing?") for p in ("%1", "%3", "%4", "%5")], False),
    (["PR 88 is indexing; 317 and 319 are open in orchard, and 57 waits on a release tag."],
     True),
])
def test_a_fleet_question_scores_clean_only_when_answered_from_the_list(turn, clean):
    [case] = [c for c in cases() if c["name"] == "fleet_pr_rollcall"]
    trace, _, _ = _play([turn], [t["say"] for t in case["turns"]])
    assert score(case["turns"][0], trace, 1) == {
        "target": None, "sends": clean, "answered": clean}


# The routing gates, to come (docs/design/live-routing.md). Each fails today, as the
# afternoon it is modelled on did.

@pytest.mark.xfail(strict=True, reason="gate: one pane per request")
def test_one_request_reaches_one_pane():
    trace, _, _ = _play([[_relay("%1"), _relay("%3"), _relay("%9")]], ["ask everyone"])
    assert [pane for pane, _, _ in trace.sends()] == ["%1"]
    assert [s for _, s, r in _statuses(trace)[1:] if r] == ["rejected", "rejected"]


@pytest.mark.xfail(strict=True, reason="gate: a voice send to a long-idle pane asks first")
def test_a_voice_send_to_a_long_idle_pane_waits_for_a_tap():
    trace, _, _ = _play([[_relay("%2")]], ["make the retry delay configurable"])
    assert [c["pane_id"] for c in trace.of("propose")] == ["%2"] and trace.keys == []


@pytest.mark.xfail(strict=True, reason="gate: a bare yes with nothing pending")
def test_a_bare_yes_with_nothing_pending_is_not_typed():
    trace, _, _ = _play([[call("open_pane", pane_id="%6")], [_relay("%6", "Go for it.")]],
                        ["show me release notes", "go for it"])
    assert trace.keys == []
    assert _statuses(trace)[-1][1] == "rejected" and _statuses(trace)[-1][2]
