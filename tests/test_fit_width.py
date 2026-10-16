"""Fit width (openbus/fit.py): a viewed lone pane's window takes the phone's width, and keeps
it until a terminal attaches or the phone unticks the toggle. Our own resize is not activity.

Runs a REAL tmux on a private socket (TMUXRC_TMUX_SOCKET), never the developer's server.
Cleanup kills that socket's panes and nothing else."""

import os
import shutil
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

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
        fit.view(pane, 0)  # nor does a give-back racing a fresh force, read under the lock
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


def test_a_window_size_the_user_chose_is_never_overwritten(pane):
    fit.view(pane, 60)
    T._run(["set-option", "-w", "-t", pane, "window-size", "largest"])  # same width
    with attached("s"):
        fit.view(pane, 60, force=True)  # neither a forced fit...
        fit.release_attached(T.list_panes())  # ...nor a give-back touches their policy
        assert local_window_size(pane) == "largest"
        assert size(pane).endswith(" ")  # and the window is no longer ours


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


@pytest.fixture
def app(pane, monkeypatch, tmp_path):
    """An app alone in its window that lays out at the window's width, as agent TUIs do (a
    rule as wide as the screen, text folded to it), redraws on resize, and prints something
    new on SIGUSR1. Yields a watcher on it, counting parses."""
    script = tmp_path / "app.sh"
    script.write_text(
        "draw() { clear; printf '\u2500%.0s' $(seq $(tput cols)); echo;"
        " echo 'the release ships on thursday once the checkout service passes its load"
        " test and the docs are published' | fold -s -w $(tput cols); }\n"
        "trap draw WINCH; trap 'echo Ship it now?' USR1; draw\n"
        "while :; do sleep 1 & wait $!; done\n")
    tui = T._run(["new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "s:",
                  f"bash {script}"]).strip()
    T._run(["kill-pane", "-t", pane])  # the app's window alone
    parses = []
    real = watcher.classify
    monkeypatch.setattr(watcher, "classify", lambda *a, **k: (
        parses.append(1), {**real(*a, **k), "parse_ok": True})[1])
    w = watcher.Watcher(target=None, use_llm=False, history=None)

    def screen():
        return T._run(["capture-pane", "-p", "-t", tui])

    def tick():
        w._tick()
        return next(s for s in w.states if s["pane_id"] == tui)

    while "release" not in screen():
        pass
    tick()
    return SimpleNamespace(id=tui, screen=screen, tick=tick, parses=parses, w=w)


def resize_and_wait(app, cols):
    fit.view(app.id, cols)
    while len(app.screen().split("\n")[0]) != cols:  # redrawn at the new width
        pass


def test_our_resize_is_not_activity(app):
    """No new parse, and the idle clock runs on (the list doesn't reorder it to "now")."""
    before = app.tick()["last_activity_at"]
    resize_and_wait(app, 50)
    assert app.tick()["last_activity_at"] == before  # mid-redraw: excused
    time.sleep(fit.REDRAW_SECONDS)
    state = app.tick()  # settled: the same content, reflowed
    assert (len(app.parses), len(app.w.snapshots[app.id])) == (1, 1)
    assert state["last_activity_at"] == before
    T._run(["resize-window", "-x", "60", "-t", app.id])  # not ours: the app's own change
    while len(app.screen().split("\n")[0]) != 60:
        pass
    app.tick()
    assert len(app.parses) == 2


def test_something_new_during_the_redraw_is_still_read(app):
    before = app.tick()["last_activity_at"]
    resize_and_wait(app, 50)
    app.tick()  # mid-redraw: excused
    os.kill(int(T._run(["display-message", "-p", "-t", app.id, "#{pane_pid}"])), signal.SIGUSR1)
    while "Ship it now?" not in app.screen():
        pass
    app.tick()  # still inside the redraw window: excused too
    time.sleep(fit.REDRAW_SECONDS)
    state = app.tick()  # settled, and not just a reflow of what was last read
    assert len(app.parses) == 2
    assert state["last_activity_at"] > before


def test_a_fit_before_the_first_look_is_read_as_usual(pane, monkeypatch):
    fit.view(pane, 50)
    w = watcher.Watcher(target=None, use_llm=False, history=None)
    w._tick()  # no card yet: the screen is parsed and snapshotted like any other
    assert len(w.snapshots[pane]) == 1


@pytest.mark.parametrize("via", ["s", "grouped"])  # or only a group sibling is attached
def test_target_mode_gives_a_window_back_too(pane, via):
    if via != "s":
        T._run(["new-session", "-d", "-t", "s", "-s", via])
    fit.view(pane, 60)
    with attached(via):
        watcher.Watcher(target="s:0", use_llm=False, history=None)._tick()
        assert restored(pane)


def test_only_a_size_change_arms_the_redraw_hook(pane):
    fit.view(pane, 200)  # already that wide: marked, nothing redraws
    assert size(pane) == "200x50 200"
    assert pane not in fit._resized
    with attached("s"):  # given back to the size it already has: nothing redraws either
        fit.release_attached(T.list_panes())
        assert restored(pane) and size(pane).startswith("200x50")
        assert pane not in fit._resized


def test_a_look_finished_before_the_resize_leaves_the_token(pane, monkeypatch):
    before = time.monotonic()
    fit.view(pane, 60)
    assert fit.just_resized(pane, before) == ""  # the old screen: the token waits
    assert fit.just_resized(pane, time.monotonic()) == "redraw"  # maybe half drawn
    monkeypatch.setattr(fit, "REDRAW_SECONDS", 0)
    assert fit.just_resized(pane, time.monotonic()) == "settled"  # spends it
    assert fit.just_resized(pane, time.monotonic()) == ""  # the next change is the app's


def test_the_hook_is_spent_on_the_next_look_even_if_nothing_moved(pane, monkeypatch):
    fit.view(pane, 60)  # `sleep` draws nothing, so later looks see no change
    monkeypatch.setattr(fit, "REDRAW_SECONDS", 0)
    watcher.Watcher(target=None, use_llm=False, history=None)._tick()
    assert pane not in fit._resized


def test_reflow_key_ignores_layout_not_content():
    wide = "\u256d\u2500\u2500\u2500\u2500\u256e\nthe release ships on thursday\n"
    narrow = "\u256d\u2500\u256e\nthe release\nships on\nthursday\n"
    assert fit.reflow_key(wide) == fit.reflow_key(narrow)
    assert fit.reflow_key(narrow) != fit.reflow_key(narrow + "Ship it now?")
