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
    rows = [(600, "s:%1", "claude", 1), (660, "s:%2", "codex", 0)]
    assert h.import_logs(rows) == 2
    assert h.import_logs(rows) == 0
    sample = h.query("all", now=700)["samples"][-1]
    assert sample["n"] == [1, 1, 0, 0]
    assert sample["source"] == "logs"
    assert sample["groups"][0]["session"] == "(historical session unknown)"
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
    assert json.loads(payload)[0]["uid"] == "boot:pid:%1"
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
    rows, stats = reconstruct(trace, journal, "boot:pid", t - 1)
    assert rows == [(t, "boot:pid:%1", "claude", 1)]
    assert stats["matched"] == 1
    assert reconstruct(trace, journal, "boot:pid", t + 10)[0] == []
    assert reconstruct(trace, journal, "otherboot:pid", t - 1)[0] == []
    second = dict(row, MESSAGE=row["MESSAGE"].replace("%1:", "%2:"))
    journal.write_text(json.dumps(row) + "\n" + json.dumps(second) + "\n")
    assert reconstruct(trace, journal, "boot:pid", t - 1)[1]["ambiguous_identity"] == 2
    journal.write_text(json.dumps(row) + "\n")
    trace.write_text(trace.read_text() * 2)
    assert reconstruct(trace, journal, "boot:pid", t - 1)[1]["ambiguous"] == 1
    row["_HOSTNAME"] = "another-machine"
    journal.write_text(json.dumps(row) + "\n")
    assert reconstruct(trace, journal, "boot:pid", t - 1)[0] == []


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

    monkeypatch.setattr(tmux, "server_uid", lambda: "server")
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
    monkeypatch.setattr(tmux, "server_uid", lambda: "server")
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
