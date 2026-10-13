"""Live Mode over a flaky link: the Gemini-reconnect backoff must keep reading the browser
socket, so a phone that is already gone (or taps stop) ends the session instead of
producing a ghost Vertex reconnect nobody is listening to."""

import asyncio

from fastapi import WebSocketDisconnect

import openbus.live as L
import openbus.live_providers as P


class _WS:
    def __init__(self, *msgs):
        self.msgs = list(msgs)

    async def receive_json(self):
        if self.msgs:
            m = self.msgs.pop(0)
            if isinstance(m, Exception):
                raise m
            return m
        await asyncio.sleep(3600)  # a live but silent browser


def test_hold_times_out_when_browser_is_silent():
    assert asyncio.run(L._hold(_WS(), 0.01, None)) is True


def test_hold_returns_false_on_stop():
    assert asyncio.run(L._hold(_WS({"action": "stop"}), 5, None)) is False


def test_hold_keeps_reading_past_a_stray_frame():
    ws = _WS({"action": "audio", "data": "AA=="}, {"action": "stop"})
    assert asyncio.run(L._hold(ws, 5, None)) is False


def test_hold_still_hears_the_view():
    """A provider outage can outlast a card's settle time, so leaving view still counts."""
    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=True)
    ws = _WS({"action": "viewing", "on": False}, {"action": "stop"})
    assert asyncio.run(L._hold(ws, 5, meter)) is False
    assert meter.unseen_since is not None


def test_hold_propagates_client_gone():
    try:
        asyncio.run(L._hold(_WS(WebSocketDisconnect()), 5, None))
    except WebSocketDisconnect:
        return
    raise AssertionError("WebSocketDisconnect must propagate so the session ends")
