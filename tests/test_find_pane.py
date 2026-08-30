"""find_pane() target resolution.

The display label prefers a user-named window over the session, so the numeric tmux
address ("work:0.0" — window/pane INDEX, not the window name) has to be matched on its
own — see issue #146.
"""

import openbus.tmux as tmux
from openbus.tmux import Pane, find_pane

# session, window_index, window_name, pane_index, id, cmd, title, cwd
NAMED = Pane("work", "0", "Resolve PR 38", "0", "%0", "node", "t", "/home/x/proj")
AUTO = Pane("other", "1", "bash", "2", "%7", "bash", "t", "/home/x/thing")


def _panes(monkeypatch, panes):
    monkeypatch.setattr(tmux, "list_panes", lambda: panes)


def test_canonical_address_matches_named_window(monkeypatch):
    """The regression: a named window makes label != session:index, so "work:0" only
    resolves if the numeric address is matched separately."""
    _panes(monkeypatch, [NAMED])
    assert NAMED.label == "Resolve PR 38"  # label is NOT session:window
    assert find_pane("work:0") is NAMED
    assert find_pane("work:0.0") is NAMED


def test_pane_id_and_label_still_match(monkeypatch):
    _panes(monkeypatch, [NAMED])
    assert find_pane("%0") is NAMED
    assert find_pane("Resolve PR 38") is NAMED
    assert find_pane("Resolve PR 38.0") is NAMED


def test_auto_named_window_falls_back_to_session(monkeypatch):
    """tmux auto-names windows after the command; the label falls back to session."""
    _panes(monkeypatch, [AUTO])
    assert find_pane("other:1") is AUTO
    assert find_pane("other:1.2") is AUTO


def test_no_match_returns_none(monkeypatch):
    _panes(monkeypatch, [NAMED, AUTO])
    assert find_pane("nope:9") is None
    assert find_pane("%99") is None


def test_none_target_picks_first(monkeypatch):
    _panes(monkeypatch, [NAMED, AUTO])
    assert find_pane(None) is NAMED


def test_empty_server(monkeypatch):
    _panes(monkeypatch, [])
    assert find_pane(None) is None
    assert find_pane("work:0") is None


def test_missing_target_warns_once(monkeypatch, caplog):
    """A target that matches nothing used to serve an empty deck silently. Warn — but
    only on the first tick, not once per poll for the life of the daemon."""
    import openbus.watcher as watcher

    monkeypatch.setattr(watcher.tmux, "server_running", lambda: True)
    monkeypatch.setattr(watcher.tmux, "find_pane", lambda t: None)
    monkeypatch.setattr(watcher.tmux, "list_panes", lambda: [])

    w = watcher.Watcher(target="nope:9", use_llm=False)
    with caplog.at_level("WARNING", logger="openbus.watcher"):
        w._tick()
        w._tick()

    hits = [r for r in caplog.records if "matches no pane" in r.getMessage()]
    assert len(hits) == 1, f"expected exactly one warning, got {len(hits)}"
    assert "nope:9" in hits[0].getMessage()
