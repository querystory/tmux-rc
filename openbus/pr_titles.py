"""Bounded, asynchronous GitHub title lookup; never block pane classification."""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor


def fetch_title(repo: str, number: int) -> str | None:
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", repo, "--json", "title"],
            capture_output=True, text=True, check=True, timeout=8,
        )
        title = json.loads(result.stdout).get("title")
        return title.strip()[:300] if isinstance(title, str) and title.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


class PRTitles:
    """Owned by the watcher thread; workers only return results, never mutate state."""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pr-title")
        self._pending = {}
        self._cache = {}
        self._closed = False

    def close(self):
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

    def enrich(self, prs: list[dict]) -> list[dict]:
        now = time.monotonic()
        for key, future in list(self._pending.items()):
            if future.done():
                title = None if future.cancelled() else future.result()
                old_title = self._cache.get(key, (0, None))[1]
                self._cache.pop(key, None)
                self._cache[key] = (now + (900 if title else 60), title or old_title)
                del self._pending[key]
        while len(self._cache) > 512:
            del self._cache[next(iter(self._cache))]
        out = []
        for pr in prs:
            key = (pr["repo"].lower(), pr["number"])
            expires, title = self._cache.get(key, (0, None))
            if (not self._closed and expires <= now and key not in self._pending
                    and len(self._pending) < 2):
                self._pending[key] = self._pool.submit(fetch_title, *key)
            out.append({**pr, **({"title": title} if title else {})})
        return out
