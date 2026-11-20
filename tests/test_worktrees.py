"""The worktree scan behind the Fleet graph's Repo and Worktree lenses."""
import subprocess
from threading import Event

from openbus import worktrees
from openbus.worktrees import Worktrees, scan


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_scan_finds_every_worktree_of_a_panes_repository(tmp_path):
    repo, bare = tmp_path / "repo", tmp_path / "tool.git"
    _git("init", "-b", "main", str(repo), cwd=tmp_path)
    _git("remote", "add", "origin", "git@github.com:example-org/shop-api.git", cwd=repo)
    _git("commit", "--allow-empty", "-m", "base", cwd=repo)
    _git("worktree", "add", "-b", "spike", str(tmp_path / "spike"), cwd=repo)
    _git("worktree", "add", "--detach", str(tmp_path / "scratch"), cwd=repo)
    _git("clone", "--bare", str(repo), str(bare), cwd=tmp_path)
    _git("remote", "remove", "origin", cwd=bare)
    _git("worktree", "add", str(tmp_path / "local"), "main", cwd=bare)

    found = {w["path"].rsplit("/", 1)[1]: w for w in scan(
        [str(repo), str(repo), str(tmp_path / "local"), str(tmp_path)], Event())}
    assert set(found) == {"repo", "spike", "scratch", "local"}
    assert found["spike"] == {"key": str(repo / ".git"), "repo": "shop-api",
                              "path": str(tmp_path / "spike"), "branch": "spike"}
    assert found["scratch"]["branch"] == ""  # detached
    assert found["local"]["repo"] == "tool"  # no GitHub origin: the common dir's own name
    stop = Event()
    stop.set()
    assert scan([str(repo)], stop) == []  # shutdown: the scan stops at once


def test_a_failed_git_listing_keeps_the_previous_rows(monkeypatch):
    monkeypatch.setattr(worktrees, "_git", lambda *a: "/c/.git\n" if "rev-parse" in a else None)
    before = [{"key": "/c/.git", "repo": "c", "path": "/wt/drift", "branch": "spike"},
              {"key": "/d/.git", "repo": "d", "path": "/elsewhere", "branch": "x"}]
    assert scan(["/wt/drift"], Event(), before) == before[:1]
    monkeypatch.setattr(worktrees, "_git", lambda *a: None)  # discovery fails too
    assert scan(["/wt/drift/src"], Event(), before) == before[:1]


def test_where_names_the_innermost_worktree():
    held = Worktrees()
    held.rows = [{"key": "/w/app/.git", "repo": "app", "path": "/w/app", "branch": "main"},
                 {"key": "/w/app/.git", "repo": "app", "path": "/w/app/.worktrees/fix",
                  "branch": "fix"}]
    assert held.where("/w/app/src") == held.rows[0]
    assert held.where("/w/app/.worktrees/fix/a") == held.rows[1]
    assert held.where("/w/apple") is None
    assert held.where(None) is None
