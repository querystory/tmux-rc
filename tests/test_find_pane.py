"""find_pane() target resolution.

The display label prefers a user-named window over the session, so the numeric tmux
address ("work:0.0" — window/pane INDEX, not the window name) has to be matched on its
own — see issue #146.
"""

from daemon import tmux
from daemon.tmux import Pane, find_pane

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


def test_auto_named_window_falls_back_to_session_and_index(monkeypatch):
    """tmux auto-names windows after the command; the label falls back to the session
    QUALIFIED BY the window index, so sibling windows don't share one label."""
    _panes(monkeypatch, [AUTO])
    assert AUTO.label == "other:1"
    assert find_pane("other:1") is AUTO
    assert find_pane("other:1.2") is AUTO


def test_auto_named_siblings_get_distinct_labels():
    """The regression: every unnamed window in a session used to render as the bare
    session name, so a fleet of agents was a column of identical headings."""
    siblings = [Pane("work", str(i), "node", "0", f"%{i}", "node", "t", "/home/x/proj")
                for i in range(3)]
    assert [p.label for p in siblings] == ["work:0", "work:1", "work:2"]


def test_agent_cli_window_names_are_generic():
    """tmux names a window after the command that launched it, so a fleet of agents
    self-names into a wall of "claude" rows. Those fall through to the qualified label
    like any other command name; a user-chosen name still wins."""
    agent = Pane("work", "6", "claude", "0", "%6", "node", "t", "/home/x/proj")
    named = Pane("work", "7", "review the PR", "0", "%7", "node", "t", "/home/x/proj")
    assert agent.label == "work:6"
    assert named.label == "review the PR"


def test_label_falls_back_to_cwd_with_index_when_session_is_generic():
    """No meaningful window OR session name: the cwd basename identifies the project,
    the index identifies the window."""
    p = Pane("0", "2", "bash", "0", "%9", "bash", "t", "/home/x/thing/")
    assert p.label == "thing:2"


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
    from daemon import watcher

    monkeypatch.setattr(watcher.tmux, "server_running", lambda: True)
    monkeypatch.setattr(watcher.tmux, "find_pane", lambda t: None)
    monkeypatch.setattr(watcher.tmux, "list_panes", list)

    w = watcher.Watcher(target="nope:9", use_llm=False)
    with caplog.at_level("WARNING", logger="daemon.watcher"):
        w._tick()
        w._tick()

    hits = [r for r in caplog.records if "matches no pane" in r.getMessage()]
    assert len(hits) == 1, f"expected exactly one warning, got {len(hits)}"
    assert "nope:9" in hits[0].getMessage()
