"""Pane cards survive a daemon restart (docs/design/activity-clock-persistence.md).

Each "daemon" is a fresh Watcher over the same SQLite file. tmux, the clock, the
parser and the bootstrap are stubbed; a parse or bootstrap call stands for an LLM call."""

import sqlite3

import pytest

import openbus.watcher as W
from openbus.history import MIGRATIONS, History

SCREEN = f"old scrollback\n{W.tmux.VISIBLE_SCREEN}\n› done, tests pass"


def daemon(monkeypatch, db, *, screen=SCREEN, pid="101", now=10_000.0, activity="9000",
           history=None):
    pane = W.tmux.Pane("work", "0", "agent", "0", "%1", "node", "Task", pid=pid,
                       window_activity=activity)
    monkeypatch.setattr(W.tmux, "server_running", lambda: True)
    monkeypatch.setattr(W.tmux, "list_panes", lambda: [pane])
    monkeypatch.setattr(W.tmux, "active_pane_id", lambda: "%1")
    monkeypatch.setattr(W.tmux, "server_uid", lambda **_kwargs: "boot:1")
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
    w, calls = daemon(monkeypatch, db, screen=screen, now=20_000.0, activity="19000")
    assert calls == [], "an unchanged screen must not cost an LLM call"
    assert card(w) == before
    assert [e["text"] for e in w.events_log["%1"]] == ["found the race", "ran the suite"]
    # Unchanged ticks write nothing.
    saves = []
    monkeypatch.setattr(w.history, "save_checkpoints", lambda rows, now=None: saves.append(rows))
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


def test_prunes_gone_panes_and_stale_foreign_servers(monkeypatch, tmp_path):
    db = tmp_path / "h.db"
    history = History(db)
    row = {"fp": "x", "last_activity_at": 1.0, "idle_since": None, "card": None}
    history.save_checkpoints([
        {**row, "uid": "boot:1:%9:5", "server": "boot:1"},    # this server, pane gone
        {**row, "uid": "old:1:%1:5", "server": "old:1"},      # other server, stale
    ], now=1.0)
    history.save_checkpoints([{**row, "uid": "new:1:%1:5", "server": "new:1"}], now=9000.0)
    daemon(monkeypatch, db, history=history, now=W.CHECKPOINT_TTL + 5000.0)
    assert set(History(db).load_checkpoints()) == {"new:1:%1:5", "boot:1:%1:101"}


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
