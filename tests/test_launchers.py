"""The dock's "+" launcher menu: config parsing and the new-window endpoint.

The security property under test: the HTTP surface accepts only a LABEL and the
daemon resolves it against ITS config — a request naming anything not configured is
refused, so the endpoint can never be handed an arbitrary command string.
"""

import json

import pytest
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


# The default launchers are real agent binaries, and the pre-flight check in
# new_window/launchers means a suite that used them unstubbed would assert on whatever
# happens to be installed on the runner — green here, red in CI, which installs tmux and
# nothing else. Tests that are not ABOUT resolution stub it to "everything resolves".
def _resolves(monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda c, path=None: c)


# `_unavailable` is given the tmux server's PATH by its callers and declines outright when
# that is None (not knowing is not evidence — see the function). These tests are about the
# PARSING, so they hand it a real-but-empty list: tmux answered, and it has nothing.
NO_PATH = "/nonexistent-tmux-path-for-tests"


def unavailable(command):
    return S._unavailable(command, NO_PATH)


@pytest.fixture(autouse=True)
def _pin_tmux_path(monkeypatch):
    """Consulting the tmux server's PATH means shelling out to a real tmux, so without
    this every test here would quietly agree with whatever server the machine happens to
    be running and with whoever started it — passing locally and failing in CI, which
    installs tmux and starts nothing. Pinned to a known-empty list; the handful of tests
    that are ABOUT the tmux side override it."""
    monkeypatch.setattr(T, "server_path", lambda: NO_PATH)


def test_new_window_runs_configured_command(monkeypatch):
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    _resolves(monkeypatch)
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
    _resolves(monkeypatch)
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
    monkeypatch.setattr(S.shutil, "which", lambda c, path=None: None)
    monkeypatch.setattr(T, "_run", lambda argv: (_ for _ in ()).throw(AssertionError("must not run")))
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "work", "launcher": "Codex"})
    assert r.status_code == 400
    assert "codex" in r.json()["detail"] and "PATH" in r.json()["detail"]


# The daemon does not start the tmux server, so the two do not share a PATH: a session
# opened from a login shell can run a launcher the daemon cannot even find. Refusing on
# the daemon's list alone would block exactly the commands this feature exists to serve.
def test_new_window_allows_a_command_only_tmux_can_find(monkeypatch):
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps(
        [{"label": "Codex", "command": "codex", "icon": "codex"}]))
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    monkeypatch.setattr(T, "server_path", lambda: "/opt/agents/bin")
    monkeypatch.setattr(S.shutil, "which",
                        lambda c, path=None: "/opt/agents/bin/codex" if path else None)
    opened = []
    monkeypatch.setattr(T, "_run", lambda argv: (opened.append(argv), "%7")[1])
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "work", "launcher": "Codex"})
    assert r.status_code == 200 and opened, "a launcher tmux can run must not be refused"


def test_new_window_allows_a_command_when_tmux_cannot_say(monkeypatch):
    """No server yet, a wedged one, an old tmux: then half the answer is known, and half
    the answer is the daemon's PATH alone — the thing that was wrong before. A launcher
    must not be refused on it."""
    monkeypatch.setenv("TMUXRC_LAUNCHERS", json.dumps(
        [{"label": "Codex", "command": "codex", "icon": "codex"}]))
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    monkeypatch.setattr(T, "server_path", lambda: None)
    monkeypatch.setattr(S.shutil, "which", lambda c, path=None: None)
    monkeypatch.setattr(T, "_run", lambda argv: "%7")
    client = TestClient(S.app)
    assert client.post("/api/windows", json={"session": "work", "launcher": "Codex"}).status_code == 200
    assert "unavailable" not in client.get("/api/launchers").json()["launchers"][0]


def test_launchers_endpoint_reads_the_tmux_path_once(monkeypatch):
    """Three entries, one tmux call: it is the same answer for all of them, and this
    endpoint is now re-read every time a menu opens."""
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    monkeypatch.setattr(S.shutil, "which", lambda c, path=None: c)
    calls = []
    monkeypatch.setattr(T, "server_path", lambda: (calls.append(1), "/opt/bin")[1])
    client = TestClient(S.app)
    assert len(client.get("/api/launchers").json()["launchers"]) == 3
    assert len(calls) == 1


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


