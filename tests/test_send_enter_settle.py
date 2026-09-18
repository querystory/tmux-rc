"""The Return must arrive after the typed text has settled, not inside its burst.

Agent TUIs take multi-line input and decide "submit" vs "newline" by timing: a Return
arriving inside a paste burst is text. Sending the literal and the Return back to back
put the burst and the Return in the same window, so an answer sent from the phone sat
composed in the input box and unsent, while the composer reported "Sent".
"""

from pathlib import Path

import pytest

from openbus import tmux


@pytest.fixture(autouse=True)
def _stable_pane_identity(monkeypatch):
    """A Return is withheld if the pane's pid changes across the settle (a recycled "%N").
    Every test here but the recycle ones is about ORDERING, and a real pane_pid would both
    shell out to tmux and add its own calls to their recorded sequences — so hold identity
    steady by default. The recycle tests override this."""
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: "1234")


def _record(monkeypatch, settle):
    """Capture the ORDER of sends and sleeps — the ordering is the whole behaviour."""
    events = []
    monkeypatch.setattr(tmux, "_ENTER_SETTLE_S", settle)
    monkeypatch.setattr(tmux, "_last_paste", {})  # no paste times carried in from another test
    monkeypatch.setattr(tmux, "_run", lambda args: events.append(("send", args[-1])) or "")
    monkeypatch.setattr(tmux.time, "sleep", lambda s: events.append(("sleep", s)))
    return events


def test_a_pause_separates_the_text_from_the_return(monkeypatch):
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "Yes, approve deployment")
    kinds = [(kind, round(v, 1) if kind == "sleep" else v) for kind, v in events]
    # The wait is what REMAINS of the window, so a Return sent straight after its text
    # waits essentially the whole of it (minus the microseconds the sends themselves took).
    assert kinds == [
        ("send", "Yes, approve deployment"),
        ("sleep", 0.3),
        ("send", "Enter"),
    ]


def test_no_return_means_no_wait(monkeypatch):
    """Typing without submitting has no Return to protect, so it pays nothing."""
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "half a thought", enter=False)
    assert events == [("send", "half a thought")]


def test_a_key_name_is_not_a_paste(monkeypatch):
    """Escape / C-c / arrows are single keystrokes — no burst to escape, no wait."""
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "C-c", enter=True, literal=False)
    assert events == [("send", "C-c")]


def test_the_wait_follows_the_last_chunk_of_a_long_paste(monkeypatch):
    """A big answer is chunked under tmux's message cap; the Return must clear the
    LAST chunk, not the first, or the race simply moves."""
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "x" * (tmux._SEND_CHUNK_BYTES + 10))
    assert [kind for kind, _ in events] == ["send", "send", "sleep", "send"]
    assert events[-1] == ("send", "Enter")


def test_the_settle_is_inside_the_send_lock(monkeypatch):
    """The gap between text and Return is the one moment another caller must NOT get in:
    its text would land in the draft we are about to submit, and we would send theirs.

    The ordering tests above are single-threaded and the concurrency test runs with the
    suite-wide settle of 0, so BOTH still pass if this sleep moves outside the lock. This
    one fails: it holds a real settle open on one thread and proves a second caller cannot
    interleave during it.
    """
    import threading

    monkeypatch.setattr(tmux, "_ENTER_SETTLE_S", 0.2)
    monkeypatch.setattr(tmux, "_last_paste", {})  # this test's own clock, not the suite's
    order, in_settle = [], threading.Event()

    def fake_run(args):
        order.append(args[-1])
        return ""

    real_sleep = tmux.time.sleep

    def fake_sleep(seconds):
        in_settle.set()  # we are in the gap; let the other caller try to barge in
        real_sleep(seconds)

    monkeypatch.setattr(tmux, "_run", fake_run)
    monkeypatch.setattr(tmux.time, "sleep", fake_sleep)

    first = threading.Thread(target=tmux.send_keys, args=("%1", "mine"))
    first.start()
    assert in_settle.wait(2), "never reached the settle"
    second = threading.Thread(target=tmux.send_keys, args=("%1", "theirs"))
    second.start()
    first.join(5)
    second.join(5)

    # The interleaving the lock exists to prevent: "theirs" between "mine" and its Return.
    assert order == ["mine", "Enter", "theirs", "Enter"], order


