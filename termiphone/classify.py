"""LLM-first classification, raw-JSON pipe.

The LLM is the parser AND the schema. We send the visible pane text to Gemini Flash
Lite with one layered prompt and pass its JSON straight through to the UI — no typed
schema in the middle. New prompt fields light up in the frontend with no backend
change. Research (see research/README.md) showed text-only beats image on accuracy,
cost, and latency across working / rewind-picker / error screens, so text is the
hot-path input; the image switch stays wired but off.

`classify` returns a plain dict: the model's JSON, plus watcher-managed fields
(pane_id, label, idle_seconds, snapshot_id, updated_at). On no/failed LLM it returns a
minimal dict (idle vs running) so the pipe never breaks.
"""

from __future__ import annotations

import re

from .tmux import Pane

# Cheap fast-path only (NOT semantic parsing): a bare shell prompt at the tail lets the
# watcher/fallback call an obviously-idle shell "idle" without an LLM call.
_SHELL_PROMPT_RE = re.compile(r"[\w.-]+@[\w.-]+.*[$#]\s*$")

# The production parser prompt. Validated in research/probe.py. General TUI patterns
# first, then a Claude Code section described semantically (not by exact strings), and
# a JSON shape the UI renders directly. Static ⇒ hits Gemini's context cache.
PARSER_PROMPT = """
You are the parser behind a phone dashboard that mirrors what's happening in a
developer's terminal panes so they can watch and control coding agents from their
phone. You are given one or more recent snapshots of ONE terminal pane as text, in
time order and labeled ("[earlier frame -N]" … "[current frame]"). Report the state
for the CURRENT frame; use the earlier frames only for continuity (stay consistent as
output trickles in — don't re-decide from scratch) and to describe what just happened
in "notable". A pane is a fixed-width character grid; treat alignment/columns as
meaningful.

Describe what is happening in a way that lets the phone UI light up usefully. Reason
about GENERAL terminal patterns by how they look/behave, NOT by exact wording (which
changes across versions and tools):
- Is the pane actively working (a spinner, a "thinking" word, an elapsed timer,
  streaming output), waiting for the user to answer something, or idle at a prompt?
- Is there a SELECTION MENU — a list of choices with one marked by a cursor/highlight
  (❯, ›, >, an arrow, a bold or colored line) that you'd move through with up/down?
- Is there an INPUT PROMPT — a yes/no confirmation, a numbered choice, or a free-text
  field the user must fill?

This is very often CLAUDE CODE (an AI coding agent). Recognize it by its style: a
rounded input box drawn with box characters, and a two-line STATUS LINE near the
bottom that typically shows — in order — the working directory and git branch, the
model name (e.g. "Opus 4.8"), a context-window percentage, an elapsed session time, a
session cost in dollars, and counts of prompts/tools; plus a permission-mode indicator
("plan mode", "accept edits", "bypass permissions"). While working it shows a whimsical
gerund + elapsed time + tokens streamed (e.g. "Shimmying… (6m 55s · ↓18.1k tokens)").
After Esc-Esc it shows a REWIND picker: a header about restoring to a previous point,
then a scrollable list of past user messages (each may note "No code changes" or a
file/diff stat like "app.js +18 -8"), one selected, with "N more above/below" markers.
It may also show a task/TODO checklist. Ignore editor/multiplexer chrome that isn't the
agent's own state (vim's "-- INSERT --", tmux's mode footer).

Return ONLY compact JSON. Include a field only if you can determine it — do not guess:
{
  "tool": "claude"|"codex"|"gemini"|"shell"|"unknown",
  "activity": "running"|"waiting"|"idle",
  "headline": "one short human sentence for the top of the card: what is happening / "
              "what is being worked on (prefer the task over echoing a spinner word)",
  "model": "...", "context_pct": <int>, "cost": "$...", "mode": "plan|accept-edits|bypass|normal",
  "working": {"verb": "...", "elapsed": "...", "tokens": "..."},
  "question": {"prompt": "...", "options": ["..."]},
  "rewind": {"entries": [{"text": "...", "note": "...", "selected": true}], "more_above": <int>, "more_below": <int>},
  "tasks": [{"text": "...", "done": true|false}],
  "notable": ["short bullets of anything else useful to show — errors, test results, "
              "files changed, what just happened, etc."]
}
"notable" is ONLY for things not already captured in a dedicated field above. Do NOT
restate the mode, model, cost, context %, or tool there (those have their own UI) —
e.g. never add a bullet like "session is in bypass permissions mode". Keep notable for
new/interesting events the other fields don't cover.
Include "question" only when genuinely blocked awaiting input; "rewind" only when the
restore picker is shown; "tasks" only when a checklist is visible.
""".strip()


def _obvious_idle(text: str) -> bool:
    for ln in reversed(text.splitlines()):
        if ln.strip():
            return bool(_SHELL_PROMPT_RE.search(ln))
    return False


def _with_prior(text: str, prior: list[str]) -> str:
    """Prepend recent prior captures (oldest→newest) as labeled context, so the model
    sees the trajectory. This keeps classification STABLE when content trickles in one
    line at a time (no re-deciding from scratch each frame → no flicker) and gives it
    material to describe what JUST happened. Prompt tokens are cheap, so this is nearly
    free. The LAST block is the current screen — the one to report state for."""
    if not prior:
        return text
    blocks = [f"[earlier frame -{len(prior) - i}]\n{p}" for i, p in enumerate(prior)]
    blocks.append(f"[current frame — report state for THIS one]\n{text}")
    return "\n\n".join(blocks)


def classify(pane: Pane, text: str, llm_fn=None, prior: list[str] | None = None) -> dict:
    """Parse `pane` into a plain dict for the UI. `llm_fn(system, text) -> dict|None`
    is the Gemini parser. `prior` is recent prior captures (oldest→newest) for
    continuity. Returns the model's JSON with pane_id/label merged in; on no/failed
    LLM, a minimal heuristic dict (idle vs running) so the pipe never breaks."""
    result = llm_fn(PARSER_PROMPT, _with_prior(text, prior or [])) if llm_fn else None
    if not isinstance(result, dict):
        result = {
            "tool": "shell" if pane.current_command in ("bash", "zsh", "sh", "fish") else "unknown",
            "activity": "idle" if _obvious_idle(text) else "running",
        }
    # A detected question/rewind means the pane is waiting, regardless of what the
    # model put in "activity" — this is the one bit of logic we keep out of the model.
    if result.get("question") or result.get("rewind"):
        result["activity"] = "waiting"
    result["pane_id"] = pane.id
    result["label"] = pane.label
    return result
