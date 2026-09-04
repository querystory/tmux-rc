"""Live Mode's model table: the server owns the list, the client only ever names a label.

Under test: table parsing and its no-config default, credential gating (a configured but
keyless entry is never offered), the rate card riding on the entry, and — the security
property shared with launchers — the WebSocket refusing any label the server did not
offer, so the client can never pick a model id or backend of its own."""

import json

import starlette.websockets
from fastapi.testclient import TestClient

import daemon.live_providers as P
from daemon import server

TABLE = [
    {"label": "Gemini 2.5", "model": "gemini-live-2.5-flash-native-audio", "proactive_audio": True},
    {"label": "Gemini 3.1", "model": "gemini-3.1-flash-live-preview", "backend": "gemini-api",
     "rates": {"audio_in": 4, "audio_out": 16}},
]


def test_default_table_is_the_pre_table_behaviour(monkeypatch):
    monkeypatch.delenv("TMUXRC_LIVE_MODELS", raising=False)
    (m,) = P.models()
    assert (m.model, m.backend) == ("gemini-live-2.5-flash-native-audio", "vertex")
    assert m.flags == {"proactive_audio": True} and m.rates == P._RATES_25
    assert m.available()  # Vertex gates on nothing — creds resolve at call time


def test_table_parses_inline_or_path_and_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    a, b = P.models()
    assert (a.label, a.backend, a.flags) == ("Gemini 2.5", "vertex", {"proactive_audio": True})
    # Missing rates fall back to 2.5's card per field — never a silent zero.
    assert b.rates == (0.50, 2.00, 4.0, 16.0) and b.key_env == "GEMINI_API_KEY"
    assert "AI Studio" in b.hint and "$4/$16" in b.hint
    p = tmp_path / "models.json"
    p.write_text(json.dumps(TABLE[:1]))
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", str(p))
    assert [m.label for m in P.models()] == ["Gemini 2.5"]
    for bad in ("nope", "[]", json.dumps([{"label": "x", "model": "y", "backend": "wat"}])):
        monkeypatch.setenv("TMUXRC_LIVE_MODELS", bad)
        assert P.models() == P._DEFAULT


def test_keyless_entry_is_configured_but_not_offered(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert [m.label for m in P.available()] == ["Gemini 2.5"]
    assert P.find(None).label == "Gemini 2.5"  # no label → the first offered
    assert P.find("Gemini 3.1") is None  # configured, keyless: refused, not defaulted
    assert P.find("rm -rf /") is None
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert P.find("Gemini 3.1").backend == "gemini-api"


def test_version_lists_offered_labels_with_hints(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    got = TestClient(server.app).get("/api/version").json()["live_models"]
    assert got == [{"label": "Gemini 2.5", "hint": "Vertex · $3/$12 per 1M audio"}]


def test_live_ws_refuses_unoffered_label(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = TestClient(server.app)
    for label in ("Gemini 3.1", "gemini-live-2.5-flash-native-audio", "OpenAI"):
        try:
            with c.websocket_connect(f"/api/live-mode?model={label}"):
                raise AssertionError("an unoffered label must close the socket")
        except starlette.websockets.WebSocketDisconnect as e:
            assert e.code == 1008
