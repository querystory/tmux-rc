"""History must distinguish no panes, no evidence, and reconstructed evidence."""
import json
import socket
from datetime import datetime

import pytest

from openbus.history import BACKFILL_TTL, History, state_index
from scripts.backfill_history import reconstruct


def pane(pid="%1", activity="running", **extra):
    return {"pane_id": pid, "activity": activity, "tool": "claude", "session": "work", **extra}


def test_history_survives_restart_and_records_empty_inventory(tmp_path):
    path = tmp_path / "history.db"
    h = History(path)
    h.record([pane()], "server", 600)
    h.record([pane(activity="waiting", waiting_on="external")], "server", 610)
    with h.connect() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
    h.record([pane(activity="idle")], "server", 620)
    h.record([], "server", 660)
    data = History(path).query("all", now=660)
    assert data["samples"][0]["n"] == [0, 0, 1, 0]
    assert data["samples"][-1]["n"] == [0, 0, 0, 0]
    assert all(s["source"] == "daemon" for s in data["samples"])


def test_heartbeat_and_outage(tmp_path):
    h = History(tmp_path / "h.db")
    h.record([pane()], "s", 600)
    h.record([pane()], "s", 660)
    data = h.query("all", now=1000)
    assert data["samples"][2]["n"] == [0, 1, 0, 0]
    assert data["samples"][-1]["n"] is None
    assert data["samples"][-1]["source"] == "gap"


def test_backfill_is_idempotent_expires_and_never_masks_live_outage(tmp_path):
    h = History(tmp_path / "h.db")
    rows = [(600, "s:%1:10", "claude", 1, 999999), (660, "s:%2:20", "codex", 0, 999999)]
    assert h.import_logs(rows) == 2
    assert h.import_logs(rows) == 0
    sample = h.query("all", now=700)["samples"][-1]
    assert sample["n"] == [1, 1, 0, 0]
    assert sample["source"] == "logs"
    assert sample["groups"][0]["session"] is None
    assert h.query("all", now=700 + BACKFILL_TTL)["samples"][-1]["n"] is None
    h.record([], "s", 720)
    assert h.query("all", now=730)["samples"][-1]["n"] == [0, 0, 0, 0]
    assert h.query("all", now=1000)["samples"][-1]["source"] == "gap"


def test_identity_and_dimensions_are_structural_only(tmp_path):
    h = History(tmp_path / "h.db")
    h.record([pane(session="new", tool="codex", headline="SECRET")], "boot:pid", 600)
    with h.connect() as db:
        payload = db.execute("SELECT panes FROM snapshots").fetchone()[0]
    assert "SECRET" not in payload
    assert json.loads(payload)[0]["uid"] == "boot:pid:%1:unknown"
    assert h.query(now=600)["samples"][-1]["groups"] == [
        {"session": "new", "tool": "codex", "n": [0, 1, 0, 0]},
    ]


@pytest.mark.parametrize(("activity", "waiting_on", "expected"), [
    ("running", None, 1), ("waiting", "user", 0), ("waiting", "external", 1),
    ("compacting", None, 1), ("idle", None, 2), ("nonsense", None, 3),
])
def test_state_semantics(activity, waiting_on, expected):
    assert state_index({"activity": activity, "waiting_on": waiting_on}) == expected


