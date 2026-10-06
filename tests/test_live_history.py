"""Live Mode's session-history tools: find_sessions and resume_session.

What must hold: the model only ever names a session id, and everything that reaches
tmux (argv, directory) comes from the agent-history index; a session that is already
running is never started twice; and without agent-history the tools don't exist."""

import asyncio
import logging
import os
import shutil
import stat
import subprocess

import pytest

import openbus.live as L
from openbus import agent_history, tmux
from openbus.tmux import Pane
from tests.test_live_mode import _FC, _METER, _WS, _run, _Session, _Watcher

_REAL_ANCESTORS = L._ancestors  # before the autouse stub replaces it

LIVE = {
    "harness": "claude", "session_id": "live-1", "title": "tmuxrc live mode", "cwd": "/repo",
    "last_active": "2026-09-05T15:29:22Z", "resume_argv": ["claude", "--resume", "live-1"],
}


@pytest.fixture(autouse=True)
def _fresh_resumes(monkeypatch):
    monkeypatch.setattr(L, "_resumed", {})
    monkeypatch.setattr(L, "_resume_lock", asyncio.Lock())  # each test runs its own loop
    # Every registered process runs under the stubbed pane pid (conftest's 1234).
    monkeypatch.setattr(L, "_ancestors", lambda pid: [pid, 1234])
    # The tools are offered (and callable) only with a binary; tests stub its calls.
    monkeypatch.setenv("TMUXRC_AGENT_HISTORY", shutil.which("true"))
    monkeypatch.delenv("TMUXRC_TARGET", raising=False)


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
                             _METER))
    return ws, session.responses[0][1]


@pytest.mark.parametrize("argv", [["claude", "--resume", "live-1"], ["codex", "resume", "live-1"],
                                  ["omp", "--resume", "live-1"]])
def test_resume_opens_the_indexed_command_in_its_directory(history, argv):
    sessions, opened = history
    sessions["live-1"] = {**LIVE, "resume_argv": argv}
    ws, r = _call("resume_session", {"session_id": "live-1"})
    assert r == {"status": "opened", "pane_id": "%40", "window": "tmuxrc live mode"}
    # argv and cwd are the index's, in the tmux session already working in that repo.
    assert opened == [("work", "tmuxrc live mode", argv, "/repo")]
    assert any(m["type"] == "typed" and m["pane_id"] == "%40" for m in ws.sent)


def test_resume_is_idempotent_until_the_session_registers(history):
    # Right after a launch the registry doesn't list it yet; a repeat must not relaunch.
    sessions, opened = history
    sessions["live-1"] = LIVE

    async def twice():
        return await asyncio.gather(*(
            L._handle_tool_call(_WS(), s, _FC(name="resume_session", args={"session_id": "live-1"}),
                                _Watcher(), _METER)
            for s in (a, b)))
    a, b = _Session(), _Session()
    _run(twice())
    statuses = sorted(x.responses[0][1]["status"] for x in (a, b))
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


@pytest.mark.parametrize("broken", ["list_panes", "new_window"])
def test_tmux_failure_is_reported_not_raised(history, monkeypatch, broken):
    # An escaped exception would end the Live receiver, and with it the voice session.
    history[0]["live-1"] = LIVE

    def fail(*a):
        raise subprocess.CalledProcessError(1, "tmux")
    monkeypatch.setattr(tmux, broken, fail)
    assert _call("resume_session", {"session_id": "live-1"})[1]["status"] == "error"


