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
# LLM parse cadence. We capture every tick (cheap, for the snapshot buffer) but only
# PARSE when the content fingerprint changed, or when this long since the last parse
# (a heartbeat so a slowly-evolving screen still refreshes). An agent that's merely
# working — spinner + timer + token counter ticking, no content change — costs ZERO
# LLM calls, because that churn is stripped from the fingerprint.
HEARTBEAT_SECONDS = 10

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
        self._prev_fp: dict[str, str] = {}  # pane_id -> fingerprint at last parse
        self._unchanged_since: dict[str, float] = {}
        self._last_parse: dict[str, float] = {}  # pane_id -> when we last called the LLM
        self._tool: dict[str, str] = {}  # pane_id -> sticky tool once identified as an agent
        self._state: dict[str, PaneState] = {}  # pane_id -> last parsed state (reused between parses)
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
        changed = fp != self._prev_fp.get(pane.id)  # real content change (timers stripped)
        if changed:
            self._unchanged_since[pane.id] = now
        idle = int(now - self._unchanged_since.get(pane.id, now))

        # Record a snapshot whenever content changed (bounded ring buffer, for timeline).
        if changed:
            hist = self.snapshots.setdefault(pane.id, [])
            hist.append({"id": f"{int(now * 1000)}", "text": text, "ts": now})
            del hist[:-SNAPSHOT_HISTORY]

        # Decide whether to PARSE (call the LLM). Parse on real content change, or on a
        # heartbeat so a slowly-drifting screen refreshes. Merely-working churn (spinner/
        # timer/tokens) is stripped from the fingerprint ⇒ no change ⇒ no call.
        cached = self._state.get(pane.id)
        due = now - self._last_parse.get(pane.id, 0) >= HEARTBEAT_SECONDS
        if cached is not None and not changed and not due:
            cached.idle_seconds = idle  # just tick the timer, reuse everything else
            cached.updated_at = now
            self.states = [cached]
            return

        state = classify(pane, text, llm_fn=classify_text if self.use_llm else None)
        self._prev_fp[pane.id] = fp
        self._last_parse[pane.id] = now

        # Sticky tool: once identified as an agent, keep that identity even when the
        # agent shells out (foreground briefly becomes bash/git). Stops claude⇄shell flap.
        if state.tool in ("claude", "codex", "gemini"):
            self._tool[pane.id] = state.tool
        elif state.tool in ("shell", "unknown") and pane.id in self._tool:
            state.tool = self._tool[pane.id]

        hist = self.snapshots.get(pane.id, [])
        state.snapshot_id = hist[-1]["id"] if hist else None
        state.idle_seconds = idle
        state.updated_at = now
        self._state[pane.id] = state
        self.states = [state]
