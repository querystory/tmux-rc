"""Shared state model. Kept deliberately small and list-shaped so the single-pane
PoC (Milestone 1) generalizes to all panes (Milestone 2) with no schema change."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Activity = Literal["running", "idle", "waiting", "unknown"]
Tool = Literal["claude", "codex", "gemini", "shell", "unknown"]
# Permission/interaction mode, mirroring Claude Code's shift-tab cycle.
Mode = Literal["normal", "plan", "accept-edits", "bypass", "unknown"]


class Question(BaseModel):
    """A detected prompt awaiting user input."""

    prompt: str
    options: list[str] = []  # empty ⇒ free-text answer expected


class RewindEntry(BaseModel):
    """One entry in Claude Code's Esc-Esc Rewind picker (a past message you can
    restore to). `selected` marks the one under the ❯ cursor."""

    text: str  # the message summary line
    note: str = ""  # e.g. "No code changes" / a code-change note
    selected: bool = False


class Rewind(BaseModel):
    """The Rewind/restore-history picker. `more_above` is how many entries scrolled
    off the top (from tmux's "↑ N more above"). Navigated with ↑/↓ (moves the ❯
    cursor) and Enter (restores)."""

    entries: list[RewindEntry] = []
    more_above: int = 0
    more_below: int = 0


class PaneState(BaseModel):
    """Everything the phone needs to render one pane. The rich fields (model, cost,
    mode, working_*) are populated by the LLM pass from an agent's status line — see
    classify.py — since regex-picking one line grabs the wrong thing (e.g. the vim/
    tmux "-- INSERT --" footer)."""

    pane_id: str
    label: str  # "session:window"
    tool: Tool = "unknown"
    activity: Activity = "unknown"
    idle_seconds: int = 0
    status_line: str = ""  # one short human phrase: "Editing models.py", "14/52 tests"
    question: Question | None = None
    rewind: Rewind | None = None  # Claude Code's Esc-Esc restore-history picker

    # Agent status-line detail (mostly Claude Code; nullable when not applicable).
    model: str | None = None  # e.g. "Sonnet 5", "Opus 4.8"
    context_pct: int | None = None  # context-window % used/left
    cost: str | None = None  # e.g. "$10.64"
    mode: Mode = "unknown"  # plan / accept-edits / bypass / normal

    # Present while actively working: the whimsical verb, elapsed, and tokens streamed.
    working_verb: str | None = None  # e.g. "Cultivating"
    elapsed: str | None = None  # e.g. "11m46s"
    tokens: str | None = None  # e.g. "13.3k"
    agents: int = 0  # count of running sub-agents/tasks, when the agent shows them

    snapshot_id: str | None = None  # latest snapshot for the timeline
    updated_at: float = 0.0
