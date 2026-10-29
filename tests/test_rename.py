"""Rename (POST /api/panes/{id}/rename): the user's own name for a pane beats the title its
agent sets, lives in tmux, and gives way again when cleared; the tmux session renames too.

Runs a REAL tmux on a private socket (TMUXRC_TMUX_SOCKET), never the developer's server.
Cleanup kills that socket's panes and nothing else."""

import shutil
import subprocess
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import openbus.server as S
from openbus import tmux as T

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
TMUX_3_4 = bool(shutil.which("tmux")) and subprocess.run(
    ["tmux", "-V"], capture_output=True, text=True).stdout.strip() == "tmux 3.4"


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
    woken = []  # a rename only wakes the watcher: names aren't on screen, nothing to re-parse
    monkeypatch.setattr(S.app.state, "watcher", SimpleNamespace(wake=lambda: woken.append("tick")),
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
    assert r.json() == {"ok": True} and client.woken == ["tick"]
    assert listed(pane).display_title == "Ship the fix"
    client.post(f"/api/panes/{pane}/rename", json={"name": "", "birth": birth})
    assert listed(pane).display_title == "Agent's own title"


def test_refuses_a_taken_session_and_a_stale_menu(pane, client):
    assert client.post(f"/api/panes/{pane}/rename", json={"session": "taken"}).status_code == 409
    stale = {"name": "x", "birth": "1"}
    assert client.post(f"/api/panes/{pane}/rename", json=stale).status_code == 409
    moved = {"session": "new", "was": "gtm-0"}  # a group's other member is attached now
    assert client.post(f"/api/panes/{pane}/rename", json=moved).status_code == 409
    assert client.post("/api/panes/%25999/rename", json={"name": "x"}).status_code == 404
    assert not T.rename(replace(listed(pane), pid="1"), "new", "x")  # checked as it renames
    assert not T.rename(replace(listed(pane), session="taken"), "new", "x")  # another pane
    assert listed(pane).session == "old" and listed(pane).name == ""


# (field, sent, kept; None = refused). A session is an allowlist: tmux parses it in every target
# built from it. A pane name is free text but one listing row: no control characters or line
# separators, which would split the row (tab-joined fields, splitlines).
NAMES = [
    ("session", "new", "new"), ("session", " Release 2 ", "Release 2"),
    ("session", "a_b-c", "a_b-c"),
    ("session", "x" * 64, "x" * 64), ("session", "x" * 65, None), ("session", " ", None),
    *(("session", bad, None) for bad in ("a:b", "a.b", "a;", "=taken", "-archive", "_a", "café",
                                         "a\tb", "a\x00b", "a\u2028b")),
    ("name", "wip;", "wip;"), ("name", "-x", "-x"), ("name", "Ship: the fix.", "Ship: the fix."),
    ("name", "#{pane_id} $ ~ 'a' \"b\" \\;", "#{pane_id} $ ~ 'a' \"b\" \\;"),
    # tmux 3.4 itself reads back "$HOME" in any pane option or title as "\$HOME".
    pytest.param("name", "a $HOME", "a $HOME", marks=pytest.mark.xfail(
        TMUX_3_4, reason="tmux 3.4 escapes $ in what it reads back", strict=True)),
    *(("name", f"a{c}b", None) for c in ("\t", "\x85", "\u2028", "\u2029", "\x00", "\x1b")),
]


@pytest.mark.parametrize(("field", "sent", "kept"), NAMES)
def test_accepted_and_refused_names(pane, client, field, sent, kept):
    r = client.post(f"/api/panes/{pane}/rename", json={field: sent})
    assert r.status_code == (200 if kept else 422)
    assert getattr(listed(pane), field) == (kept or {"session": "old", "name": ""}[field])


def test_a_session_with_its_own_spaces_still_renames(pane, client):
    T._run(["rename-session", "-t", "=old", " old "])  # made outside tmux-rc
    for other in ("old", " "):  # another session's name, not this one's
        r = client.post(f"/api/panes/{pane}/rename", json={"session": "new", "was": other})
        assert r.status_code == 409
    r = client.post(f"/api/panes/{pane}/rename", json={"session": "new", "was": " old "})
    assert r.status_code == 200 and listed(pane).session == "new"


def test_a_grouped_pane_renames_the_member_it_is_listed_under(pane, client):
    T._run(["new-session", "-d", "-s", "grp", "-t", "old"])
    shown = listed(pane)
    r = client.post(f"/api/panes/{pane}/rename", json={"name": "Mine", "birth": shown.pid})
    assert r.status_code == 200 and listed(pane).name == "Mine"
    body = {"session": "new", "birth": shown.pid, "was": shown.session}
    assert client.post(f"/api/panes/{pane}/rename", json=body).status_code == 200
    left = {"old", "grp"} - {shown.session}
    sessions = T._run(["list-sessions", "-F", "#{session_name}"]).split()
    assert set(sessions) == {"new", "taken", *left}


def test_a_taken_session_saves_neither_name(pane, client):
    r = client.post(f"/api/panes/{pane}/rename", json={"name": "Mine", "session": "taken"})
    assert r.status_code == 409 and client.woken == []
    assert listed(pane).name == "" and listed(pane).session == "old"


def test_a_single_pane_watch_by_session_name_keeps_its_session(pane, client, monkeypatch):
    for target in ("old:0", "atlas:0"):  # a session name, or a label derived from the cwd
        monkeypatch.setattr(S.app.state.watcher, "target", target, raising=False)
        assert client.post(f"/api/panes/{pane}/rename", json={"session": "new"}).status_code == 409
        assert listed(pane).session == "old"
    monkeypatch.setattr(S.app.state.watcher, "target", pane)  # by id: it survives the rename
    assert client.post(f"/api/panes/{pane}/rename", json={"session": "new"}).status_code == 200


def test_a_name_alone_changes_the_deck():
    from openbus.watcher import Watcher
    named = {"pane_id": "%1", "title": "Same", "name": "Same"}
    assert Watcher._deck_fp([named]) != Watcher._deck_fp([{**named, "name": ""}])
