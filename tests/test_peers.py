"""The hub's view of other machines (openbus/peers.py): ids, routing, and following state."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from openbus import peers

STATE = {"version": 7, "panes": [{"pane_id": "%3", "session": "main", "tool": "claude"}]}


def fleet_with(handler):
    fleet = peers.Fleet(peers.parse("lab=http://lab.test:18030"))
    fleet.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fleet


def hub(fleet):
    app = FastAPI()
    app.state.fleet = fleet
    app.add_middleware(peers.PeerProxy)

    @app.post("/api/windows")
    async def local(request: Request):
        return {"local": await request.json()}

    @app.get("/api/launchers")
    def launchers():
        return {"local": True}
    return TestClient(app)


def test_parse():
    assert [(p.name, p.url) for p in peers.parse("a=http://x:1/, b-2=https://y")] == [
        ("a", "http://x:1"), ("b-2", "https://y")]
    for bad in ("a:b=http://x", "a=ftp://x", "a=http://x a=http://y"):
        with pytest.raises(ValueError, match="TMUXRC_PEERS"):
            peers.parse(bad)


def test_peer_panes_are_prefixed():
    fleet = fleet_with(lambda r: httpx.Response(500))
    fleet.peers["lab"].state = STATE
    assert fleet.panes() == [{"pane_id": "lab:%3", "session": "lab:main", "tool": "claude",
                              "machine": "lab"}]
    assert fleet.route("lab:%3")[1] == "%3"
    assert fleet.route("%3") is None
    assert fleet.route("other:%3") is None


def test_pane_path_goes_to_its_machine():
    seen = []

    def peer(request):
        seen.append(request)
        return httpx.Response(200, json={"pane_id": "%3", "ok": True})
    client = hub(fleet_with(peer))
    r = client.post("/api/panes/lab%3A%253/send?x=1", json={"text": "hi"},
                    headers={"origin": "http://localhost:18030"})
    assert r.json() == {"pane_id": "lab:%3", "ok": True}  # the reply names it as the phone does
    assert seen[0].url == "http://lab.test:18030/api/panes/%253/send?x=1"
    assert "origin" not in seen[0].headers
    assert json.loads(seen[0].content) == {"text": "hi"}


def test_machine_query_and_body_addresses():
    seen = []

    def peer(request):
        seen.append(request)
        return httpx.Response(200, json={"pane_id": "%9"})
    client = hub(fleet_with(peer))
    assert client.get("/api/launchers?machine=lab").json() == {"pane_id": "lab:%9"}
    assert str(seen[-1].url) == "http://lab.test:18030/api/launchers"
    client.post("/api/windows", json={"session": "lab:main", "launcher": "Claude"})
    assert json.loads(seen[-1].content) == {"session": "main", "launcher": "Claude"}
    # The hub's own: no prefix, and the body it read to look still reaches the route.
    assert client.post("/api/windows", json={"session": "main"}).json() == {
        "local": {"session": "main"}}
    assert client.get("/api/launchers").json() == {"local": True}
    assert len(seen) == 2


def test_unreachable_machine_is_a_502():
    def down(request):
        raise httpx.ConnectError("refused")
    r = hub(fleet_with(down)).get("/api/panes/lab%3A%253/live")
    assert r.status_code == 502


def test_follow_tracks_a_peer_coming_and_going(monkeypatch):
    monkeypatch.setattr(peers, "RETRY", 0.01)
    answers = [httpx.Response(200, json=STATE), httpx.Response(200, json=STATE)]

    def peer(request):
        if answers:
            return answers.pop(0)
        raise httpx.ConnectError("refused")

    async def go():
        fleet = fleet_with(peer)
        task = asyncio.create_task(fleet.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if fleet.peers["lab"].error:
                break
        task.cancel()
        return fleet
    fleet = asyncio.run(go())
    lab = fleet.peers["lab"]
    assert lab.state is None and lab.error
    assert fleet.seq == 2  # it arrived once (the unchanged second answer is not news), then left
    assert fleet.machines("hub")[1] == {"name": "lab", "online": False, "error": lab.error}
