"""The shared failure backoff. A failed parse leaves the pane's screen unread so the
watcher retries it (openbus/watcher.py) — which is what lets a stuck card recover, but
also means a sustained outage would retry every pane every tick. The backoff is the
brake; without it a 27-pane server on a 1.5s tick attempts ~18 calls a second."""


import openbus.llm as L


def _clear():
    L._backoff.update(delay=0.0, until=0.0)


def _raise(exc_type_name: str, msg: str):
    exc = type(exc_type_name, (Exception,), {})(msg)
    L._handle_llm_error(exc)


def test_service_health_failures_arm_the_backoff():
    """429 always did. Auth and timeouts did not, and they are the ones that persist:
    expired auth needs a human, and a dead endpoint is not revived by asking again in
    1.5s. Retrying those every tick, for every pane, is the storm Copilot flagged."""
    for name, msg in (
        ("RefreshError", "Reauthentication is needed"),
        ("TimeoutError", "deadline timeout exceeded"),
        ("ClientError", "429 RESOURCE_EXHAUSTED"),
    ):
        _clear()
        _raise(name, msg)
        assert L.backing_off(), f"{name} must arm the backoff"
        assert 0 < L._backoff_remaining() <= 15.0
    _clear()


def test_a_per_pane_content_failure_does_not_pause_every_other_pane():
    """The backoff is SHARED, so arming it is a claim about the service, not about one
    screen. Malformed JSON is the opposite: a property of one pane's content and the
    completion it provoked. Arming on it let a single odd screen stop classification for
    every other pane for up to two minutes — and under this PR the refusals handed to
    those panes are themselves failed parses, so they spin unread until the window ends.
    That pane's own retry is bounded per-pane in the watcher (PARSE_RETRIES) instead."""
    _clear()
    _raise("JSONDecodeError", "Expecting value")
    assert not L.backing_off(), "one pane's junk must not pause the other 26"
    _clear()


def test_backoff_doubles_to_a_cap_then_a_success_clears_it():
    _clear()
    seen = []
    for _ in range(6):
        _raise("TimeoutError", "timeout")
        seen.append(L._backoff["delay"])
    assert seen[:3] == [15.0, 30.0, 60.0]
    assert seen[-1] == 120.0, "caps so a long outage settles at one try per 2min"
    # Any success resets it — recovery must be immediate, not wait out the last window.
    L._backoff.update(delay=0.0, until=0.0)
    assert not L.backing_off()


def test_an_armed_backoff_skips_the_call_entirely():
    """The brake only works if the guard actually returns before the network call."""
    _clear()
    _raise("TimeoutError", "timeout")
    called = []
    real_client = L._client
    L._client = lambda: called.append(1)  # would blow up if reached
    try:
        assert L.classify_text("system", "text") is None
        assert not called, "must not call Vertex while backing off"
        assert L.last_error["msg"], "and it says why, so the UI can show the degradation"
    finally:
        L._client = real_client
        _clear()
