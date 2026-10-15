"""Fit width (openbus/fit.py): a viewed lone pane's window takes the viewer's width, and gets
its own size back when the viewer leaves, goes quiet, or the daemon restarts.

Runs a REAL tmux on a private socket (TMUXRC_TMUX_SOCKET), never the developer's server.
Cleanup kills that socket's panes and nothing else."""

import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import openbus.server as S
from openbus import fit
from openbus import tmux as T

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")


@pytest.fixture
def pane(monkeypatch):
    name = f"fit-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TMUXRC_TMUX_SOCKET", name)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(fit, "_viewers", {})
    yield T._run(["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "s", "-x", "200",
                  "-y", "50", "sleep 60"]).strip()
    for p in T._run(["list-panes", "-a", "-F", "#{pane_id}"]).split():  # no kill-server
        subprocess.run(["tmux", "-L", name, "kill-pane", "-t", p], check=False)


def size(pane):
    return T._run(["display-message", "-p", "-t", pane,
                   "#{window_width}x#{window_height} #{@tmuxrc-fit}"]).rstrip("\n")


def local_window_size(pane):
    return T._run(["show-options", "-wqv", "-t", pane, "window-size"]).strip()


def test_fits_width_only_and_restores_on_leave(pane):
    assert fit.view(pane, "phone", 52) == (False, ["@0 52 cols"])  # nothing attached
    assert size(pane) == "52x50 52"  # height untouched: scrollback is capture's
    fit.view(pane, "phone", 0)
    assert size(pane) == "200x50 "
    assert local_window_size(pane) == ""  # inherits window-size again, not stuck manual


def test_clamps_to_a_usable_width(pane):
    fit.view(pane, "phone", 12)
    assert size(pane).startswith(f"{fit.MIN_COLS}x")


def test_split_window_is_left_alone_unless_the_pane_is_zoomed(pane):
    hidden = T._run(["split-window", "-d", "-P", "-F", "#{pane_id}", "-t", pane,
                     "sleep 60"]).strip()
    fit.view(pane, "phone", 60)
    assert size(pane) == "200x50 "
    T._run(["resize-pane", "-Z", "-t", pane])
    fit.view(hidden, "phone", 60)  # the zoomed pane fills the window, not this one
    assert size(pane) == "200x50 "
    fit.view(pane, "phone", 60)
    assert size(pane).startswith("60x")


@contextmanager
def attached(session):
    """A desktop terminal on `session`: a control-mode client, stopped by its own PID."""
    client = subprocess.Popen(["tmux", "-L", os.environ["TMUXRC_TMUX_SOCKET"], "-C", "attach",
                               "-t", session], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    try:
        while T._run(["display-message", "-p", "-t", session,
                      "#{session_attached}"]).strip() == "0":
            pass
        yield
    finally:
        client.terminate()
        client.wait()


@pytest.mark.parametrize("session", ["s", "grouped"])  # or a session sharing the window
def test_attached_terminal_needs_the_toggle(pane, session):
    if session != "s":
        T._run(["new-session", "-d", "-t", "s", "-s", session])
    with attached(session):
        assert fit.view(pane, "phone", 60) == (True, [])
        assert size(pane) == "200x50 "
        fit.view(pane, "phone", 60, force=True)
        assert size(pane).startswith("60x")
        fit.view(pane, "phone", 60)  # toggled off: put back
        assert size(pane).endswith(" ")


def test_an_attach_retires_every_unforced_viewer(pane):
    fit.view(pane, "a", 60)
    fit.view(pane, "b", 80)
    with attached("s"):
        fit.view(pane, "a", 60)  # its next report: neither viewer opted in
        assert size(pane) == "200x50 "


def test_release_after_the_pane_is_gone(pane):
    sibling = T._run(["split-window", "-d", "-P", "-F", "#{pane_id}", "-t", pane,
                      "sleep 60"]).strip()
    T._run(["resize-pane", "-Z", "-t", pane])
    fit.view(pane, "phone", 60)
    assert size(sibling).startswith("60x")
    T._run(["kill-pane", "-t", pane])
    with pytest.raises(subprocess.CalledProcessError):
        fit.view(pane, "phone", 0)
    assert size(sibling).startswith("200x")


def test_narrowest_viewer_wins_and_last_out_restores(pane):
    fit.view(pane, "a", 80)
    fit.view(pane, "b", 50)
    assert size(pane) == "50x50 50"
    fit.view(pane, "b", 0)
    assert size(pane) == "80x50 80"
    fit.view(pane, "a", 0)
    assert size(pane) == "200x50 "


def test_a_manual_resize_is_never_fought(pane):
    fit.view(pane, "phone", 60)
    T._run(["resize-window", "-x", "120", "-t", pane])
    fit.view(pane, "phone", 60)  # theirs now: mark dropped, size kept
    assert size(pane) == "120x50 "
    fit.view(pane, "phone", 60)  # and the size they set stays put
    fit.view(pane, "phone", 0)
    assert size(pane) == "120x50 "


def test_sweep_restores_what_no_viewer_wants(pane, monkeypatch):
    fit.view(pane, "phone", 60)
    fit.sweep()
    assert size(pane) == "60x50 60"  # still watched
    monkeypatch.setattr(fit, "_viewers", {})  # a restarted daemon: nobody is watching
    fit.sweep()
    assert size(pane) == "200x50 "


def test_sweep_never_undoes_a_manual_resize(pane, monkeypatch):
    fit.view(pane, "phone", 60)
    T._run(["resize-window", "-x", "120", "-t", pane])  # while the daemon was down
    monkeypatch.setattr(fit, "_viewers", {})
    fit.sweep()
    assert size(pane) == "120x50 "
    assert local_window_size(pane) == "manual"  # their setting, left alone


def test_a_split_ends_the_fit_without_a_new_report(pane):
    fit.view(pane, "phone", 60, force=True)
    T._run(["split-window", "-d", "-t", pane, "sleep 60"])
    fit.sweep()
    assert size(pane) == "200x50 "


def test_sweep_never_fits_a_window_the_user_took(pane):
    with attached("s"):
        fit.view(pane, "phone", 60)  # registered, but a terminal is attached: no fit
        T._run(["resize-window", "-x", "120", "-t", pane])
    fit.sweep()  # detached again before the phone's next report
    assert size(pane) == "120x50 "


def test_endpoint_audits_changes_not_heartbeats(pane, monkeypatch):
    audits = []
    monkeypatch.setattr(S, "_audit", lambda _r, action, pane_id, detail="", outcome="ok":
                        audits.append((action, detail, outcome)))
    client = TestClient(S.app)
    for _ in range(2):  # the second is a heartbeat: nothing changes, nothing audited
        reply = client.post(f"/api/panes/{pane}/fit", json={"cols": 70})
        assert reply.json() == {"attached": False}
    assert size(pane) == "70x50 70"
    assert client.post("/api/panes/%25999/fit", json={"cols": 70}).status_code == 404
    assert audits == [("fit", "cols=70 force=False: @0 70 cols", "ok"),
                      ("fit", "cols=70 force=False", "error: tmux rc 1")]
