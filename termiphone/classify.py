"""LLM-first pane classification.

The LLM is the parser. We do NOT hard-code the exact strings of any agent's UI —
that was the brittle approach (broke on any UI/status-line change or a different
agent). Instead we send the visible pane text to Gemini Flash Lite with a layered
prompt: general terminal patterns first, then agent-specific sections describing
things *semantically*. It returns one structured JSON object; we map it to PaneState.

Regex survives only as a cheap hot-loop fast-path in the watcher (change detection,
obvious idle shell) so we don't call the LLM every tick — never as the parser.
"""

from __future__ import annotations

import re

from .models import PaneState, Question, Rewind, RewindEntry, Tool
from .tmux import Pane

# --- cheap fast-path only (NOT semantic parsing) --------------------------------
# A bare shell prompt at the tail — lets the watcher call an idle screen "idle"
# without spending an LLM call. Everything else is the LLM's job.
_SHELL_PROMPT_RE = re.compile(r"[\w.-]+@[\w.-]+.*[$#]\s*$")

_TOOLS = {"claude", "codex", "gemini", "shell", "unknown"}
_ACTIVITIES = {"running", "idle", "waiting"}
_MODES = {"plan", "accept-edits", "bypass", "normal"}


# The layered parser prompt. General terminal reasoning first; then per-agent sections
# described by BEHAVIOR/APPEARANCE, not exact wording, so it survives UI changes.
PARSER_PROMPT = """
You are the parser for a phone dashboard that watches terminal panes running coding
agents (Claude Code, Codex, Gemini CLI) or a plain shell. You are given the visible
text of one pane. Report what is happening as a single compact JSON object. Reason
about GENERAL PATTERNS by how they look/behave — do NOT rely on exact wording, which
changes between versions and tools.

GENERAL TERMINAL PATTERNS (apply to any tool):
- Selection menu: a list of choices where one line is marked by a cursor (❯, ›, >,
  a highlight, or an arrow) and you'd move between them with up/down. Capture the
  choices in order and which is selected.
- Input prompt: the pane is blocked waiting for the user to answer — a yes/no
  confirmation, a numbered choice, or a free-text field. Capture the question and any
  explicit options.
- Working/busy: a spinner, a "thinking"/gerund word, an elapsed timer, or streaming
  output means the agent is actively working (activity="running").
- Idle: a bare shell prompt with nothing happening → activity="idle".

CLAUDE CODE (recognize it by its style — a rounded input box, a status line, clay/
orange accents):
- Status line (usually near the bottom): may show the model, a context-window
  percentage, a session cost, and a permission mode. Extract whatever is present.
- Permission/confirmation dialog: a boxed prompt asking to allow an edit/command,
  often with choices like allow / allow-always / reject. Treat as a menu/question.
- Rewind picker: appears after Esc-Esc. A header about restoring code/conversation to
  a previous point, then a list of past user messages (each may have a change note
  like "No code changes" or a file/diff stat), one marked by a cursor, with "N more
  above/below" scroll markers. Capture it as `rewind`.
- Task/TODO list: a checklist of tasks the agent is tracking (done vs open).
- Working indicator: a whimsical gerund + elapsed time + tokens streamed.

Ignore editor/multiplexer chrome that is not the agent's state (vim's "-- INSERT --",
tmux mode footers).

Output ONLY this JSON (omit fields you cannot determine — do not guess):
{
  "tool": "claude"|"codex"|"gemini"|"shell"|"unknown",
  "activity": "running"|"waiting"|"idle",
  "status_line": "<=80 char summary of what is happening; prefer WHAT is being worked
     on over echoing a spinner word",
  "model": "<model name if shown>",
  "context_pct": <int 0-100 if shown>,
  "cost": "<e.g. $10.64 if shown>",
  "mode": "plan"|"accept-edits"|"bypass"|"normal",
  "working_verb": "<gerund if working>",
  "elapsed": "<e.g. 11m46s if shown>",
  "tokens": "<e.g. 13.3k if shown>",
  "agents": <int count of running sub-agents/parallel tasks if shown, else 0>,
  "question": { "prompt": "...", "options": ["...", ...] },   // only if truly waiting for input
  "rewind": {
    "entries": [ { "text": "...", "note": "...", "selected": true|false }, ... ],
    "more_above": <int>, "more_below": <int>
  }
}
Include "question" ONLY when the pane is genuinely blocked awaiting input (a real
menu/dialog/field), never for prose that merely contains a question mark or a list.
Include "rewind" ONLY when the restore-history picker is actually shown.
""".strip()


def _obvious_idle(text: str) -> bool:
    """Cheap check: does the pane tail look like a bare shell prompt? Lets the watcher
    skip the LLM for a clearly-idle shell. Not used for anything semantic."""
    for ln in reversed(text.splitlines()):
        if ln.strip():
            return bool(_SHELL_PROMPT_RE.search(ln))
    return False


def classify(pane: Pane, text: str, llm_fn=None) -> PaneState:
    """Build a PaneState for `pane`. `llm_fn(system, text) -> dict|None` is the parser
    (Gemini). If it's None or fails, we return a minimal heuristic state (idle vs
    running) — the LLM is the source of truth for everything semantic."""
    state = PaneState(pane_id=pane.id, label=pane.label)

    result = llm_fn(PARSER_PROMPT, text) if llm_fn else None
    if not isinstance(result, dict):
        # Fallback with no LLM: only the crude idle/running guess. No fake parsing.
        state.tool = "shell" if pane.current_command in ("bash", "zsh", "sh", "fish") else "unknown"
        state.activity = "idle" if _obvious_idle(text) else "running"
        return state

    _apply(state, result)
    return state


def _apply(state: PaneState, r: dict) -> None:
    """Map the LLM's JSON onto PaneState, validating enums and shapes."""
    if (t := r.get("tool")) in _TOOLS:
        state.tool = t
    if (a := r.get("activity")) in _ACTIVITIES:
        state.activity = a
    if s := r.get("status_line"):
        state.status_line = str(s)[:120]
    if m := r.get("model"):
        state.model = str(m)[:40]
    if isinstance(r.get("context_pct"), int) and 0 <= r["context_pct"] <= 100:
        state.context_pct = r["context_pct"]
    if c := r.get("cost"):
        state.cost = str(c)[:16]
    if (mode := r.get("mode")) in _MODES:
        state.mode = mode
    for field in ("working_verb", "elapsed", "tokens"):
        if v := r.get(field):
            setattr(state, field, str(v)[:24])
    if isinstance(r.get("agents"), int) and r["agents"] > 0:
        state.agents = r["agents"]

    q = r.get("question")
    if isinstance(q, dict) and q.get("prompt"):
        state.question = Question(
            prompt=str(q["prompt"])[:200],
            options=[str(o)[:120] for o in q.get("options", []) if o][:12],
        )
        state.activity = "waiting"

    rw = r.get("rewind")
    if isinstance(rw, dict) and isinstance(rw.get("entries"), list) and rw["entries"]:
        entries = [
            RewindEntry(
                text=str(e.get("text", ""))[:200],
                note=str(e.get("note", ""))[:60],
                selected=bool(e.get("selected")),
            )
            for e in rw["entries"]
            if isinstance(e, dict)
        ]
        state.rewind = Rewind(
            entries=entries,
            more_above=rw.get("more_above", 0) if isinstance(rw.get("more_above"), int) else 0,
            more_below=rw.get("more_below", 0) if isinstance(rw.get("more_below"), int) else 0,
        )
        state.activity = "waiting"