def test_backfill_rejects_ambiguity_wrong_host_old_server_and_missing_states(tmp_path):
    trace, journal = tmp_path / "trace", tmp_path / "journal"
    stamp = "2026-09-21 12:00:00,000"
    t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()  # noqa: DTZ007
    out = {"tool": "claude", "activity": "running", "events": [{"text": "finished test"}]}
    trace.write_text(f"{stamp} OUT: {json.dumps(out)}\n")
    row = {"MESSAGE": "%1: dropped 1 re-emitted event(s), e.g. 'finished test'",
           "__REALTIME_TIMESTAMP": str(int((t + .1) * 1e6)), "_BOOT_ID": "boot",
           "_HOSTNAME": socket.gethostname(), "_SYSTEMD_USER_UNIT": "tmux-rc.service"}
    journal.write_text(json.dumps(row) + "\n")
    lifetimes = [{"server": "boot:pid", "pane_id": pid, "birth": "123",
                  "start": t - 1, "end": t + 10} for pid in ("%1", "%2")]
    rows, stats = reconstruct(trace, journal, "boot:pid", t - 1, lifetimes)
    assert rows == [(t, "boot:pid:%1:123", "claude", 1, t + 10)]
    assert stats["matched"] == 1
    assert reconstruct(trace, journal, "boot:pid", t - 1, [])[0] == []
    wrong_generation = [dict(lifetimes[0], start=t + .01)]
    assert reconstruct(trace, journal, "boot:pid", t - 1, wrong_generation)[0] == []
    overlapping = [lifetimes[0], dict(lifetimes[0], birth="456")]
    with pytest.raises(ValueError, match="Overlapping"):
        reconstruct(trace, journal, "boot:pid", t - 1, overlapping)
    assert reconstruct(trace, journal, "boot:pid", t + 10, lifetimes)[0] == []
    assert reconstruct(trace, journal, "otherboot:pid", t - 1, [])[0] == []
    second = dict(row, MESSAGE=row["MESSAGE"].replace("%1:", "%2:"))
    journal.write_text(json.dumps(row) + "\n" + json.dumps(second) + "\n")
    assert reconstruct(trace, journal, "boot:pid", t - 1, lifetimes)[1]["ambiguous_identity"] == 2
    # A second identity is ambiguous even when only the first has lifetime proof.
    assert reconstruct(trace, journal, "boot:pid", t - 1, lifetimes[:1])[0] == []
    journal.write_text(json.dumps(row) + "\n")
    trace.write_text(trace.read_text() * 2)
    assert reconstruct(trace, journal, "boot:pid", t - 1, lifetimes)[1]["ambiguous"] == 1
    row["_HOSTNAME"] = "another-machine"
    journal.write_text(json.dumps(row) + "\n")
    assert reconstruct(trace, journal, "boot:pid", t - 1, lifetimes)[0] == []


def test_all_time_query_is_bounded(tmp_path):
    h = History(tmp_path / "h.db")
    h.record([], "s", 600)
    assert len(h.query("all", now=600 + 86400 * 365)["samples"]) <= 361


