"""PR associations are semantic classifier evidence accumulated for a pane lifetime."""

import openbus.watcher as W
from openbus.watcher import Watcher


class _Pane:
    id = "%1"
    pid = "100"
    cwd = "/repo/worktree"
    label = "work:0"
    display_title = "Address review"
    session = "work"
    window_index = "0"
    window_name = "codex"
    session_active = True
    current_command = "codex"
    window_activity = ""


def test_working_prs_accumulate_but_a_visible_pr_list_adds_nothing(monkeypatch):
    frames = iter(["working on 4955", "all open PRs: 1 2 3", "now fixing 4960"])
    results = iter([
        {"tool": "codex", "activity": "running", "events": [],
         "working_prs": [{"repo": "querystory/qs-app", "number": 4955}]},
        {"tool": "codex", "activity": "idle", "events": []},
        {"tool": "codex", "activity": "running", "events": [],
         "working_prs": [{"repo": "querystory/qs-app", "number": 4960}]},
    ])
    monkeypatch.setattr(W.tmux, "capture_pane", lambda *args, **kwargs: next(frames))
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "server:%1")
    monkeypatch.setattr(W, "github_repository", lambda cwd: "querystory/qs-app")

    def classify(*args, **kwargs):
        assert kwargs["repository"] == "querystory/qs-app"
        return dict(next(results))

    monkeypatch.setattr(W, "classify", classify)
    w = Watcher(None, use_llm=False)
    pane = _Pane()
    w._forced_this_tick = set()

    first = w._tick_pane(pane)
    assert first["prs"] == [{"repo": "querystory/qs-app", "number": 4955}]
    assert "working_prs" not in first

    listed = w._tick_pane(pane)
    assert listed["prs"] == first["prs"]

    second = w._tick_pane(pane)
    assert second["prs"] == [
        {"repo": "querystory/qs-app", "number": 4955},
        {"repo": "querystory/qs-app", "number": 4960},
    ]


def test_pr_associations_leave_with_the_pane(monkeypatch):
    w = Watcher(None)
    monkeypatch.setattr(w, "_pane_event", lambda *args, **kwargs: None)
    w._prs["%1"] = [{"repo": "querystory/qs-app", "number": 4955}]
    w._repositories["%1"] = ("/repo/worktree", "querystory/qs-app")
    w._forget("%1")
    assert "%1" not in w._prs and "%1" not in w._repositories


def test_digest_exposes_accumulated_prs():
    w = Watcher(None)
    w.states = [{"pane_id": "%1", "label": "work"}]
    w._prs["%1"] = [{"repo": "querystory/qs-app", "number": 4955}]
    assert w.digest()[0]["prs"] == [
        {"repo": "querystory/qs-app", "number": 4955}
    ]


def test_repository_context_follows_a_pane_that_changes_directory(monkeypatch):
    seen = []
    monkeypatch.setattr(
        W,
        "github_repository",
        lambda cwd: seen.append(cwd) or f"querystory/{cwd.rsplit('/', 1)[-1]}",
    )
    w = Watcher(None)
    pane = _Pane()

    assert w._repository_for(pane) == "querystory/worktree"
    assert w._repository_for(pane) == "querystory/worktree"
    pane.cwd = "/repo/other"
    assert w._repository_for(pane) == "querystory/other"
    assert seen == ["/repo/worktree", "/repo/other"]


def test_scrollback_bootstrap_rehydrates_pr_associations(monkeypatch):
    monkeypatch.setattr(W, "backing_off", lambda: False)
    monkeypatch.setattr(W.tmux, "capture_pane", lambda *args, **kwargs: "scrollback")
    monkeypatch.setattr(W.tmux, "pane_uid", lambda pane: "server:%1")

    def bootstrap(pane, text, llm_fn, repository=None):
        assert repository == "querystory/qs-app"
        return {
            "summary": "Addressed review feedback",
            "name": "Review 4955",
            "events": [],
            "working_prs": [{"repo": "querystory/qs-app", "number": 4955}],
        }

    monkeypatch.setattr(W, "bootstrap", bootstrap)
    monkeypatch.setattr(W, "github_repository", lambda cwd: "querystory/qs-app")
    w = Watcher(None)
    w._maybe_bootstrap([_Pane()])

    assert w._prs["%1"] == [{"repo": "querystory/qs-app", "number": 4955}]
    assert "working_prs" not in w._boot["%1"]
