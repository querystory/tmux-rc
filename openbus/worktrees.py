"""Which repository and worktree each pane sits in, for the Fleet graph's Repo and Worktree
lenses: local git only, scanned on a worker thread and read from the cache."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from threading import Event

from .repository import github_repository

logger = logging.getLogger(__name__)
REFRESH = 900  # a scan is a git call per pane and per repository


def _git(*args: str) -> str | None:
    """No optional locks: a scan must never take an index.lock an agent then hits."""
    try:
        return subprocess.run(["git", "--no-optional-locks", *args], capture_output=True,
                              text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def holding(worktrees: list[dict], cwd: str | None) -> dict | None:
    """The innermost worktree holding cwd."""
    hits = [w for w in worktrees if cwd and (cwd + "/").startswith(w["path"] + "/")]
    return max(hits, key=lambda w: len(w["path"]), default=None)


def scan(cwds: list[str], stopping: Event, previous: list[dict] = ()) -> list[dict]:
    """Every worktree of every repository a pane sits in: the panes say which repositories
    matter, and each repository's own list finds worktrees wherever they live. A row names
    its repository by GitHub's name, else its folder, and keys it by its git common dir,
    which no two repositories share. Where git fails, the previous scan's rows stand in."""
    keys, out = set(), []
    for cwd in filter(None, set(cwds)):
        if stopping.is_set():  # each git call can take seconds; shutdown makes no more
            return out
        found = _git("-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        # Discovery failed: the repository a previous scan placed this cwd in still counts.
        keys |= {found.strip()} if found else {w["key"] for w in [holding(previous, cwd)] if w}
    for key in sorted(keys - {""}):
        if stopping.is_set():
            return out
        listing = _git("--git-dir", key, "worktree", "list", "--porcelain")
        if listing is None:
            out += [w for w in previous if w["key"] == key]
            continue
        common, repo = Path(key), None
        for block in listing.strip().split("\n\n"):
            fields = dict([*line.split(" ", 1), ""][:2] for line in block.splitlines())
            path = fields.get("worktree")
            if not path or "bare" in fields or "prunable" in fields or not os.path.isdir(path):
                continue
            repo = repo or (github_repository(path) or "").rpartition("/")[2] or (
                common.parent if common.name == ".git" else common).name.removesuffix(".git")
            out.append({"key": key, "repo": repo, "path": path,
                        "branch": fields.get("branch", "").removeprefix("refs/heads/")})
    return out


class Worktrees:
    """Owns the cache. run() rescans on a worker thread; where() only reads."""

    def __init__(self):
        self.rows, self.stopping = [], Event()  # set on shutdown, so a scan stops early

    async def run(self, watcher) -> None:
        while not watcher.booted():  # before the first tick there are no panes to place
            await watcher.wait_for_state_change(watcher.state_version(), 30)
        try:
            while True:
                try:
                    rows = await asyncio.to_thread(
                        scan, [p.get("cwd") for p in watcher.states], self.stopping, self.rows)
                    if not self.stopping.is_set():  # a scan cut short by shutdown is partial
                        self.rows = rows
                except Exception:  # one bad scan must not end the loop
                    logger.warning("worktree scan failed", exc_info=True)
                await asyncio.sleep(REFRESH)
        finally:  # cancelling the task cannot stop its thread; this lets the thread stop
            self.stopping.set()

    def where(self, cwd: str | None) -> dict | None:
        return holding(self.rows, cwd)
