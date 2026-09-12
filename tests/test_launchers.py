"""The dock's "+" launcher menu: config parsing and the new-window endpoint.

The security property under test: the HTTP surface accepts only a LABEL and the
daemon resolves it against ITS config — a request naming anything not configured is
refused, so the endpoint can never be handed an arbitrary command string.
"""

import json

from fastapi.testclient import TestClient

import openbus.server as S
import openbus.tmux as T


def test_default_launchers(monkeypatch):
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    labels = [e["label"] for e in S._launchers()]
    assert labels == ["Claude", "Codex", "Gemini"]


def test_launchers_inline_json_override(monkeypatch):
    cfg = [
        {"label": "Claude (Fable)", "command": "claude --model fable", "icon": "claude"},
        {"label": "Claude (Bedrock)", "command": "CLAUDE_CODE_USE_BEDROCK=1 claude", "icon": "claude"},
    ]
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps(cfg))
    got = S._launchers()
    assert [e["label"] for e in got] == ["Claude (Fable)", "Claude (Bedrock)"]
    assert got[0]["command"] == "claude --model fable"


def test_launchers_file_override(monkeypatch, tmp_path):
    p = tmp_path / "launchers.json"
    p.write_text(json.dumps([{"label": "Opus", "command": "claude --model opus"}]))
    monkeypatch.setenv("TMUXRC_LAUNCHERS", str(p))
    assert [e["label"] for e in S._launchers()] == ["Opus"]


def test_launchers_bad_config_falls_back(monkeypatch):
    monkeypatch.setenv("TMUXRC_LAUNCHERS", "not json at all")
    assert [e["label"] for e in S._launchers()] == ["Claude", "Codex", "Gemini"]
    monkeypatch.setenv("TMUXRC_LAUNCHERS", "[]")  # valid JSON, no valid entries
    assert [e["label"] for e in S._launchers()] == ["Claude", "Codex", "Gemini"]


def _fake_pane(session="work"):
    return T.Pane(session, "1", "w", "0", "%9", "bash", "t")


def test_new_window_runs_configured_command(monkeypatch):
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: (calls.append(argv), "%42")[1])
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "work", "launcher": "Claude"})
    assert r.status_code == 200 and r.json()["pane_id"] == "%42"
    argv = next(a for a in calls if a[0] == "new-window")
    assert "-d" in argv
    assert argv[argv.index("-t") + 1] == "work:"
    assert argv[argv.index("-c") + 1] == "#{session_path}"
    assert argv[-1] == "claude"


def test_new_window_refuses_unknown_launcher(monkeypatch):
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    monkeypatch.setattr(T, "_run", lambda argv: (_ for _ in ()).throw(AssertionError("must not run")))
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "work", "launcher": "rm -rf /"})
    assert r.status_code == 404


def test_new_window_refuses_unknown_session(monkeypatch):
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane("work")])
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "nope", "launcher": "Claude"})
    assert r.status_code == 404


def test_launchers_endpoint_omits_commands(monkeypatch):
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    client = TestClient(S.app)
    got = client.get("/api/launchers").json()["launchers"]
    assert got and all(set(e) == {"label", "icon"} for e in got)


# A launcher whose command isn't on the DAEMON's PATH used to create a window that died
# in milliseconds; the phone then reported the follow-up select() as a focus failure.
# Refusing up front is what makes the real cause (PATH) visible.


def test_new_window_refuses_command_not_on_path(monkeypatch):
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps(
        [{"label": "Codex", "command": "codex --yolo", "icon": "codex"}]))
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    monkeypatch.setattr(S.shutil, "which", lambda _: None)
    monkeypatch.setattr(T, "_run", lambda argv: (_ for _ in ()).throw(AssertionError("must not run")))
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "work", "launcher": "Codex"})
    assert r.status_code == 400
    assert "codex" in r.json()["detail"] and "PATH" in r.json()["detail"]


def test_new_window_allows_absolute_path_that_exists(monkeypatch, tmp_path):
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps(
        [{"label": "Codex", "command": f"{exe} --yolo"}]))
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    monkeypatch.setattr(T, "_run", lambda argv: "%7")
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "work", "launcher": "Codex"})
    assert r.status_code == 200 and r.json()["pane_id"] == "%7"


def test_missing_skips_env_assignments_and_gives_up_on_odd_commands():
    assert S._missing("NOPE_NOT_A_CMD_XYZ=1 no-such-command-xyz") == "no-such-command-xyz"
    assert S._missing("FOO=1 sh -c true") is None  # `sh` resolves; the assignment is not argv[0]
    # Unparseable / not a plain word: left to the shell rather than guessed at.
    assert S._missing('"unbalanced') is None
    assert S._missing("FOO=1") is None
    assert S._missing("a | b") is None


def test_launchers_endpoint_flags_unavailable(monkeypatch):
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps([
        {"label": "Claude", "command": "sh", "icon": "claude"},
        {"label": "Codex", "command": "no-such-command-xyz", "icon": "codex"},
    ]))
    client = TestClient(S.app)
    got = client.get("/api/launchers").json()["launchers"]
    assert [e["label"] for e in got] == ["Claude", "Codex"]  # never hidden
    assert "unavailable" not in got[0]
    assert "no-such-command-xyz" in got[1]["unavailable"]
    assert all("command" not in e for e in got)  # commands still never leave the daemon
