"""Expunge deletes exactly one session's files, found by its id, and refuses rather than
guess. Every config dir here is a temp dir: never the real ~/.claude or ~/.codex."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbus import expunge, server, tmux, watcher
from openbus.expunge import Empty, Refused, Session

A, B = "0f0f0f0f-1111-4222-8333-444444444444", "0e0e0e0e-5555-4666-8777-888888888888"
CODEX_SCREEN = f"› \n  gpt-5.5 high · ~/src/example-org/api · {A}\n"
CODEX_LINE = '[tui]\nstatus_line = ["model-with-reasoning", "current-dir", "session-id"]\n'
CONFIRM = {"session_id": A, "birth": "1234"}  # what the dialog posts for pane pid 1234
BRANCHY = "0d0d0d0d-9999-4aaa-8bbb-cccccccccccc"  # a git branch named like a thread id


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
    (tmp_path / "config.toml").write_text(CODEX_LINE)
    (tmp_path / "sessions/2026/10/08").mkdir(parents=True)
    (tmp_path / "projects/-src").mkdir(parents=True)
    for sid in (A, B):  # Codex threads and Claude transcripts that exist here
        (tmp_path / f"sessions/2026/10/08/rollout-2026-10-08T10-00-00-{sid}.jsonl").touch()
        (tmp_path / f"projects/-src/{sid}.jsonl").touch()
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
    # An item missing ahead of session-id: its place can't be told, so it refuses.
    ({10: ("codex", [], None)}, CODEX_SCREEN.replace("~/src/example-org/api · ", ""),
     "status line"),
    ({10: ("claude", [], "not-a-uuid")}, "", "not one this daemon"),
])
def test_identify_refuses_rather_than_guess(monkeypatch, tmp_path, procs, screen, reason):
    fake_procs(monkeypatch, tmp_path, procs)
    with pytest.raises(Refused, match=reason):
        expunge.identify("10", screen)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_agent_with_no_saved_conversation_is_empty(monkeypatch, tmp_path, harness):
    # A fresh agent, or Claude's sign-in, has an id but has written no transcript yet.
    fake_procs(monkeypatch, tmp_path, {10: (harness, [], A if harness == "claude" else None)})
    for p in tmp_path.glob(f"*/**/*{A}.jsonl"):
        p.unlink()
    with pytest.raises(Empty, match="not saved a conversation"):
        expunge.identify("10", CODEX_SCREEN)
    s_index = expunge._index(10, harness)  # agent-history's entry outlives the transcript
    s_index.mkdir(parents=True)
    (s_index / f"{A}.md").touch()
    assert expunge.identify("10", CODEX_SCREEN).session_id == A


def test_a_fresh_codex_without_session_id_configured_is_empty(monkeypatch, tmp_path):
    # Without the id no thread can be named, so it refuses with the hint while any rollout
    # was written since Codex started; with none written, a fresh one has no conversation.
    fake_procs(monkeypatch, tmp_path, {10: ("codex", [], None)})
    (tmp_path / "config.toml").write_text('[tui]\nstatus_line = ["model", "git-branch"]\n')
    with pytest.raises(Refused, match="session-id"):
        expunge.identify("10", CODEX_SCREEN)
    for p in tmp_path.glob("sessions/*/*/*/*.jsonl"):
        os.utime(p, (0, 0))  # all before Codex started (777 ticks after boot)
    with pytest.raises(Empty, match="not saved a conversation"):
        expunge.identify("10", CODEX_SCREEN)
    read = tmux.proc_read  # a resume names a conversation it has not written to yet
    monkeypatch.setattr(tmux, "proc_read", lambda pid, name: "codex\0--yolo\0resume"
                        if name == "cmdline" else read(pid, name))
    with pytest.raises(Refused, match="session-id") as e:
        expunge.identify("10", CODEX_SCREEN)
    assert not isinstance(e.value, Empty)


def test_a_fresh_codex_beside_a_saved_session_is_more_than_one(monkeypatch, tmp_path):
    fake_procs(monkeypatch, tmp_path, {10: ("bash", [11, 12], None), 11: ("codex", [], None),
                                       12: ("claude", [], A)})
    (tmp_path / "config.toml").write_text('[tui]\nstatus_line = ["model"]\n')
    for p in tmp_path.glob("sessions/*/*/*/*.jsonl"):
        os.utime(p, (0, 0))
    with pytest.raises(Refused, match="more than one"):
        expunge.identify("10", "")


def test_codex_sign_in_is_no_session(monkeypatch, tmp_path):
    # The search goes on past it: a conversation beside it is still found, and alone it is
    # nothing to expunge, whatever its screen shows.
    fake_procs(monkeypatch, tmp_path, {10: ("bash", [11, 12], None), 11: ("codex", [], None),
                                       12: ("claude", [], A)})
    read = tmux.proc_read
    for argv in (["codex", "login"], ["node", "/usr/lib/node_modules/codex/bin/codex", "login"]):
        monkeypatch.setattr(tmux, "proc_read", lambda pid, name, argv=argv: "\0".join(argv)
                            if name == "cmdline" and pid == 11 else read(pid, name))
        assert expunge.identify("10", "").session_id == A
        with pytest.raises(Empty, match="no Claude Code or Codex"):
            expunge.identify("11", "")


def test_codex_thread_comes_from_its_status_line(monkeypatch, tmp_path):
    fake_procs(monkeypatch, tmp_path, {10: ("codex", [], None)})
    assert expunge.identify("10", CODEX_SCREEN).session_id == A
    # A UUID-like branch, even one naming another thread with a rollout here, is not taken:
    # the thread id is the segment at session-id's configured place.
    (tmp_path / "config.toml").write_text(CODEX_LINE.replace('"]', '", "git-branch"]'))
    for branch in (BRANCHY, B):
        assert expunge.identify("10", CODEX_SCREEN.replace(A, f"{A} · {branch}")).session_id == A


def test_route_refuses_a_changed_session_without_killing(monkeypatch, claude):
    killed = []
    monkeypatch.setattr(tmux, "find_pane", lambda p: SimpleNamespace(id=p, pid="1234"))
    monkeypatch.setattr(tmux, "kill_window", killed.append)
    monkeypatch.setattr(expunge, "identify", lambda *_a: session("claude", claude))
    monkeypatch.setattr(tmux, "capture_pane", lambda _p: "")
    server.app.state.watcher = SimpleNamespace()
    client = TestClient(server.app)
    assert client.get("/api/panes/%251/expunge?birth=1234").json()["session_id"] == A
    monkeypatch.setattr(expunge, "identify", lambda *_a: (_ for _ in ()).throw(
        Empty("this agent has not saved a conversation yet")))
    assert client.get("/api/panes/%251/expunge?birth=1234").json() == {"nothing": True}
    assert client.post("/api/panes/%251/expunge", json=CONFIRM).status_code == 409
    monkeypatch.setattr(expunge, "identify", lambda *_a: session("claude", claude))
    # A menu drawn for an earlier pane under the same %N reaches nothing.
    r = client.get("/api/panes/%251/expunge?birth=999")
    assert r.status_code == 409 and "different window" in r.json()["detail"]
    monkeypatch.setattr(expunge, "identify", lambda *_a: (_ for _ in ()).throw(
        Refused("this pane is running a different session now")))
    r = client.post("/api/panes/%251/expunge", json=CONFIRM)
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
            return []
        order.append("kill")
        return [(pane_id, panes.pop(pane_id))]
    monkeypatch.setattr(tmux, "find_pane", find_pane)
    monkeypatch.setattr(tmux, "pane_pid", panes.get)
    monkeypatch.setattr(tmux, "kill_window", kill)
    monkeypatch.setattr(tmux, "capture_pane", lambda _p: "")
    monkeypatch.setattr(expunge, "identify", lambda *_a: session("claude", claude))
    monkeypatch.setattr(expunge, "wait_gone", lambda s: order.append("gone") or True)
    monkeypatch.setattr(expunge, "alive", lambda s: True)
    server.app.state.watcher = SimpleNamespace(
        checkpoint_key=lambda pane_id, pid: f"boot:1:{pane_id}:{pid}", history=object(),
        forget_checkpoint=lambda uid: order.append(uid) or True, drop_panes=order.append)
    client = TestClient(server.app)
    # Identified under 1234, but %1 is 999's by the kill: nothing is killed or deleted.
    assert client.post("/api/panes/work:0.0/expunge", json=CONFIRM).status_code == 409
    assert not order and panes == {"%1": "999"}
    panes["%1"] = "1234"
    r = client.post("/api/panes/work:0.0/expunge", json=CONFIRM)
    assert r.status_code == 200 and r.json()["lines"] == 1
    assert r.json()["killed"] == [["%1", "1234"]]
    assert order == ["kill", {"%1"}, "gone", "boot:1:%1:1234"]  # unpublished at the kill
    assert not (claude / f"projects/-src-api/{A}.jsonl").exists()


def test_a_killed_window_leaves_the_next_state_at_once(monkeypatch):
    """The deck the watcher published before the kill must not outlive it until a tick."""
    monkeypatch.setattr(tmux, "find_pane", lambda p: SimpleNamespace(id=p, pid="1234"))
    # tmux names what it killed: %1 and a split of it, %3.
    monkeypatch.setattr(tmux, "kill_window", lambda *_a: [("%1", "1234"), ("%3", "1240")])
    monkeypatch.setattr(tmux, "prefix_key", lambda: "C-b")
    w = watcher.Watcher(None, use_llm=False)
    w._publish_states([{"pane_id": p} for p in ("%1", "%3", "%2")], record_history=False)
    server.app.state.watcher = w
    client = TestClient(server.app)
    version = client.get("/api/state").json()["version"]
    assert client.post("/api/panes/%251/close").json()["killed"] == [["%1", "1234"], ["%3", "1240"]]
    state = client.get(f"/api/state?v={version}").json()  # returns now: the version moved
    assert [p["pane_id"] for p in state["panes"]] == ["%2"] and "%1" in w._force_parse


def test_kill_window_names_exactly_the_panes_it_killed(monkeypatch, tmp_path):
    """On a throwaway server (-S): the guard and the listing run in the kill's own command."""
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = str(tmp_path / "kill.sock")

    def run(args):
        return subprocess.check_output(["tmux", "-f", "/dev/null", "-S", socket, *args], text=True)
    monkeypatch.setattr(tmux, "_run", run)
    fmt = ["-P", "-F", "#{pane_id} #{pane_pid}"]
    target = tuple(run(["new-session", "-d", "-x", "80", "-y", "20", *fmt, "sh"]).split())
    split = tuple(run(["split-window", "-t", target[0], *fmt, "sh"]).split())
    other = run(["new-window", "-d", *fmt, "sh"]).split()[0]
    assert tmux.kill_window(target[0], "1") == []  # another process owns it now: kept
    assert sorted(tmux.kill_window(target[0], target[1])) == sorted([target, split])
    assert run(["list-panes", "-a", "-F", "#{pane_id}"]).split() == [other]
    tmux.kill_window(other)  # its last window: the throwaway server exits with it


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
    r = TestClient(server.app).post("/api/panes/%251/expunge", json=CONFIRM)
    assert r.status_code == 409 and not killed


def test_close_with_a_birth_kills_only_that_incarnation(monkeypatch):
    # The no-conversation sheet's Kill window may outlive the pane it was drawn for.
    killed = []
    monkeypatch.setattr(tmux, "find_pane", lambda p: SimpleNamespace(id="%1", pid="1234"))
    monkeypatch.setattr(tmux, "kill_window", lambda pane_id, pid=None: killed.append(pid) or (
        [] if pid not in (None, "1234") else [("%1", "1234")]))
    server.app.state.watcher = SimpleNamespace(drop_panes=lambda _ids: None)
    client = TestClient(server.app)
    assert client.post("/api/panes/%251/close?birth=999").status_code == 409
    assert client.post("/api/panes/%251/close?birth=1234").json()["killed"] == [["%1", "1234"]]
    assert client.post("/api/panes/%251/close?birth=").status_code == 200  # no birth: unguarded
    assert killed == ["999", "1234", None]
