"""Dock drag-reorder persisted into tmux (POST /api/panes/{id}/reorder → tmux.reorder_pane).

The reorder is a WINDOW-level move-window: dragging a dock icon moves its window before/
after the target's window, which is what tmux's own window-index order (and thus the
dock's flattened order) keys off. tmux is MOCKED — we assert the exact move-window command
issued for a drop, and that cross-session / unknown targets are rejected without a move."""

import subprocess
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from daemon import server, tmux


def _path(pane_id):
    # The client posts to encodeURIComponent(pane_id) — a "%N" id percent-encodes to
    # "%25N", so exercise the endpoint through the SAME encoded path a real request uses
    # (a raw "%" in a URL path is the start of an escape, not a literal).
    return f"/api/panes/{quote(pane_id, safe='')}/reorder"


def _pane(pane_id, session="main", window_index="0"):
    return tmux.Pane(
        session=session,
        window_index=window_index,
        window_name="w",
        pane_index="0",
        id=pane_id,
        current_command="bash",
        title="",
    )


def _patch(monkeypatch, panes):
    """Route find_pane at the panes we declare and capture every tmux invocation."""
    by_id = {p.id: p for p in panes}
    calls = []
    monkeypatch.setattr(tmux, "find_pane", lambda t: by_id.get(t))
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or "")
    return calls


def test_move_before_issues_move_window_b(monkeypatch):
    calls = _patch(monkeypatch, [_pane("%1", window_index="0"), _pane("%2", window_index="3")])
    tmux.reorder_pane("%2", "%1", after=False)
    assert calls == [["move-window", "-b", "-s", "main:3", "-t", "main:0"]]


def test_move_after_issues_move_window_a(monkeypatch):
    calls = _patch(monkeypatch, [_pane("%1", window_index="0"), _pane("%2", window_index="3")])
    tmux.reorder_pane("%1", "%2", after=True)
    assert calls == [["move-window", "-a", "-s", "main:0", "-t", "main:3"]]


def test_sibling_pane_drags_its_whole_window(monkeypatch):
    """Dragging ANY pane of a multi-pane window moves the WINDOW — the same single
    move-window either sibling would produce. This is the documented "panes sharing a
    window travel together" contract, and it is what the client's optimistic shuffle
    mirrors (it relocates the whole window's run, not the one dragged icon)."""
    panes = [_pane("%1", window_index="0"), _pane("%2", window_index="0"),
             _pane("%3", window_index="5")]
    for dragged in ("%1", "%2"):  # either sibling ⇒ identical command
        calls = _patch(monkeypatch, panes)
        tmux.reorder_pane(dragged, "%3", after=True)
        assert calls == [["move-window", "-a", "-s", "main:0", "-t", "main:5"]]


def test_same_window_is_noop(monkeypatch):
    # Two panes sharing a window (same index) — the drop can't reorder them at the window
    # level, so it's a harmless no-op, NOT a move that would renumber the session.
    calls = _patch(monkeypatch, [_pane("%1", window_index="2"), _pane("%2", window_index="2")])
    tmux.reorder_pane("%1", "%2", after=True)
    assert calls == []


def test_cross_session_rejected(monkeypatch):
    calls = _patch(monkeypatch, [_pane("%1", session="a", window_index="0"),
                                 _pane("%2", session="b", window_index="0")])
    with pytest.raises(RuntimeError, match="across sessions"):
        tmux.reorder_pane("%1", "%2", after=False)
    assert calls == []  # nothing issued — a rejected drag never touches tmux


def test_missing_target_rejected(monkeypatch):
    calls = _patch(monkeypatch, [_pane("%1")])
    with pytest.raises(RuntimeError, match="target pane not found"):
        tmux.reorder_pane("%1", "%99", after=False)
    assert calls == []


def test_endpoint_ok(monkeypatch):
    _patch(monkeypatch, [_pane("%1", window_index="0"), _pane("%2", window_index="1")])
    c = TestClient(server.app)
    r = c.post(_path("%1"), json={"target": "%2", "after": True})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_endpoint_unknown_source_is_404(monkeypatch):
    _patch(monkeypatch, [_pane("%1")])
    c = TestClient(server.app)
    r = c.post(_path("%9"), json={"target": "%1", "after": False})
    assert r.status_code == 404


def test_endpoint_unknown_target_is_400(monkeypatch):
    # Source exists but the body's target does not — a rejectable request (400), distinct
    # from a missing source (404). Matches the stated endpoint contract.
    _patch(monkeypatch, [_pane("%1")])
    c = TestClient(server.app)
    r = c.post(_path("%1"), json={"target": "%99", "after": False})
    assert r.status_code == 400 and "target" in r.json()["detail"]


def test_endpoint_cross_session_is_400(monkeypatch):
    _patch(monkeypatch, [_pane("%1", session="a"), _pane("%2", session="b")])
    c = TestClient(server.app)
    r = c.post(_path("%1"), json={"target": "%2", "after": False})
    assert r.status_code == 400 and "session" in r.json()["detail"]


def test_endpoint_source_vanishes_mid_call_is_404(monkeypatch):
    # Race: the source pane exists at the endpoint's guard but is gone by the time
    # reorder_pane does its own lookup (pane closed). That's a 404 (matching the guard),
    # not a 400 — a vanished source is not a bad request.
    calls = []
    seen = {"n": 0}

    def find(t):
        if t == "%2":
            return _pane("%2", window_index="1")
        seen["n"] += 1
        return _pane("%1") if seen["n"] == 1 else None  # present at guard, gone after

    monkeypatch.setattr(tmux, "find_pane", find)
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or "")
    c = TestClient(server.app)
    r = c.post(_path("%1"), json={"target": "%2", "after": False})
    # No MOVE was issued. Not `calls == []`: going through the endpoint also runs the
    # audit path, which reads the tmux server pid (`display-message -p #{pid}`) for the
    # telemetry uid. That call is unrelated to the reorder — what matters here is that a
    # vanished source never reached move-window.
    assert r.status_code == 404
    assert [c for c in calls if c[0] == "move-window"] == []


def test_endpoint_tmux_error_propagates(monkeypatch):
    # A genuine tmux failure (not a rejectable request) must surface as a 500, not be
    # swallowed — the middleware/audit path records it as an error.
    monkeypatch.setattr(tmux, "find_pane",
                        lambda t: _pane("%1") if t == "%1" else _pane("%2", window_index="1"))

    def boom(args):
        raise subprocess.CalledProcessError(returncode=1, cmd=["tmux", *args])

    monkeypatch.setattr(tmux, "_run", boom)
    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.post(_path("%1"), json={"target": "%2", "after": False})
    assert r.status_code == 500