def test_endpoint_validates_range(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from openbus.server import app

    monkeypatch.setattr(app.state, "history", History(tmp_path / "h.db"), raising=False)
    client = TestClient(app)
    assert client.get("/api/history?window=bogus").status_code == 400
    assert client.get("/api/history?window=all").json()["samples"][0]["n"] is None


def test_watcher_publishes_full_inventory_to_history(tmp_path, monkeypatch):
    from openbus import tmux
    from openbus.watcher import Watcher

    monkeypatch.setattr(tmux, "server_uid", lambda **_kwargs: "server")
    history = History(tmp_path / "h.db")
    watcher = Watcher(None, history=history)
    watcher._publish_states([pane()])
    assert history.query()["samples"][-1]["n"] == [0, 1, 0, 0]
    watcher._publish_states([])
    assert history.query()["samples"][-1]["n"] == [0, 0, 0, 0]


def test_history_unavailable_is_explicit(monkeypatch):
    from fastapi.testclient import TestClient

    from openbus.server import app

    monkeypatch.setattr(app.state, "history", None, raising=False)
    assert TestClient(app).get("/api/history").status_code == 503


@pytest.mark.parametrize(("code", "message"), [(124, "timeout"), (1, "permission denied")])
def test_collection_failure_does_not_record_empty_inventory(tmp_path, monkeypatch, code, message):
    import subprocess

    from openbus import tmux
    from openbus.watcher import Watcher

    history = History(tmp_path / "h.db")
    history.record([pane()], "server", 600)
    watcher = Watcher(None, history=history)

    def fail(_args):
        raise subprocess.CalledProcessError(code, "tmux", stderr=message)

    monkeypatch.setattr(tmux, "_run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        watcher._tick()
    with history.connect() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
    assert history.query("all", now=1000)["samples"][-1]["n"] is None


def test_confirmed_absent_server_records_empty_inventory(tmp_path, monkeypatch):
    import subprocess

    from openbus import tmux
    from openbus.watcher import Watcher

    history = History(tmp_path / "h.db")

    def absent(_args):
        raise subprocess.CalledProcessError(
            1, "tmux", stderr="no server running on /tmp/tmux/default",
        )

    monkeypatch.setattr(tmux, "_run", absent)
    monkeypatch.setattr(tmux, "server_uid", lambda **_kwargs: "server")
    Watcher(None, history=history)._tick()
    assert history.query()["samples"][-1]["n"] == [0, 0, 0, 0]


def test_foreign_history_database_does_not_prevent_startup(monkeypatch):
    import asyncio

    from openbus import server

    def foreign(_path):
        raise ValueError("host mismatch")

    async def stop(_self):
        pass

    monkeypatch.setattr(server, "History", foreign)
    monkeypatch.setattr(server.Watcher, "start", lambda _self: None)
    monkeypatch.setattr(server.Watcher, "stop", stop)

    async def check():
        async with server.lifespan(server.app):
            assert server.app.state.history is None

    asyncio.run(check())


def test_recycled_pane_records_identity_change_before_heartbeat(tmp_path):
    h = History(tmp_path / "h.db")
    h.record([pane()], "server", 600, births={"%1": "100"})
    h.record([pane()], "server", 601, births={"%1": "101"})
    with h.connect() as db:
        rows = db.execute("SELECT panes FROM snapshots ORDER BY t").fetchall()
    assert [json.loads(row[0])[0]["uid"] for row in rows] == ["server:%1:100", "server:%1:101"]


def test_log_state_never_crosses_verified_lifetime_end(tmp_path):
    h = History(tmp_path / "h.db")
    h.import_logs([(600, "s:%1:100", "claude", 1, 660),
                   (720, "s:%1:101", "codex", 0, 900)])
    samples = h.query("all", now=800)["samples"]
    assert samples[0]["n"] == [0, 1, 0, 0]
    assert samples[1]["n"] is None
    assert samples[-1]["n"] == [1, 0, 0, 0]


def test_legacy_imports_retained_but_not_used_without_lifetime_proof(tmp_path):
    import sqlite3

    path = tmp_path / "h.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE log_observations (t REAL, uid TEXT, tool TEXT, state INTEGER, "
                   "PRIMARY KEY(t, uid))")
        db.execute("INSERT INTO log_observations VALUES (600, 's:%1', 'claude', 1)")
    h = History(path)
    assert h.query("all", now=700)["samples"][-1]["n"] is None
    with h.connect() as db:
        assert db.execute("SELECT count(*) FROM log_observations").fetchone()[0] == 1


def test_database_and_recreated_sidecars_are_private(tmp_path):
    import stat

    path = tmp_path / "h.db"
    path.touch(mode=0o644)
    h = History(path)
    for value in (1, 2):
        with h.connect() as db:
            db.execute("INSERT INTO metadata VALUES (?, 'sidecar-test')", (str(value),))
            for file in (path, tmp_path / "h.db-wal", tmp_path / "h.db-shm"):
                assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_shared_parent_is_rejected_without_changing_its_permissions(tmp_path):
    import stat

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private directory"):
        History(shared / "h.db")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_heartbeats_coalesce_without_losing_outages_or_state_changes(tmp_path):
    h = History(tmp_path / "h.db")
    for now in range(600, 87000, 60):
        h.record([pane()], "s", now)
    with h.connect() as db:
        assert db.execute("SELECT count(*) FROM inventory_payloads").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
    h.record([pane(activity="idle")], "s", 87000)
    h.record([pane()], "s", 87060)
    h.record([pane()], "s", 88000)
    with h.connect() as db:
        assert db.execute("SELECT count(*) FROM inventory_payloads").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 4
    samples = h.query("1h", now=88000)["samples"]
    assert any(s["source"] == "gap" for s in samples)
    assert samples[-1]["n"] == [0, 1, 0, 0]


def test_legacy_snapshot_migration_preserves_history_and_gaps(tmp_path):
    import sqlite3

    path = tmp_path / "h.db"
    payload = json.dumps([{"uid": "s:%1", "session": "work", "tool": "claude", "state": 1}])
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE snapshots (t REAL PRIMARY KEY, panes TEXT NOT NULL)")
        db.executemany("INSERT INTO snapshots VALUES (?, ?)", [(600, payload), (660, payload),
                                                             (1000, payload), (1060, "[]")])
    for _ in range(2):
        h = History(path)
        with h.connect() as db:
            assert db.execute("SELECT t, last_seen FROM snapshots ORDER BY t").fetchall() == [
                (600, 660), (1000, 1000), (1060, 1060),
            ]
            assert db.execute("SELECT count(*) FROM inventory_payloads").fetchone()[0] == 2
        samples = h.query("all", now=1060)["samples"]
        assert samples[0]["n"] == [0, 1, 0, 0]
        assert any(s["source"] == "gap" for s in samples)
        assert samples[-1]["n"] == [0, 0, 0, 0]


def test_verified_reimport_restores_legacy_row_without_overwriting_verified_data(tmp_path):
    h = History(tmp_path / "h.db")
    with h.connect() as db:
        db.execute("INSERT INTO log_observations VALUES (600, 's:%1:10', 'claude', 2, NULL)")
    rows = [(600, "s:%1:10", "claude", 1, 900)]
    assert h.import_logs(rows) == 1
    assert h.import_logs(rows) == 0
    assert h.query("all", now=600)["samples"][-1]["n"] == [0, 1, 0, 0]
    assert h.import_logs([(600, "s:%1:10", "codex", 0, 999)]) == 0
    assert h.query("all", now=600)["samples"][-1]["n"] == [0, 1, 0, 0]


def test_progressive_ui_inventory_is_not_persisted(tmp_path, monkeypatch):
    from openbus import tmux
    from openbus.watcher import Watcher

    monkeypatch.setattr(tmux, "server_uid", lambda **_kwargs: "s")
    history = History(tmp_path / "h.db")
    watcher = Watcher(None, history=history)
    watcher._publish_states([pane(activity="unknown")], record_history=False)
    assert watcher.states[0]["activity"] == "unknown"
    with history.connect() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
    watcher._publish_states([pane()])
    assert history.query()["samples"][-1]["n"] == [0, 1, 0, 0]


def test_historical_unknown_session_is_not_a_real_session_name(tmp_path):
    h = History(tmp_path / "h.db")
    h.import_logs([(600, "s:%1:10", "claude", 1, 900)])
    h.record([pane(session="(historical session unknown)")], "s", 720)
    samples = h.query("all", now=720)["samples"]
    assert samples[0]["groups"][0]["session"] is None
    assert samples[-1]["groups"][0]["session"] == "(historical session unknown)"


@pytest.mark.parametrize("override", [{"birth": None}, {"birth": " "}, {"start": "600"},
                                      {"start": True}, {"end": None}, {"end": float("nan")},
                                      {"end": 599}, {"pane_id": 1}])
def test_backfill_rejects_invalid_interval_fields(tmp_path, override):
    life = {"server": "s", "pane_id": "%1", "birth": "10", "start": 600, "end": 900}
    with pytest.raises(ValueError, match="Invalid pane lifetime"):
        reconstruct(tmp_path / "trace", tmp_path / "journal", "s", 500, [dict(life, **override)])


def test_backfill_rejects_overlap_after_an_earlier_observation(tmp_path):
    lives = [{"server": "s", "pane_id": "%1", "birth": "10", "start": 600, "end": 900},
             {"server": "s", "pane_id": "%1", "birth": "20", "start": 800, "end": 1000}]
    # Reject the interval set before reading observations, even one at 700 which
    # initially matches only the first life but could carry into the second birth.
    with pytest.raises(ValueError, match="Overlapping"):
        reconstruct(tmp_path / "trace", tmp_path / "journal", "s", 500, lives)
