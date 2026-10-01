"""Bounded, asynchronous GitHub title/state lookup; never block pane classification."""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event


def fetch_pr(repo: str, number: int, stopping: Event | None = None) -> dict | None:
    try:
        with subprocess.Popen(
            ["gh", "pr", "view", str(number), "--repo", repo, "--json", "title,state"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as process:
            deadline = time.monotonic() + 8
            while True:
                if (stopping is not None and stopping.is_set()) or time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    return None
                try:
                    stdout, _ = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                return None
        data = json.loads(stdout)
        title, state = data.get("title"), data.get("state")
        return {
            "title": title.strip()[:300] if isinstance(title, str) and title.strip() else None,
            "state": state if state in ("OPEN", "MERGED", "CLOSED") else None,
        }
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


class PRTitles:
    """Owned by the watcher thread; workers only return results, never mutate state."""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pr-title")
        self._pending = {}
        self._cache = {}
        self._closed = False
        self._stopping = Event()

    def close(self):
        self._closed = True
        self._stopping.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def enrich(self, prs: list[dict]) -> list[dict]:
        """Titled copies of the still-open PRs; merged/closed ones are dropped."""
        now = time.monotonic()
        for key, future in list(self._pending.items()):
            if future.done():
                info = None if future.cancelled() else future.result()
                old = self._cache.get(key, (0, None))[1]
                self._cache.pop(key, None)
                self._cache[key] = (now + (900 if info else 60), info or old)
                del self._pending[key]
        while len(self._cache) > 512:
            del self._cache[next(iter(self._cache))]
        out = []
        for pr in prs:
            key = (pr["repo"].lower(), pr["number"])
            expires, info = self._cache.get(key, (0, None))
            if (not self._closed and expires <= now and key not in self._pending
                    and len(self._pending) < 2):
                try:
                    self._pending[key] = self._pool.submit(fetch_pr, *key, self._stopping)
                except RuntimeError:
                    # stop() runs on the event loop while enrich() runs in a worker.
                    if not self._closed:
                        raise
            if (info or {}).get("state") in ("MERGED", "CLOSED"):
                continue  # finished upstream: no longer a link worth showing
            title = (info or {}).get("title")
            out.append({**pr, **({"title": title} if title else {})})
        return out
