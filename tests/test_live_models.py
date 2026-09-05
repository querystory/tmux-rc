"""Live Mode's model table: the server owns the list, the client only ever names a label.

Under test: table parsing and its no-config default, credential gating (a configured but
keyless entry is never offered), the rate card riding on the entry, and — the security
property shared with launchers — the WebSocket refusing any label the server did not
offer, so the client can never pick a model id or backend of its own."""

import json

import pytest
import starlette.websockets
from fastapi.testclient import TestClient

import daemon.live_providers as P
from daemon import server

TABLE = [
    {
        "label": "Gemini 2.5",
        "model": "gemini-live-2.5-flash-native-audio",
        "proactive_audio": True,
    },
    {
        "label": "Gemini 3.1",
        "model": "gemini-3.1-flash-live-preview",
        "backend": "gemini-api",
        "rates": {"audio_in": 4, "audio_out": 16},
    },
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
    assert (a.label, a.backend, a.flags) == (
        "Gemini 2.5",
        "vertex",
        {"proactive_audio": True},
    )
    # Missing rates fall back to 2.5's card per field — never a silent zero — except the
    # cached rates, which follow the entry's OWN uncached rate (no published discount).
    assert b.rates == (0.50, 2.00, 4.0, 16.0, 0.50, 4.0) and b.needs == ("GEMINI_API_KEY",)
    assert "AI Studio" in b.hint and "$4/$16" in b.hint
    p = tmp_path / "models.json"
    p.write_text(json.dumps(TABLE[:1]))
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", str(p))
    assert [m.label for m in P.models()] == ["Gemini 2.5"]
    for bad in (
        "nope",
        "[]",
        json.dumps([{"label": "x", "model": "y", "backend": "wat"}]),
        json.dumps([TABLE[0], "not an entry"]),  # all-or-nothing: one bad entry sinks the list
    ):
        monkeypatch.setenv("TMUXRC_LIVE_MODELS", bad)
        assert P.models() == P._DEFAULT


@pytest.mark.parametrize("field", ["label", "model"])
@pytest.mark.parametrize("invalid", ["", " \t\n", None, 42])
def test_table_rejects_empty_or_nonstring_identity(monkeypatch, field, invalid):
    entry = {**TABLE[0], field: invalid}
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps([entry]))
    assert P.models() == P._DEFAULT


def test_table_trims_identity_and_round_trips_offered_label(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps([{
        "label": "  Voice test \t", "model": " model-id \n",
    }]))
    offered = TestClient(server.app).get("/api/version").json()["live_models"]
    assert offered[0]["label"] == "Voice test"
    assert P.find(offered[0]["label"]).model == "model-id"


def test_table_rejects_duplicate_normalized_labels(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps([
        {"label": "Same", "model": "first"},
        {"label": " Same ", "model": "second"},
    ]))
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


def _refused(c, path):
    """Connect and return the 1008 close the route answers an unoffered model with."""
    with pytest.raises(starlette.websockets.WebSocketDisconnect) as ei, c.websocket_connect(path):
        raise AssertionError("an unoffered model must close the socket")
    assert ei.value.code == 1008
    return ei.value


def test_live_ws_refuses_unoffered_label(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = TestClient(server.app)
    for label in ("Gemini 3.1", "gemini-live-2.5-flash-native-audio", "OpenAI"):
        assert "reload" in _refused(c, f"/api/live-mode?model={label}").reason


def test_nothing_offered_hides_live_and_names_the_cause(monkeypatch):
    """Flag on but every entry key-gated and keyless: the button must not appear, and a
    probe is told it is a credential problem — "reload the page" would be a lie here."""
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE[1:]))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = TestClient(server.app)
    v = c.get("/api/version").json()
    assert (v["live_enabled"], v["live_models"]) == (False, [])
    assert "key" in _refused(c, "/api/live-mode").reason
