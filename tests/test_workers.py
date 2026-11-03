"""Invented worker inventories exercise identity, freshness, and complete rosters."""
import copy
import json

import pytest

from openbus.workers import Reader, reconcile, validate


def snapshot():
    return {"version": 1, "source_id": "garden", "run_id": "run-1", "sequence": 1,
            "observed_at": 100, "controllers": [{"pane_id": "%1", "birth": "b", "server": "s"}],
            "workers": [{"id": "prune", "controller_pane_id": "%1", "tool": "codex",
                         "activity": "running"}]}


def test_complete_roster_preserves_original_and_unknown_on_expiry():
    panes = [{"pane_id": "%1", "birth": "b", "session": "garden",
              "subagents": [{"state": "running"}], "agents": 1}]
    before = copy.deepcopy(panes)
    published, workers = reconcile(snapshot(), panes, "s", 100)
    assert panes == before
    assert published[0]["subagents"] == []
    assert workers[0]["activity"] == "running"
    assert reconcile(snapshot(), panes, "s", 131)[1][0]["activity"] == "unknown"
    assert reconcile(snapshot(), panes, "wrong-server", 100) == (panes, [])
    assert reconcile(snapshot(), [{**panes[0], "birth": "reused"}], "s", 100)[1] == []


def test_binding_requires_worker_pane_lifetime():
    value = snapshot()
    value["workers"][0]["pane"] = {"pane_id": "%2", "birth": "child", "server": "s"}
    panes = [{"pane_id": "%1", "birth": "b"}, {"pane_id": "%2", "birth": "child"}]
    assert reconcile(value, panes, "s", 100)[1][0]["pane_id"] == "%2"
    reused = [panes[0], {**panes[1], "birth": "new"}]
    assert reconcile(value, reused, "s", 100)[1][0]["pane_id"] is None


@pytest.mark.parametrize("change", [
    {"observed_at": float("nan")}, {"observed_at": 200}, {"sequence": True},
    {"controllers": []}, {"workers": [{"id": "x", "activity": "waiting"}]},
])
def test_invalid_inventory_rejected(change):
    with pytest.raises(ValueError):
        validate({**snapshot(), **change}, 100)


def test_reader_retains_evidence_and_rejects_old_sequences(tmp_path):
    path = tmp_path / "workers.json"
    path.write_text(json.dumps(snapshot()))
    reader = Reader(str(path))
    assert reader.read(100) == "fresh"
    path.write_text("{")
    assert reader.read(101) == "unavailable"
    assert reader.read(131) == "stale"
    path.write_text(json.dumps({**snapshot(), "sequence": 0, "workers": []}))
    assert reader.read(132) == "stale"
    assert reader.snapshot["workers"]
    path.write_text(json.dumps({**snapshot(), "sequence": 2, "observed_at": 132, "workers": []}))
    assert reader.read(132) == "fresh"
    assert reader.snapshot["workers"] == []
    assert Reader(str(path)).read(200) == "stale"


def test_worker_changes_wake_clients_but_heartbeat_does_not(tmp_path, monkeypatch):
    from openbus.watcher import Watcher

    path = tmp_path / "workers.json"
    monkeypatch.setenv("TMUXRC_WORKER_SNAPSHOT", str(path))
    monkeypatch.setattr("openbus.watcher.time.time", lambda: 100)
    monkeypatch.setattr("openbus.watcher.tmux.server_uid", lambda **_: "s")
    path.write_text(json.dumps(snapshot()))
    watcher = Watcher(None)
    panes = [{"pane_id": "%1", "birth": "b", "session": "garden"}]
    watcher._publish_states(panes, record_history=False)
    version = watcher.state_version()
    value = {**snapshot(), "sequence": 2, "observed_at": 101}
    path.write_text(json.dumps(value))
    watcher._publish_states(panes, record_history=False)
    assert watcher.state_version() == version
    value["sequence"] = 3
    value["workers"][0]["activity"] = "idle"
    path.write_text(json.dumps(value))
    watcher._publish_states(panes, record_history=False)
    assert watcher.state_version() == version + 1
    assert watcher.workers[0]["activity"] == "idle"


