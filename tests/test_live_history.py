"""Live Mode's session-history tools: find_sessions and resume_session.

What must hold: the model only ever names a session id, and everything that reaches
tmux (argv, directory) comes from the agent-history index; a session that is already
running is never started twice; and without agent-history the tools don't exist."""

import asyncio
import shutil
import stat
import subprocess

import pytest

import openbus.live as L
from openbus import agent_history, tmux
from openbus.tmux import Pane
from tests.test_live_mode import _FC, _WS, _run, _Session, _Watcher

LIVE = {
    "session_id": "live-1", "title": "tmuxrc live mode", "cwd": "/repo",
    "last_active": "2026-09-05T15:29:22Z", "resume_argv": ["claude", "--resume", "live-1"],
}


@pytest.fixture(autouse=True)
def _fresh_resumes(monkeypatch):
    monkeypatch.setattr(L, "_resumed", {})
    monkeypatch.setattr(L, "_resume_lock", asyncio.Lock())  # each test runs its own loop


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


def _call(name, args, watcher=None):
    ws, session = _WS(), _Session()
    _run(L._handle_tool_call(ws, session, _FC(name=name, args=args), watcher or _Watcher(),
                             "tester"))
    return ws, session.responses[0].response


def test_resume_opens_the_indexed_command_in_its_directory(history):
    sessions, opened = history
    sessions["live-1"] = LIVE
    ws, r = _call("resume_session", {"session_id": "live-1"})
    assert r == {"status": "opened", "pane_id": "%40", "window": "tmuxrc live mode"}
    # argv and cwd are the index's, in the tmux session already working in that repo.
    assert opened == [("work", "tmuxrc live mode", ["claude", "--resume", "live-1"], "/repo")]
    assert any(m["type"] == "typed" and m["pane_id"] == "%40" for m in ws.sent)


def test_resume_is_idempotent_until_the_session_registers(history):
    # Right after a launch the registry doesn't list it yet; a repeat must not relaunch.
    sessions, opened = history
    sessions["live-1"] = LIVE

    async def twice():
        return await asyncio.gather(*(
            L._handle_tool_call(_WS(), s, _FC(name="resume_session", args={"session_id": "live-1"}),
                                _Watcher(), "tester")
            for s in (a, b)))
    a, b = _Session(), _Session()
    _run(twice())
    statuses = sorted(x.responses[0].response["status"] for x in (a, b))
    assert statuses == ["already_running", "opened"]
    assert len(opened) == 1


def test_reservation_ends_if_the_launched_pane_is_gone(history, monkeypatch):
    # tmux reuses pane ids: if the launch died before registering and %40 now belongs
    # to another process, a retry must launch again, not point at a stranger.
    sessions, opened = history
    sessions["live-1"] = LIVE
    assert _call("resume_session", {"session_id": "live-1"})[1]["status"] == "opened"
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: "9999")
    assert _call("resume_session", {"session_id": "live-1"})[1]["status"] == "opened"
    assert len(opened) == 2


def test_launch_without_a_pid_is_a_failed_launch(history, monkeypatch):
    # A pane with no pid has already closed; there is no identity to reserve, and the
    # user should hear that the resume didn't take.
    sessions, _ = history
    sessions["live-1"] = LIVE
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: None)
    monkeypatch.setattr(L, "_LAUNCH_PID_RETRY_S", 0)
    assert _call("resume_session", {"session_id": "live-1"})[1]["status"] == "error"
    assert L._resumed == {}


def test_resume_never_starts_a_second_copy(history):
    sessions, opened = history
    sessions["live-1"] = {**LIVE, "running": {"pid": 5, "tmux_pane": "%1"}}
    _, r = _call("resume_session", {"session_id": "live-1"})
    assert r == {"status": "already_running", "pane_id": "%1", "pane": "work"}
    # A pane the watcher hasn't published yet is still the running one.
    sessions["live-1"] = {**LIVE, "running": {"pid": 5, "tmux_pane": "%77"}}
    w = _Watcher()
    _, r = _call("resume_session", {"session_id": "live-1"}, w)
    assert r == {"status": "already_running", "pane_id": "%77", "pane": "%77"}
    assert w.reparsed == ["%77"]  # woken so the follow-up type_in_pane finds it
    sessions["live-1"] = {**LIVE, "running": {"pid": 5}}  # an IDE or bare terminal
    _, r = _call("resume_session", {"session_id": "live-1"})
    assert r["status"] == "rejected"
    assert opened == []


