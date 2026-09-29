import subprocess

import pytest

from openbus import repository


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:querystory/tmux-rc.git", "querystory/tmux-rc"),
        ("ssh://git@github.com/querystory/qs-app", "querystory/qs-app"),
        ("https://github.com/querystory/tmux-rc.git", "querystory/tmux-rc"),
        ("git://github.com/querystory/tmux-rc.git", "querystory/tmux-rc"),
        ("ssh://git@github.com:22/querystory/tmux-rc.git", "querystory/tmux-rc"),
        ("ssh://git@ssh.github.com:443/querystory/tmux-rc.git", "querystory/tmux-rc"),
        ("https://github.com:443/querystory/tmux-rc.git", "querystory/tmux-rc"),
        ("ssh://git@github.com.evil.test:22/querystory/tmux-rc.git", None),
        ("https://github.com/../bad.git", None),
        ("https://github.com/org/" + "a" * 257, None),
        ("git@gitlab.com:querystory/tmux-rc.git", None),
    ],
)
def test_github_repository_parses_local_origin(monkeypatch, remote, expected):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=remote + "\n")

    monkeypatch.setattr(repository.subprocess, "run", run)
    assert repository.github_repository("/repo/worktree") == expected
    assert repository.github_repository("/repo/worktree") == expected
    assert len(calls) == 2  # cache lifetime belongs to the watcher, not this reader
    assert calls[0][0] == ["git", "-C", "/repo/worktree", "remote", "get-url", "origin"]
    assert calls[0][1]["timeout"] == 2


def test_github_repository_degrades_on_git_failure(monkeypatch):
    monkeypatch.setattr(
        repository.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 2)),
    )
    assert repository.github_repository("/repo") is None
    assert repository.github_repository("") is None
