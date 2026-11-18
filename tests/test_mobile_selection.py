"""Mobile navigation must not change any desktop session's selected window."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from openbus import server


def test_selection_validates_pane_without_mutating_tmux(monkeypatch):
    # The same pane may appear in several grouped desktop views. Looking it up
    # is enough: issuing any tmux selection would pick an ambiguous desktop view.
    monkeypatch.setattr(
        server.tmux, "find_pane",
        lambda pane: SimpleNamespace(id="%1", pid="123") if pane == "%1" else None,
    )

    def unexpected_command(argv):
        raise AssertionError(f"Mobile navigation executed tmux command: {argv}")

    monkeypatch.setattr(server.tmux, "_run", unexpected_command)
    monkeypatch.setattr(server.tmux, "server_uid", lambda: "test:1")
    monkeypatch.setattr(server, "_visited", lambda *_args: None)
    client = TestClient(server.app)
    response = client.post("/api/panes/%1/select")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_missing_pane_still_returns_404(monkeypatch):
    monkeypatch.setattr(server.tmux, "find_pane", lambda _pane: None)
    response = TestClient(server.app).post("/api/panes/%9999/select")
    assert response.status_code == 404
