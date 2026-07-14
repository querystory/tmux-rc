"""Background poll loop: capture the target pane, classify it, keep the latest
PaneState and a rolling snapshot history for the timeline.

Milestone 1 watches a single target pane. The state is held in a list so the
server and the eventual multi-pane fan-out (Milestone 2) need no change.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import tmux
from .classify import classify
from .llm import classify_text
from .models import PaneState

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.5
SNAPSHOT_HISTORY = 50  # per pane


class Watcher:
    """Holds current pane state + snapshot history, refreshed by an async loop."""

    def __init__(self, target: str | None, use_llm: bool = True):
        self.target = target
        self.use_llm = use_llm
        self.states: list[PaneState] = []
        self.snapshots: dict[str, list[dict]] = {}  # pane_id -> [{id, text, ts}]
        self._prev_text: dict[str, str] = {}
        self._unchanged_since: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def snapshot_text(self, pane_id: str, snap_id: str) -> str | None:
        for s in self.snapshots.get(pane_id, []):
            if s["id"] == snap_id:
                return s["text"]
        return None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._tick)
            except Exception:  # noqa: BLE001 - never let one bad tick kill the loop
                logger.warning("watcher tick failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)

    def _tick(self) -> None:
        if not tmux.server_running():
            self.states = []
            return
        pane = tmux.find_pane(self.target)
        if pane is None:
            self.states = []
            return

        text = tmux.capture_pane(pane.id)
        now = time.time()
        prev = self._prev_text.get(pane.id)
        if text != prev:
            self._unchanged_since[pane.id] = now
        idle = int(now - self._unchanged_since.get(pane.id, now))

        state = classify(
            pane, text, prev, idle,
            llm_fn=classify_text if self.use_llm else None,
        )

        # Record a snapshot whenever the pane changed (bounded ring buffer).
        if text != prev:
            hist = self.snapshots.setdefault(pane.id, [])
            snap_id = f"{int(now * 1000)}"
            hist.append({"id": snap_id, "text": text, "ts": now})
            del hist[:-SNAPSHOT_HISTORY]
        hist = self.snapshots.get(pane.id, [])
        state.snapshot_id = hist[-1]["id"] if hist else None
        state.updated_at = now

        self._prev_text[pane.id] = text
        self.states = [state]
