"""Background poll loop: capture the target pane, classify it, keep the latest
PaneState and a rolling snapshot history for the timeline.

Milestone 1 watches a single target pane. The state is held in a list so the
server and the eventual multi-pane fan-out (Milestone 2) need no change.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from . import tmux
from .classify import classify
from .llm import classify_text
from .models import PaneState

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.5
SNAPSHOT_HISTORY = 50  # per pane
IDLE_LLM_AFTER = 6  # seconds of no change before a proactive LLM pass on a stable screen

# Volatile bits that repaint constantly on an agent status line even when nothing
# meaningful is happening. Confirmed by diffing live captures: elapsed timers, token
# counts, spinner glyphs, AND the status-line metrics that drift every tick — cost
# ($38.15→$38.31), context %, and the history counters (prompts/tools/MB). Strip them
# so "is this the same screen?" is judged on real content; otherwise the LLM re-fires
# every tick and the card flickers. (These values still reach the UI via the LLM's
# structured fields — we only ignore them for the change check.)
_VOLATILE_RE = re.compile(
    r"\d+h\d+m|\d+m\s*\d+s|\d+s\b"  # durations
    r"|↓\s*[\d.]+k?|[\d.]+k tokens"  # token counts
    r"|\$[\d.]+"  # cost
    r"|\d+%\s*ctx"  # context percent
    r"|\d+\s*(prompts|tools|imgs)|[\d.]+\s*[KMG]B"  # history counters
    r"|[⏳✳✻✶✷✽❋⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒]"  # spinner glyphs
    r"|[ \t]+$",  # trailing whitespace
    re.MULTILINE,
)


def _fingerprint(text: str) -> str:
    """Content signature of a pane, ignoring volatile timer/spinner churn."""
    return _VOLATILE_RE.sub("", text)


class Watcher:
    """Holds current pane state + snapshot history, refreshed by an async loop."""

    def __init__(self, target: str | None, use_llm: bool = True):
        self.target = target
        self.use_llm = use_llm
        self.states: list[PaneState] = []
        self.snapshots: dict[str, list[dict]] = {}  # pane_id -> [{id, text, ts}]
        self._prev_text: dict[str, str] = {}  # pane_id -> last fingerprint (for change detect)
        self._prev_raw: dict[str, str] = {}  # pane_id -> last raw capture (for classify's diff)
        self._unchanged_since: dict[str, float] = {}
        self._llm_seen: dict[str, str] = {}  # pane_id -> fingerprint already given a forced pass
        self._tool: dict[str, str] = {}  # pane_id -> sticky tool once identified as an agent
        self._state: dict[str, PaneState] = {}  # pane_id -> last classification (reused when stable)
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
        fp = _fingerprint(text)
        prev_fp = self._prev_text.get(pane.id)
        changed = fp != prev_fp  # ignore timer/spinner churn
        prev = self._prev_raw.get(pane.id)  # raw text for classify's own diff
        if changed:
            self._unchanged_since[pane.id] = now
        idle = int(now - self._unchanged_since.get(pane.id, now))

        # Stable screen ⇒ reuse the last classification verbatim. This is the key
        # anti-flicker rule: when the real content hasn't changed, we do NOT re-run the
        # LLM (which returns slightly different JSON each time) and do NOT let the card
        # flip between question/no-question or waiting/idle. Only refresh the timer.
        cached = self._state.get(pane.id)
        if not changed and cached is not None:
            cached.idle_seconds = idle
            cached.updated_at = now
            self.states = [cached]
            return

        # Content changed (or first sight): classify. Fire the LLM once per new screen.
        force_llm = self.use_llm and idle >= IDLE_LLM_AFTER and self._llm_seen.get(pane.id) != fp
        if force_llm:
            self._llm_seen[pane.id] = fp

        state = classify(
            pane, text, prev, idle,
            llm_fn=classify_text if self.use_llm else None,
            force_llm=force_llm,
        )

        # Sticky tool: once a pane is identified as an agent, keep that identity even
        # when it shells out (foreground becomes bash/git and detection would say
        # "shell"). This stops the claude→shell→claude oscillation.
        if state.tool in ("claude", "codex", "gemini"):
            self._tool[pane.id] = state.tool
        elif state.tool in ("shell", "unknown") and pane.id in self._tool:
            state.tool = self._tool[pane.id]

        self._state[pane.id] = state

        # Record a snapshot whenever the pane changed (bounded ring buffer).
        if changed:
            hist = self.snapshots.setdefault(pane.id, [])
            snap_id = f"{int(now * 1000)}"
            hist.append({"id": snap_id, "text": text, "ts": now})
            del hist[:-SNAPSHOT_HISTORY]
        hist = self.snapshots.get(pane.id, [])
        state.snapshot_id = hist[-1]["id"] if hist else None
        state.updated_at = now

        self._prev_text[pane.id] = fp
        self._prev_raw[pane.id] = text
        self.states = [state]
