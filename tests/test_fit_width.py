"""Fit width (openbus/fit.py): a viewed lone pane's window takes the phone's width, and keeps
it until a terminal attaches or the phone unticks the toggle. Our own resize is not activity.

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
from openbus import fit, watcher
from openbus import tmux as T

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")


@pytest.fixture
def pane(monkeypatch):
    name = f"fit-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TMUXRC_TMUX_SOCKET", name)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(fit, "_resized", {})
    yield T._run(["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "s", "-x", "200",
                  "-y", "50", "sleep 60"]).strip()
    for p in T._run(["list-panes", "-a", "-F", "#{pane_id}"]).split():  # no kill-server
        subprocess.run(["tmux", "-L", name, "kill-pane", "-t", p], check=False)


def size(pane):
    return T._run(["display-message", "-p", "-t", pane,
                   "#{window_width}x#{window_height} #{@tmuxrc-fit}"]).rstrip("\n")


def local_window_size(pane):
    return T._run(["show-options", "-wqv", "-t", pane, "window-size"]).strip()


def test_fits_width_only_and_keeps_it(pane):
    assert fit.view(pane, 52) is False  # nothing attached
    assert size(pane) == "52x50 52"  # height untouched: scrollback is capture's
    fit.view(pane, 52)  # an unchanged report changes nothing
    assert size(pane) == "52x50 52"


def test_clamps_to_a_usable_width(pane):
    fit.view(pane, 12)
    assert size(pane).startswith(f"{fit.MIN_COLS}x")


def test_split_window_is_left_alone_unless_the_pane_is_zoomed(pane):
    hidden = T._run(["split-window", "-d", "-P", "-F", "#{pane_id}", "-t", pane,
                     "sleep 60"]).strip()
    fit.view(pane, 60)
    assert size(pane) == "200x50 "
    T._run(["resize-pane", "-Z", "-t", pane])
    fit.view(hidden, 60)  # the zoomed pane fills the window, not this one
    assert size(pane) == "200x50 "
    fit.view(pane, 60)
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


def restored(pane):
    return size(pane).endswith(" ") and not size(pane).startswith("60x") \
        and local_window_size(pane) == ""  # inherits window-size again, not stuck manual


@pytest.mark.parametrize("session", ["s", "grouped"])  # or a session sharing the window
def test_attached_terminal_needs_the_toggle(pane, session):
    if session != "s":
        T._run(["new-session", "-d", "-t", "s", "-s", session])
    with attached(session):
        assert fit.view(pane, 60) is True
        assert size(pane) == "200x50 "
        fit.view(pane, 60, force=True)
        assert size(pane) == "60x50 60!"
        fit.release_attached(T.list_panes())  # forced: the watcher leaves it
        assert size(pane) == "60x50 60!"
        fit.view(pane, 60)  # unticked: given back
        assert restored(pane)


def test_a_terminal_attaching_gets_the_window_back(pane):
    fit.view(pane, 60)
    fit.release_attached(T.list_panes())  # nobody attached: kept
    assert size(pane) == "60x50 60"
    with attached("s"):
        fit.release_attached(T.list_panes())
        assert restored(pane)


def test_a_manual_resize_is_never_fought(pane):
    fit.view(pane, 60)
    T._run(["resize-window", "-x", "120", "-t", pane])
    fit.view(pane, 60)  # theirs now: mark dropped, size kept
    assert size(pane) == "120x50 "
    fit.view(pane, 60)  # and the size they set stays put
    assert size(pane) == "120x50 "
    with attached("s"):
        fit.view(pane, 60)
        assert size(pane) == "120x50 "


def test_endpoint_audits(pane, monkeypatch):
    audits = []
    monkeypatch.setattr(S, "_audit", lambda _r, action, pane_id, detail="", outcome="ok":
                        audits.append((action, detail, outcome)))
    client = TestClient(S.app)
    assert client.post(f"/api/panes/{pane}/fit", json={"cols": 70}).json() == {"attached": False}
    assert size(pane) == "70x50 70"
    assert client.post("/api/panes/%25999/fit", json={"cols": 70}).status_code == 404
    assert audits == [("fit", "cols=70 force=False", "ok"),
                      ("fit", "cols=70 force=False", "error: tmux rc 1")]


def test_our_resize_is_not_activity(pane, monkeypatch, tmp_path):
    """An app that lays out at the window's width redraws when we resize it. The watcher
    must not count that as the pane doing something: no new parse, the idle clock runs on."""
    app = tmp_path / "app.sh"
    app.write_text("draw() { clear; printf 'x%.0s' $(seq $(tput cols)); echo; }\n"
                   "trap draw WINCH; draw; while :; do sleep 1 & wait $!; done\n")
    tui = T._run(["new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "s",
                  f"bash {app}"]).strip()
    T._run(["kill-pane", "-t", pane])  # the app's window alone
    parses = []
    real = watcher.classify
    monkeypatch.setattr(watcher, "classify", lambda *a, **k: (
        parses.append(1), {**real(*a, **k), "parse_ok": True})[1])
    w = watcher.Watcher(target=None, use_llm=False, history=None)

    def screen():
        return T._run(["capture-pane", "-p", "-t", tui]).split("\n")[0]

    def tick():
        w._tick()
        return next(s for s in w.states if s["pane_id"] == tui)

    while "x" not in screen():
        pass
    before = tick()["last_activity_at"]
    fit.view(tui, 50)
    while len(screen()) != 50:  # redrawn at the new width
        pass
    state = tick()
    assert len(parses) == 1
    assert len(w.snapshots[tui]) == 1
    assert state["last_activity_at"] == before  # not "now": the list doesn't reorder
    monkeypatch.setattr(fit, "_resized", {})  # long after our resize, the same change is real
    T._run(["resize-window", "-x", "60", "-t", tui])
    while len(screen()) != 60:
        pass
    tick()
    assert len(parses) == 2
