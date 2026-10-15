"""The shared failure backoff. A failed parse leaves the pane's screen unread so the
watcher retries it (openbus/watcher.py) — which is what lets a stuck card recover, but
also means a sustained outage would retry every pane every tick. The backoff is the
brake; without it a 27-pane server on a 1.5s tick attempts ~18 calls a second."""


import threading
import time
import types

import pytest

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


def test_a_hung_call_fails_at_the_overall_deadline(monkeypatch):
    """The HTTP timeout never covered the token refresh or DNS before it; the overall
    deadline does, and its expiry is handled like any Vertex timeout."""
    _clear()
    release = threading.Event()

    def hang(**_kwargs):
        release.wait(5)

    monkeypatch.setattr(L, "_client", lambda: types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=hang)))
    monkeypatch.setattr(L, "_DEADLINE_S", 0.05)
    try:
        started = time.monotonic()
        assert L.classify_text("system", "text") is None
        assert time.monotonic() - started < 2
        assert "timed out" in L.last_error["msg"]
        assert L.backing_off()
    finally:
        release.set()
        _clear()


def test_a_call_in_flight_does_not_lift_a_brake_armed_after_it_started(monkeypatch):
    """Panes are read in parallel: a success that began before another pane's 429 must
    not clear that pane's brake, nor the reason the UI shows for it."""
    _clear()

    def generate(**_kwargs):  # another pane fails meanwhile, as classify_text records it
        L.last_error["msg"] = L._handle_llm_error(Exception("429 RESOURCE_EXHAUSTED"))
        return types.SimpleNamespace(text="{}", usage_metadata=None)

    monkeypatch.setattr(L, "_client", lambda: types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(L, "_record", lambda resp: None)
    monkeypatch.setattr(L, "_emit", lambda *a, **k: None)
    try:
        assert L.classify_text("system", "text") == {}
        assert L.backing_off()
        assert L.last_error["msg"]
    finally:
        _clear()
        L.last_error["msg"] = None


def test_hung_calls_are_capped_not_accumulated(monkeypatch):
    """Each timed-out call leaves its thread running until the hang ends; past the cap a
    call fails fast as a timeout instead of starting yet another thread."""
    release = threading.Event()
    monkeypatch.setattr(L, "_client", lambda: types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=lambda **_k: release.wait(5))))
    monkeypatch.setattr(L, "_DEADLINE_S", 0.01)
    monkeypatch.setattr(L, "_slots", threading.BoundedSemaphore(2))
    started = threading.active_count()
    try:
        for _ in range(4):
            with pytest.raises(TimeoutError):
                L._generate()
        assert threading.active_count() - started == 2
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name == "vertex":
                t.join(5)


def test_lifting_the_brake_waits_for_one_being_armed(monkeypatch):
    """The success path's check-and-reset holds the same lock _arm_backoff does, so a
    failure arming between the check and the reset cannot be erased."""
    _clear()
    monkeypatch.setattr(L, "_client", lambda: types.SimpleNamespace(models=types.SimpleNamespace(
        generate_content=lambda **_k: types.SimpleNamespace(text="{}", usage_metadata=None))))
    monkeypatch.setattr(L, "_record", lambda resp: None)
    monkeypatch.setattr(L, "_emit", lambda *a, **k: None)
    done = threading.Event()
    with L._backoff_lock:
        threading.Thread(target=lambda: (L.classify_text("s", "t"), done.set())).start()
        assert not done.wait(0.3), "the reset ran without the lock"
    assert done.wait(5)
    _clear()
