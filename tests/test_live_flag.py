"""Live Mode ships behind TMUXRC_LIVE_MODE (off by default): the /api/live-mode WS is
refused when off, and /api/version advertises the flag so the client hides the mic
button. The classify/marking improvements Live rode in with are NOT gated — they help
the phone cards regardless."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from openbus import live, server


def test_enabled_off_by_default_and_truthy_values(monkeypatch):
    monkeypatch.delenv("TMUXRC_LIVE_MODE", raising=False)
    assert live.enabled() is False
    for v in ("1", "true", "On", "YES"):
        monkeypatch.setenv("TMUXRC_LIVE_MODE", v)
        assert live.enabled() is True
    for v in ("0", "false", "", "nope"):
        monkeypatch.setenv("TMUXRC_LIVE_MODE", v)
        assert live.enabled() is False


def test_version_reports_flag(monkeypatch):
    c = TestClient(server.app)
    monkeypatch.delenv("TMUXRC_LIVE_MODE", raising=False)
    assert c.get("/api/version").json()["live_enabled"] is False
    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    assert c.get("/api/version").json()["live_enabled"] is True


def test_live_ws_refused_when_disabled(monkeypatch):
    import starlette.websockets

    monkeypatch.delenv("TMUXRC_LIVE_MODE", raising=False)
    c = TestClient(server.app)
    # Disabled: the server closes with policy-violation 1008 before accepting a session,
    # so the connect itself raises rather than yielding a usable socket.
    try:
        with c.websocket_connect("/api/live-mode"):
            raise AssertionError("expected the disabled route to close the socket")
    except starlette.websockets.WebSocketDisconnect as e:
        assert e.code == 1008


def test_version_reports_docs_only_when_served():
    """The Docs link shows on this flag, so it must agree with whether /docs/ answers."""
    c = TestClient(server.app)
    assert c.get("/api/version").json()["docs"] is (c.get("/docs/").status_code == 200)


@pytest.mark.parametrize("built", [False, True])
def test_docs_mounted_needs_index(tmp_path, built):
    """An empty build dir must not count: it would mount yet 404 at /docs/. The mount is
    decided at import, so check it in a fresh interpreter with TMUXRC_DOCS_DIR set."""
    if built:
        (tmp_path / "index.html").write_text("docs")
    probe = "from openbus import server; print(server.DOCS_MOUNTED)"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         env={**os.environ, "TMUXRC_DOCS_DIR": str(tmp_path)}).stdout
    assert out.strip() == str(built)
