"""Pane cards survive a daemon restart (docs/design/activity-clock-persistence.md).

Each "daemon" is a fresh Watcher over the same SQLite file. tmux, the clock, the
parser and the bootstrap are stubbed; a parse or bootstrap call stands for an LLM call."""

import json
import os
import sqlite3

import pytest

import openbus.watcher as W
from openbus.history import MIGRATIONS, History

SCREEN = f"old scrollback\n{W.tmux.VISIBLE_SCREEN}\n› done, tests pass"


def daemon(monkeypatch, db, *, screen=SCREEN, pid="101", now=10_000.0, activity="9000",
           history=None, listed=True, server=lambda **_kwargs: "boot:1"):
    pane = W.tmux.Pane("work", "0", "agent", "0", "%1", "node", "Task", pid=pid,
                       window_activity=activity)
    monkeypatch.setattr(W.tmux, "server_running", lambda: True)
    monkeypatch.setattr(W.tmux, "list_panes", lambda: [pane] if listed else [])
    monkeypatch.setattr(W.tmux, "active_pane_id", lambda: "%1")
    monkeypatch.setattr(W.tmux, "server_uid", server)
    monkeypatch.setattr(W.tmux, "capture_pane", lambda *args, **kwargs: screen)
    monkeypatch.setattr(W.time, "time", lambda: now)
    monkeypatch.setattr(W, "backing_off", lambda: False)
    calls = []

    def parse(pane, text, llm_fn=None, **_kwargs):
        calls.append("parse")
        return {"pane_id": pane.id, "activity": "idle", "tool": "codex",
                "headline": "Fixed the flaky test",
                "events": [{"text": "ran the suite"}]}

    def boot(*_args, **_kwargs):
        calls.append("bootstrap")
        return {"summary": "Hunting a flaky test", "name": "Flaky test",
                "events": [{"text": "found the race", "historical": True}]}

    monkeypatch.setattr(W, "classify", parse)
    monkeypatch.setattr(W, "bootstrap", boot)
    w = W.Watcher(None, use_llm=True, history=history or History(db))
    monkeypatch.setattr(w, "_pane_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(w, "_repository_for", lambda pane: None)
    w._tick()
    return w, calls


def card(w):
    (s,) = w.states
    return {k: s.get(k) for k in ("headline", "activity", "last_activity_at", "state_since",
                                  "session_summary", "title", "events_seq")}


def test_unchanged_screen_restores_clocks_and_card_without_llm(monkeypatch, tmp_path):
    db = tmp_path / "h.db"
    first, calls = daemon(monkeypatch, db)
    assert calls == ["parse", "bootstrap"]
    before = card(first)
    assert before["last_activity_at"] == before["state_since"] == 9000.0
    assert before["session_summary"] == "Hunting a flaky test"
    # Restart later: tmux saw a footer redraw, and scrollback slid above the screen.
    screen = SCREEN.replace("old scrollback", "newer scrollback")
    history, saves = History(db), []
    monkeypatch.setattr(history, "save_checkpoints", lambda rows, now=None: saves.append(rows))
    w, calls = daemon(monkeypatch, db, screen=screen, now=20_000.0, activity="19000",
                      history=history)
    assert calls == [], "an unchanged screen must not cost an LLM call"
    assert card(w) == before
    assert w.snapshot_text("%1", w.states[0]["snapshot_id"]) is not None
    assert [e["text"] for e in w.events_log["%1"]] == ["found the race", "ran the suite"]
    # Neither the restore nor later unchanged ticks rewrite the row.
    w._tick()
    assert saves == [] and calls == []


@pytest.mark.parametrize("change", [{"screen": "› something new"}, {"pid": "202"}])
def test_changed_screen_or_recycled_pid_falls_back(monkeypatch, tmp_path, change):
    db = tmp_path / "h.db"
    daemon(monkeypatch, db)
    w, calls = daemon(monkeypatch, db, now=20_000.0, activity="19000", **change)
    assert calls == ["parse", "bootstrap"]
    assert card(w)["last_activity_at"] == card(w)["state_since"] == 19000.0
    with w.history.connect() as db_:
        uids = {r[0] for r in db_.execute("SELECT uid FROM pane_checkpoints")}
    # A recycled pid is a new pane; the old occupant's row is pruned as gone.
    assert uids == {f"boot:1:%1:{change.get('pid', '101')}"}


def test_database_failure_falls_back(monkeypatch, tmp_path):
    db = tmp_path / "h.db"
    daemon(monkeypatch, db)
    history = History(db)

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(history, "load_checkpoints", broken)
    w, calls = daemon(monkeypatch, db, now=20_000.0, activity="19000", history=history)
    assert calls == ["parse", "bootstrap"]
    assert card(w)["last_activity_at"] == 19000.0


def test_prunes_gone_panes_and_dead_servers(monkeypatch, tmp_path):
    db = tmp_path / "h.db"
    history = History(db)
    row = {"fp": "x", "last_activity_at": 1.0, "idle_since": None, "card": None}
    live = f"boot:{os.getpid()}"
    history.save_checkpoints([{**row, "uid": f"{server}:%1:5", "server": server} for server in (
        "boot:1",           # this server: its %1:5 pane is gone
        "oldboot:77",       # previous boot
        "boot:999999999",   # this boot, pid not running
        live,               # this boot, still running: kept however long it sits still
    )])
    daemon(monkeypatch, db, history=history)
    assert set(History(db).load_checkpoints()) == {f"{live}:%1:5", "boot:1:%1:101"}


def test_migrates_old_schema_and_refuses_newer(tmp_path):
    path = tmp_path / "h.db"
    with sqlite3.connect(path) as db:  # pre-versioning database: user_version 0
        db.execute("CREATE TABLE log_observations (t REAL NOT NULL, uid TEXT NOT NULL, "
                   "tool TEXT NOT NULL, state INTEGER NOT NULL CHECK(state BETWEEN 0 AND 3), "
                   "PRIMARY KEY(t, uid))")
        db.execute("INSERT INTO log_observations VALUES (600, 's:%1', 'claude', 1)")
    h = History(path)
    with h.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert db.execute("SELECT * FROM log_observations").fetchall() == [
            (600, "s:%1", "claude", 1, None)]
        db.execute("INSERT INTO log_observations VALUES (601, 's:%1', 'claude', 5, NULL)")
        db.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
    assert h.load_checkpoints() == {}
    with pytest.raises(ValueError, match="newer"):
        History(path)


def test_empty_listing_prunes_this_servers_rows(monkeypatch, tmp_path):
    db = tmp_path / "h.db"
    daemon(monkeypatch, db)
    daemon(monkeypatch, db, listed=False)
    assert History(db).load_checkpoints() == {}


def test_same_screen_keeps_earliest_clocks_across_daemons(tmp_path):
    h = History(tmp_path / "h.db")
    row = {"uid": "s:%1:5", "server": "s", "fp": "x", "card": None}
    h.save_checkpoints([{**row, "last_activity_at": 100.0, "idle_since": 150.0}])
    h.save_checkpoints([{**row, "last_activity_at": 200.0, "idle_since": 250.0}])
    stored = h.load_checkpoints()["s:%1:5"]
    assert (stored["last_activity_at"], stored["idle_since"]) == (100.0, 150.0)
    h.save_checkpoints([{**row, "fp": "y", "last_activity_at": 300.0, "idle_since": None}])
    stored = h.load_checkpoints()["s:%1:5"]
    assert (stored["last_activity_at"], stored["idle_since"]) == (300.0, None)


def test_tmux_restart_mid_tick_writes_no_checkpoint(monkeypatch, tmp_path):
    seen = []

    def server(**_kwargs):
        seen.append(1)
        return "boot:1" if len(seen) == 1 else "boot:2"

    w, _ = daemon(monkeypatch, tmp_path / "h.db", server=server)
    assert w.history.load_checkpoints() == {}


def test_retiring_a_pr_rewrites_the_checkpoint(monkeypatch, tmp_path):
    w, _ = daemon(monkeypatch, tmp_path / "h.db")
    monkeypatch.setattr(W, "summarize_events", lambda texts: None)
    w._prs["%1"] = [{"repo": "o/r", "number": 1}]
    w._pr_titles._cache[("o/r", 1)] = (float("inf"), {"title": "t", "state": "OPEN"})
    w._tick()  # the PR joins the card
    saves = []
    monkeypatch.setattr(w.history, "save_checkpoints", lambda rows, now=None: saves.append(rows))
    w._pr_titles._cache[("o/r", 1)] = (float("inf"), {"title": "t", "state": "MERGED"})
    w._tick()
    assert w._prs["%1"] == [] and len(saves) == 1


def test_a_card_checkpointed_without_a_frame_is_parsed_again(monkeypatch, tmp_path):
    """The checkpoint key strips durations, so "sleep 10s" can restore over "sleep 20s":
    a frame taken off today's screen would let the old question's digit approve it."""
    db = tmp_path / "h.db"
    daemon(monkeypatch, db)
    with sqlite3.connect(db) as conn:
        (raw,) = conn.execute("SELECT card FROM pane_checkpoints").fetchone()
        old = json.loads(raw)
        old["state"].pop("frame")
        conn.execute("UPDATE pane_checkpoints SET card = ?", (json.dumps(old),))
    w, calls = daemon(monkeypatch, db, now=20_000.0, activity="19000")
    assert "parse" in calls
    assert w.states[0]["frame"]


def test_an_expunged_pane_is_never_checkpointed_again(monkeypatch, tmp_path):
    w, _ = daemon(monkeypatch, tmp_path / "h.db")
    monkeypatch.setattr(W, "summarize_events", lambda texts: None)
    assert w.history.load_checkpoints()
    delete = w.history.delete_checkpoints
    monkeypatch.setattr(w.history, "delete_checkpoints", lambda uids: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")))
    # The database is busy: the deletion is reported as not done, and waits for a tick.
    assert not w.forget_checkpoint(w.checkpoint_key("%1", "101"))
    assert w.history.load_checkpoints()
    monkeypatch.setattr(w.history, "delete_checkpoints", delete)
    w._checkpointed.clear()  # stands for a tick that captured the pane before it closed
    w._tick()
    assert w.history.load_checkpoints() == {}