def test_both_composers_send_one_complete_draft():
    web = Path(__file__).resolve().parents[1] / "web"
    desktop = (web / "app.js").read_text()
    mobile = (web / "m/app.js").read_text()
    for source in (desktop, mobile):
        assert 'form.append("text",' in source
        assert 'form.append("image",' in source
    assert "}/compose`" in desktop
    assert 'paneUrl(id, "compose")' in mobile


def test_a_settle_on_one_pane_does_not_stall_another(monkeypatch):
    """The lock is per pane, so the 0.3s a submit spends settling is paid by that pane
    alone. Under one global lock every other pane's input queued behind it — on a rig with
    several agent panes, one person's submit became everyone's latency, for an exclusion
    that only ever protected the draft in the pane being typed into.
    """
    import threading

    monkeypatch.setattr(tmux, "_ENTER_SETTLE_S", 0.2)
    monkeypatch.setattr(tmux, "_last_paste", {})
    # Locks are striped, so pick a second pane that provably lands on a different stripe
    # rather than assuming: two ids CAN share one, and that case is slow, not wrong.
    other = next(p for p in (f"%{n}" for n in range(2, 500))
                 if tmux._pane_lock(p) is not tmux._pane_lock("%1"))
    order, in_settle = [], threading.Event()

    monkeypatch.setattr(tmux, "_run", lambda args: order.append(args[-1]) or "")
    real_sleep = tmux.time.sleep

    def fake_sleep(seconds):
        in_settle.set()
        real_sleep(seconds)

    monkeypatch.setattr(tmux.time, "sleep", fake_sleep)

    slow = threading.Thread(target=tmux.send_keys, args=("%1", "mine"))
    slow.start()
    assert in_settle.wait(2), "never reached the settle"
    # A different pane: it must get through WHILE %1 is still mid-settle.
    tmux.send_keys(other, "C-c", enter=False, literal=False)
    assert order == ["mine", "C-c"], f"{other} waited for %1's settle: {order}"
    slow.join(5)
    assert order == ["mine", "C-c", "Enter"]


def test_a_named_enter_after_a_paste_still_waits(monkeypatch):
    """Live mode can put type_in_pane(press_enter=false) and press_key("Enter") in ONE
    model response, and we run a response's calls back to back with no round trip between
    them. So a Return asked for by NAME lands in the same burst a combined send would
    have, and has to clear the window too — the exemption is for lone keystrokes, not for
    the spelling of the key.
    """
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "the answer", enter=False)      # text, no Return
    tmux.send_keys("%1", "Enter", enter=False, literal=False)  # ...then the Return, by name
    kinds = [(kind, round(v, 1) if kind == "sleep" else v) for kind, v in events]
    assert kinds == [("send", "the answer"), ("sleep", 0.3), ("send", "Enter")]


def test_an_enter_with_no_paste_behind_it_waits_for_nothing(monkeypatch):
    """The key bar's Enter is a lone keystroke. Charging it the settle would tax the most
    tapped button on the bar for a burst that isn't there.
    """
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "Enter", enter=False, literal=False)
    assert events == [("send", "Enter")]


def test_one_pane_s_paste_does_not_delay_another_pane_s_enter(monkeypatch):
    """The paste window is a property of the pane the bytes went into."""
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "typing here", enter=False)
    tmux.send_keys("%2", "Enter", enter=False, literal=False)
    assert events == [("send", "typing here"), ("send", "Enter")]


