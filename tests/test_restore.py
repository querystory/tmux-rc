"""Reboot restore's recorder and offer (docs/design/reboot-restore.md, "The first PR").

Runs a REAL tmux on private sockets (TMUXRC_TMUX_SOCKET), never the developer's server.
Cleanup closes the exact panes each test made on its own socket; no kill-server."""

import json
import os
import shutil
import subprocess
import time
import uuid

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
    assert recorder._bindings({pane: tmux("display-message", "-p", "-t", pane, "#{pane_pid}")}) \
        == {pane: {"harness": "codex", "session_id": "front"}}
    # agent-history can't tell now: the same foreground keeps its binding.
    live = {"sessions": {}, "unknown": ["codex"]}
    assert recorder._bindings({pane: tmux("display-message", "-p", "-t", pane, "#{pane_pid}")}) \
        == {pane: {"harness": "codex", "session_id": "front"}}
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
    monkeypatch.setattr(R.agent_history, "get", entries.get)

    def judge(**pane):
        return R._judge({"cwd": str(tmp_path), **pane}, died)

    # The screen clock says stale; the transcript says recent, and wins.
    assert judge(binding={"session_id": "recent"}, last_activity=0) == (True, None)
    # The screen clock says recent; the transcript says four days idle, and wins.
    assert judge(binding={"session_id": "stale"}, last_activity=died) == (False, None)
    assert judge(binding={"session_id": "stale"}, activity="waiting") == (True, None)
    assert judge(binding={"session_id": "live"}) == (False, "it's already running")
    assert judge(binding={"session_id": "gone"}) == (False, "its conversation is gone")
    assert judge(cwd="/no/such/dir") == (False, "its directory is gone")
    assert judge(command="bash", last_activity=died) == (False, None)
    assert judge(command="vim", last_activity=died - 60) == (True, None)
