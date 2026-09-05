"""Client-side error reporting (issue #57): /api/client-error accepts a report and
forwards it to OTel via telemetry.emit_client_error. Fail-closed content policy — the
free-text message rides ONLY under TMUXRC_QSDEBUG; structural fields always. Oversized
bodies are rejected before parsing so a hostile report can't balloon daemon memory."""

from fastapi.testclient import TestClient

from openbus import server, telemetry


def _capture(monkeypatch):
    """Capture what emit_client_error hands to _emit_record, without the OTel SDK."""
    seen = {}
    monkeypatch.setattr(
        telemetry,
        "_emit_record",
        lambda body, attrs, scope=telemetry._SCOPE: seen.update(
            attrs=attrs, scope=scope
        ),
    )
    return seen


def test_valid_report_forwards_structural_fields(monkeypatch):
    seen = _capture(monkeypatch)
    c = TestClient(server.app)
    r = c.post(
        "/api/client-error",
        json={
            "kind": "mic",
            "name": "NotAllowedError",
            "session": "s1",
            "message": "Permission denied",
        },
        headers={"user-agent": "Mozilla/5.0 (Linux; Android 14) Chrome"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    a = seen["attrs"]
    assert seen["scope"] == telemetry._CLIENT_SCOPE  # own scope, not parse/live
    assert a["kind"] == "mic" and a["name"] == "NotAllowedError"
    assert a["session"] == "s1"
    assert a["ua_class"] == "android"  # derived server-side from the UA


def test_message_dropped_without_qsdebug(monkeypatch):
    monkeypatch.setattr(telemetry, "_QSDEBUG", False)
    seen = _capture(monkeypatch)
    telemetry.emit_client_error(
        kind="poll",
        name="TypeError",
        endpoint="/api/state",
        ua_class="ios",
        session="s",
        actor=None,
        message="secret in the URL",
    )
    a = seen["attrs"]
    assert "message" not in a  # free-text is fail-closed by default
    assert a["name"] == "TypeError" and a["endpoint"] == "/api/state"  # structure stays


def test_message_included_with_qsdebug(monkeypatch):
    monkeypatch.setattr(telemetry, "_QSDEBUG", True)
    seen = _capture(monkeypatch)
    telemetry.emit_client_error(
        kind="poll",
        name=None,
        endpoint=None,
        ua_class=None,
        session=None,
        actor=None,
        message="the real error text",
    )
    assert seen["attrs"]["message"] == "the real error text"


def test_oversized_body_rejected(monkeypatch):
    _capture(monkeypatch)
    c = TestClient(server.app)
    big = "x" * (server.CLIENT_ERROR_MAX_BYTES + 1)
    r = c.post("/api/client-error", json={"kind": "poll", "message": big})
    assert r.status_code == 413


def test_kind_is_capped(monkeypatch):
    # kind is client-controlled — capped like every other field so a crafted value can't
    # bloat the attribute set even under the endpoint's overall body cap.
    seen = _capture(monkeypatch)
    telemetry.emit_client_error(
        kind="k" * 500,
        name=None,
        endpoint=None,
        ua_class=None,
        session=None,
        actor=None,
        message=None,
    )
    assert len(seen["attrs"]["kind"]) == 64


def test_untrusted_ua_class_ignores_client_claim(monkeypatch):
    # ua_class comes from the request's real UA, never a client-supplied field — a
    # crafted body value must not appear in telemetry.
    seen = _capture(monkeypatch)
    c = TestClient(server.app)
    c.post(
        "/api/client-error",
        json={"kind": "onerror", "ua_class": "spoofed"},
        headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17)"},
    )
    assert seen["attrs"]["ua_class"] == "ios"
