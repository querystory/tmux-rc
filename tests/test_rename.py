"""Rename (POST /api/panes/{id}/rename): the user's own name for a pane beats the title its
agent sets, lives in tmux, and gives way again when cleared; the tmux session renames too.

Runs a REAL tmux on a private socket (TMUXRC_TMUX_SOCKET), never the developer's server.
Cleanup kills that socket's panes and nothing else."""

import shutil
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import openbus.server as S
from openbus import tmux as T

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")


@pytest.fixture
def pane(monkeypatch):
    name = f"rename-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TMUXRC_TMUX_SOCKET", name)
    monkeypatch.delenv("TMUX", raising=False)
    pane = T._run(["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "old", "sleep 60"]).strip()
    T._run(["new-session", "-d", "-s", "taken", "sleep 60"])
    T._run(["select-pane", "-t", pane, "-T", "Agent's own title"])
    yield pane
    for p in T._run(["list-panes", "-a", "-F", "#{pane_id}"]).split():  # no kill-server
        subprocess.run(["tmux", "-L", name, "kill-pane", "-t", p], check=False)


@pytest.fixture
def client(monkeypatch):
    woken = []
    monkeypatch.setattr(S.app.state, "watcher", SimpleNamespace(request_reparse=woken.append),
                        raising=False)
    monkeypatch.setattr(S, "_audit", lambda *a, **k: None)
    c = TestClient(S.app)
    c.woken = woken
    return c


def listed(pane):
    return next(p for p in T.list_panes() if p.id == pane)


def test_a_name_beats_the_agents_title_until_cleared(pane, client):
    birth = listed(pane).pid
    r = client.post(f"/api/panes/{pane}/rename", json={"name": "  Ship the fix ", "birth": birth})
    assert r.json() == {"ok": True} and client.woken == [pane]
    assert listed(pane).display_title == "Ship the fix"
    client.post(f"/api/panes/{pane}/rename", json={"name": "", "birth": birth})
    assert listed(pane).display_title == "Agent's own title"


def test_renames_the_session(pane, client):
    assert client.post(f"/api/panes/{pane}/rename", json={"session": "new"}).status_code == 200
    assert listed(pane).session == "new"


def test_refuses_a_taken_session_a_stale_menu_and_unsafe_names(pane, client):
    assert client.post(f"/api/panes/{pane}/rename", json={"session": "taken"}).status_code == 409
    stale = {"name": "x", "birth": "1"}
    assert client.post(f"/api/panes/{pane}/rename", json=stale).status_code == 409
    assert client.post("/api/panes/%25999/rename", json={"name": "x"}).status_code == 404
    for bad in ({"session": "a:b"}, {"session": "a.b"}, {"session": " "}, {"name": "a\tb"}):
        assert client.post(f"/api/panes/{pane}/rename", json=bad).status_code == 422
    assert listed(pane).session == "old" and listed(pane).name == ""