def test_every_spelling_of_a_pane_sends_to_the_same_id(monkeypatch):
    """send_keys locks per pane id, so the id it gets has to be the pane's own.

    The API accepts several spellings of one pane — "%0", the numeric address, the
    derived label — and used to hand its caller's raw string straight to send_keys. Under
    the old single global lock that was invisible; with a per-pane lock two spellings
    would take two different locks and interleave chunks into the same draft. The
    endpoint already resolves the pane to validate it, so it sends to that.
    """
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from openbus import server
    from openbus.tmux import Pane

    pane = Pane("work", "0", "Resolve PR 38", "0", "%0", "node", "t", "/home/x/proj")
    monkeypatch.setattr(server.tmux, "list_panes", lambda: [pane])
    targets, reparsed = [], []
    monkeypatch.setattr(server.tmux, "send_keys", lambda t, *a, **k: targets.append(t))
    monkeypatch.setattr(
        server.app.state, "watcher",
        SimpleNamespace(request_reparse=reparsed.append), raising=False,
    )

    client = TestClient(server.app)
    for spelling in ("%0", "work:0.0", "Resolve PR 38"):
        assert client.post(
            f"/api/panes/{spelling}/send", json={"keys": "hi", "enter": True, "literal": True}
        ).status_code == 200
    assert targets == ["%0", "%0", "%0"], f"aliases reached send_keys unresolved: {targets}"
    # Same for the reparse: the watcher matches its forced set against pane.id, so a
    # request queued under an alias fires for nobody and the card stays stale.
    assert reparsed == ["%0", "%0", "%0"], f"aliases reached request_reparse: {reparsed}"


def test_a_bare_submit_is_judged_by_the_text_not_by_itself(monkeypatch):
    """Both composers submit with an EMPTY literal, their text having gone out in earlier
    requests. That call must not count as a paste of its own: stamping it would restart
    the clock and make the Return wait the whole window again — throwing the elapsed-time
    measurement away at the one call that exists purely to be measured.
    """
    events = _record(monkeypatch, 0.3)
    now = [0.0]
    monkeypatch.setattr(tmux.time, "monotonic", lambda: now[0])

    tmux.send_keys("%1", "hello", enter=False)  # the real paste, at t=0
    now[0] = 5.0                                # long past the window
    tmux.send_keys("%1", "", enter=True)        # the bare submit
    assert events == [("send", "hello"), ("send", "Enter")], events

    # ...and when the text really is fresh, the same bare submit still waits.
    events.clear()
    tmux.send_keys("%2", "hello", enter=False)
    tmux.send_keys("%2", "", enter=True)
    assert events == [("send", "hello"), ("sleep", 0.3), ("send", "Enter")], events


def test_a_paste_that_dies_half_delivered_still_counts(monkeypatch):
    """A long answer is chunked, and a chunk can fail. The chunks already sent ARE in the
    pane, so the burst really happened: a Return after that failure — the retry, or the
    key bar — still has to clear the window, or it submits the partial draft as a newline.
    """
    events = _record(monkeypatch, 0.3)
    sent = []

    def flaky(args):
        sent.append(args[-1])
        if len(sent) == 2:  # second chunk fails; the first is already in the pane
            raise RuntimeError("tmux said no")
        events.append(("send", args[-1]))
        return ""

    monkeypatch.setattr(tmux, "_run", flaky)
    with pytest.raises(RuntimeError):
        tmux.send_keys("%1", "x" * (tmux._SEND_CHUNK_BYTES + 10), enter=True)

    events.clear()
    tmux.send_keys("%1", "Enter", enter=False, literal=False)  # the Return that follows
    kinds = [(k, round(v, 1) if k == "sleep" else v) for k, v in events]
    assert kinds == [("sleep", 0.3), ("send", "Enter")], kinds


