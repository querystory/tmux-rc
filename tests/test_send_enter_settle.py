"""The Return must arrive after the typed text has settled, not inside its burst.

Agent TUIs take multi-line input and decide "submit" vs "newline" by timing: a Return
arriving inside a paste burst is text. Sending the literal and the Return back to back
put the burst and the Return in the same window, so an answer sent from the phone sat
composed in the input box and unsent, while the composer reported "Sent".
"""

import openbus.tmux as tmux


def _record(monkeypatch, settle):
    """Capture the ORDER of sends and sleeps — the ordering is the whole behaviour."""
    events = []
    monkeypatch.setattr(tmux, "_ENTER_SETTLE_S", settle)
    monkeypatch.setattr(tmux, "_run", lambda args: events.append(("send", args[-1])) or "")
    monkeypatch.setattr(tmux.time, "sleep", lambda s: events.append(("sleep", s)))
    return events


def test_a_pause_separates_the_text_from_the_return(monkeypatch):
    events = _record(monkeypatch, 0.3)
    tmux.send_keys("%1", "Yes, approve deployment")
    assert events == [
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