def test_every_call_is_audited_in_the_journal_and_otel(history, monkeypatch, caplog):
    # One record per call, whatever the outcome: opened, failed, or refused unoffered.
    sessions, _ = history
    sessions["live-1"], sessions["live-2"] = LIVE, {**LIVE, "session_id": "live-2"}
    emitted = []
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **k: emitted.append(k))
    monkeypatch.setattr(tmux, "server_uid", lambda: "u")
    caplog.set_level(logging.INFO, logger="openbus.server.audit")
    _call("resume_session", {"session_id": "live-1"})
    monkeypatch.setattr(tmux, "new_window", lambda *a: 1 / 0)
    _call("resume_session", {"session_id": "live-2"})
    # The window opened but the phone vanished before hearing so: still on the record.
    sessions["live-3"], gone = {**LIVE, "session_id": "live-3"}, _WS()
    monkeypatch.setattr(tmux, "new_window", lambda *a: "%41")

    async def drop(obj):
        raise ConnectionError
    gone.send_json = drop
    with pytest.raises(ConnectionError):
        _run(L._handle_tool_call(gone, _Session(), _FC(name="resume_session",
             args={"session_id": "live-3"}), _Watcher(), _METER))
    monkeypatch.setattr(agent_history, "binary", lambda: None)
    _call("resume_session", {"session_id": "live-1"})
    ok, err, aborted, refused = emitted
    assert aborted["outcome"] == "error: aborted" and aborted["pane_uid"] == "u:%41"
    assert ok.items() >= {
        "action": "live_resume_session", "pane_uid": "u:%40", "actor": "tester",
        "outcome": "ok", "session": "s1", "session_id": "live-1", "tool": "claude",
        "cwd": "/repo", "window": "tmuxrc live mode", "tmux_session": "work",
    }.items()
    assert isinstance(ok["latency_ms"], int) and ok["provider"] == _METER.model.backend
    assert err["outcome"] == "error: could not open a window" and "division" in err["detail"]
    assert refused["outcome"] == "rejected: session history not available"
    lines = [r.getMessage() for r in caplog.records if r.name == "openbus.server.audit"]
    assert lines[0].startswith("AUDIT live_resume_session pane=%40 by tester ")
    assert "session_id='live-1'" in lines[0] and "window='tmuxrc live mode'" in lines[0]
    assert lines[1].endswith("[error: could not open a window]")
    assert lines[3].endswith("[rejected: session history not available]")


@pytest.mark.parametrize(("qsdebug", "audit_keys"), [(False, True), (True, True), (True, False)])
def test_spoken_content_is_recorded_only_under_qsdebug(monkeypatch, caplog, qsdebug, audit_keys):
    # A search query is the user's speech: like a transcript, it reaches the journal and
    # OTel only under QSDEBUG, and TMUXRC_AUDIT_KEYS=0 still withholds it everywhere.
    monkeypatch.setattr(agent_history, "resolve", lambda q: [])
    monkeypatch.setattr(L.telemetry, "QSDEBUG", qsdebug)
    monkeypatch.setattr(L.telemetry, "AUDIT_KEYS", audit_keys)
    monkeypatch.setattr(tmux, "server_uid", lambda: "u")
    records = []
    monkeypatch.setattr(L.telemetry, "_emit_record", lambda body, attrs, *a: records.append(attrs))
    caplog.set_level(logging.INFO, logger="openbus.server.audit")
    _call("find_sessions", {"query": "my sudo password"})
    shown = qsdebug and audit_keys
    assert ("my sudo password" in caplog.text) is shown
    assert (records[0].get("keys") == "my sudo password") is shown
    assert records[0]["results"] == 0


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


def test_daemon_held_codex_thread_is_found_by_its_status_bar(history):
    # Codex's app-server daemon holds the rollout, so agent-history names no pane; a
    # status bar configured with session-id shows the thread id.
    sessions, opened = history
    sid = "0000aaaa-0000-7000-8000-000000000001"
    sessions[sid] = {**LIVE, "harness": "codex", "session_id": sid,
                     "resume_argv": ["codex", "resume", sid], "running": {"pid": 5}}
    w = _Watcher()
    w.digest = lambda: [{**d, "tool": "codex"} for d in _Watcher.digest(w)]

    def show(pane, head):
        w.snapshots[pane] = [{"id": "s", "ts": 1.0,
                              "text": f"output\n\n› \n\n  {head} · gpt-6 medium · /repo"}]

    def resume():
        return _call("resume_session", {"session_id": sid}, w)[1]
    for head in (sid, f"tmuxrc live mode · gpt-6 medium · {sid}"):  # before or after the model
        show("%1", head)
        assert resume() == {"status": "already_running", "pane_id": "%1", "pane": "work"}
    show("%1", "tmuxrc live mode")  # a name can belong to another live thread
    assert resume()["status"] == "rejected"
    show("%1", sid)
    w.digest = _Watcher().digest  # a claude pane printing a Codex footer isn't Codex
    assert resume()["status"] == "rejected"
    # The id in output, not the status bar, isn't evidence either.
    w.digest = lambda: [{**d, "tool": "codex"} for d in _Watcher.digest(w)]
    w.snapshots = {"%1": [{"id": "s", "ts": 1.0, "text": f"{sid} · resumed ok"}]}
    assert resume()["status"] == "rejected"  # running out of reach: never a second copy
    assert opened == []