def test_the_paste_clock_forgets_only_what_can_no_longer_matter(monkeypatch):
    """The map is bounded by dropping entries older than the settle window.

    Age, not pane lifecycle, on purpose: an entry that old already computes a
    non-positive remainder, so removing it changes no behaviour at all. Keying cleanup to
    panes would mean deleting a live pane's clock at the wrong moment — ids get recycled,
    and a pane is known to be gone only after its successor is being typed into — and that
    puts the unsent-input bug back.
    """
    _record(monkeypatch, 0.3)
    monkeypatch.setattr(tmux, "_LAST_PASTE_MAX", 3)
    now = [1000.0]
    monkeypatch.setattr(tmux.time, "monotonic", lambda: now[0])

    for n in range(4):  # four stale pastes, all far older than the window
        tmux.send_keys(f"%{n}", "old", enter=False)
    now[0] += 60
    tmux.send_keys("%fresh", "new", enter=False)  # trips the prune

    assert list(tmux._last_paste) == ["%fresh"], tmux._last_paste
    # Nothing that could still delay a Return was dropped: a pane pasted into moments ago
    # survives a prune triggered around it.
    tmux.send_keys("%recent", "also new", enter=False)
    for n in range(4, 8):
        tmux.send_keys(f"%{n}", "filler", enter=False)
    assert "%recent" in tmux._last_paste


def test_a_recycled_pane_id_does_not_get_the_return(monkeypatch):
    """The settle is the only send that spans real time, so it is the only window in which
    the pane can close and tmux hand "%N" to a new one. Pressing Return there would submit
    a STRANGER'S half-typed command. The watcher already treats a pane id as non-durable
    for this reason (Pane.pid); so does this."""
    events = _record(monkeypatch, 0.3)
    pids = iter(["1234", "1234", "9999"])  # different process behind the same id after the wait
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: next(pids))

    with pytest.raises(tmux.PaneChangedError):
        tmux.send_keys("%1", "rm -rf something")

    assert ("send", "Enter") not in events, events
    kinds = [(kind, round(v, 1) if kind == "sleep" else v) for kind, v in events]
    assert kinds == [("send", "rm -rf something"), ("sleep", 0.3)]


def test_a_pane_that_vanished_mid_settle_does_not_get_the_return(monkeypatch):
    """Same guard, the simpler case: the pane is simply gone."""
    events = _record(monkeypatch, 0.3)
    pids = iter(["1234", "1234", None])
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: next(pids))

    with pytest.raises(tmux.PaneChangedError):
        tmux.send_keys("%1", "anything")

    assert ("send", "Enter") not in events, events


def test_an_unreadable_pid_blocks_delivery(monkeypatch):
    """An unknown identity cannot safely receive text or a submit."""
    events = _record(monkeypatch, 0.3)
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: None)

    with pytest.raises(tmux.PaneChangedError):
        tmux.send_keys("%1", "anything")

    assert events == []


def test_recycled_pane_between_text_and_submit_is_a_failure(monkeypatch):
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "original draft", enter=False)
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: "replacement")
    with pytest.raises(tmux.PaneChangedError):
        tmux.send_keys("%1", "", enter=True)
    assert events == [("send", "original draft")]


def test_per_pane_locks_are_retired_only_after_all_users_release_them():
    import gc

    first = tmux._pane_lock("%lock-test")
    assert tmux._pane_lock("%lock-test") is first
    assert tmux._pane_lock("%other-test") is not first
    del first
    gc.collect()
    assert "%lock-test" not in tmux._send_locks


@pytest.mark.parametrize("setting,expected", [
    (None, 0.3), ("", 0.3), ("oops", 0.3), ("nan", 0.3), ("inf", 0.3),
    ("-inf", 0.3), ("-1", 0.0), ("0", 0.0), ("0.45", 0.45),
])
def test_invalid_settle_setting_cannot_break_startup(monkeypatch, setting, expected):
    if setting is None:
        monkeypatch.delenv("TMUXRC_ENTER_SETTLE_S", raising=False)
    else:
        monkeypatch.setenv("TMUXRC_ENTER_SETTLE_S", setting)
    assert tmux._enter_settle_seconds() == expected
