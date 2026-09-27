"""Live Mode's session-history tools: find_sessions and resume_session.

What must hold: the model only ever names a session id, and everything that reaches
tmux (argv, directory) comes from the agent-history index; a session that is already
running is never started twice; and without agent-history the tools don't exist."""

import stat

import pytest

import openbus.live as L
from openbus import agent_history, tmux
from openbus.tmux import Pane
from tests.test_live_mode import _FC, _WS, _run, _Session, _Watcher

LIVE = {
    "session_id": "live-1", "title": "tmuxrc live mode", "cwd": "/repo",
    "last_active": "2026-09-05T15:29:22Z", "resume_argv": ["claude", "--resume", "live-1"],
}


@pytest.fixture
def history(monkeypatch, tmp_path):
    """agent-history stubbed at its Python boundary; returns a dict of sessions to serve
    and the list of windows opened."""
    sessions, opened = {}, []
    monkeypatch.setattr(agent_history, "get", lambda sid: sessions.get(sid))
    monkeypatch.setattr(L.os.path, "isdir", lambda p: p == "/repo")
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **k: None)
    monkeypatch.setattr(tmux, "list_panes", lambda: [
        Pane("other", "1", "w", "0", "%9", "zsh", "", cwd="/elsewhere",
             window_active="1", pane_active="1"),
        Pane("work", "2", "w", "0", "%3", "zsh", "", cwd="/repo/sub"),
    ])
    monkeypatch.setattr(tmux, "new_window",
                        lambda *a: opened.append(a) or "%40")
    return sessions, opened


def _call(name, args):
    ws, session = _WS(), _Session()
    _run(L._handle_tool_call(ws, session, _FC(name=name, args=args), _Watcher(), "tester"))
    return ws, session.responses[0].response


def test_resume_opens_the_indexed_command_in_its_directory(history):
    sessions, opened = history
    sessions["live-1"] = LIVE
    ws, r = _call("resume_session", {"session_id": "live-1"})
    assert r == {"status": "opened", "pane_id": "%40", "window": "tmuxrc live mode"}
    # argv and cwd are the index's, in the tmux session already working in that repo.
    assert opened == [("work", "tmuxrc live mode", ["claude", "--resume", "live-1"], "/repo")]
    assert any(m["type"] == "typed" and m["pane_id"] == "%40" for m in ws.sent)


def test_resume_never_starts_a_second_copy(history):
    sessions, opened = history
    sessions["live-1"] = {**LIVE, "running": {"pid": 5, "tmux_pane": "%1"}}
    _, r = _call("resume_session", {"session_id": "live-1"})
    assert r == {"status": "already_running", "pane_id": "%1", "pane": "work"}
    sessions["live-1"] = {**LIVE, "running": {"pid": 5}}  # an IDE or bare terminal
    _, r = _call("resume_session", {"session_id": "live-1"})
    assert r["status"] == "rejected"
    assert opened == []


@pytest.mark.parametrize(("entry", "args"), [
    (None, {"session_id": "nope"}),  # unknown to the index
    ({**LIVE, "resume_argv": ["sh", "-c", "rm -rf ~"]}, {"session_id": "live-1"}),
    ({**LIVE, "resume_argv": []}, {"session_id": "live-1"}),  # a subagent
    ({**LIVE, "cwd": "/gone"}, {"session_id": "live-1"}),
    (LIVE, {"session_id": "live-1", "command": "claude"}),  # extra args
    (LIVE, {"session_id": 7}),
])
def test_resume_rejects(history, entry, args):
    sessions, opened = history
    if entry:
        sessions["live-1"] = entry
    _, r = _call("resume_session", args)
    assert r["status"] == "rejected"
    assert opened == []


def test_find_sessions_returns_routing_hints_only(monkeypatch):
    monkeypatch.setattr(agent_history, "resolve", lambda q: [{
        "repo": "/home/u/src/tmux-rc", "score": 9,
        "sessions": [
            {**LIVE, "running": {"pid": 5, "tmux_pane": "%1"}, "prs": ["x"], "source": "/s"},
            {**LIVE, "session_id": "old", "title": ""},
        ],
    }])
    _, r = _call("find_sessions", {"query": "live mode"})
    assert r == {"status": "ok", "results": [{"repo": "tmux-rc", "sessions": [
        {"session_id": "live-1", "title": "tmuxrc live mode", "last_active": "2026-09-05",
         "running_in": "work", "pane_id": "%1"},
        {"session_id": "old", "title": "(untitled)", "last_active": "2026-09-05"},
    ]}]}
    monkeypatch.setattr(agent_history, "resolve", lambda q: None)
    assert _call("find_sessions", {"query": "x"})[1]["status"] == "error"
    assert _call("find_sessions", {"query": " "})[1]["status"] == "rejected"


def test_tools_offered_only_with_agent_history(monkeypatch):
    def names():
        return {f.name for t in L._tools() for f in t.function_declarations}
    monkeypatch.setattr(agent_history, "binary", lambda: "/bin/agent-history")
    assert {"find_sessions", "resume_session"} <= names()
    monkeypatch.setattr(agent_history, "binary", lambda: None)
    assert names() == {"type_in_pane", "press_key"}


def test_client_runs_the_binary_with_a_literal_query(monkeypatch, tmp_path):
    # A stand-in agent-history that echoes its argv, to pin what the daemon passes.
    fake = tmp_path / "agent-history"
    fake.write_text('#!/bin/sh\nprintf \'{"projects":[{"argv":"%s"}]}\' "$*"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("TMUXRC_AGENT_HISTORY", str(fake))
    [p] = agent_history.resolve("-all live mode")
    assert p["argv"].endswith("-- -all live mode")  # never read as a flag

    fake.write_text("#!/bin/sh\nexit 1\n")
    assert agent_history.get("x") is None
    monkeypatch.setenv("TMUXRC_AGENT_HISTORY", str(tmp_path / "missing"))
    assert agent_history.binary() is None and agent_history.resolve("q") is None


def test_new_window_passes_argv_without_a_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or "%7\n")
    assert tmux.new_window("work", "n", ["claude", "--resume", "a b;c"], "/repo") == "%7"
    assert calls[-1][-6:] == ["-n", "n", "--", "claude", "--resume", "a b;c"]
    assert calls[-1][calls[-1].index("-c") + 1] == "/repo"
    tmux.new_window("work", "n", "codex --yolo")  # a configured launcher: shell string
    assert calls[-1][-2:] == ["--", "codex --yolo"]
    assert calls[-1][calls[-1].index("-c") + 1] == "#{session_path}"

