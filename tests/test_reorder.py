"""Dock drag-reorder persisted into tmux (POST /api/panes/{id}/reorder → tmux.reorder_pane).

The reorder is a WINDOW-level move-window: dragging a dock icon moves its window before/
after the target's window, which is what tmux's own window-index order (and thus the
dock's flattened order) keys off. tmux is MOCKED — we assert the exact move-window command
issued for a drop, and that cross-session / unknown targets are rejected without a move."""

import logging
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
    assert calls == [
        ["move-window", "-b", "-s", "main:3", "-t", "main:0"],
        ["move-window", "-r", "-t", "main:"],
    ]


def test_move_after_issues_move_window_a(monkeypatch):
    calls = _patch(monkeypatch, [_pane("%1", window_index="0"), _pane("%2", window_index="3")])
    tmux.reorder_pane("%1", "%2", after=True)
    assert calls == [
        ["move-window", "-a", "-s", "main:0", "-t", "main:3"],
        ["move-window", "-r", "-t", "main:"],
    ]


def test_renumber_is_a_separate_command(monkeypatch):
    """-r must NEVER ride along on the move itself. tmux (verified on 3.4) treats
    `move-window -r -s X -t Y` as a renumber and IGNORES the move, exiting 0 having done
    nothing — folding the two together would silently turn every drag into a no-op. So
    the move command must carry no -r, and the renumber must carry no -s."""
    calls = _patch(monkeypatch, [_pane("%1", window_index="0"), _pane("%2", window_index="3")])
    tmux.reorder_pane("%1", "%2", after=True)
    move, renumber = calls
    assert "-r" not in move          # the move must actually move
    assert "-s" not in renumber      # the renumber must not pretend to move
    assert renumber == ["move-window", "-r", "-t", "main:"]


def test_renumber_failure_does_not_fail_the_reorder(monkeypatch):
    """The move landed; a failed cosmetic renumber must not report it as an error."""
    calls = []

    def run(args):
        calls.append(args)
        if "-r" in args:
            raise subprocess.CalledProcessError(returncode=1, cmd=["tmux", *args])
        return ""

    by_id = {p.id: p for p in [_pane("%1", window_index="0"), _pane("%2", window_index="3")]}
    monkeypatch.setattr(tmux, "find_pane", lambda t: by_id.get(t))
    monkeypatch.setattr(tmux, "_run", run)
    tmux.reorder_pane("%1", "%2", after=True)  # must NOT raise
    assert len(calls) == 2  # it still attempted the renumber


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
        assert calls == [
            ["move-window", "-a", "-s", "main:0", "-t", "main:5"],
            ["move-window", "-r", "-t", "main:"],
        ]


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


def test_endpoint_resolves_the_source_exactly_once(monkeypatch):
    """The endpoint has NO preflight find_pane: reorder_pane resolves both panes itself,
    so a reorder costs two lookups, not three, on a gesture-driven path. Resolving the
    source once also means there is no window in which it can vanish between a check and
    the move — the race an earlier preflight version had to reason about cannot arise."""
    looked = []

    def find(t):
        looked.append(t)
        return {"%1": _pane("%1", window_index="0"),
                "%2": _pane("%2", window_index="1")}.get(t)

    monkeypatch.setattr(tmux, "find_pane", find)
    monkeypatch.setattr(tmux, "_run", lambda args: "")
    c = TestClient(server.app)
    r = c.post(_path("%1"), json={"target": "%2", "after": False})
    assert r.status_code == 200
    assert looked == ["%1", "%2"]  # one lookup each, no preflight


def test_endpoint_missing_source_is_404_without_a_move(monkeypatch):
    """A source that does not exist is a 404 and never reaches move-window — the same
    contract the old preflight guard provided, now served by reorder_pane's own lookup."""
    calls = []
    monkeypatch.setattr(tmux, "find_pane",
                        lambda t: _pane("%2", window_index="1") if t == "%2" else None)
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or "")
    c = TestClient(server.app)
    r = c.post(_path("%1"), json={"target": "%2", "after": False})
    # Not `calls == []`: the audit path also reads the tmux server pid
    # (`display-message -p #{pid}`) for the telemetry uid, which is unrelated to the
    # reorder. What matters is that a missing source never reached move-window.
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


def test_audit_detail_escapes_client_supplied_target(monkeypatch, caplog):
    """`target` is client-supplied and the audit trail is a security surface, so it must
    reach the log escaped and capped — one request must never be able to forge a second
    audit record. Mirrors the !r + [:80] treatment new_window already applies."""
    _patch(monkeypatch, [_pane("%1")])
    c = TestClient(server.app)
    forged = "%2\nreorder_pane pane=%9 outcome=ok BOGUS"
    with caplog.at_level(logging.INFO, logger="daemon.server.audit"):
        c.post(_path("%1"), json={"target": forged, "after": False})
    lines = [r.getMessage() for r in caplog.records]
    assert lines, "the reorder attempt must be audited at all"
    # The raw newline never reaches the log: it survives only as the escaped \n that !r
    # produces, so the forged text cannot become a record of its own.
    assert not any("\n" in ln for ln in lines)
    assert any("\\n" in ln for ln in lines)


def test_audit_detail_caps_an_overlong_target(monkeypatch, caplog):
    """A megabyte of target text must not become a megabyte of audit log."""
    _patch(monkeypatch, [_pane("%1")])
    c = TestClient(server.app)
    with caplog.at_level(logging.INFO, logger="daemon.server.audit"):
        c.post(_path("%1"), json={"target": "%" + "A" * 5000, "after": False})
    for ln in (r.getMessage() for r in caplog.records):
        assert "A" * 200 not in ln


def test_audit_escapes_the_client_supplied_pane_id(monkeypatch, caplog):
    """pane_id is a URL path segment, so "%0A" reaches _audit as a real newline. It is
    escaped in _audit itself — hardening the shared helper rather than this one endpoint,
    so every audited route (send/select/reorder/paste) gets the guarantee and a future
    route cannot forget it."""
    _patch(monkeypatch, [_pane("%1")])
    c = TestClient(server.app)
    forged = "%9\nAUDIT reorder_pane pane=%1 by someone-else"
    with caplog.at_level(logging.INFO, logger="daemon.server.audit"):
        c.post(_path(forged), json={"target": "%1", "after": False})
    lines = [r.getMessage() for r in caplog.records]
    assert lines, "the attempt must be audited at all"
    assert not any("\n" in ln for ln in lines)  # no forged second record
    assert any("\\n" in ln for ln in lines)     # it survives only as escaped text
