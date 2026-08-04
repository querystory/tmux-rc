"""Shared state model. Kept deliberately small and list-shaped so the single-pane
PoC (Milestone 1) generalizes to all panes (Milestone 2) with no schema change."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Activity = Literal["running", "idle", "waiting", "compacting", "unknown"]
# WHOM a `waiting` pane is blocked on: "user" (an input affordance the human must fill —
# actionable) vs "external" (a background subagent / Copilot / CI / poll it spawned —
# just busy). Absent ⇒ treat as "user" (the safe default; never hide a real user-wait).
WaitingOn = Literal["user", "external"]
Tool = Literal["claude", "codex", "gemini", "shell", "unknown"]
# Permission/interaction mode, mirroring Claude Code's shift-tab cycle.
Mode = Literal["normal", "plan", "accept-edits", "bypass", "unknown"]


class Question(BaseModel):
    """A detected prompt awaiting user input."""

    prompt: str
    options: list[str] = []  # empty ⇒ free-text answer expected


class Task(BaseModel):
    """One item in the agent's OWN visible plan/TODO checklist (a ☐/☑ list on screen).
    Distinct from a spawned sub-agent — see SubAgent."""

    text: str
    done: bool = False


class SubAgent(BaseModel):
    """A background agent THIS agent spawned to run in parallel (e.g. Claude Code's
    `general-purpose  <task>  Nm  ↓Nk tokens` rows, or a "waiting for N background
    agents" line). Not a checklist item — it's a running/finished worker, so it carries
    a live `state` and whatever cheap on-screen signal (elapsed/tokens) is shown."""

    label: str  # what the sub-agent is doing, e.g. "In-depth review PR 4012"
    # The CONTRACT is running|done (default running). Consumers stay lenient on
    # violations: classify's count and the UI both treat anything that isn't exactly
    # "done" as running — a stray value degrades to "still working", never to dropped.
    state: Literal["running", "done"] = "running"
    elapsed: str | None = None  # e.g. "2m", if the row shows it
    tokens: str | None = None  # e.g. "88.7k", if the row shows it


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


class Copyable(BaseModel):
    """Text on screen whose purpose is to be pasted somewhere else — a command to run
    in another terminal, a drafted commit message, a generated token, a code block.
    Copying out of a raw terminal capture on a phone is the thing this exists to avoid
    (wrapped lines, box-drawing borders and gutter glyphs land inside the selection), so
    the parser reconstructs clean paste-ready text and the card offers one-tap copy."""

    label: str  # one-line summary of WHAT it is: "Commit message", "gcloud auth command"
    text: str  # the verbatim paste-ready content (unwrapped, chrome stripped)


class PaneState(BaseModel):
    """Everything the phone needs to render one pane. The rich fields (model, cost,
    mode, working_*) are populated by the LLM pass from an agent's status line — see
    classify.py — since regex-picking one line grabs the wrong thing (e.g. the vim/
    tmux "-- INSERT --" footer)."""

    pane_id: str
    label: str  # best human name (window > session > cwd; LLM may refine)
    # Structural tmux identity, so the client can group windows under their session
    # and follow focus per session (label collapses all of this into one string).
    session: str = ""  # tmux session name, e.g. "gtm"
    window_index: str = ""  # window number as shown in tmux's status bar
    window_name: str = ""
    # Focused pane WITHIN its session (current window's active pane). One per session —
    # meaningful with several sessions attached, unlike the global tmux_active.
    session_active: bool = False
    tool: Tool = "unknown"
    activity: Activity = "unknown"
    waiting_on: WaitingOn | None = None  # only meaningful when activity == "waiting"
    idle_seconds: int = 0
    status_line: str = ""  # one short human phrase: "Editing models.py", "14/52 tests"
    question: Question | None = None
    rewind: Rewind | None = None  # Claude Code's Esc-Esc restore-history picker

    # Agent status-line detail (mostly Claude Code; nullable when not applicable).
    model: str | None = None  # e.g. "Sonnet 5", "Opus 4.8"
    context_pct: int | None = (
        None  # context-window % USED (parser converts 'left' displays)
    )
    cost: str | None = None  # e.g. "$10.64"
    mode: Mode = "unknown"  # plan / accept-edits / bypass / normal

    # Present while actively working: the whimsical verb, elapsed, and tokens streamed.
    working_verb: str | None = None  # e.g. "Cultivating"
    elapsed: str | None = None  # e.g. "11m46s"
    tokens: str | None = None  # e.g. "13.3k"
    # The agent's own plan checklist (tasks) vs. background workers it spawned
    # (subagents) — two genuinely different things, kept apart. `agents` (running
    # sub-agent count) stays derivable for the dock badge, computed in classify.py.
    tasks: list[Task] = []
    subagents: list[SubAgent] = []
    agents: int = 0  # count of RUNNING subagents (derived from subagents[])
    copyables: list[Copyable] = []  # paste-me-elsewhere text, one-tap copy in the card

    snapshot_id: str | None = None  # latest snapshot for the timeline
    updated_at: float = 0.0
