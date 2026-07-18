"""Presence-aware image delivery (_deliver_image): a locked session cannot read the
clipboard (GNOME blocks unfocused reads), so delivery must fall back to typing the
staged path; unlocked sessions get the clipboard + Ctrl-V inline embed."""

from daemon import server

PNG = b"\x89PNG\r\n\x1a\n" + b"fakepixels"  # magic prefix: skips the Pillow transcode


def test_locked_session_types_the_path(monkeypatch):
    sent = []
    monkeypatch.setattr(server.tmux, "session_locked", lambda: True)
    monkeypatch.setattr(
        server.tmux, "set_clipboard_image",
        lambda png: (_ for _ in ()).throw(AssertionError("clipboard used while locked")),
    )
    monkeypatch.setattr(
        server.tmux, "send_keys",
        lambda pane, keys, **kw: sent.append((keys, kw.get("literal"))),
    )
    mode = server._deliver_image("%1", PNG, "/tmp/x.png")
    assert mode == "path"
    assert sent == [(" /tmp/x.png ", True)]  # spaced token, literal text, no Ctrl-V


def test_unlocked_session_pastes_inline(monkeypatch):
    sent = []
    monkeypatch.setattr(server.tmux, "session_locked", lambda: False)
    monkeypatch.setattr(server.tmux, "set_clipboard_image", lambda png: ["wl-copy"])
    monkeypatch.setattr(
        server.tmux, "send_keys",
        lambda pane, keys, **kw: sent.append((keys, kw.get("literal"))),
    )
    mode = server._deliver_image("%1", PNG, "/tmp/x.png")
    assert mode == "clipboard:wl-copy"
    assert sent == [("C-v", False)]  # key-name send, not literal text


def test_unlocked_but_no_clipboard_tool_falls_back_to_path(monkeypatch):
    sent = []
    monkeypatch.setattr(server.tmux, "session_locked", lambda: False)
    monkeypatch.setattr(server.tmux, "set_clipboard_image", lambda png: [])
    monkeypatch.setattr(
        server.tmux, "send_keys",
        lambda pane, keys, **kw: sent.append(keys),
    )
    mode = server._deliver_image("%1", PNG, "/tmp/x.png")
    assert mode == "path"
    assert sent == [" /tmp/x.png "]
