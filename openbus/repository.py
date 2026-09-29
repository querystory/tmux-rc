"""Resolve a pane's local checkout to its GitHub repository, without network access."""

from __future__ import annotations

import re
import subprocess

_GITHUB_REMOTE = re.compile(
    r"^(?:git@github\.com:|ssh://git@(?:ssh\.)?github\.com(?::[0-9]+)?/"
    r"|(?:https?|git)://github\.com(?::[0-9]+)?/)"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


def github_repository(cwd: str) -> str | None:
    """Return ``owner/name`` for cwd's origin, or None when it is not a GitHub repo.

    ``git remote get-url`` reads local configuration only. The watcher owns the
    bounded-lifetime cache, so a failed lookup or changed origin can be retried.
    """
    if not cwd:
        return None
    try:
        remote = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = _GITHUB_REMOTE.fullmatch(remote)
    if not match:
        return None
    repo = match.group("repo")
    if len(repo) > 256 or any(part in {".", ".."} for part in repo.split("/")):
        return None
    return repo
