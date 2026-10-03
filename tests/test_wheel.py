"""tmux.wheel sends scroll-wheel reports to a fullscreen app, and to nothing else.

The gate is the app's own request: SGR mouse reports on the alternate screen. An inline
app's history is already in tmux's scrollback, and the bytes would reach a shell or an
inline agent as garbage keystrokes."""
import os
import shutil
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import openbus.tmux as T
from openbus import server


def fake_tmux(monkeypatch, flags="1 1 0"):
    sent = []

    def run(argv):
        if argv[0] == "display-message":
            return f"80 24 {flags}\n"
        sent.append(argv)
        return ""

    monkeypatch.setattr(T, "pane_pid", lambda _pane_id: "1234")
    monkeypatch.setattr(T, "_run", run)
    return sent


def report(argv):
    return bytes.fromhex("".join(argv[argv.index("-H") + 1:])).decode()


@pytest.mark.parametrize(("lines", "seq"), [(2, "\x1b[<64;41;13M" * 2), (-1, "\x1b[<65;41;13M")])
def test_notches_go_to_the_pane_centre(monkeypatch, lines, seq):
    sent = fake_tmux(monkeypatch)
    assert T.wheel("%1", lines, expected_pid="1234")
    assert [report(argv) for argv in sent] == [seq]


# sgr, alternate screen, tmux copy mode
@pytest.mark.parametrize("flags", ["0 1 0", "1 0 0", "1 1 1"])
def test_only_a_fullscreen_mouse_app_is_scrolled(monkeypatch, flags):
    sent = fake_tmux(monkeypatch, flags)
    assert not T.wheel("%1", 1, expected_pid="1234")
    assert sent == []


def test_zero_notches_send_nothing(monkeypatch):
    sent = fake_tmux(monkeypatch)
    assert not T.wheel("%1", 0, expected_pid="1234")
    assert sent == []


def test_recycled_pane_never_receives_a_stale_wheel(monkeypatch):
    sent = fake_tmux(monkeypatch)
    monkeypatch.setattr(T, "pane_pid", lambda _pane_id: "5678")
    with pytest.raises(T.PaneChangedError):
        T.wheel("%1", 1, expected_pid="1234")
    assert sent == []


@pytest.mark.parametrize("sent", [False, True])
def test_endpoint_canonicalizes_audits_and_never_reparses(monkeypatch, sent):
    wheel, audit = Mock(return_value=sent), Mock()
    watcher = SimpleNamespace(request_reparse=Mock())
    monkeypatch.setattr(T, "find_pane", lambda _alias: SimpleNamespace(id="%7", pid="42"))
    monkeypatch.setattr(T, "wheel", wheel)
    monkeypatch.setattr(server, "_audit", audit)
    monkeypatch.setattr(server.app.state, "watcher", watcher, raising=False)
    response = TestClient(server.app).post("/api/panes/work:0.0/wheel", json={"lines": 3})
    assert response.status_code == 200
    assert response.json() == {"sent": sent}
    wheel.assert_called_once_with("%7", 3, expected_pid="42")
    assert audit.call_args.kwargs["outcome"] == ("ok" if sent else "not sent")
    watcher.request_reparse.assert_not_called()


@pytest.mark.parametrize(("failure", "status"),
                         [("missing", 404), ("timeout", 504), ("changed", 409)])
def test_endpoint_audits_refusals(monkeypatch, failure, status):
    pane = None if failure == "missing" else SimpleNamespace(id="%7", pid="42")
    monkeypatch.setattr(T, "find_pane", lambda _id: pane)
    error = (T.PaneChangedError("changed") if failure == "changed"
             else subprocess.CalledProcessError(124, "tmux"))
    wheel, audit = Mock(side_effect=error), Mock()
    monkeypatch.setattr(T, "wheel", wheel)
    monkeypatch.setattr(server, "_audit", audit)
    response = TestClient(server.app).post("/api/panes/alias/wheel", json={"lines": 1})
    assert response.status_code == status
    audit.assert_called_once()
    assert audit.call_args.kwargs["outcome"].startswith(("rejected", "error"))


@pytest.mark.parametrize("lines", [31, -31, "up"])
def test_endpoint_bounds_one_request(monkeypatch, lines):
    wheel = Mock()
    monkeypatch.setattr(T, "wheel", wheel)
    response = TestClient(server.app).post("/api/panes/%1/wheel", json={"lines": lines})
    assert response.status_code == 422
    wheel.assert_not_called()


def test_real_tmux_delivers_only_to_a_mouse_app(tmp_path, monkeypatch):
    """Against a private tmux server (never the user's: explicit -S, $TMUX unset): a
    shell gets nothing, and an app that turns on the alternate screen and SGR mouse
    reports reads the exact wheel bytes."""
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = str(tmp_path / "wheel.sock")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}

    def run(args):
        return subprocess.check_output(["tmux", "-S", socket, *args], text=True, env=env)

    def wait_for(predicate):
        for _ in range(100):
            if predicate():
                return True
            time.sleep(0.02)
        return False

    pane = None
    try:
        pane = run(["new-session", "-d", "-P", "-F", "#{pane_id}",
                    "-x", "40", "-y", "10", "sh"]).strip()
        monkeypatch.setattr(T, "_run", run)
        pid = T.pane_pid(pane)
        assert not T.wheel(pane, 1, expected_pid=pid)
        run(["send-keys", "-t", pane,
             r"printf '\033[?1049h\033[?1000h\033[?1006h'; stty -icanon -echo; cat -v", "Enter"])
        assert wait_for(lambda: run(["display-message", "-p", "-t", pane,
                                     "#{alternate_on}#{mouse_sgr_flag}"]).strip() == "11")
        assert T.wheel(pane, 1, expected_pid=pid)
        assert wait_for(lambda: "^[[<64;21;6M" in run(["capture-pane", "-p", "-t", pane]))
    finally:
        # Close only the pane this test made; the private server exits with it.
        if pane:
            subprocess.run(["tmux", "-S", socket, "kill-pane", "-t", pane], check=False,
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
