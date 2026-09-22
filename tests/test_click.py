"""tmux.click turns a frame-relative tap into an SGR mouse report on the pane.

Rows count up from the frame's last line, so they must resolve against the VISIBLE
screen's last line, and nothing may be written to a pane whose app didn't ask for
mouse reports (a shell would echo it as garbage)."""
import hashlib

import pytest

import openbus.tmux as T


def fake_tmux(monkeypatch, screen, sgr="1", physical=None):
    sent = []

    def run(argv):
        if argv[0] == "display-message":
            return f"80 {sgr}\n"
        if argv[0] == "capture-pane":
            assert argv[-1] in {"-0", "-200"}  # geometry or live-frame freshness
            return physical if "-J" not in argv and physical is not None else screen
        sent.append(argv)
        return ""

    monkeypatch.setattr(T, "pane_pid", lambda _pane_id: "1234")
    monkeypatch.setattr(T, "_run", run)
    return sent


def report(argv):
    return bytes.fromhex("".join(argv[argv.index("-H") + 1:])).decode()


def test_row_counts_up_from_visible_last_line(monkeypatch):
    # 4 visible rows plus the trailing blanks the live frame also rstrips
    sent = fake_tmux(monkeypatch, "a\nb\nc\nd\n\n\n")
    assert T.click("%1", expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb\nc\nd").hexdigest(),
                       from_bottom=1, col=5)
    assert report(sent[0]) == "\x1b[<0;5;3M\x1b[<0;5;3m"


def test_no_mouse_mode_sends_nothing(monkeypatch):
    sent = fake_tmux(monkeypatch, "a\nb", sgr="0")
    assert not T.click("%1", expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb").hexdigest(),
                       from_bottom=0, col=1)
    assert sent == []


def test_tap_on_history_sends_nothing(monkeypatch):
    sent = fake_tmux(monkeypatch, "a\nb")
    assert not T.click("%1", expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb").hexdigest(),
                       from_bottom=2, col=1)
    assert sent == []


def test_wrapped_screen_is_not_safe_to_click(monkeypatch):
    sent = fake_tmux(monkeypatch, "long joined line\nlast", physical="long\njoined line\nlast")
    assert not T.click("%1", expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb").hexdigest(),
                       from_bottom=0, col=1)
    assert sent == []


def test_out_of_bounds_coordinates_send_nothing(monkeypatch):
    sent = fake_tmux(monkeypatch, "a\nb")
    assert not T.click("%1", expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb").hexdigest(),
                       from_bottom=-1, col=1)
    assert not T.click("%1", expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb").hexdigest(),
                       from_bottom=0, col=0)
    assert not T.click("%1", expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb").hexdigest(),
                       from_bottom=0, col=81)
    assert sent == []


def test_recycled_pane_never_receives_a_stale_click(monkeypatch):
    sent = fake_tmux(monkeypatch, "a\nb")
    monkeypatch.setattr(T, "pane_pid", lambda _pane_id: "5678")
    with pytest.raises(T.PaneChangedError):
        T.click("%1",
                       from_bottom=0, col=1, expected_pid="1234",
                       expected_frame=hashlib.md5(b"a\nb").hexdigest())
    assert sent == []


@pytest.mark.parametrize("sent", [False, True])
def test_click_endpoint_canonicalizes_and_reparses_only_sent_clicks(monkeypatch, sent):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from openbus.server import app

    click = Mock(return_value=sent)
    watcher = SimpleNamespace(request_reparse=Mock())
    monkeypatch.setattr(T, "find_pane", lambda _alias: SimpleNamespace(id="%7", pid="42"))
    monkeypatch.setattr(T, "click", click)
    monkeypatch.setattr(app.state, "watcher", watcher, raising=False)
    response = TestClient(app).post("/api/panes/work:0.0/click",
                                    json={"frame": "a" * 32, "from_bottom": 2, "col": 4})
    assert response.status_code == 200
    assert response.json() == {"sent": sent}
    click.assert_called_once_with("%7", 2, 4, expected_pid="42", expected_frame="a" * 32)
    assert watcher.request_reparse.call_args_list == ([(("%7",), {})] if sent else [])


@pytest.mark.parametrize(("failure", "status"), [("missing", 404), ("gone", 404),
                                                ("timeout", 504), ("changed", 409)])
def test_click_endpoint_reports_pane_failures(monkeypatch, failure, status):
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from openbus import server

    pane = None if failure == "missing" else SimpleNamespace(id="%7", pid="42")
    monkeypatch.setattr(T, "find_pane", lambda _id: pane)
    error = subprocess.CalledProcessError(124 if failure == "timeout" else 1, "tmux")
    if failure == "changed": error = T.PaneChangedError("changed")
    click = Mock(side_effect=error)
    audit = Mock()
    monkeypatch.setattr(T, "click", click)
    monkeypatch.setattr(server, "_audit", audit)
    response = TestClient(server.app).post(
        "/api/panes/alias/click", json={"frame": "a" * 32, "from_bottom": 0, "col": 1},
    )
    assert response.status_code == status
    audit.assert_called_once()
    if failure == "missing": click.assert_not_called()


@pytest.mark.parametrize("body", [{"from_bottom": -1, "col": 1}, {"from_bottom": 0, "col": 0}])
def test_click_endpoint_rejects_invalid_coordinates(monkeypatch, body):
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from openbus.server import app

    click = Mock()
    monkeypatch.setattr(T, "click", click)
    assert TestClient(app).post("/api/panes/%1/click", json=body).status_code == 422
    click.assert_not_called()


@pytest.mark.parametrize(("code", "status"), [(1, 404), (124, 504)])
def test_click_endpoint_audits_and_maps_preflight_failures(monkeypatch, code, status):
    import subprocess
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from openbus import server

    lookup = Mock(side_effect=subprocess.CalledProcessError(code, "tmux"))
    monkeypatch.setattr(T, "find_pane", lookup)
    click, audit = Mock(), Mock()
    monkeypatch.setattr(T, "click", click)
    monkeypatch.setattr(server, "_audit", audit)
    response = TestClient(server.app).post(
        "/api/panes/alias/click", json={"frame": "a" * 32, "from_bottom": 0, "col": 1},
    )
    assert response.status_code == status
    click.assert_not_called()
    audit.assert_called_once()


def test_changed_frame_never_receives_click(monkeypatch):
    sent = fake_tmux(monkeypatch, "new menu")
    assert not T.click("%1", from_bottom=0, col=1, expected_pid="1234",
                       expected_frame=hashlib.md5(b"old menu").hexdigest())
    assert sent == []
