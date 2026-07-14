"""Shared state model. Kept deliberately small and list-shaped so the single-pane
PoC (Milestone 1) generalizes to all panes (Milestone 2) with no schema change."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Activity = Literal["running", "idle", "waiting", "unknown"]
Tool = Literal["claude", "codex", "gemini", "shell", "unknown"]


class Question(BaseModel):
    """A detected prompt awaiting user input."""

    prompt: str
    options: list[str] = []  # empty ⇒ free-text answer expected


class PaneState(BaseModel):
    """Everything the phone needs to render one pane."""

    pane_id: str
    label: str  # "session:window"
    tool: Tool = "unknown"
    activity: Activity = "unknown"
    idle_seconds: int = 0
    status_line: str = ""  # one short human phrase: "Editing models.py", "14/52 tests"
    context_pct: int | None = None  # Claude Code context-window %, when detectable
    question: Question | None = None
    snapshot_id: str | None = None  # latest snapshot for the timeline
    updated_at: float = 0.0
