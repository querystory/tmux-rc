"""In-memory Host admission, without making requests to a running daemon."""

import asyncio

import pytest

from openbus.host_security import TrustedHostMiddleware


def request(monkeypatch, hosts=(), *, kind="http", configured="", bind="127.0.0.1",
            extra=()):
    monkeypatch.setenv("TMUXRC_ALLOWED_HOSTS", configured)
    monkeypatch.setenv("TMUXRC_HOST", bind)
    calls, sent = [], []

    async def app(scope, receive, send):
        calls.append("dispatched")

    async def receive():
        raise AssertionError("Admission must not consume a request body")

    async def send(message):
        sent.append(message)

    headers = [("host", host) for host in hosts] + list(extra)
    scope = {"type": kind, "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(TrustedHostMiddleware(app)(scope, receive, send))
    return calls, sent


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:18030", "127.0.0.1:18030",
    "[::1]:18030", "console.example.com", "console.example.com:443"])
def test_local_and_configured_hosts_dispatch(monkeypatch, kind, host):
    calls, sent = request(monkeypatch, [host], kind=kind, configured="console.example.com")
    assert calls == ["dispatched"] and not sent


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("hosts", [[], [""], ["other.example.com"],
    ["console.example.com.other.example.com"], ["localhost", "other.example.com"],
    ["localhost:bad"], ["localhost:0"], ["localhost:70000"], ["localhost/path"],
    ["user@localhost"], ["localhost?"], ["localhost#"], [" localhost"], ["::1"],
    ["[::1"], ["*.example.com"]])
def test_unknown_missing_and_malformed_hosts_refused(monkeypatch, kind, hosts):
    calls, sent = request(monkeypatch, hosts, kind=kind, configured="console.example.com")
    assert not calls
    assert sent[0].get("status") == 400 if kind == "http" else sent[0]["code"] == 1008


def test_forwarded_host_cannot_authorize_an_unknown_host(monkeypatch):
    calls, sent = request(monkeypatch, ["other.example.com"],
                          extra=[("x-forwarded-host", "localhost"),
                                 ("forwarded", "host=localhost")])
    assert not calls and sent[0]["status"] == 400


@pytest.mark.parametrize("bind", ["192.0.2.10", "dev.example.com", "::1"])
def test_explicit_bind_is_allowed(monkeypatch, bind):
    host = f"[{bind}]" if ":" in bind else bind
    assert request(monkeypatch, [host], bind=bind)[0] == ["dispatched"]


@pytest.mark.parametrize("bind", ["0.0.0.0", "::"])
def test_wildcard_bind_does_not_allow_arbitrary_hosts(monkeypatch, bind):
    assert not request(monkeypatch, ["other.example.com"], bind=bind)[0]


def test_wildcard_configuration_fails_closed(monkeypatch):
    with pytest.raises(ValueError):
        request(monkeypatch, configured="*.example.com")


def test_server_installs_host_policy():
    from openbus import server

    assert any(m.cls is TrustedHostMiddleware for m in server.app.user_middleware)
