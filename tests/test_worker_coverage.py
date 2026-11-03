"""Worker coverage follows evidence age independently of pane collection."""
import json

import pytest

from openbus.history import History
from openbus.watcher import Watcher


@pytest.mark.parametrize("roster", [[], [{"id": "worker", "tool": "codex", "activity": "running",
    "controller_pane_id": "%1", "pane": {"pane_id": "%2", "birth": "2", "server": "s"}}]])
def test_empty_background_coverage_expires_recovers_and_survives_restart(
    tmp_path, monkeypatch, roster,
):
    from openbus import watcher

    clock = [600]
    monkeypatch.setattr(watcher.time, "time", lambda: clock[0])
    monkeypatch.setattr(watcher.tmux, "server_uid", lambda **_: "s")
    path = tmp_path / "workers.json"
    monkeypatch.setenv("TMUXRC_WORKER_SNAPSHOT", str(path))
    inventory = {"version": 1, "source_id": "garden", "run_id": "one", "sequence": 1,
                 "observed_at": 600,
                 "controllers": [{"pane_id": "%1", "birth": "1", "server": "s"}],
                 "workers": roster}
    path.write_text(json.dumps(inventory))
    h = History(tmp_path / "history.db")
    w = Watcher(None, history=h)
    panes = [{"pane_id": "%1", "birth": "1", "session": "garden",
              "tool": "shell", "activity": "idle"},
             {"pane_id": "%2", "birth": "2", "session": "orchard",
              "tool": "codex", "activity": "running"}]
    w._publish_states(panes, history_server="s")
    first = h.query(now=600)["samples"][-1]
    assert first["background"] == [0] * 7
    path.write_text("{")
    clock[0] = 610
    w._publish_states(panes, history_server="s")
    assert h.query(now=610)["samples"][-1]["background"] == [0] * 7
    clock[0] = 660
    w._publish_states(panes, history_server="s")
    stale = h.query(now=660)["samples"][-1]
    assert stale["background"] is None
    assert stale["worker_gap_sessions"] == ["garden"]
    assert stale["n"] == first["n"]
    path.write_text(json.dumps(inventory))
    w = Watcher(None, history=h)
    clock[0] = 720
    w._publish_states(panes, history_server="s")
    assert h.query(now=720)["samples"][-1]["background"] is None
    path.write_text(json.dumps({**inventory, "sequence": 2, "observed_at": 780}))
    clock[0] = 780
    w._publish_states(panes, history_server="s")
    assert h.query(now=780)["samples"][-1]["background"] == [0] * 7
