"""Reboot restore's recorder and offer (docs/design/reboot-restore.md, "The first PR").

Runs a REAL tmux on private sockets (TMUXRC_TMUX_SOCKET), never the developer's server.
Cleanup closes the exact panes each test made on its own socket; no kill-server."""

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import openbus.restore as R
import openbus.tmux as T
from openbus.history import History

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")


@pytest.fixture
def server(monkeypatch, tmp_path):
    made = []
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(R.agent_history, "live", lambda: {"sessions": {}, "unknown": []})
    monkeypatch.setattr(R.agent_history, "reconcile", lambda: None)

    def start():
        name = f"restore-test-{uuid.uuid4().hex[:8]}"
        made.append(name)
        monkeypatch.setenv("TMUXRC_TMUX_SOCKET", name)
        return name

    yield start
    for name in made:
        panes = subprocess.run(["tmux", "-L", name, "list-panes", "-a", "-F", "#{pane_id}"],
                               capture_output=True, text=True, check=False).stdout.split()
        for pane in panes:
            subprocess.run(["tmux", "-L", name, "kill-pane", "-t", pane], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tmux(*args):
    return T._run(list(args)).strip()


def stop():
    """End the current test server the way a crash would leave it: gone."""
    for pane in tmux("list-panes", "-a", "-F", "#{pane_id}").split():
        subprocess.run(["tmux", "-L", os.environ["TMUXRC_TMUX_SOCKET"], "kill-pane", "-t", pane],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while T.server_running() and time.time() < deadline:
        time.sleep(0.05)


def rows(history):
    with history.connect() as db:
        return db.execute("SELECT server, last_seen, payload FROM layout_snapshots").fetchall()


def test_snapshot_reproduces_the_layout(server, tmp_path):
    server()
    tmux("new-session", "-d", "-s", "work", "-c", str(tmp_path), "-x", "120", "-y", "40")
    tmux("split-window", "-t", "work:0", "-c", "/")
    tmux("new-window", "-d", "-t", "work:4", "-c", str(tmp_path))
    tmux("rename-window", "-t", "work:4", "notes")
    tmux("resize-pane", "-Z", "-t", "work:0.1")
    recorder = R.Recorder(History(tmp_path / "h.db"))
    recorder.tick([{"pane_id": tmux("display-message", "-p", "-t", "work:4", "#{pane_id}"),
                    "tool": "claude", "agent_title": "Fix the flaky test", "cost": "$1.20"}])
    (server_id, _, payload), = rows(recorder.history)
    snap = json.loads(payload)
    assert server_id == R.identity()
    (session,) = snap["sessions"]
    assert session["name"] == "work" and session["path"] == str(tmp_path)
    assert [(w["index"], w["current"]) for w in session["windows"]] == [(0, True), (4, False)]
    first, notes = (snap["windows"][w["id"]] for w in session["windows"])
    assert [p["cwd"] for p in first["panes"]] == [str(tmp_path), "/"]
    assert first["zoomed"] and not first["manual"] and first["layout"].count(",") > 4
    assert notes["name"] == "notes" and notes["manual"]
    assert notes["panes"][0]["title"] == "Fix the flaky test"
    assert notes["panes"][0]["cost"] == "$1.20"


def test_unchanged_fleet_rewrites_only_the_stamp(server, tmp_path):
    server()
    tmux("new-session", "-d", "-s", "work", "-x", "120", "-y", "40")
    recorder = R.Recorder(History(tmp_path / "h.db"))
    recorder.tick([], now=1000)
    recorder.tick([], now=1030)  # inside the minute: nothing at all
    assert rows(recorder.history)[0][1] == 1000
    recorder.tick([], now=1060)
    (_, seen, before), = rows(recorder.history)
    assert seen == 1060
    tmux("split-window", "-t", "work:0")
    recorder.tick([], now=1120)
    assert rows(recorder.history)[0][2] != before


def test_a_server_replaced_mid_snapshot_writes_nothing(server, tmp_path, monkeypatch):
    server()
    tmux("new-session", "-d", "-s", "work")
    ids = iter(["boot:1:100", "boot:2:200"])  # the old server's, then its replacement's
    monkeypatch.setattr(R, "identity", lambda: next(ids))
    recorder = R.Recorder(History(tmp_path / "h.db"))
    recorder.tick([])
    assert rows(recorder.history) == []


def test_dead_server_is_offered_and_a_live_one_is_not(server, tmp_path, monkeypatch):
    history = History(tmp_path / "h.db")
    server()
    tmux("new-session", "-d", "-s", "old", "-c", str(tmp_path))
    R.Recorder(history).tick([], now=1000)
    assert R.candidate(history) is None  # still running: a daemon restart offers nothing
    stop()
    server()  # the replacement, which writes a newer row of its own
    tmux("new-session", "-d", "-s", "fresh")
    R.Recorder(history).tick([], now=2000)
    found = R.candidate(history)
    assert found["died_at"] == 1000 and found["sessions"][0]["name"] == "old"
    assert R.consume(history, found["server"], "dismissed")
    assert not R.consume(history, found["server"], "restored")  # one tap wins
    assert R.candidate(history) is None  # and an older loss never surfaces in its place


def test_grouped_and_linked_windows_are_recorded_once(server, tmp_path):
    server()
    tmux("new-session", "-d", "-s", "a")
    tmux("new-window", "-d", "-t", "a:3")
    tmux("new-session", "-d", "-s", "b", "-t", "a")  # grouped: shares a's windows
    tmux("new-session", "-d", "-s", "c")
    tmux("link-window", "-s", "a:3", "-t", "c:7")  # linked, no group
    recorder = R.Recorder(History(tmp_path / "h.db"))
    recorder.tick([])
    snap = json.loads(rows(recorder.history)[0][2])
    sessions = {s["name"]: s for s in snap["sessions"]}
    assert sessions["b"]["group"] == "a" and not sessions["c"]["group"]
    shared = sessions["a"]["windows"][1]["id"]
    assert shared in {w["id"] for w in sessions["c"]["windows"]}
    assert sum(len(w["panes"]) for w in snap["windows"].values()) == 3  # each pane once


def test_binding_follows_the_foreground_agent(server, tmp_path, monkeypatch):
    server()
    tmux("new-session", "-d", "-s", "work")
    pane = tmux("display-message", "-p", "-t", "work:0", "#{pane_id}")
    # A stopped agent in the background, the foreground one in front of it.
    tmux("send-keys", "-t", pane, "sleep 301 & sleep 302", "Enter")
    deadline = time.time() + 5
    while "sleep 302" not in subprocess.run(["pgrep", "-af", "sleep 30"], capture_output=True,
                                            text=True, check=False).stdout:
        assert time.time() < deadline
        time.sleep(0.05)

    def pid(args):
        return int(subprocess.run(["pgrep", "-f", args], capture_output=True, text=True,
                                  check=False).stdout.split()[0])

    sessions = {"behind": {"pid": pid("sleep 301"), "harness": "claude"},
                "front": {"pid": pid("sleep 302"), "harness": "codex"}}
    live = {"sessions": sessions, "unknown": []}
    monkeypatch.setattr(R.agent_history, "live", lambda: live)
    recorder = R.Recorder(History(tmp_path / "h.db"))
    front = {"harness": "codex", "session_id": "front", "account": str(Path.home() / ".codex")}
    assert recorder._bindings({pane: tmux("display-message", "-p", "-t", pane, "#{pane_pid}")}) \
        == {pane: front}
    # agent-history can't tell now: the same foreground keeps its binding.
    live = {"sessions": {}, "unknown": ["codex"]}
    assert recorder._bindings({pane: tmux("display-message", "-p", "-t", pane, "#{pane_pid}")}) \
        == {pane: front}
    tmux("send-keys", "-t", pane, "C-c", "")


def test_a_reused_pid_is_a_dead_server(server):
    server()
    tmux("new-session", "-d", "-s", "work")
    live = R.identity()
    assert R.alive(live)
    boot, pid, _start = live.rsplit(":", 2)
    assert not R.alive(f"{boot}:{pid}:1")  # same pid, another process's start time
    assert not R.alive(f"otherboot:{pid}:{_start}")


def test_defaults_follow_the_transcript_and_say_why_not(tmp_path, monkeypatch):
    died = 1_000_000.0
    entries = {"recent": {"last_active": "1970-01-12T13:40:00+00:00", "cwd": str(tmp_path)},
               "stale": {"last_active": "1970-01-01T00:00:00+00:00"},
               "live": {"running": {"pid": 1}}, "gone": {"source_missing": True}}

    def judge(**pane):
        return R._judge({"cwd": str(tmp_path), **pane}, died, entries)

    # The screen clock says stale; the transcript says recent, and wins.
    assert judge(binding={"session_id": "recent"}, last_activity=0) == (True, None)
    # The screen clock says recent; the transcript says four days idle, and wins.
    assert judge(binding={"session_id": "stale"}, last_activity=died) == (False, None)
    assert judge(binding={"session_id": "stale"}, activity="waiting") == (True, None)
    assert judge(binding={"session_id": "live"}) == (False, "it's already running")
    assert judge(binding={"session_id": "gone"}) == (False, "its conversation is gone")
    # Without agent-history nothing can be resumed, so a bound pane is blocked, not called gone.
    assert R._judge({"cwd": str(tmp_path), "binding": {"session_id": "gone"},
                     "last_activity": died}, died, None) == (
        False, "agent-history isn't answering; rebuild it with make install-units")
    assert judge(cwd="/no/such/dir") == (False, "its directory is gone")
    # The transcript's directory, where the harness will look, is the one that must exist.
    entries["moved"] = {"cwd": "/no/such/dir"}
    assert judge(binding={"session_id": "moved"}) == (False, "its directory is gone")
    assert judge(command="bash", last_activity=died) == (False, None)
    assert judge(command="vim", last_activity=died - 60) == (True, None)


def _shape(snapshot):
    """What a restore must reproduce, minus the ids a new server assigns afresh."""
    windows = snapshot["windows"]
    return sorted((s["name"], s["group"] == "", w["index"], w["current"], windows[w["id"]]["name"],
                   windows[w["id"]]["manual"], windows[w["id"]]["zoomed"],
                   windows[w["id"]]["layout"].split(",", 1)[1],
                   tuple(p["cwd"] for p in windows[w["id"]]["panes"]))
                  for s in snapshot["sessions"] for w in s["windows"])


def test_build_reproduces_a_dead_servers_layout(server, tmp_path, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)  # no systemd scope in tests
    history = History(tmp_path / "h.db")
    server()
    tmux("new-session", "-d", "-s", "work", "-c", str(tmp_path), "-x", "160", "-y", "48")
    tmux("split-window", "-h", "-t", "work:0", "-c", "/")
    tmux("new-window", "-d", "-t", "work:3", "-c", "/tmp")
    tmux("rename-window", "-t", "work:3", "notes")
    tmux("split-window", "-t", "work:3", "-c", "/")
    tmux("resize-pane", "-Z", "-t", "work:3.1")
    tmux("new-session", "-d", "-s", "peer", "-t", "work")  # a grouped member
    R.Recorder(history).tick([], now=1000)
    before = json.loads(rows(history)[0][2])
    stop()
    server()  # no server at all now: build must start one
    found = R.candidate(history)
    chosen = {(wid, p["index"]) for wid, w in found["windows"].items() for p in w["panes"]}
    built = R.build(found, chosen)
    assert len(built) == 4
    R.Recorder(history).tick([], now=2000)
    after = json.loads(next(r[2] for r in rows(history) if r[1] == 2000))
    assert _shape(after) == _shape(before)


def test_build_never_merges_into_a_live_session_and_leaves_out_what_wasnt_chosen(
        server, tmp_path, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    history = History(tmp_path / "h.db")
    server()
    tmux("new-session", "-d", "-s", "work", "-c", str(tmp_path))
    tmux("new-window", "-d", "-t", "work:1", "-c", "/")
    R.Recorder(history).tick([], now=1000)
    stop()
    server()
    tmux("new-session", "-d", "-s", "work")  # a login script's session, by the same name
    found = R.candidate(history)
    first = next(w for w in found["sessions"][0]["windows"] if w["index"] == 1)
    R.build(found, {(first["id"], 0)})
    names = set(tmux("list-sessions", "-F", "#{session_name}").split())
    assert names == {"work", "work-restored"}
    assert tmux("list-windows", "-t", "work-restored", "-F", "#{window_index}") == "1"
    assert tmux("display-message", "-p", "-t", "work-restored:1", "#{pane_current_path}") == "/"


def test_launch_types_each_resume_and_skips_one_running_meanwhile(server, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(R, "READY", 1)
    server()
    tmux("new-session", "-d", "-s", "work")
    a = tmux("display-message", "-p", "-t", "work:0", "#{pane_id}")
    b = tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", a)
    entries = {"one": {"resume_argv": ["codex", "resume", "one"]},
               "two": {"resume_argv": ["codex", "resume", "two"], "running": {"pid": 1}}}
    monkeypatch.setattr(R.agent_history, "get", entries.get)
    audits = []
    watcher = type("W", (), {"states": [], "request_reparse": lambda self, pane: None})()
    built = [(a, {"binding": {"session_id": "one"}}), (b, {"binding": {"session_id": "two"}})]
    asyncio.run(R.launch(built, watcher, lambda *a: audits.append(a)))
    assert ("restore_resume", a, "one", "ok") in audits
    assert audits.count(("restore_resume", b, "two",
                         "skipped: running, unknown or not resumable")) == 1
    deadline = time.time() + 5
    while "codex resume one" not in tmux("capture-pane", "-p", "-t", a):
        assert time.time() < deadline
        time.sleep(0.05)
    assert "codex resume two" not in tmux("capture-pane", "-p", "-t", b)


def _post(monkeypatch, tmp_path, build):
    """The restore endpoint over a one-pane offer, `build` standing in for the layout."""
    from fastapi.testclient import TestClient

    import openbus.server as S
    found = {"server": "dead", "windows": {"@1": {"panes": [{"index": 0, "blocked": None}]}}}
    spent = []
    monkeypatch.setattr(S.app.state, "history", History(tmp_path / "h.db"), raising=False)
    monkeypatch.setattr(R, "offer", lambda _h: found)
    monkeypatch.setattr(R, "consume", lambda _h, _s, how: spent.append(how) or True)
    monkeypatch.setattr(R, "build", build)
    reply = TestClient(S.app).post("/api/restore", json={"server": "dead",
                                                          "panes": [["@1", 0], ["@9", 3]]})
    return reply, spent


def test_restore_offers_again_only_when_tmux_built_nothing(server, tmp_path, monkeypatch):
    server()
    monkeypatch.setenv("TMUXRC_TARGET", "%1")  # single-pane mode would show none of it
    reply, spent = _post(monkeypatch, tmp_path, None)
    assert (reply.status_code, spent) == (409, [])
    monkeypatch.delenv("TMUXRC_TARGET")

    def refuse(_found, chosen):
        assert chosen == {("@1", 0)}  # a pane the offer doesn't hold is never built
        raise subprocess.CalledProcessError(1, "tmux")
    reply, spent = _post(monkeypatch, tmp_path, refuse)
    assert reply.status_code == 500
    assert spent == ["restored", None]

    def half(_found, _chosen):
        tmux("new-session", "-d", "-s", "work")
        raise subprocess.CalledProcessError(1, "tmux")
    reply, spent = _post(monkeypatch, tmp_path, half)
    assert reply.status_code == 500
    assert "work" in reply.json()["detail"]
    assert spent == ["restored"]  # a retry would build "work" again, as work-restored

    import openbus.server as S
    asks = []  # the ask before the build answers; the one after times out

    def ask():
        asks.append(1)
        if len(asks) > 1:
            raise subprocess.CalledProcessError(1, "tmux")
        return set()
    monkeypatch.setattr(S, "_session_names", ask)
    reply, spent = _post(monkeypatch, tmp_path, refuse)
    assert reply.status_code == 500
    assert spent == ["restored"]  # what it built can't be told: kept spent, not built twice


def test_build_gives_each_session_its_recorded_size(server, tmp_path, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    history = History(tmp_path / "h.db")
    server()
    tmux("new-session", "-d", "-s", "work", "-x", "178", "-y", "41")
    tmux("split-window", "-t", "work:0")
    tmux("split-window", "-t", "work:0")
    tmux("new-window", "-d", "-t", "work:1")
    R.Recorder(history).tick([], now=1000)
    stop()
    server()
    found = R.candidate(history)
    first, second = found["sessions"][0]["windows"]
    # Two of three panes: the saved layout can't apply, so tiled runs at the window's size.
    R.build(found, {(first["id"], 0), (first["id"], 1), (second["id"], 0)})
    sizes = tmux("list-windows", "-t", "work", "-F", "#{window_width}x#{window_height}")
    assert sizes.split() == ["178x41", "178x41"]


def _transcript(config, sid):
    """A one-record Claude transcript for `sid` under account dir `config`."""
    log = config / "projects" / "-src-garden" / f"{sid}.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"type":"user"}\n')


def test_recorder_keeps_each_agents_account_and_each_sessions_default(server, tmp_path,
                                                                       monkeypatch):
    server()
    personal = tmp_path / ".claude-personal"
    tmux("new-session", "-d", "-s", "work")
    tmux("set-environment", "-t", "work", "CODEX_HOME", str(tmp_path / ".codex-side"))
    tmux("set-environment", "-t", "work", "-r", "CLAUDE_CONFIG_DIR")
    pane = tmux("display-message", "-p", "-t", "work:0", "#{pane_id}")
    tmux("send-keys", "-t", pane, f"env CLAUDE_CONFIG_DIR={personal}/ API_TOKEN=hunter2 "
         "sleep 304", "Enter")
    agent = _until("^sleep 304")
    monkeypatch.setattr(R.agent_history, "live", lambda: {
        "sessions": {"s1": {"pid": agent, "harness": "claude"}}, "unknown": []})
    recorder = R.Recorder(History(tmp_path / "h.db"))
    recorder.tick([])
    (_, _, payload), = rows(recorder.history)
    snap = json.loads(payload)
    assert snap["sessions"][0]["accounts"] == {"claude": None,
                                               "codex": str(tmp_path / ".codex-side")}
    (window,) = snap["windows"].values()
    assert window["panes"][0]["binding"] == {"harness": "claude", "session_id": "s1",
                                             "account": str(personal)}
    assert "hunter2" not in payload and "API_TOKEN" not in payload  # just the account dir
    tmux("send-keys", "-t", pane, "C-c", "")


def test_resume_is_pinned_to_the_recorded_account():
    entry = {"resume_argv": ["claude", "--resume", "s1"],
             "source": f"{Path.home()}/.claude/projects/-src/s1.jsonl"}  # the older copy
    personal = f"{Path.home()}/.claude-personal"
    assert R.accounts.resume_command(entry, personal) == [
        "env", f"CLAUDE_CONFIG_DIR={personal}", "claude", "--resume", "s1"]
    assert R.accounts.resume_command(entry, f"{Path.home()}/.claude") == [
        "env", "-u", "CLAUDE_CONFIG_DIR", "claude", "--resume", "s1"]
    assert R.accounts.resume_command({"resume_argv": ["codex", "resume", "s2"]},
                                     "/x/.codex-b")[:2] == ["env", "CODEX_HOME=/x/.codex-b"]
    assert R.accounts.resume_command({"resume_argv": ["omp", "--resume", "s3"]}, "/x") == [
        "omp", "--resume", "s3"]


def test_a_conversation_gone_from_its_account_is_never_resumed_elsewhere_unasked(
        tmp_path, monkeypatch):
    personal, work, side = (tmp_path / d for d in (".claude-personal", ".claude", ".claude-b"))
    _transcript(work, "s1")
    listed = [{"dir": str(d), "provider": "claude", "name": n, "tilde": str(d)}
              for d, n in ((personal, "Personal"), (work, "Work"), (side, "Side"))]
    monkeypatch.setattr(R.accounts, "profiles", lambda: listed)
    entries = {"s1": {"last_active": "1970-01-12T13:40:00+00:00", "cwd": str(tmp_path)}}
    pane = {"cwd": str(tmp_path), "activity": "waiting",
            "binding": {"harness": "claude", "session_id": "s1", "account": str(personal)}}
    # Only the work account has it: offered, unticked, naming where it would resume.
    assert R._judge(pane, 1_000_000.0, entries) == (False, None)
    assert pane["resume_from"] == str(work)
    assert pane["note"].startswith(f"its account dir {personal} no longer has this conversation")
    assert "resume from Work" in pane["note"]
    # Two others have it: no guess.
    _transcript(side, "s1")
    pane = {**pane, "binding": dict(pane["binding"])}
    pane.pop("resume_from"), pane.pop("note")
    assert R._judge(pane, 1_000_000.0, entries)[1].endswith("several other accounts do")
    # Its own account still has it: resumed there, as recorded.
    _transcript(personal, "s1")
    assert R._judge({**pane}, 1_000_000.0, entries) == (True, None)


def _until(pid_of: str):
    deadline = time.time() + 5
    while not (found := subprocess.run(["pgrep", "-f", pid_of], capture_output=True, text=True,
                                       check=False).stdout.split()):
        assert time.time() < deadline
        time.sleep(0.05)
    return int(found[0])


def test_e2e_restored_agent_starts_on_its_recorded_account(server, tmp_path, monkeypatch):
    """Record, lose the server, rebuild, resume: on a private socket, with a stub `claude`
    that prints the config dir it was started with. The id also sits in the default dir
    (an older copy, which agent-history names), as after a Move."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(R, "READY", 1)
    bin_dir, personal = tmp_path / "bin", tmp_path / ".claude-personal"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text('#!/bin/sh\n'
                    'echo "resumed $2 on CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR-unset}"\n')
    stub.chmod(0o755)
    _transcript(personal, "s1")
    # argv, not a shell string: a shell's startup files could put the real claude first.
    shell = f"env PATH={bin_dir}:/usr/bin:/bin bash --norc --noprofile"
    history = History(tmp_path / "h.db")
    server()
    tmux("new-session", "-d", "-s", "work", *shell.split())
    tmux("set-environment", "-t", "work", "CLAUDE_CONFIG_DIR", str(personal))
    tmux("set-environment", "-t", "work", "-r", "CODEX_HOME")
    pane = tmux("display-message", "-p", "-t", "work:0", "#{pane_id}")
    tmux("send-keys", "-t", pane, f"env CLAUDE_CONFIG_DIR={personal} sleep 305", "Enter")
    agent = _until("^sleep 305")
    monkeypatch.setattr(R.agent_history, "live", lambda: {
        "sessions": {"s1": {"pid": agent, "harness": "claude"}}, "unknown": []})
    R.Recorder(history).tick([], now=1000)
    stop()
    server()
    tmux("new-session", "-d", "-s", "keep", *shell.split())  # holds default-command
    tmux("set-option", "-g", "default-command", shell)
    found = R.candidate(history)
    built = R.build(found, {(w, p["index"]) for w, win in found["windows"].items()
                            for p in win["panes"]})
    assert {f"CLAUDE_CONFIG_DIR={personal}", "-CODEX_HOME"} <= set(
        tmux("show-environment", "-t", "work").split())  # the session's default, re-applied
    monkeypatch.setattr(R.agent_history, "get", {"s1": {
        "resume_argv": ["claude", "--resume", "s1"],
        "source": str(Path.home() / ".claude/projects/-src/s1.jsonl")}}.get)
    watcher = type("W", (), {"states": [], "request_reparse": lambda self, pane: None})()
    audits = []
    asyncio.run(R.launch(built, watcher, lambda *a: audits.append(a)))
    ((restored, _),) = built
    assert audits == [("restore_resume", restored, "s1", "ok")]
    deadline = time.time() + 5
    while f"resumed s1 on CLAUDE_CONFIG_DIR={personal}\n" not in tmux(
            "capture-pane", "-pJ", "-t", restored) + "\n":
        assert time.time() < deadline, tmux("capture-pane", "-pJ", "-t", restored)
        time.sleep(0.05)
    print(tmux("capture-pane", "-pJ", "-t", restored))  # evidence with -s
    # Gone from that account since: skipped, never resumed on whatever holds the id.
    (personal / "projects" / "-src-garden" / "s1.jsonl").unlink()
    audits.clear()
    asyncio.run(R.launch(built, watcher, lambda *a: audits.append(a)))
    assert audits == [("restore_resume", restored, "s1", "skipped: its account dir lacks it")]
