"""X-Tunnel-User trust: believed only from loopback and, once TMUXRC_TUNNEL_SECRET_FILE is
set, only with a matching X-Tunnel-Secret — so a local process (an agent in a pane) can't
act or be billed as the tunnel owner by setting one header. Unset keeps the legacy model."""

import logging
import os

import pytest
from starlette.requests import Request

from openbus import server, telemetry

OWNER = "owner@example.com"


def _req(peer="127.0.0.1", **headers):
    raw = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": (peer, 5000)})


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(telemetry, "TUNNEL_SECRET", b"s3cret")


def test_legacy_mode_trusts_any_loopback_claim(monkeypatch):
    monkeypatch.setattr(telemetry, "TUNNEL_SECRET", None)  # env var unset: the default
    assert telemetry.tunnel_user(_req(x_tunnel_user=OWNER)) == OWNER
    assert telemetry.tunnel_user(_req("10.0.0.5", x_tunnel_user=OWNER)) is None


@pytest.mark.usefixtures("secret")
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x_tunnel_user": OWNER, "x_tunnel_secret": "s3cret"}, OWNER),
        ({"x_tunnel_user": OWNER}, None),
        ({"x_tunnel_user": OWNER, "x_tunnel_secret": "wrong"}, None),
        ({"x_tunnel_user": OWNER, "x_tunnel_secret": ""}, None),
        ({"x_tunnel_secret": "s3cret"}, None),
    ],
)
def test_secret_required_when_configured(headers, expected):
    assert telemetry.tunnel_user(_req(**headers)) == expected


@pytest.mark.usefixtures("secret")
def test_valid_secret_from_lan_peer_still_untrusted():
    lan = _req("10.0.0.5", x_tunnel_user=OWNER, x_tunnel_secret="s3cret")
    assert telemetry.tunnel_user(lan) is None


@pytest.mark.usefixtures("secret")
def test_compare_is_constant_time(monkeypatch):
    calls = []

    def spy(a, b):
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(telemetry.hmac, "compare_digest", spy)
    telemetry.tunnel_user(_req(x_tunnel_user=OWNER, x_tunnel_secret="guess"))
    assert calls == [(b"guess", b"s3cret")]


@pytest.mark.usefixtures("secret")
def test_audit_records_spoof_attempt(caplog, monkeypatch):
    monkeypatch.setattr(telemetry, "emit_action", lambda **_: None)
    with caplog.at_level(logging.INFO, logger="openbus.server.audit"):
        server._audit(_req(x_tunnel_user=OWNER, x_tunnel_secret="nope"), "send", "%1")
        server._audit(_req(x_tunnel_user=OWNER, x_tunnel_secret="s3cret"), "send", "%1")
    spoof, real = (r.getMessage() for r in caplog.records)
    assert f"by local:127.0.0.1 claiming {OWNER!r} without a valid tunnel secret" in spoof
    assert real.endswith(f"by {OWNER}")


def test_secret_file_created_private_then_reused(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "tunnel-secret"
    monkeypatch.setenv("TMUXRC_TUNNEL_SECRET_FILE", str(path))
    first = telemetry._load_tunnel_secret()
    assert len(first) >= 32 and os.stat(path).st_mode & 0o777 == 0o600
    assert telemetry._load_tunnel_secret() == first


def test_empty_secret_file_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "tunnel-secret"
    path.write_text("\n")
    monkeypatch.setenv("TMUXRC_TUNNEL_SECRET_FILE", str(path))
    with pytest.raises(ValueError, match="empty"):
        telemetry._load_tunnel_secret()
