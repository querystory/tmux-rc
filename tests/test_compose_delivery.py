"""A complete draft must stay together even when another client types concurrently."""

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbus import server, tmux


@pytest.fixture
def client(monkeypatch, tmp_path):
    pane = SimpleNamespace(id="%1", pid="1234")
    monkeypatch.setattr(tmux, "find_pane", lambda target: pane)
    monkeypatch.setattr(server, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "IMG_DIR", tmp_path)
    monkeypatch.setattr(
        server.app.state, "watcher", SimpleNamespace(request_reparse=lambda p: None), raising=False,
    )
    return TestClient(server.app)


def test_compose_preserves_text_image_order_and_sends_one_enter(client, monkeypatch):
    events = []
    monkeypatch.setattr(tmux, "_run", lambda args: events.append(args[-1]))
    monkeypatch.setattr(server, "_deliver_image", lambda *a: events.append("IMAGE"))
    response = client.post("/api/panes/alias/compose", files=[
        ("text", (None, "before")), ("image", ("test.png", b"image", "image/png")),
        ("text", (None, "after")),
    ])
    assert response.status_code == 200
    assert events == ["before", "IMAGE", "after", "Enter"]


def test_all_segments_are_validated_before_typing(client, monkeypatch):
    events = []
    monkeypatch.setattr(tmux, "_run", lambda args: events.append(args))
    response = client.post("/api/panes/%1/compose", files=[
        ("text", (None, "must not be typed")),
        ("image", ("bad.txt", b"not an image", "text/plain")),
    ])
    assert response.status_code == 415
    assert events == []


def test_composer_identity_failure_returns_conflict(client, monkeypatch):
    monkeypatch.setattr(tmux, "pane_pid", lambda p: "replacement")
    events = []
    monkeypatch.setattr(tmux, "_run", lambda args: events.append(args))
    response = client.post("/api/panes/%1/compose", files=[("text", (None, "draft"))])
    assert response.status_code == 409
    assert events == []


def test_plain_send_identity_failure_is_not_reported_as_success(client, monkeypatch):
    def changed(*a, **kw):
        raise tmux.PaneChangedError("Pane changed")

    monkeypatch.setattr(tmux, "send_keys", changed)
    response = client.post("/api/panes/%1/send", json={"keys": "draft"})
    assert response.status_code == 409


def test_concurrent_sender_cannot_split_a_composer(monkeypatch):
    events = []
    image_started, release_image, contender_started = (threading.Event() for _ in range(3))
    monkeypatch.setattr(tmux, "_run", lambda args: events.append(args[-1]))

    def image(*args):
        image_started.set()
        assert release_image.wait(3)
        events.append("IMAGE")

    monkeypatch.setattr(server, "_deliver_image", image)
    errors = []

    def compose():
        try:
            server._deliver_composer("%1", "1234", ["before", (b"png", "/tmp/x"), "after"])
        except Exception as error:  # noqa: BLE001 - assert thread failures on the main thread
            errors.append(error)

    def contender():
        contender_started.set()
        tmux.send_keys("%1", "other")

    first = threading.Thread(target=compose)
    second = threading.Thread(target=contender)
    first.start()
    try:
        assert image_started.wait(3)
        second.start()
        assert contender_started.wait(3)
        assert events == ["before"]
    finally:
        release_image.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert events == ["before", "IMAGE", "after", "Enter", "other", "Enter"]


def test_image_failure_never_submits_partial_draft(client, monkeypatch):
    events = []
    monkeypatch.setattr(tmux, "_run", lambda args: events.append(args[-1]))

    def fail(*args):
        raise RuntimeError("clipboard failed")

    monkeypatch.setattr(server, "_deliver_image", fail)
    with pytest.raises(RuntimeError, match="clipboard failed"):
        client.post("/api/panes/%1/compose", files=[
            ("text", (None, "before")), ("image", ("test.png", b"image", "image/png")),
        ])
    assert events == ["before"]


@pytest.mark.parametrize("endpoint", ["image", "compose"])
def test_staging_failure_is_audited(client, monkeypatch, endpoint):
    from fastapi import HTTPException

    audits = []
    monkeypatch.setattr(server, "_audit", lambda *a, **kw: audits.append(kw))

    def refused(*args):
        raise HTTPException(500, "unsafe staging directory")

    monkeypatch.setattr(server, "_stage_image", refused)
    field = "file" if endpoint == "image" else "image"
    response = client.post(f"/api/panes/%1/{endpoint}",
                           files=[(field, ("test.png", b"image", "image/png"))])
    assert response.status_code == 500
    assert len(audits) == 1
    assert audits[0]["outcome"].startswith("error:")


def test_unknown_composer_pane_is_audited(client, monkeypatch):
    audits = []
    monkeypatch.setattr(server, "_audit", lambda *a, **kw: audits.append(kw))
    monkeypatch.setattr(tmux, "find_pane", lambda p: None)
    response = client.post("/api/panes/%404/compose", files=[("text", (None, "draft"))])
    assert response.status_code == 404
    assert len(audits) == 1
    assert "pane not found" in audits[0]["outcome"]
