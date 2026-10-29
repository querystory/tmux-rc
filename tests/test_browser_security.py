"""In-memory admission checks: no network, browser, provider or real terminal."""

import asyncio

import pytest

from openbus.browser_security import BrowserOriginMiddleware


def request(monkeypatch, headers=(), *, kind="http", method="POST", configured=""):
    monkeypatch.setenv("TMUXRC_ALLOWED_ORIGINS", configured)
    monkeypatch.setenv("TMUXRC_PORT", "18030")
    calls, sent = [], []

    async def app(scope, receive, send):
        calls.append("dispatched")

    async def receive():
        raise AssertionError("Admission must not consume request bodies")

    async def send(message):
        sent.append(message)

    scope = {"type": kind, "method": method,
             "headers": [(key.encode(), value.encode()) for key, value in headers]}
    asyncio.run(BrowserOriginMiddleware(app)(scope, receive, send))
    return calls, sent


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("origin", ["https://other.example.com", "null", "",
    "http://localhost:18031", "https://localhost:18030", "http://localhost:18030/path",
    "http://localhost:18030?", "http://localhost:18030#", "http://localhost:bad",
    "http://user@localhost:18030", "http://localhost:0", " http://localhost:18030"])
def test_foreign_or_malformed_origins_never_dispatch(monkeypatch, kind, origin):
    calls, sent = request(monkeypatch, [("origin", origin)], kind=kind)
    assert not calls
    assert sent[0].get("status") == 403 if kind == "http" else sent[0]["code"] == 1008


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("origin", ["http://localhost:18030", "http://127.0.0.1:18030",
    "http://[::1]:18030", "https://console.example.com", "https://console.example.com:443"])
def test_local_and_configured_origins_dispatch(monkeypatch, kind, origin):
    calls, sent = request(monkeypatch, [("origin", origin)], kind=kind,
                          configured="https://console.example.com")
    assert calls == ["dispatched"] and not sent


def test_duplicate_origins_are_refused(monkeypatch):
    calls, sent = request(monkeypatch, [("origin", "http://localhost:18030")] * 2)
    assert not calls and sent[0]["status"] == 403


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_headerless_cli_still_works(monkeypatch, kind):
    assert request(monkeypatch, kind=kind)[0] == ["dispatched"]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_browser_metadata_without_origin_fails_closed(monkeypatch, site):
    calls, sent = request(monkeypatch, [("sec-fetch-site", site)])
    assert not calls and sent[0]["status"] == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_read_requests_are_unchanged(monkeypatch, method):
    assert request(monkeypatch, [("origin", "null")], method=method)[0] == ["dispatched"]


def test_broken_origin_configuration_fails_closed(monkeypatch):
    with pytest.raises(ValueError, match="origin"):
        request(monkeypatch, configured="https://console.example.com/path")


def test_server_installs_browser_policy():
    from openbus import server

    assert any(m.cls is BrowserOriginMiddleware for m in server.app.user_middleware)