@pytest.mark.parametrize(("entry", "args"), [
    (None, {"session_id": "nope"}),  # unknown to the index
    ({**LIVE, "resume_argv": ["sh", "-c", "rm -rf ~"]}, {"session_id": "live-1"}),
    ({**LIVE, "resume_argv": []}, {"session_id": "live-1"}),  # a subagent
    ({**LIVE, "cwd": "/gone"}, {"session_id": "live-1"}),
    ({**LIVE, "running_unknown": True}, {"session_id": "live-1"}),  # registry unreadable
    (LIVE, {"session_id": "live-1", "command": "claude"}),  # extra args
    (LIVE, {"session_id": 7}),
])
def test_resume_rejects(history, entry, args):
    sessions, opened = history
    if entry:
        sessions["live-1"] = entry
    _, r = _call("resume_session", args)
    assert r["status"] in {"rejected", "error"}
    assert opened == []


def test_find_sessions_returns_routing_hints_only(monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setattr(agent_history, "resolve", lambda q: [{
        "repo": "/home/u/src/tmux-rc", "score": 9,
        "sessions": [
            {**LIVE, "running": {"pid": 5, "tmux_pane": "%1"}, "prs": ["x"], "source": "/s"},
            {**LIVE, "session_id": "old", "title": "", "running_unknown": True},
            {**LIVE, "session_id": "new", "running": {"pid": 6, "tmux_pane": "%77"}},
            {**LIVE, "session_id": "ide", "running": {"pid": 7}},
        ],
    }])
    _, r = _call("find_sessions", {"query": "live mode"})
    assert r == {"status": "ok", "results": [{"repo": "~/src/tmux-rc", "sessions": [
        {"session_id": "live-1", "title": "tmuxrc live mode", "last_active": "2026-09-05",
         "running_in": "work", "pane_id": "%1"},
        {"session_id": "old", "title": "(untitled)", "last_active": "2026-09-05",
         "running_unknown": True},
        {"session_id": "new", "title": "tmuxrc live mode", "last_active": "2026-09-05",
         "running_in": "%77", "pane_id": "%77"},
        {"session_id": "ide", "title": "tmuxrc live mode", "last_active": "2026-09-05",
         "running_elsewhere": True},
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



@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_new_window_argv_reaches_the_program_unparsed(tmp_path, monkeypatch):
    # tmux execs a multi-argument command directly (no shell), which is what makes the
    # argv form safe for index data. Pinned against a real, private tmux server.
    sock = str(tmp_path / "tmux.sock")
    real = subprocess.run

    def private(argv, *a, **k):
        if argv[:1] == ["tmux"]:
            argv = ["tmux", "-S", sock, *argv[1:]]
        return real(argv, *a, **k)
    monkeypatch.setattr(tmux.subprocess, "run", private)
    out, marker = tmp_path / "argv", tmp_path / "PWNED"
    real(["tmux", "-S", sock, "new-session", "-d", "-s", "t"], check=True)
    try:
        tmux.new_window("t", "n", ["sh", "-c", 'printf "%s|" "$@" > "$0"', str(out),
                                   f"a b;touch {marker}", "$(x)"], str(tmp_path))
        for _ in range(50):
            if out.exists() and out.read_text():
                break
            subprocess.run(["sleep", "0.05"], check=True)
        assert out.read_text() == f"a b;touch {marker}|$(x)|"
        assert not marker.exists()
    finally:
        real(["tmux", "-S", sock, "kill-server"], check=False)