def test_unread_screen_keeps_reported_workers_in_history(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from openbus.watcher import Watcher

    path = tmp_path / "workers.json"
    monkeypatch.setenv("TMUXRC_WORKER_SNAPSHOT", str(path))
    monkeypatch.setattr("openbus.watcher.time.time", lambda: 100)
    monkeypatch.setattr("openbus.watcher.tmux.server_uid", lambda **_: "s")
    path.write_text(json.dumps(snapshot()))
    history = Mock()
    watcher = Watcher(None, history=history)
    watcher._publish_states([{"pane_id": "%1", "birth": "b", "session": "garden"}],
                            history_server="s", unread={"%1"})
    args, kwargs = history.record.call_args
    assert args[0][0]["activity"] == "unknown"
    assert kwargs["workers"][0]["activity"] == "running"


@pytest.mark.parametrize("field", ["controllers", "workers"])
def test_duplicate_identity_cannot_replace_good_roster(tmp_path, field):
    path = tmp_path / "workers.json"
    good = snapshot()
    path.write_text(json.dumps(good))
    reader = Reader(str(path))
    assert reader.read(100) == "fresh"
    invalid = {**snapshot(), "sequence": 2}
    invalid[field] = invalid[field] * 2
    path.write_text(json.dumps(invalid))
    assert reader.read(101) == "unavailable"
    assert reader.snapshot == good


def test_retired_run_cannot_return_even_with_newer_timestamp(tmp_path):
    path = tmp_path / "workers.json"
    reader = Reader(str(path))
    path.write_text(json.dumps(snapshot()))
    assert reader.read(100) == "fresh"
    second = {**snapshot(), "run_id": "run-2", "observed_at": 110}
    path.write_text(json.dumps(second))
    assert reader.read(110) == "fresh"
    path.write_text(json.dumps({**snapshot(), "observed_at": 120, "sequence": 4}))
    assert reader.read(120) == "unavailable"
    assert reader.snapshot == second


def test_oversized_inventory_keeps_last_known_workers(tmp_path):
    from openbus.workers import MAX_BYTES

    path = tmp_path / "workers.json"
    path.write_text(json.dumps(snapshot()))
    reader = Reader(str(path))
    assert reader.read(100) == "fresh"
    path.write_bytes(b" " * (MAX_BYTES + 1))
    assert reader.read(131) == "stale"
    assert reader.snapshot["workers"]


def test_done_and_waiting_workers_keep_independent_semantics():
    value = snapshot()
    value["workers"] = [{**value["workers"][0], "id": target, "activity": "waiting",
                         "waiting_on": target} for target in ("user", "external")]
    value["workers"].append({**value["workers"][0], "id": "finished", "activity": "done"})
    panes = [{"pane_id": "%1", "birth": "b", "activity": "idle"}]
    published, reported = reconcile(validate(value, 100), panes, "s", 100)
    assert published[0]["activity"] == "idle"
    assert {w["waiting_on"] for w in reported} == {"user", "external"}
    assert len(reported) == 2
    assert all(w["activity"] == "unknown" and w["waiting_on"] is None
               for w in reconcile(value, panes, "s", 131)[1])


@pytest.mark.parametrize("stamp", [float("inf"), -1, True, 106, 10**400, -(10**400)])
def test_untrustworthy_timestamps_never_supply_live_activity(tmp_path, stamp):
    path = tmp_path / "workers.json"
    path.write_text(json.dumps({**snapshot(), "observed_at": stamp}))
    reader = Reader(str(path))
    assert reader.read(100) == "unavailable"
    assert reader.snapshot is None


def test_deeply_nested_file_cannot_interrupt_collection(tmp_path):
    path = tmp_path / "workers.json"
    path.write_text(json.dumps(snapshot()))
    reader = Reader(str(path))
    assert reader.read(100) == "fresh"
    path.write_text("[" * 10000 + "]" * 10000)
    assert reader.read(101) == "unavailable"
    assert reader.snapshot == snapshot()


def test_initially_missing_source_records_unmeasured_workers(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from openbus.watcher import Watcher

    monkeypatch.setenv("TMUXRC_WORKER_SNAPSHOT", str(tmp_path / "missing.json"))
    monkeypatch.setattr("openbus.watcher.tmux.server_uid", lambda **_: "s")
    history = Mock()
    watcher = Watcher(None, history=history)
    watcher._publish_states([{"pane_id": "%1", "birth": "b", "activity": "idle"}],
                            history_server="s")
    args, kwargs = history.record.call_args
    assert args[0][0]["activity"] == "idle"
    assert kwargs["workers"] == []
    assert kwargs["workers_observed"] is False


def test_oversized_timestamp_does_not_interrupt_pane_publication(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from openbus.watcher import Watcher

    path = tmp_path / "workers.json"
    monkeypatch.setenv("TMUXRC_WORKER_SNAPSHOT", str(path))
    monkeypatch.setattr("openbus.watcher.time.time", lambda: 100)
    monkeypatch.setattr("openbus.watcher.tmux.server_uid", lambda **_: "s")
    path.write_text(json.dumps(snapshot()))
    history = Mock()
    watcher = Watcher(None, history=history)
    controller = {"pane_id": "%1", "birth": "b", "session": "garden", "activity": "idle"}
    ordinary = {"pane_id": "%2", "birth": "c", "session": "orchard", "activity": "idle"}
    watcher._publish_states([controller, ordinary], history_server="s")
    path.write_text(json.dumps({**snapshot(), "sequence": 2, "observed_at": 10**400}))
    watcher._publish_states([controller, {**ordinary, "activity": "running"}], history_server="s")
    assert watcher.worker_source == {"status": "unavailable"}
    assert watcher.states[1]["activity"] == "running"
    assert watcher.workers[0]["activity"] == "running"
    args, kwargs = history.record.call_args
    assert args[0][1]["activity"] == "running"
    assert kwargs["workers"][0]["activity"] == "running"


def test_custom_harness_flows_from_file_to_its_own_history_group(tmp_path):
    from openbus.history import History

    value = snapshot()
    value["workers"][0]["tool"] = "garden-runner"
    path = tmp_path / "workers.json"
    path.write_text(json.dumps(value))
    reader = Reader(str(path))
    assert reader.read(100) == "fresh"
    controller = {"pane_id": "%1", "birth": "b", "session": "garden",
                  "tool": "shell", "activity": "idle"}
    panes, workers = reconcile(reader.snapshot, [controller], "s", 100)
    history = History(tmp_path / "history.db")
    history.record(panes, "s", 100, workers=workers)
    sample = history.query(now=100)["samples"][-1]
    custom = next(g for g in sample["groups"] if g["tool"] == "garden-runner")
    assert custom["session"] == "garden"
    assert custom["n"] == [0] * 7
    assert custom["background"] == [0, 1, 0, 0, 0, 0, 0]
    assert sample["n"] == [0, 0, 1, 0, 0, 0, 0]
