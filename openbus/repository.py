"""Resolve a pane's local checkout to its GitHub repository, without network access."""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache

_GITHUB_REMOTE = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https?://github\.com/)"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


@lru_cache(maxsize=256)
def github_repository(cwd: str) -> str | None:
    """Return ``owner/name`` for cwd's origin, or None when it is not a GitHub repo.

    ``git remote get-url`` reads local configuration only. Caching by cwd keeps it out
    of the classification hot path after a pane's first parse.
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
    return match.group("repo") if match else None
