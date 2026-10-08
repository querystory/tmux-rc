"""Expunge deletes exactly one session's files, found by its id, and refuses rather than
guess. Every config dir here is a temp dir: never the real ~/.claude or ~/.codex."""
import json
import sqlite3
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


def jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A Claude config dir and agent-history index holding sessions A and B."""
    monkeypatch.setenv("AGENT_HISTORY_DIR", str(tmp_path / "ah"))
    root = tmp_path / "claude"
    for sid in (A, B):
        for rel in (f"projects/-src-api/{sid}.jsonl", f"projects/-src-api/{sid}/subagents/x.jsonl",
                    f"file-history/{sid}/f@v1", f"session-env/{sid}/hook.sh",
                    f"todos/{sid}-agent-{sid}.json", f"../ah/index/claude/{sid}.md",
                    f"../ah/index/claude/{sid}/agent.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x")
    jsonl(root / "history.jsonl", [{"display": "a", "sessionId": A},
                                   {"display": "b", "sessionId": B},
                                   {"display": A, "sessionId": B}])
    (root / "history.jsonl").chmod(0o600)
    return root


def test_claude_expunges_one_session_and_leaves_the_other(claude, tmp_path):
    before = files(tmp_path)
    result = expunge.expunge(Session("claude", A, claude, 1, "1"))
    gone = before - files(tmp_path)
    assert gone and all(A in path for path in gone)
    assert sorted(result["files"]) == sorted([f"{A}.jsonl", A, A, A, f"{A}-agent-{A}.json",
                                              f"{A}.md", A])
    assert result["lines"] == 1
    # B's line survives, including one whose text happens to contain A's id.
    assert [json.loads(line)["sessionId"] for line in (claude / "history.jsonl").open()] == [B, B]
    assert (claude / "history.jsonl").stat().st_mode & 0o777 == 0o600
    assert {f"claude/projects/-src-api/{B}.jsonl", f"ah/index/claude/{B}.md"} <= files(tmp_path)


def test_codex_expunges_rollout_lines_and_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_DIR", str(tmp_path / "ah"))
    root = tmp_path / "codex"
    day = root / "sessions/2026/10/08"
    day.mkdir(parents=True)
    for sid in (A, B):
        (day / f"rollout-2026-10-08T10-00-00-{sid}.jsonl").write_text("x")
    jsonl(root / "history.jsonl", [{"session_id": A}, {"session_id": B}])
    jsonl(root / "session_index.jsonl", [{"id": A}, {"id": B}])
    with sqlite3.connect(root / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
        db.execute("CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)")
        db.executemany("INSERT INTO threads VALUES (?, 't')", [(A,), (B,)])
        db.executemany("INSERT INTO thread_items VALUES (?, '{}')", [(A,), (A,), (B,)])
    result = expunge.expunge(Session("codex", A, root, 1, "1"))
    assert result == {"files": [f"rollout-2026-10-08T10-00-00-{A}.jsonl"], "lines": 2, "rows": 3}
    assert [p.name for p in day.iterdir()] == [f"rollout-2026-10-08T10-00-00-{B}.jsonl"]
    with sqlite3.connect(root / "state_5.sqlite") as db:
        assert db.execute("SELECT id FROM threads").fetchall() == [(B,)]
        assert db.execute("SELECT thread_id FROM thread_items").fetchall() == [(B,)]


def test_a_path_resolving_outside_the_root_refuses_before_deleting(claude, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / f"{A}.jsonl").write_text("not ours")
    (claude / "projects/-linked").symlink_to(outside)
    before = files(tmp_path)
    with pytest.raises(Refused, match="outside"):
        expunge.expunge(Session("claude", A, claude, 1, "1"))
    assert files(tmp_path) == before


def fake_procs(monkeypatch, tmp_path, procs):
    """procs: pid -> (comm, children, claude session id or None)."""
    def read(pid, name):
        comm = procs[int(pid)][0]
        if name == "stat":
            return f"{pid} ({comm}) S " + " ".join(["0"] * 18) + " 777 0"
        if name == "comm":
            return comm + "\n"
        return f"CLAUDE_CONFIG_DIR={tmp_path}\0CODEX_HOME={tmp_path}\0" if name == "environ" else ""
    for pid, (_, _, sid) in procs.items():
        if sid:
            (tmp_path / "sessions").mkdir(exist_ok=True)
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
    with pytest.raises(Refused, match="different session"):
        expunge.identify("10", "", expected=B)


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


def test_route_refuses_a_changed_session_without_killing(monkeypatch, claude):
    killed = []
    monkeypatch.setattr(tmux, "kill_window", killed.append)
    monkeypatch.setattr(expunge, "identify", lambda *_a: Session("claude", A, claude, 1, "1"))
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
    order, panes, newer = [], {"%1": "1234"}, ["999"]

    def find_pane(pane_id):  # by the kill, tmux may have given %1 to a newer process
        if newer:
            panes[pane_id] = newer.pop()
        return SimpleNamespace(id=pane_id)

    def kill(pane_id, pid=None):  # tmux.kill_window's guard, as the tmux server applies it
        if panes[pane_id] == pid:
            order.append("kill")
            panes.pop(pane_id)
    monkeypatch.setattr(tmux, "find_pane", find_pane)
    monkeypatch.setattr(tmux, "pane_pid", panes.get)
    monkeypatch.setattr(tmux, "kill_window", kill)
    monkeypatch.setattr(tmux, "capture_pane", lambda _p: "")
    monkeypatch.setattr(expunge, "identify", lambda *_a: Session("claude", A, claude, 1, "1"))
    monkeypatch.setattr(expunge, "wait_gone", lambda s: order.append("gone") or True)
    server.app.state.watcher = SimpleNamespace(
        forget_checkpoint=lambda pane_id, pid: order.append((pane_id, pid)) or True)
    client = TestClient(server.app)
    # Identified under 1234, but %1 is 999's by the kill: nothing is killed or deleted.
    assert client.post("/api/panes/%251/expunge", json={"session_id": A}).status_code == 409
    assert not order and panes == {"%1": "999"}
    panes["%1"] = "1234"
    r = client.post("/api/panes/%251/expunge", json={"session_id": A})
    assert r.status_code == 200 and r.json()["lines"] == 1
    assert order == ["kill", "gone", ("%1", "1234")]
    assert not (claude / f"projects/-src-api/{A}.jsonl").exists()


def test_kill_window_guard_runs_in_one_tmux_command(monkeypatch):
    sent = []
    monkeypatch.setattr(tmux, "_run", sent.append)
    tmux.kill_window("%1", "1234")
    assert sent == [["if-shell", "-F", "-t", "%1", "#{==:#{pane_pid},1234}", "kill-window -t %1"]]


def test_an_unreadable_registration_refuses(monkeypatch, tmp_path):
    fake_procs(monkeypatch, tmp_path, {10: ("bash", [11, 12], None), 11: ("claude", [], A),
                                       12: ("claude", [], None)})
    (tmp_path / "sessions" / "12.json").write_text("{half")
    with pytest.raises(Refused, match="can't be read"):
        expunge.identify("10", "")
