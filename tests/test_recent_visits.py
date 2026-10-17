"""The ☰ drawer's Recent: what the user opened, kept in the history database
(History.visit), and GET /api/recent, which answers with what of it is still there."""

import os
from types import SimpleNamespace

from fastapi.testclient import TestClient

from openbus import history, scratch, server
from openbus.history import History


def test_a_revisit_moves_up_and_only_the_newest_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "VISITS", 3)
    h = History(tmp_path / "h.db")
    for at, key in enumerate(["a", "b", "a", "c", "d"]):
        h.visit("pane", key, at)
    h.visit("scratch", "mock", 1.5)
    # a, revisited, is one row at its later time; b, the oldest of four, is pruned
    assert h.visits("pane") == [("d", 4), ("c", 3), ("a", 2)]
    assert h.visits("scratch") == []  # older than the newest three, of either kind
    assert History(tmp_path / "h.db").visits("pane")[0] == ("d", 4)  # it survives a restart


def test_opened_skips_folders_gone_and_takes_titles_as_the_gallery_does(tmp_path):
    for name, title in [("one", "First"), ("two", "Second")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "index.html").write_text(f"<title>{title}</title>")
    (tmp_path / "loose.html").write_text("a file, not a folder")
    os.symlink(tmp_path / "one", tmp_path / "link")
    visits = [("gone", 9), ("loose.html", 8), ("link", 7), ("..", 6), ("two", 5), ("one", 4)]
    assert scratch.opened(tmp_path, visits, 5) == [
        {"title": "Second", "href": "two/", "at": 5}, {"title": "First", "href": "one/", "at": 4}]
    assert len(scratch.opened(tmp_path, visits, 1)) == 1


def test_opening_a_pane_is_a_visit_and_recent_lists_only_live_ones(tmp_path, monkeypatch):
    births = {"%1": "100", "%2": "200", "%3": "300"}
    watcher = SimpleNamespace(states=[{"pane_id": p} for p in ("%1", "%2")],
                              pane_birth=births.get)
    h = History(tmp_path / "h.db")
    monkeypatch.setattr(server.app.state, "watcher", watcher, raising=False)
    monkeypatch.setattr(server.app.state, "history", h, raising=False)
    monkeypatch.setattr(server.tmux, "find_pane", lambda _id: object())
    monkeypatch.setattr(server.tmux, "select_pane", lambda _id: None)
    monkeypatch.setattr(server, "_audit", lambda *_a, **_k: None)
    (tmp_path / "s" / "mock").mkdir(parents=True)
    (tmp_path / "s" / "mock" / "index.html").write_text("<title>A mock</title>")
    monkeypatch.setattr(server, "_scratch_dir", str(tmp_path / "s"))
    client = TestClient(server.app)
    for pane in ("%1", "%3", "%2", "%1"):  # %3 closes after; %1 again is no second row
        assert client.post(f"/api/panes/{pane}/select").status_code == 200
    h.visit("pane", "%2:old", 1e12)  # a recycled id: some other pane than %2 now
    h.visit("scratch", "mock")
    body = client.get("/api/recent").json()
    assert [p["pane_id"] for p in body["panes"]] == ["%1", "%2"]
    at = h.visits("scratch")[0][1]
    assert body["scratch"] == [{"title": "A mock", "href": "mock/", "at": at}]
    assert client.get("/api/recent?n=1").json()["panes"] == body["panes"][:1]
    assert client.get("/api/recent?n=0").status_code == 422
    monkeypatch.setattr(server.app.state, "history", None)  # no database: nothing opened yet
    assert client.get("/api/recent").json() == {"panes": [], "scratch": []}
