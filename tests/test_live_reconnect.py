"""Live Mode over a flaky link: the Gemini-reconnect backoff must keep reading the browser
socket, so a phone that is already gone (or taps stop) ends the session instead of
producing a ghost Vertex reconnect nobody is listening to."""

import asyncio

from fastapi import WebSocketDisconnect

import daemon.live as L


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
    assert asyncio.run(L._hold(_WS(), 0.01)) is True


def test_hold_returns_false_on_stop():
    assert asyncio.run(L._hold(_WS({"action": "stop"}), 5)) is False


def test_hold_tolerates_a_stray_frame():
    assert asyncio.run(L._hold(_WS({"action": "audio", "data": "AA=="}), 5)) is True


def test_hold_propagates_client_gone():
    try:
        asyncio.run(L._hold(_WS(WebSocketDisconnect()), 5))
    except WebSocketDisconnect:
        return
    raise AssertionError("WebSocketDisconnect must propagate so the session ends")
