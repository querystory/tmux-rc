"""POST /api/sessions: start a tmux session from the phone, including when no tmux
server is running at all (the host just rebooted).

Runs a REAL tmux on a private socket (TMUXRC_TMUX_SOCKET), never the developer's server:
the endpoint creates sessions, and the test that proves "no server -> one gets started"
needs a socket nobody else is using. Cleanup kills that socket's server and nothing else.
"""

import json
import os
import shutil
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient

import openbus.server as S
import openbus.tmux as T

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")


@pytest.fixture
def sock(monkeypatch):
    name = f"newsess-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TMUXRC_TMUX_SOCKET", name)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)  # no systemd scope in tests
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps([
        {"label": "Sleeper", "command": "sleep 30"},
        {"label": "Ghost", "command": "no-such-cmd-x"},
    ]))
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def audits(monkeypatch):
    lines = []
    monkeypatch.setattr(S, "_audit", lambda _r, action, pane, detail="", keys=None,
                        outcome="ok": lines.append((action, pane, outcome)))
    return lines


def _fmt(pane, fmt):
    return T._run(["display-message", "-p", "-t", pane, fmt]).strip()


def test_starts_the_server_when_none_is_running(sock, audits, tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/opt/venv")
    monkeypatch.setenv("PATH", "/opt/venv/bin:" + os.environ["PATH"])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    assert not T.server_running()
    r = TestClient(S.app).post("/api/sessions", json={"name": "work", "cwd": str(tmp_path)})
    assert r.status_code == 200, r.text
    pane = r.json()["pane_id"]
    assert _fmt(pane, "#{session_name} #{pane_current_path}") == f"work {tmp_path}"
    # The server it started carries a user's environment, not the daemon's.
    env = T._run(["show-environment", "-g"])
    assert "OPENAI_API_KEY" not in env and "VIRTUAL_ENV" not in env and "/opt/venv/bin" not in env
    assert audits == [("new_session", pane, "ok")]


def test_existing_name_is_a_conflict(sock, audits, tmp_path):
    client, body = TestClient(S.app), {"name": "dup", "cwd": str(tmp_path)}
    assert client.post("/api/sessions", json=body).status_code == 200
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    assert audits[-1][2].startswith("rejected")


def test_launcher_runs_in_the_first_window(sock, audits, tmp_path):
    r = TestClient(S.app).post("/api/sessions",
                               json={"name": "agent", "cwd": str(tmp_path), "launcher": "Sleeper"})
    assert r.status_code == 200, r.text
    pane = r.json()["pane_id"]
    assert _fmt(pane, "#{window_name} #{pane_start_command}").startswith("Sleeper ")
    assert "sleep 30" in _fmt(pane, "#{pane_start_command}")


@pytest.mark.parametrize(("body", "status"), [
    ({"name": "x", "cwd": "/no/such/dir"}, 422),
    ({"name": "x", "cwd": "relative"}, 422),
    ({"name": "x", "launcher": "rm -rf /"}, 404),
    ({"name": "x", "launcher": "Ghost"}, 400),
    ({"name": "a:b"}, 422),
    ({"name": "a.b"}, 422),
    ({"name": ""}, 422),
])
def test_refusals_never_touch_tmux(sock, audits, body, status):
    r = TestClient(S.app).post("/api/sessions", json=body)
    assert r.status_code == status, r.text
    assert not T.server_running()
    if "name" not in r.text:  # schema failures never reach the handler
        assert audits and audits[-1][2].startswith("rejected")
