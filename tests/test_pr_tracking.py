"""PR associations are semantic classifier evidence accumulated for a pane lifetime."""

import pytest

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
    w = Watcher(None, use_llm=True)
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


@pytest.mark.parametrize("server_exists", [True, False])
def test_pr_associations_clear_when_the_last_pane_disappears(monkeypatch, server_exists):
    w = Watcher(None, use_llm=False)
    monkeypatch.setattr(W.tmux, "server_running", lambda: server_exists)
    monkeypatch.setattr(W.tmux, "list_panes", list)
    monkeypatch.setattr(w, "_pane_event", lambda *args, **kwargs: None)
    w._prs["%1"] = [{"repo": "querystory/qs-app", "number": 4955}]
    w._repositories["%1"] = ("/repo", "querystory/qs-app")
    w._birth["%1"] = "100"
    w._tick()
    assert not w._prs and not w._repositories and not w._birth
    assert w.states == []


def test_unmatched_target_is_not_proof_of_pane_death(monkeypatch):
    w = Watcher("renamed-label", use_llm=False)
    monkeypatch.setattr(W.tmux, "server_running", lambda: True)
    monkeypatch.setattr(W.tmux, "find_pane", lambda target: None)
    w._prs["%1"] = [{"repo": "querystory/qs-app", "number": 4955}]
    w._tick()
    assert w._prs["%1"] == [{"repo": "querystory/qs-app", "number": 4955}]


def test_digest_exposes_accumulated_prs():
    w = Watcher(None)
    w.states = [{"pane_id": "%1", "label": "work", "cwd": "/repo/worktree"}]
    w._prs["%1"] = [{"repo": "querystory/qs-app", "number": 4955}]
    assert w.digest()[0]["prs"] == [
        {"repo": "querystory/qs-app", "number": 4955}
    ]
    assert w.digest()[0]["cwd"] == "/repo/worktree"


def test_associations_keep_recent_evidence_with_a_fixed_budget():
    w = Watcher(None)
    for number in range(1, W.MAX_PRS_PER_PANE + 1):
        w._accumulate_prs("%1", [{"repo": "querystory/qs-app", "number": number}])
    w._accumulate_prs("%1", [{"repo": "QUERYSTORY/qs-app", "number": 1}])
    w._accumulate_prs("%1", [{"repo": "querystory/qs-app", "number": 1000}])
    assert len(w._prs["%1"]) == W.MAX_PRS_PER_PANE
    assert w._prs["%1"][-2:] == [
        {"repo": "querystory/qs-app", "number": 1},
        {"repo": "querystory/qs-app", "number": 1000},
    ]
    assert not any(p["number"] == 2 for p in w._prs["%1"])


def test_no_llm_mode_skips_repository_resolution(monkeypatch):
    w = Watcher(None, use_llm=False)
    monkeypatch.setattr(W.tmux, "capture_pane", lambda *args, **kwargs: "user@host:~$ ")
    monkeypatch.setattr(W, "github_repository", lambda cwd: pytest.fail("unused repository lookup"))
    state = w._tick_pane(_Pane())
    assert state["cwd"] == "/repo/worktree"
    assert not state.get("prs")
    assert not w._repositories


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


def test_repository_cache_retries_failures_and_refreshes_changed_origins(monkeypatch):
    now = [0.0]
    results = iter([None, "org/recovered", "org/renamed"])
    monkeypatch.setattr(W.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(W, "github_repository", lambda cwd: next(results))
    w = Watcher(None)
    pane = _Pane()
    assert w._repository_for(pane) is None
    assert w._repository_for(pane) is None
    now[0] += W.REPOSITORY_REFRESH_SECONDS
    assert w._repository_for(pane) == "org/recovered"
    assert w._repository_for(pane) == "org/recovered"
    now[0] += W.REPOSITORY_REFRESH_SECONDS
    assert w._repository_for(pane) == "org/renamed"


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
