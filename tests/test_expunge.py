"""Expunge deletes exactly one session's files, found by its id, and refuses rather than
guess. Every config dir here is a temp dir: never the real ~/.claude or ~/.codex."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbus import expunge, server, tmux
from openbus.expunge import Refused, Session

A, B = "0f0f0f0f-1111-4222-8333-444444444444", "0e0e0e0e-5555-4666-8777-888888888888"
CODEX_SCREEN = f"› \n  gpt-5.5 high · ~/src/example-org/api · {A}\n"


def files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def session(harness: str, root: Path) -> Session:
    return Session(harness, A, root, root.parent / "ah" / "index" / harness, 1, "1")


def jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def claude(tmp_path):
    """A Claude config dir and agent-history index holding sessions A and B."""
    root = tmp_path / "claude"
    for sid in (A, B):
        for rel in (f"projects/-src-api/{sid}.jsonl", f"projects/-src-api/{sid}/subagents/x.jsonl",
                    f"file-history/{sid}/f@v1", f"session-env/{sid}/hook.sh",
                    f"todos/{sid}-agent-{sid}.json", f"../ah/index/claude/{sid}.md",
                    f"../ah/index/claude/{sid}/agent.md",
                    f"../ah/index/claude/{sid}/agent-{sid[:4]}.md",  # a subagent with its own:
                    f"../ah/index/claude/agent-{sid[:4]}/grandchild.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x")
    jsonl(root / "history.jsonl", [{"display": "a", "sessionId": A},
                                   {"display": "b", "sessionId": B},
                                   {"display": A, "sessionId": B}])
    (root / "history.jsonl").chmod(0o600)
    return root


def test_claude_expunges_one_session_and_leaves_the_other(claude, tmp_path):
    before = files(tmp_path)
    result = expunge.expunge(session("claude", claude))
    gone = before - files(tmp_path)
    assert gone and all(A in path or f"agent-{A[:4]}" in path for path in gone)
    assert sorted(result["files"]) == sorted([f"{A}.jsonl", A, A, A, f"{A}-agent-{A}.json",
                                              f"{A}.md", A, f"agent-{A[:4]}"])
    assert result["lines"] == 1
    # B's line survives, including one whose text happens to contain A's id.
    assert [json.loads(line)["sessionId"] for line in (claude / "history.jsonl").open()] == [B, B]
    assert (claude / "history.jsonl").stat().st_mode & 0o777 == 0o600
    assert {f"claude/projects/-src-api/{B}.jsonl", f"ah/index/claude/{B}.md"} <= files(tmp_path)


def test_codex_expunges_rollouts_and_lines(tmp_path):
    root = tmp_path / "codex"
    day = root / "sessions/2026/10/08"
    day.mkdir(parents=True)
    for sid in (A, B):
        (day / f"rollout-2026-10-08T10-00-00-{sid}.jsonl").write_text("x")
        (day / f"rollout-2026-10-09T10-00-00-{sid}_0001.jsonl").write_text("x")  # a segment
    jsonl(root / "history.jsonl", [{"session_id": A}, {"session_id": B}])
    with (root / "history.jsonl").open("a") as f:  # two lines a crash cut short
        f.write(f'{{"session_id":"{B}","text":"about {A}, cut sh\n{{"session_id":"{A}","text":"cut')
    jsonl(root / "session_index.jsonl", [{"id": A}, {"id": B}])
    result = expunge.expunge(session("codex", root))
    assert sorted(result["files"]) == [f"rollout-2026-10-08T10-00-00-{A}.jsonl",
                                       f"rollout-2026-10-09T10-00-00-{A}_0001.jsonl"]
    assert result["lines"] == 3
    assert [line[:20] for line in (root / "history.jsonl").open()] == [
        f'{{"session_id": "{B}'[:20], f'{{"session_id":"{B}'[:20]]  # B's, even cut short
    assert all(B in p.name for p in day.iterdir()) and len(list(day.iterdir())) == 2


def test_a_path_resolving_outside_the_root_refuses_before_deleting(claude, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / f"{A}.jsonl").write_text("not ours")
    (claude / "projects/-linked").symlink_to(outside)
    before = files(tmp_path)
    with pytest.raises(Refused, match="outside"):
        expunge.expunge(session("claude", claude))
    assert files(tmp_path) == before


def test_lines_appended_during_the_rewrite_survive(claude, monkeypatch):
    log, mkstemp, replace = claude / "history.jsonl", expunge.tempfile.mkstemp, os.replace
    writer = log.open("a")  # another session's, opened before the rename

    def line(text):
        writer.write(json.dumps({"display": text, "sessionId": B}) + "\n")
        writer.flush()

    def appending(**kwargs):  # while the new file is written
        line("during")
        return mkstemp(**kwargs)

    def racing(src, dst):  # and into the old file as the new one takes its name
        replace(src, dst)
        writer.write(json.dumps({"display": "late", "sessionId": A}) + "\n")  # the session's
        line("racing")
    monkeypatch.setattr(expunge.tempfile, "mkstemp", appending)
    monkeypatch.setattr(expunge.os, "replace", racing)
    assert expunge.expunge(session("claude", claude))["lines"] == 2  # the late one counts
    assert [json.loads(x)["display"] for x in log.open()] == ["b", A, "during", "racing"]


def test_codex_without_session_id_on_its_status_line_refuses(monkeypatch, tmp_path):
    fake_procs(monkeypatch, tmp_path, {10: ("codex", [], None)})
    (tmp_path / "config.toml").write_text('[tui]\nstatus_line = ["model", "git-branch"]\n')
    with pytest.raises(Refused, match="session-id"):
        expunge.identify("10", CODEX_SCREEN)


def test_a_leftover_registration_of_the_session_goes_too(claude):
    (claude / "sessions").mkdir()
    for pid, sid in ((1, A), (2, B)):
        (claude / f"sessions/{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "procStart": "9"}))
    assert "1.json" in expunge.expunge(session("claude", claude))["files"]
    assert [p.name for p in (claude / "sessions").iterdir()] == ["2.json"]


def test_a_session_file_that_is_a_symlink_refuses(claude, tmp_path):
    transcript = claude / f"projects/-src-api/{A}.jsonl"
    transcript.rename(claude / "elsewhere.jsonl")
    transcript.symlink_to(claude / "elsewhere.jsonl")  # inside the root, but not the file
    with pytest.raises(Refused, match="symlink"):
        expunge.expunge(session("claude", claude))
    assert (claude / "elsewhere.jsonl").exists()


def fake_procs(monkeypatch, tmp_path, procs):
    """procs: pid -> (comm, children, claude session id or None)."""
    def read(pid, name):
        comm = procs[int(pid)][0]
        if name == "stat":
            return f"{pid} ({comm}) S " + " ".join(["0"] * 18) + " 777 0"
        if name == "comm":
            return comm + "\n"
        env = f"CLAUDE_CONFIG_DIR={tmp_path}\0CODEX_HOME={tmp_path}\0AGENT_HISTORY_DIR={tmp_path}\0"
        return env if name == "environ" else ""
    (tmp_path / "config.toml").write_text('[tui]\nstatus_line = ["model", "session-id"]\n')
    (tmp_path / "sessions/2026/10/08").mkdir(parents=True)
    for sid in (A, B):  # Codex threads that exist here
        (tmp_path / f"sessions/2026/10/08/rollout-2026-10-08T10-00-00-{sid}.jsonl").touch()
    for pid, (_, _, sid) in procs.items():
        if sid:
            (tmp_path / "sessions" / f"{pid}.json").write_text(
                json.dumps({"pid": pid, "sessionId": sid, "procStart": "777"}))
    monkeypatch.setattr(tmux, "proc_read", read)
    monkeypatch.setattr(expunge, "_children", lambda pid: procs[pid][1])


def test_identify_finds_the_one_agent_under_the_pane(monkeypatch, tmp_path):
    # The agent's own subprocess (a headless claude it ran) is not a second session.
    fake_procs(monkeypatch, tmp_path, {10: ("bash", [11], None), 11: ("claude", [12], A),
                                       12: ("claude", [], B)})
    s = expunge.identify("10", "")
    assert (s.harness, s.session_id, s.root, s.pid) == ("claude", A, tmp_path, 11)
    assert s.index == tmp_path / "index" / "claude"  # the agent's AGENT_HISTORY_DIR, not ours
    with pytest.raises(Refused, match="different session"):
        expunge.identify("10", "", expected=B)
    monkeypatch.setattr(tmux, "proc_read", lambda pid, name: f"HOME={tmp_path}\0" * (
        name == "environ") or "")
    assert expunge._home(11, "CODEX_HOME", ".codex") == tmp_path / ".codex"  # its HOME


@pytest.mark.parametrize(("procs", "screen", "reason"), [
    ({10: ("bash", [11, 12], None), 11: ("claude", [], A), 12: ("claude", [], B)}, "",
     "more than one"),
    ({10: ("bash", [], None)}, "", "no Claude Code or Codex"),
    ({10: ("codex", [], None)}, "› \n  gpt-5.5 high · ~/src/example-org/api\n", "status line"),
    ({10: ("codex", [], None)}, CODEX_SCREEN.replace("api ·", f"api · {B} ·"), "status line"),
    ({10: ("claude", [], "not-a-uuid")}, "", "not one this daemon"),
])
def test_identify_refuses_rather_than_guess(monkeypatch, tmp_path, procs, screen, reason):
    fake_procs(monkeypatch, tmp_path, procs)
    with pytest.raises(Refused, match=reason):
        expunge.identify("10", screen)


def test_codex_thread_comes_from_its_status_line(monkeypatch, tmp_path):
    fake_procs(monkeypatch, tmp_path, {10: ("codex", [], None)})
    assert expunge.identify("10", CODEX_SCREEN).session_id == A
    # A UUID-shaped segment no rollout is named for (a branch, say) is not a second thread.
    branch = "0d0d0d0d-9999-4aaa-8bbb-cccccccccccc"
    assert expunge.identify("10", CODEX_SCREEN.replace("api ·", f"{branch} ·")).session_id == A


def test_route_refuses_a_changed_session_without_killing(monkeypatch, claude):
    killed = []
    monkeypatch.setattr(tmux, "find_pane", lambda p: SimpleNamespace(id=p, pid="1234"))
    monkeypatch.setattr(tmux, "kill_window", killed.append)
    monkeypatch.setattr(expunge, "identify", lambda *_a: session("claude", claude))
    monkeypatch.setattr(tmux, "capture_pane", lambda _p: "")
    server.app.state.watcher = SimpleNamespace()
    client = TestClient(server.app)
    assert client.get("/api/panes/%251/expunge").json()["session_id"] == A
    monkeypatch.setattr(expunge, "identify", lambda *_a: (_ for _ in ()).throw(
        Refused("this pane is running a different session now")))
    r = client.post("/api/panes/%251/expunge", json={"session_id": A})
    assert r.status_code == 409 and "different session" in r.json()["detail"]
    assert not killed and (claude / f"projects/-src-api/{A}.jsonl").exists()


def test_route_kills_first_then_deletes(monkeypatch, claude):
    order, panes, calls = [], {"%1": "1234"}, []

    def find_pane(alias):  # "work:0.0" is %1; by the kill, %1 may be a newer process's
        calls.append(alias)
        pane = SimpleNamespace(id="%1", pid=panes["%1"])
        if len(calls) == 1:
            panes["%1"] = "999"
        return pane

    def kill(pane_id, pid=None):  # tmux.kill_window's guard, as the tmux server applies it
        if panes[pane_id] != pid:
            return False
        order.append("kill")
        panes.pop(pane_id)
        return True
    monkeypatch.setattr(tmux, "find_pane", find_pane)
    monkeypatch.setattr(tmux, "pane_pid", panes.get)
    monkeypatch.setattr(tmux, "kill_window", kill)
    monkeypatch.setattr(tmux, "capture_pane", lambda _p: "")
    monkeypatch.setattr(expunge, "identify", lambda *_a: session("claude", claude))
    monkeypatch.setattr(expunge, "wait_gone", lambda s: order.append("gone") or True)
    monkeypatch.setattr(expunge, "alive", lambda s: True)
    server.app.state.watcher = SimpleNamespace(
        checkpoint_key=lambda pane_id, pid: f"boot:1:{pane_id}:{pid}", history=object(),
        forget_checkpoint=lambda uid: order.append(uid) or True)
    client = TestClient(server.app)
    # Identified under 1234, but %1 is 999's by the kill: nothing is killed or deleted.
    assert client.post("/api/panes/work:0.0/expunge", json={"session_id": A}).status_code == 409
    assert not order and panes == {"%1": "999"}
    panes["%1"] = "1234"
    r = client.post("/api/panes/work:0.0/expunge", json={"session_id": A})
    assert r.status_code == 200 and r.json()["lines"] == 1
    assert order == ["kill", "gone", "boot:1:%1:1234"]
    assert not (claude / f"projects/-src-api/{A}.jsonl").exists()


def test_kill_window_guard_runs_in_one_tmux_command(monkeypatch):
    sent = []
    monkeypatch.setattr(tmux, "_run", lambda argv: sent.append(argv) or "kept\n")
    assert not tmux.kill_window("%1", "1234")  # the other branch ran: the pane changed hands
    assert sent == [["if-shell", "-F", "-t", "%1", "#{==:#{pane_pid},1234}", "kill-window -t %1",
                     "display-message -p kept"]]


def test_an_unreadable_registration_refuses(monkeypatch, tmp_path):
    fake_procs(monkeypatch, tmp_path, {10: ("bash", [11, 12], None), 11: ("claude", [], A),
                                       12: ("claude", [], None)})
    for broken in ("{half", "{}", '{"pid": 12, "sessionId": "x"}'):  # no procStart
        (tmp_path / "sessions" / "12.json").write_text(broken)
        with pytest.raises(Refused, match="can't be read"):
            expunge.identify("10", "")


def test_route_refuses_when_the_identified_agent_is_gone(monkeypatch, claude):
    killed = []
    monkeypatch.setattr(tmux, "find_pane", lambda p: SimpleNamespace(id="%1", pid="1234"))
    monkeypatch.setattr(tmux, "kill_window", lambda *a: killed.append(a))
    monkeypatch.setattr(tmux, "capture_pane", lambda _p: "")
    monkeypatch.setattr(expunge, "identify", lambda *_a: session("claude", claude))
    monkeypatch.setattr(expunge, "alive", lambda s: False)  # the shell now runs another agent
    server.app.state.watcher = SimpleNamespace(checkpoint_key=lambda *_a: "uid")
    r = TestClient(server.app).post("/api/panes/%251/expunge", json={"session_id": A})
    assert r.status_code == 409 and not killed