def test_registry_pane_counts_only_if_its_process_runs_there(history, monkeypatch):
    # The registry's %N may belong to another tmux server; here it's an unrelated pane.
    sessions, opened = history
    sessions["live-1"] = {**LIVE, "running": {"pid": 5, "tmux_pane": "%1"}}
    monkeypatch.setattr(L, "_ancestors", lambda pid: [pid])
    assert _call("resume_session", {"session_id": "live-1"})[1]["status"] == "rejected"
    assert opened == []
    monkeypatch.setattr(L, "_ancestors", _REAL_ANCESTORS)
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: str(os.getppid()))
    assert L._pane_of({"pid": os.getpid(), "tmux_pane": "%1"}) == "%1"  # real /proc walk
    monkeypatch.setattr(tmux, "pane_pid", lambda pane_id: "999999999")
    assert L._pane_of({"pid": os.getpid(), "tmux_pane": "%1"}) is None


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
            {**LIVE, "running": {"pid": 5, "tmux_pane": "%1"}, "prs": ["x"], "source": "/s",
             "matched": ["live", "mode"]},
            {**LIVE, "session_id": "old", "title": "", "running_unknown": True},
            {**LIVE, "session_id": "new", "running": {"pid": 6, "tmux_pane": "%77"}},
            {**LIVE, "session_id": "ide", "running": {"pid": 7}},
            {**LIVE, "session_id": "cx", "harness": "codex"},
        ],
    }])
    w = _Watcher()
    _, r = _call("find_sessions", {"query": "live mode"}, w)
    assert w.reparsed == ["%77"]  # the unpublished running pane is woken
    assert r == {"status": "ok", "results": [{"repo": "~/src/tmux-rc", "sessions": [
        {"session_id": "live-1", "tool": "claude", "title": "tmuxrc live mode",
         "last_active": "2026-09-05", "matched": ["live", "mode"], "running_in": "work",
         "pane_id": "%1"},
        {"session_id": "old", "tool": "claude", "title": "(untitled)",
         "last_active": "2026-09-05", "matched": [], "running_unknown": True},
        {"session_id": "new", "tool": "claude", "title": "tmuxrc live mode",
         "last_active": "2026-09-05", "matched": [], "running_in": "%77", "pane_id": "%77"},
        {"session_id": "ide", "tool": "claude", "title": "tmuxrc live mode",
         "last_active": "2026-09-05", "matched": [], "running_elsewhere": True},
        {"session_id": "cx", "tool": "codex", "title": "tmuxrc live mode",
         "last_active": "2026-09-05", "matched": []},
    ]}]}
    monkeypatch.setattr(agent_history, "resolve", lambda q: None)
    assert _call("find_sessions", {"query": "x"})[1]["status"] == "error"
    assert _call("find_sessions", {"query": " "})[1]["status"] == "rejected"


def test_tools_offered_only_with_agent_history(monkeypatch):
    def offered():
        names = {t["name"] for t in L.live_providers.tools()}
        # A call to a tool that wasn't offered is refused, not run.
        refused = _call("find_sessions", {"query": "x"})[1]["status"] == "rejected"
        assert refused == ("find_sessions" not in names)
        return names
    monkeypatch.setattr(agent_history, "resolve", lambda q: [])
    assert {"find_sessions", "resume_session"} <= offered()
    monkeypatch.setenv("TMUXRC_TARGET", "%3")  # single-pane mode can't address new windows
    assert offered() == {"type_in_pane", "press_key", "send_image_to_pane", "open_pane"}
    monkeypatch.delenv("TMUXRC_TARGET")
    monkeypatch.setattr(agent_history, "binary", lambda: None)
    assert offered() == {"type_in_pane", "press_key", "send_image_to_pane", "open_pane"}


def test_client_runs_the_binary_with_a_literal_query(monkeypatch, tmp_path):
    # A stand-in agent-history that echoes its argv, to pin what the daemon passes.
    fake = tmp_path / "agent-history"
    fake.write_text('#!/bin/sh\nprintf \'{"projects":[{"argv":"%s"}]}\' "$*"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("TMUXRC_AGENT_HISTORY", str(fake))
    [p] = agent_history.resolve("-all live mode")
    assert p["argv"].endswith("-- -all live mode")  # never read as a flag
    assert "-harness" not in p["argv"]  # every harness Live can resume is searched
    assert "codex" in agent_history.RESUMABLE

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
