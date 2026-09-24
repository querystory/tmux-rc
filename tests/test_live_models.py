"""Live Mode's model table: the server owns the list, the client only ever names a label.

Under test: table parsing and its no-config default, credential gating (a configured but
keyless entry is never offered), the rate card riding on the entry, and — the security
property shared with launchers — the WebSocket refusing any label the server did not
offer, so the client can never pick a model id or backend of its own."""

import json

import pytest
import starlette.websockets
from fastapi.testclient import TestClient

import openbus.live_providers as P
from openbus import live as L
from openbus import server

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
    assert m.available()  # Vertex gates on the project alone — creds resolve at call time
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    assert not m.available()  # no project: connect() could only fail, so never offered


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
    assert L.pick(offered[0]["label"]).model == "model-id"


def test_table_rejects_duplicate_normalized_labels(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps([
        {"label": "Same", "model": "first"},
        {"label": " Same ", "model": "second"},
    ]))
    assert P.models() == P._DEFAULT


def test_keyless_entry_is_configured_but_not_offered(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # GPT-Live joins the menu, not the table
    assert [m.label for m in P.available()] == ["Gemini 2.5"]
    assert L.pick(None).label == "Gemini 2.5"  # no label → the first offered
    assert L.pick("Gemini 3.1") is None  # configured, keyless: refused, not defaulted
    assert L.pick("rm -rf /") is None
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert L.pick("Gemini 3.1").backend == "gemini-api"


def test_version_lists_offered_labels_with_hints(monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # GPT-Live is appended from outside the table when its key is set (it is an adapter,
    # not a table entry) — keep it out so this covers the TABLE's own contribution.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    got = TestClient(server.app).get("/api/version").json()["live_models"]
    assert got == [{"label": "Gemini 2.5", "hint": "Vertex · $3/$12 per 1M audio"}]


def test_gpt_live_joins_the_menu_on_its_key_alone(monkeypatch):
    """GPT-Live owns a whole session rather than a connection the seam can open, so it is
    not in TMUXRC_LIVE_MODELS at all. It still has to reach the same menu, under the same
    "only if its key is set" rule the table entries follow — /api/version is the one list
    the client picks from, so anything selectable has to appear in it, or the socket would
    refuse a pick the menu itself offered."""
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(TABLE))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    without = TestClient(server.app).get("/api/version").json()["live_models"]
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    with_key = TestClient(server.app).get("/api/version").json()["live_models"]
    assert [m["label"] for m in with_key] == [m["label"] for m in without] + ["GPT-Live 1"]
    # Its own hint, not a rate card: GPT-Live bills voice by the MINUTE, so rendering the
    # per-1M-audio line every table entry gets would put a number on the picker that
    # describes nothing the user will be charged.
    assert with_key[-1]["hint"] == "OpenAI · $0.05/min + backend"


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_table_rejects_rates_that_cannot_be_money(monkeypatch, bad):
    """A rate card is multiplied by token counts and summed into the status-bar total, so a
    negative one SUBTRACTS from the session cost and a NaN poisons it — and the picker
    advertises the same number. All-or-nothing, like every other malformed field: the
    operator gets the defaults and a warning, never a silently corrected price."""
    monkeypatch.setenv(
        "TMUXRC_LIVE_MODELS", json.dumps([{**TABLE[0], "rates": {"audio_out": bad}}])
    )
    assert P.models() == P._DEFAULT


def test_a_configured_entry_may_claim_gpt_lives_label_and_wins_it(monkeypatch):
    """GPT-Live is appended to the menu, not configured into the table, so nothing stops an
    operator from naming a table entry after it. One menu and one resolver is what keeps
    that from splitting: the row the picker shows and the model the socket opens are read
    off the same list, so the configured entry wins BOTH — rather than the menu showing two
    rows while the gate sent them both to the adapter, leaving the configured one
    unreachable."""
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps([{
        "label": "GPT-Live 1", "model": "gemini-live-2.5-flash-native-audio",
    }]))
    labels = [m["label"] for m in TestClient(server.app).get("/api/version").json()["live_models"]]
    assert labels == ["GPT-Live 1"]
    assert L.pick("GPT-Live 1").model == "gemini-live-2.5-flash-native-audio"


def test_a_keyless_entry_still_owns_its_label_against_gpt_live(monkeypatch):
    """The reservation holds while the configured entry is KEYLESS and therefore off the
    menu. A label belongs to whoever configured it, not to whoever currently has
    credentials — otherwise the remembered pick "GPT-Live 1" would answer as the adapter
    today and as the operator's own model the moment their key landed, which is the one
    thing label-only selection exists to prevent. Unoffered means refused, not reassigned."""
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # the operator's entry has no key
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps([
        {"label": "Gemini 2.5", "model": "gemini-live-2.5-flash-native-audio"},
        {"label": "GPT-Live 1", "model": "gemini-3.1-flash-live-preview",
         "backend": "gemini-api"},
    ]))
    labels = [m["label"] for m in TestClient(server.app).get("/api/version").json()["live_models"]]
    assert labels == ["Gemini 2.5"]  # neither the keyless entry nor the adapter squatting it
    assert L.pick("GPT-Live 1") is None
    # The key lands: the label resolves to the model the operator named, never the adapter.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert L.pick("GPT-Live 1").model == "gemini-3.1-flash-live-preview"


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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # GPT-Live is key-gated the same way
    c = TestClient(server.app)
    v = c.get("/api/version").json()
    assert (v["live_enabled"], v["live_models"]) == (False, [])
    assert "key" in _refused(c, "/api/live-mode").reason
