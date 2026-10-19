"""agent_history.live() fails closed and says so. A binary built before the `live`
subcommand existed prints "unknown command" on stdout and exits 0; for weeks that made
every restore snapshot bind no agent to its conversation, with nothing in the log."""

import subprocess

from openbus import agent_history


def test_live_failure_is_none_and_warns_once(monkeypatch, caplog):
    stale = subprocess.CompletedProcess([], 0, "unknown command: live\n", "")
    monkeypatch.setattr(agent_history, "binary", lambda: "/stale/agent-history")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: stale)
    agent_history._warn_unavailable.cache_clear()
    with caplog.at_level("WARNING", logger="openbus.agent_history"):
        assert agent_history.live() is None
        assert agent_history.live() is None  # the next minute's snapshot
    hits = [r.getMessage() for r in caplog.records if "/stale/agent-history" in r.getMessage()]
    assert len(hits) == 1, hits
    assert "rebuild it from agent-history/" in hits[0]