def test_unavailable_skips_env_assignments_and_gives_up_on_odd_commands():
    assert "no-such-command-xyz" in unavailable("NOPE_XYZ=1 no-such-command-xyz")
    assert unavailable("FOO=1 sh -c true") is None  # `sh` resolves; the assignment isn't argv[0]
    # Everything it can't confidently decide is left to the shell rather than guessed at.
    assert unavailable('"unbalanced') is None
    assert unavailable("FOO=1") is None
    assert unavailable("a | b") is None
    # A newline separates commands exactly as `;` does, but shlex would eat it as
    # whitespace — so `cd /tmp\nclaude`, a launcher that works, would be judged on the
    # shell builtin `cd` and refused. Control characters are shell syntax, not argv.
    assert unavailable("cd /tmp\nclaude") is None
    # A backslash escapes, and the non-posix split keeps it as a literal instead — so
    # this would otherwise split three ways and judge "baz" as the command.
    assert unavailable("FOO=bar\\ baz no-such-command-xyz") is None
    # `exec claude` replaces the shell with the agent so the pane dies with it. `exec` is
    # a builtin, so argv[0] is the word after it, not the one which() would be asked for.
    assert unavailable("exec no-such-command-xyz").startswith("no-such-command-xyz")
    assert unavailable("exec sh") is None
    # Assignments prefix `exec`; a word AFTER it is exec's argument, so `exec FOO=1 sh`
    # genuinely makes sh hunt for a file called "FOO=1" (verified against sh). Reporting
    # that is the honest answer — stripping it would wave through a launcher that fails.
    assert unavailable("FOO=1 exec sh") is None
    assert unavailable("exec FOO=1 sh").startswith("FOO=1")
    # exec takes options of its own (`exec -a name cmd`, `exec -- cmd`). Resolving one as
    # if it were the command would refuse a launcher that works, which is the one outcome
    # this must never produce — so an option after `exec` ends the judging.
    assert unavailable("exec -a agent no-such-command-xyz") is None
    assert unavailable("exec -- no-such-command-xyz") is None
    assert unavailable("command no-such-command-xyz").startswith("no-such-command-xyz")
    assert unavailable("command -p sh") is None
    # A PATH assignment changes the very lookup we would be doing, so don't do it.
    assert unavailable("PATH=/opt/x/bin no-such-command-xyz") is None
    # A relative path is resolved by tmux against the SESSION's directory (new_window
    # -c #{session_path}), not the daemon's cwd — checking it here would answer about a
    # different file, so it isn't checked at all.
    assert unavailable("./no-such-command-xyz") is None


def test_unavailable_judges_argv0_despite_ordinary_arguments():
    # Only argv[0] is resolved, so only argv[0] has to be a plain word. Holding the rest
    # to the same spelling meant one colon abandoned the check — and a config with a URL
    # or a versioned model name in it is exactly the kind whose binary lives off PATH.
    why = unavailable("no-such-command-xyz --endpoint https://api.example.com")
    assert why and "no-such-command-xyz" in why
    assert unavailable("sh --model claude-3:latest --x=1,2") is None
    # Shell syntax anywhere still means this is not a plain argv to judge.
    for line in ("sh --pipe | tee", "sh $(hostname)", "sh *.py", "sh 'quoted'"):
        assert unavailable(line) is None, line


def test_new_window_wakes_the_watcher_for_the_pane_it_made(monkeypatch):
    """The latency half of this: without the wake the new pane waits out a poll interval
    before anything publishes it, which is the delay the endpoint exists to remove."""
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    _resolves(monkeypatch)
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    monkeypatch.setattr(T, "_run", lambda argv: "%42")
    asked = []
    S.app.state.watcher = type("W", (), {"request_reparse": lambda self, pane_id: asked.append(pane_id)})()
    try:
        client = TestClient(S.app)
        assert client.post("/api/windows", json={"session": "work", "launcher": "Claude"}).status_code == 200
        assert asked == ["%42"]  # the id just created, not the one the user was on
    finally:
        del S.app.state.watcher


def test_new_window_succeeds_with_no_watcher_running(monkeypatch):
    """The window already exists by then, so a watcher that isn't up must not turn a
    success into a 500 — the next ordinary tick finds the pane regardless."""
    monkeypatch.delenv("TMUXRC_LAUNCHERS", raising=False)
    _resolves(monkeypatch)
    monkeypatch.setattr(T, "list_panes", lambda: [_fake_pane()])
    monkeypatch.setattr(T, "_run", lambda argv: "%42")
    assert getattr(S.app.state, "watcher", None) is None
    client = TestClient(S.app)
    r = client.post("/api/windows", json={"session": "work", "launcher": "Claude"})
    assert r.status_code == 200 and r.json()["pane_id"] == "%42"


def test_unavailable_distinguishes_a_bad_path_from_a_bad_name(tmp_path):
    # Telling someone whose config already holds an absolute path to "use an absolute
    # path" sends them to fix the one thing that isn't wrong.
    assert "PATH" in unavailable("no-such-command-xyz")
    bad = tmp_path / "nope"
    assert "PATH" not in unavailable(str(bad))
    assert str(bad) in unavailable(str(bad))


def test_unavailable_expands_tilde(monkeypatch, tmp_path):
    # `~/.nvm/.../codex` is the escape hatch the error message itself recommends, so it
    # has to be the one the check can actually resolve. shlex/which do no expansion.
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert unavailable("~/codex --yolo") is None
    why = unavailable("~/absent")
    assert why and "~/absent" in why  # named as configured, not as expanded
    # sh does not expand `~` inside quotes, so neither may we: the shell would look for a
    # literal "~/codex" and fail. Quoting is already a "can't say", and must stay one.
    assert unavailable("'~/codex'") is None


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
